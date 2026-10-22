"""웹 v1 응답 경계. 도메인 정수/검증기를 바꾸지 않고 엄격히 검사한다.

v1은 합성/관측 자료 모드를 구분한다. 통합된 프론트엔드와 같은 계약을 사용한다.
"""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from FRS.core.request_normalization import SupportedConditions
from FRS.core.request_normalization.conditions import GAME_REQUIRED_FIELDS
from FRS.schema.scope import FORMATION_SLOTS, PRIORITY_CLUBS

WEB_CONTRACT = "web-agent.v1"
DataMode = Literal["synthetic", "observed"]
Text = Annotated[str, Field(min_length=1)]
BP = Annotated[str, Field(max_length=34, pattern=r"^(0|[1-9][0-9]*)$")]
Salary = Annotated[str, Field(max_length=19, pattern=r"^(0|[1-9][0-9]*)$")]
Identifier = Annotated[str, Field(max_length=30, pattern=r"^(0|[1-9][0-9]*)$")]
Grade = Annotated[int, Field(ge=5, le=11)]
Score = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Status = Literal[
    "needs_clarification",
    "optimal",
    "feasible",
    "infeasible",
    "unknown",
    "data_expired",
    "internal_error",
    "llm_unavailable",
]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class WebConditions(WireModel):
    price_cap: BP
    salary_cap: Salary
    team_color: Text
    formation: Text
    allowed_reinforcements: list[Grade]

    @model_validator(mode="after")
    def supported(self):
        if (
            int(self.salary_cap) > 2**63 - 1
            or self.team_color not in PRIORITY_CLUBS
            or self.formation not in FORMATION_SLOTS
            or not self.allowed_reinforcements
            or self.allowed_reinforcements != sorted(set(self.allowed_reinforcements))
        ):
            raise ValueError("지원하는 정확한 조건이 필요합니다.")
        return self


class WebIssue(WireModel):
    field: Text
    kind: Literal["missing", "invalid", "unsupported", "ambiguous"]
    code: Text
    message: Text
    occurrence: Annotated[int, Field(ge=0, le=2**53 - 1)] | None


class WebSupported(WireModel):
    required_fields: list[Text]
    clubs: list[Text]
    formations: list[Text]
    reinforcements: list[Grade]

    @model_validator(mode="after")
    def fixed_scope(self):
        if self.model_dump() not in (
            SupportedConditions().model_dump(mode="json"),
            SupportedConditions(required_fields=GAME_REQUIRED_FIELDS).model_dump(
                mode="json"
            ),
        ):
            raise ValueError("고정 지원 목록이 일치하지 않습니다.")
        return self


class WebRow(WireModel):
    slot: Text
    spid: Identifier
    person_id: Identifier
    name: Text
    reinforce_level: Grade
    price: BP
    salary: Salary
    raw_ovr: Annotated[int, Field(ge=0, le=200)]
    performance: Score
    placement_type: Text

    @model_validator(mode="after")
    def precise(self):
        if int(self.salary) > 2**63 - 1 or self.performance != self.raw_ovr / 2:
            raise ValueError("급여/OVR 점수 계약이 일치하지 않습니다.")
        return self


class WebSquad(WireModel):
    rows: Annotated[list[WebRow], Field(min_length=11, max_length=11)]
    total_price: Annotated[str, Field(max_length=36, pattern=r"^(0|[1-9][0-9]*)$")]
    total_salary: Annotated[str, Field(max_length=21, pattern=r"^(0|[1-9][0-9]*)$")]
    total_ovr: Annotated[int, Field(ge=0, le=2200)]
    performance: Annotated[float, Field(ge=0, le=1100, allow_inf_nan=False)]

    @model_validator(mode="after")
    def sums(self):
        if (
            len({r.spid for r in self.rows}) != 11
            or len({r.person_id for r in self.rows}) != 11
            or sum(int(r.price) for r in self.rows) != int(self.total_price)
            or sum(int(r.salary) for r in self.rows) != int(self.total_salary)
            or sum(r.raw_ovr for r in self.rows) != self.total_ovr
            or sum(r.performance for r in self.rows) != self.performance
        ):
            raise ValueError("선수 중복 또는 합계 불일치입니다.")
        return self


class WebSnapshot(WireModel):
    id: Text
    t0: str
    valid_until: str | None

    @field_validator("t0", "valid_until")
    @classmethod
    def aware_iso(cls, value):
        if value is None:
            return value
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})", value
        ):
            raise ValueError("시간대 있는 ISO 시각이 필요합니다.")
        datetime.fromisoformat(value)
        return value


class WebFinal(WireModel):
    status: Status
    conditions: WebConditions | None
    issues: list[WebIssue]
    supported: WebSupported
    squad: WebSquad | None
    snapshot: WebSnapshot | None
    versions: dict[Text, Text]
    answer: Text
    narration_source: Literal["template"]
    data_mode: DataMode

    @model_validator(mode="after")
    def consistent(self):
        if self.versions.get("web_contract") != WEB_CONTRACT:
            raise ValueError("웹 버전이 일치하지 않습니다.")
        if self.status in ("optimal", "feasible"):
            if (
                self.conditions is None
                or self.squad is None
                or self.snapshot is None
                or self.snapshot.valid_until is None
                or self.issues
            ):
                raise ValueError("성공 결과의 필수 근거가 없습니다.")
            rows, conditions = self.squad.rows, self.conditions
            if (
                [r.slot for r in rows] != list(FORMATION_SLOTS[conditions.formation])
                or any(
                    r.reinforce_level not in conditions.allowed_reinforcements
                    for r in rows
                )
                or int(self.squad.total_price) > int(conditions.price_cap)
                or int(self.squad.total_salary) > int(conditions.salary_cap)
            ):
                raise ValueError("자리/강화/상한이 일치하지 않습니다.")
        elif self.squad is not None:
            raise ValueError("비성공 결과에는 표가 없어야 합니다.")
        if self.status == "needs_clarification" and (
            self.conditions is not None or not self.issues
        ):
            raise ValueError("명확화에는 사유만 있어야 합니다.")
        return self
