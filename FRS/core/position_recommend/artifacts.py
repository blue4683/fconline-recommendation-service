"""명시한 버전의 로컬 산출물만 읽는다. 모델 부재 시 학습하지 않는다."""

import hashlib
import importlib.metadata
import io
import json
import math
import platform
from pathlib import Path
from typing import Literal

import joblib
import numpy as np
from pydantic import BaseModel, ConfigDict, model_validator

from FRS.core.position_recommend.data_contract import (
    FEATURE_NAMES,
    InputContractError,
    InputSelection,
    feature_spec,
    to_model_input,
)
from FRS.core.position_recommend.model_contract import (
    SHA256,
    FittedContract,
    canonical,
    digest,
)
from FRS.core.position_recommend.probabilities import probability_result
from FRS.schema.models import Position, Text

ARTIFACT_FILES = {"model.joblib", "contract.joblib"}
LIBRARIES = (
    "numpy",
    "pandas",
    "scikit-learn",
    "scipy",
    "joblib",
    "xgboost",
    "pydantic",
)


def runtime_versions():
    return {
        "python": platform.python_version(),
        **{name: importlib.metadata.version(name) for name in LIBRARIES},
    }


def _parameters(value):
    # XGBoost 기본 missing=NaN도 비표준 JSON 대신 명시한 표기로 보존한다.
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return {
            "__nonfinite_float__": "nan"
            if math.isnan(value)
            else ("inf" if value > 0 else "-inf")
        }
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise InputContractError("모델 설정의 키는 문자열이어야 합니다.")
        return {k: _parameters(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_parameters(v) for v in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise InputContractError("JSON으로 기록할 수 없는 모델 설정입니다.")


def _model_spec(model, classes):
    if not callable(getattr(model, "predict_proba", None)) or not callable(
        getattr(model, "get_params", None)
    ):
        raise InputContractError("모델의 확률/설정 인터페이스가 없습니다.")
    encoded = np.asarray(getattr(model, "classes_", []))
    if encoded.dtype.kind not in "iu" or not np.array_equal(
        encoded, np.arange(len(classes))
    ):
        raise InputContractError("모델과 포지션 인코더의 클래스 순서가 다릅니다.")
    if getattr(model, "n_features_in_", None) != len(FEATURE_NAMES):
        raise InputContractError("모델의 입력 피처 수가 다릅니다.")
    if (
        hasattr(model, "feature_names_in_")
        and tuple(model.feature_names_in_) != FEATURE_NAMES
    ):
        raise InputContractError("모델의 입력 열 순서가 다릅니다.")
    return dict(
        type=f"{type(model).__module__}.{type(model).__qualname__}",
        parameters=_parameters(model.get_params(deep=False)),
    )


class ArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal[1]
    feature_specification: dict
    fitted_contract: dict
    classes: tuple[Position, ...]
    model: dict
    runtime: dict[Text, Text]
    files: dict[Text, SHA256]
    model_version: SHA256

    @model_validator(mode="after")
    def consistent(self):
        if set(self.files) != ARTIFACT_FILES:
            raise ValueError("산출물 파일 목록이 다릅니다.")
        if (
            not self.classes
            or len(set(self.classes)) != len(self.classes)
            or self.fitted_contract.get("classes") != [c.value for c in self.classes]
        ):
            raise ValueError("매니페스트의 클래스가 다릅니다.")
        if (
            digest(self.model_dump(mode="json", exclude={"model_version"}))
            != self.model_version
        ):
            raise ValueError("model_version과 매니페스트 해시가 다릅니다.")
        return self


def _manifest(payload):
    return ArtifactManifest.model_validate(
        {**payload, "model_version": digest(payload)}
    )


class PositionPredictor:
    def __init__(self, model, fitted, manifest):
        self._model, self._fitted, self.manifest = model, fitted, manifest
        self._model_version = manifest.model_version
        self._check()

    def _check(self):
        manifest = ArtifactManifest.model_validate(
            self.manifest.model_dump(mode="json")
        )
        if (
            manifest.model_version != self._model_version
            or manifest.feature_specification != feature_spec()
            or manifest.runtime != runtime_versions()
            or type(self._fitted) is not FittedContract
            or self._fitted.specification() != manifest.fitted_contract
            or _model_spec(self._model, self._fitted.classes) != manifest.model
        ):
            raise InputContractError("로드한 모델·변환·스펙·실행 환경이 다릅니다.")

    def predict_proba(self, spids, *, cards, selection: InputSelection):
        self._check()
        if not isinstance(selection, InputSelection):
            raise InputContractError("InputSelection으로 관측을 명시해야 합니다.")
        ids = tuple(spids)
        if (
            not ids
            or len(set(ids)) != len(ids)
            or any(type(i) is not int or not 1_000_000 <= i <= 999_999_999 for i in ids)
            or set(ids) != {key.spid for key in selection.observations}
        ):
            raise InputContractError("요청한 카드와 명시 관측이 다르거나 중복됐습니다.")
        base = self._fitted.record["context"]["selection"]["base_reinforce_level"]
        if selection.base_reinforce_level != base:
            raise InputContractError("모델의 기준 강화와 입력이 다릅니다.")
        X = to_model_input(cards, selection=selection).loc[list(ids)]
        transformed = self._fitted.preprocessor.transform(X)
        values = self._model.predict_proba(transformed)
        return probability_result(
            spids=ids,
            classes=self._fitted.classes,
            values=values,
            model_version=self._model_version,
        )


def save_bundle(directory, *, model, fitted: FittedContract):
    if type(fitted) is not FittedContract:
        raise InputContractError("검증된 학습 계약이 필요합니다.")
    description = fitted.specification()
    model_spec = _model_spec(model, fitted.classes)
    # 설정을 먼저 직렬화한다. 지원하지 않는 값이면 디렉터리를 만들지 않는다.
    canonical(model_spec)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    joblib.dump(model, directory / "model.joblib", compress=0)
    joblib.dump(fitted, directory / "contract.joblib", compress=0)
    manifest = _manifest(
        dict(
            contract_version=1,
            feature_specification=feature_spec(),
            fitted_contract=description,
            classes=list(fitted.classes),
            model=model_spec,
            runtime=runtime_versions(),
            files={
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in sorted(ARTIFACT_FILES)
            },
        )
    )
    temporary = directory / "manifest.json.tmp"
    temporary.write_bytes(canonical(manifest.model_dump(mode="json")))
    temporary.replace(directory / "manifest.json")
    return manifest


def _strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InputContractError("JSON 키가 중복됐습니다.")
            result[key] = value
        return result

    def constant(value):
        raise InputContractError("JSON에 비표준 상수가 있습니다.")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def load_bundle(directory, *, expected_model_version):
    directory = Path(directory)
    if not directory.is_dir():
        raise InputContractError("모델 산출물 디렉터리가 없습니다.")
    paths = list(directory.iterdir())
    if {p.name for p in paths} != ARTIFACT_FILES | {"manifest.json"} or any(
        p.is_symlink() or not p.is_file() for p in paths
    ):
        raise InputContractError(
            "모델 파일이 미완료이거나 추가 파일·심볼릭 링크가 있습니다."
        )
    manifest = ArtifactManifest.model_validate(
        _strict_json((directory / "manifest.json").read_bytes())
    )
    if manifest.model_version != expected_model_version:
        raise InputContractError("명시한 모델 버전과 산출물이 다릅니다.")
    if (
        manifest.feature_specification != feature_spec()
        or manifest.runtime != runtime_versions()
    ):
        raise InputContractError("피처 스펙 또는 라이브러리 버전이 다릅니다.")
    payloads = {name: (directory / name).read_bytes() for name in ARTIFACT_FILES}
    if any(
        hashlib.sha256(body).hexdigest() != manifest.files[name]
        for name, body in payloads.items()
    ):
        raise InputContractError("산출물 파일의 SHA256이 다릅니다.")
    # 해시를 검증한 동일 bytes를 로드한다. 호출자가 작성한 로컬 번들만 다룬다.
    model = joblib.load(io.BytesIO(payloads["model.joblib"]))
    fitted = joblib.load(io.BytesIO(payloads["contract.joblib"]))
    return PositionPredictor(model, fitted, manifest)
