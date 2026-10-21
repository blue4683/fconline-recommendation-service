"""HTTP와 분리한 MCP 입력/출력과 정적 지원 정보."""

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from FRS.agent.candidate_selection import selection_spec
from FRS.agent.candidates import PreparationIssue, candidate_spec, canonical
from FRS.agent.material import SyntheticLeagueSpec
from FRS.api.agent_contract import (
    WebConditions,
    WebIssue,
    WebSnapshot,
    WebSquad,
    WebSupported,
    WireModel,
)
from FRS.api.agent_events import RequestId
from FRS.core.candidate_search.search import BatchSelection
from FRS.core.request_normalization import SupportedConditions, conditions_spec
from FRS.core.request_normalization.conditions import (
    GAME_REQUIRED_FIELDS,
    GAME_SALARY_CAP,
)
from FRS.core.squad_recommend.snapshot import RequestSnapshot
from FRS.schema.scope import FORMATION_SLOTS

Original = Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
ERRORS = {
    "invalid_request": "요청 형식과 길이를 확인해 주세요.",
    "busy": "다른 스쿼드 작업이 진행 중입니다.",
    "request_deadline": "요청 작업 기한이 지났거나 중단됐습니다.",
    "internal_error": "처리 오류로 추천을 반환하지 않습니다.",
    "invalid_selection": "후보 선택 결과의 형식을 확인해 주세요.",
    "candidate_set_mismatch": "원문과 후보 묶음이 일치하지 않습니다.",
    "invalid_context_selection": "지원하지 않는 문맥 선택입니다.",
    "unknown_candidate": "알 수 없는 후보 ID입니다.",
    "duplicate_candidate": "같은 후보 ID를 중복 선택했습니다.",
    "field_mismatch": "후보 ID와 필드가 일치하지 않습니다.",
    "incomplete_coverage": "원문의 후보를 빠짐없이 선택해야 합니다.",
    "invalid_registry": "원문의 후보 묶음을 확인해 주세요.",
}


class BuildRequest(WireModel):
    request_id: RequestId
    original: Original
    selection: dict[str, JsonValue]


class SafeError(WireModel):
    code: str
    message: str


class MCPPresentation(WireModel):
    kind: Literal["code_presentation"]
    data_mode: Literal["synthetic"]
    status: Literal[
        "needs_clarification",
        "optimal",
        "feasible",
        "infeasible",
        "unknown",
        "data_expired",
        "internal_error",
    ]
    presentation_version: str
    graph_version: str
    normalizer_version: str | None
    engine_version: str | None
    conditions: WebConditions | None
    issues: list[WebIssue]
    supported: WebSupported | None
    squad: WebSquad | None
    snapshot: WebSnapshot | None
    evaluated_at: str | None
    narration_source: Literal["template"]
    model_metadata: Literal["not_provided"]
    code: str | None
    answer: str
    coverage: list[dict[str, JsonValue]] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.status in ("optimal", "feasible"):
            if (
                self.conditions is None
                or self.squad is None
                or self.snapshot is None
                or self.snapshot.valid_until is None
                or self.issues
            ):
                raise ValueError("성공 근거가 없습니다.")
            conditions, rows = self.conditions, self.squad.rows
            if (
                [r.slot for r in rows] != list(FORMATION_SLOTS[conditions.formation])
                or any(
                    r.reinforce_level not in conditions.allowed_reinforcements
                    for r in rows
                )
                or int(self.squad.total_price) > int(conditions.price_cap)
                or int(self.squad.total_salary) > int(conditions.salary_cap)
            ):
                raise ValueError("출력과 조건이 일치하지 않습니다.")
        elif self.squad is not None:
            raise ValueError("비성공 결과에는 표가 없어야 합니다.")
        if self.status == "needs_clarification" and (
            self.conditions is not None or not self.issues
        ):
            raise ValueError("명확화에는 사유가 필요합니다.")
        return self


class MaterialContext(WireModel):
    material_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    specification: SyntheticLeagueSpec
    batches: BatchSelection


class BuildEnvelope(WireModel):
    contract_version: Literal["mcp-build.v1"] = "mcp-build.v1"
    request_id: RequestId | None
    outcome: Literal["input_issues", "selection_error", "agent_result", "error"]
    issues: list[PreparationIssue] = Field(default_factory=list)
    presentation: MCPPresentation | None = None
    context: MaterialContext | None = None
    error: SafeError | None = None
    versions: dict[str, str]
    data_mode: Literal["synthetic"] = "synthetic"

    @model_validator(mode="after")
    def shape(self):
        if (
            self.versions.get("model") != "not_invoked_client_selection"
            or self.versions.get("contract") != "mcp-build.v1"
        ):
            raise ValueError("MCP 실행 출처가 일치하지 않습니다.")
        if self.context is not None and (
            self.context.specification.seed != 123
            or self.context.specification.members_per_family != 3
        ):
            raise ValueError("확정된 합성 생성 범위를 유지해야 합니다.")
        if self.outcome != "agent_result" and self.context is not None:
            raise ValueError("자료를 읽지 않은 경로에 context가 있으면 안 됩니다.")
        if (
            self.presentation is not None
            and self.presentation.status in ("optimal", "feasible")
            and self.context is None
        ):
            raise ValueError("성공 자료의 명시 context가 필요합니다.")
        if self.outcome == "input_issues":
            if (
                not self.issues
                or self.presentation is not None
                or self.error is not None
            ):
                raise ValueError("준비 문제 응답이 일치하지 않습니다.")
        elif self.outcome == "agent_result":
            if self.issues or self.error is not None or self.presentation is None:
                raise ValueError("구성 결과 응답이 일치하지 않습니다.")
        elif self.issues or self.presentation is not None or self.error is None:
            raise ValueError("안전한 오류 응답이 필요합니다.")
        if self.error and (
            self.error.code not in ERRORS
            or self.error.message != ERRORS[self.error.code]
        ):
            raise ValueError("안전한 오류 코드가 필요합니다.")
        return self


def mcp_spec():
    spec = dict(
        version=1,
        contract="mcp-build.v1",
        transport="stdio",
        sdk="mcp==2.3.0",
        request=BuildRequest.model_json_schema(),
        output=BuildEnvelope.model_json_schema(),
        parallel=1,
        queue=0,
        deadline_seconds=150,
        retry=False,
        fallback=False,
        cancellation="keep_slot_until_physical_worker_ends",
        model_calls=False,
        salary_cap=GAME_SALARY_CAP,
        context="explicit_fixed_synthetic_batches_and_observed_at",
        request_id="correlation_only",
    )
    return {**spec, "mcp_policy_version": hashlib.sha256(canonical(spec)).hexdigest()}


def versions():
    return dict(
        mcp_policy=mcp_spec()["mcp_policy_version"],
        contract="mcp-build.v1",
        candidate=candidate_spec(fixed_salary_cap=GAME_SALARY_CAP)["candidate_version"],
        selection=selection_spec()["selection_version"],
        normalizer=conditions_spec(fixed_salary_cap=GAME_SALARY_CAP)[
            "normalizer_version"
        ],
        model="not_invoked_client_selection",
    )


def supported_resource():
    return dict(
        contract_version="mcp-build.v1",
        data_mode="synthetic",
        supported=SupportedConditions(required_fields=GAME_REQUIRED_FIELDS).model_dump(
            mode="json"
        ),
        limits=dict(original_codepoints=4000, candidates=128, contexts=64),
        input="급여 상한310 고정. 예산·클럽·포메이션·강화를 명시합니다. 변경 없는 원문과 candidate-selection.v1을 사용하며 급여 ID를 임의 보충하지 않습니다.",
        versions=versions(),
    )


@dataclass(frozen=True)
class MCPResponse:
    payload: bytes
    snapshot: RequestSnapshot | None = None

    def __post_init__(self):
        if type(self.payload) is not bytes:
            raise TypeError("MCP 응답은 불변 bytes여야 합니다.")
        if self.snapshot is not None and type(self.snapshot) is not RequestSnapshot:
            raise TypeError("원래 RequestSnapshot이 필요합니다.")

    def encode(self, *, clock):
        body = BuildEnvelope.model_validate(json.loads(self.payload)).model_dump(
            mode="json"
        )
        encoded = canonical(body)
        if self.snapshot is not None:
            if (
                body["presentation"] is None
                or body["presentation"]["snapshot"] is None
                or body["presentation"]["snapshot"]["id"] != self.snapshot.snapshot_id
            ):
                raise ValueError("출력과 스냅샷이 일치하지 않습니다.")
            if self.snapshot.check_response(clock())["status"] == "data_expired":
                result = body["presentation"]
                result.update(
                    status="data_expired",
                    squad=None,
                    answer="자료가 만료되어 추천을 반환하지 않습니다.",
                )
                encoded = canonical(
                    BuildEnvelope.model_validate(body).model_dump(mode="json")
                )
        elif body["presentation"] is not None and body["presentation"]["status"] in (
            "optimal",
            "feasible",
        ):
            raise ValueError("성공 결과의 원래 스냅샷이 없습니다.")
        return encoded


def response(*, request_id, outcome, snapshot=None, **values):
    if "issues" in values:
        values["issues"] = list(values["issues"])
    if "presentation" in values:
        values["presentation"] = MCPPresentation.model_validate(values["presentation"])
    body = BuildEnvelope(
        request_id=request_id, outcome=outcome, versions=versions(), **values
    )
    return MCPResponse(canonical(body.model_dump(mode="json")), snapshot)


def error_response(code, request_id=None, *, outcome="error"):
    code = code if code in ERRORS else "internal_error"
    return response(
        request_id=request_id,
        outcome=outcome,
        error=SafeError(code=code, message=ERRORS[code]),
    )
