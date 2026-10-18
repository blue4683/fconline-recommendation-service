"""가상 선수·수치로 검색 계약과 읽기 전용 어댑터를 검증한다."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from bson import BSON
from bson.codec_options import CodecOptions
from pydantic import ValidationError
from pymongo.errors import OperationFailure
from test_schema import OBSERVED, VALID, card, common, price

from FRS.core.candidate_search import BatchSelection, CandidateQuery, candidate_search
from FRS.core.candidate_search.mongo import (
    CANDIDATE_INDEXES,
    MongoCandidateRepository,
    create_candidate_indexes,
)
from FRS.schema.models import CardSalary, ReinforceStats, TeamColor

CLUBS = ("가상클럽_알파",)
BATCHES = BatchSelection(
    player_card="synthetic-card",
    card_salary="synthetic-salary",
    card_price="synthetic-price",
    reinforce_stats="synthetic-reinforce",
    team_color="synthetic-color",
)
RESOURCES = {
    "cards": "player_card",
    "salaries": "card_salary",
    "prices": "card_price",
    "reinforcements": "reinforce_stats",
    "team_colors": "team_color",
}


def query(**changes):
    return CandidateQuery(
        **{
            "position": "ST",
            "salary_cap": 20,
            "price_cap": 10**20 + 7,
            "team_color": CLUBS[0],
            "reinforce_level": 5,
            "when": OBSERVED,
            "batches": BATCHES,
            **changes,
        }
    )


def changed(row, **updates):
    return type(row)(**{**row.model_dump(), **updates})


def dataset(level=5):
    version = "base-v1" if level == 5 else "ability-v6"
    return {
        "cards": [card(batch_id=BATCHES.player_card, version="base-v1")],
        "salaries": [
            CardSalary(
                **common(
                    batch_id=BATCHES.card_salary,
                    version=version,
                    reinforce_level=level,
                    salary=20,
                )
            )
        ],
        "prices": [
            price(
                batch_id=BATCHES.card_price, version="price-v1", reinforce_level=level
            )
        ],
        "reinforcements": [
            ReinforceStats(
                **common(
                    batch_id=BATCHES.reinforce_stats,
                    version="ability-v6",
                    reinforce_level=6,
                    baseline_reinforce_level=5,
                    baseline_card_version="base-v1",
                    stats_delta={"speed": 3},
                )
            )
        ],
        "team_colors": [
            TeamColor(
                **common(
                    batch_id=BATCHES.team_color,
                    version="color-v1",
                    type="club",
                    name=CLUBS[0],
                )
            )
        ],
    }


def search(data=None, q=None):
    return candidate_search(
        q or query(), **(dataset() if data is None else data), supported_clubs=CLUBS
    )


def test_exact_caps_and_distinct_resource_batches_join_without_losing_provenance():
    data = dataset()
    before = deepcopy(data)
    result = search(data)
    assert result.inspected_count == 1 and not result.exclusions
    candidate = result.candidates[0]
    assert candidate.card.spid == 123000001
    assert candidate.salary.salary == 20
    assert candidate.price.price == 10**20 + 7
    assert candidate.price.version != candidate.card.version
    assert candidate.team_color.version != candidate.card.version
    assert candidate.reinforcement is None
    assert data == before


@pytest.mark.parametrize(
    "changes,key",
    [
        ({"salary_cap": 19}, "card_salary"),
        ({"price_cap": 10**20 + 6}, "card_price"),
        ({"salary_cap": 0}, "card_salary"),
        ({"price_cap": 0}, "card_price"),
    ],
)
def test_strict_caps_exclude_with_one_reason_per_card(changes, key):
    result = search(q=query(**changes))
    assert not result.candidates and result.excluded_count == 1
    assert result.exclusion_counts == {f"{key}:over_cap": 1}


def test_empty_input_and_orphan_resources_do_not_invent_cards():
    data = dataset()
    data["cards"] = []
    assert search(data).inspected_count == 0


@pytest.mark.parametrize("position", ["RCB", "LCB", "RS", "LS", "GK"])
def test_registered_position_does_not_expand_lateral_or_group_slots(position):
    result = search(q=query(position=position))
    assert result.exclusion_counts == {"player_card:position_mismatch": 1}


def test_registered_secondary_position_is_accepted():
    assert len(search(q=query(position="CF")).candidates) == 1


@pytest.mark.parametrize(
    "options", [{"team_color": "가상클럽_미지원"}, {"team_color_type": "nation"}]
)
def test_unsupported_team_color_is_a_request_error(options):
    result = search(q=query(**options))
    assert result.request_errors == ("unsupported_team_color",)
    assert not result.candidates and not result.exclusions


def test_default_scope_does_not_silently_accept_synthetic_clubs():
    result = candidate_search(query(), **dataset())
    assert result.request_errors == ("unsupported_team_color",)


@pytest.mark.parametrize("resource", RESOURCES)
@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "conflict",
        "status_unknown",
        "status_stale",
        "status_untradeable",
        "expired",
        "not_yet_observed",
        "retention_expired",
        "wrong_batch",
    ],
)
def test_every_required_resource_is_checked_and_reported(resource, problem):
    data = dataset(6)
    row = data[resource][0]
    if problem == "missing":
        data[resource] = []
    elif problem == "conflict":
        data[resource].append(changed(row, version="conflicting-version"))
    elif problem.startswith("status_"):
        data[resource] = [
            changed(row, status=problem.removeprefix("status_"), valid_until=None)
        ]
    elif problem == "expired":
        data[resource] = [
            changed(
                row, observed_at=OBSERVED - timedelta(hours=1), valid_until=OBSERVED
            )
        ]
    elif problem == "not_yet_observed":
        data[resource] = [changed(row, observed_at=OBSERVED + timedelta(seconds=1))]
    elif problem == "retention_expired":
        data[resource] = [
            changed(
                row,
                observed_at=OBSERVED - timedelta(hours=1),
                valid_until=OBSERVED,
                delete_after=OBSERVED,
            )
        ]
    elif problem == "wrong_batch":
        data[resource] = [changed(row, batch_id="unselected-batch")]
    result = search(data, query(reinforce_level=6))
    if resource == "cards" and problem in ("missing", "wrong_batch"):
        assert result.inspected_count == 0 and not result.candidates
        return
    expected = "missing" if problem == "wrong_batch" else problem
    assert result.exclusion_counts == {f"{RESOURCES[resource]}:{expected}": 1}
    assert not result.candidates and result.inspected_count == 1


def test_identical_duplicates_and_input_order_do_not_change_results():
    data = dataset(6)
    baseline = search(data, query(reinforce_level=6))
    for rows in data.values():
        rows.extend(deepcopy(rows))
        rows.reverse()
    assert search(data, query(reinforce_level=6)) == baseline


def test_ambiguous_expired_record_is_not_overridden_by_fresh_record():
    data = dataset()
    data["prices"].append(
        changed(
            data["prices"][0],
            observed_at=OBSERVED - timedelta(hours=1),
            valid_until=OBSERVED,
        )
    )
    assert search(data).exclusion_counts == {"card_price:conflict": 1}


def test_as_of_time_has_inclusive_observation_and_exclusive_expiry():
    assert search(q=query(when=OBSERVED)).candidates
    assert search(q=query(when=VALID - timedelta(microseconds=1))).candidates
    assert search(q=query(when=VALID)).exclusion_counts == {"player_card:expired": 1}
    assert search(
        q=query(when=OBSERVED - timedelta(microseconds=1))
    ).exclusion_counts == {"player_card:not_yet_observed": 1}


@pytest.mark.parametrize(
    "options",
    [{"baseline_card_version": "wrong-base"}, {"baseline_reinforce_level": 4}],
)
def test_enhanced_ability_requires_exact_baseline_identity(options):
    data = dataset(6)
    data["reinforcements"] = [changed(data["reinforcements"][0], **options)]
    assert search(data, query(reinforce_level=6)).exclusion_counts == {
        "reinforce_stats:baseline_mismatch": 1
    }


@pytest.mark.parametrize("level", [5, 6])
def test_salary_requires_the_selected_ability_version(level):
    data = dataset(level)
    data["salaries"] = [changed(data["salaries"][0], version="other-ability")]
    assert search(data, query(reinforce_level=level)).exclusion_counts == {
        "card_salary:version_mismatch": 1
    }


def test_enhanced_candidate_keeps_observed_delta_and_never_reuses_base_salary():
    data = dataset(6)
    candidate = search(data, query(reinforce_level=6)).candidates[0]
    assert candidate.reinforcement.stats_delta == {"speed": 3}
    data["salaries"] = dataset()["salaries"]
    assert search(data, query(reinforce_level=6)).exclusion_counts == {
        "card_salary:missing": 1
    }


def test_other_club_and_other_grade_do_not_supply_missing_observations():
    data = dataset()
    data["team_colors"] = [changed(data["team_colors"][0], name=CLUBS[0] + " II")]
    assert search(data).exclusion_counts == {"team_color:missing": 1}
    data = dataset()
    data["prices"] = [changed(data["prices"][0], reinforce_level=6)]
    assert search(data).exclusion_counts == {"card_price:missing": 1}


def test_results_are_sorted_by_spid_without_ranking_and_counts_reconcile():
    data = dataset()
    for resource, rows in data.items():
        changes = {"spid": 123000002}
        if resource == "cards":
            changes.update(person_id=2, name="가상선수_0002")
        rows.insert(0, changed(rows[0], **changes))
    result = search(data)
    assert [c.card.spid for c in result.candidates] == [123000001, 123000002]
    data["cards"][0] = changed(data["cards"][0], positions=["GK"])
    result = search(data)
    assert result.inspected_count == 2 and result.excluded_count == 1
    assert sum(result.exclusion_counts.values()) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"salary_cap": -1},
        {"salary_cap": True},
        {"salary_cap": "20"},
        {"price_cap": 1.5},
        {"price_cap": 10**34},
        {"price_cap": True},
        {"reinforce_level": 4},
        {"reinforce_level": 12},
        {"reinforce_level": True},
        {"when": OBSERVED.replace(tzinfo=None)},
        {"position": "FW"},
    ],
)
def test_invalid_request_is_rejected_before_lookup(options):
    with pytest.raises(ValidationError):
        query(**options)


def test_batch_selection_requires_every_resource_and_rejects_implicit_latest():
    values = BATCHES.model_dump()
    for name in values:
        with pytest.raises(ValidationError):
            BatchSelection(
                **{key: value for key, value in values.items() if key != name}
            )
        with pytest.raises(ValidationError):
            BatchSelection(**{**values, name: "  "})


def mongo_database(data):
    database = MagicMock()
    database.with_options.return_value = database
    collections = {name: MagicMock() for name in RESOURCES.values()}
    database.__getitem__.side_effect = collections.__getitem__
    for arg, name in RESOURCES.items():
        documents = [
            BSON.encode(row.to_document()).decode(
                codec_options=CodecOptions(tz_aware=True)
            )
            for row in data[arg]
        ]
        database[name].find.return_value = documents
    return database


def test_mongo_uses_exact_batches_and_preserves_conflict_diagnostics_and_bson_bp():
    data = dataset(6)
    data["prices"].append(changed(data["prices"][0], price=10**20 + 8))
    db = mongo_database(data)
    result = MongoCandidateRepository(db, supported_clubs=CLUBS).search(
        query(reinforce_level=6)
    )
    assert result == search(data, query(reinforce_level=6))
    assert result.exclusion_counts == {"card_price:conflict": 1}
    assert db.with_options.call_args.kwargs["codec_options"].tz_aware
    for resource, batch_id in BATCHES.model_dump().items():
        selector = {"batch_id": batch_id}
        if resource in ("card_salary", "card_price", "reinforce_stats"):
            selector["reinforce_level"] = 6
        if resource == "team_color":
            selector.update(type="club", name=CLUBS[0])
        db[resource].find.assert_called_once_with(selector)
        db[resource].create_index.assert_not_called()
        db[resource].insert_one.assert_not_called()


def test_mongo_bson_roundtrip_retains_price_above_int64_exactly():
    data = dataset()
    data["prices"] = [changed(data["prices"][0], price=10**34 - 1)]
    q = query(price_cap=10**34 - 1)
    result = MongoCandidateRepository(
        mongo_database(data), supported_clubs=CLUBS
    ).search(q)
    assert result == search(data, q)
    assert result.candidates[0].price.price == 10**34 - 1


def test_mongo_does_not_lookup_unsupported_team_color():
    db = MagicMock()
    result = MongoCandidateRepository(db, supported_clubs=CLUBS).search(
        query(team_color="가상클럽_미지원")
    )
    assert result.request_errors == ("unsupported_team_color",)
    db.__getitem__.assert_not_called()


def test_mongo_propagates_db_error_instead_of_reporting_empty_candidates():
    db = mongo_database(dataset())
    db["player_card"].find.side_effect = OperationFailure("가상 조회 실패")
    with pytest.raises(OperationFailure):
        MongoCandidateRepository(db, supported_clubs=CLUBS).search(query())


def test_mongo_rejects_invalid_stored_schema_instead_of_silently_skipping():
    db = mongo_database(dataset())
    db["player_card"].find.return_value = [{"spid": 123000001}]
    with pytest.raises(ValidationError):
        MongoCandidateRepository(db, supported_clubs=CLUBS).search(query())


def test_indexes_are_explicit_nonunique_and_do_not_destroy_conflicting_evidence():
    db = mongo_database(dataset())
    create_candidate_indexes(db)
    for resource, keys in CANDIDATE_INDEXES.items():
        db[resource].create_index.assert_called_once_with(
            list(keys), name="candidate_lookup"
        )
