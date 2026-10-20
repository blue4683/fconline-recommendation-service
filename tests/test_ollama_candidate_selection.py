"""외부/실제 모델 호출 없이 ID 선택과 1회 실행 경계를 검증한다."""

import asyncio
import json
from unittest.mock import Mock

import httpx2
import pytest
from langsmith import get_tracing_context

from FRS.agent.candidate_comparison import (
    candidate_cases,
    comparison_plan,
    main,
    run_comparison,
)
from FRS.agent.candidate_selection import CandidateSelectionError, decode_selection
from FRS.agent.candidates import FIELDS, prepare_candidates
from FRS.agent.comparison import LocalMetadataError
from FRS.agent.ollama_selection import (
    OllamaIDSelector,
    selection_request,
    selector_spec,
)
from FRS.agent.ollama_transport import (
    MAX_RESPONSE_BYTES,
    MODELS,
    ChatSample,
    OllamaExtractionError,
)

CASE = candidate_cases()[0]
REGISTRY = prepare_candidates(CASE.original).registry


def answer(registry):
    return json.dumps(
        dict(
            candidate_set_id=registry.candidate_set_id,
            **{
                field: [v.candidate_id for v in registry.candidates if v.field == field]
                for field in FIELDS
            },
            unsupported=[],
        )
    )


def envelope(content=None, **changes):
    result = dict(
        model=MODELS[0],
        done=True,
        done_reason="stop",
        message=dict(
            role="assistant", content=content or answer(REGISTRY), thinking="비공개"
        ),
        eval_count=17,
        load_duration=300,
        prompt_eval_count=True,
        eval_duration=-1,
    )
    return {**result, **changes}


def test_id_transport_and_decoder_restore_manual_source_without_offsets(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    calls = []

    def handle(request):
        assert get_tracing_context()["enabled"] is False
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        calls.append(request)
        body = json.loads(request.content)
        assert body == selection_request(MODELS[0], REGISTRY)
        data = json.loads(body["messages"][1]["content"])
        assert data["original"] == CASE.original
        assert "start" not in data["candidates"][0]
        assert body["format"]["properties"]["candidate_set_id"]["enum"] == [
            REGISTRY.candidate_set_id
        ]
        assert body["think"] is False and body["keep_alive"] == 0
        return httpx2.Response(200, json=envelope())

    selector = OllamaIDSelector(MODELS[0], transport=httpx2.MockTransport(handle))
    sample = selector.sample(REGISTRY)
    assert decode_selection(CASE.original, REGISTRY, sample.content) == CASE.expected
    assert dict(sample.metrics) == {"load_duration": 300, "eval_count": 17}
    assert "비공개" not in repr(sample) and len(calls) == 1


def test_both_model_requests_differ_only_in_model_and_specs_are_copied():
    first = selection_request(MODELS[0], REGISTRY)
    second = selection_request(MODELS[1], REGISTRY)
    assert first.pop("model") != second.pop("model")
    assert first == second
    first["options"]["seed"] = 999
    first["format"]["properties"]["price_cap"] = {}
    assert selection_request(MODELS[0], REGISTRY)["options"]["seed"] == 123
    assert selector_spec()["options"]["seed"] == 123
    assert comparison_plan() == comparison_plan()


@pytest.mark.parametrize("model", ["qwen2.5:0.5b", "remote-model", None])
def test_unapproved_models_never_call_http(model):
    with pytest.raises(ValueError):
        OllamaIDSelector(model)


def test_tampered_registry_rejected_before_http():
    transport = Mock(side_effect=AssertionError("HTTP 금지"))
    broken = REGISTRY.model_copy(update={"original": "예산 2조"})
    with pytest.raises(CandidateSelectionError):
        OllamaIDSelector(MODELS[0], transport=transport).select(broken)
    transport.assert_not_called()


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_http_error_stops_without_redirect_or_retry(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx2.Response(status, headers={"location": "https://example.org"})

    with pytest.raises(OllamaExtractionError) as error:
        OllamaIDSelector(MODELS[0], transport=httpx2.MockTransport(handle)).select(
            REGISTRY
        )
    assert error.value.stop and error.value.code == "http_error" and len(calls) == 1


@pytest.mark.parametrize(
    "change", [dict(done=False), dict(model="wrong"), dict(message={})]
)
def test_incomplete_or_mismatched_envelope_is_unknown_server_state(change):
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(200, json=envelope(**change))
    )
    with pytest.raises(OllamaExtractionError) as error:
        OllamaIDSelector(MODELS[0], transport=transport).select(REGISTRY)
    assert error.value.stop and error.value.code == "invalid_envelope"


@pytest.mark.parametrize(
    "raw", [b"NaN", b'{"done":true,"done":true}', b"\xff", b"[" * 2000]
)
def test_invalid_envelope_bytes_are_safe_and_terminal(raw):
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200, content=raw))
    with pytest.raises(OllamaExtractionError) as error:
        OllamaIDSelector(MODELS[0], transport=transport).select(REGISTRY)
    assert error.value.stop and error.value.code == "invalid_envelope"


def test_response_limit_is_terminal():
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    )
    with pytest.raises(OllamaExtractionError) as error:
        OllamaIDSelector(MODELS[0], transport=transport).select(REGISTRY)
    assert error.value.stop and error.value.code == "response_too_large"


def test_total_deadline_and_connect_error_are_terminal(monkeypatch):
    real = asyncio.timeout
    monkeypatch.setattr(
        "FRS.agent.ollama_transport.asyncio.timeout", lambda _: real(0.001)
    )

    async def delayed(request):
        await asyncio.sleep(0.03)
        return httpx2.Response(200, json=envelope())

    with pytest.raises(OllamaExtractionError) as error:
        OllamaIDSelector(MODELS[0], transport=httpx2.MockTransport(delayed)).select(
            REGISTRY
        )
    assert error.value.stop and error.value.code == "deadline"

    def refused(request):
        raise httpx2.ConnectError("private")

    with pytest.raises(OllamaExtractionError) as error:
        OllamaIDSelector(MODELS[0], transport=httpx2.MockTransport(refused)).select(
            REGISTRY
        )
    assert error.value.stop and error.value.code == "connection_error"
    assert "private" not in str(error.value)


def test_fixed_cases_and_manual_ground_truth_preserve_repeated_occurrences():
    plan = comparison_plan()
    assert len(plan["cases"]) == 7 and plan["inference_limit"] == 12
    assert [r["expected_preparation"] for r in plan["cases"]].count("ready") == 6
    for case in candidate_cases():
        preparation = prepare_candidates(case.original)
        if preparation.issues:
            assert case.case_id == "unsupported"
            continue
        assert (
            decode_selection(
                case.original, preparation.registry, answer(preparation.registry)
            )
            == case.expected
        )
    spans = candidate_cases()[-1].expected.price_cap
    assert spans[0].text == spans[1].text and spans[0].start != spans[1].start


def success_factory(root, run_id, calls):
    def factory(model):
        def sample(registry):
            saved = json.loads((root / run_id / "manifest.json").read_text())
            assert saved["attempts"][-1]["status"] == "pending"
            assert saved["attempts"][-1]["number"] == len(calls) + 1
            assert saved["plan"]["plan_sha256"]
            calls.append((model, registry.original))
            return ChatSample(answer(registry), 0.2, ())

        return Mock(sample=sample)

    return factory


def test_pending_before_model_skip_preparation_fail_and_approval_cannot_reuse(tmp_path):
    calls = []
    result = run_comparison(
        tmp_path,
        "test",
        factory=success_factory(tmp_path, "test", calls),
        metadata=lambda: {},
    )
    assert (
        result["status"] == "completed"
        and len(result["attempts"]) == 12
        and len(calls) == 12
    )
    assert len(result["cases"]) == 14
    blocked = [c for c in result["cases"] if c["status"] == "input_issues"]
    assert len(blocked) == 2 and all(
        not c["model_called"] and "attempt_number" not in c for c in blocked
    )
    assert all(
        a["assessment"]["evidence_exact"] and a["assessment"]["semantics_match"]
        for a in result["attempts"]
    )
    missing = [a for a in result["attempts"] if a["case_id"] == "missing"]
    assert all(
        a["assessment"]["actual"]["status"] == "needs_clarification" for a in missing
    )
    for name in ("test", "new"):
        with pytest.raises(FileExistsError):
            run_comparison(tmp_path, name, factory=Mock(), metadata=Mock())
    assert len(calls) == 12


@pytest.mark.parametrize(
    "content",
    ["{}", "NaN", '{"x":1,"x":2}', answer(REGISTRY).replace("v-001", "v-999")],
)
def test_completed_bad_selection_consumes_attempt_without_retry(tmp_path, content):
    sample = Mock(return_value=ChatSample(content, 0.1, ()))
    result = run_comparison(
        tmp_path, "invalid", factory=lambda _: Mock(sample=sample), metadata=lambda: {}
    )
    assert result["status"] == "completed" and sample.call_count == 12
    assert all(a["status"] == "invalid_selection" for a in result["attempts"])
    assert all(
        c["status"] in ("provider_error", "input_issues") for c in result["cases"]
    )


@pytest.mark.parametrize(
    "code", ["connection_error", "deadline", "http_error", "invalid_envelope"]
)
def test_server_error_stops_all_remaining_calls(tmp_path, code):
    sample = Mock(side_effect=OllamaExtractionError(code, stop=True))
    result = run_comparison(
        tmp_path, "stopped", factory=lambda _: Mock(sample=sample), metadata=lambda: {}
    )
    assert result["status"] == "stopped" and len(result["attempts"]) == 1
    assert result["attempts"][0]["status"] == "failed"
    sample.assert_called_once()


def test_completed_length_error_is_quality_failure_not_retry(tmp_path):
    sample = Mock(side_effect=OllamaExtractionError("incomplete_output", content="{"))
    result = run_comparison(
        tmp_path, "length", factory=lambda _: Mock(sample=sample), metadata=lambda: {}
    )
    assert result["status"] == "completed" and sample.call_count == 12
    assert all(a["error"] == "incomplete_output" for a in result["attempts"])


def test_interrupt_preserves_unknown_attempt_and_lock(tmp_path):
    sample = Mock(side_effect=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        run_comparison(
            tmp_path,
            "interrupt",
            factory=lambda _: Mock(sample=sample),
            metadata=lambda: {},
        )
    result = json.loads((tmp_path / "interrupt" / "manifest.json").read_text())
    assert result["status"] == "interrupted_or_failed"
    assert result["attempts"][0]["status"] == "pending"
    with pytest.raises(FileExistsError):
        run_comparison(tmp_path, "new", factory=Mock(), metadata=Mock())


def test_metadata_failure_is_zero_inference_and_not_restartable(tmp_path):
    factory = Mock(side_effect=AssertionError("추론 금지"))
    result = run_comparison(
        tmp_path,
        "metadata",
        factory=factory,
        metadata=Mock(side_effect=LocalMetadataError(1)),
    )
    assert result["status"] == "stopped_preflight" and result["attempts"] == []
    assert result["metadata_http_attempts"] == 1
    factory.assert_not_called()


@pytest.mark.parametrize("run_id", [None, "", "../outside", "x" * 65])
def test_invalid_id_has_no_files_or_network(tmp_path, run_id):
    with pytest.raises(ValueError):
        run_comparison(tmp_path / "new", run_id, factory=Mock(), metadata=Mock())
    assert not (tmp_path / "new").exists()


def test_default_cli_prints_plan_without_executing(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["candidate_comparison"])
    run = Mock(side_effect=AssertionError("실행 금지"))
    monkeypatch.setattr("FRS.agent.candidate_comparison.run_comparison", run)
    main()
    assert json.loads(capsys.readouterr().out)["inference_limit"] == 12
    run.assert_not_called()
