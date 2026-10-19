"""스냅샷의 고정·출처·강화·기한 계약을 가상 선수와 수치만으로 검증한다."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import timedelta, timezone
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from test_candidate_search import BATCHES, CLUBS, changed, dataset
from test_schema import OBSERVED, VALID

from FRS.core.candidate_search import candidate_search
from FRS.core.squad_recommend.snapshot import (
    SnapshotQuery,
    build_snapshot,
    snapshot_spec,
)
from FRS.schema.scope import FORMATION_SLOTS


def query(**changes):
    return SnapshotQuery(
        **{
            "formation": "4-2-2-2",
            "team_color": CLUBS[0],
            "salary_cap": 100,
            "price_cap": 10**25,
            "allowed_reinforcements": (5,),
            "batches": BATCHES,
            "t0": OBSERVED,
            **changes,
        }
    )


def material():
    data = dataset()
    extra = dataset(6)
    data["salaries"] += extra["salaries"]
    data["prices"] += extra["prices"]
    data["reinforcements"] = [
        changed(data["reinforcements"][0], position_ovr={"ST": 120})
    ]
    return data


def build(data=None, q=None):
    return build_snapshot(
        q or query(), **(material() if data is None else data), supported_clubs=CLUBS
    )


def pairs(snapshot):
    return {
        (r["spid"], r["reinforce_level"], r["slot"])
        for r in snapshot.to_dict()["candidates"]
    }


def test_mixed_grade_variants_keep_absolute_ovr_and_full_provenance():
    snap = build(q=query(allowed_reinforcements=(6, 5)))
    content = snap.to_dict()
    assert pairs(snap) == {
        (123000001, grade, slot) for grade in (5, 6) for slot in ("RS", "LS")
    }
    assert content["query"]["allowed_reinforcements"] == [5, 6]
    assert [r["performance"] for r in content["candidates"]] == [50, 60, 50, 60]
    five, six = content["candidates"][:2]
    assert "reinforce_stats" not in five["observations"]
    assert six["observations"]["reinforce_stats"]["position_ovr"] == {"ST": 120}
    assert six["observations"]["card_salary"]["version"] == "ability-v6"
    assert five["observations"]["card_price"]["price"] == 10**20 + 7
    assert content["metadata"]["used_observation_count"] == 7
    assert set(content["metadata"]["batches"]) == set(BATCHES.model_dump().values())
    assert content["metadata"]["model_version"] is None


@pytest.mark.parametrize("formation", FORMATION_SLOTS)
def test_only_requested_formation_slots_are_evaluated(formation):
    snap = build(q=query(formation=formation))
    content = snap.to_dict()
    assert {r["slot"] for r in content["candidates"] + content["exclusions"]} <= set(
        FORMATION_SLOTS[formation]
    )
    assert {r["slot"] for r in content["candidates"]} == (
        {"RS", "LS"} if formation == "4-2-2-2" else {"ST"}
    )


@pytest.mark.parametrize(
    "registered", [("CB",), ("RCB",), ("LCB",), ("CB", "RCB", "LCB")]
)
def test_symmetric_registered_family_union_deduplicates_each_card_grade_slot(
    registered,
):
    data = material()
    data["cards"] = [
        changed(data["cards"][0], positions=registered, position_ovr={"CB": 110})
    ]
    snap = build(data)
    assert pairs(snap) == {(123000001, 5, "RCB"), (123000001, 5, "LCB")}
    assert len(snap.to_dict()["candidates"]) == 2
    assert not [e for e in snap.to_dict()["exclusions"] if e["slot"] in ("RCB", "LCB")]


def test_unregistered_ovr_and_out_of_range_score_do_not_produce_candidate():
    data = material()
    data["cards"] = [
        changed(data["cards"][0], positions=("CF",), position_ovr={"ST": 120})
    ]
    assert not build(data).to_dict()["candidates"]
    data["cards"] = [
        changed(data["cards"][0], positions=("ST",), position_ovr={"ST": 201})
    ]
    snap = build(data)
    assert {
        (e["resource"], e["reason"])
        for e in snap.to_dict()["exclusions"]
        if e["slot"] in ("RS", "LS")
    } == {("performance", "ovr_out_of_range")}


def test_missing_grade_has_no_fallback_but_valid_variant_remains():
    snap = build(q=query(allowed_reinforcements=(5, 7)))
    assert {p[1] for p in pairs(snap)} == {5}
    assert any(
        e["reinforce_level"] == 7
        and e["reason"] == "missing"
        and e["resource"] == "reinforce_stats"
        for e in snap.to_dict()["exclusions"]
    )


def test_missing_ovr_excludes_only_selected_grade_pairs():
    data = material()
    data["reinforcements"] = [changed(data["reinforcements"][0], position_ovr={})]
    snap = build(data, query(allowed_reinforcements=(5, 6)))
    assert {p[1] for p in pairs(snap)} == {5}
    assert any(e["reason"] == "missing_ovr" for e in snap.to_dict()["exclusions"])


@pytest.mark.parametrize(
    "resource,reason",
    [("prices", "conflict"), ("cards", "conflict"), ("team_colors", "status_unknown")],
)
def test_conflicts_and_unknown_are_reported_without_latest_selection(resource, reason):
    data = material()
    row = data[resource][0]
    if reason == "conflict":
        data[resource].append(
            changed(
                row,
                version="another-version",
                observed_at=OBSERVED - timedelta(minutes=5),
            )
        )
    else:
        data[resource] = [changed(row, status="unknown", valid_until=None)]
    before = deepcopy(data)
    snap = build(data)
    assert not snap.to_dict()["candidates"]
    assert any(e["reason"] == reason for e in snap.to_dict()["exclusions"])
    assert snap.to_dict()["metadata"]["valid_until"] is None
    assert data == before


def test_independent_batches_do_not_join_unselected_sources():
    values = BATCHES.model_dump()
    snap = build(q=query(batches={**values, "card_price": "absent-batch"}))
    assert not pairs(snap)
    assert any(
        e["resource"] == "card_price" and e["reason"] == "missing"
        for e in snap.to_dict()["exclusions"]
    )


def test_expiry_uses_all_included_candidates_but_no_excluded_observation():
    data = material()
    earlier = OBSERVED + timedelta(minutes=20)
    data["prices"][1] = changed(data["prices"][1], valid_until=earlier)
    snap = build(data, query(allowed_reinforcements=(5, 6)))
    assert snap.to_dict()["metadata"]["valid_until"] == earlier.isoformat()
    assert snap.check_response(earlier - timedelta(microseconds=1))["status"] == "ready"
    response = snap.check_response(earlier)
    assert response["status"] == "data_expired" and response["candidates"] == []
    assert pairs(snap)
    base_only = build(data)
    assert base_only.to_dict()["metadata"]["valid_until"] == VALID.isoformat()


def test_excluded_short_lived_candidate_does_not_expire_valid_pool():
    data = material()
    for key in data:
        row = data[key][0]
        updates = dict(spid=123000002, valid_until=OBSERVED + timedelta(minutes=1))
        if key == "cards":
            updates.update(person_id=2, name="가상선수_0002", position_ovr={"ST": 201})
        data[key].append(changed(row, **updates))
    snap = build(data)
    assert {p[0] for p in pairs(snap)} == {123000001}
    assert snap.to_dict()["metadata"]["valid_until"] == VALID.isoformat()
    assert snap.check_response(OBSERVED + timedelta(minutes=2))["status"] == "ready"


def test_observation_range_uses_original_used_times_and_retention_deadline():
    data = material()
    earlier = OBSERVED - timedelta(minutes=10)
    deadline = OBSERVED + timedelta(minutes=15)
    data["prices"][0] = changed(
        data["prices"][0],
        observed_at=earlier,
        valid_until=deadline,
        delete_after=deadline,
    )
    meta = build(data).to_dict()["metadata"]
    assert meta["observed_at_min"] == earlier.isoformat()
    assert meta["observed_at_max"] == OBSERVED.isoformat()
    assert meta["valid_until"] == deadline.isoformat()


def test_empty_material_reports_no_candidates_without_global_infeasibility():
    snap = build({k: [] for k in material()})
    meta = snap.to_dict()["metadata"]
    assert meta["batches"] == [] and meta["used_observation_count"] == 0
    assert (
        meta["observed_at_min"]
        is meta["observed_at_max"]
        is meta["valid_until"]
        is None
    )
    assert snap.check_response(VALID)["status"] == "no_candidates"


def test_input_and_returned_nested_mutations_cannot_change_snapshot():
    data = material()
    before = deepcopy(data)
    snap = build(data)
    original = snap.to_dict()
    assert data == before
    data["cards"][0].position_ovr["ST"] = 199
    data["cards"][0].stats["speed"] = 7
    data["prices"].clear()
    returned = snap.to_dict()
    returned["candidates"][0]["observations"]["player_card"]["stats"]["speed"] = 1
    returned["metadata"]["batches"].clear()
    response = snap.check_response(OBSERVED)
    response["candidates"].clear()
    assert snap.to_dict() == original
    with pytest.raises(FrozenInstanceError):
        snap._payload = b"changed"


def test_input_order_and_identical_duplicates_preserve_hash_and_conflicts():
    data = material()
    original = build(data, query(allowed_reinforcements=(5, 6)))
    for rows in data.values():
        rows.extend(deepcopy(rows))
        rows.reverse()
    reordered = build(data, query(allowed_reinforcements=(6, 5)))
    assert reordered.to_dict() == original.to_dict()
    data["prices"][0] = changed(data["prices"][0], price=10**20 + 9)
    assert (
        build(data, query(allowed_reinforcements=(5, 6))).snapshot_id
        != original.snapshot_id
    )


def test_each_generator_is_read_once_and_no_external_io_occurs():
    class Once:
        def __init__(self, rows):
            self.rows, self.count = rows, 0

        def __iter__(self):
            self.count += 1
            assert self.count == 1
            return iter(self.rows)

    sources = {k: Once(v) for k, v in material().items()}
    with (
        patch("builtins.open", side_effect=AssertionError("파일 I/O 금지")),
        patch("socket.socket", side_effect=AssertionError("네트워크 금지")),
    ):
        snap = build(sources)
        assert snap.check_response(OBSERVED)["status"] == "ready"
        assert snap.check_response(VALID)["status"] == "data_expired"
    assert all(s.count == 1 for s in sources.values())


def test_family_searches_are_cached_in_memory_and_exact_search_stays_unchanged():
    with patch(
        "FRS.core.squad_recommend.snapshot.candidate_search", wraps=candidate_search
    ) as search:
        build()
    positions = [call.args[0].position for call in search.call_args_list]
    assert len(positions) == len(set(positions))
    assert {"ST", "RS", "LS"} <= set(positions)


@pytest.mark.parametrize(
    "updates",
    [
        {"formation": "3-5-2"},
        {"allowed_reinforcements": ()},
        {"allowed_reinforcements": (5, 5)},
        {"allowed_reinforcements": (4,)},
        {"allowed_reinforcements": (12,)},
        {"allowed_reinforcements": (True,)},
        {"allowed_reinforcements": ("5",)},
        {"t0": OBSERVED.replace(tzinfo=None)},
        {"salary_cap": -1},
        {"price_cap": True},
        {"price_cap": 10**34},
        {"model_version": "implicit"},
    ],
)
def test_invalid_request_is_rejected(updates):
    with pytest.raises(ValidationError):
        query(**updates)


@pytest.mark.parametrize(
    "when", [OBSERVED - timedelta(microseconds=1), OBSERVED.replace(tzinfo=None), "now"]
)
def test_invalid_response_time_is_rejected(when):
    with pytest.raises(ValueError):
        build().check_response(when)


def test_equivalent_timezone_is_normalized_for_hash_and_expiry():
    local = timezone(timedelta(hours=9))
    assert (
        build(q=query(t0=OBSERVED.astimezone(local))).snapshot_id == build().snapshot_id
    )
    assert build().check_response(VALID.astimezone(local))["status"] == "data_expired"


def test_invalid_nested_observation_is_revalidated_without_silent_empty_result():
    data = material()
    data["cards"][0].stats["speed"] = -1
    with pytest.raises(ValidationError):
        build(data)
    data = material()
    data["cards"] = [{}]
    with pytest.raises(TypeError):
        build(data)


def test_unsupported_club_is_rejected_before_consuming_material():
    def forbidden():
        raise AssertionError("미지원 클럽에서 자료를 읽으면 안 됩니다.")
        yield

    with pytest.raises(ValueError, match="클럽"):
        build({k: forbidden() for k in material()}, query(team_color="가상클럽_미지원"))


def test_snapshot_spec_is_independent_and_returned_copy_is_mutable_safely():
    before = snapshot_spec()
    spec = snapshot_spec()
    spec["model_extension"] = "enabled"
    assert snapshot_spec() == before
    assert len(before["snapshot_policy_version"]) == 64
    assert (
        build().to_dict()["metadata"]["snapshot_policy_version"]
        == before["snapshot_policy_version"]
    )


@pytest.mark.parametrize(
    "resource", ["cards", "salaries", "prices", "reinforcements", "team_colors"]
)
@pytest.mark.parametrize("problem", ["unknown", "expired", "future"])
def test_required_resource_failure_excludes_only_its_affected_variant(
    resource, problem
):
    data = material()
    row = data[resource][0]
    changes = {
        "unknown": dict(status="unknown", valid_until=None),
        "expired": dict(
            observed_at=OBSERVED - timedelta(hours=1), valid_until=OBSERVED
        ),
        "future": dict(observed_at=OBSERVED + timedelta(minutes=1)),
    }[problem]
    data[resource][0] = changed(row, **changes)
    snap = build(data, query(allowed_reinforcements=(5, 6)))
    levels = {p[1] for p in pairs(snap)}
    expected = (
        {6}
        if resource in ("salaries", "prices")
        else ({5} if resource == "reinforcements" else set())
    )
    assert levels == expected
    expected_reason = {
        "unknown": "status_unknown",
        "expired": "expired",
        "future": "not_yet_observed",
    }[problem]
    assert any(e["reason"] == expected_reason for e in snap.to_dict()["exclusions"])


@pytest.mark.parametrize(
    "changes,resource",
    [({"salary_cap": 19}, "card_salary"), ({"price_cap": 10**20 + 6}, "card_price")],
)
def test_individual_cap_violation_is_reported_without_claiming_team_infeasibility(
    changes, resource
):
    snap = build(q=query(**changes))
    assert snap.check_response(OBSERVED)["status"] == "no_candidates"
    assert any(
        e["resource"] == resource and e["reason"] == "over_cap"
        for e in snap.to_dict()["exclusions"]
    )


def test_same_person_different_cards_are_retained_for_later_selection_constraints():
    data = material()
    for key, rows in data.items():
        updates = dict(spid=124000001)
        if key == "cards":
            updates.update(season_id=124)
        rows.append(changed(rows[0], **updates))
    assert {p[0] for p in pairs(build(data))} == {123000001, 124000001}


def test_every_supported_grade_can_be_retained_without_choosing_best_grade():
    data = material()
    for grade in range(7, 12):
        for key in ("salaries", "prices", "reinforcements"):
            row = data[key][-1]
            data[key].append(changed(row, reinforce_level=grade))
    snap = build(data, query(allowed_reinforcements=tuple(range(5, 12))))
    assert {p[1] for p in pairs(snap)} == set(range(5, 12))
    assert len(pairs(snap)) == 14


def test_exact_position_search_mismatch_does_not_mask_real_family_resource_failure():
    data = material()
    data["prices"] = []
    excluded = [e for e in build(data).to_dict()["exclusions"] if e["slot"] == "RS"]
    assert excluded == [
        {
            "spid": 123000001,
            "reinforce_level": 5,
            "slot": "RS",
            "resource": "card_price",
            "reason": "missing",
        }
    ]
