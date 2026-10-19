"""합성 카드로 화이트리스트·명시 관측·라벨 확장과 그룹 분리 오류를 검증한다."""

from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest
from pydantic import ValidationError
from test_schema import OBSERVED, card

from FRS.core.position_recommend.data_contract import (
    FEATURE_NAMES,
    STAT_FEATURES,
    STRING_DTYPE,
    InputContractError,
    InputSelection,
    ObservationKey,
    feature_spec,
    to_labeled_input,
    to_model_input,
    validate_card_split,
    validate_model_input,
)
from FRS.utils.crawling.ability_parser import keyword_map


def complete(**changes):
    return card(
        **{
            "stats": {name: index + 10 for index, name in enumerate(STAT_FEATURES)},
            "height": 181,
            "weight": 76,
            "physical": "가상체형_A",
            "dribble_skill": 4,
            "left_foot": 3,
            "right_foot": 5,
            **changes,
        }
    )


def selection(*cards, grade=5):
    return InputSelection(
        observations=tuple(
            ObservationKey(spid=c.spid, batch_id=c.batch_id, version=c.version)
            for c in cards
        ),
        base_reinforce_level=grade,
    )


def test_exact_40_features_and_values_exclude_identifiers_labels_and_metadata():
    c = complete(stats={**complete().stats, "salary": 987})
    before = c.model_dump()
    X = to_model_input([c], selection=selection(c))
    assert tuple(X.columns) == FEATURE_NAMES and X.shape == (1, 40)
    assert tuple(keyword_map().values()) == STAT_FEATURES
    assert X.index.tolist() == [c.spid] and X.index.name == "spid"
    assert X.loc[c.spid, "speed"] == 10 and X.loc[c.spid, "right_foot"] == 5
    assert X.loc[c.spid, "physical"] == "가상체형_A"
    assert all(X[name].dtype == "int64" for name in FEATURE_NAMES if name != "physical")
    assert X.physical.dtype == STRING_DTYPE
    assert not {
        "salary",
        "price",
        "position",
        "name",
        "spid",
        "batch_id",
        "version",
        "ST",
    } & set(X.columns)
    assert c.model_dump() == before
    assert len(feature_spec()["features"]) == 40
    spec = feature_spec()
    spec["features"].clear()
    assert len(feature_spec()["features"]) == 40 and len(feature_spec()["sha256"]) == 64


@pytest.mark.parametrize("name", FEATURE_NAMES)
def test_each_required_feature_missing_is_an_error(name):
    c = complete()
    payload = c.model_dump()
    if name in STAT_FEATURES:
        del payload["stats"][name]
    else:
        payload[name] = None
    with pytest.raises(InputContractError, match="누락"):
        to_model_input([payload], selection=selection(c))


@pytest.mark.parametrize("value", [True, 2.5, "12", -1, 2**63, None])
def test_invalid_numeric_values_are_not_coerced_or_filled(value):
    c = complete()
    c.stats["speed"] = value
    warning = (
        pytest.warns(UserWarning, match="serializer")
        if isinstance(value, (float, str))
        else nullcontext()
    )
    with warning, pytest.raises(ValidationError):
        to_model_input([c], selection=selection(c))


def test_selected_observations_have_exact_order_and_identical_duplicates_are_removed():
    a, b = complete(), complete(spid=123000002, person_id=2)
    wanted = selection(b, a)
    X = to_model_input([a.model_dump(), b, a], selection=wanted)
    assert X.index.tolist() == [b.spid, a.spid]


@pytest.mark.parametrize(
    "change", ["batch", "version", "missing", "extra", "grade", "time", "stats"]
)
def test_unselected_missing_or_conflicting_observations_fail(change):
    a = complete()
    data = [a]
    if change == "batch":
        data = [complete(batch_id="other")]
    elif change == "version":
        data = [complete(version="other")]
    elif change == "missing":
        data = []
    elif change == "extra":
        data += [complete(spid=123000002, person_id=2)]
    elif change == "grade":
        data = [complete(base_reinforce_level=6)]
    elif change == "time":
        data += [complete(observed_at=OBSERVED + timedelta(seconds=1))]
    else:
        data += [complete(height=182)]
    with pytest.raises(InputContractError):
        to_model_input(data, selection=selection(a))


def test_selection_cannot_hide_two_observations_of_the_same_card():
    a = complete()
    with pytest.raises(ValidationError, match="하나"):
        selection(a, a)
    with pytest.raises(ValidationError):
        InputSelection(observations=(), base_reinforce_level=5)


def test_historical_unknown_and_stale_inputs_preserve_original_status_and_times():
    for c in (complete(status="unknown", valid_until=None), complete(status="stale")):
        before = c.model_dump()
        assert to_model_input([c], selection=selection(c)).shape == (1, 40)
        assert c.model_dump() == before


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "extra",
        "order",
        "duplicate",
        "float",
        "object",
        "null",
        "empty",
        "negative",
        "blank",
    ],
)
def test_completed_feature_tables_are_strictly_validated(change):
    c = complete()
    X = to_model_input([c], selection=selection(c))
    if change == "missing":
        X = X.drop(columns="speed")
    elif change == "extra":
        X["spid"] = c.spid
    elif change == "order":
        X = X[list(reversed(X.columns))]
    elif change == "duplicate":
        X = pd.concat([X, X[["speed"]]], axis=1)
    elif change == "float":
        X["speed"] = X.speed.astype("float64")
    elif change == "object":
        X["physical"] = X.physical.astype("object")
    elif change == "null":
        X.loc[c.spid, "physical"] = pd.NA
    elif change == "empty":
        X = X.iloc[:0]
    elif change == "negative":
        X.loc[c.spid, "speed"] = -1
    else:
        X.loc[c.spid, "physical"] = " "
    with pytest.raises(InputContractError):
        validate_model_input(X)


@pytest.fixture
def labeled():
    a, b = complete(), complete(spid=123000002, person_id=2, positions=["GK"])
    return to_labeled_input([a, b], selection=selection(a, b))


def test_multiple_registered_positions_expand_labels_without_entering_X(labeled):
    assert labeled.X.shape == (3, 40)
    assert labeled.y.tolist() == ["ST", "CF", "GK"]
    assert labeled.card_groups.tolist() == [123000001, 123000001, 123000002]
    assert labeled.X.iloc[0].equals(labeled.X.iloc[1])
    assert "person_id" not in labeled.X and "position" not in labeled.X
    groups = labeled.card_groups
    groups.iloc[0] = 999999999
    assert labeled.card_groups.iloc[0] == 123000001
    validate_card_split(labeled, train_indices=[0, 1], test_indices=[2])


def test_missing_labels_fail_in_training_but_not_feature_extraction():
    c = complete(positions=[], status="unknown", valid_until=None)
    assert len(to_model_input([c], selection=selection(c))) == 1
    with pytest.raises(InputContractError, match="라벨"):
        to_labeled_input([c], selection=selection(c))


@pytest.mark.parametrize(
    "train,test",
    [
        ([0], [1, 2]),
        ([0, 1], [1, 2]),
        ([0, 1], []),
        ([0, 0], [1, 2]),
        ([0], [2]),
        ([0, 1], [3]),
        ([True, 1], [2]),
        ([0.0, 1], [2]),
    ],
)
def test_group_leakage_or_invalid_partition_fails(labeled, train, test):
    with pytest.raises(InputContractError):
        validate_card_split(labeled, train_indices=train, test_indices=test)


@pytest.mark.parametrize(
    "change", ["row_index", "metadata", "group", "label", "features", "version"]
)
def test_split_validation_rejects_misaligned_or_conflicting_rows(labeled, change):
    if change == "row_index":
        labeled = replace(labeled, y=labeled.y.set_axis([3, 4, 5]))
    elif change == "metadata":
        labeled = replace(labeled, metadata=labeled.metadata.iloc[:2])
    elif change == "group":
        labeled.metadata.loc[0, "person_id"] = 9
    elif change == "label":
        labeled.y.iloc[0] = "CB_UNKNOWN"
    elif change == "features":
        labeled.X.loc[0, "speed"] += 1
    else:
        labeled.metadata.loc[0, "version"] = "other"
    with pytest.raises(InputContractError):
        validate_card_split(labeled, train_indices=[0, 1], test_indices=[2])


def test_group_rule_keeps_same_person_in_distinct_cards_as_separate_card_groups():
    a, b = complete(), complete(spid=124000001, season_id=124)
    rows = to_labeled_input([a, b], selection=selection(a, b))
    assert rows.metadata.person_id.nunique() == 1 and rows.card_groups.nunique() == 2
    validate_card_split(rows, train_indices=[0, 1], test_indices=[2, 3])


def test_labels_keep_observed_codes_without_fixed_fifteen_class_mapping():
    c = complete(positions=["RCB", "SW"])
    rows = to_labeled_input([c], selection=selection(c))
    assert rows.y.tolist() == ["RCB", "SW"]
    assert "CB" not in set(rows.y) and "ST" not in set(rows.y)
