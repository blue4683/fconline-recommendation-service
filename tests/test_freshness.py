"""D8의 UTC·정확한 만료 경계·기존 상태와 기한 보존을 검증한다."""

from datetime import datetime, timedelta, timezone

import pytest

from FRS.core.freshness import (
    FreshnessRule,
    Resource,
    assess,
    specification,
    window,
)
from FRS.schema.models import CardPrice, CardSalary, TeamColor

OBSERVED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def price(**changes):
    return CardPrice.model_validate(
        {
            "batch_id": "synthetic-prices",
            "source": "synthetic",
            "observed_at": OBSERVED,
            "delete_after": None,
            "spid": 123000001,
            "version": "synthetic-v1",
            "status": "unknown",
            "valid_until": None,
            "reinforce_level": 5,
            "price_type": "unknown",
            "price": 1234,
            **changes,
        }
    )


@pytest.mark.parametrize(
    "resource,valid_hours,refresh_hours",
    [
        (Resource.PRICE, 24, 12),
        (Resource.SALARY, 168, 72),
        (Resource.ABILITY, 168, 72),
        (Resource.TEAM_COLOR, 168, 72),
    ],
)
def test_selected_durations_and_refresh_before_expiration(
    resource, valid_hours, refresh_hours
):
    timing = window(resource, OBSERVED, at=OBSERVED)
    assert timing.refresh_due_at == OBSERVED + timedelta(hours=refresh_hours)
    assert timing.policy_expires_at == OBSERVED + timedelta(hours=valid_hours)
    assert not timing.refresh_due and not timing.expired_by_policy


def test_exact_refresh_and_expiry_boundaries():
    assert not window(
        Resource.PRICE, OBSERVED, at=OBSERVED + timedelta(hours=12, microseconds=-1)
    ).refresh_due
    assert window(
        Resource.PRICE, OBSERVED, at=OBSERVED + timedelta(hours=12)
    ).refresh_due
    assert not window(
        Resource.PRICE, OBSERVED, at=OBSERVED + timedelta(hours=24, microseconds=-1)
    ).expired_by_policy
    assert window(
        Resource.PRICE, OBSERVED, at=OBSERVED + timedelta(hours=24)
    ).expired_by_policy


def test_original_observation_and_timezone_are_used():
    local = OBSERVED.astimezone(timezone(timedelta(hours=9)))
    timing = window(Resource.PRICE, local, at=local + timedelta(hours=13))
    assert timing.observed_at == OBSERVED
    assert timing.refresh_due_at == OBSERVED + timedelta(hours=12)
    assert timing.refresh_due and timing.refresh_due_at.tzinfo == timezone.utc


def test_unknown_is_not_promoted_or_modified():
    record = price()
    before = record.model_dump()
    result = assess(record, at=OBSERVED + timedelta(hours=1))
    assert not result.usable and result.effective_valid_until is None
    assert result.source_status == "unknown"
    assert record.model_dump() == before


def test_policy_caps_long_validity_and_preserves_short_validity():
    long = price(
        status="ok", price_type="reference", valid_until=OBSERVED + timedelta(days=2)
    )
    assert long.is_usable_at(OBSERVED + timedelta(hours=24))
    assert assess(long, at=OBSERVED + timedelta(hours=24, microseconds=-1)).usable
    result = assess(long, at=OBSERVED + timedelta(hours=24))
    assert not result.usable and result.effective_valid_until == OBSERVED + timedelta(
        hours=24
    )
    assert long.valid_until == OBSERVED + timedelta(days=2)
    short = price(
        status="ok", price_type="reference", valid_until=OBSERVED + timedelta(hours=2)
    )
    result = assess(short, at=OBSERVED + timedelta(hours=3))
    assert not result.usable and result.effective_valid_until == short.valid_until


def test_salary_and_unknown_teamcolor_keep_their_source_status():
    fields = price().model_dump(exclude={"price", "price_type", "reinforce_level"})
    salary = CardSalary(**fields, reinforce_level=5, salary=20)
    color = TeamColor(**fields, type="club", name="가상클럽")
    for record in (salary, color):
        assert not assess(record, at=OBSERVED).usable
        assert record.status == "unknown" and record.valid_until is None


@pytest.mark.parametrize("bad", [OBSERVED.replace(tzinfo=None), "2026-01-01"])
def test_naive_or_invalid_times_are_rejected(bad):
    with pytest.raises(ValueError):
        window(Resource.PRICE, bad, at=OBSERVED)
    with pytest.raises(ValueError):
        window(Resource.PRICE, OBSERVED, at=bad)


def test_future_observation_cannot_be_usable():
    record = price(
        status="ok", price_type="reference", valid_until=OBSERVED + timedelta(hours=1)
    )
    result = assess(record, at=OBSERVED - timedelta(seconds=1))
    assert result.window.future_observation and not result.usable


@pytest.mark.parametrize("valid,refresh", [(0, 1), (1, 0), (1, 2), (-1, -2)])
def test_invalid_intervals_are_rejected(valid, refresh):
    with pytest.raises(ValueError):
        FreshnessRule(timedelta(hours=valid), timedelta(hours=refresh))


def test_policy_manifest_is_deterministic_and_not_a_scheduler():
    left, right = specification(), specification()
    assert left == right and len(left["policy_hash"]) == 64
    assert left["mode"] == "manual" and left["auto_execution"] is False
    left["rules"]["price"]["valid_for_seconds"] = 1
    assert specification() == right
    with pytest.raises(TypeError):
        assess(object(), at=OBSERVED)
