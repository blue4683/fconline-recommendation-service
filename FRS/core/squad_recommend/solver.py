"""같은 스냅샷으로 CP-SAT와 탐욕 비교를 실행하고 독립 검증 후에만 해를 반환한다."""

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Annotated, Callable, Literal

import ortools
from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict, Field

from FRS.core.squad_recommend.snapshot import RequestSnapshot
from FRS.core.squad_recommend.validator import (
    SnapshotError,
    SnapshotExpired,
    SquadPick,
    SquadSelection,
    ValidationReport,
    prepare_snapshot,
    validate_squad,
)
from FRS.schema.scope import FORMATION_SLOTS


class SolverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_time_seconds: Annotated[
        float, Field(strict=True, gt=0, allow_inf_nan=False)
    ] = 10.0
    workers: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    seed: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] = 123


def engine_spec():
    spec = dict(
        version=1,
        slots="exactly_one_per_formation_slot",
        uniqueness="at_most_one_spid_and_person_id",
        club="all_11_positive_membership_without_bonus",
        objective="sum_absolute_position_ovr",
        budget="exact_base_1000000_digits_with_nonnegative_slack",
        return_solution="independent_validator_and_response_expiry_pass",
        greedy_order="formation_order_then_cost_or_negative_ovr_then_spid_then_grade",
        greedy_backtracking=False,
        greedy_failure="not_infeasibility_proof",
    )
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return {**spec, "engine_version": hashlib.sha256(encoded).hexdigest()}


class EngineResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    method: Literal["cp_sat", "cheapest", "performance"]
    status: Literal[
        "optimal",
        "feasible",
        "infeasible",
        "unknown",
        "internal_error",
        "data_expired",
        "greedy_failed",
    ]
    snapshot_id: str
    engine_version: str = Field(default_factory=lambda: engine_spec()["engine_version"])
    ortools_version: str = ortools.__version__
    config: SolverConfig | None = None
    solver_status: str | None = None
    validation: ValidationReport | None = None
    coverage: tuple[tuple[str, int], ...] = ()
    reasons: tuple[str, ...] = ()
    best_bound: float | None = None
    absolute_gap: float | None = None


def _utc_now():
    return datetime.now(timezone.utc)


def _initial(snapshot, method, when):
    try:
        prepared = prepare_snapshot(snapshot, when=when)
    except SnapshotExpired:
        return None, EngineResult(
            method=method, status="data_expired", snapshot_id=snapshot.snapshot_id
        )
    except SnapshotError as error:
        return None, EngineResult(
            method=method,
            status="internal_error",
            snapshot_id=getattr(snapshot, "snapshot_id", ""),
            reasons=(str(error),),
        )
    return prepared, None


def _coverage(prepared):
    return tuple(
        (slot, sum(c.slot == slot for c in prepared.candidates))
        for slot in FORMATION_SLOTS[prepared.query.formation]
    )


def _finish(
    snapshot,
    prepared,
    *,
    method,
    status,
    clock,
    picks=(),
    solver_status=None,
    reasons=(),
    bound=None,
    config=None,
):
    report = None
    if status in ("optimal", "feasible"):
        selection = SquadSelection(snapshot_id=prepared.snapshot_id, picks=tuple(picks))
        report = validate_squad(snapshot, selection, when=prepared.query.t0)
        if not report.valid:
            status = (
                "data_expired" if "data_expired" in report.errors else "internal_error"
            )
            reasons = report.errors
            report = None
    # 비용이 큰 독립 검증을 마친 뒤 응답 직전의 시각으로 최저 기한을 확인한다.
    when = clock()
    if (
        not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        raise ValueError("응답 확인 시각에는 시간대가 필요합니다.")
    if when < prepared.query.t0:
        status, reasons, report = "internal_error", ("time_before_snapshot",), None
    elif prepared.valid_until is not None and when >= prepared.valid_until:
        status, reasons, report = "data_expired", ("data_expired",), None
    if report is None:
        bound = None
    return EngineResult(
        method=method,
        config=config,
        status=status,
        snapshot_id=prepared.snapshot_id,
        solver_status=solver_status,
        validation=report,
        coverage=_coverage(prepared),
        reasons=tuple(reasons),
        best_bound=bound / 2 if bound is not None else None,
        absolute_gap=max(0.0, (bound - report.total_ovr) / 2)
        if bound is not None
        else None,
    )


def _add_exact_cap(model, variables, values, cap, name):
    """선택액+비음수 잔액=상한을 자리올림으로 표현해 금액을 반올림하지 않는다."""
    base = 1_000_000
    count = max(1, (len(str(cap)) + 5) // 6)
    carry = 0
    for digit in range(count):
        power = base**digit
        slack = model.new_int_var(0, base - 1, f"{name}_slack_{digit}")
        next_carry = (
            model.new_int_var(0, 11, f"{name}_carry_{digit}")
            if digit + 1 < count
            else 0
        )
        amount = sum(
            ((value // power) % base) * variable
            for value, variable in zip(values, variables, strict=True)
        )
        model.add(amount + slack + carry == (cap // power) % base + base * next_carry)
        carry = next_carry


def solve_squad(
    snapshot: RequestSnapshot,
    *,
    config: SolverConfig | None = None,
    clock: Callable[[], datetime] = _utc_now,
):
    config = (
        SolverConfig()
        if config is None
        else SolverConfig.model_validate(config.model_dump())
    )
    prepared, invalid = _initial(snapshot, "cp_sat", clock())
    if invalid is not None:
        return invalid.model_copy(update={"config": config})
    missing = tuple(slot for slot, count in _coverage(prepared) if count == 0)
    if missing:
        return _finish(
            snapshot,
            prepared,
            config=config,
            method="cp_sat",
            status="infeasible",
            clock=clock,
            reasons=tuple(f"empty_slot:{slot}" for slot in missing),
        )
    model = cp_model.CpModel()
    variables = [
        model.new_bool_var(f"pick_{i}") for i in range(len(prepared.candidates))
    ]
    slots, cards, people = defaultdict(list), defaultdict(list), defaultdict(list)
    for candidate, variable in zip(prepared.candidates, variables, strict=True):
        slots[candidate.slot].append(variable)
        cards[candidate.spid].append(variable)
        people[candidate.observations.player_card.person_id].append(variable)
    for slot in FORMATION_SLOTS[prepared.query.formation]:
        model.add_exactly_one(slots[slot])
    for group in (*cards.values(), *people.values()):
        model.add(sum(group) <= 1)
    _add_exact_cap(
        model,
        variables,
        [c.observations.card_price.price for c in prepared.candidates],
        prepared.query.price_cap,
        "price",
    )
    _add_exact_cap(
        model,
        variables,
        [c.observations.card_salary.salary for c in prepared.candidates],
        prepared.query.salary_cap,
        "salary",
    )
    model.maximize(
        sum(c.raw_ovr * x for c, x in zip(prepared.candidates, variables, strict=True))
    )
    if model.validate():
        return _finish(
            snapshot,
            prepared,
            config=config,
            method="cp_sat",
            status="internal_error",
            clock=clock,
            solver_status="MODEL_INVALID",
            reasons=("model_invalid",),
        )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = config.max_time_seconds
    solver.parameters.num_search_workers = config.workers
    solver.parameters.random_seed = config.seed
    try:
        raw = solver.solve(model)
    except (ValueError, RuntimeError, OverflowError):
        return _finish(
            snapshot,
            prepared,
            config=config,
            method="cp_sat",
            status="internal_error",
            clock=clock,
            reasons=("solver_failure",),
        )
    statuses = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
        cp_model.MODEL_INVALID: "internal_error",
    }
    status = statuses.get(raw, "internal_error")
    picks, bound = (), None
    if status in ("optimal", "feasible"):
        picks = tuple(
            SquadPick(spid=c.spid, slot=c.slot, reinforce_level=c.reinforce_level)
            for c, x in zip(prepared.candidates, variables, strict=True)
            if solver.value(x)
        )
        bound = solver.best_objective_bound
    return _finish(
        snapshot,
        prepared,
        config=config,
        method="cp_sat",
        status=status,
        clock=clock,
        picks=picks,
        solver_status=solver.status_name(raw),
        bound=bound,
    )


def greedy_squad(
    snapshot: RequestSnapshot,
    *,
    method: Literal["cheapest", "performance"],
    clock: Callable[[], datetime] = _utc_now,
):
    if method not in ("cheapest", "performance"):
        raise ValueError("지원하지 않는 탐욕 방식입니다.")
    prepared, invalid = _initial(snapshot, method, clock())
    if invalid is not None:
        return invalid
    picked, people, cards = [], set(), set()
    cost = salary = 0
    for slot in FORMATION_SLOTS[prepared.query.formation]:
        pool = [c for c in prepared.candidates if c.slot == slot]
        pool.sort(
            key=lambda c: (
                c.observations.card_price.price if method == "cheapest" else -c.raw_ovr,
                c.spid,
                c.reinforce_level,
            )
        )
        chosen = None
        for candidate in pool:
            observations = candidate.observations
            if candidate.spid in cards or observations.player_card.person_id in people:
                continue
            if (
                cost + observations.card_price.price > prepared.query.price_cap
                or salary + observations.card_salary.salary > prepared.query.salary_cap
            ):
                continue
            chosen = candidate
            break
        if chosen is None:
            return _finish(
                snapshot,
                prepared,
                method=method,
                status="greedy_failed",
                clock=clock,
                reasons=(f"no_greedy_choice:{slot}",),
            )
        picked.append(
            SquadPick(
                spid=chosen.spid, slot=slot, reinforce_level=chosen.reinforce_level
            )
        )
        cards.add(chosen.spid)
        people.add(chosen.observations.player_card.person_id)
        cost += chosen.observations.card_price.price
        salary += chosen.observations.card_salary.salary
    return _finish(
        snapshot, prepared, method=method, status="feasible", clock=clock, picks=picked
    )
