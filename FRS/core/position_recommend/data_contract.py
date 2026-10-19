"""등록 포지션 분류기의 원시 입력 계약. I/O·전처리 fit·학습은 수행하지 않는다."""

import hashlib
import json
from dataclasses import dataclass
from numbers import Integral
from typing import Annotated, Iterable

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from FRS.schema.models import CardId, PlayerCard, Position, ReinforceLevel, Text

STAT_FEATURES = (
    "speed",
    "accelartion",
    "goal_determination",
    "shooting_power",
    "mid-distance_shot",
    "positioning",
    "volley_shot",
    "penalty_kick",
    "short_pass",
    "field_of_vision",
    "cross",
    "long_pass",
    "free_kick",
    "curve",
    "dribble",
    "ball_control",
    "agility",
    "balance",
    "reaction_speed",
    "man-to-man_defense",
    "tackle",
    "intercept",
    "header",
    "sliding_tackle",
    "struggle",
    "stamina",
    "positiveness",
    "jump",
    "composure",
    "gk_diving",
    "gk_handling",
    "gk_kick",
    "gk_reaction_speed",
    "gk_positioning",
)
EXTRA_FEATURES = (
    "height",
    "weight",
    "physical",
    "dribble_skill",
    "left_foot",
    "right_foot",
)
FEATURE_NAMES = STAT_FEATURES + EXTRA_FEATURES
STRING_DTYPE = pd.StringDtype(storage="python")


class InputContractError(ValueError):
    pass


class ObservationKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    spid: CardId
    batch_id: Text
    version: Text


class InputSelection(BaseModel):
    """전체 입력의 관측 키와 강화 기준을 호출자가 고정한다. 최신 선택은 없다."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    observations: Annotated[tuple[ObservationKey, ...], Field(min_length=1)]
    base_reinforce_level: ReinforceLevel

    @model_validator(mode="after")
    def unique_cards(self):
        ids = [key.spid for key in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("한 카드에는 하나의 명시 관측만 선택해야 합니다.")
        return self


def feature_spec():
    """공유할 원시 피처 스펙의 복사본과 결정적 해시. 변환기는 후속 범위다."""
    spec = dict(
        version=1,
        stage="raw_card_input",
        features=[
            dict(
                name=name,
                dtype="string[python]" if name == "physical" else "int64",
                source=f"stats.{name}" if name in STAT_FEATURES else name,
            )
            for name in FEATURE_NAMES
        ],
        preprocessing="none",
        missing="error",
    )
    encoded = json.dumps(
        spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {**spec, "sha256": hashlib.sha256(encoded.encode()).hexdigest()}


def _selected(cards: Iterable[PlayerCard | dict], selection: InputSelection):
    if not isinstance(selection, InputSelection):
        raise InputContractError("InputSelection으로 관측을 명시해야 합니다.")
    selection = InputSelection.model_validate(selection.model_dump())
    expected = {key.spid: key for key in selection.observations}
    selected = {}
    for value in cards:
        # 모델 객체도 다시 검증하여 model_copy/construct나 가변 stats의 우회를 막는다.
        card = PlayerCard.model_validate(
            value.model_dump() if isinstance(value, PlayerCard) else value
        )
        key = ObservationKey(
            spid=card.spid, batch_id=card.batch_id, version=card.version
        )
        if expected.get(card.spid) != key:
            raise InputContractError("명시하지 않은 카드·배치·관측 버전입니다.")
        if card.base_reinforce_level != selection.base_reinforce_level:
            raise InputContractError("기준 강화가 다릅니다.")
        if card.spid in selected and selected[card.spid] != card:
            raise InputContractError("같은 카드의 관측이 충돌합니다.")
        selected[card.spid] = card
    if set(selected) != set(expected):
        raise InputContractError("명시한 카드 관측이 누락됐습니다.")
    return tuple(selected[key.spid] for key in selection.observations)


def _frame(cards):
    rows = []
    for card in cards:
        missing = set(STAT_FEATURES) - set(card.stats)
        if missing or any(getattr(card, name) is None for name in EXTRA_FEATURES):
            raise InputContractError("필수 40개 피처가 누락됐습니다.")
        rows.append(
            {
                **{name: card.stats[name] for name in STAT_FEATURES},
                **{name: getattr(card, name) for name in EXTRA_FEATURES},
            }
        )
    index = pd.Index([card.spid for card in cards], dtype="int64", name="spid")
    frame = pd.DataFrame(rows, columns=FEATURE_NAMES, index=index)
    for name in FEATURE_NAMES:
        frame[name] = frame[name].astype(
            STRING_DTYPE if name == "physical" else "int64"
        )
    validate_model_input(frame)
    return frame


def to_model_input(cards, *, selection: InputSelection) -> pd.DataFrame:
    """검증된 관측을 카드당 한 행으로 변환한다. spid는 X 열이 아닌 인덱스다.

    원본/완료 배치 검증은 I/O 경계에서 먼저 수행해야 한다. unknown·과거 관측을
    학습 입력으로 허용하더라도 추천 상태·관측 시각·기한을 변경하지 않는다.
    """
    return _frame(_selected(cards, selection))


def validate_model_input(frame):
    if not isinstance(frame, pd.DataFrame) or tuple(frame.columns) != FEATURE_NAMES:
        raise InputContractError("피처 열 이름·개수·순서가 스펙과 다릅니다.")
    if frame.empty or frame.isna().any().any():
        raise InputContractError("빈 입력이나 누락 피처를 허용하지 않습니다.")
    for name in FEATURE_NAMES:
        column = frame[name]
        dtype = STRING_DTYPE if name == "physical" else "int64"
        if column.dtype != dtype:
            raise InputContractError(f"피처 자료형이 다릅니다: {name}")
        if name == "physical":
            if (column.str.len() == 0).any() or (column != column.str.strip()).any():
                raise InputContractError("체형 문자열이 유효하지 않습니다.")
        elif (column < 0).any() or (name in EXTRA_FEATURES and (column == 0).any()):
            raise InputContractError(f"피처 값의 범위가 다릅니다: {name}")


@dataclass(frozen=True)
class LabeledInput:
    X: pd.DataFrame
    y: pd.Series
    metadata: pd.DataFrame
    selection: InputSelection

    @property
    def card_groups(self):
        return self.metadata["spid"].copy()


def to_labeled_input(cards, *, selection: InputSelection) -> LabeledInput:
    selected = _selected(cards, selection)
    raw = _frame(selected)
    row_ids, labels, metadata = [], [], []
    for offset, card in enumerate(selected):
        if not card.positions:
            raise InputContractError("등록 포지션 라벨이 없습니다.")
        for position in card.positions:
            row_ids.append(offset)
            labels.append(position.value)
            metadata.append(
                dict(
                    spid=card.spid,
                    person_id=card.person_id,
                    batch_id=card.batch_id,
                    version=card.version,
                )
            )
    X = raw.iloc[row_ids].reset_index(drop=True)
    return LabeledInput(
        X=X,
        y=pd.Series(labels, dtype=STRING_DTYPE, name="position"),
        metadata=pd.DataFrame(metadata).astype({"spid": "int64", "person_id": "int64"}),
        selection=InputSelection.model_validate(selection.model_dump()),
    )


def validate_card_split(rows: LabeledInput, *, train_indices, test_indices):
    """분할은 하지 않고 호출자가 지정한 전체 행의 카드 분리를 검증한다."""
    validate_model_input(rows.X)
    expected_index = pd.RangeIndex(len(rows.X))
    if (
        not rows.X.index.equals(expected_index)
        or not rows.y.index.equals(expected_index)
        or not rows.metadata.index.equals(expected_index)
        or len(rows.y) != len(rows.X)
        or len(rows.metadata) != len(rows.X)
        or rows.y.isna().any()
        or not set(rows.y) <= {position.value for position in Position}
        or tuple(rows.metadata.columns) != ("spid", "person_id", "batch_id", "version")
    ):
        raise InputContractError("라벨·식별 정보와 행의 대응이 다릅니다.")
    meta = rows.metadata
    if (
        meta.isna().any().any()
        or meta["spid"].dtype != "int64"
        or meta["person_id"].dtype != "int64"
        or not meta["spid"].between(1_000_000, 999_999_999).all()
        or not (meta["person_id"] == meta["spid"] % 1_000_000).all()
        or rows.y.dtype != STRING_DTYPE
    ):
        raise InputContractError("카드 그룹 식별 정보가 유효하지 않습니다.")
    if not isinstance(rows.selection, InputSelection):
        raise InputContractError("라벨 행의 원래 관측 선택이 없습니다.")
    selected = InputSelection.model_validate(rows.selection.model_dump())
    expected = {(k.spid, k.batch_id, k.version) for k in selected.observations}
    actual = set(
        meta[["spid", "batch_id", "version"]].itertuples(index=False, name=None)
    )
    if actual != expected:
        raise InputContractError("라벨 행과 원래 관측 선택이 다릅니다.")
    for _, indices in meta.groupby("spid").groups.items():
        group = meta.loc[indices]
        for _, row in group.iterrows():
            ObservationKey(
                spid=int(row.spid), batch_id=row.batch_id, version=row.version
            )
        if (
            group[["person_id", "batch_id", "version"]].drop_duplicates().shape[0] != 1
            or rows.X.loc[indices].drop_duplicates().shape[0] != 1
            or rows.y.loc[indices].duplicated().any()
        ):
            raise InputContractError("같은 카드의 피처·관측·포지션 라벨이 충돌합니다.")
    sides = []
    for indices in (train_indices, test_indices):
        indices = list(indices)
        if (
            not indices
            or any(isinstance(i, bool) or not isinstance(i, Integral) for i in indices)
            or len(indices) != len(set(indices))
            or any(i < 0 or i >= len(rows.X) for i in indices)
        ):
            raise InputContractError("분할 행 번호가 유효하지 않습니다.")
        sides.append(set(indices))
    train, test = sides
    if train & test or train | test != set(expected_index):
        raise InputContractError("분할 행이 중복되거나 누락됐습니다.")
    groups = rows.card_groups
    if set(groups.iloc[sorted(train)]) & set(groups.iloc[sorted(test)]):
        raise InputContractError("동일 카드의 행이 train/test에 함께 있습니다.")
