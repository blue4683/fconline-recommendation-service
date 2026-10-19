"""가상 OVR로 점수·강화 출처·부분 제외를 검증한다. 실제 점수 실행은 없다."""

import copy
import hashlib
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_model_input_contract import complete
from test_schema import OBSERVED

from FRS.core.squad_recommend.performance import (
    PerformanceQuery,
    performance_spec,
    score_performance,
)
from FRS.schema.models import ReinforceStats


def query(**changes):
    return PerformanceQuery(
        **{
            "spid": 123000001,
            "slot": "RCB",
            "reinforce_level": 5,
            "when": OBSERVED,
            **changes,
        }
    )


def card(**changes):
    return complete(
        **{
            "position_ovr": {"CB": 120, "CDM": 121, "CM": 122, "CAM": 123, "ST": 124},
            **changes,
        }
    )


def reinforcement(base, **changes):
    metadata = base.model_dump(
        include={
            "spid",
            "batch_id",
            "source",
            "observed_at",
            "valid_until",
            "delete_after",
            "status",
        }
    )
    return ReinforceStats(
        **{
            **metadata,
            "version": "synthetic-grade7",
            "reinforce_level": 7,
            "baseline_reinforce_level": 5,
            "baseline_card_version": base.version,
            "stats_delta": {"speed": 8},
            "position_ovr": {"CB": 128},
            **changes,
        }
    )


@pytest.mark.parametrize("ovr,expected", [(1, 0.5), (120, 60), (137, 68.5), (200, 100)])
def test_fixed_normalization_preserves_exact_raw_ovr(ovr, expected):
    result = score_performance(query(), card=card(position_ovr={"CB": ovr}))
    assert result.performance == expected and result.raw_ovr == ovr
    assert result.exclusion is None and result.ovr_position == "CB"


@pytest.mark.parametrize(
    "slot,key,value",
    [
        ("RCB", "CB", 120),
        ("LCB", "CB", 120),
        ("RDM", "CDM", 121),
        ("LDM", "CDM", 121),
        ("RCM", "CM", 122),
        ("LCM", "CM", 122),
        ("RAM", "CAM", 123),
        ("LAM", "CAM", 123),
        ("RS", "ST", 124),
        ("LS", "ST", 124),
        ("ST", "ST", 124),
    ],
)
def test_scoring_aliases_do_not_decide_registration_or_placement(slot, key, value):
    base = card(positions=["GK"])
    result = score_performance(query(slot=slot), card=base)
    assert result.ovr_position == key and result.performance == value / 2
    assert "placement" not in result.model_dump() and base.positions == ("GK",)


def test_reinforcement_uses_absolute_ovr_and_base_grade_ignores_unused_row():
    base = card()
    enhanced = reinforcement(base)
    result = score_performance(
        query(reinforce_level=7), card=base, reinforcement=enhanced
    )
    assert result.raw_ovr == 128 and result.performance == 64
    assert [(r.resource, r.version) for r in result.observations] == [
        ("player_card", base.version),
        ("reinforce_stats", enhanced.version),
    ]
    assert result.observations[1].valid_until == enhanced.valid_until
    result = score_performance(query(), card=base, reinforcement=enhanced)
    assert result.raw_ovr == 120 and len(result.observations) == 1


def test_missing_and_out_of_range_only_exclude_the_affected_pair():
    values = [card(position_ovr={}), card(position_ovr={"CB": 201}), card()]
    results = [score_performance(query(), card=value) for value in values]
    assert [r.exclusion for r in results] == ["missing_ovr", "ovr_out_of_range", None]
    assert results[0].performance is None and results[1].raw_ovr == 201
    assert results[1].performance is None and results[2].performance == 60


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"spid": 123000002, "person_id": 2}, "card_mismatch"),
        ({"base_reinforce_level": 6}, "baseline_mismatch"),
        ({"status": "unknown", "valid_until": None}, "unusable_observation"),
        ({"status": "stale"}, "unusable_observation"),
    ],
)
def test_invalid_card_identity_baseline_or_status_is_not_scored(changes, reason):
    result = score_performance(query(), card=card(**changes))
    assert result.exclusion == reason and result.performance is None


@pytest.mark.parametrize(
    "changes",
    [
        {"reinforce_level": 6},
        {"baseline_card_version": "different"},
        {"baseline_reinforce_level": 4},
        {"spid": 123000002},
    ],
)
def test_reinforcement_must_match_the_exact_target_and_baseline(changes):
    base = card()
    result = score_performance(
        query(reinforce_level=7),
        card=base,
        reinforcement=reinforcement(base, **changes),
    )
    assert result.exclusion == "reinforcement_mismatch" and result.performance is None


def test_missing_or_expired_reinforcement_never_uses_base_or_stats_delta():
    base = card()
    result = score_performance(query(reinforce_level=7), card=base)
    assert result.exclusion == "missing_reinforcement"
    enhanced = reinforcement(base, position_ovr={})
    assert (
        score_performance(
            query(reinforce_level=7), card=base, reinforcement=enhanced
        ).exclusion
        == "missing_ovr"
    )
    enhanced = reinforcement(base, status="unknown", valid_until=None)
    assert (
        score_performance(
            query(reinforce_level=7), card=base, reinforcement=enhanced
        ).exclusion
        == "unusable_observation"
    )


@pytest.mark.parametrize("offset", [-timedelta(microseconds=1), timedelta(hours=1)])
def test_not_yet_observed_and_expiry_boundary_are_excluded(offset):
    base = card(valid_until=OBSERVED + timedelta(hours=1))
    assert (
        score_performance(query(when=OBSERVED + offset), card=base).exclusion
        == "unusable_observation"
    )


def test_spec_is_versioned_and_caller_mutation_does_not_change_rules():
    spec = performance_spec()
    version = spec.pop("scorer_version")
    assert (
        hashlib.sha256(
            json.dumps(
                spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        == version
    )
    spec["aliases"]["RCB"] = "GK"
    spec["ovr_max"] = 999
    assert performance_spec()["ovr_max"] == 200
    result = score_performance(query(), card=card())
    assert result.scorer_version == version and result.performance == 60


def test_inputs_are_not_mutated_and_scoring_performs_no_file_io(monkeypatch):
    base = card()
    enhanced = reinforcement(base)
    before = copy.deepcopy((base.model_dump(), enhanced.model_dump()))
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: pytest.fail("점수 함수의 파일 I/O 금지"),
    )
    assert (
        score_performance(
            query(reinforce_level=7), card=base, reinforcement=enhanced
        ).performance
        == 64
    )
    assert (base.model_dump(), enhanced.model_dump()) == before


def test_mutated_or_missing_observations_are_rejected_without_score():
    base = card()
    base.stats["speed"] = -1
    assert score_performance(query(), card=base).exclusion == "invalid_observation"
    assert score_performance(query(), card=None).exclusion == "missing_card"
    assert score_performance(query(), card={}).exclusion == "invalid_observation"


@pytest.mark.parametrize(
    "changes",
    [
        {"spid": "123000001"},
        {"reinforce_level": True},
        {"reinforce_level": 4},
        {"reinforce_level": 12},
        {"slot": "RF"},
        {"when": OBSERVED.replace(tzinfo=None)},
    ],
)
def test_invalid_requests_are_not_silently_coerced(changes):
    with pytest.raises(ValidationError):
        query(**changes)


def test_request_requires_the_explicit_query_type():
    with pytest.raises(TypeError):
        score_performance({}, card=card())


def test_selected_ovr_key_has_no_direct_slot_or_registered_position_fallback():
    base = card(position_ovr={"RCB": 150, "ST": 140})
    result = score_performance(query(), card=base)
    assert result.exclusion == "missing_ovr" and result.performance is None


def test_reinforcement_expiry_is_checked_while_the_base_is_still_usable():
    base = card(valid_until=OBSERVED + timedelta(hours=1))
    enhanced = reinforcement(base, valid_until=OBSERVED + timedelta(minutes=30))
    result = score_performance(
        query(reinforce_level=7, when=OBSERVED + timedelta(minutes=30)),
        card=base,
        reinforcement=enhanced,
    )
    assert result.exclusion == "unusable_observation"


def test_independently_selected_resource_batches_preserve_each_source():
    base = card()
    enhanced = reinforcement(base, batch_id="synthetic-reinforcement-batch")
    result = score_performance(
        query(reinforce_level=7), card=base, reinforcement=enhanced
    )
    assert result.performance == 64
    assert [r.batch_id for r in result.observations] == [
        base.batch_id,
        "synthetic-reinforcement-batch",
    ]
