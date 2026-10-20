"""합성 리그의 재현성·5강 호환·명시 배치·실제 엔진 연결을 검증한다."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from test_condition_normalization import extracted_request
from test_schema import OBSERVED

from FRS.agent.material import (
    SyntheticLeagueSpec,
    generate_synthetic_material,
    synthetic_material_spec,
)
from FRS.core.reinforcement import derive_abilities, reinforcement_delta
from FRS.core.squad_recommend.build import build_squad_request
from FRS.schema.models import PlayerCard
from FRS.schema.scope import FORMATION_SLOTS, PRIORITY_CLUBS


def config(**changes):
    return SyntheticLeagueSpec(
        **{
            "run_id": "synthetic-test",
            "seed": 123,
            "observed_at": OBSERVED,
            "members_per_family": 3,
            **changes,
        }
    )


@pytest.fixture(scope="module")
def material():
    return generate_synthetic_material(config())


def build(material, formation="4-2-3-1", club="리버풀", **text_changes):
    original, extracted = extracted_request(
        {
            "price_cap": str(10**34 - 1) + "BP",
            "salary_cap": "999",
            "formation": formation,
            "team_color": club,
            "allowed_reinforcements": "5강,11강",
            **text_changes,
        }
    )
    return build_squad_request(
        original,
        extracted,
        batches=material.batches,
        t0=OBSERVED,
        clock=lambda: OBSERVED,
        **material.load(material.batches),
    )


@pytest.mark.parametrize("formation", tuple(FORMATION_SLOTS))
@pytest.mark.parametrize("club", PRIORITY_CLUBS)
def test_all_clubs_and_formations_reach_real_solver_with_independent_validation(
    material, formation, club
):
    result = build(material, formation, club)
    assert result.engine.status == "optimal"
    report = result.engine.validation
    assert report.valid and len(report.players) == 11
    assert len({p.person_id for p in report.players}) == 11
    assert {p.slot for p in report.players} == set(FORMATION_SLOTS[formation])
    assert report.total_price == sum(p.price for p in report.players)
    assert report.total_salary == sum(p.salary for p in report.players)
    assert report.total_ovr == sum(p.raw_ovr for p in report.players)
    assert {p.reinforce_level for p in report.players} <= {5, 11}


def test_provenance_reproduces_level_five_and_absolute_ovr_and_delta(material):
    content = material.to_dict()
    originals = {c["spid"]: PlayerCard.model_validate(c) for c in content["originals"]}
    data = material.load(material.batches)
    cards = {c.spid: c for c in data["cards"]}
    assert len(cards) == 108
    assert len(data["prices"]) == len(data["salaries"]) == 756
    assert len(data["reinforcements"]) == 648 and len(data["team_colors"]) == 108
    for item in content["ability_provenance"]:
        expected = derive_abilities(originals[item["spid"]], item["target_level"])
        assert item["derived_version"] == expected.version
        assert (
            item["source_payload_sha256"] == expected.to_dict()["source_payload_sha256"]
        )
    for c in cards.values():
        expected = derive_abilities(originals[c.spid], 5).to_dict()
        assert c.base_reinforce_level == 5
        assert c.version == expected["version"]
        assert (
            c.stats == expected["stats"] and c.position_ovr == expected["position_ovr"]
        )
        assert c.height is None and c.left_foot is None
    for row in data["reinforcements"]:
        c = cards[row.spid]
        expected = derive_abilities(originals[row.spid], row.reinforce_level).to_dict()
        assert row.baseline_card_version == c.version
        assert set(row.stats_delta.values()) == {
            reinforcement_delta(5, row.reinforce_level)
        }
        assert row.position_ovr == expected["position_ovr"]
    for rows in data.values():
        assert all(r.source == "synthetic" and r.observed_at == OBSERVED for r in rows)


def test_same_inputs_are_deterministic_and_changed_generation_gets_new_batches(
    material,
):
    assert generate_synthetic_material(config()) == material
    for changes in (
        {"seed": 124},
        {"run_id": "other-run"},
        {"observed_at": OBSERVED + timedelta(seconds=1)},
        {"members_per_family": 2},
    ):
        other = generate_synthetic_material(config(**changes))
        assert other.material_id != material.material_id
        assert other.batches != material.batches


def test_explicit_batches_and_independent_copies(material):
    with pytest.raises(ValueError, match="명시 배치"):
        material.load(material.batches.model_copy(update={"card_price": "other"}))
    with pytest.raises(TypeError):
        material.load(None)
    first = material.load(material.batches)
    first["cards"][0].stats["speed"] = 999
    first["prices"][0].price = 1
    content = material.to_dict()
    content["records"]["cards"].clear()
    second = material.load(material.batches)
    assert second["cards"][0].stats["speed"] != 999
    assert second["prices"][0].price != 1
    assert second["prices"][0].valid_until == OBSERVED + timedelta(hours=24)
    assert second["cards"][0].valid_until == OBSERVED + timedelta(days=7)


def test_known_infeasible_budget_and_singleton_shortage_are_not_faked(material):
    assert build(material, price_cap="0BP").engine.status == "infeasible"
    tiny = generate_synthetic_material(config(members_per_family=1))
    assert build(tiny).engine.status == "infeasible"


def test_expiry_is_not_extended_by_loading_material_again(material):
    original, extracted = extracted_request(
        {"price_cap": "1조", "salary_cap": "999", "allowed_reinforcements": "5강,11강"}
    )
    result = build_squad_request(
        original,
        extracted,
        batches=material.batches,
        t0=OBSERVED,
        clock=lambda: OBSERVED + timedelta(hours=24),
        **material.load(material.batches),
    )
    assert result.engine.status == "data_expired" and result.engine.validation is None
    assert material.load(material.batches)["prices"][
        0
    ].valid_until == OBSERVED + timedelta(hours=24)


@pytest.mark.parametrize(
    "changes",
    [
        {"seed": True},
        {"seed": -1},
        {"members_per_family": 0},
        {"members_per_family": 31},
        {"run_id": "../escape"},
        {"run_id": ""},
        {"observed_at": OBSERVED.replace(tzinfo=None)},
    ],
)
def test_generation_requires_valid_explicit_inputs(changes):
    with pytest.raises(ValidationError):
        config(**changes)


def test_no_io_or_hidden_generation_or_model_use():
    with (
        patch("builtins.open", side_effect=AssertionError("파일 접근 금지")),
        patch("socket.socket", side_effect=AssertionError("네트워크 접근 금지")),
    ):
        generated = generate_synthetic_material(config(members_per_family=1))
        assert generated.load(generated.batches)["cards"]
        assert (
            synthetic_material_spec()["player_metadata"]
            == "not_generated_not_model_input"
        )
