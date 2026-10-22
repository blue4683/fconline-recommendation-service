"""가상 선수로 OVR 우선·동점 사용액 최대화·시간/정수 경계를 검증한다."""

from unittest.mock import patch

import pytest
from ortools.sat.python import cp_model
from test_candidate_search import changed
from test_schema import OBSERVED, VALID
from test_squad_engine import data_set, snapshot, solve

from FRS.core.squad_recommend.solver import solve_squad


def choices(unit=10, deltas=(60, 50), ovrs=(100, 100)):
    data = data_set(cost=unit)
    index = next(i for i, c in enumerate(data["cards"]) if c.positions == ("ST",))
    for person, delta, ovr in zip((100, 101), deltas, ovrs, strict=True):
        for resource in data:
            if resource == "reinforcements":
                continue
            row = data[resource][index]
            updates = dict(spid=123000000 + person)
            if resource == "cards":
                updates.update(
                    person_id=person,
                    name=f"가상선수_{person:04d}",
                    position_ovr={"ST": ovr},
                )
            elif resource == "prices":
                updates.update(price=unit + delta)
            data[resource].append(changed(row, **updates))
    return data


@pytest.mark.parametrize("unit", [10, 10**16, 10**18, 10**32])
def test_tied_ovr_uses_the_most_budget_with_exact_carries_and_large_bp(unit):
    deltas = (999_999, 1_000_000)
    cap = 11 * unit + sum(deltas) + 1
    result = solve(snapshot(choices(unit, deltas), price_cap=cap))
    assert result.status == "optimal" and result.price_optimization_status == "optimal"
    assert result.validation.valid and result.validation.total_ovr == 1100
    assert result.validation.total_price == cap - 1
    assert result.best_bound == 550 and result.absolute_gap == 0


def test_one_bp_over_budget_option_is_excluded_from_the_secondary_optimum():
    unit = 10**30
    result = solve(snapshot(choices(unit, (1, 2)), price_cap=11 * unit + 2))
    assert result.validation.total_price == 11 * unit + 2
    assert result.validation.total_ovr == 1100
    assert result.price_optimization_status == "optimal"


def test_spending_never_trades_away_a_single_ovr_point():
    data = choices(deltas=(1, 1_000_000), ovrs=(101, 100))
    result = solve(snapshot(data, price_cap=2_000_000))
    assert result.validation.total_ovr == 1101
    assert result.validation.total_price == 1_000_111
    assert result.price_optimization_status == "optimal"
    # 비싼 저점수 후보가 더 좋은 OVR을 포기하게 만들지 않는다.
    data = choices(deltas=(1_000_000, 2_000_000), ovrs=(99, 99))
    result = solve(snapshot(data, price_cap=4_000_000))
    assert result.validation.total_ovr == 1100
    assert result.validation.total_price == 110


def test_primary_feasible_result_is_not_frozen_as_a_proven_optimum():
    actual = cp_model.CpSolver.solve
    calls = []

    def feasible(self, model):
        calls.append(self)
        assert actual(self, model) == cp_model.OPTIMAL
        return cp_model.FEASIBLE

    with patch.object(cp_model.CpSolver, "solve", feasible):
        result = solve(snapshot(choices()))
    assert len(calls) == 1
    assert result.status == "feasible" and result.price_optimization_status == "not_run"
    assert result.validation.valid


def test_primary_and_digit_solves_share_one_ten_second_budget():
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

    with (
        patch("FRS.core.squad_recommend.solver.monotonic", side_effect=(0, 2, 6, 11)),
        patch.object(cp_model.CpSolver, "solve", inspect),
    ):
        result = solve(
            snapshot(choices(10**25, (10**25, 2 * 10**25)), price_cap=10**30)
        )
    assert seen == [(10, 1, 123), (8, 1, 123), (4, 1, 123)]
    assert (
        result.status == "optimal" and result.price_optimization_status == "incomplete"
    )
    assert result.validation.valid and result.validation.total_ovr == 1100
    assert result.best_bound == 550 and result.absolute_gap == 0


def test_no_secondary_solve_starts_when_primary_used_the_whole_time_budget():
    actual = cp_model.CpSolver.solve
    calls = []

    def inspect(self, model):
        calls.append(self)
        return actual(self, model)

    with (
        patch("FRS.core.squad_recommend.solver.monotonic", side_effect=(0, 11)),
        patch.object(cp_model.CpSolver, "solve", inspect),
    ):
        result = solve(snapshot(choices()))
    assert len(calls) == 1
    assert (
        result.status == "optimal" and result.price_optimization_status == "incomplete"
    )
    assert result.validation.valid


@pytest.mark.parametrize("secondary", [cp_model.UNKNOWN, cp_model.FEASIBLE])
def test_unfinished_secondary_keeps_the_valid_ovr_optimum_and_primary_bound(secondary):
    actual = cp_model.CpSolver.solve
    calls = []

    def unfinished(self, model):
        calls.append(self)
        if len(calls) == 1 or secondary == cp_model.FEASIBLE:
            assert actual(self, model) == cp_model.OPTIMAL
        return cp_model.OPTIMAL if len(calls) == 1 else secondary

    with patch.object(cp_model.CpSolver, "solve", unfinished):
        result = solve(snapshot(choices()))
    assert len(calls) == 2
    assert (
        result.status == "optimal" and result.price_optimization_status == "incomplete"
    )
    assert result.validation.valid and result.validation.total_ovr == 1100
    assert result.best_bound == 550 and result.absolute_gap == 0


@pytest.mark.parametrize(
    "secondary", [cp_model.MODEL_INVALID, cp_model.INFEASIBLE, "error"]
)
def test_secondary_model_or_solver_failures_do_not_return_an_unchecked_squad(secondary):
    actual = cp_model.CpSolver.solve
    calls = []

    def fail(self, model):
        calls.append(self)
        if len(calls) == 1:
            return actual(self, model)
        if secondary == "error":
            raise RuntimeError("가상 보조 탐색 오류")
        return secondary

    with patch.object(cp_model.CpSolver, "solve", fail):
        result = solve(snapshot(choices()))
    assert result.status == "internal_error"
    assert result.validation is None and result.price_optimization_status is None


def test_expiry_still_suppresses_both_optimization_results():
    times = iter((OBSERVED, VALID))
    result = solve_squad(snapshot(choices()), clock=lambda: next(times))
    assert result.status == "data_expired"
    assert result.validation is None and result.price_optimization_status is None


def test_an_unfinished_secondary_solution_cannot_reduce_incumbent_spending():
    snap = snapshot(choices())
    rows = snap.to_dict()["candidates"]
    actual = cp_model.CpSolver.solve
    calls = []

    def restricted(self, model):
        calls.append(self)
        clone = model.clone()
        for person in (100, 101):
            variables = [
                clone.get_bool_var_from_proto_index(i)
                for i, row in enumerate(rows)
                if row["observations"]["player_card"]["person_id"] == person
            ]
            clone.add(sum(variables) == (1 if len(calls) == 1 else 0))
        assert actual(self, clone) == cp_model.OPTIMAL
        return cp_model.OPTIMAL if len(calls) == 1 else cp_model.FEASIBLE

    with patch.object(cp_model.CpSolver, "solve", restricted):
        result = solve(snap)
    assert len(calls) == 2
    assert result.price_optimization_status == "incomplete"
    assert result.validation.valid and result.validation.total_ovr == 1100
    assert result.validation.total_price == 220
