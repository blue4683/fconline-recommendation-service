"""명시 선택 시에만 쓰는 필드 우선·GK 단계·다요소 보조 목적의 계약."""

from typing import Annotated

from pydantic import Field, model_validator

from FRS.core.squad_recommend.multi_factor import score_with_validated_policy
from FRS.core.squad_recommend.multi_factor_policy import (
    Frozen,
    ScoringPolicy,
    content_hash,
)
from FRS.core.squad_recommend.performance import PerformanceQuery

GK_PRIORITY = "gk_priority_multi_factor"
GK_SLOT = "GK"
OUTFIELD_TOLERANCE = 5
GK_TOLERANCE = 2
# GK 단계에서 GK를 위해 양보할 수 있는 필드 OVR 합의 최대 폭.
GK_EXCHANGE = 15
DEFAULT_SECONDS = 30.0
# 단계별 CP-SAT presolve의 probing 수준. 이 모드에만 적용한다.
PROBING_LEVEL = 0
STAGES = ("outfield_ovr", "gk_ovr", "multi_factor", "price")

Hash = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class MultiFactorObjective(Frozen):
    """호출자가 정책 원문과 기대 해시를 함께 고정한다. 기본 정책은 없다."""

    policy: ScoringPolicy
    expected_policy_version: Hash

    @model_validator(mode="after")
    def pinned(self):
        if self.policy.version != self.expected_policy_version:
            raise ValueError("요청한 정책 버전이 제공한 내용과 다릅니다.")
        return self

    def profile_for(self, slot):
        return next(s.profile_id for s in self.policy.slots if s.slot == slot)

    def score(self, query: PerformanceQuery, *, card, reinforcement):
        # 정책은 이 객체를 만들 때 검증하고 기대 해시와 대조했다.
        return score_with_validated_policy(
            query,
            policy=self.policy,
            profile_id=self.profile_for(query.slot),
            card=card,
            reinforcement=reinforcement,
        )


def objective_spec(objective: MultiFactorObjective):
    """스냅샷 메타데이터에 고정해 검증기가 정책 원문으로 점수를 재계산하게 한다."""
    spec = dict(
        version=1,
        objective=GK_PRIORITY,
        stages=list(STAGES),
        outfield="sum_raw_ovr_of_non_gk_slots",
        gk_slot=GK_SLOT,
        gk_stage="max_gk_then_outfield_within_outfield_exchange",
        gk_exchange=GK_EXCHANGE,
        outfield_tolerance=OUTFIELD_TOLERANCE,
        outfield_tolerance_basis="outfield_of_gk_stage",
        gk_tolerance=GK_TOLERANCE,
        multi_factor="sum_of_quantized_score_units_all_11_slots",
        price="maximum_total_price_at_proven_multi_factor_optimum",
        eligibility="existing_ovr_contract_then_multi_factor_exclusion_no_fallback",
        policy=objective.policy.model_dump(mode="json"),
        policy_version=objective.policy.version,
    )
    return {**spec, "objective_version": content_hash(spec)}


def objective_from_spec(spec):
    objective = MultiFactorObjective(
        policy=ScoringPolicy.model_validate(spec["policy"]),
        expected_policy_version=spec["policy_version"],
    )
    if objective_spec(objective) != spec:
        raise ValueError("objective_mismatch")
    return objective
