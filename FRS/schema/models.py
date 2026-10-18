"""수집과 추천 사이에서 공유하는 문서 계약. I/O와 실제 데이터는 포함하지 않는다."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal

from bson.decimal128 import Decimal128
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
StatName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]
CardId = Annotated[int, Field(strict=True, ge=1_000_000, le=999_999_999)]
NonNegative = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Positive = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
SignedValue = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
ReinforceLevel = Annotated[int, Field(strict=True, ge=1, le=13)]
# 모든 정수를 손실 없이 Decimal128로 표현할 수 있는 범위다. 게임 상한이 아니다.
BP = Annotated[int, Field(strict=True, ge=1, le=10**34 - 1)]


class Position(StrEnum):
    GK = "GK"
    SW = "SW"
    RWB = "RWB"
    RB = "RB"
    RCB = "RCB"
    CB = "CB"
    LCB = "LCB"
    LB = "LB"
    LWB = "LWB"
    RDM = "RDM"
    CDM = "CDM"
    LDM = "LDM"
    RM = "RM"
    RCM = "RCM"
    CM = "CM"
    LCM = "LCM"
    LM = "LM"
    RAM = "RAM"
    CAM = "CAM"
    LAM = "LAM"
    RF = "RF"
    CF = "CF"
    LF = "LF"
    RW = "RW"
    RS = "RS"
    ST = "ST"
    LS = "LS"
    LW = "LW"


class DataStatus(StrEnum):
    OK = "ok"
    UNKNOWN = "unknown"
    UNTRADEABLE = "untradeable"
    STALE = "stale"


class SourceKind(StrEnum):
    API = "nexon_api"
    WEB = "nexon_web"
    SYNTHETIC = "synthetic"


class TeamColorType(StrEnum):
    CLUB = "club"
    NATION = "nation"
    TRAIT = "trait"
    ENHANCEMENT = "enhancement"


class PriceType(StrEnum):
    UNKNOWN = "unknown"
    REFERENCE = "reference"
    TRANSACTION = "transaction"


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    schema_version: Literal[1] = 1

    def to_document(self):
        return self.model_dump(mode="python")

    @classmethod
    def from_document(cls, document):
        values = dict(document)
        values.pop("_id", None)
        return cls.model_validate(values)


class ObservedDocument(Document):
    batch_id: Text
    source: SourceKind
    observed_at: AwareDatetime
    delete_after: AwareDatetime | None

    @field_validator("observed_at", "delete_after")
    @classmethod
    def normalize_time(cls, value):
        if value is None:
            return None
        value = value.astimezone(timezone.utc)
        # BSON의 밀리초 정밀도와 왕복 변환 결과를 일치시킨다.
        return value.replace(microsecond=(value.microsecond // 1000) * 1000)

    @model_validator(mode="after")
    def validate_retention(self):
        if self.delete_after is not None and self.delete_after <= self.observed_at:
            raise ValueError("삭제 기한은 관측 시각보다 뒤여야 합니다.")
        return self


class Observation(ObservedDocument):
    status: DataStatus
    valid_until: AwareDatetime | None

    @field_validator("valid_until")
    @classmethod
    def normalize_validity(cls, value):
        return cls.normalize_time(value)

    @model_validator(mode="after")
    def validate_validity(self):
        if self.status == DataStatus.OK and self.valid_until is None:
            raise ValueError("ok 상태에는 확인된 신선도 기한이 필요합니다.")
        if self.valid_until is not None:
            if self.valid_until <= self.observed_at:
                raise ValueError("신선도 기한은 관측 시각보다 뒤여야 합니다.")
            if self.delete_after is not None and self.valid_until > self.delete_after:
                raise ValueError("신선도 기한은 삭제 기한을 넘을 수 없습니다.")
        return self

    def is_usable_at(self, when: datetime) -> bool:
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("조회 시각에는 시간대가 필요합니다.")
        when = when.astimezone(timezone.utc)
        return (
            self.status == DataStatus.OK
            and self.observed_at <= when
            and self.valid_until is not None
            and when < self.valid_until
            and (self.delete_after is None or when < self.delete_after)
        )


class CardObservation(Observation):
    spid: CardId
    version: Text


class PlayerCard(CardObservation):
    season_id: Annotated[int, Field(strict=True, ge=1, le=999)]
    person_id: Annotated[int, Field(strict=True, ge=0, le=999_999)]
    name: Text
    base_reinforce_level: ReinforceLevel
    positions: tuple[Position, ...]
    stats: dict[StatName, NonNegative]
    position_ovr: dict[Position, Positive] = Field(default_factory=dict)
    height: Positive | None = None
    weight: Positive | None = None
    physical: Text | None = None
    dribble_skill: Positive | None = None
    left_foot: Positive | None = None
    right_foot: Positive | None = None
    skills: tuple[Text, ...] | None = None
    used: NonNegative | None = None

    @model_validator(mode="after")
    def validate_card(self):
        if (self.season_id, self.person_id) != divmod(self.spid, 1_000_000):
            raise ValueError("spid와 시즌·인물 ID가 일치하지 않습니다.")
        if len(set(self.positions)) != len(self.positions):
            raise ValueError("등록 포지션은 중복될 수 없습니다.")
        if self.status == DataStatus.OK and (not self.positions or not self.stats):
            raise ValueError("ok 선수에는 등록 포지션과 관측 능력치가 필요합니다.")
        return self


class CardSalary(CardObservation):
    reinforce_level: ReinforceLevel
    salary: Positive | None

    @model_validator(mode="after")
    def validate_salary(self):
        if self.status == DataStatus.OK and self.salary is None:
            raise ValueError("ok 급여에는 관측값이 필요합니다.")
        return self


class CardPrice(CardObservation):
    reinforce_level: ReinforceLevel
    price_type: PriceType
    price: BP | None

    @field_validator("price", mode="before")
    @classmethod
    def decode_bson_price(cls, value):
        if isinstance(value, Decimal128):
            decimal = value.to_decimal()
            if not decimal.is_finite() or decimal != decimal.to_integral_value():
                raise ValueError("BP는 유한한 정수여야 합니다.")
            return int(decimal)
        return value

    @model_validator(mode="after")
    def validate_price(self):
        if self.status == DataStatus.OK:
            if self.price is None or self.price_type == PriceType.UNKNOWN:
                raise ValueError("ok 가격에는 관측값과 확인된 가격 의미가 필요합니다.")
        return self

    def to_document(self):
        document = super().to_document()
        if self.price is not None:
            document["price"] = Decimal128(str(self.price))
        return document


class ReinforceStats(CardObservation):
    reinforce_level: ReinforceLevel
    baseline_reinforce_level: ReinforceLevel
    baseline_card_version: Text
    stats_delta: dict[StatName, SignedValue]
    position_ovr: dict[Position, Positive] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_stats(self):
        if self.status == DataStatus.OK and not (self.stats_delta or self.position_ovr):
            raise ValueError("ok 강화 기록에는 관측한 차이값 또는 OVR이 필요합니다.")
        return self


class TeamColor(CardObservation):
    type: TeamColorType
    name: Text


class FetchOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    STOPPED = "stopped"


class FailureKind(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    REQUEST_ERROR = "request_error"
    HTTP_ERROR = "http_error"
    NOT_FOUND = "not_found"
    BLOCKED = "blocked"
    SLOW_RESPONSE = "slow_response"
    UNEXPECTED_RESPONSE = "unexpected_response"
    PARSING = "parsing"


class CrawlLog(ObservedDocument):
    spid: CardId | None
    resource: Literal["metadata", "ability", "price"]
    outcome: FetchOutcome
    attempt: Positive
    duration_ms: NonNegative
    http_status: Annotated[int, Field(strict=True, ge=100, le=599)] | None
    failure_kind: FailureKind | None

    @model_validator(mode="after")
    def validate_outcome(self):
        if self.outcome == FetchOutcome.OK:
            if self.failure_kind is not None or self.http_status != 200:
                raise ValueError("성공 로그에는 HTTP 200과 오류 없음이 필요합니다.")
        elif self.failure_kind is None:
            raise ValueError("실패·중단 로그에는 오류 종류가 필요합니다.")
        if self.failure_kind == FailureKind.BLOCKED:
            if self.outcome != FetchOutcome.STOPPED or self.http_status not in (
                403,
                429,
            ):
                raise ValueError("차단 로그는 403·429 중단으로 기록해야 합니다.")
        return self


DOCUMENT_MODELS = {
    "player_card": PlayerCard,
    "card_salary": CardSalary,
    "card_price": CardPrice,
    "reinforce_stats": ReinforceStats,
    "team_color": TeamColor,
    "crawl_log": CrawlLog,
}
