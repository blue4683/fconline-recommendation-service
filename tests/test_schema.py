from datetime import datetime, timedelta, timezone

import pytest
from bson import BSON, ObjectId
from bson.codec_options import CodecOptions
from bson.decimal128 import Decimal128
from pydantic import ValidationError

from FRS.schema.models import (
    DOCUMENT_MODELS,
    CardPrice,
    CardSalary,
    CrawlLog,
    PlayerCard,
    ReinforceStats,
    TeamColor,
)
from FRS.schema.scope import (
    FORMATION_SLOTS,
    formation_slots,
    supports_reinforcement,
    supports_team_color,
)
from FRS.utils.db.client import Client, DatabaseConfig

OBSERVED = datetime(2026, 1, 1, tzinfo=timezone.utc)
VALID = OBSERVED + timedelta(hours=1)


def common(**changes):
    return {
        "batch_id": "synthetic-batch",
        "source": "synthetic",
        "observed_at": OBSERVED,
        "valid_until": VALID,
        "delete_after": None,
        "status": "ok",
        "spid": 123000001,
        "version": "synthetic-v1",
        **changes,
    }


def card(**changes):
    payload = common(
        season_id=123,
        person_id=1,
        name="가상선수_0001",
        base_reinforce_level=5,
        positions=["ST", "CF"],
        stats={"speed": 100},
        position_ovr={"ST": 100},
    )
    return PlayerCard(**{**payload, **changes})


def price(**changes):
    payload = common(reinforce_level=5, price_type="reference", price=10**20 + 7)
    return CardPrice(**{**payload, **changes})


def test_indefinite_retention_still_expires_for_recommendation():
    record = card()
    assert record.delete_after is None
    assert record.is_usable_at(OBSERVED)
    assert record.is_usable_at(VALID - timedelta(microseconds=1))
    assert not record.is_usable_at(VALID)
    assert not record.is_usable_at(OBSERVED - timedelta(seconds=1))


def test_missing_delete_after_is_not_implicitly_indefinite():
    payload = common(reinforce_level=5, salary=10)
    del payload["delete_after"]
    with pytest.raises(ValidationError, match="delete_after"):
        CardSalary(**payload)


@pytest.mark.parametrize("field", ["observed_at", "valid_until", "delete_after"])
def test_naive_timestamps_are_rejected(field):
    with pytest.raises(ValidationError):
        CardSalary(
            **common(reinforce_level=5, salary=10, **{field: datetime(2026, 1, 2)})
        )


def test_naive_query_timestamp_is_rejected():
    with pytest.raises(ValueError, match="시간대"):
        card().is_usable_at(datetime(2026, 1, 1))


@pytest.mark.parametrize(
    "changes",
    [
        {"valid_until": OBSERVED},
        {"valid_until": None},
        {"delete_after": OBSERVED},
        {"delete_after": OBSERVED + timedelta(minutes=1)},
    ],
)
def test_inconsistent_lifetimes_are_rejected(changes):
    with pytest.raises(ValidationError):
        CardSalary(**common(reinforce_level=5, salary=10, **changes))


def test_unknown_freshness_and_salary_remain_unusable():
    record = CardSalary(
        **common(reinforce_level=5, salary=None, status="unknown", valid_until=None)
    )
    assert record.salary is None
    assert not record.is_usable_at(OBSERVED)


@pytest.mark.parametrize("status", ["unknown", "untradeable", "stale"])
def test_non_ok_values_are_never_usable(status):
    record = price(status=status)
    assert not record.is_usable_at(OBSERVED)


def test_salary_requires_positive_known_value_when_ok():
    for value in (None, 0, -1, True, 1.5, "10"):
        with pytest.raises(ValidationError):
            CardSalary(**common(reinforce_level=5, salary=value))


@pytest.mark.parametrize("value", [None, 0, -1, True, 1.5, "100", 10**34])
def test_bad_or_unknown_price_cannot_be_ok(value):
    with pytest.raises(ValidationError):
        price(price=value)


def test_unknown_price_meaning_is_not_usable_even_with_a_number():
    with pytest.raises(ValidationError):
        price(price_type="unknown")
    record = price(price_type="unknown", status="unknown", valid_until=None)
    assert record.price is not None
    assert not record.is_usable_at(OBSERVED)


def test_large_integer_bp_survives_bson_round_trip():
    record = price(price=10**33 + 7)
    document = record.to_document()
    assert isinstance(document["price"], Decimal128)
    decoded = BSON.encode(document).decode(codec_options=CodecOptions(tz_aware=True))
    decoded["_id"] = ObjectId()
    restored = CardPrice.from_document(decoded)
    assert restored == record
    assert restored.price == 10**33 + 7
    assert "_id" in decoded


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1.5"])
def test_non_integer_bson_decimal_is_rejected(value):
    with pytest.raises(ValidationError):
        price(price=Decimal128(value))


def test_timezone_and_precision_are_stable_in_bson():
    local = datetime(2026, 1, 1, 9, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9)))
    record = card(observed_at=local)
    assert record.observed_at == OBSERVED.replace(microsecond=123000)
    decoded = BSON.encode(record.to_document()).decode(
        codec_options=CodecOptions(tz_aware=True)
    )
    assert PlayerCard.from_document(decoded) == record


def test_real_driver_uses_aware_datetimes_without_connecting():
    config = DatabaseConfig(
        "127.0.0.1", 27017, "synthetic", "synthetic", "synthetic_db"
    )
    with Client(config=config) as connection:
        assert connection.db.codec_options.tz_aware


@pytest.mark.parametrize(
    "changes", [{"season_id": 124}, {"person_id": 2}, {"spid": True}]
)
def test_card_identity_must_match_spid(changes):
    with pytest.raises(ValidationError):
        card(**changes)


@pytest.mark.parametrize("positions", [["1"], ["-1"], ["8"], ["SUB"], ["ST", "ST"], []])
def test_invalid_or_noise_positions_are_rejected(positions):
    with pytest.raises(ValidationError):
        card(positions=positions)


def test_missing_attributes_are_not_filled_with_zero():
    record = card()
    assert record.height is None
    assert record.used is None
    assert record.skills is None
    assert record.stats.get("unknown_stat") is None
    with pytest.raises(ValidationError):
        card(stats={})


def test_nation_and_club_membership_stay_separate():
    club = TeamColor(**common(type="club", name="가상클럽_0001"))
    nation = TeamColor(**common(type="nation", name="가상국가_0001"))
    assert club.type != nation.type
    assert supports_team_color(club.type)
    assert not supports_team_color(nation.type)


def test_reinforcement_difference_requires_explicit_baseline():
    payload = common(
        reinforce_level=7,
        baseline_reinforce_level=5,
        baseline_card_version="synthetic-base",
        stats_delta={"speed": -1},
    )
    record = ReinforceStats(**payload)
    assert record.stats_delta["speed"] == -1
    del payload["baseline_card_version"]
    with pytest.raises(ValidationError):
        ReinforceStats(**payload)


@pytest.mark.parametrize(
    "level,expected", [(4, False), (5, True), (11, True), (12, False), (True, False)]
)
def test_mvp_reinforcement_boundaries(level, expected):
    assert supports_reinforcement(level) is expected


def test_mvp_formations_preserve_lateral_slots():
    assert set(FORMATION_SLOTS) == {"4-2-3-1", "4-1-2-3", "4-2-2-2"}
    assert all(
        len(slots) == len(set(slots)) == 11 for slots in FORMATION_SLOTS.values()
    )
    assert {"RM", "LM", "RS", "LS"} <= set(formation_slots("4-2-2-2"))
    assert {"CDM", "RCM", "LCM"} <= set(formation_slots("4-1-2-3"))
    with pytest.raises(ValueError):
        formation_slots("4-4-2")


def test_undeclared_attributes_do_not_silently_enter_schema():
    with pytest.raises(ValidationError):
        card(unrecognized_feature=10)


def test_blocked_log_cannot_look_like_success_or_retryable_failure():
    payload = {
        "batch_id": "synthetic-batch",
        "source": "synthetic",
        "observed_at": OBSERVED,
        "delete_after": None,
        "spid": 123000001,
        "resource": "ability",
        "attempt": 1,
        "duration_ms": 1,
        "http_status": 429,
        "failure_kind": "blocked",
        "outcome": "stopped",
    }
    assert CrawlLog(**payload).outcome == "stopped"
    for outcome in ("ok", "failed"):
        with pytest.raises(ValidationError):
            CrawlLog(**{**payload, "outcome": outcome})


def test_all_document_contracts_export_json_schema():
    assert len(DOCUMENT_MODELS) == 6
    for name, model in DOCUMENT_MODELS.items():
        schema = model.model_json_schema()
        assert schema["type"] == "object", name
        assert schema["additionalProperties"] is False
