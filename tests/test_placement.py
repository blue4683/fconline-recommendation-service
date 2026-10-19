"""기본 배치 계열과 성능/모델/다른 제약의 경계를 합성 관측으로 검증한다."""

import copy
from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_performance import card
from test_schema import OBSERVED

from FRS.core.squad_recommend.performance import PerformanceQuery, score_performance
from FRS.core.squad_recommend.placement import PlacementQuery, placement, placement_spec


def query(**changes):
    return PlacementQuery(
        **{
            "spid": 123000001,
            "slot": "RCB",
            "reinforce_level": 5,
            "when": OBSERVED,
            **changes,
        }
    )


@pytest.mark.parametrize(
    "registered,slot",
    [
        ("CB", "RCB"),
        ("CB", "LCB"),
        ("RCB", "LCB"),
        ("LCB", "RCB"),
        ("CDM", "RDM"),
        ("CDM", "LDM"),
        ("RDM", "LDM"),
        ("CM", "RCM"),
        ("CM", "LCM"),
        ("LCM", "RCM"),
        ("CAM", "RAM"),
        ("CAM", "LAM"),
        ("RAM", "LAM"),
        ("ST", "RS"),
        ("ST", "LS"),
        ("RS", "ST"),
        ("LS", "RS"),
        ("GK", "GK"),
        ("RB", "RB"),
        ("LB", "LB"),
        ("RW", "RW"),
        ("LW", "LW"),
        ("RM", "RM"),
        ("LM", "LM"),
        ("CAM", "CAM"),
        ("CDM", "CDM"),
    ],
)
def test_registered_and_requested_codes_use_the_same_family(registered, slot):
    result = placement(query(slot=slot), card=card(positions=[registered]))
    assert result.allowed and result.placement_type == "base"
    assert result.matched_positions == (registered,) and result.exclusion is None
    assert result.observation.resource == "player_card"


@pytest.mark.parametrize(
    "registered,slot",
    [
        ("LB", "RB"),
        ("RB", "LB"),
        ("RWB", "RB"),
        ("RW", "RM"),
        ("RF", "RW"),
        ("CF", "ST"),
        ("LW", "LAM"),
        ("GK", "RCB"),
    ],
)
def test_unapproved_neighbouring_positions_are_not_expanded(registered, slot):
    result = placement(query(slot=slot), card=card(positions=[registered]))
    assert not result.allowed and result.placement_type is None
    assert result.matched_positions == () and result.exclusion == "unregistered_family"


def test_multiple_registration_matches_keep_the_observation_order():
    base = card(positions=["LCM", "ST", "CM", "RCM"])
    result = placement(query(slot="RCM"), card=base)
    assert result.matched_positions == ("LCM", "CM", "RCM")


def test_performance_does_not_make_an_unregistered_family_allowed():
    base = card(positions=["GK"], position_ovr={"CB": 140})
    assert (
        score_performance(
            PerformanceQuery.model_validate(query().model_dump()), card=base
        ).performance
        == 70
    )
    assert not placement(query(), card=base).allowed


def test_basic_placement_does_not_guarantee_ovr_or_enhancement_availability():
    base = card(positions=["CB"], position_ovr={})
    request = query(reinforce_level=7)
    assert placement(request, card=base).allowed
    assert (
        score_performance(
            PerformanceQuery.model_validate(request.model_dump()), card=base
        ).exclusion
        == "missing_reinforcement"
    )
    assert (
        score_performance(
            PerformanceQuery.model_validate(query().model_dump()), card=base
        ).exclusion
        == "missing_ovr"
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"spid": 123000002, "person_id": 2}, "card_mismatch"),
        ({"base_reinforce_level": 6}, "baseline_mismatch"),
        ({"status": "unknown", "valid_until": None}, "unusable_observation"),
        ({"status": "stale"}, "unusable_observation"),
    ],
)
def test_unusable_identity_baseline_and_status_are_excluded(changes, reason):
    result = placement(query(), card=card(positions=["CB"], **changes))
    assert not result.allowed and result.exclusion == reason


@pytest.mark.parametrize("offset", [-timedelta(microseconds=1), timedelta(hours=1)])
def test_observation_and_expiry_boundaries(offset):
    base = card(positions=["CB"], valid_until=OBSERVED + timedelta(hours=1))
    assert (
        placement(query(when=OBSERVED + offset), card=base).exclusion
        == "unusable_observation"
    )


def test_spec_mutation_does_not_enable_model_extension_or_change_families():
    spec = placement_spec()
    version = spec["placement_version"]
    spec["families"]["RCB"] = "GK"
    spec["model_extension"] = "enabled"
    result = placement(query(), card=card(positions=["GK"]))
    assert not result.allowed and result.placement_version == version
    assert placement_spec()["model_extension"] == "disabled"
    with pytest.raises(ValidationError):
        query(model_probability=1.0)


def test_invalid_or_missing_observation_is_not_allowed():
    assert placement(query(), card=None).exclusion == "missing_card"
    assert placement(query(), card={}).exclusion == "invalid_observation"
    base = card(positions=["CB"])
    base.stats["speed"] = -1
    assert placement(query(), card=base).exclusion == "invalid_observation"


def test_data_and_sources_are_preserved_without_file_io(monkeypatch):
    base = card(positions=["CB"])
    before = copy.deepcopy(base.model_dump())
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: pytest.fail("배치 함수의 파일 I/O 금지"),
    )
    result = placement(query(), card=base)
    assert result.allowed and base.model_dump() == before
    assert (
        result.observation.version == base.version
        and result.observation.valid_until == base.valid_until
    )
    assert "performance" not in result.model_dump()


@pytest.mark.parametrize(
    "changes",
    [
        {"spid": "123000001"},
        {"slot": "CF"},
        {"reinforce_level": True},
        {"reinforce_level": 12},
        {"when": OBSERVED.replace(tzinfo=None)},
    ],
)
def test_invalid_requests_are_not_coerced(changes):
    with pytest.raises(ValidationError):
        query(**changes)


def test_placement_requires_its_explicit_query_type():
    with pytest.raises(TypeError):
        placement({}, card=card())
