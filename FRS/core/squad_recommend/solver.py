"""같은 스냅샷으로 CP-SAT와 탐욕 비교를 실행하고 독립 검증 후에만 해를 반환한다."""

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from time import monotonic
from typing import Annotated, Callable, Literal

import ortools
from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict, Field, model_serializer

from FRS.core.squad_recommend.objective import (
    DEFAULT_SECONDS,
    GK_EXCHANGE,
    GK_PRIORITY,
    GK_SLOT,
    GK_TOLERANCE,
    OUTFIELD_TOLERANCE,
    STAGES,
)
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
        secondary_objective="maximum_total_price_at_proven_optimal_ovr",
        secondary_precision="exact_safe_integer_objective_or_price_slack_digits",
        search_time_budget="shared_across_primary_and_secondary_solves",
        optimal_status="primary_ovr_proven_price_status_separate",
        budget="exact_base_1000000_digits_with_nonnegative_slack",
        return_solution="independent_validator_and_response_expiry_pass",
        greedy_order="formation_order_then_cost_or_negative_ovr_then_spid_then_grade",
        greedy_backtracking=False,
        greedy_failure="not_infeasibility_proof",
    )
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return {**spec, "engine_version": hashlib.sha256(encoded).hexdigest()}


def gk_priority_engine_spec():
    """명시 다요소 목적의 엔진 사양. 기존 OVR 사양과 해시를 공유하지 않는다."""
    base = {k: v for k, v in engine_spec().items() if k != "engine_version"}
    spec = dict(
        base,
        objective=GK_PRIORITY,
        secondary_objective="lexicographic_stages",
        stages=list(STAGES),
        gk_stage="max_16_gk_plus_outfield_within_outfield_exchange",
        gk_exchange=GK_EXCHANGE,
        outfield_tolerance=OUTFIELD_TOLERANCE,
        gk_tolerance=GK_TOLERANCE,
        default_time_seconds=DEFAULT_SECONDS,
        search_time_budget="shared_across_all_stages",
        optimal_status="outfield_gk_and_multi_factor_stages_proven_price_separate",
        unfinished_stage="keep_best_valid_solution_later_stages_not_run",
        bound="not_reported",
        greedy_order="unchanged_ovr_or_cost_baseline",
    )
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return {**spec, "engine_version": hashlib.sha256(encoded).hexdigest()}


class ObjectiveStage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Literal["outfield_ovr", "gk_ovr", "multi_factor"]
    status: Literal["optimal", "incomplete", "not_run"]
    value: int | None = None


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
    price_optimization_status: Literal["not_run", "optimal", "incomplete"] | None = None
    validation: ValidationReport | None = None
    coverage: tuple[tuple[str, int], ...] = ()
    reasons: tuple[str, ...] = ()
    best_bound: float | None = None
    absolute_gap: float | None = None
    objective: Literal["ovr", "gk_priority_multi_factor"] = "ovr"
    objective_stages: tuple[ObjectiveStage, ...] = ()

    @model_serializer(mode="wrap")
    def _omit_default_objective(self, handler):
        # OVR 목적 결과의 직렬화를 다요소 도입 전과 같게 유지한다.
        data = handler(self)
        if self.objective == "ovr" and not self.objective_stages:
            data.pop("objective", None)
            data.pop("objective_stages", None)
        return data


def _utc_now():
    return datetime.now(timezone.utc)


def _label(objective):
    if objective is None:
        return {}
    return dict(
        objective=GK_PRIORITY,
        engine_version=gk_priority_engine_spec()["engine_version"],
    )


def _initial(snapshot, method, when):
    try:
        prepared = prepare_snapshot(snapshot, when=when)
    except SnapshotExpired:
        # 만료 판정은 메타데이터 검증을 마친 뒤이므로 목적 표시를 그대로 읽는다.
        objective = snapshot.to_dict()["metadata"].get("objective")
        return None, EngineResult(
            method=method,
            status="data_expired",
            snapshot_id=snapshot.snapshot_id,
            **_label(objective),
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
    price_optimization_status=None,
    stages=(),
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
        price_optimization_status=price_optimization_status if report else None,
        validation=report,
        coverage=_coverage(prepared),
        reasons=tuple(reasons),
        best_bound=bound / 2 if bound is not None else None,
        absolute_gap=max(0.0, (bound - report.total_ovr) / 2)
        if bound is not None
        else None,
        objective_stages=tuple(stages) if report else (),
        **_label(prepared.objective),
    )


def _add_exact_cap(model, variables, values, cap, name):
    """선택액+비음수 잔액=상한을 자리올림으로 표현해 금액을 반올림하지 않는다."""
    base = 1_000_000
    count = max(1, (len(str(cap)) + 5) // 6)
    carry = 0
    digits = []
    for digit in range(count):
        power = base**digit
        slack = model.new_int_var(0, base - 1, f"{name}_slack_{digit}")
        digits.append(slack)
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
    return tuple(digits)


def _maximize_price(
    model, variables, candidates, slack_digits, chosen, price_cap, config, deadline
):
    """호출자가 앞 목적을 고정한 모델에서 사용액을 최대화한다.

    큰 BP는 잔액의 높은 자리부터 비교한다.
    """
    prices = [c.observations.card_price.price for c in candidates]
    best = tuple(chosen)
    best_price = sum(price * x for price, x in zip(prices, best, strict=True))
    by_slot = defaultdict(list)
    for c, price in zip(candidates, prices, strict=True):
        by_slot[c.slot].append(price)
    # 자리별 최대액은 중복/급여를 무시한 상한이다. 이미 도달했다면 추가 탐색 불필요.
    ceiling = min(price_cap, sum(max(values) for values in by_slot.values()))
    if sum(prices) <= 2**53 - 1:
        # 목적값/경계의 double 운반도 정수 정밀도를 잃지 않는 작은 금액만 사용한다.
        objectives = [
            (sum(p * x for p, x in zip(prices, variables, strict=True)), None)
        ]
    else:
        floor = sum(min(values) for values in by_slot.values())
        objectives = []
        for index in reversed(range(len(slack_digits))):
            digit = slack_digits[index]
            power = 1_000_000**index
            lo, hi = (price_cap - ceiling) // power, (price_cap - floor) // power
            if lo == hi:
                # 상한/하한으로 이미 고정된 잔액 자리는 다시 탐색하지 않는다.
                model.add(digit == lo % 1_000_000)
            else:
                objectives.append((digit, digit))
    for objective, digit in objectives:
        if best_price == ceiling:
            return best, "optimal"
        if digit is None:
            model.maximize(objective)
        else:
            model.minimize(objective)
        model.clear_hints()
        for variable, value in zip(variables, best, strict=True):
            model.add_hint(variable, value)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return best, "incomplete"
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = remaining
        solver.parameters.num_search_workers = config.workers
        solver.parameters.random_seed = config.seed
        raw = solver.solve(model)
        if raw == cp_model.UNKNOWN:
            return best, "incomplete"
        if raw not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            raise RuntimeError("가격 보조 탐색이 이미 확인한 유효해와 모순됩니다.")
        values = tuple(solver.value(x) for x in variables)
        price = sum(p * x for p, x in zip(prices, values, strict=True))
        # 시간 내 찾은 동점 해가 더 저렴하면 기존 유효해를 유지한다.
        if price >= best_price:
            best, best_price = values, price
        if raw != cp_model.OPTIMAL:
            return best, "incomplete"
        if digit is not None:
            model.add(digit == solver.value(digit))
    return best, "optimal"


def _base_model(prepared):
    """목적과 무관한 11명·중복·정확 금액 제약만 담은 모델을 만든다."""
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
    price_digits = _add_exact_cap(
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
    return model, variables, price_digits


def _solve_stage(model, variables, hint, config, deadline):
    """공유 기한의 남은 시간으로 한 단계를 푼다. 시간이 없으면 실행하지 않는다."""
    model.clear_hints()
    if hint is not None:
        for variable, value in zip(variables, hint, strict=True):
            model.add_hint(variable, value)
    remaining = deadline - monotonic()
    if remaining <= 0:
        return None, None
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = remaining
    solver.parameters.num_search_workers = config.workers
    solver.parameters.random_seed = config.seed
    raw = solver.solve(model)
    values = (
        tuple(solver.value(x) for x in variables)
        if raw in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        else None
    )
    return raw, values


def _solve_gk_priority(snapshot, prepared, *, config, clock):
    """필드 OVR → 필드 고정 GK OVR → 허용폭 안 다요소 합 → 같은 점수 안 사용액."""
    candidates = prepared.candidates
    outfield = [0 if c.slot == GK_SLOT else c.raw_ovr for c in candidates]
    goalkeeper = [c.raw_ovr if c.slot == GK_SLOT else 0 for c in candidates]
    scores = [c.score_units for c in candidates]

    def total(weights, values):
        return sum(w * x for w, x in zip(weights, values, strict=True))

    def constrained(*bounds):
        """새 모델에 (계수, 연산, 값) 제약을 더한다. 앞 단계 고정을 풀 수 있게 한다."""
        model, variables, digits = _base_model(prepared)
        for weights, operator, value in bounds:
            term = sum(w * x for w, x in zip(weights, variables, strict=True))
            model.add(term == value if operator == "==" else term >= value)
        return model, variables, digits

    def maximize(model, variables, weights, hint):
        """단계 목적을 풀고 시작 해보다 나빠지지 않는 해만 채택한다."""
        model.maximize(sum(w * x for w, x in zip(weights, variables, strict=True)))
        raw, values = _solve_stage(model, variables, hint, config, deadline)
        if raw not in (None, cp_model.OPTIMAL, cp_model.FEASIBLE, cp_model.UNKNOWN):
            raise RuntimeError("단계 탐색이 이미 확인한 유효해와 모순됩니다.")
        if values is not None and total(weights, values) >= total(weights, hint):
            hint = values
        return raw == cp_model.OPTIMAL, hint

    def finish(status, *, chosen=(), raw=None, stages=(), price_status=None):
        picks = tuple(
            SquadPick(spid=c.spid, slot=c.slot, reinforce_level=c.reinforce_level)
            for c, x in zip(candidates, chosen, strict=True)
            if x
        )
        ran = {s.name for s in stages}
        stages = (
            *stages,
            *(
                ObjectiveStage(name=name, status="not_run")
                for name in STAGES[:3]
                if name not in ran
            ),
        )
        return _finish(
            snapshot,
            prepared,
            config=config,
            method="cp_sat",
            status=status,
            clock=clock,
            picks=picks,
            solver_status=raw,
            price_optimization_status=price_status,
            stages=stages,
        )

    def stage(name, proven, values, weights):
        return ObjectiveStage(
            name=name,
            status="optimal" if proven else "incomplete",
            value=total(weights, values),
        )

    deadline = monotonic() + config.max_time_seconds
    try:
        model, variables, _ = constrained()
        model.maximize(sum(w * x for w, x in zip(outfield, variables, strict=True)))
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
        raw, chosen = _solve_stage(model, variables, None, config, deadline)
        name = "UNKNOWN" if raw is None else cp_model.CpSolver().status_name(raw)
        if chosen is None:
            status = "infeasible" if raw == cp_model.INFEASIBLE else "unknown"
            if raw not in (None, cp_model.INFEASIBLE, cp_model.UNKNOWN):
                status = "internal_error"
            return finish(status, raw=name)
        done = [stage("outfield_ovr", raw == cp_model.OPTIMAL, chosen, outfield)]
        if raw != cp_model.OPTIMAL:
            return finish(
                "feasible", chosen=chosen, raw=name, stages=done, price_status="not_run"
            )
        best_outfield = total(outfield, chosen)

        # S2: 필드를 최대 GK_EXCHANGE까지 양보해 GK를 최대화하고, 같은 GK면 필드를
        # 최대화한다. 필드 변동폭이 GK_EXCHANGE 이하라 계수 GK_EXCHANGE+1이 정확하다.
        model, variables, _ = constrained((outfield, ">=", best_outfield - GK_EXCHANGE))
        weights = [
            (GK_EXCHANGE + 1) * g + f for g, f in zip(goalkeeper, outfield, strict=True)
        ]
        proven, chosen = maximize(model, variables, weights, chosen)
        done.append(stage("gk_ovr", proven, chosen, goalkeeper))
        if not proven:
            return finish(
                "feasible", chosen=chosen, raw=name, stages=done, price_status="not_run"
            )

        # S3: 필드·GK 허용폭 안에서 다요소 점수 합을 최대화한다.
        bounds = (
            (outfield, ">=", total(outfield, chosen) - OUTFIELD_TOLERANCE),
            (goalkeeper, ">=", total(goalkeeper, chosen) - GK_TOLERANCE),
        )
        model, variables, _ = constrained(*bounds)
        proven, chosen = maximize(model, variables, scores, chosen)
        done.append(stage("multi_factor", proven, chosen, scores))
        if not proven:
            return finish(
                "feasible", chosen=chosen, raw=name, stages=done, price_status="not_run"
            )

        # S4: 같은 다요소 합 안에서 기존 정확 BP 방식으로 사용액을 최대화한다.
        model, variables, digits = constrained(
            *bounds, (scores, "==", total(scores, chosen))
        )
        chosen, price_status = _maximize_price(
            model,
            variables,
            candidates,
            digits,
            chosen,
            prepared.query.price_cap,
            config,
            deadline,
        )
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
    return finish(
        "optimal", chosen=chosen, raw=name, stages=done, price_status=price_status
    )


def solve_squad(
    snapshot: RequestSnapshot,
    *,
    config: SolverConfig | None = None,
    clock: Callable[[], datetime] = _utc_now,
):
    explicit = config is not None
    config = (
        SolverConfig.model_validate(config.model_dump()) if explicit else SolverConfig()
    )
    prepared, invalid = _initial(snapshot, "cp_sat", clock())
    if not explicit and (
        (prepared is not None and prepared.objective is not None)
        or (invalid is not None and invalid.objective == GK_PRIORITY)
    ):
        # 명시 다요소 목적만 기본 30초를 공유한다. OVR 모드는 10초를 유지한다.
        config = SolverConfig(max_time_seconds=DEFAULT_SECONDS)
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
    if prepared.objective is not None:
        return _solve_gk_priority(snapshot, prepared, config=config, clock=clock)
    model, variables, price_digits = _base_model(prepared)
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
    deadline = monotonic() + config.max_time_seconds
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
    price_status = None
    if status in ("optimal", "feasible"):
        chosen = tuple(solver.value(x) for x in variables)
        bound = solver.best_objective_bound
        price_status = "not_run"
        if status == "optimal":
            try:
                ovr = sum(
                    c.raw_ovr * x
                    for c, x in zip(prepared.candidates, chosen, strict=True)
                )
                model.add(
                    sum(
                        c.raw_ovr * x
                        for c, x in zip(prepared.candidates, variables, strict=True)
                    )
                    == ovr
                )
                chosen, price_status = _maximize_price(
                    model,
                    variables,
                    prepared.candidates,
                    price_digits,
                    chosen,
                    prepared.query.price_cap,
                    config,
                    deadline,
                )
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
        picks = tuple(
            SquadPick(spid=c.spid, slot=c.slot, reinforce_level=c.reinforce_level)
            for c, x in zip(prepared.candidates, chosen, strict=True)
            if x
        )
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
        price_optimization_status=price_status,
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
