"""모델 대역으로 그래프 분기·실제 엔진·단일 요청·추적 비활성화를 확인한다."""

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from langsmith.run_helpers import get_tracing_context
from test_condition_normalization import extracted_request
from test_schema import OBSERVED
from test_synthetic_material import config

from FRS.agent.deps import AgentDependencies
from FRS.agent.extraction import ExtractionError
from FRS.agent.graph import SquadAgent, agent_spec
from FRS.agent.material import generate_synthetic_material
from FRS.schema.scope import FORMATION_SLOTS, PRIORITY_CLUBS


@pytest.fixture(scope="module")
def material():
    return generate_synthetic_material(config())


def request(**changes):
    return extracted_request(
        {
            "price_cap": "1조",
            "salary_cap": "999",
            "allowed_reinforcements": "5강,11강",
            **changes,
        }
    )


def agent(extracted, material, *, clock=lambda: OBSERVED):
    extractor = Mock()
    extractor.extract.return_value = extracted
    provider = Mock(wraps=material)
    deps = AgentDependencies(extractor, provider, material.batches, clock)
    return SquadAgent(deps), extractor, provider


@pytest.mark.parametrize("formation", tuple(FORMATION_SLOTS))
@pytest.mark.parametrize("club", PRIORITY_CLUBS)
def test_injected_graph_reaches_real_solver_with_validated_material(
    material, formation, club
):
    original, extracted = request(formation=formation, team_color=club)
    graph, extractor, provider = agent(extracted, material)
    result = graph.run(original, t0=OBSERVED)
    assert result.error is None
    assert result.stages == ("extract", "normalize", "build")
    assert result.normalization.status == "ready"
    assert result.build.engine.status == "optimal"
    assert result.build.engine.validation.valid
    assert result.graph_version == agent_spec()["graph_version"]
    extractor.extract.assert_called_once_with(original)
    provider.load.assert_called_once_with(material.batches)


@pytest.mark.parametrize(
    "changes,omitted,unsupported",
    [
        ({}, ("salary_cap",), ()),
        ({"team_color": "뮌헨"}, (), ()),
        ({"price_cap": ("1조", "2조")}, (), ()),
        ({}, (), ("가상선수 제외",)),
    ],
)
def test_normalization_issues_never_touch_context_material_clock_or_solver(
    changes, omitted, unsupported
):
    original, extracted = extracted_request(
        changes, omitted=omitted, unsupported=unsupported
    )
    extractor = Mock()
    extractor.extract.return_value = extracted
    provider = Mock()
    provider.load.side_effect = AssertionError("자료 호출 금지")
    clock = Mock(side_effect=AssertionError("시계 호출 금지"))
    graph = SquadAgent(AgentDependencies(extractor, provider, None, clock))
    with patch("FRS.agent.graph.build_squad_request") as build:
        result = graph.run(original, t0=None)
    assert result.normalization.status == "needs_clarification"
    assert result.build is None and result.stages == ("extract", "normalize")
    provider.load.assert_not_called()
    clock.assert_not_called()
    build.assert_not_called()


@pytest.mark.parametrize("original", [None, "", "   ", "가" * 4001])
def test_invalid_original_never_calls_extractor_or_data(original):
    extractor, provider, clock = Mock(), Mock(), Mock()
    graph = SquadAgent(AgentDependencies(extractor, provider, None, clock))
    result = graph.run(original, t0=None)
    assert result.normalization.status == "needs_clarification"
    assert result.stages == ("normalize",) and result.build is None
    extractor.extract.assert_not_called()
    provider.load.assert_not_called()
    clock.assert_not_called()


def test_extraction_failure_is_not_retried_and_does_not_leak_message(material):
    original, extracted = request()
    graph, extractor, provider = agent(extracted, material)
    extractor.extract.side_effect = ExtractionError("공개하면 안 되는 가상 내부 정보")
    result = graph.run(original, t0=None)
    assert result.error == "extraction_error"
    assert result.normalization is None and result.build is None
    extractor.extract.assert_called_once()
    provider.load.assert_not_called()
    assert "공개하면" not in str(result)


def test_unexpected_programming_error_is_not_silently_converted_to_success(material):
    original, extracted = request()
    graph, extractor, provider = agent(extracted, material)
    extractor.extract.side_effect = RuntimeError("합성 버그")
    with pytest.raises(RuntimeError, match="합성 버그"):
        graph.run(original, t0=OBSERVED)
    provider.load.assert_not_called()


def test_malformed_extraction_and_wrong_span_remain_normalization_failures(material):
    original, extracted = request()
    for bad in (
        {},
        extracted.model_copy(
            update={
                "price_cap": (
                    extracted.price_cap[0].model_copy(update={"text": "없는 값"}),
                )
            }
        ),
    ):
        graph, _, provider = agent(bad, material)
        result = graph.run(original, t0=OBSERVED)
        assert result.normalization.issues and result.build is None
        provider.load.assert_not_called()


def test_repeated_requests_do_not_accumulate_history_or_prior_conditions(material):
    original, extracted = request()
    graph, extractor, provider = agent(extracted, material)
    assert graph.run(original, t0=OBSERVED).build.engine.validation.valid
    later, missing = extracted_request(omitted=("price_cap",))
    extractor.extract.return_value = missing
    result = graph.run(later, t0=None)
    assert result.build is None
    assert any(i.field == "price_cap" for i in result.normalization.issues)
    assert provider.load.call_count == 1


def test_engine_expiry_and_infeasibility_are_not_rewritten(material):
    original, extracted = request()
    graph, _, _ = agent(
        extracted, material, clock=lambda: OBSERVED + timedelta(hours=24)
    )
    assert graph.run(original, t0=OBSERVED).build.engine.status == "data_expired"
    original, extracted = request(price_cap="0BP")
    graph, _, _ = agent(extracted, material)
    result = graph.run(original, t0=OBSERVED)
    assert result.build.engine.status == "infeasible"
    assert result.build.engine.validation is None


def test_environment_cannot_enable_external_tracing_on_fake_graph(
    material, monkeypatch
):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "synthetic-not-a-secret")
    original, extracted = request()
    with (
        patch("httpx.Client.send") as httpx_send,
        patch("httpx2.Client.send") as httpx2_send,
        patch("requests.Session.send") as requests_send,
    ):
        graph, extractor, _ = agent(extracted, material)

        def checked_extract(_original):
            assert get_tracing_context()["enabled"] is False
            return extracted

        extractor.extract.side_effect = checked_extract
        assert graph.run(original, t0=OBSERVED).build.engine.validation.valid
        httpx_send.assert_not_called()
        httpx2_send.assert_not_called()
        requests_send.assert_not_called()
