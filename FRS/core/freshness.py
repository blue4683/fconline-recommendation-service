"""D8의 시각 계산. 원본 상태/기한을 변경하거나 품질·자격을 승격하지 않는다."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType

from FRS.schema.models import (
    CardPrice,
    CardSalary,
    PlayerCard,
    ReinforceStats,
    TeamColor,
)


class Resource(StrEnum):
    PRICE = "price"
    SALARY = "salary"
    ABILITY = "ability"
    TEAM_COLOR = "team_color"


@dataclass(frozen=True)
class FreshnessRule:
    valid_for: timedelta
    refresh_every: timedelta

    def __post_init__(self):
        if not timedelta(0) < self.refresh_every <= self.valid_for:
            raise ValueError("갱신 간격은 양수이고 유효기간 이하여야 합니다.")


RULES = MappingProxyType(
    {
        Resource.PRICE: FreshnessRule(timedelta(hours=24), timedelta(hours=12)),
        Resource.SALARY: FreshnessRule(timedelta(days=7), timedelta(days=3)),
        Resource.ABILITY: FreshnessRule(timedelta(days=7), timedelta(days=3)),
        Resource.TEAM_COLOR: FreshnessRule(timedelta(days=7), timedelta(days=3)),
    }
)


def utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("시간대가 있는 시각이 필요합니다.")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class FreshnessWindow:
    observed_at: datetime
    refresh_due_at: datetime
    policy_expires_at: datetime
    refresh_due: bool
    expired_by_policy: bool
    future_observation: bool


def window(
    resource: Resource, observed_at: datetime, *, at: datetime
) -> FreshnessWindow:
    rule = RULES[Resource(resource)]
    observed_at, at = utc(observed_at), utc(at)
    refresh_due_at = observed_at + rule.refresh_every
    expires_at = observed_at + rule.valid_for
    return FreshnessWindow(
        observed_at,
        refresh_due_at,
        expires_at,
        at >= refresh_due_at,
        at >= expires_at,
        at < observed_at,
    )


def resource_for(record):
    if isinstance(record, CardPrice):
        return Resource.PRICE
    if isinstance(record, CardSalary):
        return Resource.SALARY
    if isinstance(record, (PlayerCard, ReinforceStats)):
        return Resource.ABILITY
    if isinstance(record, TeamColor):
        return Resource.TEAM_COLOR
    raise TypeError("신선도를 평가할 수 없는 문서 종류입니다.")


@dataclass(frozen=True)
class ObservationFreshness:
    window: FreshnessWindow
    source_status: str
    effective_valid_until: datetime | None
    usable: bool


def assess(record, *, at: datetime) -> ObservationFreshness:
    """기존 기한을 연장하지 않는다. unknown/자격/가격 의미 판단은 기존 상태를 따른다."""
    temporal = window(resource_for(record), record.observed_at, at=at)
    deadline = (
        min(record.valid_until, temporal.policy_expires_at)
        if record.valid_until is not None
        else None
    )
    return ObservationFreshness(
        temporal,
        record.status,
        deadline,
        record.is_usable_at(utc(at)) and not temporal.expired_by_policy,
    )


def specification():
    rules = {
        kind.value: dict(
            valid_for_seconds=int(rule.valid_for.total_seconds()),
            refresh_every_seconds=int(rule.refresh_every.total_seconds()),
        )
        for kind, rule in RULES.items()
    }
    payload = dict(version=1, rules=rules, mode="manual", auto_execution=False)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**payload, "policy_hash": digest}
