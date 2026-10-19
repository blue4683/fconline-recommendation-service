"""공통 전처리의 학습 행 경계와 출처 계약. 분할 생성·모델 학습은 하지 않는다."""

import hashlib
import json
from typing import Annotated, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator
from sklearn.preprocessing import LabelEncoder, StandardScaler

from FRS.core.position_recommend.data_contract import (
    FEATURE_NAMES,
    InputContractError,
    InputSelection,
    LabeledInput,
    feature_spec,
    validate_card_split,
    validate_model_input,
)
from FRS.schema.models import CardId, DataStatus, NonNegative, Position, Positive, Text

SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NUMERIC_FEATURES = tuple(name for name in FEATURE_NAMES if name != "physical")


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class TrainingContext(BaseModel):
    """호출자가 원본 검증 후 제공하는 입력 출처. 추천 가능 승격을 뜻하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    selection: InputSelection
    source_manifest_hashes: dict[Text, SHA256]
    input_cards_sha256: SHA256
    source_status_counts: dict[DataStatus, NonNegative]

    @model_validator(mode="after")
    def matching_sources(self):
        batches = {key.batch_id for key in self.selection.observations}
        if set(self.source_manifest_hashes) != batches:
            raise ValueError("선택한 배치와 원본 manifest 해시가 다릅니다.")
        if sum(self.source_status_counts.values()) != len(self.selection.observations):
            raise ValueError("입력 카드의 상태 집계가 다릅니다.")
        return self


class FitRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    context: TrainingContext
    label_policy: Literal["registered_position_rows"]
    group_key: Literal["spid"]
    split_seed: NonNegative | None
    train_indices: tuple[NonNegative, ...]
    test_indices: tuple[NonNegative, ...]
    row_spids: tuple[CardId, ...]
    total_rows: Positive
    train_rows: Positive
    test_rows: Positive
    labeled_data_sha256: SHA256

    @model_validator(mode="after")
    def valid_partition(self):
        train, test = set(self.train_indices), set(self.test_indices)
        if (
            len(train) != self.train_rows
            or len(test) != self.test_rows
            or len(self.train_indices) != len(train)
            or len(self.test_indices) != len(test)
            or train & test
            or train | test != set(range(self.total_rows))
            or len(self.row_spids) != self.total_rows
            or set(self.row_spids)
            != {k.spid for k in self.context.selection.observations}
        ):
            raise ValueError("학습 분할 기록이 유효하지 않습니다.")
        if {self.row_spids[i] for i in train} & {self.row_spids[i] for i in test}:
            raise ValueError("학습 기록의 카드 그룹이 겹칩니다.")
        return self


class CommonPreprocessor:
    """동일 객체로 학습/추론 변환. 새 체형을 자동 학습하거나 대체하지 않는다."""

    def __init__(self):
        self.scaler = StandardScaler()
        self.physical_encoder = LabelEncoder()
        self.feature_spec_sha256 = feature_spec()["sha256"]

    def _fit(self, training_X):
        if hasattr(self.scaler, "mean_") or hasattr(self.physical_encoder, "classes_"):
            raise InputContractError("이미 fit한 전처리기를 다시 학습할 수 없습니다.")
        validate_model_input(training_X)
        self.scaler.fit(training_X.loc[:, NUMERIC_FEATURES])
        self.physical_encoder.fit(training_X["physical"].to_numpy(dtype=str))
        return self

    def specification(self):
        if (
            type(self.scaler) is not StandardScaler
            or type(self.physical_encoder) is not LabelEncoder
            or self.feature_spec_sha256 != feature_spec()["sha256"]
            or not hasattr(self.scaler, "scale_")
            or not hasattr(self.physical_encoder, "classes_")
        ):
            raise InputContractError("전처리기가 없거나 입력 스펙이 다릅니다.")
        if (
            tuple(self.scaler.feature_names_in_) != NUMERIC_FEATURES
            or self.scaler.n_features_in_ != len(NUMERIC_FEATURES)
            or not self.scaler.with_mean
            or not self.scaler.with_std
            or not self.scaler.copy
        ):
            raise InputContractError("숫자 전처리기의 열/설정이 다릅니다.")
        arrays = [
            np.asarray(getattr(self.scaler, name))
            for name in ("mean_", "var_", "scale_")
        ]
        if (
            any(a.shape != (39,) or not np.isfinite(a).all() for a in arrays)
            or (arrays[1] < 0).any()
            or (arrays[2] <= 0).any()
            or np.ndim(self.scaler.n_samples_seen_) != 0
            or not np.isfinite(self.scaler.n_samples_seen_)
            or int(self.scaler.n_samples_seen_) != self.scaler.n_samples_seen_
            or int(self.scaler.n_samples_seen_) <= 0
        ):
            raise InputContractError("숫자 전처리기의 학습 상태가 유효하지 않습니다.")
        categories = self.physical_encoder.classes_.tolist()
        if (
            not categories
            or any(
                not isinstance(v, str) or not v or v != v.strip() for v in categories
            )
            or categories != sorted(set(categories))
        ):
            raise InputContractError("체형 인코더의 범주가 유효하지 않습니다.")
        return dict(
            version=1,
            numeric_transform="StandardScaler",
            with_mean=True,
            with_std=True,
            physical_transform="LabelEncoder",
            unknown_physical="error",
            feature_spec_sha256=self.feature_spec_sha256,
            numeric_features=list(NUMERIC_FEATURES),
            output_features=list(FEATURE_NAMES),
            output_dtype="float64",
            mean=arrays[0].tolist(),
            variance=arrays[1].tolist(),
            scale=arrays[2].tolist(),
            samples_seen=int(self.scaler.n_samples_seen_),
            physical_classes=categories,
        )

    def transform(self, frame):
        validate_model_input(frame)
        self.specification()
        physical = frame["physical"].to_numpy(dtype=str)
        if not set(physical) <= set(self.physical_encoder.classes_):
            raise InputContractError("학습에 없던 체형입니다. 입력을 수정해야 합니다.")
        numeric = self.scaler.transform(frame.loc[:, NUMERIC_FEATURES])
        result = pd.DataFrame(numeric, columns=NUMERIC_FEATURES, index=frame.index)
        result["physical"] = self.physical_encoder.transform(physical).astype("float64")
        result = result.loc[:, FEATURE_NAMES].astype("float64")
        if not np.isfinite(result.to_numpy()).all():
            raise InputContractError("전처리 결과에 비유한 값이 있습니다.")
        return result


class FittedContract:
    def __init__(self, preprocessor, label_encoder, record):
        self.preprocessor = preprocessor
        self.label_encoder = label_encoder
        # 호출자의 dict를 공유하지 않는다. 로드 후에도 명세와 상태를 대조한다.
        self.record = json.loads(canonical(record))

    @property
    def classes(self):
        if type(self.label_encoder) is not LabelEncoder or not hasattr(
            self.label_encoder, "classes_"
        ):
            raise InputContractError("포지션 인코더가 없습니다.")
        values = self.label_encoder.classes_.tolist()
        if (
            not values
            or values != sorted(set(values))
            or not set(values) <= {p.value for p in Position}
        ):
            raise InputContractError("포지션 클래스 매핑이 유효하지 않습니다.")
        return tuple(values)

    def specification(self):
        if type(self.preprocessor) is not CommonPreprocessor:
            raise InputContractError("공통 전처리기의 종류가 다릅니다.")
        record = FitRecord.model_validate(self.record)
        preprocessing = self.preprocessor.specification()
        if preprocessing["samples_seen"] != record.train_rows:
            raise InputContractError("전처리 fit 행 수와 분할 기록이 다릅니다.")
        return dict(
            preprocessing=preprocessing,
            classes=list(self.classes),
            training=json.loads(canonical(self.record)),
        )

    def encode_labels(self, labels):
        values = list(labels)
        if not values or not set(values) <= set(self.classes):
            raise InputContractError("학습에 없거나 누락된 포지션 라벨입니다.")
        return self.label_encoder.transform(values)


def fit_training_contract(
    rows: LabeledInput,
    *,
    context: TrainingContext,
    train_indices,
    test_indices,
    split_seed: int | None,
):
    """지정 분할을 검증하고 train의 전처리/라벨만 fit한다. 모델은 다루지 않는다."""
    if not isinstance(context, TrainingContext):
        raise InputContractError("TrainingContext로 검증한 출처를 명시해야 합니다.")
    context = TrainingContext.model_validate(context.model_dump())
    train, test = tuple(train_indices), tuple(test_indices)
    validate_card_split(rows, train_indices=train, test_indices=test)
    expected = {(k.spid, k.batch_id, k.version) for k in context.selection.observations}
    actual = set(
        rows.metadata[["spid", "batch_id", "version"]].itertuples(
            index=False, name=None
        )
    )
    if actual != expected or rows.selection != context.selection:
        raise InputContractError("학습 행과 명시한 관측 출처가 다릅니다.")
    if split_seed is not None and (
        type(split_seed) is not int or not 0 <= split_seed <= 2**63 - 1
    ):
        raise InputContractError(
            "분할 시드는 비음수 정수 또는 명시한 None이어야 합니다."
        )
    preprocessor = CommonPreprocessor()._fit(rows.X.iloc[list(train)])
    label_encoder = LabelEncoder().fit(rows.y.iloc[list(train)].to_numpy(dtype=str))
    record = dict(
        context=context.model_dump(mode="json"),
        label_policy="registered_position_rows",
        group_key="spid",
        split_seed=split_seed,
        train_indices=list(map(int, train)),
        test_indices=list(map(int, test)),
        row_spids=rows.metadata["spid"].tolist(),
        total_rows=len(rows.X),
        train_rows=len(train),
        test_rows=len(test),
        labeled_data_sha256=digest(
            dict(
                X=rows.X.to_dict(orient="records"),
                y=rows.y.tolist(),
                metadata=rows.metadata.to_dict(orient="records"),
            )
        ),
    )
    fitted = FittedContract(preprocessor, label_encoder, record)
    fitted.specification()
    return fitted
