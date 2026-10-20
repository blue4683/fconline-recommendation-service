"""등록 포지션 계열의 기본 배치만 판정한다. 모델·I/O·성능 점수는 사용하지 않는다."""

import hashlib
import json
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from FRS.core.squad_recommend.performance import ObservationReference, PerformanceQuery
from FRS.schema.models import PlayerCard, Position
from FRS.schema.scope import FORMATION_SLOTS

# D5의 배치 정책이다. D7 점수 키 대응이 바뀌어도 자동으로 배치를 확대하지 않는다.
_FAMILIES = MappingProxyType(
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


class PlacementQuery(PerformanceQuery):
    """성능 요청과 같은 MVP 슬롯·강화·기준 시각을 사용하되 별도로 검증한다."""


def placement_spec():
    spec = dict(
        version=1,
        method="registered_position_family",
        base_reinforce_level=5,
        supported_reinforcements=list(range(5, 12)),
        supported_slots=sorted(
            {slot for slots in FORMATION_SLOTS.values() for slot in slots}
        ),
        families=dict(_FAMILIES),
        allowed_type="base",
        model_extension="disabled",
        query_contract="MVP slots, grades 5-11, explicit spid and aware time",
        freshness="base_card_is_usable_at_query_time",
        performance="not_evaluated",
    )
    encoded = json.dumps(
        spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {**spec, "placement_version": hashlib.sha256(encoded.encode()).hexdigest()}


class PlacementResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query: PlacementQuery
    placement_version: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    family: Position
    allowed: bool
    placement_type: Literal["base"] | None
    matched_positions: tuple[Position, ...]
    observation: ObservationReference | None
    exclusion: (
        Literal[
            "missing_card",
            "invalid_observation",
            "card_mismatch",
            "baseline_mismatch",
            "unusable_observation",
            "unregistered_family",
        ]
        | None
    )

    @model_validator(mode="after")
    def consistent_result(self):
        if self.allowed:
            if (
                self.placement_type != "base"
                or self.exclusion is not None
                or not self.matched_positions
                or self.observation is None
            ):
                raise ValueError("기본 배치 결과와 근거가 일치하지 않습니다.")
        elif (
            self.placement_type is not None
            or self.matched_positions
            or self.exclusion is None
        ):
            raise ValueError("배치 제외 결과와 사유가 일치하지 않습니다.")
        return self


def placement(query: PlacementQuery, *, card: PlayerCard | None):
    """한 카드·슬롯의 등록 계열만 판정한다. 같은 인물/가격/팀컬러는 별도 제약이다."""
    if not isinstance(query, PlacementQuery):
        raise TypeError("PlacementQuery로 배치 요청을 명시해야 합니다.")
    query = PlacementQuery.model_validate(query.model_dump())
    family = Position(_FAMILIES.get(query.slot, query.slot))
    reference = None

    def result(*, reason=None, matched=()):
        return PlacementResult(
            query=query,
            placement_version=placement_spec()["placement_version"],
            family=family,
            allowed=reason is None,
            placement_type="base" if reason is None else None,
            matched_positions=matched,
            observation=reference,
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
    reference = ObservationReference(
        resource="player_card",
        **card.model_dump(
            include=set(ObservationReference.model_fields) - {"resource"}
        ),
    )
    if card.spid != query.spid:
        return result(reason="card_mismatch")
    if card.base_reinforce_level != 5:
        return result(reason="baseline_mismatch")
    if not card.is_usable_at(query.when):
        return result(reason="unusable_observation")
    matched = tuple(p for p in card.positions if _FAMILIES.get(p, p) == family)
    if not matched:
        return result(reason="unregistered_family")
    return result(matched=matched)
