"""합성 카드만으로 분할·실제 소형 XGBoost·올바른 집합 평가를 검증한다."""

import json

import numpy as np
import pytest
from test_model_input_contract import complete, selection

from FRS.core.position_recommend import train
from FRS.core.position_recommend.data_contract import (
    InputContractError,
    to_labeled_input,
)
from FRS.core.position_recommend.evaluation import evaluate_probabilities, row_coverage
from FRS.core.position_recommend.probabilities import probability_result
from FRS.diagnostics.model_input_report import VerifiedCards


@pytest.fixture
def verified():
    cards = tuple(
        complete(
            spid=123000001 + i,
            person_id=1 + i,
            positions=[("CF", "ST", "GK")[i % 3]],
            stats={k: v + i % 3 * 10 for k, v in complete().stats.items()},
        )
        for i in range(30)
    )
    return VerifiedCards(
        cards, selection(*cards), {"synthetic-batch": {"manifest_sha256": "a" * 64}}
    )


def test_group_split_is_deterministic_disjoint_and_uses_fixed_policy(verified):
    rows = to_labeled_input(verified.cards, selection=verified.selection)
    a, b = train.grouped_split(rows)
    c, d = train.grouped_split(rows)
    assert np.array_equal(a, c) and np.array_equal(b, d)
    assert len(a) == 21 and len(b) == 9
    details = train.split_report(rows, a, b)
    assert (
        details["overlapping_cards"] == 0
        and details["test_size"] == 0.3
        and details["seed"] == 123
    )
    with pytest.raises(InputContractError):
        train.grouped_split(rows, "name")


def test_real_synthetic_xgboost_training_bundle_replays_and_preserves_inputs(
    verified, tmp_path
):
    original = [c.model_dump() for c in verified.cards]
    result = train.train_verified(verified, tmp_path)
    assert result["cards"] == 30 and result["labeled_rows"] == 30
    assert result["classes"] == ["CF", "GK", "ST"]
    assert result["save_load_probabilities_identical"]
    assert result["metrics"]["registered_label_coverage"] == 1
    assert result["metrics"]["cards"] == 9
    assert result["network_requests_sent"] == 0 and not result["observations_modified"]
    assert (tmp_path / "bundle/manifest.json").is_file()
    assert [c.model_dump() for c in verified.cards] == original


def test_set_metrics_use_complete_tokens_and_distinguish_card_and_label_coverage():
    result = probability_result(
        spids=[123000001, 123000002],
        classes=["CF", "ST"],
        values=[[0.95, 0.05], [0.5, 0.5]],
        model_version="a" * 64,
    )
    metrics = evaluate_probabilities(
        result, {123000001: ("CF", "ST"), 123000002: ("ST",)}
    )
    assert metrics["registered_label_coverage"] == pytest.approx(2 / 3)
    assert metrics["card_any_label_coverage"] == 1
    assert metrics["card_all_labels_coverage"] == 0.5
    assert metrics["mean_prediction_set_size"] == 1.5
    assert metrics["per_position_recall"]["ST"] == dict(
        support_cards=2, hit_cards=1, recall=0.5
    )
    assert row_coverage([["ST"], ["CF"]], ["ST", "ST"]) == 0.5
    with pytest.raises(ValueError):
        row_coverage([["S", "T"]], ["ST"])
    with pytest.raises(ValueError):
        row_coverage(["ST"], ["ST"])
    with pytest.raises(ValueError):
        evaluate_probabilities(result, {123000001: "ST", 123000002: ("ST",)})


def test_untrained_truth_positions_have_zero_recall_without_class_expansion():
    result = probability_result(
        spids=[123000001], classes=["ST"], values=[[1.0]], model_version="a" * 64
    )
    metrics = evaluate_probabilities(result, {123000001: ("RF",)})
    assert metrics["per_position_recall"]["RF"]["recall"] == 0
    assert metrics["untrained_truth_positions"] == ["RF"]


def test_run_rejects_wrong_cohort_and_preserves_stopped_record(
    tmp_path, monkeypatch, verified
):
    monkeypatch.setattr(train, "load_verified_cards", lambda root, batch_ids: verified)
    with pytest.raises(InputContractError, match="1,104"):
        train.run_training(tmp_path, "stage-2-a-synthetic")
    state = json.loads(
        (tmp_path / "artifacts/models/stage-2-a-synthetic/run.json").read_text()
    )
    assert state["status"] == "stopped"
    with pytest.raises(FileExistsError):
        train.run_training(tmp_path, "stage-2-a-synthetic")


@pytest.mark.parametrize("run_id", ["../invalid", "stage-2-a-../invalid", "other"])
def test_invalid_run_id_is_rejected_before_io(tmp_path, run_id):
    with pytest.raises(InputContractError):
        train.run_training(tmp_path, run_id)
    assert not (tmp_path / "artifacts").exists()


def test_default_cli_only_prints_plan(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["train"])

    def forbidden(*args, **kwargs):
        pytest.fail("기본 명령에서 학습하면 안 됩니다.")

    monkeypatch.setattr(train, "run_training", forbidden)
    train.main()
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "plan_only" and result["seed"] == 123
