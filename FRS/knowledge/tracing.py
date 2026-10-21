"""허용 metadata만 만드는 LangSmith SDK 단일 전송. 기본은 비활성이다."""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from uuid import UUID, uuid5

import requests
from langsmith import Client
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from FRS.knowledge.observation import STAGES, STATUSES

ENDPOINTS = ("https://api.smith.langchain.com", "https://eu.api.smith.langchain.com")


class TraceExportError(ValueError):
    pass


@dataclass(frozen=True)
class TraceSettings:
    endpoint: str
    project: str
    api_key: str = field(repr=False)

    def __post_init__(self):
        if (
            not isinstance(self.project, str)
            or self.endpoint not in ENDPOINTS
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.project)
        ):
            raise TraceExportError("trace_configuration_error")
        if (
            not isinstance(self.api_key, str)
            or not self.api_key
            or len(self.api_key) > 1000
        ):
            raise TraceExportError("trace_configuration_error")

    @classmethod
    def from_env(cls):
        def setting(name, legacy):
            current, old = os.environ.get(name, ""), os.environ.get(legacy, "")
            if current and old and current != old:
                raise TraceExportError("trace_configuration_conflict")
            return current or old

        return cls(
            setting("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT"),
            setting("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"),
            setting("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"),
        )


def _checked_time(value):
    try:
        time = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise TraceExportError("invalid_observation") from None
    if time.tzinfo is None or time.utcoffset() is None:
        raise TraceExportError("invalid_observation")
    return time


def preview_trace(record):
    """임의 원문을 받지 않고 관측의 허용 필드만 재구성한다."""
    if not isinstance(record, dict) or set(record) != {
        "schema_version",
        "trace_id",
        "corpus_version",
        "generator_kind",
        "status",
        "spans",
    }:
        raise TraceExportError("invalid_observation")
    try:
        root = str(UUID(record["trace_id"]))
    except (TypeError, ValueError, AttributeError):
        raise TraceExportError("invalid_observation") from None
    if (
        record["generator_kind"] not in ("test_double", "oracle", "ollama_qwen3_8b")
        or record["schema_version"] != "knowledge-observation.v1"
        or not isinstance(record["corpus_version"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["corpus_version"])
        or record["status"] not in STATUSES
    ):
        raise TraceExportError("invalid_observation")
    spans = record["spans"]
    if not isinstance(spans, list) or not 1 <= len(spans) <= 3:
        raise TraceExportError("invalid_observation")
    expected = {
        "stage",
        "started_at",
        "finished_at",
        "elapsed_ms",
        "status",
        "input_tokens",
        "output_tokens",
    }
    checked = []
    for span in spans:
        if (
            not isinstance(span, dict)
            or set(span) != expected
            or span["stage"] not in STAGES
            or span["status"] not in STATUSES
        ):
            raise TraceExportError("invalid_observation")
        start, end = (
            _checked_time(span["started_at"]),
            _checked_time(span["finished_at"]),
        )
        if end < start or (checked and start < checked[-1][2]):
            raise TraceExportError("invalid_observation")
        if (
            type(span["elapsed_ms"]) not in (int, float)
            or not isfinite(span["elapsed_ms"])
            or span["elapsed_ms"] < 0
        ):
            raise TraceExportError("invalid_observation")
        if any(
            x is not None and (type(x) is not int or not 0 <= x <= 2**63 - 1)
            for x in (span["input_tokens"], span["output_tokens"])
        ):
            raise TraceExportError("invalid_observation")
        checked.append((span, start, end))
    if tuple(x[0]["stage"] for x in checked) != STAGES[: len(checked)]:
        raise TraceExportError("invalid_observation")
    order = checked[0][1].strftime("%Y%m%dT%H%M%S%fZ") + root

    def make(name, run_id, start, end, status, metadata, parent=None):
        return dict(
            id=run_id,
            trace_id=root,
            name=name,
            run_type={
                "retrieve": "retriever",
                "generate": "llm"
                if record["generator_kind"] == "ollama_qwen3_8b"
                else "chain",
                "validate": "parser",
            }.get(name, "chain"),
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            dotted_order=order
            if parent is None
            else order + "." + start.strftime("%Y%m%dT%H%M%S%fZ") + run_id,
            parent_run_id=parent,
            inputs={},
            outputs={},
            error=None if status in ("ok", "insufficient_evidence") else status,
            extra={"metadata": metadata},
        )

    payloads = [
        make(
            "knowledge-qa",
            root,
            checked[0][1],
            checked[-1][2],
            record["status"],
            {
                "status": record["status"],
                "corpus_version": record["corpus_version"],
                "generator_kind": record["generator_kind"],
            },
        )
    ]
    for span, start, end in checked:
        meta = {
            k: span[k]
            for k in ("stage", "elapsed_ms", "status", "input_tokens", "output_tokens")
        }
        payloads.append(
            make(
                span["stage"],
                str(uuid5(UUID(root), span["stage"])),
                start,
                end,
                span["status"],
                meta,
                parent=root,
            )
        )
    return dict(
        schema_version="langsmith-preview.v1", record_count=len(payloads), runs=payloads
    )


class TraceSession(requests.Session):
    def __init__(
        self, settings, max_attempts, expected_runs, *, sender=None, before_send=None
    ):
        super().__init__()
        if type(max_attempts) is not int or not 1 <= max_attempts <= 64:
            raise TraceExportError("invalid_request_budget")
        self.trust_env = False
        self.mount("https://", HTTPAdapter(max_retries=Retry(total=0)))
        self.settings, self.max_attempts = settings, max_attempts
        self.expected_runs = {
            str(run["id"]): json.loads(
                json.dumps(dict(run, session_name=settings.project))
            )
            for run in expected_runs
        }
        self.attempts, self.last_status = 0, None
        self._sender, self.before_send = sender, before_send

    def request(self, method, url, **kwargs):
        if (
            method.upper() != "POST"
            or url != self.settings.endpoint + "/runs"
            or self.attempts >= self.max_attempts
        ):
            raise TraceExportError("trace_request_boundary")
        try:
            body = json.loads(kwargs.get("data", ""))
        except (ValueError, TypeError):
            raise TraceExportError("invalid_trace_payload") from None
        if body != self.expected_runs.get(str(body.get("id"))):
            raise TraceExportError("invalid_trace_payload")
        allowed = {
            "id",
            "trace_id",
            "name",
            "run_type",
            "start_time",
            "end_time",
            "dotted_order",
            "parent_run_id",
            "inputs",
            "outputs",
            "error",
            "extra",
            "session_name",
        }
        if (
            set(body) != allowed
            or body["inputs"]
            or body["outputs"]
            or body["session_name"] != self.settings.project
        ):
            raise TraceExportError("invalid_trace_payload")
        if body["name"] not in ("knowledge-qa", *STAGES) or body["error"] not in (
            None,
            *STATUSES,
        ):
            raise TraceExportError("invalid_trace_payload")
        metadata = body["extra"]
        keys = (
            {"status", "corpus_version", "generator_kind"}
            if body["name"] == "knowledge-qa"
            else {"stage", "elapsed_ms", "status", "input_tokens", "output_tokens"}
        )
        if set(metadata) != {"metadata"} or set(metadata["metadata"]) != keys:
            raise TraceExportError("invalid_trace_payload")
        # SDK가 전송 직전 metadata를 자동 확장해도 여기서 차단한다.
        self.attempts += 1
        self.last_status = None
        if self.before_send:
            self.before_send(body)
        kwargs.update(allow_redirects=False, timeout=(5, 10))
        response = (
            self._sender(method, url, **kwargs)
            if self._sender
            else super().request(method, url, **kwargs)
        )
        self.last_status = response.status_code
        if not 200 <= response.status_code < 300:
            raise TraceExportError("trace_http_error")
        return response


def export_trace(
    record,
    *,
    enabled=False,
    settings=None,
    max_attempts=4,
    sender=None,
    before_send=None,
):
    if enabled is not True:
        return dict(status="disabled", attempts=0, sent=0)
    if type(settings) is not TraceSettings:
        raise TraceExportError("trace_configuration_error")
    preview = preview_trace(record)
    if preview["record_count"] > max_attempts:
        raise TraceExportError("invalid_request_budget")
    session = TraceSession(
        settings, max_attempts, preview["runs"], sender=sender, before_send=before_send
    )
    sent = 0
    client = None
    try:
        client = Client(
            api_key=settings.api_key,
            api_urls={settings.endpoint: settings.api_key},
            session=session,
            auto_batch_tracing=False,
            hide_inputs=True,
            hide_outputs=True,
            omit_traced_runtime_info=True,
            tracing_mode="langsmith",
            tracing_sampling_rate=1.0,
            retry_config=Retry(total=0),
            timeout_ms=10000,
        )
        for run in preview["runs"]:
            client.create_run(project_name=settings.project, **run)
            if session.last_status is None or not 200 <= session.last_status < 300:
                raise TraceExportError("trace_unconfirmed")
            sent += 1
    except Exception:
        return dict(
            status="failed",
            attempts=session.attempts,
            sent=sent,
            error_code="trace_export_error",
        )
    finally:
        if client is not None:
            client.close()
        session.close()
    return dict(status="sent", attempts=session.attempts, sent=sent)
