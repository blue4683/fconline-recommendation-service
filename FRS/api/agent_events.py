"""순수 요청 검사와 요청별 SSE 직렬화. 서버/모델/DB 접속은 없다."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator

from FRS.agent.candidates import canonical
from FRS.agent.json_contract import strict_json
from FRS.api.agent_contract import WEB_CONTRACT, DataMode, WireModel
from FRS.api.agent_projection import WebOutcome, error_outcome, projection_spec

MAX_BODY_BYTES = 65536
REQUEST_DEADLINE_SECONDS = 150
ALLOWED_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
RequestId = Annotated[
    str, Field(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
]
PROGRESS = {
    "agent": "조건 확인과 스쿼드 구성을 시작합니다.",
    "render": "결과와 응답 기한을 확인합니다.",
}


class StreamRequest(WireModel):
    contract_version: Literal["web-agent.v1"]
    request_id: RequestId
    message: Annotated[str, Field(min_length=1, max_length=4000)]

    @field_validator("message")
    @classmethod
    def original(cls, value):
        if not value.strip():
            raise ValueError("조건 원문이 필요합니다.")
        value.encode("utf-8")
        return value


class RequestError(ValueError):
    def __init__(self, code, status_code=422):
        self.code, self.status_code = code, status_code
        super().__init__("요청 형식과 길이를 확인해 주세요.")


def parse_request(raw):
    if type(raw) is not bytes:
        raise RequestError("invalid_request")
    if len(raw) > MAX_BODY_BYTES:
        raise RequestError("body_too_large", 413)
    try:
        return StreamRequest.model_validate(strict_json(raw.decode("utf-8")))
    except (ValueError, TypeError, RecursionError) as exc:
        raise RequestError("invalid_request") from exc


def event_spec():
    spec = dict(
        version=1,
        web_contract=WEB_CONTRACT,
        request=StreamRequest.model_json_schema(),
        body_bytes=MAX_BODY_BYTES,
        response_deadline_seconds=REQUEST_DEADLINE_SECONDS,
        origins=list(ALLOWED_ORIGINS),
        credentials=False,
        projection_version=projection_spec()["projection_version"],
        progress=dict(PROGRESS),
        sequence="started_0_then_progress_then_single_final_or_error",
        retry=False,
        request_id="correlation_only_no_deduplication_or_result_cache",
    )
    return {**spec, "event_version": hashlib.sha256(canonical(spec)).hexdigest()}


class EventSequence:
    def __init__(self, request_id, data_mode="synthetic"):
        self.request_id = TypeAdapter(RequestId).validate_python(request_id)
        self.data_mode = TypeAdapter(DataMode).validate_python(data_mode)
        self._seq, self._started, self._terminal = 0, False, False

    def _encode(self, kind, payload):
        if self._terminal or (kind != "started" and not self._started):
            raise ValueError("SSE 실행 순서가 일치하지 않습니다.")
        encoded = canonical(
            dict(
                contract_version=WEB_CONTRACT,
                request_id=self.request_id,
                seq=self._seq,
                type=kind,
                payload=payload,
            )
        )
        self._seq += 1
        return b"event: " + kind.encode("ascii") + b"\ndata: " + encoded + b"\n\n"

    def started(self):
        if self._started:
            raise ValueError("중복 시작 이벤트입니다.")
        output = self._encode("started", dict(data_mode=self.data_mode))
        self._started = True
        return output

    def progress(self, stage):
        if stage not in PROGRESS:
            raise ValueError("등록된 실제 단계가 필요합니다.")
        return self._encode("progress", dict(stage=stage, message=PROGRESS[stage]))

    def terminal(self, outcome, *, clock):
        if self._terminal or not self._started:
            raise ValueError("SSE 종료 순서가 일치하지 않습니다.")
        if type(outcome) is not WebOutcome:
            outcome = error_outcome()
        try:
            payload = json.loads(outcome.encode_payload(clock=clock))
        except (ValueError, TypeError, KeyError):
            outcome = error_outcome()
            payload = json.loads(outcome.encode_payload(clock=clock))
        output = self._encode(outcome.kind, payload)
        self._terminal = True
        return output
