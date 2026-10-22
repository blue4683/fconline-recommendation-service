"""정형 조건문의 원문 후보와 문맥을 고정한다. 모델·I/O·조건 보충은 없다."""

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from FRS.core.request_normalization import (
    ExtractedConditions,
    SourceSpan,
    conditions_spec,
    normalize_conditions,
)
from FRS.core.request_normalization.amounts import MAX_SOURCE_LENGTH
from FRS.core.request_normalization.conditions import validate_salary_policy
from FRS.schema.scope import FORMATION_SLOTS, PRIORITY_CLUBS

MAX_CANDIDATES = 128
MAX_CONTEXTS = 64
FIELDS = (
    "price_cap",
    "salary_cap",
    "team_color",
    "formation",
    "allowed_reinforcements",
)
CandidateField = Literal[
    "price_cap", "salary_cap", "team_color", "formation", "allowed_reinforcements"
]
_DIGITS = "0123456789"
_PREFIX = re.compile(r"\s*(예산|급여)\s+")
_LEVEL_START = re.compile(r"\s*[0-9]+(?:\s*~\s*[0-9]+)?\s*강")
_FORMATION_START = re.compile(r"\s*[0-9]+(?:-[0-9]+){2,4}(?![0-9-])")
_SUFFIX = re.compile(r"(?:으로|로) 구성해줘\Z")


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def candidate_spec(*, fixed_salary_cap=None):
    spec = dict(
        version=1,
        source_encoding="utf-8",
        max_source_length=MAX_SOURCE_LENGTH,
        max_values=MAX_CANDIDATES,
        max_contexts=MAX_CONTEXTS,
        normalization_version=conditions_spec(fixed_salary_cap=fixed_salary_cap)[
            "normalizer_version"
        ],
        fields=FIELDS,
        separator="comma_with_numeric_and_reinforcement_list_protection",
        prefixes=["예산", "급여"],
        patterns=dict(
            prefix=_PREFIX.pattern,
            levels=_LEVEL_START.pattern,
            formation=_FORMATION_START.pattern,
            suffix=_SUFFIX.pattern,
        ),
        order="any",
        aliases=False,
        suffixes=["으로 구성해줘", "로 구성해줘"],
        final_period=True,
        unknown="whole_input_clarification_before_selector",
        invalid_value="existing_parser_reason_before_selector",
        repeated="occurrence_ids_without_last_value_selection",
        coverage="all_prepared_values_once_in_declared_fields",
        scope="controlled_internal_preparation_not_free_korean_or_http",
    )
    return {**spec, "candidate_version": hashlib.sha256(canonical(spec)).hexdigest()}


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PreparationIssue(_Frozen):
    code: str
    message: str
    field: str = "original"
    context_id: str | None = None
    span: SourceSpan | None = None


class ValueCandidate(_Frozen):
    candidate_id: Annotated[str, Field(strict=True, pattern=r"^v-[0-9]{3}$")]
    context_id: Annotated[str, Field(strict=True, pattern=r"^ctx-[0-9]{3}$")]
    field: CandidateField
    span: SourceSpan


class CandidateContext(_Frozen):
    context_id: Annotated[str, Field(strict=True, pattern=r"^ctx-[0-9]{3}$")]
    span: SourceSpan
    candidate_id: str | None
    suffix: SourceSpan | None = None


class CandidateSet(_Frozen):
    fixed_salary_cap: Literal[310] | None = None
    original: Annotated[
        str, Field(strict=True, min_length=1, max_length=MAX_SOURCE_LENGTH)
    ]
    candidate_version: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
    candidates: Annotated[tuple[ValueCandidate, ...], Field(max_length=MAX_CANDIDATES)]
    contexts: Annotated[tuple[CandidateContext, ...], Field(max_length=MAX_CONTEXTS)]

    @property
    def source_sha256(self):
        return hashlib.sha256(self.original.encode()).hexdigest()

    @property
    def candidate_set_id(self):
        data = self.model_dump(mode="json")
        if self.fixed_salary_cap is None:
            data.pop("fixed_salary_cap")
        return hashlib.sha256(canonical(data)).hexdigest()


class CandidatePreparation(_Frozen):
    candidate_version: str
    registry: CandidateSet | None
    issues: tuple[PreparationIssue, ...]

    @property
    def status(self):
        return "needs_clarification" if self.issues else "ready"


def _span(original, start, end):
    return SourceSpan(start=start, end=end, text=original[start:end])


def _trim(original, start, end):
    while start < end and original[start].isspace():
        start += 1
    while end > start and original[end - 1].isspace():
        end -= 1
    return start, end


def _segments(original):
    """분리만 수행한다. 숫자 쉼표의 문법 검사/강화 해석은 기존 파서가 맡는다."""
    start, segments = 0, []
    numeric, level = bool(_PREFIX.match(original)), bool(_LEVEL_START.match(original))
    for index, char in enumerate(original):
        if char != ",":
            continue
        next_level = _LEVEL_START.match(original, index + 1)
        next_formation = _FORMATION_START.match(original, index + 1)
        number_comma = (
            numeric
            and index > start
            and index + 1 < len(original)
            and original[index - 1] in _DIGITS
            and original[index + 1] in _DIGITS
            and not next_level
            and not next_formation
        )
        if number_comma or (level and next_level):
            continue
        segments.append((start, index))
        if len(segments) >= MAX_CONTEXTS:
            return None
        start = index + 1
        numeric = bool(_PREFIX.match(original, start))
        level = bool(_LEVEL_START.match(original, start))
    segments.append((start, len(original)))
    return segments


def _field(original, start, end):
    text = original[start:end]
    prefix = _PREFIX.match(original, start)
    if prefix is not None and prefix.end() <= end:
        return ("price_cap" if prefix[1] == "예산" else "salary_cap"), prefix.end(), end
    if text in PRIORITY_CLUBS:
        return "team_color", start, end
    if text in FORMATION_SLOTS or re.fullmatch(r"[0-9]+(?:-[0-9]+){2,4}", text):
        return "formation", start, end
    if text and text[0] in _DIGITS and text.endswith("강"):
        return "allowed_reinforcements", start, end
    return None, start, end


def prepare_candidates(original, *, fixed_salary_cap=None) -> CandidatePreparation:
    validate_salary_policy(fixed_salary_cap)
    version = candidate_spec(fixed_salary_cap=fixed_salary_cap)["candidate_version"]

    def early(code, message):
        return CandidatePreparation(
            candidate_version=version,
            registry=None,
            issues=(PreparationIssue(code=code, message=message),),
        )

    if not isinstance(original, str):
        return early("invalid_text", "문자열 원문이 필요합니다.")
    if len(original) > MAX_SOURCE_LENGTH:
        return early("source_too_long", "원문 길이 한도를 초과했습니다.")
    try:
        original.encode("utf-8")
    except UnicodeError:
        return early("invalid_unicode", "원문 문자 인코딩을 확인해 주세요.")
    if not original.strip():
        return early("empty_source", "조건을 원문에 명시해야 합니다.")
    segments = _segments(original)
    if segments is None:
        return early(
            "context_limit", "문맥 수 한도를 초과해 입력 전체를 처리하지 않습니다."
        )
    values, contexts, issues = [], [], []
    for index, (start, end) in enumerate(segments):
        start, end = _trim(original, start, end)
        context_id = f"ctx-{index + 1:03d}"
        if start == end:
            issues.append(
                PreparationIssue(
                    code="empty_context",
                    context_id=context_id,
                    message="비어 있는 조건 절을 확인해 주세요.",
                )
            )
            continue
        context_span = _span(original, start, end)
        body_end = end
        if index == len(segments) - 1:
            if original[body_end - 1] == ".":
                body_end -= 1
            _, body_end = _trim(original, start, body_end)
            suffix = _SUFFIX.search(original[start:body_end])
            if suffix is not None:
                body_end = start + suffix.start()
        body_start, body_end = _trim(original, start, body_end)
        suffix_span = _span(original, body_end, end) if body_end < end else None
        field, value_start, value_end = _field(original, body_start, body_end)
        value_start, value_end = _trim(original, value_start, value_end)
        candidate_id = None
        if field is None or value_start == value_end:
            issues.append(
                PreparationIssue(
                    code="unrecognized_context",
                    field=field or "original",
                    context_id=context_id,
                    span=context_span,
                    message="해석되지 않은 절이 있습니다. 정형 조건문으로 다시 입력해 주세요.",
                )
            )
        else:
            candidate_id = f"v-{len(values) + 1:03d}"
            span = _span(original, value_start, value_end)
            values.append(
                ValueCandidate(
                    candidate_id=candidate_id,
                    context_id=context_id,
                    field=field,
                    span=span,
                )
            )
            parsed = normalize_conditions(
                original,
                ExtractedConditions(**{field: (span,)}),
                fixed_salary_cap=fixed_salary_cap,
            )
            issues.extend(
                PreparationIssue(
                    code=issue.code,
                    message=issue.message,
                    field=field,
                    context_id=context_id,
                    span=span,
                )
                for issue in parsed.issues
                if issue.field == field
            )
        contexts.append(
            CandidateContext(
                context_id=context_id,
                span=context_span,
                candidate_id=candidate_id,
                suffix=suffix_span,
            )
        )
        if len(values) > MAX_CANDIDATES:
            return early(
                "candidate_limit",
                "후보 수 한도를 초과해 입력 전체를 처리하지 않습니다.",
            )
    registry = CandidateSet(
        fixed_salary_cap=fixed_salary_cap,
        original=original,
        candidate_version=version,
        candidates=tuple(values),
        contexts=tuple(contexts),
    )
    return CandidatePreparation(
        candidate_version=version, registry=registry, issues=tuple(issues)
    )
