"""포지션 코드를 글자로 분해하지 않는 집합 기반 평가 지표."""

import numpy as np

from FRS.core.position_recommend.data_contract import InputContractError
from FRS.core.position_recommend.probabilities import (
    ProbabilityResult,
    threshold_positions,
)
from FRS.schema.models import Position


def position_set(values):
    if isinstance(values, str):
        raise InputContractError(
            "포지션 목록을 명시해야 합니다. 문자열을 글자로 나누지 않습니다."
        )
    values = tuple(values)
    if not values or len(values) != len(set(values)):
        raise InputContractError("정답 포지션 목록이 비었거나 중복됐습니다.")
    return frozenset(Position(value).value for value in values)


def row_coverage(predictions, labels):
    """레거시 단일 라벨 행의 포함률. 정답 문자열은 하나의 코드로 비교한다."""
    predictions, labels = list(predictions), list(labels)
    if not labels or len(predictions) != len(labels):
        raise InputContractError("예측과 정답의 행 수가 다르거나 비었습니다.")
    hits = 0
    for predicted, label in zip(predictions, labels, strict=True):
        if isinstance(predicted, str):
            raise InputContractError("예측 포지션은 코드 목록이어야 합니다.")
        codes = {Position(p).value for p in predicted}
        hits += Position(label).value in codes
    return hits / len(labels)


def evaluate_probabilities(result: ProbabilityResult, truth, *, threshold=0.1):
    result = ProbabilityResult.model_validate(result.model_dump())
    if set(truth) != set(result.probabilities):
        raise InputContractError("확률과 정답의 카드 ID가 다릅니다.")
    predicted = threshold_positions(result, threshold)
    actual = {spid: position_set(values) for spid, values in truth.items()}
    positions = sorted(set().union(*actual.values()))
    total_labels = sum(len(values) for values in actual.values())
    label_hits = sum(
        len(values & set(predicted[spid])) for spid, values in actual.items()
    )
    recalls = {}
    for position in positions:
        support = sum(position in values for values in actual.values())
        hits = sum(
            position in actual[spid] and position in predicted[spid] for spid in actual
        )
        recalls[position] = dict(
            support_cards=support, hit_cards=hits, recall=hits / support
        )
    return dict(
        threshold=threshold,
        cards=len(actual),
        registered_labels=total_labels,
        registered_label_coverage=label_hits / total_labels,
        card_any_label_coverage=sum(
            bool(values & set(predicted[spid])) for spid, values in actual.items()
        )
        / len(actual),
        card_all_labels_coverage=sum(
            values <= set(predicted[spid]) for spid, values in actual.items()
        )
        / len(actual),
        mean_prediction_set_size=float(
            np.mean([len(values) for values in predicted.values()])
        ),
        per_position_recall=recalls,
        untrained_truth_positions=sorted(
            set(positions) - {p.value for p in result.classes}
        ),
    )
