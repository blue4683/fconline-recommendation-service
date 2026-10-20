"""수동 합성 근거의 필드 정규화·동시 오류·누락·미지원 보존을 검증한다."""

import copy
import hashlib
import json
from itertools import product
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from FRS.core.request_normalization import SourceSpan
from FRS.core.request_normalization.conditions import (
    ConditionValueError,
    ExtractedConditions,
    NormalizedConditions,
    conditions_spec,
    normalize_conditions,
    parse_reinforcements,
    parse_salary,
)

DEFAULT_TEXTS = {
    "price_cap": "1조 5천억",
    "salary_cap": "300",
    "team_color": "리버풀",
    "formation": "4-2-3-1",
    "allowed_reinforcements": "5~11강",
}


def extracted_request(overrides=None, omitted=(), unsupported=()):
    """실제 원문 탐색기가 아니라 테스트 작성자가 지정한 근거 위치를 생성한다."""
    fields = {**DEFAULT_TEXTS, **(overrides or {})}
    original, evidence = "⚽ 조건\n", {}
    for field, value in (*fields.items(), ("unsupported", unsupported)):
        if field in omitted:
            continue
        evidence[field] = []
        for text in (value,) if isinstance(value, str) else value:
            original += field + ": "
            start = len(original)
            original += text
            evidence[field].append(
                SourceSpan(start=start, end=len(original), text=text)
            )
            original += "\n"
    return original, ExtractedConditions(**evidence)


def test_complete_conditions_preserve_evidence_and_have_no_internal_selection():
    original, extracted = extracted_request()
    before = extracted.model_dump()
    result = normalize_conditions(original, extracted)
    assert result.status == "ready" and not result.issues
    assert result.conditions.model_dump() == {
        "price_cap": 1500000000000,
        "salary_cap": 300,
        "team_color": "리버풀",
        "formation": "4-2-3-1",
        "allowed_reinforcements": (5, 6, 7, 8, 9, 10, 11),
    }
    assert result.evidence.model_dump() == before == extracted.model_dump()
    assert result.normalizer_version == conditions_spec()["normalizer_version"]
    assert set(type(result.conditions).model_fields) == set(DEFAULT_TEXTS)
    assert result.supported.clubs == ("리버풀", "첼시", "바이에른 뮌헨")
    assert result.supported.reinforcements == tuple(range(5, 12))


@pytest.mark.parametrize(
    "club,formation,levels",
    list(
        product(
            ("리버풀", "첼시", "바이에른 뮌헨"),
            ("4-2-3-1", "4-1-2-3", "4-2-2-2"),
            ("5강", "11강"),
        )
    ),
)
def test_all_supported_clubs_formations_and_grade_endpoints(club, formation, levels):
    original, extracted = extracted_request(
        {"team_color": club, "formation": formation, "allowed_reinforcements": levels}
    )
    result = normalize_conditions(original, extracted)
    assert result.status == "ready"
    assert (
        result.conditions.team_color == club
        and result.conditions.formation == formation
    )
    assert result.conditions.allowed_reinforcements == (int(levels[:-1]),)


@pytest.mark.parametrize("field", DEFAULT_TEXTS)
def test_each_missing_field_is_reported_without_defaults_or_partial_conditions(field):
    original, extracted = extracted_request(omitted=(field,))
    result = normalize_conditions(original, extracted)
    assert result.status == "needs_clarification" and result.conditions is None
    assert [(issue.field, issue.kind) for issue in result.issues] == [
        (field, "missing")
    ]


def test_empty_extraction_reports_all_five_missing_fields():
    result = normalize_conditions(
        "조건을 아직 정하지 않았습니다.", ExtractedConditions()
    )
    assert result.conditions is None
    assert {issue.field for issue in result.issues} == set(DEFAULT_TEXTS)
    assert {issue.kind for issue in result.issues} == {"missing"}


@pytest.mark.parametrize(
    "text,value",
    [
        ("0", 0),
        ("300", 300),
        ("1,000", 1000),
        (" 9,223,372,036,854,775,807 ", 2**63 - 1),
        ("000300", 300),
        ("0" * 3999 + "1", 1),
    ],
)
def test_salary_exact_integer_boundaries_and_commas(text, value):
    assert parse_salary(text) == value
    assert type(parse_salary(text)) is int


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "-1",
        "+300",
        "3.0",
        "3e2",
        "3_00",
        "3 00",
        "30,0",
        "1234,567",
        "300급여",
        "삼백",
        "３００",
        "٣٠٠",
        "300~350",
    ],
)
def test_salary_never_repairs_bad_grouping_units_or_non_integers(text):
    with pytest.raises(ConditionValueError) as error:
        parse_salary(text)
    assert error.value.code == "invalid_salary"


@pytest.mark.parametrize("text", [str(2**63), "9" * 4000])
def test_salary_outside_existing_integer_contract_is_rejected_before_int_conversion(
    text,
):
    with pytest.raises(ConditionValueError) as error:
        parse_salary(text)
    assert error.value.code == "salary_out_of_range"


@pytest.mark.parametrize(
    "text,levels",
    [
        ("5강", (5,)),
        ("11강", (11,)),
        ("5~11강", (5, 6, 7, 8, 9, 10, 11)),
        ("7~7강", (7,)),
        ("11강,5강,7강,5강", (5, 7, 11)),
        (" 5 ~ 7 강 ", (5, 6, 7)),
        (" 5강 , 7강 ", (5, 7)),
    ],
)
def test_reinforcements_single_closed_range_list_and_deduplication(text, levels):
    assert parse_reinforcements(text) == levels


@pytest.mark.parametrize(
    "text,code",
    [
        ("4강", "unsupported_reinforcement"),
        ("12강", "unsupported_reinforcement"),
        ("5강,12강", "unsupported_reinforcement"),
        ("4~11강", "unsupported_reinforcement"),
        ("5~12강", "unsupported_reinforcement"),
        ("11~5강", "reversed_range"),
        ("9" * 3999 + "강", "unsupported_reinforcement"),
        ("5~" + "9" * 3996 + "강", "unsupported_reinforcement"),
        ("5", "invalid_reinforcement_syntax"),
        ("5~11", "invalid_reinforcement_syntax"),
        ("5,7강", "invalid_reinforcement_syntax"),
        ("5강,", "invalid_reinforcement_syntax"),
        ("5강~11강", "invalid_reinforcement_syntax"),
        ("5~7강,9강", "invalid_reinforcement_syntax"),
        ("5-11강", "invalid_reinforcement_syntax"),
        ("5강 또는 7강", "invalid_reinforcement_syntax"),
        ("５강", "invalid_reinforcement_syntax"),
        ("5.0강", "invalid_reinforcement_syntax"),
        ("", "invalid_reinforcement_syntax"),
    ],
)
def test_unsupported_or_malformed_reinforcements_do_not_return_partial_sets(text, code):
    with pytest.raises(ConditionValueError) as error:
        parse_reinforcements(text)
    assert error.value.code == code


@pytest.mark.parametrize("parser", [parse_salary, parse_reinforcements])
@pytest.mark.parametrize("value", [None, True, 5, b"5", " " * 4001])
def test_field_parsers_reject_non_string_or_overlong_input(parser, value):
    with pytest.raises(ConditionValueError):
        parser(value)


@pytest.mark.parametrize(
    "field,text,code",
    [
        ("team_color", "뮌헨", "unsupported_club"),
        ("team_color", "바이에른  뮌헨", "unsupported_club"),
        ("team_color", "가상 클럽", "unsupported_club"),
        ("formation", "4231", "unsupported_formation"),
        ("formation", "4‐2‐3‐1", "unsupported_formation"),
        ("formation", "4-3-3", "unsupported_formation"),
    ],
)
def test_aliases_other_names_and_internal_whitespace_are_not_guessed(field, text, code):
    original, extracted = extracted_request({field: text})
    result = normalize_conditions(original, extracted)
    assert result.conditions is None
    assert [(issue.field, issue.kind, issue.code) for issue in result.issues] == [
        (field, "unsupported", code)
    ]
    assert getattr(result.evidence, field)[0].text == text


def test_outer_whitespace_is_trimmed_only_after_exact_span_validation():
    original, extracted = extracted_request(
        {"team_color": " 리버풀 ", "formation": "\t4-2-3-1\n", "salary_cap": " 300 "}
    )
    result = normalize_conditions(original, extracted)
    assert result.conditions.team_color == "리버풀"
    assert result.conditions.formation == "4-2-3-1"
    assert result.evidence.team_color[0].text == " 리버풀 "
    bad = extracted.model_copy(
        update={
            "team_color": (
                extracted.team_color[0].model_copy(update={"text": "리버풀"}),
            )
        }
    )
    assert normalize_conditions(original, bad).conditions is None


def test_errors_missing_conflicts_and_unsupported_conditions_are_all_preserved():
    original, extracted = extracted_request(
        {
            "price_cap": ("1조", "2조"),
            "salary_cap": "30,0",
            "team_color": "뮌헨",
            "allowed_reinforcements": "5강,12강",
        },
        omitted=("formation",),
        unsupported=("가상선수A 제외", "국적 조건"),
    )
    result = normalize_conditions(original, extracted)
    assert result.conditions is None and result.status == "needs_clarification"
    assert {(issue.field, issue.code) for issue in result.issues} == {
        ("price_cap", "conflicting_values"),
        ("salary_cap", "invalid_salary"),
        ("team_color", "unsupported_club"),
        ("allowed_reinforcements", "unsupported_reinforcement"),
        ("formation", "missing_condition"),
        ("unsupported", "unsupported_condition"),
    }
    assert len(result.issues) == 7
    assert result.evidence.unsupported == extracted.unsupported
    assert result.supported.required_fields == tuple(DEFAULT_TEXTS)
    assert all("가상선수A" not in issue.message for issue in result.issues)


@pytest.mark.parametrize(
    "field,repeated",
    [
        ("price_cap", ("1조 5천억", "1.5조")),
        ("salary_cap", ("300", "000300")),
        ("team_color", ("첼시", "첼시")),
        ("formation", ("4-2-2-2", "4-2-2-2")),
        ("allowed_reinforcements", ("5~7강", "7강,5강,6강,5강")),
    ],
)
def test_equivalent_repeated_values_keep_all_original_occurrences(field, repeated):
    original, extracted = extracted_request({field: repeated})
    result = normalize_conditions(original, extracted)
    assert result.status == "ready" and len(getattr(result.evidence, field)) == 2
    assert getattr(result.evidence, field) == getattr(extracted, field)


@pytest.mark.parametrize(
    "field,repeated",
    [
        ("price_cap", ("1조", "2조")),
        ("salary_cap", ("300", "350")),
        ("team_color", ("첼시", "리버풀")),
        ("formation", ("4-2-2-2", "4-2-3-1")),
        ("allowed_reinforcements", ("5강", "7강")),
    ],
)
def test_conflicting_fields_are_not_resolved_by_last_value_or_set_union(
    field, repeated
):
    original, extracted = extracted_request({field: repeated})
    result = normalize_conditions(original, extracted)
    assert result.conditions is None
    assert [(issue.field, issue.code) for issue in result.issues] == [
        (field, "conflicting_values")
    ]


def test_one_valid_and_one_invalid_occurrence_cannot_hide_the_invalid_value():
    original, extracted = extracted_request({"price_cap": ("1조", "약2조")})
    result = normalize_conditions(original, extracted)
    assert result.conditions is None and result.issues[0].code == "ambiguous_amount"
    assert len(result.evidence.price_cap) == 2


def test_multiple_span_mismatches_are_not_repaired_by_search_or_parsed():
    original, extracted = extracted_request()
    changes = {
        field: (getattr(extracted, field)[0].model_copy(update={"start": 0, "end": 1}),)
        for field in ("price_cap", "salary_cap")
    }
    bad = extracted.model_copy(update=changes)
    with (
        patch(
            "FRS.core.request_normalization.conditions.parse_amount",
            side_effect=AssertionError("불일치 금액 파싱 금지"),
        ),
        patch(
            "FRS.core.request_normalization.conditions.parse_salary",
            side_effect=AssertionError("불일치 급여 파싱 금지"),
        ),
    ):
        result = normalize_conditions(original, bad)
    assert result.conditions is None and len(result.issues) == 2
    assert {issue.code for issue in result.issues} == {"span_mismatch"}
    assert not result.evidence.price_cap and not result.evidence.salary_cap


def test_unsupported_span_must_also_match_original_before_it_is_returned():
    original, extracted = extracted_request(unsupported=("가상선수B 제외",))
    bad = extracted.model_copy(
        update={
            "unsupported": (
                extracted.unsupported[0].model_copy(update={"text": "없는 근거"}),
            )
        }
    )
    result = normalize_conditions(original, bad)
    assert result.conditions is None and result.issues[0].code == "span_mismatch"
    assert not result.evidence.unsupported


@pytest.mark.parametrize(
    "original,code",
    [(None, "invalid_text"), (True, "invalid_text"), ("가" * 4001, "source_too_long")],
)
def test_original_type_and_length_are_validated_even_for_empty_extraction(
    original, code
):
    result = normalize_conditions(original, ExtractedConditions())
    assert result.conditions is None and result.issues[0].code == code
    assert result.evidence == ExtractedConditions()


def test_source_length_boundary_does_not_truncate():
    original, extracted = extracted_request()
    assert (
        normalize_conditions(original + " " * (4000 - len(original)), extracted).status
        == "ready"
    )
    assert (
        normalize_conditions(
            original + " " * (4001 - len(original)), extracted
        ).conditions
        is None
    )


@pytest.mark.parametrize(
    "extracted",
    [
        None,
        {},
        ExtractedConditions.model_construct(
            price_cap=(SourceSpan.model_construct(start=True, end=3, text="1조"),)
        ),
    ],
)
def test_invalid_extraction_and_validation_bypass_are_rejected(extracted):
    result = normalize_conditions("1조", extracted)
    assert result.conditions is None and result.issues[0].code == "invalid_extraction"


def test_input_and_output_are_deeply_immutable_and_specs_return_independent_copies():
    original, extracted = extracted_request()
    before = copy.deepcopy(extracted.model_dump())
    result = normalize_conditions(original, extracted)
    with pytest.raises(ValidationError):
        result.conditions.salary_cap = 350
    with pytest.raises(ValidationError):
        result.evidence.price_cap[0].text = "2조"
    with pytest.raises(ValidationError):
        result.supported.clubs = ("가상",)
    dumped = result.model_dump()
    dumped["evidence"]["price_cap"][0]["text"] = "2조"
    assert extracted.model_dump() == before == result.evidence.model_dump()
    spec = conditions_spec()
    version = spec.pop("normalizer_version")
    assert (
        version
        == hashlib.sha256(
            json.dumps(
                spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
    )
    spec["clubs"].append("가상")
    assert conditions_spec()["normalizer_version"] == version
    assert "가상" not in conditions_spec()["clubs"]


def test_invalid_normalized_dto_cannot_supply_other_clubs_or_noncanonical_levels():
    values = dict(
        price_cap=1,
        salary_cap=300,
        team_color="리버풀",
        formation="4-2-3-1",
        allowed_reinforcements=(5,),
    )
    for changes in (
        {"salary_cap": True},
        {"team_color": "뮌헨"},
        {"formation": "4231"},
        {"allowed_reinforcements": ()},
        {"allowed_reinforcements": (5, 5)},
        {"allowed_reinforcements": (7, 5)},
        {"allowed_reinforcements": (True,)},
        {"allowed_reinforcements": (4, 5)},
        {"batch_id": "자동 선택 금지"},
    ):
        with pytest.raises(ValidationError):
            NormalizedConditions(**{**values, **changes})


def test_no_file_network_or_squad_execution_and_unreported_semantics_are_not_claimed():
    original, extracted = extracted_request()
    original += (
        "가상선수C 제외"  # 추출기가 보고하지 않은 절은 이 순수 모듈이 발견하지 않는다.
    )
    with (
        patch("builtins.open", side_effect=AssertionError("파일 I/O 금지")),
        patch("socket.socket", side_effect=AssertionError("네트워크 금지")),
    ):
        result = normalize_conditions(original, extracted)
    assert result.status == "ready"
    assert not result.evidence.unsupported


@pytest.mark.parametrize(
    "budget,salary",
    [
        ("0BP", "0"),
        ("9007199254740993BP", "9,007,199,254,740,993"),
        ("9999999999999999999999999999999999BP", "9,223,372,036,854,775,807"),
    ],
)
def test_assembled_limits_preserve_zero_and_large_exact_integers(budget, salary):
    original, extracted = extracted_request({"price_cap": budget, "salary_cap": salary})
    result = normalize_conditions(original, extracted)
    assert result.status == "ready"
    assert result.conditions.price_cap == int(budget[:-2])
    assert result.conditions.salary_cap == int(salary.replace(",", ""))


def test_multiple_distinct_bad_occurrences_report_original_indices_and_keep_verified_evidence():
    original, extracted = extracted_request({"price_cap": ("1조", "약2조", "3조")})
    spans = list(extracted.price_cap)
    spans[0] = spans[0].model_copy(update={"text": "없는 근거"})
    bad = extracted.model_copy(update={"price_cap": tuple(spans)})
    result = normalize_conditions(original, bad)
    assert result.conditions is None
    assert [(issue.code, issue.occurrence) for issue in result.issues] == [
        ("span_mismatch", 0),
        ("ambiguous_amount", 1),
    ]
    assert result.evidence.price_cap == extracted.price_cap[1:]


def test_unknown_fields_or_numeric_extractions_are_not_silently_discarded():
    with pytest.raises(ValidationError):
        ExtractedConditions(price_cap=1500000000000)
    with pytest.raises(ValidationError):
        ExtractedConditions(nation="가상 국적")
