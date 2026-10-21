"""내부 결과를 웹 필드로 투영한다. 준비 사유의 출처/응답 기한을 보존한다."""

import hashlib
import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from FRS.agent.candidate_graph import CandidateRunResult, candidate_agent_spec
from FRS.agent.candidate_selection import selection_spec
from FRS.agent.candidates import FIELDS, candidate_spec, canonical, prepare_candidates
from FRS.agent.graph import agent_spec
from FRS.agent.presentation import present_agent_result, presentation_spec
from FRS.api.agent_contract import WEB_CONTRACT, WebFinal
from FRS.core.request_normalization import (
    ExtractedConditions,
    SupportedConditions,
    normalize_conditions,
)
from FRS.core.squad_recommend.snapshot import RequestSnapshot

ERROR_MESSAGES = {
    "selection_provider_error": "조건 추출 모델을 사용할 수 없습니다. 잠시 후 새 요청을 보내 주세요.",
    "invalid_selection": "모델의 조건 선택 응답을 검증하지 못했습니다.",
    "candidate_set_mismatch": "모델 응답의 요청 근거가 일치하지 않습니다.",
    "invalid_context_selection": "모델 응답의 문맥 선택을 검증하지 못했습니다.",
    "unknown_candidate": "모델이 알 수 없는 조건 후보를 선택했습니다.",
    "duplicate_candidate": "모델이 같은 조건 후보를 중복 선택했습니다.",
    "field_mismatch": "모델이 조건 후보를 다른 항목에 배정했습니다.",
    "incomplete_coverage": "모델 응답에서 조건 후보가 누락됐습니다.",
    "invalid_registry": "요청의 조건 근거를 검증하지 못했습니다.",
    "internal_error": "처리 중 오류가 발생해 추천을 반환하지 않습니다.",
    "request_deadline": "응답 기한을 초과해 추천을 반환하지 않습니다.",
}
CLARIFY = "필수 조건과 안내된 사유를 확인하고 완전한 요청을 다시 입력해 주세요."
EXPIRED = "자료가 만료되어 추천을 반환하지 않습니다."
DATA_MODES = ("synthetic", "observed")
_PREPARATION_KINDS = {
    **dict.fromkeys(
        (
            "invalid_text",
            "source_too_long",
            "invalid_unicode",
            "empty_source",
            "context_limit",
            "candidate_limit",
            "empty_context",
            "unrecognized_context",
            "amount_too_long",
            "empty_amount",
            "invalid_syntax",
            "invalid_number",
            "unit_order",
            "missing_unit",
            "missing_coefficient",
            "fractional_bp",
            "amount_out_of_range",
            "invalid_salary",
            "salary_out_of_range",
            "reversed_range",
            "invalid_reinforcement_syntax",
        ),
        "invalid",
    ),
    "unsupported_reinforcement": "unsupported",
    "unsupported_formation": "unsupported",
    "unsupported_club": "unsupported",
    "ambiguous_amount": "ambiguous",
}


def projection_spec():
    spec = dict(
        version=1,
        web_contract=WEB_CONTRACT,
        output=WebFinal.model_json_schema(),
        preparation_kinds=dict(_PREPARATION_KINDS),
        errors=dict(ERROR_MESSAGES),
        messages=dict(clarification=CLARIFY, expiry=EXPIRED),
        data_modes=list(DATA_MODES),
        presentation_version=presentation_spec()["presentation_version"],
        candidate_version=candidate_spec()["candidate_version"],
        selection_version=selection_spec()["selection_version"],
        final_expiry="after_dto_validation_and_serialization_before_returning_bytes",
        selection_provenance="explicit_execution_metadata_not_inferred_from_presentation",
    )
    return {**spec, "projection_version": hashlib.sha256(canonical(spec)).hexdigest()}


@dataclass(frozen=True)
class SelectionProvenance:
    model: str
    digest: str
    selector_version: str

    def __post_init__(self):
        if self.model != "qwen3:8b" or any(
            not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None
            for v in (self.digest, self.selector_version)
        ):
            raise ValueError("명시한 데모 모델 실행 출처가 필요합니다.")


@dataclass(frozen=True)
class WebOutcome:
    kind: str
    _payload: bytes
    _snapshot: RequestSnapshot | None = None

    def encode_payload(self, *, clock):
        if type(self._payload) is not bytes:
            raise TypeError("불변 웹 payload bytes가 필요합니다.")
        body = json.loads(self._payload)
        if self.kind == "error":
            if (
                set(body) != {"code", "message"}
                or body["code"] not in ERROR_MESSAGES
                or body["message"] != ERROR_MESSAGES[body["code"]]
            ):
                raise ValueError("안전한 기술 오류가 필요합니다.")
            return canonical(body)
        if self.kind != "final":
            raise ValueError("알 수 없는 종료 종류입니다.")
        body = WebFinal.model_validate(body).model_dump(mode="json")
        encoded = canonical(body)
        if self._snapshot is not None:
            if (
                body["snapshot"] is None
                or body["snapshot"]["id"] != self._snapshot.snapshot_id
            ):
                raise ValueError("웹 결과와 스냅샷이 일치하지 않습니다.")
            # DTO 검증/JSON 직렬화까지 끝낸 다음 응답 시계를 읽는다.
            checked = self._snapshot.check_response(clock())
            if checked["status"] == "data_expired":
                body.update(status="data_expired", squad=None, answer=EXPIRED)
                return canonical(WebFinal.model_validate(body).model_dump(mode="json"))
        elif body["status"] in ("optimal", "feasible"):
            raise ValueError("성공 결과의 원래 스냅샷이 없습니다.")
        return encoded

    def to_dict(self, *, clock):
        return json.loads(self.encode_payload(clock=clock))


def error_outcome(code="internal_error"):
    if code not in ERROR_MESSAGES:
        code = "internal_error"
    return WebOutcome("error", canonical(dict(code=code, message=ERROR_MESSAGES[code])))


def project_result(
    original,
    result,
    *,
    clock,
    provenance=None,
    material_id=None,
    data_mode="synthetic",
):
    """복구/모델 호출 없이 원문-준비-도메인 결과의 결합을 검사한다."""
    try:
        if data_mode not in DATA_MODES:
            raise ValueError("명시한 자료 모드가 필요합니다.")
        return _project(
            original,
            result,
            clock=clock,
            provenance=provenance,
            material_id=material_id,
            data_mode=data_mode,
        )
    except (ValueError, TypeError, KeyError, ValidationError):
        return error_outcome()


def _project(original, result, *, clock, provenance, material_id, data_mode):
    if (
        type(result) is not CandidateRunResult
        or result.adapter_version != candidate_agent_spec()["adapter_version"]
    ):
        raise ValueError("결과 wrapper가 일치하지 않습니다.")
    preparation = prepare_candidates(original)
    if preparation != result.preparation:
        raise ValueError("원문/준비 결과가 일치하지 않습니다.")
    versions = dict(
        web_contract=WEB_CONTRACT,
        projection=projection_spec()["projection_version"],
        preparation=preparation.candidate_version,
        selection="unused",
        adapter=result.adapter_version,
        normalizer="unused",
        graph="unused",
        engine="unused",
        presentation="unused",
        material="unused",
        model="unused",
        model_digest="unused",
        selector="unused",
    )
    if provenance is not None and type(provenance) is not SelectionProvenance:
        raise ValueError("실행 출처가 올바르지 않습니다.")
    if result.outcome == "input_issues":
        if (
            not preparation.issues
            or result.agent_result is not None
            or result.error is not None
            or provenance is not None
        ):
            raise ValueError("준비 실패의 실행 상태가 일치하지 않습니다.")
        issues = [
            dict(
                field=i.field,
                kind=_PREPARATION_KINDS[i.code],
                code=f"prepare.{i.code}",
                message=i.message,
                occurrence=None,
            )
            for i in preparation.issues
        ]
        body = dict(
            status="needs_clarification",
            conditions=None,
            issues=issues,
            supported=SupportedConditions().model_dump(mode="json"),
            squad=None,
            snapshot=None,
            versions=versions,
            answer=CLARIFY,
            narration_source="template",
            data_mode=data_mode,
        )
        return WebOutcome(
            "final", canonical(WebFinal.model_validate(body).model_dump(mode="json"))
        )
    if preparation.issues:
        raise ValueError("준비 차단 결과의 실행이 일치하지 않습니다.")
    if result.outcome == "provider_error":
        if result.agent_result is not None or result.error not in ERROR_MESSAGES:
            raise ValueError("선택 오류 결과가 일치하지 않습니다.")
        return error_outcome(result.error)
    if (
        result.outcome != "agent_result"
        or result.error is not None
        or result.agent_result is None
    ):
        raise ValueError("기존 Agent 결과가 없습니다.")
    agent = result.agent_result
    if (
        agent.graph_version != agent_spec()["graph_version"]
        or agent.normalization is None
    ):
        raise ValueError("그래프 출처가 일치하지 않습니다.")
    evidence = ExtractedConditions(
        **{
            field: tuple(
                v.span for v in preparation.registry.candidates if v.field == field
            )
            for field in FIELDS
        }
    )
    # 선택을 대신 수행하는 것이 아니라 이미 반환된 전체 근거의 일치만 대조한다.
    if agent.normalization != normalize_conditions(original, evidence):
        raise ValueError("요청의 전체 출현 근거가 일치하지 않습니다.")
    presented = present_agent_result(agent, clock=clock, data_mode=data_mode).to_dict()
    versions.update(
        selection=selection_spec()["selection_version"],
        graph=agent.graph_version,
        normalizer=presented["normalizer_version"] or "unused",
        engine=presented["engine_version"] or "unused",
        presentation=presented["presentation_version"],
    )
    if provenance is not None:
        versions.update(
            model=provenance.model,
            model_digest=provenance.digest,
            selector=provenance.selector_version,
        )
    snapshot = None
    if agent.build is not None:
        if (
            not isinstance(material_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", material_id) is None
        ):
            raise ValueError("명시 자료 ID가 필요합니다.")
        versions["material"] = material_id
        if presented["snapshot"] is not None:
            snapshot = agent.build.snapshot
    body = {
        field: presented[field]
        for field in (
            "status",
            "conditions",
            "issues",
            "supported",
            "squad",
            "snapshot",
            "answer",
            "narration_source",
            "data_mode",
        )
    }
    body["versions"] = versions
    if body["supported"] is None:
        body["supported"] = SupportedConditions().model_dump(mode="json")
    final = WebFinal.model_validate(body)
    return WebOutcome("final", canonical(final.model_dump(mode="json")), snapshot)
