import json

import pytest
import requests

from FRS.knowledge.observation import Observation
from FRS.knowledge.tracing import (
    TraceExportError,
    TraceSettings,
    export_trace,
    preview_trace,
)


def record():
    obs = Observation("a" * 64)
    for stage in ["retrieve", "generate", "validate"]:
        with obs.span(stage) as span:
            if stage == "generate":
                span.input_tokens, span.output_tokens = 10, 5
    return obs.to_dict()


def settings():
    return TraceSettings(
        "https://api.smith.langchain.com", "frs-test", "unit-test-secret"
    )


def response(status=200):
    r = requests.Response()
    r.status_code = status
    r._content = b"{}"
    r.request = requests.Request(
        "POST", "https://api.smith.langchain.com/runs"
    ).prepare()
    return r


def test_disabled_by_default_even_when_environment_enables_tracing(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "do-not-send")
    assert export_trace(record()) == dict(status="disabled", attempts=0, sent=0)


def test_actual_sdk_payloads_have_only_approved_metadata_and_hierarchy():
    calls = []
    pending = []

    def sender(method, url, **kwargs):
        assert len(pending) == len(calls) + 1
        body = json.loads(kwargs["data"])
        calls.append(body)
        assert method == "POST" and url == "https://api.smith.langchain.com/runs"
        assert kwargs["allow_redirects"] is False
        return response()

    r = export_trace(
        record(),
        enabled=True,
        settings=settings(),
        sender=sender,
        before_send=lambda body: pending.append(body),
    )
    assert r == dict(status="sent", attempts=4, sent=4)
    assert len(calls) == 4 and calls[0]["parent_run_id"] is None
    assert all(c["parent_run_id"] == calls[0]["id"] for c in calls[1:])
    assert all(c["inputs"] == c["outputs"] == {} for c in calls)
    assert all(set(c["extra"]) == {"metadata"} for c in calls)
    assert "unit-test-secret" not in json.dumps(calls)
    assert [c["run_type"] for c in calls] == ["chain", "retriever", "chain", "parser"]


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_sdk_export_failure_stops_without_retry(status):
    calls = []

    def sender(*args, **kwargs):
        calls.append(1)
        return response(status)

    r = export_trace(record(), enabled=True, settings=settings(), sender=sender)
    assert r["status"] == "failed" and r["attempts"] == 1 and r["sent"] == 0
    assert len(calls) == 1


def test_connection_failure_is_single_attempt():
    calls = []

    def sender(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError("secret")

    r = export_trace(record(), enabled=True, settings=settings(), sender=sender)
    assert r == dict(
        status="failed", attempts=1, sent=0, error_code="trace_export_error"
    )
    assert len(calls) == 1


def test_partial_export_is_not_claimed_complete():
    calls = []

    def sender(*args, **kwargs):
        calls.append(1)
        return response(200 if len(calls) < 3 else 429)

    r = export_trace(record(), enabled=True, settings=settings(), sender=sender)
    assert r["sent"] == 2 and r["attempts"] == 3 and r["status"] == "failed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("question", "raw question"),
        ("corpus_version", "private path"),
        ("status", "raw exception"),
        ("trace_id", "invalid"),
    ],
)
def test_invalid_observation_cannot_enter_payload(field, value):
    r = record()
    r[field] = value
    with pytest.raises(TraceExportError):
        preview_trace(r)


def test_arbitrary_span_fields_or_bad_time_or_nan_rejected():
    for field, value in [
        ("answer", "secret"),
        ("elapsed_ms", float("nan")),
        ("input_tokens", -1),
        ("finished_at", "yesterday"),
        ("status", "exception text"),
    ]:
        r = record()
        r["spans"][0][field] = value
        with pytest.raises(TraceExportError):
            preview_trace(r)


def test_missing_configuration_and_insufficient_budget_fail_before_transmission():
    with pytest.raises(TraceExportError):
        export_trace(record(), enabled=True)
    with pytest.raises(TraceExportError):
        export_trace(record(), enabled=True, settings=settings(), max_attempts=3)
    assert "unit-test-secret" not in repr(settings())


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.smith.langchain.com",
        "https://example.com",
        "https://api.smith.langchain.com/path",
    ],
)
def test_only_explicit_known_trace_endpoints_allowed(endpoint):
    with pytest.raises(TraceExportError):
        TraceSettings(endpoint, "frs", "key")


def test_metadata_cannot_be_modified_after_preview(monkeypatch):
    import FRS.knowledge.tracing as tracing

    class AlteringClient:
        def __init__(self, **kwargs):
            self.session = kwargs["session"]

        def create_run(self, project_name, **run):
            run["extra"]["metadata"]["status"] = "private data"
            self.session.request(
                "POST",
                "https://api.smith.langchain.com/runs",
                data=json.dumps(dict(run, session_name=project_name)),
            )

        def close(self):
            pass

    monkeypatch.setattr(tracing, "Client", AlteringClient)
    result = export_trace(
        record(),
        enabled=True,
        settings=settings(),
        sender=lambda *a, **k: pytest.fail("must not send"),
    )
    assert result["status"] == "failed" and result["attempts"] == 0


def test_sdk_constructor_failure_is_safe_and_sends_nothing(monkeypatch):
    import FRS.knowledge.tracing as tracing

    def fail(**kwargs):
        raise ValueError("private config message")

    monkeypatch.setattr(tracing, "Client", fail)
    result = export_trace(record(), enabled=True, settings=settings())
    assert result == dict(
        status="failed", attempts=0, sent=0, error_code="trace_export_error"
    )


def test_generation_kind_does_not_mislabel_oracle_as_actual_llm():
    obs = Observation("a" * 64, generator_kind="ollama_qwen3_8b")
    for stage in ["retrieve", "generate", "validate"]:
        with obs.span(stage):
            pass
    p = preview_trace(obs.to_dict())
    assert p["runs"][2]["run_type"] == "llm"
    assert p["runs"][0]["extra"]["metadata"]["generator_kind"] == "ollama_qwen3_8b"
    oracle = record()
    assert preview_trace(oracle)["runs"][2]["run_type"] == "chain"


def test_expected_insufficient_evidence_is_not_a_langsmith_technical_error():
    obs = Observation("a" * 64)
    with obs.span("retrieve") as span:
        span.status = obs.status = "insufficient_evidence"
    preview = preview_trace(obs.to_dict())
    assert all(r["error"] is None for r in preview["runs"])
    assert preview["runs"][0]["extra"]["metadata"]["status"] == "insufficient_evidence"


def test_legacy_langchain_configuration_is_supported_without_copying_key(monkeypatch):
    for name in ["LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_PROJECT"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGCHAIN_API_KEY", "legacy-secret")
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")
    monkeypatch.setenv("LANGCHAIN_PROJECT", "legacy-project")
    settings = TraceSettings.from_env()
    assert settings.project == "legacy-project" and settings.api_key == "legacy-secret"
    assert "legacy-secret" not in repr(settings)
    assert export_trace(record())["attempts"] == 0


def test_conflicting_current_and_legacy_settings_are_rejected(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "one-secret")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "another-secret")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")
    monkeypatch.setenv("LANGSMITH_PROJECT", "test")
    monkeypatch.setenv("LANGCHAIN_PROJECT", "test")
    with pytest.raises(TraceExportError, match="conflict"):
        TraceSettings.from_env()
