"""실제 모델/외부 네트워크 없이 로컬 어댑터와 1회 상한을 검증한다."""

import asyncio
import json
from unittest.mock import Mock

import httpx2
import pytest

from FRS.agent.comparison import (
    assess,
    comparison_plan,
    main,
    run_comparison,
    smoke_cases,
)
from FRS.agent.ollama import (
    MAX_RESPONSE_BYTES,
    MODELS,
    ExtractionSample,
    OllamaExtractionError,
    OllamaExtractor,
    chat_request,
    extraction_spec,
    strict_json,
)
from FRS.core.request_normalization import ExtractedConditions

CASE = smoke_cases()[0]


def envelope(content=None, **changes):
    return {
        "model": MODELS[0],
        "done": True,
        "done_reason": "stop",
        "message": {
            "role": "assistant",
            "content": content
            if content is not None
            else CASE.expected.model_dump_json(),
        },
        "load_duration": 200,
        "total_duration": 900,
        "eval_count": 200,
        "thinking": "저장 금지",
        **changes,
    }


def extractor(response):
    def handle(request):
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["think"] is False and body["stream"] is False
        assert body["keep_alive"] == 0
        assert body["options"]["num_predict"] == 1024
        assert set(body["format"]["required"]) == set(ExtractedConditions.model_fields)
        return response

    return OllamaExtractor(MODELS[0], transport=httpx2.MockTransport(handle))


def test_mocked_adapter_preserves_all_evidence_and_safe_metrics():
    sample = extractor(httpx2.Response(200, json=envelope())).sample(CASE.original)
    assert sample.extracted == CASE.expected
    assert dict(sample.metrics) == {
        "load_duration": 200,
        "total_duration": 900,
        "eval_count": 200,
    }
    assert sample.elapsed_seconds >= 0
    assert "저장 금지" not in repr(sample)
    assert assess(CASE, sample.extracted)["semantics_match"]


@pytest.mark.parametrize(
    "content",
    [
        '{"price_cap":[],"price_cap":[]}',
        "NaN",
        "{}",
        "[]",
        CASE.expected.model_dump_json()[:-1] + ',"extra":true}',
        CASE.expected.model_dump_json().replace('"start":3', '"start":true', 1),
    ],
)
def test_malformed_duplicate_missing_extra_and_strict_fields_fail(content):
    with pytest.raises(OllamaExtractionError) as error:
        extractor(httpx2.Response(200, json=envelope(content))).extract(CASE.original)
    assert error.value.code == "invalid_extraction" and not error.value.stop
    assert str(error.value) == "로컬 조건 추출에 실패했습니다."


@pytest.mark.parametrize(
    "changes",
    [
        {"done": False},
        {"model": "different"},
        {"message": {}},
        {"message": {"role": "user", "content": "{}"}},
    ],
)
def test_invalid_envelope_stops_unknown_server_state(changes):
    with pytest.raises(OllamaExtractionError) as error:
        extractor(httpx2.Response(200, json=envelope(**changes))).extract(CASE.original)
    assert error.value.code == "invalid_envelope" and error.value.stop


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_http_failures_are_not_followed_or_retried(status):
    with pytest.raises(OllamaExtractionError) as error:
        extractor(
            httpx2.Response(status, headers={"location": "https://example.org"})
        ).extract(CASE.original)
    assert error.value.code == "http_error" and error.value.stop


def test_truncated_generation_is_failed_without_partial_repair():
    with pytest.raises(OllamaExtractionError) as error:
        extractor(httpx2.Response(200, json=envelope(done_reason="length"))).extract(
            CASE.original
        )
    assert error.value.code == "incomplete_output"


def test_oversize_response_is_stopped():
    with pytest.raises(OllamaExtractionError) as error:
        extractor(
            httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
        ).extract(CASE.original)
    assert error.value.code == "response_too_large" and error.value.stop


def test_total_deadline_includes_wait_for_response(monkeypatch):
    real_timeout = asyncio.timeout
    monkeypatch.setattr(
        "FRS.agent.ollama.asyncio.timeout", lambda _: real_timeout(0.001)
    )

    async def delayed(request):
        await asyncio.sleep(0.03)
        return httpx2.Response(200, json=envelope())

    with pytest.raises(OllamaExtractionError) as error:
        OllamaExtractor(MODELS[0], transport=httpx2.MockTransport(delayed)).extract(
            CASE.original
        )
    assert error.value.code == "deadline" and error.value.stop


def test_connection_failure_is_not_retried():
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx2.ConnectError("private diagnostic")

    with pytest.raises(OllamaExtractionError) as error:
        OllamaExtractor(MODELS[0], transport=httpx2.MockTransport(fail)).extract(
            CASE.original
        )
    assert len(calls) == 1
    assert error.value.code == "connection_error" and "private" not in str(error.value)


@pytest.mark.parametrize("source", [None, "", " ", "x" * 4001])
def test_invalid_input_has_no_http(source):
    transport = Mock(side_effect=AssertionError("HTTP 금지"))
    with pytest.raises(ValueError):
        chat_request(MODELS[0], source)
    transport.assert_not_called()


def test_only_approved_models_and_independent_spec_copies():
    with pytest.raises(ValueError):
        OllamaExtractor("qwen2.5:0.5b")
    first = extraction_spec()
    first["options"]["seed"] = 999
    assert extraction_spec()["options"]["seed"] == 123
    assert comparison_plan() == comparison_plan()
    assert comparison_plan()["inference_limit"] == 10


def test_bad_offsets_do_not_get_repaired_into_semantic_success():
    value = CASE.expected.model_dump()
    value["price_cap"][0]["start"] += 1
    wrong = ExtractedConditions.model_validate(value)
    result = assess(CASE, wrong)
    assert not result["spans_valid"] and not result["semantics_match"]
    assert result["actual"]["conditions"] is None


def test_fixed_scenarios_cover_ready_missing_unsupported_conflict_decimal():
    rows = [assess(case, case.expected) for case in smoke_cases()]
    assert all(
        r["spans_valid"] and r["evidence_exact"] and r["semantics_match"] for r in rows
    )
    assert [r["actual"]["status"] for r in rows] == [
        "ready",
        "needs_clarification",
        "needs_clarification",
        "needs_clarification",
        "ready",
    ]
    assert rows[0]["actual"]["conditions"]["price_cap"] == 1500000000000
    assert rows[4]["actual"]["conditions"]["allowed_reinforcements"] == [5, 7]


def successful_factory(root, run_id, calls):
    def factory(model):
        def sample(original):
            manifest = json.loads((root / run_id / "manifest.json").read_text())
            assert manifest["attempts"][-1]["status"] == "pending"
            assert manifest["attempts"][-1]["number"] == len(calls) + 1
            calls.append((model, original))
            case = next(c for c in smoke_cases() if c.original == original)
            return ExtractionSample(
                case.expected, case.expected.model_dump_json(), 0.1, ()
            )

        return Mock(sample=sample)

    return factory


def test_single_approval_ledger_reserved_before_http_and_cannot_restart(tmp_path):
    calls = []
    result = run_comparison(
        tmp_path,
        "test-once",
        factory=successful_factory(tmp_path, "test-once", calls),
        metadata=lambda: {},
    )
    assert result["status"] == "completed" and len(calls) == 10
    assert all(a["assessment"]["semantics_match"] for a in result["attempts"])
    for run_id in ("test-once", "another-id"):
        with pytest.raises(FileExistsError):
            run_comparison(tmp_path, run_id, factory=Mock(), metadata=Mock())
    assert len(calls) == 10


@pytest.mark.parametrize("code", ["deadline", "connection_error", "http_error"])
def test_uncertain_failure_stops_remaining_calls(tmp_path, code):
    sample = Mock(side_effect=OllamaExtractionError(code, stop=True))
    result = run_comparison(
        tmp_path, "stopped", factory=lambda _: Mock(sample=sample), metadata=lambda: {}
    )
    assert result["status"] == "stopped" and len(result["attempts"]) == 1
    assert result["attempts"][0]["status"] == "failed"
    sample.assert_called_once()


def test_known_completed_bad_json_consumes_attempt_without_retry(tmp_path):
    sample = Mock(side_effect=OllamaExtractionError("invalid_extraction", content="{}"))
    result = run_comparison(
        tmp_path, "invalid", factory=lambda _: Mock(sample=sample), metadata=lambda: {}
    )
    assert result["status"] == "completed" and sample.call_count == 10
    assert all(a["error"] == "invalid_extraction" for a in result["attempts"])


def test_process_interrupt_keeps_reserved_pending_attempt(tmp_path):
    sample = Mock(side_effect=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        run_comparison(
            tmp_path,
            "interrupted",
            factory=lambda _: Mock(sample=sample),
            metadata=lambda: {},
        )
    report = json.loads((tmp_path / "interrupted" / "manifest.json").read_text())
    assert report["status"] == "interrupted_or_failed"
    assert len(report["attempts"]) == 1 and report["attempts"][0]["status"] == "pending"
    with pytest.raises(FileExistsError):
        run_comparison(tmp_path, "new", factory=Mock(), metadata=Mock())


def test_plan_cli_performs_no_network_or_file_write(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["comparison"])
    network = Mock(side_effect=AssertionError("HTTP 금지"))
    monkeypatch.setattr("FRS.agent.comparison.local_metadata", network)
    main()
    assert json.loads(capsys.readouterr().out)["inference_limit"] == 10
    network.assert_not_called()


@pytest.mark.parametrize("run_id", [None, "", "../outside", "x" * 65])
def test_invalid_run_id_never_creates_root(tmp_path, run_id):
    root = tmp_path / "untouched"
    with pytest.raises(ValueError):
        run_comparison(root, run_id)
    assert not root.exists()


def test_strict_json_rejects_nonfinite_and_duplicate_nested_fields():
    for source in ('{"x":NaN}', '{"x":{"a":1,"a":2}}'):
        with pytest.raises(ValueError):
            strict_json(source)


def test_mocked_ollama_reaches_graph_solver_and_code_presentation():
    from test_schema import OBSERVED
    from test_synthetic_material import config

    from FRS.agent.deps import AgentDependencies
    from FRS.agent.graph import SquadAgent
    from FRS.agent.material import generate_synthetic_material
    from FRS.agent.presentation import present_agent_result

    material = generate_synthetic_material(config())
    graph = SquadAgent(
        AgentDependencies(
            extractor(httpx2.Response(200, json=envelope())),
            material,
            material.batches,
            lambda: OBSERVED,
        )
    )
    result = graph.run(CASE.original, t0=OBSERVED)
    shown = present_agent_result(result, clock=lambda: OBSERVED).to_dict()
    assert shown["status"] == "optimal"
    assert len(shown["squad"]["rows"]) == 11
    assert shown["kind"] == "code_presentation"


def test_mocked_bad_offset_stops_graph_before_material_or_engine():
    from FRS.agent.deps import AgentDependencies
    from FRS.agent.graph import SquadAgent

    wrong = CASE.expected.model_dump()
    wrong["price_cap"][0]["start"] += 1
    provider = Mock(side_effect=AssertionError("자료 금지"))
    graph = SquadAgent(
        AgentDependencies(
            extractor(
                httpx2.Response(
                    200, json=envelope(json.dumps(wrong, ensure_ascii=False))
                )
            ),
            provider,
            None,
            Mock(side_effect=AssertionError("시계 금지")),
        )
    )
    result = graph.run(CASE.original, t0=None)
    assert result.normalization.status == "needs_clarification"
    assert result.build is None
    provider.load.assert_not_called()


def test_preflight_failure_is_safe_persistent_and_never_calls_model(tmp_path):
    from FRS.agent.comparison import LocalMetadataError

    factory = Mock(side_effect=AssertionError("추론 금지"))
    result = run_comparison(
        tmp_path,
        "preflight",
        factory=factory,
        metadata=Mock(side_effect=LocalMetadataError(1)),
    )
    assert result["status"] == "stopped_preflight"
    assert result["metadata_http_attempts"] == 1
    assert result["attempts"] == []
    factory.assert_not_called()
    with pytest.raises(FileExistsError):
        run_comparison(tmp_path, "another", factory=factory, metadata=Mock())
