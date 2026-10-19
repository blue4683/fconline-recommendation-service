"""명시 관측의 OVR만 점수로 변환한다. 배치 판정·I/O·학습은 수행하지 않는다."""

import hashlib
import json
from datetime import timezone
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from FRS.schema.models import (
    CardId,
    DataStatus,
    NonNegative,
    PlayerCard,
    Position,
    ReinforceStats,
    SourceKind,
    Text,
)
from FRS.schema.scope import FORMATION_SLOTS

_ALIASES = MappingProxyType(
    {
        "RCB": "CB",
        "LCB": "CB",
        "RDM": "CDM",
        "LDM": "CDM",
        "RCM": "CM",
        "LCM": "CM",
        "RAM": "CAM",
        "LAM": "CAM",
        "RS": "ST",
        "LS": "ST",
    }
)
_SLOTS = frozenset(slot for slots in FORMATION_SLOTS.values() for slot in slots)
_VERSION = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def performance_spec():
    """호출마다 새 사양을 반환해 호출자가 점수 규칙을 변경할 수 없게 한다."""
    spec = dict(
        version=1,
        method="position_ovr",
        base_reinforce_level=5,
        supported_reinforcements=list(range(5, 12)),
        supported_slots=sorted(_SLOTS),
        ovr_min=1,
        ovr_max=200,
        formula="ovr / 2",
        clipping=False,
        rounding="none",
        aliases=dict(_ALIASES),
        missing_or_out_of_range="exclude_pair",
        fallback="none",
        reinforcement_ovr="absolute",
        freshness="is_usable_at_query_time",
        placement="not_evaluated",
    )
    encoded = json.dumps(
        spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {**spec, "scorer_version": hashlib.sha256(encoded.encode()).hexdigest()}


class PerformanceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    spid: CardId
    slot: Position
    reinforce_level: Annotated[int, Field(strict=True, ge=5, le=11)]
    when: AwareDatetime

    @field_validator("when")
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc)

    @field_validator("slot")
    @classmethod
    def supported_slot(cls, value):
        if value not in _SLOTS:
            raise ValueError("MVP 포메이션의 지원 슬롯이 아닙니다.")
        return value


class ObservationReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    resource: Literal["player_card", "reinforce_stats"]
    spid: CardId
    batch_id: Text
    version: Text
    source: SourceKind
    status: DataStatus
    observed_at: AwareDatetime
    valid_until: AwareDatetime | None
    delete_after: AwareDatetime | None


ExclusionReason = Literal[
    "missing_card",
    "invalid_observation",
    "card_mismatch",
    "baseline_mismatch",
    "missing_reinforcement",
    "reinforcement_mismatch",
    "unusable_observation",
    "missing_ovr",
    "ovr_out_of_range",
]


class PerformanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query: PerformanceQuery
    scorer_version: _VERSION
    ovr_position: Position
    raw_ovr: NonNegative | None
    performance: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] | None
    observations: tuple[ObservationReference, ...]
    exclusion: ExclusionReason | None

    @model_validator(mode="after")
    def consistent_result(self):
        if self.exclusion is not None:
            if self.performance is not None:
                raise ValueError("제외 결과에는 성공 점수를 넣을 수 없습니다.")
        elif (
            self.raw_ovr is None
            or not 1 <= self.raw_ovr <= 200
            or self.performance != self.raw_ovr / 2
            or not self.observations
        ):
            raise ValueError("OVR과 점수·출처가 일치하지 않습니다.")
        return self


def _reference(resource, observation):
    return ObservationReference(
        resource=resource,
        **observation.model_dump(
            include={
                "spid",
                "batch_id",
                "version",
                "source",
                "status",
                "observed_at",
                "valid_until",
                "delete_after",
            }
        ),
    )


def score_performance(
    query: PerformanceQuery,
    *,
    card: PlayerCard | None,
    reinforcement: ReinforceStats | None = None,
):
    """한 카드·슬롯의 결과를 반환한다. 데이터 제외는 다른 조합 평가를 중단하지 않는다."""
    if not isinstance(query, PerformanceQuery):
        raise TypeError("PerformanceQuery로 점수 요청을 명시해야 합니다.")
    query = PerformanceQuery.model_validate(query.model_dump())
    position = Position(_ALIASES.get(query.slot, query.slot))
    references = []

    def result(*, reason=None, ovr=None):
        return PerformanceResult(
            query=query,
            scorer_version=performance_spec()["scorer_version"],
            ovr_position=position,
            raw_ovr=ovr,
            performance=ovr / 2 if reason is None else None,
            observations=tuple(references),
            exclusion=reason,
        )

    if card is None:
        return result(reason="missing_card")
    if not isinstance(card, PlayerCard):
        return result(reason="invalid_observation")
    try:
        card = PlayerCard.model_validate(card.model_dump())
    except ValidationError:
        return result(reason="invalid_observation")
    references.append(_reference("player_card", card))
    if card.spid != query.spid:
        return result(reason="card_mismatch")
    if card.base_reinforce_level != 5:
        return result(reason="baseline_mismatch")
    if not card.is_usable_at(query.when):
        return result(reason="unusable_observation")
    ability = card
    if query.reinforce_level != card.base_reinforce_level:
        if reinforcement is None:
            return result(reason="missing_reinforcement")
        if not isinstance(reinforcement, ReinforceStats):
            return result(reason="invalid_observation")
        try:
            reinforcement = ReinforceStats.model_validate(reinforcement.model_dump())
        except ValidationError:
            return result(reason="invalid_observation")
        references.append(_reference("reinforce_stats", reinforcement))
        if (
            reinforcement.spid != query.spid
            or reinforcement.reinforce_level != query.reinforce_level
            or reinforcement.baseline_reinforce_level != card.base_reinforce_level
            or reinforcement.baseline_card_version != card.version
        ):
            return result(reason="reinforcement_mismatch")
        if not reinforcement.is_usable_at(query.when):
            return result(reason="unusable_observation")
        ability = reinforcement
    ovr = ability.position_ovr.get(position)
    if ovr is None:
        return result(reason="missing_ovr")
    if not 1 <= ovr <= 200:
        return result(reason="ovr_out_of_range", ovr=ovr)
    return result(ovr=ovr)
