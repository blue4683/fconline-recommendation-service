"""검증된 스쿼드만 정확한 문자열 수치와 코드 템플릿으로 출력한다.

자료 모드(합성/관측)는 실행 서비스가 명시하며 후보 관측의 출처와 일치해야 한다.
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from FRS.agent.graph import AgentRunResult
from FRS.core.request_normalization import ConditionNormalizationResult
from FRS.core.squad_recommend.snapshot import RequestSnapshot
from FRS.core.squad_recommend.solver import EngineResult
from FRS.core.squad_recommend.validator import (
    SnapshotError,
    SnapshotExpired,
    SquadPick,
    SquadSelection,
    prepare_snapshot,
    validate_squad,
)
from FRS.schema.models import SourceKind
from FRS.schema.scope import FORMATION_SLOTS

_MESSAGES = {
    "needs_clarification": "필수 조건과 안내된 사유를 확인하고 완전한 요청을 다시 입력해 주세요.",
    "optimal": "합성 자료 기준으로 조건을 만족하는 11명을 검증했고 최적성을 확인했습니다.",
    "feasible": "합성 자료 기준으로 조건을 만족하는 11명을 검증했습니다. 최적성은 미확인입니다.",
    "infeasible": "보유 합성 자료 기준으로 조건을 만족하는 스쿼드가 없습니다.",
    "unknown": "시간 안에 해의 존재 여부를 확정하지 못했습니다.",
    "data_expired": "자료가 만료되어 추천을 반환하지 않습니다.",
    "internal_error": "처리 또는 검증 중 오류가 발생해 추천을 반환하지 않습니다.",
}
# 관측 자료 모드에서 자료 기준을 말하는 문구만 바꾼다. 나머지 상태 문구는 같다.
_OBSERVED_MESSAGES = {
    **_MESSAGES,
    "optimal": "넥슨 데이터센터 관측 자료 기준으로 조건을 만족하는 11명을 검증했고 최적성을 확인했습니다. 가격은 참고가격입니다.",
    "feasible": "넥슨 데이터센터 관측 자료 기준으로 조건을 만족하는 11명을 검증했습니다. 최적성은 미확인입니다. 가격은 참고가격입니다.",
    "infeasible": "보유 관측 자료 기준으로 조건을 만족하는 스쿼드가 없습니다.",
}
_PRICE_INCOMPLETE = " 동일 OVR 내 최대 예산 활용은 시간 내 확인하지 못했습니다."
_MODES = {
    "synthetic": (SourceKind.SYNTHETIC, _MESSAGES),
    "observed": (SourceKind.WEB, _OBSERVED_MESSAGES),
}


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def presentation_spec():
    spec = dict(
        version=1,
        scope="internal_presentation_not_http_contract",
        data_modes={mode: source.value for mode, (source, _) in _MODES.items()},
        numeric_values="BP_salary_and_identifiers_as_exact_decimal_strings",
        successful_rows="11_in_formation_order_after_independent_revalidation",
        conditions="same_normalized_conditions_and_snapshot",
        narrative="code_templates_only_no_llm_prose",
        expiry="response_clock_after_validation_and_table_construction",
        model_metadata="not_provided_by_this_internal_layer",
        messages=dict(_MESSAGES),
        observed_messages=dict(_OBSERVED_MESSAGES),
        price_optimization_incomplete=_PRICE_INCOMPLETE,
    )
    return {
        **spec,
        "presentation_version": hashlib.sha256(_canonical(spec)).hexdigest(),
    }


@dataclass(frozen=True)
class CodePresentation:
    _payload: bytes

    def __post_init__(self):
        if type(self._payload) is not bytes:
            raise TypeError("출력 자료는 불변 bytes로 고정해야 합니다.")

    def to_dict(self):
        return json.loads(self._payload)


def present_agent_result(
    result: AgentRunResult, *, clock, data_mode="synthetic"
) -> CodePresentation:
    if not isinstance(result, AgentRunResult):
        raise TypeError("내부 AgentRunResult가 필요합니다.")
    if data_mode not in _MODES:
        raise ValueError("명시한 자료 모드가 필요합니다.")
    source, messages = _MODES[data_mode]
    price_status = None
    payload = dict(
        kind="code_presentation",
        data_mode=data_mode,
        presentation_version=presentation_spec()["presentation_version"],
        graph_version=result.graph_version,
        normalizer_version=None,
        engine_version=None,
        conditions=None,
        issues=[],
        supported=None,
        squad=None,
        snapshot=None,
        evaluated_at=None,
        narration_source="template",
        model_metadata="not_provided",
    )

    def finish(status, code=None):
        answer = messages[status]
        if status == "optimal" and price_status == "incomplete":
            answer += _PRICE_INCOMPLETE
        body = {**payload, "status": status, "code": code, "answer": answer}
        if status not in ("optimal", "feasible"):
            body["squad"] = None
        return CodePresentation(_canonical(body))

    if result.error:
        return finish("internal_error", "extraction_error")
    try:
        if not isinstance(result.normalization, ConditionNormalizationResult):
            raise ValueError
        normalized = ConditionNormalizationResult.model_validate(
            result.normalization.model_dump()
        )
    except ValueError:
        return finish("internal_error", "invalid_normalization")
    payload["issues"] = [issue.model_dump(mode="json") for issue in normalized.issues]
    payload["normalizer_version"] = normalized.normalizer_version
    payload["supported"] = normalized.supported.model_dump(mode="json")
    if normalized.issues:
        if (
            normalized.status != "needs_clarification"
            or normalized.conditions is not None
        ):
            return finish("internal_error", "invalid_normalization")
        return finish("needs_clarification")
    if normalized.status != "ready" or normalized.conditions is None:
        return finish("internal_error", "invalid_normalization")
    values = normalized.conditions.model_dump(mode="json")
    payload["conditions"] = {
        **values,
        "price_cap": str(values["price_cap"]),
        "salary_cap": str(values["salary_cap"]),
    }
    build = result.build
    if build is None or build.normalization != normalized or build.engine is None:
        return finish("internal_error", "inconsistent_build")
    try:
        engine = EngineResult.model_validate(build.engine.model_dump())
        price_status = engine.price_optimization_status
        payload["engine_version"] = engine.engine_version
        snapshot = build.snapshot
        if (
            not isinstance(snapshot, RequestSnapshot)
            or engine.snapshot_id != snapshot.snapshot_id
            or engine.method != "cp_sat"
            or engine.status == "greedy_failed"
        ):
            raise ValueError
        if engine.status in ("internal_error", "data_expired"):
            return finish(engine.status)
        contents = snapshot.to_dict()
        t0 = datetime.fromisoformat(contents["query"]["t0"])
        prepared = prepare_snapshot(snapshot, when=t0)
        query = prepared.query.model_dump(mode="json")
        if any(query[key] != value for key, value in values.items()):
            raise ValueError
        if any(
            row.source != source
            for candidate in prepared.candidates
            for row in (
                candidate.observations.player_card,
                candidate.observations.card_salary,
                candidate.observations.card_price,
                candidate.observations.team_color,
                *(
                    (candidate.observations.reinforce_stats,)
                    if candidate.observations.reinforce_stats is not None
                    else ()
                ),
            )
        ):
            raise ValueError
        payload["snapshot"] = {
            "id": snapshot.snapshot_id,
            "t0": query["t0"],
            "valid_until": contents["metadata"]["valid_until"],
        }
        counts = Counter(candidate.slot for candidate in prepared.candidates)
        payload["coverage"] = [
            {"slot": slot, "count": counts[slot]}
            for slot in FORMATION_SLOTS[prepared.query.formation]
        ]
        if engine.status in ("optimal", "feasible"):
            if engine.validation is None or not engine.validation.valid:
                raise ValueError
            selection = SquadSelection(
                snapshot_id=snapshot.snapshot_id,
                picks=tuple(
                    SquadPick(
                        spid=p.spid, slot=p.slot, reinforce_level=p.reinforce_level
                    )
                    for p in engine.validation.players
                ),
            )
            report = validate_squad(snapshot, selection, when=t0)
            if not report.valid or report != engine.validation:
                raise ValueError
            by_slot = {player.slot: player for player in report.players}
            payload["squad"] = dict(
                rows=[
                    {
                        **by_slot[slot].model_dump(mode="json"),
                        "spid": str(by_slot[slot].spid),
                        "person_id": str(by_slot[slot].person_id),
                        "price": str(by_slot[slot].price),
                        "salary": str(by_slot[slot].salary),
                    }
                    for slot in FORMATION_SLOTS[prepared.query.formation]
                ],
                total_price=str(report.total_price),
                total_salary=str(report.total_salary),
                total_ovr=report.total_ovr,
                performance=report.performance,
            )
        elif engine.validation is not None:
            raise ValueError
    except SnapshotExpired:
        return finish("data_expired")
    except (ValueError, TypeError, KeyError, SnapshotError):
        payload["snapshot"] = None
        return finish("internal_error", "inconsistent_snapshot_or_validation")
    if not callable(clock):
        raise TypeError("출력 직전 응답 시계를 명시해야 합니다.")
    # 자료 재검증·합계 대조·표 구성을 마친 뒤에만 최종 기한을 확인한다.
    checked = snapshot.check_response(clock())
    payload["evaluated_at"] = checked["evaluated_at"]
    if checked["status"] == "data_expired":
        return finish("data_expired")
    return finish(engine.status)
