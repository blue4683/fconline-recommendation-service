"""명시 실험 정책의 순수 다요소 점수. 명시 목적 선택 시에만 스냅샷에 연결한다."""

from fractions import Fraction
from math import gcd
from typing import Annotated, Literal

from pydantic import Field, model_validator

from FRS.core.squad_recommend.multi_factor_policy import (
    Component,
    Frozen,
    ScoringPolicy,
    Weight,
    content_hash,
)
from FRS.core.squad_recommend.performance import (
    ExclusionReason,
    ObservationReference,
    PerformanceQuery,
    score_performance,
)
from FRS.schema.models import PlayerCard, Position, ReinforceStats

Hash = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
NonNegative = Annotated[int, Field(strict=True, ge=0)]


class ExactScore(Frozen):
    numerator: NonNegative
    denominator: Annotated[int, Field(strict=True, ge=1)]

    @model_validator(mode="after")
    def normalized(self):
        if self.numerator > 100 * self.denominator:
            raise ValueError("정확 점수는 0~100 범위여야 합니다.")
        if gcd(self.numerator, self.denominator) != 1:
            raise ValueError("정확 점수는 기약분수여야 합니다.")
        return self

    @classmethod
    def of(cls, value):
        return cls(numerator=value.numerator, denominator=value.denominator)

    def fraction(self):
        return Fraction(self.numerator, self.denominator)


class RawValue(Frozen):
    name: str
    value: Annotated[int, Field(strict=True)] | Annotated[str, Field(strict=True)]


class ScoreComponent(Frozen):
    name: Component
    weight: Weight
    value: ExactScore
    contribution: ExactScore
    inputs: tuple[RawValue, ...]


class MultiFactorResult(Frozen):
    query: PerformanceQuery
    policy_id: str
    scorer_version: Hash
    profile_id: str
    input_version: Hash | None
    ovr_position: Position
    raw_ovr: int | None
    exact_score: ExactScore | None
    score_units: Annotated[int, Field(strict=True, ge=0, le=10000)] | None
    components: tuple[ScoreComponent, ...]
    observations: tuple[ObservationReference, ...]
    exclusion: (
        ExclusionReason
        | Literal[
            "missing_feature",
            "missing_stat_delta",
            "stat_out_of_range",
            "stat_overflow",
            "invalid_foot",
            "body_out_of_range",
            "unknown_body_shape",
        ]
        | None
    )
    fields: tuple[str, ...]

    @model_validator(mode="after")
    def consistent(self):
        if self.exclusion is not None:
            if (
                self.score_units is not None
                or self.exact_score is not None
                or self.components
            ):
                raise ValueError("제외 결과에는 점수를 넣을 수 없습니다.")
        else:
            if (
                not self.components
                or not self.observations
                or self.input_version is None
            ):
                raise ValueError("성공 결과에는 구성 항목과 출처가 필요합니다.")
            if len({c.name for c in self.components}) != len(self.components):
                raise ValueError("점수 구성 항목이 중복입니다.")
            total_weight = sum(c.weight for c in self.components)
            if not total_weight:
                raise ValueError("활성 가중치 합이 0입니다.")
            total = Fraction(0)
            for c in self.components:
                expected = c.value.fraction() * c.weight / total_weight
                if c.contribution.fraction() != expected:
                    raise ValueError("항목 기여가 가중 평균과 다릅니다.")
                total += expected
            if self.exact_score is None or self.exact_score.fraction() != total:
                raise ValueError("정확 합계가 구성 항목과 다릅니다.")
            if self.score_units != _units(total):
                raise ValueError("정수 점수가 정확 합계와 다릅니다.")
        return self

    @property
    def display_score(self):
        if self.score_units is None:
            return None
        return f"{self.score_units // 100}.{self.score_units % 100:02d}"


class _Excluded(Exception):
    def __init__(self, reason, field):
        self.reason = reason
        self.field = field


def _units(score):
    scaled = score * 100
    return (2 * scaled.numerator + scaled.denominator) // (2 * scaled.denominator)


def _curve_value(curve, value, field):
    if value is None:
        raise _Excluded("missing_feature", field)
    if not curve.points[0].value <= value <= curve.points[-1].value:
        raise _Excluded("body_out_of_range", field)
    for a, b in zip(curve.points, curve.points[1:]):
        if a.value <= value <= b.value:
            return Fraction(a.score) + Fraction(value - a.value, b.value - a.value) * (
                b.score - a.score
            )
    raise AssertionError("검증한 선호 함수의 구간을 찾을 수 없습니다.")


def _body_value(rule, card):
    value, inputs = Fraction(0), []
    for field, weight, curve in (
        ("height", rule.height_weight, rule.height_curve),
        ("weight", rule.weight_weight, rule.weight_curve),
    ):
        if weight:
            raw = getattr(card, field)
            value += _curve_value(curve, raw, field) * weight / 100
            inputs.append(RawValue(name=field, value=raw))
    if rule.shape_weight:
        if card.physical is None:
            raise _Excluded("missing_feature", "physical")
        shapes = {s.name: s.score for s in rule.shapes}
        if card.physical not in shapes:
            raise _Excluded("unknown_body_shape", "physical")
        value += Fraction(shapes[card.physical] * rule.shape_weight, 100)
        inputs.append(RawValue(name="physical", value=card.physical))
    return value, tuple(inputs)


def _stat_value(group, card, reinforcement):
    value, inputs = Fraction(0), []
    for stat in sorted(group.stats, key=lambda s: s.name):
        if not stat.weight:
            continue
        if stat.name not in card.stats:
            raise _Excluded("missing_feature", stat.name)
        raw = card.stats[stat.name]
        if reinforcement is not None:
            if stat.name not in reinforcement.stats_delta:
                raise _Excluded("missing_stat_delta", stat.name)
            raw += reinforcement.stats_delta[stat.name]
        if raw > 2**63 - 1:
            raise _Excluded("stat_overflow", stat.name)
        if not 0 <= raw <= stat.upper:
            raise _Excluded("stat_out_of_range", stat.name)
        value += Fraction(raw * stat.weight, stat.upper)
        inputs.append(RawValue(name=stat.name, value=raw))
    return value, tuple(inputs)


def score_multi_factor(
    query: PerformanceQuery,
    *,
    policy: ScoringPolicy,
    expected_policy_version: str,
    profile_id: str,
    card: PlayerCard | None,
    reinforcement: ReinforceStats | None = None,
):
    """정책 오류는 거부하고 관측/능력치 오류는 카드·슬롯별 제외로 반환한다."""
    if not isinstance(policy, ScoringPolicy):
        raise TypeError("ScoringPolicy로 명시 정책을 제공해야 합니다.")
    policy = ScoringPolicy.model_validate(policy.model_dump())
    if expected_policy_version != policy.version:
        raise ValueError("요청한 정책 버전이 제공한 내용과 다릅니다.")
    return score_with_validated_policy(
        query,
        policy=policy,
        profile_id=profile_id,
        card=card,
        reinforcement=reinforcement,
    )


def score_with_validated_policy(
    query: PerformanceQuery,
    *,
    policy: ScoringPolicy,
    profile_id: str,
    card: PlayerCard | None,
    reinforcement: ReinforceStats | None = None,
):
    """이미 검증·버전 고정한 정책으로 점수를 낸다. 많은 후보를 같은 정책으로 계산할 때
    정책 재검증을 반복하지 않기 위한 내부 경로이며 카드·질의 검증은 그대로 한다."""
    if not isinstance(query, PerformanceQuery):
        raise TypeError("PerformanceQuery로 점수 요청을 명시해야 합니다.")
    query = PerformanceQuery.model_validate(query.model_dump())
    if profile_id != next(s.profile_id for s in policy.slots if s.slot == query.slot):
        raise ValueError("프로필이 명시한 슬롯 대응과 다릅니다.")
    role = next(p for p in policy.profiles if p.name == profile_id)
    baseline = score_performance(query, card=card, reinforcement=reinforcement)
    input_version = None

    def result(*, exclusion=None, fields=(), components=(), exact=None):
        return MultiFactorResult(
            query=query,
            policy_id=policy.policy_id,
            scorer_version=policy.version,
            profile_id=profile_id,
            input_version=input_version,
            ovr_position=baseline.ovr_position,
            raw_ovr=baseline.raw_ovr,
            exact_score=None if exact is None else ExactScore.of(exact),
            score_units=None if exact is None else _units(exact),
            components=components,
            observations=baseline.observations,
            exclusion=exclusion,
            fields=fields,
        )

    if baseline.exclusion is not None:
        return result(exclusion=baseline.exclusion)
    card = PlayerCard.model_validate(card.model_dump())
    enhanced = None
    if query.reinforce_level != card.base_reinforce_level:
        enhanced = ReinforceStats.model_validate(reinforcement.model_dump())
    input_version = content_hash(
        dict(
            policy_version=policy.version,
            profile_id=profile_id,
            query=query.model_dump(mode="json"),
            card=card.model_dump(mode="json"),
            reinforcement=None
            if enhanced is None
            else enhanced.model_dump(mode="json"),
        )
    )
    active = tuple(
        w
        for w in sorted(role.weights, key=lambda w: w.name)
        if w.weight and not (w.name == "body" and policy.mode == "core-stat-foot")
    )
    total_weight = sum(w.weight for w in active)
    groups = {g.name: g for g in policy.groups}
    components, exact = [], Fraction(0)
    try:
        for component in active:
            if component.name == "weak_foot":
                feet = card.left_foot, card.right_foot
                for field, foot in zip(("left_foot", "right_foot"), feet, strict=True):
                    if foot is None:
                        raise _Excluded("missing_feature", field)
                    if not 1 <= foot <= 5:
                        raise _Excluded("invalid_foot", field)
                value = Fraction(policy.weak_foot_scores[min(feet) - 1])
                inputs = tuple(
                    RawValue(name=f, value=v)
                    for f, v in (
                        ("left_foot", feet[0]),
                        ("right_foot", feet[1]),
                        ("weak_foot", min(feet)),
                    )
                )
            elif component.name == "body":
                value, inputs = _body_value(role.body, card)
            else:
                value, inputs = _stat_value(groups[component.name], card, enhanced)
            contribution = value * component.weight / total_weight
            components.append(
                ScoreComponent(
                    name=component.name,
                    weight=component.weight,
                    value=ExactScore.of(value),
                    contribution=ExactScore.of(contribution),
                    inputs=inputs,
                )
            )
            exact += contribution
    except _Excluded as exc:
        return result(exclusion=exc.reason, fields=(exc.field,))
    return result(components=tuple(components), exact=exact)
