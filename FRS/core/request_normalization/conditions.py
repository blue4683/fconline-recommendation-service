"""명시한 원문 근거만으로 필수 다섯 조건을 검증한다. 추출·I/O·스쿼드 실행은 없다."""

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from FRS.core.request_normalization.amounts import (
    MAX_BP,
    MAX_SOURCE_LENGTH,
    NormalizationError,
    SourceSpan,
    normalization_spec,
    parse_amount,
    validate_source_span,
)
from FRS.schema.models import NonNegative
from FRS.schema.scope import (
    FORMATION_SLOTS,
    PRIORITY_CLUBS,
    SUPPORTED_REINFORCEMENTS,
)

_FIELDS = (
    "price_cap",
    "salary_cap",
    "team_color",
    "formation",
    "allowed_reinforcements",
)
_INTEGER = re.compile(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\Z")
_LEVEL = re.compile(r"([0-9]+)\s*강\Z")
_RANGE = re.compile(r"([0-9]+)\s*~\s*([0-9]+)\s*강\Z")
MAX_SALARY = 2**63 - 1


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractedConditions(_FrozenModel):
    """필드별 모든 출현 근거. 빈 목록은 미확인/누락이며 기본 조건이 아니다."""

    price_cap: tuple[SourceSpan, ...] = ()
    salary_cap: tuple[SourceSpan, ...] = ()
    team_color: tuple[SourceSpan, ...] = ()
    formation: tuple[SourceSpan, ...] = ()
    allowed_reinforcements: tuple[SourceSpan, ...] = ()
    unsupported: tuple[SourceSpan, ...] = ()


class NormalizedConditions(_FrozenModel):
    price_cap: Annotated[int, Field(strict=True, ge=0, le=MAX_BP)]
    salary_cap: NonNegative
    team_color: Annotated[str, Field(strict=True)]
    formation: Annotated[str, Field(strict=True)]
    allowed_reinforcements: tuple[Annotated[int, Field(strict=True)], ...]

    @field_validator("team_color")
    @classmethod
    def supported_club(cls, value):
        if value not in PRIORITY_CLUBS:
            raise ValueError("정식 지원 클럽이 필요합니다.")
        return value

    @field_validator("formation")
    @classmethod
    def supported_formation(cls, value):
        if value not in FORMATION_SLOTS:
            raise ValueError("정식 지원 포메이션이 필요합니다.")
        return value

    @field_validator("allowed_reinforcements")
    @classmethod
    def supported_levels(cls, value):
        if (
            not value
            or value != tuple(sorted(set(value)))
            or any(level not in SUPPORTED_REINFORCEMENTS for level in value)
        ):
            raise ValueError("5~11의 정렬된 중복 없는 강화 집합이 필요합니다.")
        return value


class SupportedConditions(_FrozenModel):
    required_fields: tuple[str, ...] = _FIELDS
    clubs: tuple[str, ...] = PRIORITY_CLUBS
    formations: tuple[str, ...] = tuple(FORMATION_SLOTS)
    reinforcements: tuple[int, ...] = SUPPORTED_REINFORCEMENTS


class ConditionIssue(_FrozenModel):
    field: str
    kind: Literal["missing", "invalid", "unsupported", "ambiguous"]
    code: str
    message: str
    occurrence: NonNegative | None = None


class ConditionNormalizationResult(_FrozenModel):
    status: Literal["ready", "needs_clarification"]
    conditions: NormalizedConditions | None
    issues: tuple[ConditionIssue, ...]
    evidence: ExtractedConditions
    supported: SupportedConditions
    normalizer_version: str


class ConditionValueError(ValueError):
    """원문을 복사하지 않는 필드 문법/미지원 오류."""

    def __init__(self, code, message, kind="invalid"):
        self.code, self.kind = code, kind
        super().__init__(message)


def _text(value):
    if not isinstance(value, str):
        raise ConditionValueError("invalid_text", "문자열 입력이 필요합니다.")
    if len(value) > MAX_SOURCE_LENGTH:
        raise ConditionValueError("source_too_long", "원문 길이 한도를 초과했습니다.")
    return value.strip()


def parse_salary(text: str) -> int:
    value = _text(text)
    if _INTEGER.fullmatch(value) is None:
        raise ConditionValueError(
            "invalid_salary", "급여는 올바른 쉼표를 쓰는 비음수 정수여야 합니다."
        )
    digits = value.replace(",", "").lstrip("0") or "0"
    bound = str(MAX_SALARY)
    if len(digits) > len(bound) or (len(digits) == len(bound) and digits > bound):
        raise ConditionValueError(
            "salary_out_of_range", "급여가 정수 범위를 벗어났습니다."
        )
    return int(digits)


def _level(digits):
    digits = digits.lstrip("0") or "0"
    if len(digits) > 2 or int(digits) not in SUPPORTED_REINFORCEMENTS:
        raise ConditionValueError(
            "unsupported_reinforcement", "강화는 5~11만 지원합니다.", "unsupported"
        )
    return int(digits)


def parse_reinforcements(text: str) -> tuple[int, ...]:
    value = _text(text)
    interval = _RANGE.fullmatch(value)
    if interval is not None:
        lower, upper = (_level(digits) for digits in interval.groups())
        if lower > upper:
            raise ConditionValueError(
                "reversed_range", "강화 범위는 오름차순이어야 합니다."
            )
        return tuple(range(lower, upper + 1))
    levels = []
    for item in value.split(","):
        match = _LEVEL.fullmatch(item.strip())
        if match is None:
            raise ConditionValueError(
                "invalid_reinforcement_syntax",
                "강화의 단일·범위·목록 문법을 확인해야 합니다.",
            )
        levels.append(_level(match.group(1)))
    return tuple(sorted(set(levels)))


def _exact(text, supported, code, message):
    value = _text(text)
    if value not in supported:
        raise ConditionValueError(code, message, "unsupported")
    return value


def conditions_spec():
    spec = dict(
        version=1,
        amount_normalizer=normalization_spec()["normalizer_version"],
        source_length=MAX_SOURCE_LENGTH,
        required_fields=list(_FIELDS),
        salary=dict(
            grammar="ASCII integer or valid three-digit comma groups", max=MAX_SALARY
        ),
        clubs=list(PRIORITY_CLUBS),
        formations=list(FORMATION_SLOTS),
        reinforcements=list(SUPPORTED_REINFORCEMENTS),
        reinforcement_grammar=["N강", "N~M강", "N강,M강,..."],
        range_inclusive=True,
        duplicate_levels="sorted_unique_within_one_occurrence",
        repeated_fields="same_canonical_value_only_preserve_all_spans",
        conflicting_fields="report_without_last_value_or_union",
        whitespace="trim_edges_and_around_reinforcement_delimiters_after_span_check",
        aliases=False,
        defaults=False,
        any_issue="no_completed_conditions",
        evidence="only_exact_original_spans_including_invalid_values",
        scope="manual_or_synthetic_extraction_only_no_io_or_snapshot",
    )
    encoded = json.dumps(
        spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return {**spec, "normalizer_version": hashlib.sha256(encoded.encode()).hexdigest()}


def normalize_conditions(original: str, extracted: ExtractedConditions):
    """검증된 근거와 모든 사유를 반환한다. 원문 전체의 의미를 추출/보증하지 않는다."""
    issues, verified, values = [], {}, {}

    def issue(field, kind, code, message, occurrence=None):
        issues.append(
            ConditionIssue(
                field=field,
                kind=kind,
                code=code,
                message=message,
                occurrence=occurrence,
            )
        )

    def result():
        return ConditionNormalizationResult(
            status="needs_clarification" if issues else "ready",
            conditions=None if issues else NormalizedConditions(**values),
            issues=tuple(issues),
            evidence=ExtractedConditions(**verified),
            supported=SupportedConditions(),
            normalizer_version=conditions_spec()["normalizer_version"],
        )

    if not isinstance(original, str) or len(original) > MAX_SOURCE_LENGTH:
        code = "invalid_text" if not isinstance(original, str) else "source_too_long"
        issue("original", "invalid", code, str(NormalizationError(code)))
        return result()
    try:
        if not isinstance(extracted, ExtractedConditions):
            raise ValueError
        extracted = ExtractedConditions.model_validate(
            extracted.model_dump(warnings=False)
        )
    except ValueError:
        issue(
            "extraction",
            "invalid",
            "invalid_extraction",
            "추출 결과의 구조가 올바르지 않습니다.",
        )
        return result()

    # 모든 구간을 먼저 대조한다. 불일치 근거를 파싱하거나 다른 출현 위치로 바꾸지 않는다.
    occurrences = {}
    for field in (*_FIELDS, "unsupported"):
        verified[field], occurrences[field] = [], []
        spans = getattr(extracted, field)
        if field in _FIELDS and not spans:
            issue(
                field,
                "missing",
                "missing_condition",
                "필수 조건을 원문에 명시해야 합니다.",
            )
        for index, span in enumerate(spans):
            try:
                valid = validate_source_span(original, span)
            except NormalizationError as error:
                issue(field, "invalid", error.code, str(error), index)
                continue
            verified[field].append(valid)
            occurrences[field].append((index, valid))

    parsers = {
        "price_cap": parse_amount,
        "salary_cap": parse_salary,
        "team_color": lambda text: _exact(
            text,
            PRIORITY_CLUBS,
            "unsupported_club",
            "정식 지원 클럽 이름을 사용해야 합니다.",
        ),
        "formation": lambda text: _exact(
            text,
            FORMATION_SLOTS,
            "unsupported_formation",
            "정식 지원 포메이션을 사용해야 합니다.",
        ),
        "allowed_reinforcements": parse_reinforcements,
    }
    for field in _FIELDS:
        parsed = []
        for index, span in occurrences[field]:
            try:
                parsed.append(parsers[field](span.text))
            except NormalizationError as error:
                kind = "ambiguous" if error.code == "ambiguous_amount" else "invalid"
                issue(field, kind, error.code, str(error), index)
            except ConditionValueError as error:
                issue(field, error.kind, error.code, str(error), index)
        if parsed:
            if any(value != parsed[0] for value in parsed[1:]):
                issue(
                    field,
                    "ambiguous",
                    "conflicting_values",
                    "같은 조건의 여러 값이 충돌합니다.",
                )
            else:
                values[field] = parsed[0]
    for index, _span in occurrences["unsupported"]:
        issue(
            "unsupported",
            "unsupported",
            "unsupported_condition",
            "현재 지원 범위에 없는 조건이 있습니다.",
            index,
        )
    return result()
