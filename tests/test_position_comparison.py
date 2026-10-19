"""합성 자료로 누수 비교의 격리·인물 분리·레거시 오류 수정을 검증한다."""

import json

import pytest
from pydantic import ValidationError
from test_model_input_contract import complete, selection

from FRS.core.position_recommend import compare, train
from FRS.core.position_recommend.artifacts import load_bundle
from FRS.core.position_recommend.data_contract import (
    InputContractError,
    to_labeled_input,
)
from FRS.core.position_recommend.model import Classifier
from FRS.core.position_recommend.model_contract import FitRecord, fit_training_contract
from FRS.diagnostics.model_input_report import VerifiedCards


@pytest.fixture
def verified():
    cards = tuple(
        complete(
            spid=season * 1_000_000 + person,
            season_id=season,
            person_id=person,
            positions=[("CF", "ST"), ("ST", "CF"), ("GK",)][person % 3],
            stats={k: v + person % 3 * 10 for k, v in complete().stats.items()},
        )
        for season in (123, 124)
        for person in range(1, 31)
    )
    return VerifiedCards(
        cards, selection(*cards), {"synthetic-batch": {"manifest_sha256": "a" * 64}}
    )


def test_person_contract_rejects_cross_season_leakage_in_fit_and_record(verified):
    rows = to_labeled_input(verified.cards, selection=verified.selection)
    context = train.training_context(verified)
    a, b = train.grouped_split(rows)
    assert train.split_report(rows, a, b)["overlapping_persons"] > 0
    with pytest.raises(InputContractError, match="인물"):
        fit_training_contract(
            rows,
            context=context,
            train_indices=a,
            test_indices=b,
            split_seed=123,
            group_key="person_id",
        )
    fitted = fit_training_contract(
        rows, context=context, train_indices=a, test_indices=b, split_seed=123
    )
    with pytest.raises(ValidationError, match="인물"):
        FitRecord.model_validate({**fitted.record, "group_key": "person_id"})
    a, b = train.grouped_split(rows, "person_id")
    fitted = fit_training_contract(
        rows,
        context=context,
        train_indices=a,
        test_indices=b,
        split_seed=123,
        group_key="person_id",
    )
    assert fitted.specification()["training"]["group_key"] == "person_id"


def test_five_case_comparison_isolates_leaky_artifacts_and_preserves_inputs(
    verified, tmp_path
):
    original = [c.model_dump() for c in verified.cards]
    result = compare.compare_verified(verified, tmp_path)
    cases = result["cases"]
    assert list(cases) == [c[0] for c in compare.CASES]
    assert cases["legacy_row_global"]["split"]["group_key"] is None
    assert cases["legacy_row_global"]["split"]["overlapping_cards"] > 0
    assert cases["card_train"]["split"]["overlapping_persons"] > 0
    for name, _, scope in compare.CASES:
        case = cases[name]
        assert case["save_load_probabilities_identical"]
        assert case["preprocessing_fit_rows"] == (
            result["labeled_rows"] if scope == "all" else case["split"]["train_rows"]
        )
        assert 0 <= case["heldout_row_label_coverage"] <= 1
        folder = tmp_path / name
        if scope == "all":
            metadata = json.loads(
                (folder / "comparison_only/experiment.json").read_text()
            )
            assert metadata["comparison_only"] and not metadata["deployable"]
            assert not (folder / "bundle").exists()
            with pytest.raises(InputContractError):
                load_bundle(
                    folder / "comparison_only",
                    expected_model_version=case["artifact_version"],
                )
        else:
            loaded = load_bundle(
                folder / "bundle", expected_model_version=case["artifact_version"]
            )
            assert loaded.manifest.fitted_contract["training"]["group_key"] == (
                "person_id" if name.startswith("person") else "spid"
            )
        if name.startswith("person"):
            assert case["split"]["overlapping_persons"] == 0
    assert [c.model_dump() for c in verified.cards] == original
    assert result["network_requests_sent"] == 0 and not result["observations_modified"]


def test_legacy_precision_uses_whole_codes_and_missing_model_never_trains(
    tmp_path, monkeypatch
):
    model = Classifier()
    assert model.precision([["ST"], ["CF"]], ["ST", "ST"]) == 50
    assert model.precision([["ST"], []], [["CF", "ST"], ["GK"]]) == 50
    with pytest.raises(ValueError):
        model.precision([["S", "T"]], ["ST"])
    with pytest.raises(InputContractError):
        model.train(tmp_path / "model.pickle")
    monkeypatch.setattr(model, "train", lambda *args: pytest.fail("자동 학습 금지"))
    with pytest.raises(FileNotFoundError):
        model.call(tmp_path / "model.pickle")
    assert list(tmp_path.iterdir()) == []


def test_comparison_run_rejects_wrong_cohort_and_does_not_restart(
    tmp_path, monkeypatch, verified
):
    monkeypatch.setattr(
        compare, "load_verified_cards", lambda *args, **kwargs: verified
    )
    with pytest.raises(InputContractError, match="1,104"):
        compare.run_comparison(tmp_path, "stage-2-b-synthetic")
    state = json.loads(
        (tmp_path / "artifacts/models/stage-2-b-synthetic/run.json").read_text()
    )
    assert state["status"] == "stopped"
    with pytest.raises(FileExistsError):
        compare.run_comparison(tmp_path, "stage-2-b-synthetic")


def test_train_only_comparison_rejects_unseen_body_without_changing_split(
    verified, tmp_path
):
    rows = to_labeled_input(verified.cards, selection=verified.selection)
    _, test = train.grouped_split(rows)
    heldout_id = rows.metadata.iloc[int(test[0])].spid
    cards = tuple(
        c.model_copy(update={"physical": "가상체형_B"}) if c.spid == heldout_id else c
        for c in verified.cards
    )
    changed = VerifiedCards(cards, verified.selection, verified.source_hashes)
    with pytest.raises(InputContractError, match="학습에 없던 체형"):
        compare.compare_verified(changed, tmp_path)
    assert not (tmp_path / "card_train/bundle").exists()
    assert not (tmp_path / "person_train").exists()


@pytest.mark.parametrize(
    "run_id", ["../invalid", "stage-2-b-../invalid", "stage-2-a-test"]
)
def test_invalid_comparison_id_rejected_before_io(tmp_path, run_id):
    with pytest.raises(InputContractError):
        compare.run_comparison(tmp_path, run_id)
    assert not (tmp_path / "artifacts").exists()


def test_default_cli_is_plan_only(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["compare"])
    monkeypatch.setattr(
        compare, "run_comparison", lambda *args: pytest.fail("기본 실행에서 학습 금지")
    )
    compare.main()
    assert json.loads(capsys.readouterr().out)["mode"] == "plan_only"
