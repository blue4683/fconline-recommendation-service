"""전체 학습 클래스의 확률 계약. 임계값 목록은 별도 호환 함수다."""

from typing import Annotated

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from FRS.core.position_recommend.data_contract import InputContractError
from FRS.core.position_recommend.model_contract import SHA256
from FRS.schema.models import CardId, Position

SUM_TOLERANCE = 1e-6
Probability = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class ProbabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_version: SHA256
    classes: tuple[Position, ...]
    probabilities: dict[CardId, dict[Position, Probability]]

    @model_validator(mode="after")
    def validate_space(self):
        if (
            not self.classes
            or len(set(self.classes)) != len(self.classes)
            or not self.probabilities
        ):
            raise ValueError("클래스 공간이나 카드별 확률이 비어 있거나 중복됐습니다.")
        for row in self.probabilities.values():
            if tuple(row) != self.classes or not np.isclose(
                sum(row.values()), 1, rtol=0, atol=SUM_TOLERANCE
            ):
                raise ValueError("클래스 순서 또는 확률 합이 다릅니다.")
        return self


def probability_result(*, spids, classes, values, model_version):
    ids = tuple(spids)
    if (
        not ids
        or len(ids) != len(set(ids))
        or any(type(i) is not int or not 1_000_000 <= i <= 999_999_999 for i in ids)
    ):
        raise InputContractError(
            "확률 조회 카드 ID가 비어 있거나 중복/범위 오류입니다."
        )
    values = np.asarray(values)
    classes = tuple(classes)
    if (
        values.shape != (len(ids), len(classes))
        or values.dtype.kind not in "fiu"
        or not np.isfinite(values).all()
        or (values < 0).any()
        or (values > 1).any()
        or not np.allclose(values.sum(axis=1), 1, rtol=0, atol=SUM_TOLERANCE)
    ):
        raise InputContractError(
            "모델 확률의 행/열·유한성·범위·합이 유효하지 않습니다."
        )
    return ProbabilityResult(
        model_version=model_version,
        classes=classes,
        probabilities={
            spid: {p: float(v) for p, v in zip(classes, row, strict=True)}
            for spid, row in zip(ids, values, strict=True)
        },
    )


def threshold_positions(result: ProbabilityResult, threshold=0.1):
    """기존 0.1 이상 목록 호환. 성능 점수나 배치 허용 규칙이 아니다."""
    result = ProbabilityResult.model_validate(result.model_dump())
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (float, int))
        or not np.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise InputContractError("확률 임계값은 0~1의 유한 숫자여야 합니다.")
    return {
        spid: tuple(p for p, v in row.items() if v >= threshold)
        for spid, row in result.probabilities.items()
    }
