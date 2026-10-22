"""다요소 실험 정책의 불변 입력 계약. 운영 기본값이나 정책 자동 선택은 없다."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from FRS.schema.abilities import STAT_NAMES
from FRS.schema.models import Position
from FRS.schema.scope import FORMATION_SLOTS

Weight = Annotated[int, Field(strict=True, ge=0, le=100)]
Positive = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
Identifier = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Component = Literal[
    "pace",
    "shooting",
    "passing",
    "control",
    "defending",
    "physical",
    "gk",
    "weak_foot",
    "body",
]
StatComponent = Literal[
    "pace", "shooting", "passing", "control", "defending", "physical", "gk"
]
_STAT_GROUPS = frozenset(
    ("pace", "shooting", "passing", "control", "defending", "physical", "gk")
)
_COMPONENTS = _STAT_GROUPS | {"weak_foot", "body"}
_SLOTS = frozenset(slot for slots in FORMATION_SLOTS.values() for slot in slots)


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class StatRule(Frozen):
    name: Annotated[str, Field(strict=True)]
    weight: Weight
    upper: Positive


class StatGroup(Frozen):
    name: StatComponent
    stats: Annotated[tuple[StatRule, ...], Field(min_length=1, max_length=34)]

    @model_validator(mode="after")
    def complete(self):
        names = [s.name for s in self.stats]
        if len(set(names)) != len(names) or not set(names) <= set(STAT_NAMES):
            raise ValueError("능력치 키가 미등록이거나 중복입니다.")
        if sum(s.weight for s in self.stats) != 100:
            raise ValueError("그룹 내부 비율 합은 100이어야 합니다.")
        return self


class ComponentWeight(Frozen):
    name: Component
    weight: Weight


class CurvePoint(Frozen):
    value: Positive
    score: Weight


class PreferenceCurve(Frozen):
    points: Annotated[tuple[CurvePoint, ...], Field(min_length=2, max_length=64)]

    @model_validator(mode="after")
    def ordered(self):
        if any(a.value >= b.value for a, b in zip(self.points, self.points[1:])):
            raise ValueError("신체 기준점은 중복 없이 오름차순이어야 합니다.")
        return self


class ShapePreference(Frozen):
    name: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    score: Weight


class BodyRule(Frozen):
    height_weight: Weight
    weight_weight: Weight
    shape_weight: Weight
    height_curve: PreferenceCurve | None = None
    weight_curve: PreferenceCurve | None = None
    shapes: Annotated[tuple[ShapePreference, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def explicit_functions(self):
        if self.height_weight + self.weight_weight + self.shape_weight != 100:
            raise ValueError("신체 항목 비율 합은 100이어야 합니다.")
        if bool(self.height_weight) != (self.height_curve is not None):
            raise ValueError("키의 양수 가중치와 명시 선호 함수가 함께 필요합니다.")
        if bool(self.weight_weight) != (self.weight_curve is not None):
            raise ValueError("몸무게의 양수 가중치와 명시 선호 함수가 함께 필요합니다.")
        names = [s.name for s in self.shapes]
        if bool(self.shape_weight) != bool(names) or len(names) != len(set(names)):
            raise ValueError("체형의 양수 가중치와 중복 없는 선호표가 함께 필요합니다.")
        return self


class RoleProfile(Frozen):
    name: Identifier
    weights: Annotated[tuple[ComponentWeight, ...], Field(min_length=9, max_length=9)]
    body: BodyRule | None = None

    @model_validator(mode="after")
    def complete(self):
        if {w.name for w in self.weights} != _COMPONENTS:
            raise ValueError("9개 항목을 중복 없이 명시해야 합니다.")
        if sum(w.weight for w in self.weights) != 100:
            raise ValueError("프로필 가중치 합은 100이어야 합니다.")
        return self


class SlotProfile(Frozen):
    slot: Position
    profile_id: Identifier


class ScoringPolicy(Frozen):
    """호출자가 제공하는 실험 정책. 숫자 가중치와 체형 선호 기본값은 없다."""

    policy_id: Identifier
    status: Literal["experimental"]
    mode: Literal["core-stat-foot", "body-aware"]
    groups: Annotated[tuple[StatGroup, ...], Field(min_length=7, max_length=7)]
    profiles: Annotated[tuple[RoleProfile, ...], Field(min_length=1, max_length=20)]
    slots: Annotated[tuple[SlotProfile, ...], Field(min_length=20, max_length=20)]
    weak_foot_scores: Annotated[tuple[Weight, ...], Field(min_length=5, max_length=5)]

    @model_validator(mode="after")
    def complete(self):
        if {g.name for g in self.groups} != _STAT_GROUPS:
            raise ValueError("7개 능력치 그룹을 중복 없이 명시해야 합니다.")
        names = [s.name for group in self.groups for s in group.stats]
        if len(names) != len(STAT_NAMES) or set(names) != set(STAT_NAMES):
            raise ValueError("34개 능력치를 한 번씩 배정해야 합니다.")
        roles = {r.name for r in self.profiles}
        if len(roles) != len(self.profiles):
            raise ValueError("프로필 이름이 중복입니다.")
        if {s.slot for s in self.slots} != _SLOTS:
            raise ValueError("기존 지원 슬롯 전체를 중복 없이 대응해야 합니다.")
        if {s.profile_id for s in self.slots} != roles:
            raise ValueError("모든 슬롯과 프로필의 대응을 명시해야 합니다.")
        if any(a > b for a, b in zip(self.weak_foot_scores, self.weak_foot_scores[1:])):
            raise ValueError("약발 등급이 높아질 때 점수가 하락할 수 없습니다.")
        for role in self.profiles:
            body_weight = next(w.weight for w in role.weights if w.name == "body")
            needs_body = self.mode == "body-aware" and body_weight > 0
            if needs_body != (role.body is not None):
                raise ValueError("활성 신체 항목에만 명시 선호 함수가 필요합니다.")
            if self.mode == "core-stat-foot" and body_weight == 100:
                raise ValueError("신체 항목을 제외하면 활성 가중치가 없습니다.")
        return self

    def spec(self):
        """정책 내용과 계산 의미를 함께 해시하며 의미 없는 행 순서는 정규화한다."""
        spec = self.model_dump(mode="json")
        spec["groups"].sort(key=lambda g: g["name"])
        for group in spec["groups"]:
            group["stats"].sort(key=lambda s: s["name"])
        spec["profiles"].sort(key=lambda p: p["name"])
        for role in spec["profiles"]:
            role["weights"].sort(key=lambda w: w["name"])
            if role["body"] is not None:
                role["body"]["shapes"].sort(key=lambda s: s["name"])
        spec["slots"].sort(key=lambda s: s["slot"])
        return {
            "policy": spec,
            "algorithm_version": 1,
            "normalization": "100 * stat / upper; reject outside [0, upper]",
            "reinforcement": "base5 stats + linked target delta once",
            "eligibility": "current OVR contract; placement not evaluated",
            "body": "explicit piecewise linear curves and exact category map",
            "aggregation": "weighted mean; core excludes body for all cards",
            "rounding": "exact rational; final half up to 0.01 point",
            "missing": "exclude pair; no fallback or per-card reweighting",
        }

    @property
    def version(self):
        return content_hash(self.spec())


def content_hash(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
