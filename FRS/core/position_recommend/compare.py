"""승인된 과거 관측으로 분할/전처리 비교를 수동 실행한다. 기본은 계획 출력이다."""

import argparse
import hashlib
import json
import re
from pathlib import Path

import joblib
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder

from FRS.core.position_recommend.artifacts import (
    load_bundle,
    runtime_versions,
    save_bundle,
)
from FRS.core.position_recommend.data_contract import (
    InputContractError,
    to_labeled_input,
    to_model_input,
)
from FRS.core.position_recommend.evaluation import evaluate_probabilities, row_coverage
from FRS.core.position_recommend.model_contract import (
    CommonPreprocessor,
    digest,
    fit_training_contract,
)
from FRS.core.position_recommend.probabilities import (
    probability_result,
    threshold_positions,
)
from FRS.core.position_recommend.train import (
    BATCHES,
    MODEL_PARAMS,
    SPLIT_SEED,
    TEST_SIZE,
    baseline_model,
    grouped_split,
    split_report,
    training_context,
)
from FRS.diagnostics.model_input_report import load_verified_cards
from FRS.utils.crawling.pilot import ROOT, now, write_json

# 전체 fit은 누수 영향을 비교하는 진단 전용이다. 운영 번들로 저장하지 않는다.
CASES = (
    ("legacy_row_global", None, "all"),
    ("card_global", "spid", "all"),
    ("card_train", "spid", "train"),
    ("person_global", "person_id", "all"),
    ("person_train", "person_id", "train"),
)


def validate_run_id(value):
    if (
        not isinstance(value, str)
        or len(value) > 80
        or re.fullmatch(r"stage-2-b-[a-z0-9]+(?:-[a-z0-9]+)*", value) is None
    ):
        raise InputContractError("평가 실행 ID가 유효하지 않습니다.")


def _comparison_artifact(output, model, preprocessor, encoder, record):
    """누수 재현 산출물은 운영 매니페스트가 없는 별도 형식으로 보관한다."""
    folder = output / "comparison_only"
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / "experiment.joblib"
    joblib.dump((model, preprocessor, encoder), path)
    metadata = dict(
        comparison_only=True,
        deployable=False,
        record=record,
        preprocessing=preprocessor.specification(),
        classes=list(encoder.classes_),
        model_parameters=MODEL_PARAMS,
        runtime_versions=runtime_versions(),
        files={path.name: hashlib.sha256(path.read_bytes()).hexdigest()},
    )
    version = digest(metadata)
    metadata["experiment_version"] = version
    write_json(folder / "experiment.json", metadata)
    return version, joblib.load(path)


def compare_verified(verified, output, *, model_factory=baseline_model):
    """완료/원본 검증은 실행기가 담당한다. 분할/모델 설정을 자동 탐색하지 않는다."""
    output = Path(output)
    rows = to_labeled_input(verified.cards, selection=verified.selection)
    context = training_context(verified)
    all_X = to_model_input(verified.cards, selection=verified.selection)
    results = {}
    for name, group_key, fit_scope in CASES:
        folder = output / name
        folder.mkdir(parents=True, exist_ok=False)
        if group_key is None:
            train, test = train_test_split(
                np.arange(len(rows.X)), test_size=TEST_SIZE, random_state=SPLIT_SEED
            )
        else:
            train, test = grouped_split(rows, group_key)
        train, test = tuple(map(int, train)), tuple(map(int, test))
        if fit_scope == "all":
            preprocessor = CommonPreprocessor()._fit(rows.X)
            encoder = LabelEncoder().fit(rows.y)
            fitted = None
        else:
            fitted = fit_training_contract(
                rows,
                context=context,
                train_indices=train,
                test_indices=test,
                split_seed=SPLIT_SEED,
                group_key=group_key,
            )
            preprocessor, encoder = fitted.preprocessor, fitted.label_encoder
        encoded = encoder.transform(rows.y.iloc[list(train)])
        if set(encoded) != set(range(len(encoder.classes_))):
            raise InputContractError("학습 클래스가 비교 인코더와 다릅니다.")
        heldout = set(rows.metadata.iloc[list(test)].spid)
        ids = [int(spid) for spid in all_X.index if spid in heldout]
        validation_X = preprocessor.transform(all_X.loc[ids])
        model = model_factory()
        model.fit(preprocessor.transform(rows.X.iloc[list(train)]), encoded)
        values = model.predict_proba(validation_X)
        split = split_report(rows, train, test, group_key=group_key)
        if fitted is None:
            record = dict(
                context=context.model_dump(mode="json"),
                split=split,
                train_indices=train,
                test_indices=test,
                preprocessing_fit_indices=list(range(len(rows.X))),
                row_spids=list(map(int, rows.metadata.spid)),
                row_labels=list(rows.y),
                labeled_data_sha256=digest(
                    dict(X=rows.X.to_dict(orient="list"), y=list(rows.y))
                ),
            )
            version, (loaded_model, loaded_preprocessor, loaded_encoder) = (
                _comparison_artifact(folder, model, preprocessor, encoder, record)
            )
        else:
            manifest = save_bundle(folder / "bundle", model=model, fitted=fitted)
            loaded = load_bundle(
                folder / "bundle", expected_model_version=manifest.model_version
            )
            version = manifest.model_version
            loaded_model, loaded_preprocessor, loaded_encoder = (
                loaded._model,
                loaded._fitted.preprocessor,
                loaded._fitted.label_encoder,
            )
        if not np.array_equal(
            encoder.classes_, loaded_encoder.classes_
        ) or not np.array_equal(
            values,
            loaded_model.predict_proba(loaded_preprocessor.transform(all_X.loc[ids])),
        ):
            raise InputContractError("평가 산출물 저장/로드 후 확률이 다릅니다.")
        probabilities = probability_result(
            spids=ids,
            classes=list(encoder.classes_),
            values=values,
            model_version=version,
        )
        truth = {
            c.spid: tuple(p.value for p in c.positions)
            for c in verified.cards
            if c.spid in heldout
        }
        predicted = threshold_positions(probabilities)
        report = dict(
            split=split,
            preprocessing_fit_scope=fit_scope,
            preprocessing_fit_rows=int(preprocessor.scaler.n_samples_seen_),
            comparison_only=fit_scope == "all",
            artifact_version=version,
            classes=list(encoder.classes_),
            heldout_row_label_coverage=row_coverage(
                [predicted[rows.metadata.iloc[i].spid] for i in test],
                rows.y.iloc[list(test)],
            ),
            metrics=evaluate_probabilities(probabilities, truth),
            save_load_probabilities_identical=True,
        )
        write_json(folder / "validation_truth.json", truth)
        write_json(
            folder / "validation_probabilities.json",
            probabilities.model_dump(mode="json"),
        )
        write_json(folder / "report.json", report)
        results[name] = report
    summary = dict(
        stage="2-B",
        cards=len(verified.cards),
        labeled_rows=len(rows.X),
        input_cards_sha256=context.input_cards_sha256,
        source_hashes=verified.source_hashes,
        model_parameters=MODEL_PARAMS,
        network_requests_sent=0,
        observations_modified=False,
        cases=results,
    )
    write_json(output / "report.json", summary)
    return summary


def run_comparison(root, run_id):
    validate_run_id(run_id)
    output = Path(root) / "artifacts/models" / run_id
    output.mkdir(parents=True, exist_ok=False)
    state = dict(
        run_id=run_id,
        status="verifying",
        started_at=now().isoformat(),
        batches=list(BATCHES),
    )
    write_json(output / "run.json", state)
    try:
        verified = load_verified_cards(root, batch_ids=BATCHES)
        if len(verified.cards) != 1104 or verified.selection.base_reinforce_level != 5:
            raise InputContractError("승인한 1,104개 기본 5강 입력이 아닙니다.")
        state["status"] = "comparing"
        write_json(output / "run.json", state)
        result = compare_verified(verified, output)
        state.update(
            status="completed",
            output_hashes={
                str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(output.rglob("*"))
                if p.is_file() and p != output / "run.json"
            },
        )
        return result
    except BaseException as error:
        state.update(
            status="stopped",
            failure=dict(type=type(error).__name__, message=str(error)),
        )
        raise
    finally:
        state["finished_at"] = now().isoformat()
        write_json(output / "run.json", state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if not args.run:
        print(
            json.dumps(
                dict(
                    mode="plan_only",
                    stage="2-B",
                    batches=BATCHES,
                    cases=CASES,
                    test_size=TEST_SIZE,
                    seed=SPLIT_SEED,
                ),
                ensure_ascii=False,
            )
        )
    elif not args.run_id:
        parser.error("실행에는 --run-id가 필요합니다.")
    else:
        print(json.dumps(run_comparison(ROOT, args.run_id), ensure_ascii=False))


if __name__ == "__main__":
    main()
