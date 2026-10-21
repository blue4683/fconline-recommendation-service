"""서버 registry의 ID만 원문 근거로 복원한다. 자동 출현 선택/재호출은 없다."""

import hashlib
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field

from FRS.agent.candidates import (
    FIELDS,
    MAX_CANDIDATES,
    MAX_CONTEXTS,
    CandidateSet,
    candidate_spec,
    canonical,
    prepare_candidates,
)
from FRS.agent.extraction import ExtractionError
from FRS.agent.json_contract import strict_json
from FRS.core.request_normalization import ExtractedConditions

MAX_SELECTION_BYTES = 131072
ValueID = Annotated[str, Field(strict=True, pattern=r"^v-[0-9]{3}$")]
ContextID = Annotated[str, Field(strict=True, pattern=r"^ctx-[0-9]{3}$")]
IDs = Annotated[tuple[ValueID, ...], Field(max_length=MAX_CANDIDATES)]


class CandidateSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_set_id: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
    price_cap: IDs
    salary_cap: IDs
    team_color: IDs
    formation: IDs
    allowed_reinforcements: IDs
    unsupported: Annotated[tuple[ContextID, ...], Field(max_length=MAX_CONTEXTS)]


class CandidateSelector(Protocol):
    def select(self, candidates: CandidateSet) -> str: ...


class CandidateSelectionError(ExtractionError):
    def __init__(self, code):
        self.code = code
        super().__init__("후보 선택 결과를 검증하지 못했습니다.")


def selection_spec():
    spec = dict(
        version=1,
        protocol="candidate-selection.v1",
        candidate_version=candidate_spec()["candidate_version"],
        output=CandidateSelection.model_json_schema(),
        max_response_bytes=MAX_SELECTION_BYTES,
        registry="regenerate_from_unchanged_original_before_use",
        coverage="all_values_exactly_once_with_declared_field",
        unsupported="empty_for_ready_controlled_registry",
        repeated_order="preserve_original_occurrence_order_after_explicit_selection",
        retries=False,
        fallback=False,
    )
    return {**spec, "selection_version": hashlib.sha256(canonical(spec)).hexdigest()}


def checked_registry(original, registry):
    if type(registry) is not CandidateSet:
        raise CandidateSelectionError("invalid_registry")
    try:
        registry = CandidateSet.model_validate(registry.model_dump(warnings=False))
        expected = prepare_candidates(
            original, fixed_salary_cap=registry.fixed_salary_cap
        )
        if expected.issues or expected.registry != registry:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise CandidateSelectionError("invalid_registry") from exc
    return registry


def decode_selection(original, registry, raw: str) -> ExtractedConditions:
    registry = checked_registry(original, registry)
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_SELECTION_BYTES:
            raise ValueError
        selected = CandidateSelection.model_validate(strict_json(raw))
    except (ValueError, TypeError, RecursionError) as exc:
        raise CandidateSelectionError("invalid_selection") from exc
    if selected.candidate_set_id != registry.candidate_set_id:
        raise CandidateSelectionError("candidate_set_mismatch")
    if selected.unsupported:
        raise CandidateSelectionError("invalid_context_selection")
    known = {value.candidate_id: value for value in registry.candidates}
    seen = set()
    for field in FIELDS:
        for value_id in getattr(selected, field):
            if value_id not in known:
                raise CandidateSelectionError("unknown_candidate")
            if value_id in seen:
                raise CandidateSelectionError("duplicate_candidate")
            if known[value_id].field != field:
                raise CandidateSelectionError("field_mismatch")
            seen.add(value_id)
    if seen != set(known):
        raise CandidateSelectionError("incomplete_coverage")
    # 전 ID가 모델에 의해 명시 선택됐음을 확인한 뒤 원래 출현 순서로만 복원한다.
    return ExtractedConditions(
        **{
            field: tuple(
                value.span for value in registry.candidates if value.field == field
            )
            for field in FIELDS
        }
    )
