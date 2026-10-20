"""실제 요청/LLM 없이 정확한 BP·원문 근거·거부 경계를 검증한다."""

from fractions import Fraction
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from FRS.core.request_normalization import (
    NormalizationError,
    SourceSpan,
    normalization_spec,
    normalize_amount,
    parse_amount,
    validate_source_span,
)


@pytest.mark.parametrize(
    "text,bp",
    [
        ("1조 5천억", 1500000000000),
        ("1.5조", 1500000000000),
        ("150,000BP", 150000),
        ("1천2백34억", 123400000000),
        ("1경 2조 3억 4만 5BP", 10**16 + 2 * 10**12 + 3 * 10**8 + 4 * 10**4 + 5),
        ("1조5천BP", 10**12 + 5000),
        ("0.00000001억", 1),
        ("0.000000000001조", 1),
        ("0.0000000000001조 0.9BP", 1),
        ("1,234.5만", 12345000),
        ("1.5천억", 150000000000),
        (" 1조\t5천억\n", 1500000000000),
        ("1조\u00a05천억", 1500000000000),
        ("0BP", 0),
        ("000BP", 0),
        ("0.0조", 0),
        ("9999999999999999999999999999999999BP", 10**34 - 1),
        ("9007199254740993BP", 9007199254740993),
    ],
)
def test_exact_supported_amounts_without_float_rounding(text, bp):
    assert parse_amount(text) == bp and type(parse_amount(text)) is int


@pytest.mark.parametrize(
    "text,code",
    [
        ("1000", "missing_unit"),
        ("1조5000", "missing_unit"),
        ("1천", "missing_unit"),
        ("1조1조", "unit_order"),
        ("1억1조", "unit_order"),
        ("1BP1억", "unit_order"),
        ("1천1천억", "unit_order"),
        ("1백1천억", "unit_order"),
        ("1,00억", "invalid_number"),
        ("1234,567BP", "invalid_number"),
        ("1.2BP", "fractional_bp"),
        ("0.0000000000001조", "fractional_bp"),
        ("10000000000000000000000000000000000BP", "amount_out_of_range"),
        ("1조~2조", "ambiguous_amount"),
        ("약1조", "ambiguous_amount"),
        ("1조 내외", "ambiguous_amount"),
        ("1조 또는 2조", "ambiguous_amount"),
        ("-1조", "ambiguous_amount"),
        ("1 2조", "invalid_syntax"),
        ("일조", "invalid_syntax"),
        ("천억", "missing_coefficient"),
        ("조", "missing_coefficient"),
        ("1조억", "missing_coefficient"),
        ("1억원", "invalid_syntax"),
        ("1e3BP", "invalid_syntax"),
        ("1/2BP", "invalid_syntax"),
        ("1_000BP", "invalid_syntax"),
        ("NaNBP", "invalid_syntax"),
        ("InfinityBP", "invalid_syntax"),
        ("+1조", "invalid_syntax"),
        ("1.조", "invalid_syntax"),
        (".5조", "invalid_syntax"),
        ("１조", "invalid_syntax"),
        ("١조", "invalid_syntax"),
        ("1조 미지원접미사", "invalid_syntax"),
        ("1조\u200b5천억", "invalid_syntax"),
        ("", "empty_amount"),
        (" \t\n", "empty_amount"),
    ],
)
def test_ambiguous_missing_or_unsupported_syntax_is_never_partially_accepted(
    text, code
):
    with pytest.raises(NormalizationError) as caught:
        parse_amount(text)
    assert caught.value.code == code


@pytest.mark.parametrize("value", [None, 1, 1.5, True, b"1BP"])
def test_non_string_amount_is_rejected(value):
    with pytest.raises(NormalizationError) as caught:
        parse_amount(value)
    assert caught.value.code == "invalid_text"


def test_length_boundaries_do_not_truncate_or_compute_overlong_text():
    assert parse_amount(" " * 97 + "1BP") == 1
    with patch(
        "FRS.core.request_normalization.amounts._tokens",
        side_effect=AssertionError("파싱 전 거부"),
    ):
        with pytest.raises(NormalizationError) as caught:
            parse_amount(" " * 98 + "1BP")
    assert caught.value.code == "amount_too_long"
    text = "가" * 3997 + "1BP"
    span = SourceSpan(start=3997, end=4000, text="1BP")
    assert normalize_amount(text, span).bp == 1
    with pytest.raises(NormalizationError) as caught:
        normalize_amount(text + "가", span)
    assert caught.value.code == "source_too_long"


def test_unicode_codepoint_offsets_and_raw_whitespace_are_preserved():
    original = "⚽ 예산:  1조 5천억  "
    text = "  1조 5천억  "
    start = original.index(text)
    span = SourceSpan(start=start, end=start + len(text), text=text)
    result = normalize_amount(original, span)
    assert result.bp == 1500000000000 and result.span == span
    assert (
        result.span.text == text
        and original[result.span.start : result.span.end] == text
    )
    assert result.normalizer_version == normalization_spec()["normalizer_version"]


@pytest.mark.parametrize(
    "span,code",
    [
        (SourceSpan(start=0, end=3, text="1BP"), "span_mismatch"),
        (SourceSpan(start=3, end=7, text="1BP"), "span_out_of_bounds"),
        (SourceSpan(start=2, end=5, text="2BP"), "span_mismatch"),
    ],
)
def test_span_mismatch_or_out_of_bounds_is_not_repaired_by_search(span, code):
    with pytest.raises(NormalizationError) as caught:
        normalize_amount("예산1BP", span)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "updates",
    [
        {"start": True},
        {"start": -1},
        {"end": 0},
        {"start": 3, "end": 3},
        {"end": 4001},
        {"start": "0"},
        {"text": ""},
        {"text": 1},
        {"extra": "ignored"},
    ],
)
def test_invalid_span_structure_is_rejected(updates):
    with pytest.raises(ValidationError):
        SourceSpan(**{"start": 0, "end": 3, "text": "1BP", **updates})


def test_frozen_model_bypass_is_revalidated_and_dict_input_not_silently_accepted():
    bad = SourceSpan.model_construct(start=-1, end=3, text="1BP")
    for span in (bad, {"start": 0, "end": 3, "text": "1BP"}):
        with pytest.raises(NormalizationError) as caught:
            validate_source_span("1BP", span)
        assert caught.value.code == "invalid_span"


def test_equivalent_unicode_normalization_is_not_substituted_before_evidence_check():
    with pytest.raises(NormalizationError) as caught:
        validate_source_span("가1BP", SourceSpan(start=0, end=2, text="가1"))
    assert caught.value.code == "span_mismatch"


def test_separate_occurrence_has_explicit_identity_and_no_original_rewrite():
    original = "1BP 그리고 1BP"
    span = SourceSpan(start=8, end=11, text="1BP")
    assert normalize_amount(original, span).span.start == 8
    assert original == "1BP 그리고 1BP"
    with pytest.raises(ValidationError):
        normalize_amount(original, span).span.start = 0


def test_amount_span_is_not_a_full_request_semantic_validator():
    original = "예산 1조 말고 2조"
    span = SourceSpan(start=3, end=5, text="1조")
    # 부분 문자열의 일치는 수정/부정 의미를 검증하지 않는다.
    assert normalize_amount(original, span).bp == 10**12


def test_spec_copy_is_independent_and_parser_has_no_file_or_network_io():
    expected = normalization_spec()
    changed = normalization_spec()
    changed["large_units"]["조"] = 1
    assert normalization_spec() == expected
    with (
        patch("builtins.open", side_effect=AssertionError("파일 I/O 금지")),
        patch("socket.socket", side_effect=AssertionError("네트워크 금지")),
    ):
        assert (
            normalize_amount("예산1.5조", SourceSpan(start=2, end=6, text="1.5조")).bp
            == 1500000000000
        )


@pytest.mark.parametrize("coefficient", ["0.0001", "1.2345", "999999.0001"])
@pytest.mark.parametrize(
    "unit,factor", [("만", 10**4), ("억", 10**8), ("조", 10**12), ("경", 10**16)]
)
def test_decimal_unit_conversion_matches_exact_rational_oracle(
    coefficient, unit, factor
):
    expected = Fraction(coefficient) * factor
    assert expected.denominator == 1
    assert parse_amount(coefficient + unit) == expected.numerator
