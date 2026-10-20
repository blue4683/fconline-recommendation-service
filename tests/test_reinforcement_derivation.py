"""가상 1강 값으로 규칙·출처·불변성·실패 경계를 검증한다."""

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from test_schema import card

from FRS.core.position_recommend.data_contract import STAT_FEATURES, feature_spec
from FRS.core.reinforcement import (
    DerivedAbilities,
    derive_abilities,
    reinforcement_bonus,
    reinforcement_delta,
    reinforcement_spec,
)
from FRS.schema.abilities import STAT_NAMES
from FRS.schema.scope import supports_reinforcement

EXPECTED = (0, 1, 2, 4, 6, 8, 11, 15, 17, 19, 21, 24, 27)


def base(**changes):
    return card(
        **{
            "base_reinforce_level": 1,
            "stats": {key: 40 + i for i, key in enumerate(STAT_NAMES)},
            "position_ovr": {"ST": 90, "CF": 80},
            "height": 180,
            "dribble_skill": 3,
            "left_foot": 4,
            **changes,
        }
    )


@pytest.mark.parametrize("level,bonus", tuple(enumerate(EXPECTED, 1)))
def test_all_levels_derive_every_stat_and_absolute_existing_ovr(level, bonus):
    original = base()
    before = copy.deepcopy(original.model_dump())
    result = derive_abilities(original, level).to_dict()
    assert result["target_level"] == level and result["bonus"] == bonus
    assert result["stats"] == {k: v + bonus for k, v in original.stats.items()}
    assert result["position_ovr"] == {"ST": 90 + bonus, "CF": 80 + bonus}
    assert original.model_dump() == before
    for key in ("height", "dribble_skill", "left_foot", "salary", "price", "positions"):
        assert key not in result
    assert result["source_reference"] == {
        k: original.model_dump(mode="json")[k]
        for k in (
            "batch_id",
            "version",
            "source",
            "base_reinforce_level",
            "status",
            "observed_at",
            "valid_until",
            "delete_after",
        )
    }


def test_level_five_baseline_differences_do_not_double_add_cumulative_bonus():
    assert [reinforcement_delta(5, n) for n in range(6, 12)] == [2, 5, 9, 11, 13, 15]
    original = base()
    fifth = derive_abilities(original, 5).to_dict()
    for level in range(1, 14):
        actual = derive_abilities(original, level).to_dict()
        delta = reinforcement_delta(5, level)
        assert fifth["stats"]["speed"] + delta == actual["stats"]["speed"]
        assert reinforcement_delta(level, 5) == -delta


@pytest.mark.parametrize("bad", [0, 14, -1, True, False, 5.0, "5", None])
def test_invalid_levels_are_not_coerced_or_clipped(bad):
    with pytest.raises(ValueError):
        derive_abilities(base(), bad)
    with pytest.raises(ValueError):
        reinforcement_delta(bad, 5)


def test_missing_and_extra_stat_keys_are_errors():
    for stats in ({}, {"speed": 40}, {**base().stats, "height": 180}):
        with pytest.raises((ValueError, ValidationError)):
            derive_abilities(base(status="unknown", stats=stats), 5)


def test_level_one_is_required_and_forged_objects_are_revalidated():
    with pytest.raises(ValueError, match="1강"):
        derive_abilities(base(base_reinforce_level=5), 6)
    with pytest.raises(TypeError):
        derive_abilities(base().model_dump(), 5)
    with pytest.raises(ValidationError):
        derive_abilities(base().model_copy(update={"stats": {"speed": True}}), 5)


@pytest.mark.parametrize("status", ["unknown", "stale", "untradeable"])
def test_unknown_and_non_ok_source_state_and_no_deadline_are_preserved(status):
    original = base(status=status, valid_until=None)
    data = derive_abilities(original, 13).to_dict()
    assert data["source_reference"]["status"] == status
    assert data["source_reference"]["valid_until"] is None
    assert data["source_reference"]["delete_after"] is None


def test_missing_ovr_is_not_filled_and_over_200_is_not_clipped():
    assert derive_abilities(base(position_ovr={}), 13).to_dict()["position_ovr"] == {}
    assert derive_abilities(base(position_ovr={"ST": 200}), 13).to_dict()[
        "position_ovr"
    ] == {"ST": 227}


@pytest.mark.parametrize("field", ["stats", "position_ovr"])
def test_integer_overflow_is_rejected(field):
    values = dict(base().stats) if field == "stats" else {"ST": 90}
    values["speed" if field == "stats" else "ST"] = 2**63 - 1
    with pytest.raises(ValueError, match="정수 범위"):
        derive_abilities(base(**{field: values}), 2)


def test_canonical_hashes_bind_source_rule_and_target_and_are_order_independent():
    original = base()
    result = derive_abilities(original, 5)
    assert (
        result.to_dict()["source_payload_sha256"]
        == hashlib.sha256(
            json.dumps(
                original.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
    )
    assert result.to_dict()["rule_version"] == reinforcement_spec()["rule_version"]
    assert (
        derive_abilities(base(stats=dict(reversed(list(original.stats.items())))), 5)
        == result
    )
    assert (
        derive_abilities(base(version="different-source"), 5).version != result.version
    )
    assert derive_abilities(original, 6).version != result.version


def test_result_and_rule_copies_do_not_mutate_calculations():
    result = derive_abilities(base(), 5)
    before = result.to_dict()
    changed = result.to_dict()
    changed["stats"]["speed"] = 999
    changed["source_reference"]["valid_until"] = None
    assert result.to_dict() == before
    with pytest.raises(FrozenInstanceError):
        result._payload = b"changed"
    with pytest.raises(TypeError):
        DerivedAbilities(bytearray())
    spec = reinforcement_spec()
    spec["bonuses"][0] = 999
    assert reinforcement_bonus(1) == 0


def test_existing_feature_contract_and_mvp_recommendation_scope_are_unchanged():
    assert STAT_FEATURES == STAT_NAMES
    assert feature_spec()["sha256"] == (
        "908426caef42b031b7927e10f6a7e1788cfa9aa4edfa9f36c3292e8c55240a31"
    )
    assert [n for n in range(1, 14) if supports_reinforcement(n)] == list(range(5, 12))


def test_derivation_requires_no_file_or_network_access():
    original = base()
    with (
        patch("builtins.open", side_effect=AssertionError("파일 접근 금지")),
        patch("socket.socket", side_effect=AssertionError("네트워크 접근 금지")),
    ):
        result = derive_abilities(original, 13)
        assert result.to_dict()["position_ovr"]["ST"] == 117
