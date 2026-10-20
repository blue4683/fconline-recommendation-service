"""외부 추출기와 분리한 순수 원문·금액·필수 조건 검증 계약."""

from FRS.core.request_normalization.amounts import (
    NormalizationError,
    NormalizedAmount,
    SourceSpan,
    normalization_spec,
    normalize_amount,
    parse_amount,
    validate_source_span,
)
from FRS.core.request_normalization.conditions import (
    ConditionIssue,
    ConditionNormalizationResult,
    ConditionValueError,
    ExtractedConditions,
    NormalizedConditions,
    SupportedConditions,
    conditions_spec,
    normalize_conditions,
    parse_reinforcements,
    parse_salary,
)

__all__ = [
    "ConditionIssue",
    "ConditionNormalizationResult",
    "ConditionValueError",
    "ExtractedConditions",
    "NormalizedConditions",
    "SupportedConditions",
    "conditions_spec",
    "normalize_conditions",
    "parse_reinforcements",
    "parse_salary",
    "NormalizationError",
    "NormalizedAmount",
    "SourceSpan",
    "normalization_spec",
    "normalize_amount",
    "parse_amount",
    "validate_source_span",
]
