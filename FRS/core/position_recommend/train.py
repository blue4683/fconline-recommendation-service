"""확정한 1-B/C·70/30·시드 123으로 기본 5강 분류기를 수동 학습한다."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import numpy as np
from sklearn.model_selection import GroupShuffleSplit
from xgboost import XGBClassifier

from FRS.core.position_recommend.artifacts import (
    PositionPredictor,
    load_bundle,
    save_bundle,
)
from FRS.core.position_recommend.data_contract import (
    InputContractError,
    InputSelection,
    to_labeled_input,
)
from FRS.core.position_recommend.evaluation import evaluate_probabilities
from FRS.core.position_recommend.model_contract import (
    TrainingContext,
    digest,
    fit_training_contract,
)
from FRS.diagnostics.model_input_report import (
    COLLECTION_ID,
    PILOT_ID,
    load_verified_cards,
)
from FRS.utils.crawling.pilot import ROOT, now, write_json

BATCHES = (PILOT_ID, COLLECTION_ID)
TEST_SIZE = 0.3
SPLIT_SEED = 123
MODEL_PARAMS = dict(
    objective="multi:softprob",
    n_estimators=50,
    max_depth=5,
    gamma=0,
    importance_type="gain",
    reg_lambda=1,
    random_state=100,
    device="cpu",
    n_jobs=1,
)


def baseline_model():
    return XGBClassifier(**MODEL_PARAMS)


def validate_run_id(value):
    if (
        not isinstance(value, str)
        or len(value) > 80
        or re.fullmatch(r"stage-2-a-[a-z0-9]+(?:-[a-z0-9]+)*", value) is None
    ):
        raise InputContractError("학습 실행 ID가 유효하지 않습니다.")


def grouped_split(rows, key="spid"):
    if key not in {"spid", "person_id"}:
        raise InputContractError("지원하지 않는 그룹 분할입니다.")
    return tuple(
        next(
            GroupShuffleSplit(
                n_splits=1, test_size=TEST_SIZE, random_state=SPLIT_SEED
            ).split(rows.X, groups=rows.metadata[key])
        )
    )


def training_context(verified):
    return TrainingContext(
        selection=verified.selection,
        source_manifest_hashes={
            batch: values["manifest_sha256"]
            for batch, values in verified.source_hashes.items()
        },
        input_cards_sha256=digest([c.model_dump(mode="json") for c in verified.cards]),
        source_status_counts=dict(Counter(c.status.value for c in verified.cards)),
    )


def split_report(rows, train, test):
    def values(indices, key):
        return set(rows.metadata.iloc[list(indices)][key])

    return dict(
        algorithm="GroupShuffleSplit",
        group_key="spid",
        test_size=TEST_SIZE,
        seed=SPLIT_SEED,
        train_rows=len(train),
        test_rows=len(test),
        train_cards=len(values(train, "spid")),
        test_cards=len(values(test, "spid")),
        train_persons=len(values(train, "person_id")),
        test_persons=len(values(test, "person_id")),
        overlapping_cards=len(values(train, "spid") & values(test, "spid")),
        overlapping_persons=len(values(train, "person_id") & values(test, "person_id")),
        train_label_counts=dict(Counter(rows.y.iloc[list(train)])),
        test_label_counts=dict(Counter(rows.y.iloc[list(test)])),
    )


def train_verified(verified, output, *, model_factory=baseline_model):
    """원본 검증을 마친 관측만 받는다. 실행 정책/완료 검증은 외부 실행기가 담당한다."""
    output = Path(output)
    rows = to_labeled_input(verified.cards, selection=verified.selection)
    train, test = grouped_split(rows)
    context = training_context(verified)
    fitted = fit_training_contract(
        rows,
        context=context,
        train_indices=train,
        test_indices=test,
        split_seed=SPLIT_SEED,
    )
    train_X = fitted.preprocessor.transform(rows.X.iloc[train])
    # 미학습 체형은 모델 fit 전에 오류로 보고한다. 분할/범주를 자동 변경하지 않는다.
    fitted.preprocessor.transform(rows.X.iloc[test])
    model = model_factory()
    model.fit(train_X, fitted.encode_labels(rows.y.iloc[train]))
    manifest = save_bundle(output / "bundle", model=model, fitted=fitted)
    heldout = set(rows.metadata.iloc[test]["spid"])
    cards = tuple(c for c in verified.cards if c.spid in heldout)
    selected = InputSelection(
        observations=tuple(
            k for k in verified.selection.observations if k.spid in heldout
        ),
        base_reinforce_level=5,
    )
    ids = [c.spid for c in cards]
    original = PositionPredictor(model, fitted, manifest).predict_proba(
        ids, cards=cards, selection=selected
    )
    loaded = load_bundle(
        output / "bundle", expected_model_version=manifest.model_version
    )
    replay = loaded.predict_proba(ids, cards=cards, selection=selected)
    if not np.array_equal(
        np.array([list(p.values()) for p in original.probabilities.values()]),
        np.array([list(p.values()) for p in replay.probabilities.values()]),
    ):
        raise InputContractError("저장/로드 전후 모델 확률이 다릅니다.")
    truth = {c.spid: tuple(p.value for p in c.positions) for c in cards}
    report = dict(
        stage="2-A",
        model_version=manifest.model_version,
        model_parameters=MODEL_PARAMS,
        cards=len(verified.cards),
        labeled_rows=len(rows.X),
        classes=list(fitted.classes),
        source_hashes=verified.source_hashes,
        input_cards_sha256=context.input_cards_sha256,
        source_status_counts=context.model_dump(mode="json")["source_status_counts"],
        split=split_report(rows, train, test),
        metrics=evaluate_probabilities(replay, truth),
        save_load_probabilities_identical=True,
        network_requests_sent=0,
        observations_modified=False,
    )
    write_json(output / "validation_truth.json", truth)
    write_json(output / "validation_probabilities.json", replay.model_dump(mode="json"))
    write_json(output / "report.json", report)
    return report


def run_training(root, run_id):
    validate_run_id(run_id)
    output = Path(root) / "artifacts/models" / run_id
    output.mkdir(parents=True, exist_ok=False)
    state = dict(
        run_id=run_id,
        status="verifying",
        started_at=now().isoformat(),
        batches=list(BATCHES),
        test_size=TEST_SIZE,
        seed=SPLIT_SEED,
        model_parameters=MODEL_PARAMS,
    )
    write_json(output / "run.json", state)
    try:
        verified = load_verified_cards(root, batch_ids=BATCHES)
        if len(verified.cards) != 1104 or verified.selection.base_reinforce_level != 5:
            raise InputContractError("승인한 1,104개 기본 5강 입력이 아닙니다.")
        state["status"] = "training"
        write_json(output / "run.json", state)
        report = train_verified(verified, output)
        state.update(
            status="completed",
            model_version=report["model_version"],
            output_hashes={
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in output.glob("*.json")
                if p.name != "run.json"
            },
        )
        return report
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
                    batches=BATCHES,
                    test_size=TEST_SIZE,
                    seed=SPLIT_SEED,
                    model_parameters=MODEL_PARAMS,
                    network_requests_sent=0,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not args.run_id:
        parser.error("--run-id를 명시해야 합니다.")
    try:
        result = run_training(ROOT, args.run_id)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
