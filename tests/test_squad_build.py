"""가상 관측만으로 조건→스냅샷→실제 CP-SAT와 독립 검증 연결을 확인한다."""

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from test_candidate_search import BATCHES, changed
from test_condition_normalization import extracted_request
from test_schema import OBSERVED, VALID, common
from test_squad_engine import data_set

from FRS.core.candidate_search.search import BatchSelection
from FRS.core.squad_recommend.build import build_spec, build_squad_request
from FRS.core.squad_recommend.solver import EngineResult, SolverConfig
from FRS.core.squad_recommend.validator import (
    SquadPick,
    SquadSelection,
    ValidationReport,
    validate_squad,
)
from FRS.schema.models import ReinforceStats
from FRS.schema.scope import FORMATION_SLOTS, PRIORITY_CLUBS


def inputs(formation="4-2-2-2", club="리버풀", cost=10, **text_changes):
    original, extracted = extracted_request(
        {
            "price_cap": str(11 * cost) + "BP",
            "salary_cap": "55",
            "team_color": club,
            "formation": formation,
            "allowed_reinforcements": "5강",
            **text_changes,
        }
    )
    data = data_set(formation=formation, cost=cost)
    data["team_colors"] = [changed(row, name=club) for row in data["team_colors"]]
    return (
        original,
        extracted,
        dict(batches=BATCHES, t0=OBSERVED, clock=lambda: OBSERVED, **data),
    )


def run(request):
    original, extracted, kwargs = request
    return build_squad_request(original, extracted, **kwargs)


@pytest.mark.parametrize("formation", FORMATION_SLOTS)
@pytest.mark.parametrize("club", PRIORITY_CLUBS)
def test_all_supported_formations_clubs_reach_real_solver_and_independent_validator(
    formation, club
):
    original, extracted, kwargs = inputs(formation, club)
    before = {
        key: copy.deepcopy(kwargs[key])
        for key in ("cards", "salaries", "prices", "reinforcements", "team_colors")
    }
    result = build_squad_request(original, extracted, **kwargs)
    assert result.normalization.status == "ready"
    assert result.engine.status == "optimal" and result.engine.config == SolverConfig()
    report = result.engine.validation
    assert report.valid and len(report.players) == 11
    assert (
        report.total_price == 110
        and report.total_salary == 55
        and report.total_ovr == 1100
    )
    assert {p.slot for p in report.players} == set(FORMATION_SLOTS[formation])
    query = result.snapshot.to_dict()["query"]
    assert query["team_color"] == club and query["batches"] == BATCHES.model_dump()
    assert datetime.fromisoformat(query["t0"]) == OBSERVED
    selection = SquadSelection(
        snapshot_id=result.snapshot.snapshot_id,
        picks=tuple(
            SquadPick(spid=p.spid, reinforce_level=p.reinforce_level, slot=p.slot)
            for p in report.players
        ),
    )
    assert validate_squad(result.snapshot, selection, when=OBSERVED) == report
    assert result.engine.snapshot_id == result.snapshot.snapshot_id
    assert result.build_policy_version == build_spec()["build_policy_version"]
    for key, previous in before.items():
        assert kwargs[key] == previous
    assert result.normalization.evidence == extracted


class Unreadable:
    def __iter__(self):
        raise AssertionError("조건 오류 시 자료를 읽으면 안 됩니다.")


@pytest.mark.parametrize(
    "changes,omitted,unsupported",
    [
        ({}, ("salary_cap",), ()),
        ({"team_color": "뮌헨"}, (), ()),
        ({"price_cap": ("1조", "2조")}, (), ()),
        ({"allowed_reinforcements": "5강,12강"}, (), ()),
        ({}, (), ("가상선수A 제외",)),
    ],
)
def test_normalization_failures_never_touch_context_material_clock_snapshot_or_engine(
    changes, omitted, unsupported
):
    original, extracted = extracted_request(
        changes, omitted=omitted, unsupported=unsupported
    )
    clock = Mock(side_effect=AssertionError("조건 오류 시 시계 호출 금지"))
    with (
        patch("FRS.core.squad_recommend.build.build_snapshot") as snapshot,
        patch("FRS.core.squad_recommend.build.solve_squad") as engine,
    ):
        result = build_squad_request(
            original,
            extracted,
            batches=None,
            t0=None,
            clock=clock,
            **{
                key: Unreadable()
                for key in (
                    "cards",
                    "salaries",
                    "prices",
                    "reinforcements",
                    "team_colors",
                )
            },
        )
    assert (
        result.normalization.conditions is None
        and result.snapshot is None
        and result.engine is None
    )
    snapshot.assert_not_called()
    engine.assert_not_called()
    clock.assert_not_called()


def test_mismatched_source_span_stops_all_data_work():
    original, extracted, kwargs = inputs()
    bad = extracted.model_copy(
        update={
            "price_cap": (extracted.price_cap[0].model_copy(update={"text": "999BP"}),)
        }
    )
    with (
        patch("FRS.core.squad_recommend.build.build_snapshot") as snapshot,
        patch("FRS.core.squad_recommend.build.solve_squad") as engine,
    ):
        result = build_squad_request(original, bad, **kwargs)
    assert result.normalization.issues[0].code == "span_mismatch"
    snapshot.assert_not_called()
    engine.assert_not_called()


@pytest.mark.parametrize("difference", [-1, 0, 1])
def test_exact_large_budget_boundary_survives_full_normalization_to_solver(difference):
    cost = 10**32 + 3
    result = run(inputs(cost=cost, price_cap=str(11 * cost + difference) + "BP"))
    assert result.engine.status == ("infeasible" if difference < 0 else "optimal")
    if difference >= 0:
        assert result.engine.validation.total_price == 11 * cost
    else:
        assert result.engine.validation is None


@pytest.mark.parametrize(
    "salary,expected", [("54", "infeasible"), ("55", "optimal"), ("0", "infeasible")]
)
def test_salary_boundary_is_not_silently_increased(salary, expected):
    result = run(inputs(salary_cap=salary))
    assert result.engine.status == expected
    assert result.snapshot.to_dict()["query"]["salary_cap"] == int(salary)


def test_unobserved_requested_grade_is_not_filled_with_other_grade():
    result = run(inputs(allowed_reinforcements="6강"))
    assert result.engine.status == "infeasible" and result.engine.validation is None
    assert result.snapshot.to_dict()["query"]["allowed_reinforcements"] == [6]
    assert not result.snapshot.to_dict()["candidates"]


def test_allowed_mixed_grade_set_reaches_snapshot_and_solver_without_retraining():
    original, extracted, kwargs = inputs(allowed_reinforcements="6강,5강,6강")
    for card in kwargs["cards"]:
        kwargs["reinforcements"].append(
            ReinforceStats(
                **common(
                    spid=card.spid,
                    batch_id=BATCHES.reinforce_stats,
                    version="v6",
                    reinforce_level=6,
                    baseline_reinforce_level=5,
                    baseline_card_version=card.version,
                    stats_delta={"speed": 1},
                    position_ovr={position: 120 for position in card.position_ovr},
                )
            )
        )
    kwargs["salaries"] += [
        changed(row, reinforce_level=6, version="v6") for row in kwargs["salaries"]
    ]
    kwargs["prices"] += [changed(row, reinforce_level=6) for row in kwargs["prices"]]
    result = build_squad_request(original, extracted, **kwargs)
    assert (
        result.engine.status == "optimal" and result.engine.validation.total_ovr == 1320
    )
    assert {p.reinforce_level for p in result.engine.validation.players} == {6}
    assert result.snapshot.to_dict()["query"]["allowed_reinforcements"] == [5, 6]
    assert {
        row["reinforce_level"] for row in result.snapshot.to_dict()["candidates"]
    } == {5, 6}


def test_explicit_wrong_batch_does_not_choose_available_other_batch():
    original, extracted, kwargs = inputs()
    kwargs["batches"] = BATCHES.model_copy(update={"card_price": "다른-명시-배치"})
    result = build_squad_request(original, extracted, **kwargs)
    assert result.engine.status == "infeasible" and result.engine.validation is None
    assert not result.snapshot.to_dict()["candidates"]
    assert (
        result.snapshot.to_dict()["query"]["batches"]["card_price"] == "다른-명시-배치"
    )


@pytest.mark.parametrize("kind", ["conflict", "unknown", "expired"])
def test_invalid_observation_remains_excluded_and_missing_goalkeeper_is_not_repaired(
    kind,
):
    original, extracted, kwargs = inputs()
    row = kwargs["prices"][0]
    if kind == "conflict":
        kwargs["prices"].append(changed(row, price=11))
    elif kind == "unknown":
        kwargs["prices"][0] = changed(row, status="unknown", valid_until=None)
    else:
        kwargs["prices"][0] = changed(
            row, observed_at=OBSERVED - timedelta(hours=2), valid_until=OBSERVED
        )
    result = build_squad_request(original, extracted, **kwargs)
    assert result.engine.status == "infeasible" and result.engine.validation is None
    assert any(
        row["spid"] == 123000001 for row in result.snapshot.to_dict()["exclusions"]
    )


def test_response_deadline_drops_squad_after_search_and_independent_validation():
    original, extracted, kwargs = inputs()
    kwargs["clock"] = Mock(side_effect=[OBSERVED, VALID])
    result = build_squad_request(original, extracted, **kwargs)
    assert result.engine.status == "data_expired" and result.engine.validation is None
    assert result.engine.best_bound is None and result.engine.absolute_gap is None
    assert kwargs["clock"].call_count == 2


@pytest.mark.parametrize(
    "status", ["unknown", "infeasible", "internal_error", "data_expired"]
)
def test_existing_non_solution_status_is_preserved_without_fallback_or_retry(status):
    original, extracted, kwargs = inputs()

    def response(snapshot, *, clock):
        return EngineResult(
            method="cp_sat",
            status=status,
            snapshot_id=snapshot.snapshot_id,
            reasons=("합성-상태-대역",),
        )

    with patch(
        "FRS.core.squad_recommend.build.solve_squad", side_effect=response
    ) as engine:
        result = build_squad_request(original, extracted, **kwargs)
    assert result.engine.status == status and result.engine.reasons == (
        "합성-상태-대역",
    )
    assert result.engine.validation is None
    engine.assert_called_once()


def test_independent_validator_failure_never_returns_found_players():
    original, extracted, kwargs = inputs()

    def reject(snapshot, selection, *, when):
        return ValidationReport(
            snapshot_id=snapshot.snapshot_id, valid=False, errors=("합성-검증-거부",)
        )

    with patch("FRS.core.squad_recommend.solver.validate_squad", side_effect=reject):
        result = build_squad_request(original, extracted, **kwargs)
    assert result.engine.status == "internal_error" and result.engine.validation is None
    assert result.engine.reasons == ("합성-검증-거부",)


@pytest.mark.parametrize(
    "context",
    [
        {"batches": None},
        {"batches": BATCHES.model_dump()},
        {"batches": BatchSelection.model_construct(player_card="")},
        {"t0": datetime(2026, 1, 1)},
        {"t0": OBSERVED.isoformat()},
        {"clock": None},
    ],
)
def test_bad_internal_context_does_not_read_data_or_call_solver(context):
    original, extracted, kwargs = inputs()
    kwargs.update(
        {
            key: Unreadable()
            for key in ("cards", "salaries", "prices", "reinforcements", "team_colors")
        }
    )
    kwargs.update(context)
    with patch("FRS.core.squad_recommend.build.solve_squad") as engine:
        with pytest.raises((TypeError, ValueError)):
            build_squad_request(original, extracted, **kwargs)
    engine.assert_not_called()


def test_malformed_material_aborts_before_solver_without_fabricated_result():
    original, extracted, kwargs = inputs()
    kwargs["cards"] = [object()]
    with patch("FRS.core.squad_recommend.build.solve_squad") as engine:
        with pytest.raises(TypeError):
            build_squad_request(original, extracted, **kwargs)
    engine.assert_not_called()


class OneRead:
    def __init__(self, rows):
        self.rows, self.count = rows, 0

    def __iter__(self):
        self.count += 1
        assert self.count == 1, "자료 재순회 금지"
        yield from self.rows


def test_each_material_is_read_once_snapshot_is_detached_and_no_file_or_network():
    original, extracted, kwargs = inputs()
    sources = {
        key: kwargs[key]
        for key in ("cards", "salaries", "prices", "reinforcements", "team_colors")
    }
    streams = {key: OneRead(rows) for key, rows in sources.items()}
    kwargs.update(streams)
    with (
        patch("builtins.open", side_effect=AssertionError("파일 I/O 금지")),
        patch("socket.socket", side_effect=AssertionError("네트워크 금지")),
    ):
        result = build_squad_request(original, extracted, **kwargs)
    assert result.engine.status == "optimal" and all(
        stream.count == 1 for stream in streams.values()
    )
    snapshot_id = result.snapshot.snapshot_id
    sources["cards"][0].stats["speed"] = 999
    sources["prices"].clear()
    assert result.snapshot.snapshot_id == snapshot_id
    assert result.engine.validation.total_price == 110
    with pytest.raises(FrozenInstanceError):
        result.engine = None


def test_explicit_offset_is_normalized_to_utc_without_changing_observation_time():
    original, extracted, kwargs = inputs()
    kwargs["t0"] = OBSERVED.astimezone(timezone(timedelta(hours=9)))
    result = build_squad_request(original, extracted, **kwargs)
    assert result.engine.status == "optimal"
    assert datetime.fromisoformat(result.snapshot.to_dict()["query"]["t0"]) == OBSERVED


def test_policy_hash_is_deterministic_and_linked_specs_are_independent_copies():
    spec = build_spec()
    version = spec.pop("build_policy_version")
    encoded = json.dumps(
        spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert hashlib.sha256(encoded.encode()).hexdigest() == version
    spec["solver_config"]["workers"] = 999
    assert build_spec()["build_policy_version"] == version
    assert build_spec()["solver_config"] == SolverConfig().model_dump()


def test_expired_before_engine_search_stays_expired_without_recompute():
    original, extracted, kwargs = inputs()
    kwargs["clock"] = Mock(return_value=VALID)
    result = build_squad_request(original, extracted, **kwargs)
    assert result.engine.status == "data_expired" and result.engine.validation is None
    assert kwargs["clock"].call_count == 1


def test_clock_before_snapshot_is_existing_internal_error_without_time_repair():
    original, extracted, kwargs = inputs()
    kwargs["clock"] = lambda: OBSERVED - timedelta(seconds=1)
    result = build_squad_request(original, extracted, **kwargs)
    assert result.engine.status == "internal_error" and result.engine.validation is None
    assert result.engine.reasons == ("time_before_snapshot",)


def test_invalid_response_clock_value_remains_a_contract_error():
    original, extracted, kwargs = inputs()
    kwargs["clock"] = lambda: datetime(2026, 1, 1)
    with pytest.raises(ValueError):
        build_squad_request(original, extracted, **kwargs)
