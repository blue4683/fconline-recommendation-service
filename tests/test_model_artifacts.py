"""합성 전처리·확률 대역으로 공통 변환과 산출물 무결성을 검증한다."""

import copy
import hashlib
import json
from dataclasses import replace

import numpy as np
import pytest
from test_model_input_contract import complete, selection

from FRS.core.position_recommend import artifacts
from FRS.core.position_recommend.data_contract import (
    FEATURE_NAMES,
    InputContractError,
    to_labeled_input,
    to_model_input,
)
from FRS.core.position_recommend.model_contract import (
    CommonPreprocessor,
    TrainingContext,
    canonical,
    digest,
    fit_training_contract,
)
from FRS.core.position_recommend.probabilities import (
    probability_result,
    threshold_positions,
)
from FRS.schema.models import DataStatus


class ProbabilityModel:
    """학습하지 않는 테스트 대역. 실제 분류기 정확도를 나타내지 않는다."""

    def __init__(self):
        self.classes_ = np.array([0, 1])
        self.n_features_in_ = 40
        self.feature_names_in_ = np.array(FEATURE_NAMES)
        self.values = None
        self.last_input = None

    def get_params(self, deep=False):
        return {"probability": 0.25, "missing": float("nan")}

    def predict_proba(self, X):
        self.last_input = X.copy()
        return (
            np.tile([0.25, 0.75], (len(X), 1)) if self.values is None else self.values
        )

    def fit(self, *args, **kwargs):
        raise AssertionError("모델 학습을 호출하면 안 됩니다.")


@pytest.fixture
def training():
    cards = [
        complete(stats={**complete().stats, "speed": 100}),
        complete(
            spid=123000002,
            person_id=2,
            positions=["ST"],
            stats={**complete().stats, "speed": 200},
        ),
        complete(
            spid=123000003,
            person_id=3,
            positions=["GK"],
            stats={**complete().stats, "speed": 900000},
        ),
    ]
    selected = selection(*cards)
    rows = to_labeled_input(cards, selection=selected)
    context = TrainingContext(
        selection=selected,
        source_manifest_hashes={
            "synthetic-batch": hashlib.sha256(b"synthetic-source").hexdigest()
        },
        input_cards_sha256=digest([c.model_dump(mode="json") for c in cards]),
        source_status_counts={"ok": 3},
    )
    return cards, rows, context


def fitted_for(training):
    _, rows, context = training
    return fit_training_contract(
        rows,
        context=context,
        train_indices=[0, 1, 2],
        test_indices=[3],
        split_seed=None,
    )


@pytest.fixture
def bundle(training, tmp_path):
    fitted = fitted_for(training)
    model = ProbabilityModel()
    path = tmp_path / "bundle"
    manifest = artifacts.save_bundle(path, model=model, fitted=fitted)
    return path, manifest, model, fitted


def test_fit_uses_only_train_rows_and_shared_transform_does_not_refit(training):
    cards, rows, _ = training
    original = rows.X.copy(deep=True)
    fitted = fitted_for(training)
    before = fitted.specification()
    spec = before["preprocessing"]
    assert spec["mean"][0] == pytest.approx((100 + 100 + 200) / 3)
    assert spec["samples_seen"] == 3
    assert fitted.classes == ("CF", "ST") and "GK" not in fitted.classes
    assert fitted.encode_labels(["ST", "CF"]).tolist() == [1, 0]
    transformed = fitted.preprocessor.transform(rows.X)
    assert transformed.shape == (4, 40) and tuple(transformed.columns) == FEATURE_NAMES
    assert all(transformed[name].dtype == "float64" for name in FEATURE_NAMES)
    assert transformed.iloc[3, 0] > 1000
    assert rows.X.equals(original) and fitted.specification() == before
    raw = to_model_input(cards, selection=selection(*cards))
    assert fitted.preprocessor.transform(raw).iloc[0].equals(transformed.iloc[0])
    with pytest.raises(InputContractError, match="라벨"):
        fitted.encode_labels(["GK"])


def test_unknown_physical_is_rejected_without_extending_encoder(training):
    fitted = fitted_for(training)
    c = complete(physical="새가상체형")
    X = to_model_input([c], selection=selection(c))
    before = fitted.specification()
    with pytest.raises(InputContractError, match="입력을 수정"):
        fitted.preprocessor.transform(X)
    assert fitted.specification() == before
    with pytest.raises(InputContractError, match="전처리기"):
        CommonPreprocessor().transform(X)


@pytest.mark.parametrize(
    "change", ["leak", "context", "seed", "source_hash", "status_count"]
)
def test_fit_rejects_bad_partition_or_provenance(training, change):
    _, rows, context = training
    train, test, seed = [0, 1, 2], [3], None
    if change == "leak":
        train, test = [0, 2], [1, 3]
    elif change == "context":
        rows.metadata.loc[2, "batch_id"] = "different"
    elif change == "seed":
        seed = True
    elif change == "source_hash":
        context = context.model_copy(
            update={"source_manifest_hashes": {"other": "a" * 64}}
        )
    else:
        context = context.model_copy(
            update={"source_status_counts": {DataStatus.OK: 2}}
        )
    with pytest.raises(ValueError):
        fit_training_contract(
            rows,
            context=context,
            train_indices=train,
            test_indices=test,
            split_seed=seed,
        )


def test_saved_loaded_transform_and_probability_mapping_match(bundle, training):
    path, manifest, model, fitted = bundle
    predictor = artifacts.load_bundle(
        path, expected_model_version=manifest.model_version
    )
    cards, _, context = training
    before = [c.model_dump() for c in cards]
    ids = [c.spid for c in reversed(cards)]
    result = predictor.predict_proba(ids, cards=cards, selection=context.selection)
    assert list(result.probabilities) == ids
    assert (
        result.classes == ("CF", "ST")
        and result.model_version == manifest.model_version
    )
    assert all(row == {"CF": 0.25, "ST": 0.75} for row in result.probabilities.values())
    assert set(threshold_positions(result)[ids[0]]) == {"CF", "ST"}
    assert threshold_positions(result, 0.5)[ids[0]] == ("ST",)
    assert [c.model_dump() for c in cards] == before
    raw = to_model_input(cards, selection=context.selection)
    expected = fitted.preprocessor.transform(raw.loc[ids])
    assert predictor._model.last_input.equals(expected)
    assert model.last_input is None
    assert manifest.model["parameters"]["missing"] == {"__nonfinite_float__": "nan"}


def test_bundle_version_is_deterministic_and_bound_to_files_and_training(
    bundle, tmp_path, training
):
    path, manifest, model, fitted = bundle
    other = artifacts.save_bundle(tmp_path / "identical", model=model, fitted=fitted)
    assert (
        other.model_version == manifest.model_version and other.files == manifest.files
    )
    modified = copy.deepcopy(fitted)
    modified.record["context"]["input_cards_sha256"] = "b" * 64
    changed = artifacts.save_bundle(tmp_path / "changed", model=model, fitted=modified)
    assert changed.model_version != manifest.model_version
    with pytest.raises(FileExistsError):
        artifacts.save_bundle(path, model=model, fitted=fitted)


@pytest.mark.parametrize(
    "change", ["classes", "feature_count", "column_order", "unfitted"]
)
def test_model_contract_is_checked_before_writing(tmp_path, training, change):
    model = ProbabilityModel()
    if change == "classes":
        model.classes_ = np.array([1, 0])
    elif change == "feature_count":
        model.n_features_in_ = 41
    elif change == "column_order":
        model.feature_names_in_ = model.feature_names_in_[::-1]
    else:
        del model.classes_
    with pytest.raises(InputContractError):
        artifacts.save_bundle(
            tmp_path / "invalid", model=model, fitted=fitted_for(training)
        )
    assert not (tmp_path / "invalid").exists()


def rewrite_manifest(path, change):
    value = json.loads((path / "manifest.json").read_text())
    change(value)
    value["model_version"] = digest(
        {k: v for k, v in value.items() if k != "model_version"}
    )
    (path / "manifest.json").write_bytes(canonical(value))
    return value["model_version"]


@pytest.mark.parametrize(
    "change",
    [
        "file",
        "version",
        "runtime",
        "feature",
        "extra_file",
        "symlink",
        "incomplete",
        "duplicate_json",
    ],
)
def test_invalid_bundle_fails_before_deserialization(bundle, monkeypatch, change):
    path, manifest, _, _ = bundle
    expected = manifest.model_version
    if change == "file":
        (path / "model.joblib").write_bytes(b"tampered")
    elif change == "version":
        expected = "f" * 64
    elif change == "runtime":
        expected = rewrite_manifest(path, lambda v: v["runtime"].update(python="0.0.0"))
    elif change == "feature":
        expected = rewrite_manifest(
            path, lambda v: v["feature_specification"].update(version=999)
        )
    elif change == "extra_file":
        (path / "extra.joblib").write_bytes(b"extra")
    elif change == "symlink":
        source = path / "model.joblib"
        target = path.parent / "outside.joblib"
        source.replace(target)
        source.symlink_to(target)
    elif change == "incomplete":
        (path / "manifest.json").unlink()
    else:
        text = (path / "manifest.json").read_text()
        (path / "manifest.json").write_text('{"contract_version":1,' + text[1:])

    def forbidden(*args, **kwargs):
        pytest.fail("검증 실패 후 역직렬화하면 안 됩니다.")

    monkeypatch.setattr(artifacts.joblib, "load", forbidden)
    with pytest.raises(ValueError):
        artifacts.load_bundle(path, expected_model_version=expected)


def test_missing_model_never_triggers_training(tmp_path):
    with pytest.raises(InputContractError, match="없습니다"):
        artifacts.load_bundle(tmp_path / "missing", expected_model_version="a" * 64)


def test_rehashed_fitted_mapping_tamper_is_rejected_after_loading(bundle):
    path, _, _, fitted = bundle
    tampered = copy.deepcopy(fitted)
    tampered.label_encoder.classes_ = np.array(["GK", "ST"])
    artifacts.joblib.dump(tampered, path / "contract.joblib", compress=0)
    checksum = hashlib.sha256((path / "contract.joblib").read_bytes()).hexdigest()
    expected = rewrite_manifest(
        path, lambda v: v["files"].update({"contract.joblib": checksum})
    )
    with pytest.raises(InputContractError):
        artifacts.load_bundle(path, expected_model_version=expected)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_physical",
        "grade",
        "duplicate_id",
        "missing_id",
        "columns",
        "changed_state",
    ],
)
def test_inference_contract_errors_do_not_call_model(bundle, training, change):
    path, manifest, _, _ = bundle
    predictor = artifacts.load_bundle(
        path, expected_model_version=manifest.model_version
    )
    cards, _, context = training
    ids = [c.spid for c in cards]
    selected = context.selection
    if change == "unknown_physical":
        cards = [cards[0].model_copy(update={"physical": "새가상체형"}), *cards[1:]]
    elif change == "grade":
        selected = selected.model_copy(update={"base_reinforce_level": 6})
    elif change == "duplicate_id":
        ids.append(ids[0])
    elif change == "missing_id":
        ids.pop()
    elif change == "columns":
        predictor._model.feature_names_in_ = np.array(list(reversed(FEATURE_NAMES)))
    else:
        predictor._fitted.preprocessor.scaler.mean_[0] += 1
    with pytest.raises(ValueError):
        predictor.predict_proba(ids, cards=cards, selection=selected)
    assert predictor._model.last_input is None


@pytest.mark.parametrize(
    "values",
    [
        [[float("nan"), 1]],
        [[float("inf"), 0]],
        [[-0.1, 1.1]],
        [[0.1, 0.2]],
        [[0.2, 0.3, 0.5]],
        [0.25, 0.75],
        [[0.25, 0.75], [0.25, 0.75]],
        [[True, False]],
        [["0.25", "0.75"]],
    ],
)
def test_invalid_probability_matrices_are_not_normalized_or_clipped(values):
    with pytest.raises(ValueError):
        probability_result(
            spids=[123000001],
            classes=["CF", "ST"],
            values=values,
            model_version="a" * 64,
        )


@pytest.mark.parametrize("threshold", [True, -1, 2, float("nan"), "0.1"])
def test_threshold_is_explicit_finite_probability(threshold):
    result = probability_result(
        spids=[123000001],
        classes=["CF", "ST"],
        values=[[0.25, 0.75]],
        model_version="a" * 64,
    )
    with pytest.raises(InputContractError):
        threshold_positions(result, threshold)


def test_preprocessor_cannot_be_refit_in_place(training):
    fitted = fitted_for(training)
    before = fitted.specification()
    with pytest.raises(InputContractError, match="다시"):
        fitted.preprocessor._fit(training[1].X)
    assert fitted.specification() == before


def test_all_classes_and_zero_probabilities_are_preserved_without_expansion():
    result = probability_result(
        spids=[123000001],
        classes=["CF", "ST"],
        values=np.array([[0, 1]], dtype="float32"),
        model_version="a" * 64,
    )
    assert result.probabilities[123000001] == {"CF": 0.0, "ST": 1.0}
    assert "LS" not in result.probabilities[123000001]
    assert threshold_positions(result)[123000001] == ("ST",)
    single = probability_result(
        spids=[123000001], classes=["GK"], values=[[1.0]], model_version="a" * 64
    )
    assert single.classes == ("GK",)


def test_small_probability_roundoff_is_accepted_without_normalization():
    result = probability_result(
        spids=[123000001],
        classes=["CF", "ST"],
        values=[[0.25, 0.7499995]],
        model_version="a" * 64,
    )
    assert result.probabilities[123000001]["ST"] == 0.7499995


def test_partial_serialization_is_not_loadable_or_automatically_repaired(
    tmp_path, training, monkeypatch
):
    path = tmp_path / "partial"

    def interrupted(*args, **kwargs):
        raise OSError("합성 저장 오류")

    monkeypatch.setattr(artifacts.joblib, "dump", interrupted)
    with pytest.raises(OSError, match="저장"):
        artifacts.save_bundle(
            path, model=ProbabilityModel(), fitted=fitted_for(training)
        )
    assert path.exists() and not (path / "manifest.json").exists()
    with pytest.raises(InputContractError):
        artifacts.load_bundle(path, expected_model_version="a" * 64)


def test_current_xgboost_parameters_can_be_recorded_without_training():
    from xgboost import XGBClassifier

    model = XGBClassifier(n_estimators=50, max_depth=5, reg_lambda=1, random_state=100)
    parameters = artifacts._parameters(model.get_params(deep=False))
    assert parameters["missing"] == {"__nonfinite_float__": "nan"}
    assert json.loads(canonical(parameters))["n_estimators"] == 50


def test_training_context_cannot_change_the_original_reinforcement_baseline(training):
    _, rows, context = training
    payload = context.model_dump()
    payload["selection"]["base_reinforce_level"] = 6
    changed = TrainingContext.model_validate(payload)
    with pytest.raises(InputContractError, match="출처"):
        fit_training_contract(
            rows,
            context=changed,
            train_indices=[0, 1, 2],
            test_indices=[3],
            split_seed=None,
        )


def test_labeled_rows_keep_their_original_observation_selection(training):
    _, rows, context = training
    changed = rows.metadata.copy()
    changed.loc[changed.spid == 123000001, "version"] = "different"
    with pytest.raises(InputContractError, match="관측 선택"):
        fit_training_contract(
            replace(rows, metadata=changed),
            context=context,
            train_indices=[0, 1, 2],
            test_indices=[3],
            split_seed=None,
        )
