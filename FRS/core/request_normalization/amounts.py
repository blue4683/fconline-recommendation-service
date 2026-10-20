"""원문 근거를 보존하고 명시된 금액만 정확한 정수 BP로 변환한다. I/O는 없다."""

import hashlib
import json
import re
from fractions import Fraction
from types import MappingProxyType
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_SOURCE_LENGTH = 4000
MAX_AMOUNT_LENGTH = 100
MAX_BP = 10**34 - 1
_SMALL = MappingProxyType({"십": 10, "백": 100, "천": 1000})
_LARGE = MappingProxyType(
    {"만": 10**4, "억": 10**8, "조": 10**12, "경": 10**16, "BP": 1}
)
_TOKEN = re.compile(r"[0-9]+(?:,[0-9]+)*(?:\.[0-9]+)?|[십백천만억조경]|BP")
_NUMBER = re.compile(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?\Z")
_MESSAGES = MappingProxyType(
    {
        "invalid_text": "문자열 입력이 필요합니다.",
        "source_too_long": "원문 길이 한도를 초과했습니다.",
        "invalid_span": "원문 구간 형식이 올바르지 않습니다.",
        "span_out_of_bounds": "원문 구간이 문자열 범위를 벗어났습니다.",
        "span_mismatch": "원문 구간과 근거 문자열이 일치하지 않습니다.",
        "amount_too_long": "금액 길이 한도를 초과했습니다.",
        "empty_amount": "금액이 비어 있습니다.",
        "ambiguous_amount": "명확한 금액 하나를 다시 입력해야 합니다.",
        "invalid_syntax": "지원하지 않는 금액 문법입니다.",
        "invalid_number": "숫자 또는 쉼표 묶음이 올바르지 않습니다.",
        "unit_order": "단위는 중복 없이 내림차순이어야 합니다.",
        "missing_unit": "BP 또는 큰 금액 단위를 명시해야 합니다.",
        "missing_coefficient": "단위 앞의 숫자를 명시해야 합니다.",
        "fractional_bp": "금액을 정수 BP로 정확히 표현할 수 없습니다.",
        "amount_out_of_range": "금액이 지원하는 BP 범위를 벗어났습니다.",
    }
)


class NormalizationError(ValueError):
    """호출자가 되묻기 사유로 사용할 수 있는 코드. 원문을 오류 메시지에 넣지 않는다."""

    def __init__(self, code):
        self.code = code
        super().__init__(_MESSAGES[code])


class SourceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start: Annotated[int, Field(strict=True, ge=0, le=MAX_SOURCE_LENGTH)]
    end: Annotated[int, Field(strict=True, ge=1, le=MAX_SOURCE_LENGTH)]
    text: Annotated[str, Field(strict=True, min_length=1, max_length=MAX_SOURCE_LENGTH)]

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("end는 start보다 뒤여야 합니다.")
        return self


class NormalizedAmount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    span: SourceSpan
    bp: Annotated[int, Field(strict=True, ge=0, le=MAX_BP)]
    normalizer_version: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def normalization_spec():
    spec = dict(
        version=1,
        source_length=MAX_SOURCE_LENGTH,
        amount_length=MAX_AMOUNT_LENGTH,
        index="unicode_codepoint_zero_based_end_exclusive",
        source_rewrite=False,
        max_bp=MAX_BP,
        small_units=dict(_SMALL),
        large_units=dict(_LARGE),
        numbers="ASCII decimal with optional valid three-digit comma groups",
        coefficient="explicit numeric coefficients with descending unique small units",
        unit_order="descending_unique_large_units_with_final_explicit_BP_if_needed",
        numeric_only="reject_missing_unit",
        implicit_units=False,
        arithmetic="exact_string_fraction_sum_then_integer_check",
        rounding=False,
        unsupported_tokens="reject_without_partial_parse",
        scope="one_amount_span_only",
    )
    encoded = json.dumps(
        spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return {**spec, "normalizer_version": hashlib.sha256(encoded).hexdigest()}


def validate_source_span(original: str, span: SourceSpan):
    if not isinstance(original, str):
        raise NormalizationError("invalid_text")
    if len(original) > MAX_SOURCE_LENGTH:
        raise NormalizationError("source_too_long")
    if not isinstance(span, SourceSpan):
        raise NormalizationError("invalid_span")
    try:
        span = SourceSpan.model_validate(span.model_dump(warnings=False))
    except ValidationError:
        raise NormalizationError("invalid_span") from None
    if span.end > len(original):
        raise NormalizationError("span_out_of_bounds")
    if original[span.start : span.end] != span.text:
        raise NormalizationError("span_mismatch")
    return span


def _tokens(text):
    result, index = [], 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        match = _TOKEN.match(text, index)
        if match is None:
            raise NormalizationError("invalid_syntax")
        token = match.group()
        if token[0] in "0123456789" and _NUMBER.fullmatch(token) is None:
            raise NormalizationError("invalid_number")
        result.append(token)
        index = match.end()
    return result


def _coefficient(tokens):
    if not tokens:
        raise NormalizationError("missing_coefficient")
    total, index, last_small = Fraction(0), 0, 10_000
    while index < len(tokens):
        number = tokens[index]
        if _NUMBER.fullmatch(number) is None:
            raise NormalizationError("missing_coefficient")
        value = Fraction(number.replace(",", ""))
        index += 1
        if index < len(tokens) and tokens[index] in _SMALL:
            factor = _SMALL[tokens[index]]
            if factor >= last_small:
                raise NormalizationError("unit_order")
            total += value * factor
            last_small = factor
            index += 1
        else:
            if index != len(tokens):
                raise NormalizationError("invalid_syntax")
            total += value
    return total


def parse_amount(text: str) -> int:
    """승인 문법의 전체 문자열을 소비한다. 숫자/단위/정밀도를 보충하거나 버리지 않는다."""
    if not isinstance(text, str):
        raise NormalizationError("invalid_text")
    if len(text) > MAX_AMOUNT_LENGTH:
        raise NormalizationError("amount_too_long")
    if not text.strip():
        raise NormalizationError("empty_amount")
    if any(marker in text for marker in ("약", "내외", "~", "～", "∼", "-", "또는")):
        raise NormalizationError("ambiguous_amount")
    tokens = _tokens(text)
    total, group, last_large = Fraction(0), [], 10**20
    for token in tokens:
        if token in _LARGE:
            factor = _LARGE[token]
            if factor >= last_large:
                raise NormalizationError("unit_order")
            total += _coefficient(group) * factor
            last_large, group = factor, []
        else:
            group.append(token)
    if group or last_large == 10**20:
        raise NormalizationError("missing_unit")
    if total.denominator != 1:
        raise NormalizationError("fractional_bp")
    if not 0 <= total.numerator <= MAX_BP:
        raise NormalizationError("amount_out_of_range")
    return total.numerator


def normalize_amount(original: str, span: SourceSpan):
    verified = validate_source_span(original, span)
    return NormalizedAmount(
        span=verified,
        bp=parse_amount(verified.text),
        normalizer_version=normalization_spec()["normalizer_version"],
    )
