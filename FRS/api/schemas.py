"""HTTP 경계의 요청·응답 계약. 기존 문서/확률 스키마를 재사용한다."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from FRS.core.position_recommend.data_contract import InputSelection
from FRS.core.position_recommend.model_contract import SHA256
from FRS.schema.models import PlayerCard, Text


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CardQuery(ApiModel):
    batch_id: Text
    version: Text


class CardResponse(ApiModel):
    card: PlayerCard
    evaluated_at: datetime
    usable: bool


class PositionRequest(ApiModel):
    model_version: SHA256
    selection: InputSelection


class HealthResponse(ApiModel):
    status: str
    database: str
    model: str
    model_version: str | None
