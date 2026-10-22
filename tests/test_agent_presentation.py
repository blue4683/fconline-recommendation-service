"""코드 출력의 독립 검증·정확한 큰 수·만료·실패 시 표 미노출을 확인한다."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from test_agent_graph import agent, request
from test_schema import OBSERVED
from test_squad_build import inputs
from test_synthetic_material import config

from FRS.agent.graph import AgentRunResult
from FRS.agent.material import generate_synthetic_material
from FRS.agent.presentation import present_agent_result
from FRS.core.squad_recommend.build import build_squad_request
from FRS.core.squad_recommend.validator import validate_squad
from FRS.schema.models import SourceKind
from FRS.schema.scope import FORMATION_SLOTS


@pytest.fixture(scope="module")
def success():
    material = generate_synthetic_material(config())
    original, extracted = request()
    graph, _, _ = agent(extracted, material)
    return graph.run(original, t0=OBSERVED)


def render(result, clock=lambda: OBSERVED):
    return present_agent_result(result, clock=clock).to_dict()


def with_engine(result, **changes):
    return replace(
        result,
        build=replace(
            result.build, engine=result.build.engine.model_copy(update=changes)
        ),
    )


def test_successful_table_matches_independent_report_and_formation_order(success):
    output = render(success)
    report = success.build.engine.validation
    assert output["status"] == "optimal" and output["narration_source"] == "template"
    table = output["squad"]
    assert len(table["rows"]) == 11
    assert [r["slot"] for r in table["rows"]] == list(
        FORMATION_SLOTS[success.normalization.conditions.formation]
    )
    assert table["total_price"] == str(report.total_price)
    assert table["total_salary"] == str(report.total_salary)
    assert output["snapshot"]["id"] == success.build.snapshot.snapshot_id
    assert output["engine_version"] == success.build.engine.engine_version
    assert (
        output["data_mode"] == "synthetic"
        and output["model_metadata"] == "not_provided"
    )
    assert all(
        isinstance(row[k], str)
        for row in table["rows"]
        for k in ("spid", "person_id", "price", "salary")
    )


def test_large_bp_salary_and_caps_are_exact_decimal_strings():
    original, extracted, kwargs = inputs(cost=10**30 + 7, salary_cap=str(2**63 - 1))
    wage = 2**53 + 1
    kwargs["salaries"] = [
        row.model_copy(update={"salary": wage}) for row in kwargs["salaries"]
    ]
    built = build_squad_request(original, extracted, **kwargs)
    result = AgentRunResult("synthetic-graph", built.normalization, built)
    output = render(result)
    assert output["squad"]["total_price"] == str(11 * (10**30 + 7))
    assert output["squad"]["total_salary"] == str(11 * wage)
    assert output["conditions"]["salary_cap"] == str(2**63 - 1)
    assert output["squad"]["rows"][0]["salary"] == str(wage)


def test_final_expiry_check_happens_after_independent_validation_and_removes_table(
    success,
):
    deadline = datetime.fromisoformat(
        success.build.snapshot.to_dict()["metadata"]["valid_until"]
    )
    with patch(
        "FRS.agent.presentation.validate_squad", wraps=validate_squad
    ) as validate:

        def clock():
            validate.assert_called_once()
            return deadline

        output = render(success, clock)
    assert output["status"] == "data_expired" and output["squad"] is None
    assert "최적성을 확인" not in output["answer"]


@pytest.mark.parametrize("field", ["total_price", "total_salary", "total_ovr"])
def test_tampered_report_totals_are_rejected_without_partial_table(success, field):
    report = success.build.engine.validation
    tampered = report.model_copy(update={field: getattr(report, field) + 1})
    output = render(with_engine(success, validation=tampered))
    assert output["status"] == "internal_error" and output["squad"] is None


def test_mismatched_snapshot_or_normalized_conditions_are_rejected(success):
    output = render(with_engine(success, snapshot_id="unrelated"))
    assert output["status"] == "internal_error" and output["squad"] is None
    changed = success.normalization.model_copy(
        update={
            "conditions": success.normalization.conditions.model_copy(
                update={"price_cap": 0}
            )
        }
    )
    output = render(replace(success, normalization=changed))
    assert output["status"] == "internal_error" and output["squad"] is None


@pytest.mark.parametrize(
    "status", ["infeasible", "unknown", "internal_error", "data_expired"]
)
def test_non_success_states_have_no_table_and_keep_distinct_meanings(success, status):
    output = render(with_engine(success, status=status, validation=None))
    assert output["status"] == status and output["squad"] is None
    if status == "unknown":
        assert "확정하지 못" in output["answer"]
        assert "스쿼드가 없습니다" not in output["answer"]


def test_feasible_is_not_labeled_optimal(success):
    output = render(with_engine(success, status="feasible", solver_status="FEASIBLE"))
    assert output["status"] == "feasible" and output["squad"]
    assert "최적성은 미확인" in output["answer"]


def test_normalization_and_extraction_failures_never_read_clock_or_emit_squad():
    material = generate_synthetic_material(config(members_per_family=1))
    original, extracted = request(team_color="뮌헨")
    graph, _, provider = agent(extracted, material)
    failed = graph.run(original, t0=None)
    clock = Mock(side_effect=AssertionError("시계 호출 금지"))
    output = render(failed, clock)
    assert output["status"] == "needs_clarification" and output["issues"]
    assert output["squad"] is None
    provider.load.assert_not_called()
    output = render(
        AgentRunResult("synthetic", error="arbitrary private failure"), clock
    )
    assert output["status"] == "internal_error"
    assert "arbitrary private" not in str(output)
    clock.assert_not_called()


def test_comparison_solver_and_non_synthetic_material_are_not_presented_as_agent_success(
    success,
):
    output = render(with_engine(success, method="cheapest"))
    assert output["status"] == "internal_error" and output["squad"] is None
    original, extracted, kwargs = inputs()
    kwargs["cards"][0] = kwargs["cards"][0].model_copy(
        update={"source": SourceKind.WEB}
    )
    built = build_squad_request(original, extracted, **kwargs)
    output = render(AgentRunResult("synthetic", built.normalization, built))
    assert output["status"] == "internal_error" and output["squad"] is None


def test_output_is_immutable_and_requires_no_file_network_or_material_reload(success):
    with (
        patch("builtins.open", side_effect=AssertionError("파일 접근 금지")),
        patch("socket.socket", side_effect=AssertionError("네트워크 접근 금지")),
    ):
        value = present_agent_result(success, clock=lambda: OBSERVED)
    before = value.to_dict()
    value.to_dict()["squad"]["rows"].clear()
    assert value.to_dict() == before
    with pytest.raises(FrozenInstanceError):
        value._payload = b"changed"


def test_invalid_response_clock_is_rejected_after_validation(success):
    with pytest.raises(ValueError, match="시간대"):
        render(success, lambda: OBSERVED.replace(tzinfo=None))


def test_declared_data_mode_must_match_every_candidate_source(success):
    shown = present_agent_result(
        success, clock=lambda: OBSERVED, data_mode="observed"
    ).to_dict()
    assert (shown["status"], shown["squad"], shown["data_mode"]) == (
        "internal_error",
        None,
        "observed",
    )
    with pytest.raises(ValueError):
        present_agent_result(success, clock=lambda: OBSERVED, data_mode="real")


@pytest.mark.parametrize("price_status", ["optimal", "incomplete"])
def test_price_search_notice_does_not_confuse_proven_ovr_with_price_optimality(
    success, price_status
):
    result = with_engine(success, price_optimization_status=price_status)
    output = render(result)
    assert output["status"] == "optimal" and output["squad"]
    assert (
        "동일 OVR 내 최대 예산 활용은 시간 내 확인하지 못했습니다." in output["answer"]
    ) == (price_status == "incomplete")


def test_price_search_notice_is_not_shown_after_expiry(success):
    result = with_engine(success, price_optimization_status="incomplete")
    deadline = datetime.fromisoformat(
        success.build.snapshot.to_dict()["metadata"]["valid_until"]
    )
    output = render(result, clock=lambda: deadline)
    assert output["status"] == "data_expired" and output["squad"] is None
    assert "최대 예산 활용" not in output["answer"]
