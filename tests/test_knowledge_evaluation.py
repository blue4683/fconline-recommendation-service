import json

import httpx2
import pytest

import FRS.knowledge.evaluate as evaluation
from FRS.agent.ollama_transport import ChatSample, OllamaExtractionError
from FRS.knowledge.tracing import TraceSettings, preview_trace


def prepare(monkeypatch, tmp_path, *, model_error=False, invalid_output=False):
    root = tmp_path / "approval"
    monkeypatch.setattr(evaluation, "APPROVAL_ROOT", root)
    original = evaluation.subprocess.check_output
    monkeypatch.setattr(
        evaluation.subprocess,
        "check_output",
        lambda args, **kwargs: "" if "status" in args else original(args, **kwargs),
    )

    def metadata(request):
        d = json.loads((root / evaluation.RUN_ID / "manifest.json").read_text())
        assert d["metadata_attempts"][-1]["status"] == "pending"
        if request.url.path == "/api/version":
            return httpx2.Response(200, json={"version": "test-version"})
        return httpx2.Response(
            200,
            json={"models": [{"name": "qwen3:8b", "digest": evaluation.MODEL_DIGEST}]},
        )

    monkeypatch.setattr(
        evaluation.httpx2,
        "HTTPTransport",
        lambda **kwargs: httpx2.MockTransport(metadata),
    )
    by_question = {c["question"]: c for c in evaluation.cases()}

    class Generator:
        def generate(self, question, hits, version):
            d = json.loads((root / evaluation.RUN_ID / "manifest.json").read_text())
            assert len(d["metadata_attempts"]) == 2
            assert d["inference_attempts"][-1]["status"] == "pending"
            if model_error:
                raise OllamaExtractionError("connection_error", stop=True)
            c = by_question[question]
            content = (
                "invalid"
                if invalid_output and c["case_id"] == "K01"
                else json.dumps(
                    dict(
                        status=c["status"],
                        answer=c["oracle"],
                        source_ids=[c["source_id"]] if c["source_id"] else [],
                    )
                )
            )
            return ChatSample(content, 0.1, ())

    monkeypatch.setattr(evaluation, "OllamaAnswerGenerator", Generator)
    return root


def test_offline_is_oracle_not_actual_model_metrics():
    d = evaluation.offline_evaluate()
    assert d["passed"] == d["total"] == 7
    assert d["actual_model_requests"] == d["actual_trace_requests"] == 0
    assert all(c["observation"]["generator_kind"] == "oracle" for c in d["cases"])
    assert all(r["run_type"] != "llm" for c in d["cases"] for r in c["preview"]["runs"])
    assert all(c["checks"]["retrieval_hit"] is not False for c in d["cases"])


def test_mock_live_run_reserves_attempts_and_cannot_repeat(monkeypatch, tmp_path):
    root = prepare(monkeypatch, tmp_path)
    result = evaluation.run_live()
    assert result["status"] == "completed"
    assert result["summary"] == dict(
        total=7,
        executed=7,
        answer_checks_passed=7,
        model_requests=6,
        metadata_requests=2,
        trace_requests=0,
    )
    assert (root / "approval-consumed.lock").read_text() == evaluation.RUN_ID
    before = (root / evaluation.RUN_ID / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        evaluation.run_live()
    assert (root / evaluation.RUN_ID / "manifest.json").read_bytes() == before


def test_model_connection_failure_stops_remaining_cases(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path, model_error=True)
    result = evaluation.run_live()
    assert result["status"] == "stopped"
    assert (
        result["summary"]["executed"] == 1 and result["summary"]["model_requests"] == 1
    )
    assert result["cases"][0]["result"]["status"] == "provider_error"


def test_invalid_model_output_counts_failure_without_retry(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path, invalid_output=True)
    result = evaluation.run_live()
    assert result["status"] == "completed"
    assert result["summary"]["answer_checks_passed"] == 6
    assert result["summary"]["model_requests"] == 6
    assert result["cases"][0]["result"]["error_code"] == "invalid_output"


def test_metadata_failure_stops_before_any_inference(monkeypatch, tmp_path):
    root = prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(
        evaluation.httpx2,
        "HTTPTransport",
        lambda **kwargs: httpx2.MockTransport(lambda request: httpx2.Response(403)),
    )
    result = evaluation.run_live()
    assert result["status"] == "stopped"
    assert len(result["metadata_attempts"]) == 1
    assert result["summary"]["model_requests"] == 0
    assert (root / "approval-consumed.lock").exists()


def test_dirty_sources_stop_before_consuming_approval(monkeypatch, tmp_path):
    root = prepare(monkeypatch, tmp_path)
    original = evaluation.subprocess.check_output
    monkeypatch.setattr(
        evaluation.subprocess,
        "check_output",
        lambda args, **kwargs: (
            " M source.py" if "status" in args else original(args, **kwargs)
        ),
    )
    with pytest.raises(ValueError, match="commit_sources"):
        evaluation.run_live()
    assert not root.exists()


def test_trace_failure_stops_future_model_calls(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(
        TraceSettings,
        "from_env",
        classmethod(
            lambda cls: TraceSettings(
                "https://api.smith.langchain.com", "test", "fake-secret"
            )
        ),
    )

    def export(record, **kwargs):
        kwargs["before_send"](preview_trace(record)["runs"][0])
        return dict(status="failed", attempts=1, sent=0)

    monkeypatch.setattr(evaluation, "export_trace", export)
    result = evaluation.run_live(upload=True)
    assert result["status"] == "stopped" and result["summary"]["model_requests"] == 1
    assert result["summary"]["trace_requests"] == 1
    assert result["trace_attempts"][0]["status"] == "failed_or_unknown"


def test_trace_success_budget_and_one_confirmation(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(
        TraceSettings,
        "from_env",
        classmethod(
            lambda cls: TraceSettings(
                "https://api.smith.langchain.com", "test", "fake-secret"
            )
        ),
    )

    def export(record, **kwargs):
        runs = preview_trace(record)["runs"]
        for run in runs:
            kwargs["before_send"](run)
        return dict(status="sent", attempts=len(runs), sent=len(runs))

    def confirm(trace_id, settings, journal):
        assert trace_id == journal.data["cases"][0]["observation"]["trace_id"]
        r = journal.reserve("trace_attempts", 29, kind="read", run_id=trace_id)
        r.update(status="response_received", confirmed=True, http_status=200)
        journal.save()

    monkeypatch.setattr(evaluation, "export_trace", export)
    monkeypatch.setattr(evaluation, "_confirm_first_trace", confirm)
    result = evaluation.run_live(upload=True)
    assert result["status"] == "completed" and result["summary"]["trace_requests"] == 27
    assert result["trace_attempts"][-1]["confirmed"] is True
    assert "fake-secret" not in json.dumps(result)


def test_unknown_attempt_counts_against_budget(tmp_path):
    journal = evaluation.Journal(tmp_path / "manifest.json", {})
    journal.reserve("inference_attempts", 1, case_id="K01")
    with pytest.raises(RuntimeError):
        journal.reserve("inference_attempts", 1, case_id="K01")
    assert len(journal.data["inference_attempts"]) == 1
    assert journal.data["inference_attempts"][0]["status"] == "pending"


@pytest.mark.parametrize(
    "status,payload,valid",
    [
        (
            200,
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "app_path": "/o/example/project",
            },
            True,
        ),
        (302, {}, False),
        (403, {}, False),
        (429, {}, False),
        (200, {"id": "wrong"}, False),
        (
            200,
            {"id": "00000000-0000-4000-8000-000000000001", "secret": "not-stored"},
            True,
        ),
    ],
)
def test_root_confirmation_is_one_read_with_safe_fields(
    monkeypatch, tmp_path, status, payload, valid
):
    trace_id = "00000000-0000-4000-8000-000000000001"
    journal = evaluation.Journal(tmp_path / "manifest.json", {})
    settings = TraceSettings("https://api.smith.langchain.com", "test", "fake-key")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == settings.endpoint + "/runs/" + trace_id
        return httpx2.Response(status, json=payload)

    original = evaluation.httpx2.Client
    monkeypatch.setattr(
        evaluation.httpx2,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx2.MockTransport(handler)),
    )
    if valid:
        evaluation._confirm_first_trace(trace_id, settings, journal)
        assert journal.data["trace_attempts"][0]["confirmed"] is True
    else:
        with pytest.raises(ValueError):
            evaluation._confirm_first_trace(trace_id, settings, journal)
    assert len(calls) == 1
    assert "not-stored" not in json.dumps(journal.data)
