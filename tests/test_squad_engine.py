"""가상 선수·수치로 최적화, 독립 검증, 큰 정수와 상태의 의미를 확인한다."""

import itertools
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError
from test_candidate_search import BATCHES, CLUBS, changed
from test_schema import OBSERVED, VALID, card, common, price

from FRS.core.squad_recommend.placement import placement_spec
from FRS.core.squad_recommend.snapshot import (
    RequestSnapshot,
    SnapshotQuery,
    build_snapshot,
)
from FRS.core.squad_recommend.solver import SolverConfig, greedy_squad, solve_squad
from FRS.core.squad_recommend.validator import SquadPick, SquadSelection, validate_squad
from FRS.schema.models import CardSalary, ReinforceStats, TeamColor
from FRS.schema.scope import FORMATION_SLOTS

SLOTS = FORMATION_SLOTS["4-2-2-2"]


def data_set(cost=10, salary=5, formation="4-2-2-2"):
    data = dict(cards=[], salaries=[], prices=[], reinforcements=[], team_colors=[])
    aliases = placement_spec()["families"]
    for index, slot in enumerate(FORMATION_SLOTS[formation], 1):
        position = aliases.get(slot, slot)
        identity = dict(spid=123000000 + index)
        data["cards"].append(
            card(
                **identity,
                person_id=index,
                name=f"가상선수_{index:04d}",
                batch_id=BATCHES.player_card,
                version="base-v1",
                positions=[position],
                position_ovr={position: 100},
            )
        )
        data["salaries"].append(
            CardSalary(
                **common(
                    **identity,
                    batch_id=BATCHES.card_salary,
                    version="base-v1",
                    reinforce_level=5,
                    salary=salary,
                )
            )
        )
        data["prices"].append(
            price(**identity, batch_id=BATCHES.card_price, price=cost)
        )
        data["team_colors"].append(
            TeamColor(
                **common(
                    **identity, batch_id=BATCHES.team_color, type="club", name=CLUBS[0]
                )
            )
        )
    return data


def snapshot(data=None, **changes):
    values = dict(
        formation="4-2-2-2",
        team_color=CLUBS[0],
        salary_cap=100,
        price_cap=1000,
        allowed_reinforcements=(5,),
        batches=BATCHES,
        t0=OBSERVED,
    )
    values.update(changes)
    query = SnapshotQuery(**values)
    return build_snapshot(
        query, **(data_set() if data is None else data), supported_clubs=CLUBS
    )


def selection(snap, **updates):
    picks = [
        SquadPick(spid=123000000 + i, slot=slot, reinforce_level=5)
        for i, slot in enumerate(SLOTS, 1)
    ]
    return SquadSelection(
        **{"snapshot_id": snap.snapshot_id, "picks": picks, **updates}
    )


def forge(snap, change):
    content = snap.to_dict()
    content.pop("snapshot_id")
    change(content)
    return RequestSnapshot(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    )


def solve(snap, **kwargs):
    return solve_squad(snap, clock=lambda: OBSERVED, **kwargs)


def brute_ovr(snap):
    rows = snap.to_dict()["candidates"]
    q = snap.to_dict()["query"]
    pools = [[r for r in rows if r["slot"] == slot] for slot in SLOTS]
    best = None
    for picked in itertools.product(*pools):
        if (
            len({r["spid"] for r in picked}) != 11
            or len({r["observations"]["player_card"]["person_id"] for r in picked})
            != 11
        ):
            continue
        if (
            sum(r["observations"]["card_price"]["price"] for r in picked)
            > q["price_cap"]
        ):
            continue
        if (
            sum(r["observations"]["card_salary"]["salary"] for r in picked)
            > q["salary_cap"]
        ):
            continue
        objective = sum(r["raw_ovr"] for r in picked)
        best = objective if best is None else max(best, objective)
    return best


def test_solver_and_both_greedy_methods_return_only_independently_valid_squads():
    snap = snapshot()
    for result in (
        solve(snap),
        *(
            greedy_squad(snap, method=m, clock=lambda: OBSERVED)
            for m in ("cheapest", "performance")
        ),
    ):
        assert result.status in ("optimal", "feasible")
        report = result.validation
        assert report.valid and len(report.players) == 11
        assert report.total_price == 110 and report.total_salary == 55
        assert report.performance == 550 and report.total_ovr == 1100
        chosen = SquadSelection(
            snapshot_id=snap.snapshot_id,
            picks=[
                SquadPick(**p.model_dump(include=set(SquadPick.model_fields)))
                for p in report.players
            ],
        )
        assert validate_squad(snap, chosen, when=OBSERVED) == report
    assert solve(snap).best_bound == 550 and solve(snap).absolute_gap == 0


@pytest.mark.parametrize("cost", [10**20 + 7, 10**32 + 3])
@pytest.mark.parametrize("difference", [-1, 0, 1])
def test_price_beyond_int64_preserves_exact_one_bp_boundary(cost, difference):
    snap = snapshot(data_set(cost=cost), price_cap=11 * cost + difference)
    result = solve(snap)
    assert result.status == ("infeasible" if difference < 0 else "optimal")
    if difference >= 0:
        assert result.validation.total_price == 11 * cost
    else:
        assert result.validation is None


@pytest.mark.parametrize("difference", [-1, 0, 1])
def test_salary_sum_beyond_safe_float_precision_is_exact(difference):
    amount = 10**16 + 3
    snap = snapshot(data_set(salary=amount), salary_cap=11 * amount + difference)
    result = solve(snap)
    assert result.status == ("infeasible" if difference < 0 else "optimal")
    if difference >= 0:
        assert result.validation.total_salary == 11 * amount


def test_maximum_34_digit_bp_contract_is_not_truncated():
    cap = 10**34 - 1
    cost = cap // 11
    snap = snapshot(data_set(cost=cost), price_cap=cap)
    assert solve(snap).validation.total_price == cost * 11


def test_grouped_person_constraint_makes_different_season_cards_mutually_exclusive():
    data = data_set()
    # 다른 시즌의 같은 인물로 RCB와 LCB를 채울 수 없다.
    for key in ("cards", "salaries", "prices", "team_colors"):
        row = data[key][2]
        changes = dict(spid=124000002)
        if key == "cards":
            changes.update(season_id=124, person_id=2)
        data[key][2] = changed(row, **changes)
    snap = snapshot(data)
    assert solve(snap).status == "infeasible"
    picks = list(selection(snap).picks)
    picks[2] = SquadPick(spid=124000002, slot="LCB", reinforce_level=5)
    report = validate_squad(snap, selection(snap, picks=picks), when=OBSERVED)
    assert not report.valid and "duplicate_person" in report.errors


def test_same_card_cannot_fill_two_slots_or_use_two_reinforcements():
    snap = snapshot()
    picks = list(selection(snap).picks)
    picks[2] = SquadPick(spid=123000002, slot="LCB", reinforce_level=5)
    report = validate_squad(snap, selection(snap, picks=picks), when=OBSERVED)
    assert {"duplicate_card", "duplicate_person"} <= set(report.errors)
    assert report.players == () and report.total_price is None


@pytest.mark.parametrize(
    "problem", ["count", "slot", "missing_card", "missing_grade", "snapshot_id"]
)
def test_invalid_selection_is_rejected_without_any_players_or_totals(problem):
    snap = snapshot()
    picks = list(selection(snap).picks)
    updates = {}
    if problem == "count":
        picks.pop()
    elif problem == "slot":
        picks[0] = SquadPick(spid=picks[0].spid, slot="ST", reinforce_level=5)
    elif problem == "missing_card":
        picks[0] = SquadPick(spid=123000999, slot="GK", reinforce_level=5)
    elif problem == "missing_grade":
        picks[0] = SquadPick(spid=picks[0].spid, slot="GK", reinforce_level=6)
    else:
        updates["snapshot_id"] = "0" * 64
    report = validate_squad(
        snap, selection(snap, picks=picks, **updates), when=OBSERVED
    )
    assert not report.valid and report.errors and not report.players
    assert report.total_price is report.total_salary is report.performance is None


@pytest.mark.parametrize(
    "key,cap,reason",
    [("price_cap", 109, "over_budget"), ("salary_cap", 54, "over_salary")],
)
def test_validator_recomputes_team_total_from_original_observations(key, cap, reason):
    snap = snapshot(**{key: cap})
    report = validate_squad(snap, selection(snap), when=OBSERVED)
    assert reason in report.errors and not report.players


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["candidates"][0].update(performance=1),
        lambda d: d["candidates"][0].update(raw_ovr=199),
        lambda d: d["candidates"][0].update(placement_type="extended"),
        lambda d: d["candidates"][0]["observations"]["team_color"].update(
            name="가상클럽_다름"
        ),
        lambda d: d["candidates"][0]["observations"]["card_price"].update(
            batch_id="other"
        ),
        lambda d: d["candidates"][0]["observations"]["card_salary"].update(
            version="wrong"
        ),
        lambda d: d["candidates"][0]["observations"]["player_card"].update(
            status="unknown"
        ),
        lambda d: d["candidates"][0]["observations"]["card_price"].update(
            valid_until=None, status="unknown"
        ),
        lambda d: d["metadata"].update(valid_until="2099-01-01T00:00:00+00:00"),
        lambda d: d["metadata"].update(model_version="unapproved"),
        lambda d: d["candidates"].append(d["candidates"][0]),
    ],
)
def test_invalid_snapshot_observations_or_cached_values_are_not_trusted(change):
    bad = forge(snapshot(), change)
    report = validate_squad(bad, selection(bad), when=OBSERVED)
    assert not report.valid and report.errors
    assert solve(bad).status == "internal_error"


def test_expiry_before_and_during_solver_suppresses_all_solutions():
    snap = snapshot()
    assert solve_squad(snap, clock=lambda: VALID).status == "data_expired"
    times = iter((OBSERVED, VALID))
    result = solve_squad(snap, clock=lambda: next(times))
    assert result.status == "data_expired" and result.validation is None
    assert validate_squad(snap, selection(snap), when=VALID).errors == ("data_expired",)
    assert validate_squad(
        snap, selection(snap), when=VALID - timedelta(microseconds=1)
    ).valid


@pytest.mark.parametrize("method", ["cheapest", "performance"])
def test_greedy_expiry_and_failure_never_claim_infeasibility(method):
    snap = snapshot()
    assert (
        greedy_squad(snap, method=method, clock=lambda: VALID).status == "data_expired"
    )
    impossible = snapshot(price_cap=109)
    result = greedy_squad(impossible, method=method, clock=lambda: OBSERVED)
    assert result.status == "greedy_failed" and result.validation is None


def test_missing_slot_is_reported_before_cp_sat_call():
    data = data_set()
    data["cards"] = data["cards"][1:]
    with patch(
        "FRS.core.squad_recommend.solver.cp_model.CpSolver",
        side_effect=AssertionError("탐색 불필요"),
    ):
        result = solve(snapshot(data))
    assert result.status == "infeasible" and result.reasons == ("empty_slot:GK",)


@pytest.mark.parametrize(
    "status,expected",
    [
        (cp_model.UNKNOWN, "unknown"),
        (cp_model.INFEASIBLE, "infeasible"),
        (cp_model.MODEL_INVALID, "internal_error"),
    ],
)
def test_no_solution_statuses_are_distinct_and_never_read_solver_values(
    status, expected
):
    with (
        patch.object(cp_model.CpSolver, "solve", return_value=status),
        patch.object(cp_model.CpSolver, "value", side_effect=AssertionError("해 없음")),
    ):
        result = solve(snapshot())
    assert result.status == expected and result.validation is None


def test_actual_feasible_solution_is_not_relabelled_optimal():
    actual = cp_model.CpSolver.solve

    def feasible(self, model):
        assert actual(self, model) == cp_model.OPTIMAL
        return cp_model.FEASIBLE

    with patch.object(cp_model.CpSolver, "solve", feasible):
        result = solve(snapshot())
    assert result.status == "feasible" and result.validation.valid


def test_model_invalid_and_solver_error_return_internal_error():
    with patch.object(cp_model.CpModel, "validate", return_value="가상 오류"):
        assert solve(snapshot()).status == "internal_error"
    with patch.object(
        cp_model.CpSolver, "solve", side_effect=RuntimeError("가상 실행 오류")
    ):
        assert solve(snapshot()).reasons == ("solver_failure",)


def test_independent_validator_failure_prevents_solver_solution_return():
    from FRS.core.squad_recommend.validator import ValidationReport

    rejected = ValidationReport(
        snapshot_id=snapshot().snapshot_id, valid=False, errors=("forced_violation",)
    )
    with patch("FRS.core.squad_recommend.solver.validate_squad", return_value=rejected):
        result = solve(snapshot())
    assert result.status == "internal_error" and result.validation is None


def test_ties_in_greedy_follow_spid_then_grade_and_formation_order():
    snap = snapshot()
    for method in ("cheapest", "performance"):
        report = greedy_squad(snap, method=method, clock=lambda: OBSERVED).validation
        assert tuple(p.slot for p in report.players) == SLOTS
        assert [p.spid for p in report.players] == list(range(123000001, 123000012))


@pytest.mark.parametrize(
    "updates",
    [
        {"max_time_seconds": 0},
        {"max_time_seconds": float("inf")},
        {"workers": 2},
        {"seed": True},
        {"seed": -1},
    ],
)
def test_invalid_execution_config_is_rejected(updates):
    with pytest.raises(ValidationError):
        SolverConfig(**updates)


def test_input_price_and_salary_cannot_be_submitted_as_selection_fields():
    with pytest.raises(ValidationError):
        SquadPick(spid=123000001, slot="GK", reinforce_level=5, price=1)


def test_engine_and_validator_do_not_call_file_or_network_io():
    snap = snapshot()
    before = snap.to_dict()
    with (
        patch("builtins.open", side_effect=AssertionError("파일 I/O 금지")),
        patch("socket.socket", side_effect=AssertionError("네트워크 금지")),
    ):
        assert solve(snap).validation.valid
        assert validate_squad(snap, selection(snap), when=OBSERVED).valid
    assert snap.to_dict() == before


def clone_card(data, original, person, *, cost, ovr, positions=None, season=123):
    for resource in ("cards", "salaries", "prices", "team_colors"):
        row = next(r for r in data[resource] if r.spid == original)
        updates = dict(spid=season * 1000000 + person)
        if resource == "cards":
            updates.update(
                season_id=season,
                person_id=person,
                name=f"가상선수_{person:04d}",
                position_ovr=ovr,
            )
            if positions is not None:
                updates["positions"] = positions
        elif resource == "prices":
            updates["price"] = cost
        data[resource].append(changed(row, **updates))


@pytest.mark.parametrize("budget", [110, 119, 120, 130])
def test_cp_sat_matches_exhaustive_optimum_on_small_budget_tradeoff(budget):
    data = data_set()
    clone_card(data, 123000001, 20, cost=20, ovr={"GK": 150})
    clone_card(data, 123000004, 21, cost=20, ovr={"RB": 140})
    snap = snapshot(data, price_cap=budget)
    result = solve(snap)
    assert result.status == "optimal"
    assert result.validation.total_ovr == brute_ovr(snap)
    for method in ("cheapest", "performance"):
        baseline = greedy_squad(snap, method=method, clock=lambda: OBSERVED)
        if baseline.validation:
            assert result.validation.total_ovr >= baseline.validation.total_ovr


@pytest.mark.parametrize("method", ["cheapest", "performance"])
def test_greedy_can_fail_while_cp_sat_finds_a_valid_squad(method):
    data = data_set()
    data["cards"][0] = changed(
        data["cards"][0], positions=["GK", "RB"], position_ovr={"GK": 200, "RB": 200}
    )
    clone_card(data, 123000001, 20, cost=20, ovr={"GK": 100}, positions=["GK"])
    for key in data:
        data[key] = [r for r in data[key] if r.spid != 123000004]
    snap = snapshot(data)
    assert (
        greedy_squad(snap, method=method, clock=lambda: OBSERVED).status
        == "greedy_failed"
    )
    result = solve(snap)
    assert result.status == "optimal" and result.validation.total_ovr == brute_ovr(snap)


def test_mixed_reinforcement_optimum_preserves_person_uniqueness_and_exact_ovr():
    data = data_set()
    data["salaries"].append(
        changed(data["salaries"][0], reinforce_level=6, version="ability-v6")
    )
    data["prices"].append(changed(data["prices"][0], reinforce_level=6, price=11))
    data["reinforcements"].append(
        ReinforceStats(
            **common(
                batch_id=BATCHES.reinforce_stats,
                version="ability-v6",
                reinforce_level=6,
                baseline_reinforce_level=5,
                baseline_card_version="base-v1",
                stats_delta={"speed": 1},
                position_ovr={"GK": 101},
            )
        )
    )
    snap = snapshot(data, allowed_reinforcements=(5, 6), price_cap=111)
    report = solve(snap).validation
    assert report.performance == 550.5 and report.total_ovr == 1101
    assert len({p.person_id for p in report.players}) == 11
    assert report.players[0].reinforce_level == 6 and report.total_price == 111
    assert report.total_ovr == brute_ovr(snap)


@pytest.mark.parametrize("formation", FORMATION_SLOTS)
def test_all_three_mvp_formations_return_eleven_distinct_people(formation):
    snap = snapshot(data_set(formation=formation), formation=formation)
    report = solve(snap).validation
    assert (
        report.valid
        and tuple(p.slot for p in report.players) == FORMATION_SLOTS[formation]
    )
    assert len({p.person_id for p in report.players}) == 11


@pytest.mark.parametrize(
    "updates",
    [{"workers": True}, {"max_time_seconds": True}, {"max_time_seconds": "10"}],
)
def test_execution_numbers_do_not_accept_boolean_or_string_coercion(updates):
    with pytest.raises(ValidationError):
        SolverConfig(**updates)


def test_effective_config_runtime_and_policy_are_recorded():
    from FRS.core.squad_recommend.solver import engine_spec

    actual = cp_model.CpSolver.solve
    seen = []

    def inspect(self, model):
        seen.append(
            (
                self.parameters.max_time_in_seconds,
                self.parameters.num_search_workers,
                self.parameters.random_seed,
            )
        )
        return actual(self, model)

    with patch.object(cp_model.CpSolver, "solve", inspect):
        result = solve(snapshot())
    assert seen == [(10, 1, 123)]
    assert result.config == SolverConfig() and result.ortools_version == "9.15.6755"
    assert result.engine_version == engine_spec()["engine_version"]
    altered = engine_spec()
    altered["greedy_backtracking"] = True
    assert not engine_spec()["greedy_backtracking"]


def test_feasible_bound_gap_is_reported_in_performance_units():
    from unittest.mock import PropertyMock

    actual = cp_model.CpSolver.solve

    def feasible(self, model):
        actual(self, model)
        return cp_model.FEASIBLE

    with (
        patch.object(cp_model.CpSolver, "solve", feasible),
        patch.object(
            cp_model.CpSolver,
            "best_objective_bound",
            new_callable=PropertyMock,
            return_value=1200,
        ),
    ):
        result = solve(snapshot())
    assert (
        result.status == "feasible"
        and result.best_bound == 600
        and result.absolute_gap == 50
    )


def test_greedy_grade_tie_uses_lower_reinforcement_instead_of_higher_grade():
    data = data_set()
    data["salaries"].append(
        changed(data["salaries"][0], reinforce_level=6, version="ability-v6")
    )
    data["prices"].append(changed(data["prices"][0], reinforce_level=6))
    data["reinforcements"].append(
        ReinforceStats(
            **common(
                batch_id=BATCHES.reinforce_stats,
                version="ability-v6",
                reinforce_level=6,
                baseline_reinforce_level=5,
                baseline_card_version="base-v1",
                stats_delta={"speed": 0},
                position_ovr={"GK": 100},
            )
        )
    )
    snap = snapshot(data, allowed_reinforcements=(6, 5))
    for method in ("cheapest", "performance"):
        assert (
            greedy_squad(snap, method=method, clock=lambda: OBSERVED)
            .validation.players[0]
            .reinforce_level
            == 5
        )


def test_invalid_snapshot_type_and_naive_clock_are_not_empty_candidate_successes():
    assert solve_squad(None, clock=lambda: OBSERVED).status == "internal_error"
    with pytest.raises(ValueError, match="시간대"):
        solve_squad(snapshot(), clock=lambda: OBSERVED.replace(tzinfo=None))


def test_expiry_during_independent_validation_is_checked_after_validation():
    from FRS.core.squad_recommend import solver

    actual = solver.validate_squad
    state = {"when": OBSERVED}

    def delayed(*args, **kwargs):
        report = actual(*args, **kwargs)
        state["when"] = VALID
        return report

    with patch.object(solver, "validate_squad", side_effect=delayed):
        result = solve_squad(snapshot(), clock=lambda: state["when"])
    assert result.status == "data_expired" and result.validation is None


@pytest.mark.parametrize("status", [cp_model.UNKNOWN, cp_model.INFEASIBLE])
def test_non_solution_results_also_apply_response_expiry(status):
    times = iter((OBSERVED, VALID))
    with patch.object(cp_model.CpSolver, "solve", return_value=status):
        result = solve_squad(snapshot(), clock=lambda: next(times))
    assert result.status == "data_expired" and result.validation is None


def test_salary_maximum_signed_int64_cap_is_kept_exact():
    cap = 2**63 - 1
    cost = cap // 11
    result = solve(snapshot(data_set(salary=cost), salary_cap=cap))
    assert result.status == "optimal" and result.validation.total_salary == cost * 11


def test_too_few_distinct_people_is_infeasible_even_with_nonempty_slot_pools():
    data = data_set()
    # 한 카드가 두 자리 후보로 남지만 전체 인물 수는 10명뿐이다.
    data["cards"][0] = changed(
        data["cards"][0], positions=["GK", "RB"], position_ovr={"GK": 100, "RB": 100}
    )
    for key in data:
        data[key] = [r for r in data[key] if r.spid != 123000004]
    result = solve(snapshot(data))
    assert result.status == "infeasible" and all(
        count > 0 for _, count in result.coverage
    )
