"""수동/합성 근거의 정규화·명시 스냅샷·기존 CP-SAT를 외부 I/O 없이 연결한다."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from FRS.core.candidate_search.search import BatchSelection
from FRS.core.request_normalization import (
    ConditionNormalizationResult,
    ExtractedConditions,
    conditions_spec,
    normalize_conditions,
)
from FRS.core.squad_recommend.objective import MultiFactorObjective
from FRS.core.squad_recommend.snapshot import (
    RequestSnapshot,
    SnapshotQuery,
    build_snapshot,
    snapshot_spec,
)
from FRS.core.squad_recommend.solver import (
    EngineResult,
    SolverConfig,
    engine_spec,
    solve_squad,
)
from FRS.schema.models import (
    CardPrice,
    CardSalary,
    PlayerCard,
    ReinforceStats,
    TeamColor,
)


@dataclass(frozen=True)
class SquadBuildResult:
    normalization: ConditionNormalizationResult
    build_policy_version: str
    snapshot: RequestSnapshot | None = None
    engine: EngineResult | None = None


def build_spec(*, fixed_salary_cap=None):
    spec = dict(
        version=1,
        normalizer_version=conditions_spec(fixed_salary_cap=fixed_salary_cap)[
            "normalizer_version"
        ],
        snapshot_policy_version=snapshot_spec()["snapshot_policy_version"],
        engine_version=engine_spec()["engine_version"],
        solver_config=SolverConfig().model_dump(),
        normalization_issue="return_without_context_material_clock_snapshot_or_solver",
        internal_context="explicit_resource_batches_aware_t0_and_response_clock",
        material="provided_iterables_only_snapshot_copies_once",
        solver="cp_sat_with_existing_independent_validation_and_expiry",
        post_solver="immediate_result_without_render_or_material_reread",
        fallback=False,
        llm=False,
        io=False,
    )
    encoded = json.dumps(
        spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return {
        **spec,
        "build_policy_version": hashlib.sha256(encoded.encode()).hexdigest(),
    }


def build_squad_request(
    original: str,
    extracted: ExtractedConditions,
    *,
    batches: BatchSelection,
    t0: datetime,
    clock: Callable[[], datetime],
    cards: Iterable[PlayerCard],
    salaries: Iterable[CardSalary],
    prices: Iterable[CardPrice],
    reinforcements: Iterable[ReinforceStats],
    team_colors: Iterable[TeamColor],
    fixed_salary_cap=None,
    objective: MultiFactorObjective | None = None,
) -> SquadBuildResult:
    """objective는 내부 명시 호출 전용이다. 생략하면 기존 OVR 목적을 쓴다."""
    normalized = normalize_conditions(
        original, extracted, fixed_salary_cap=fixed_salary_cap
    )
    version = build_spec(fixed_salary_cap=fixed_salary_cap)["build_policy_version"]
    if normalized.issues:
        return SquadBuildResult(normalized, version)
    if not isinstance(batches, BatchSelection):
        raise TypeError("자료 종류별 BatchSelection을 명시해야 합니다.")
    if not isinstance(t0, datetime) or t0.tzinfo is None or t0.utcoffset() is None:
        raise ValueError("기준 시각은 시간대 있는 datetime으로 명시해야 합니다.")
    if not callable(clock):
        raise TypeError("응답 확인 시계를 명시해야 합니다.")
    query = SnapshotQuery(
        **normalized.conditions.model_dump(),
        batches=BatchSelection.model_validate(batches.model_dump()),
        t0=t0,
    )
    snapshot = build_snapshot(
        query,
        cards=cards,
        salaries=salaries,
        prices=prices,
        reinforcements=reinforcements,
        team_colors=team_colors,
        objective=objective,
    )
    # 비용이 큰 검증·만료 확인은 기존 엔진이 마친다. 이후 자료 재조회/표 생성은 없다.
    engine = solve_squad(snapshot, clock=clock)
    return SquadBuildResult(normalized, version, snapshot, engine)
