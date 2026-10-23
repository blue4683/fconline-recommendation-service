"""가상 선수로 필드 우선·GK 단계·다요소 보조·사용액 목적과 기존 OVR 모드 불변을 검증한다."""

import itertools
import json
import random
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError
from test_candidate_search import BATCHES, CLUBS, changed
from test_schema import OBSERVED, VALID
from test_squad_engine import SLOTS, data_set, forge, snapshot, solve

from FRS.core.squad_recommend.multi_factor_policy import ScoringPolicy
from FRS.core.squad_recommend.objective import (
    GK_PRIORITY,
    MultiFactorObjective,
    objective_spec,
)
from FRS.core.squad_recommend.snapshot import SnapshotQuery, build_snapshot
from FRS.core.squad_recommend.solver import (
    engine_spec,
    gk_priority_engine_spec,
    greedy_squad,
    solve_squad,
)
from FRS.schema.abilities import STAT_NAMES

EXAMPLE = (
    Path(__file__).resolve().parents[1] / "docs/examples/multi-factor-core-policy.json"
)
POLICY = ScoringPolicy.model_validate(json.loads(EXAMPLE.read_text()))
OBJECTIVE = MultiFactorObjective(policy=POLICY, expected_policy_version=POLICY.version)


def stats(value):
    return {name: value for name in STAT_NAMES}


def rich(data, *, value=100, feet=(1, 5)):
    """다요소 점수에 필요한 34개 능력치·양발·신체를 가상 값으로 채운다."""
    data["cards"] = [
        changed(
            row,
            stats=stats(value),
            left_foot=feet[0],
            right_foot=feet[1],
            height=180,
            weight=75,
            physical="가상체형_A",
        )
        for row in data["cards"]
    ]
    return data


def base(**kwargs):
    return rich(data_set(**kwargs))


def add(data, position, person, *, ovr=100, salary=5, price=10, season=123, **card):
    """position 계열의 첫 기본 행을 복제해 다른 인물의 추가 후보를 만든다."""
    index = next(i for i, c in enumerate(data["cards"]) if c.positions == (position,))
    spid = season * 1_000_000 + person
    for resource in ("cards", "salaries", "prices", "team_colors"):
        row = data[resource][index]
        updates = dict(spid=spid)
        if resource == "cards":
            updates.update(
                season_id=season,
                person_id=person,
                name=f"가상선수_{person:04d}",
                position_ovr={position: ovr},
                **card,
            )
        elif resource == "salaries":
            updates.update(salary=salary)
        elif resource == "prices":
            updates.update(price=price)
        data[resource].append(changed(row, **updates))
    return spid


def mf(data, **changes):
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
    return build_snapshot(
        SnapshotQuery(**values), **data, supported_clubs=CLUBS, objective=OBJECTIVE
    )


def picked(result):
    return {p.slot: p for p in result.validation.players}


def test_without_objective_snapshot_and_result_bytes_are_unchanged():
    data = base()
    plain = snapshot(data)
    content = plain.to_dict()
    assert "objective" not in content["metadata"]
    assert all("score_units" not in row for row in content["candidates"])
    result = solve(plain)
    dumped = result.model_dump()
    assert result.objective == "ovr" and result.objective_stages == ()
    assert "objective" not in dumped and "objective_stages" not in dumped
    assert "total_score_units" not in dumped["validation"]
    assert all("score_units" not in p for p in dumped["validation"]["players"])
    assert result.engine_version == engine_spec()["engine_version"]
    assert mf(data).snapshot_id != plain.snapshot_id


def test_outfield_ovr_comes_first_beyond_the_fifteen_point_exchange():
    data = base()
    striker = add(data, "ST", 201, ovr=120, salary=10)
    keeper = add(data, "GK", 202, ovr=130, salary=10)
    # 급여 상한 60에서는 둘 중 하나만 올릴 수 있다. GK를 올리면 필드가 20 낮아진다.
    ovr = solve(snapshot(data, salary_cap=60))
    assert picked(ovr)["GK"].spid == keeper and ovr.validation.total_ovr == 1130
    result = solve(mf(data, salary_cap=60))
    assert result.status == "optimal" and result.objective == GK_PRIORITY
    assert picked(result)["GK"].spid != keeper
    assert striker in {p.spid for p in result.validation.players}
    assert [(s.name, s.status, s.value) for s in result.objective_stages] == [
        ("outfield_ovr", "optimal", 1020),
        ("gk_ovr", "optimal", 100),
        ("multi_factor", "optimal", result.validation.total_score_units),
    ]
    assert result.best_bound is None and result.absolute_gap is None
    assert result.engine_version == gk_priority_engine_spec()["engine_version"]


def test_goalkeeper_can_take_up_to_fifteen_outfield_points():
    data = base()
    striker = add(data, "ST", 201, ovr=110, salary=10)
    keeper = add(data, "GK", 202, ovr=120, salary=10)
    # GK를 올리면 필드가 10 낮아지므로 양보폭 15 안이다.
    result = solve(mf(data, salary_cap=60))
    assert picked(result)["GK"].spid == keeper
    assert striker not in {p.spid for p in result.validation.players}
    assert [s.value for s in result.objective_stages[:2]] == [1010, 120]


@pytest.mark.parametrize("gain, goalkeeper_first", [(15, True), (16, False)])
def test_goalkeeper_exchange_boundary_is_fifteen(gain, goalkeeper_first):
    data = base()
    add(data, "ST", 201, ovr=100 + gain, salary=10)
    keeper = add(data, "GK", 202, ovr=140, salary=10)
    result = solve(mf(data, salary_cap=60))
    assert (picked(result)["GK"].spid == keeper) is goalkeeper_first


def test_goalkeeper_uses_the_salary_left_after_the_outfield():
    data = base()
    striker = add(data, "ST", 201, ovr=130, salary=6)
    add(data, "GK", 202, ovr=120, salary=12)
    second = add(data, "GK", 203, ovr=110, salary=11)
    # 더 좋은 GK는 필드 30을 포기해야 하므로 남은 급여로 살 수 있는 GK를 고른다.
    result = solve(mf(data, salary_cap=62))
    assert striker in {p.spid for p in result.validation.players}
    assert picked(result)["GK"].spid == second
    assert result.objective_stages[1].value == 110


@pytest.mark.parametrize("drop, chosen", [(5, True), (6, False)])
def test_outfield_tolerance_admits_a_better_multi_factor_card_up_to_minus_five(
    drop, chosen
):
    data = base()
    # 약발 5인 대체 ST는 OVR이 낮지만 다요소 점수가 더 높다.
    alternative = add(data, "ST", 201, ovr=100 - drop, left_foot=5, right_foot=5)
    result = solve(mf(data))
    assert result.status == "optimal"
    spids = {p.spid for p in result.validation.players}
    assert (alternative in spids) is chosen
    assert result.objective_stages[0].value == 1000
    total_outfield = sum(p.raw_ovr for p in result.validation.players if p.slot != "GK")
    assert total_outfield >= 1000 - 5


@pytest.mark.parametrize("drop, chosen", [(2, True), (3, False)])
def test_goalkeeper_tolerance_is_minus_two(drop, chosen):
    data = base()
    keeper = add(data, "GK", 201, ovr=100 - drop, stats=stats(150))
    result = solve(mf(data))
    assert (picked(result)["GK"].spid == keeper) is chosen


def test_equal_multi_factor_total_spends_more_but_never_trades_score_for_price():
    data = base()
    expensive = add(data, "ST", 201, price=70)
    cheaper = add(data, "ST", 202, price=60)
    # 매우 비싸지만 능력치가 낮은 후보는 다요소 합을 낮추므로 고르지 않는다.
    weaker = add(data, "ST", 203, price=10_000, stats=stats(90))
    result = solve(mf(data, price_cap=100_000))
    spids = {p.spid for p in result.validation.players}
    assert {expensive, cheaper} <= spids and weaker not in spids
    assert result.validation.total_price == 9 * 10 + 70 + 60
    assert result.price_optimization_status == "optimal"


def test_multi_factor_exclusion_is_reported_and_not_counted_for_expiry():
    data = base()
    early = VALID - timedelta(minutes=30)
    spid = add(data, "ST", 201, left_foot=None)
    for resource in ("cards", "salaries", "prices", "team_colors"):
        data[resource][-1] = changed(data[resource][-1], valid_until=early)
    plain = snapshot(data).to_dict()
    assert plain["metadata"]["valid_until"] == early.isoformat()
    content = mf(data).to_dict()
    assert content["metadata"]["valid_until"] == VALID.isoformat()
    assert spid not in {row["spid"] for row in content["candidates"]}
    reasons = {
        (row["slot"], row["resource"], row["reason"])
        for row in content["exclusions"]
        if row["spid"] == spid and row["resource"] != "player_card"
    }
    assert reasons == {
        (slot, "multi_factor", "missing_feature") for slot in ("LS", "RS")
    }


def test_snapshot_pins_policy_text_and_candidate_scores():
    content = mf(base()).to_dict()
    assert content["metadata"]["objective"] == objective_spec(OBJECTIVE)
    assert content["metadata"]["objective"]["outfield_tolerance"] == 5
    assert content["metadata"]["objective"]["gk_tolerance"] == 2
    assert all(isinstance(row["score_units"], int) for row in content["candidates"])


def bump_score(content):
    content["candidates"][0]["score_units"] += 1


def change_weight(content):
    content["metadata"]["objective"]["policy"]["weak_foot_scores"][0] = 1


def drop_objective(content):
    del content["metadata"]["objective"]


def loosen_tolerance(content):
    content["metadata"]["objective"]["outfield_tolerance"] = 50


@pytest.mark.parametrize(
    "change, reason",
    [
        (bump_score, "multi_factor_mismatch"),
        (change_weight, "objective_mismatch"),
        (drop_objective, "multi_factor_mismatch"),
        (loosen_tolerance, "objective_mismatch"),
    ],
)
def test_validator_recomputes_scores_and_rejects_forged_objectives(change, reason):
    result = solve(forge(mf(base()), change))
    assert result.status == "internal_error" and result.reasons == (reason,)
    assert result.validation is None


def test_policy_version_must_match_its_content():
    with pytest.raises(ValidationError):
        MultiFactorObjective(policy=POLICY, expected_policy_version="0" * 64)


@pytest.mark.parametrize(
    "call, stages",
    [
        (1, ("incomplete", "not_run", "not_run")),
        (2, ("optimal", "incomplete", "not_run")),
        (3, ("optimal", "optimal", "incomplete")),
    ],
)
def test_unproven_stage_keeps_the_valid_solution_and_skips_later_stages(call, stages):
    actual = cp_model.CpSolver.solve
    calls = []

    def unfinished(self, model):
        calls.append(self)
        raw = actual(self, model)
        return cp_model.FEASIBLE if len(calls) == call else raw

    data = base()
    add(data, "ST", 201, ovr=110, salary=10)
    add(data, "GK", 202, ovr=120, salary=10)
    with patch.object(cp_model.CpSolver, "solve", unfinished):
        result = solve(mf(data, salary_cap=60))
    assert len(calls) == call
    assert result.status == "feasible" and result.validation.valid
    assert tuple(s.status for s in result.objective_stages) == stages
    assert result.price_optimization_status == "not_run"


def test_stages_share_one_time_budget():
    seen = []
    actual = cp_model.CpSolver.solve

    def inspect(self, model):
        seen.append(self.parameters.max_time_in_seconds)
        return actual(self, model)

    with (
        patch("FRS.core.squad_recommend.solver.monotonic", side_effect=(0, 1, 31)),
        patch.object(cp_model.CpSolver, "solve", inspect),
    ):
        result = solve(mf(base()))
    # 명시 다요소 목적만 기본 30초를 공유하고 OVR 모드는 10초를 유지한다.
    assert seen == [29] and result.config.max_time_seconds == 30
    assert solve(snapshot(base())).config.max_time_seconds == 10
    assert result.status == "feasible"
    assert [s.status for s in result.objective_stages] == [
        "optimal",
        "incomplete",
        "not_run",
    ]


def test_greedy_baseline_and_expiry_keep_the_objective_label():
    snap = mf(base())
    greedy = greedy_squad(snap, method="performance", clock=lambda: OBSERVED)
    assert greedy.status == "feasible" and greedy.objective == GK_PRIORITY
    assert greedy.validation.total_score_units is not None
    expired = solve_squad(snap, clock=lambda: VALID)
    assert expired.status == "data_expired" and expired.objective == GK_PRIORITY


def lexicographic(snap):
    """작은 문제를 전수 탐색해 단계별 사전식 최적값을 구한다."""
    content = snap.to_dict()
    query = content["query"]
    pools = [[r for r in content["candidates"] if r["slot"] == s] for s in SLOTS]
    rows = []
    for combo in itertools.product(*pools):
        spids = {r["spid"] for r in combo}
        people = {r["observations"]["player_card"]["person_id"] for r in combo}
        price = sum(r["observations"]["card_price"]["price"] for r in combo)
        salary = sum(r["observations"]["card_salary"]["salary"] for r in combo)
        if len(spids) != 11 or len(people) != 11:
            continue
        if price > query["price_cap"] or salary > query["salary_cap"]:
            continue
        outfield = sum(r["raw_ovr"] for r in combo if r["slot"] != "GK")
        keeper = sum(r["raw_ovr"] for r in combo if r["slot"] == "GK")
        score = sum(r["score_units"] for r in combo)
        rows.append((outfield, keeper, score, price))
    if not rows:
        return None
    best_outfield = max(r[0] for r in rows)
    exchange = [r for r in rows if r[0] >= best_outfield - 15]
    best_keeper = max(r[1] for r in exchange)
    outfield = max(r[0] for r in exchange if r[1] == best_keeper)
    allowed = [r for r in rows if r[0] >= outfield - 5 and r[1] >= best_keeper - 2]
    best_score = max(r[2] for r in allowed)
    best_price = max(r[3] for r in allowed if r[2] == best_score)
    return best_outfield, best_keeper, outfield, best_score, best_price


@pytest.mark.parametrize("seed", range(12))
def test_small_random_problems_match_exhaustive_lexicographic_optimum(seed):
    rng = random.Random(seed)
    data = base()
    data["cards"] = [
        changed(row, stats=stats(rng.randint(80, 150)), left_foot=rng.randint(1, 5))
        for row in data["cards"]
    ]
    positions = [c.positions[0] for c in data["cards"]]
    extras = {slot: 1 for slot in range(11)}
    for slot in rng.sample(range(11), 3):
        extras[slot] = 2
    person = 300
    for slot, count in extras.items():
        for _ in range(count):
            person += 1
            # 일부는 직전 후보와 같은 인물의 다른 시즌 카드로 만든다.
            same = rng.random() < 0.1
            add(
                data,
                positions[slot],
                person - 1 if same else person,
                season=124 if same else 123,
                ovr=rng.randint(92, 110),
                salary=rng.randint(2, 9),
                price=rng.randint(1, 60),
                stats=stats(rng.randint(80, 150)),
                left_foot=rng.randint(1, 5),
            )
    snap = mf(data, salary_cap=rng.randint(50, 70), price_cap=rng.randint(150, 400))
    expected = lexicographic(snap)
    result = solve(snap)
    if expected is None:
        assert result.status == "infeasible"
        return
    best_outfield, best_keeper, gk_outfield, best_score, best_price = expected
    assert result.status == "optimal"
    assert result.price_optimization_status == "optimal"
    assert [s.value for s in result.objective_stages] == [
        best_outfield,
        best_keeper,
        best_score,
    ]
    report = result.validation
    outfield = sum(p.raw_ovr for p in report.players if p.slot != "GK")
    keeper = picked(result)["GK"].raw_ovr
    assert outfield >= gk_outfield - 5 and keeper >= best_keeper - 2
    assert report.total_score_units == best_score
    assert report.total_price == best_price
