"""정형 후보·명시 ID 선택·기존 엔진 경계를 합성 입력으로 검증한다."""

import itertools
import json
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from langsmith.run_helpers import get_tracing_context
from pydantic import ValidationError
from test_schema import OBSERVED
from test_synthetic_material import config

from FRS.agent.candidate_graph import CandidateSquadAgent, candidate_agent_spec
from FRS.agent.candidate_selection import (
    MAX_SELECTION_BYTES,
    CandidateSelectionError,
    decode_selection,
    selection_spec,
)
from FRS.agent.candidates import (
    FIELDS,
    MAX_CANDIDATES,
    candidate_spec,
    prepare_candidates,
)
from FRS.agent.comparison import smoke_cases
from FRS.agent.extraction import ExtractionError
from FRS.agent.material import generate_synthetic_material
from FRS.agent.presentation import present_agent_result
from FRS.core.request_normalization import normalize_conditions
from FRS.schema.scope import FORMATION_SLOTS, PRIORITY_CLUBS

SOURCE = "예산 1조 5천억, 급여 999, 리버풀, 4-2-3-1, 5강,11강으로 구성해줘."


def selection(registry):
    """시험 대역/파서 단독 기준. 실제 모델의 올바른 선택을 주장하지 않는다."""
    return {
        "candidate_set_id": registry.candidate_set_id,
        **{
            field: [v.candidate_id for v in registry.candidates if v.field == field]
            for field in FIELDS
        },
        "unsupported": [],
    }


def decode(original, payload=None):
    registry = prepare_candidates(original).registry
    return decode_selection(
        original, registry, json.dumps(payload or selection(registry))
    )


def test_correct_source_offsets_and_whole_compound_values():
    p = prepare_candidates(SOURCE)
    assert p.status == "ready" and not p.issues
    registry = p.registry
    assert [(v.field, v.span.text) for v in registry.candidates] == [
        ("price_cap", "1조 5천억"),
        ("salary_cap", "999"),
        ("team_color", "리버풀"),
        ("formation", "4-2-3-1"),
        ("allowed_reinforcements", "5강,11강"),
    ]
    for item in (*registry.candidates, *registry.contexts):
        span = item.span
        assert SOURCE[span.start : span.end] == span.text
    assert registry.contexts[-1].suffix.text == "으로 구성해줘."
    normalized = normalize_conditions(SOURCE, decode(SOURCE))
    assert normalized.status == "ready"
    assert normalized.conditions.price_cap == 1500000000000
    assert normalized.conditions.allowed_reinforcements == (5, 11)


def test_all_clause_orders_preserve_meaning_and_occurrence_spans():
    clauses = ("예산 1.5조", "급여 300", "첼시", "4-2-2-2", "5~11강")
    for order in itertools.permutations(clauses):
        source = ", ".join(order)
        p = prepare_candidates(source)
        assert p.status == "ready"
        normalized = normalize_conditions(source, decode(source))
        assert normalized.status == "ready"
        assert normalized.conditions.price_cap == 1500000000000
        assert normalized.conditions.salary_cap == 300
        assert normalized.conditions.team_color == "첼시"
        assert normalized.conditions.allowed_reinforcements == tuple(range(5, 12))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1조 5천억", 1500000000000),
        ("1.5조", 1500000000000),
        ("150,000BP", 150000),
        ("999,999,999BP", 999999999),
        ("9999999999999999999999999999999999BP", 10**34 - 1),
    ],
)
def test_budget_is_parsed_exactly_without_comma_fragmentation(value, expected):
    source = SOURCE.replace("1조 5천억", value)
    p = prepare_candidates(source)
    assert p.status == "ready" and len(p.registry.contexts) == 5
    assert normalize_conditions(source, decode(source)).conditions.price_cap == expected


@pytest.mark.parametrize(
    "levels,expected",
    [
        ("5강", (5,)),
        ("5~11강", tuple(range(5, 12))),
        ("5강,7강", (5, 7)),
        ("5강, 7강,5강", (5, 7)),
    ],
)
def test_reinforcement_list_is_one_source_candidate(levels, expected):
    source = SOURCE.replace("5강,11강", levels)
    p = prepare_candidates(source)
    assert p.status == "ready"
    values = [v for v in p.registry.candidates if v.field == "allowed_reinforcements"]
    assert len(values) == 1 and values[0].span.text == levels
    assert (
        normalize_conditions(source, decode(source)).conditions.allowed_reinforcements
        == expected
    )


@pytest.mark.parametrize(
    "source",
    [
        "예산 1조5000, 급여 300, 리버풀, 4-2-3-1, 5강",
        "예산 1,00BP, 급여 300, 리버풀, 4-2-3-1, 5강",
        "예산 1조, 급여 3,00, 리버풀, 4-2-3-1, 5강",
        "예산 약 1조, 급여 300, 리버풀, 4-2-3-1, 5강",
        "예산 1조, 급여 300, 리버풀 제외, 4-2-3-1, 5강",
        "예산 1조, 급여 300, 리버풀 말고 첼시, 4-2-3-1, 5강",
        "예산 1조, 급여 300, 리버풀, 4-2-3-1, 5강, 빠른 선수",
        "예산 1조, 급여 300, 뮌헨, 4-2-3-1, 5강",
        "예산 1조, 급여 300, 리버풀, 4231, 5강",
        "예산 1조, 급여 300, 리버풀, 4-3-3, 5강",
        "예산 1조, 급여 300, 리버풀, 4-2-3-1, 4강",
        "예산 1조, 급여 300, 리버풀, 4-2-3-1, 5강,7~11강",
        "예산 1조, 급여 300, 리버풀, 4-2-3-1, 5강. 가상선수 제외.",
        "예산 1조, 급여 300, 리버풀, 4-2-3-1, 5강 구성해줘",
        "예산 1조,,급여 300, 리버풀, 4-2-3-1, 5강",
        ",예산 1조, 급여 300, 리버풀, 4-2-3-1, 5강",
        "예산 1조, 급여 300, 리버풀, 4-2-3-1, 5강,",
    ],
)
def test_malformed_unknown_negative_corrective_contexts_never_recover_prefix(source):
    p = prepare_candidates(source)
    assert p.status == "needs_clarification" and p.issues
    if p.registry:
        with pytest.raises(CandidateSelectionError, match="후보 선택"):
            decode_selection(source, p.registry, json.dumps(selection(p.registry)))


def test_invalid_budget_is_retained_whole_instead_of_valid_prefix():
    source = SOURCE.replace("1조 5천억", "1조5000")
    p = prepare_candidates(source)
    assert p.status == "needs_clarification"
    value = p.registry.candidates[0]
    assert value.span.text == "1조5000"
    assert any(i.field == "price_cap" for i in p.issues)


def test_repeated_budget_spans_keep_both_values_for_conflict_detection():
    source = "예산 1조, 예산 2조, 급여 300, 리버풀, 4-2-3-1, 5강"
    p = prepare_candidates(source)
    assert p.status == "ready"
    prices = [v for v in p.registry.candidates if v.field == "price_cap"]
    assert len(prices) == 2 and prices[0].candidate_id != prices[1].candidate_id
    normalized = normalize_conditions(source, decode(source))
    assert normalized.conditions is None
    assert any(i.code == "conflicting_values" for i in normalized.issues)
    raw = selection(p.registry)
    raw["price_cap"].pop()
    with pytest.raises(CandidateSelectionError) as error:
        decode_selection(source, p.registry, json.dumps(raw))
    assert error.value.code == "incomplete_coverage"


def test_same_text_repetitions_have_distinct_ids_and_restore_original_order():
    source = SOURCE.replace("예산 1조 5천억", "예산 1조, 예산 1조")
    p = prepare_candidates(source)
    raw = selection(p.registry)
    raw["price_cap"].reverse()
    extracted = decode_selection(source, p.registry, json.dumps(raw))
    assert len(extracted.price_cap) == 2
    assert extracted.price_cap[0].start < extracted.price_cap[1].start
    assert extracted.price_cap[0].text == extracted.price_cap[1].text == "1조"
    assert normalize_conditions(source, extracted).status == "ready"


def test_missing_field_is_not_filled_by_preparation_or_selection():
    source = SOURCE.replace(" 급여 999,", "")
    p = prepare_candidates(source)
    assert p.status == "ready"
    raw = selection(p.registry)
    assert raw["salary_cap"] == []
    normalized = normalize_conditions(
        source, decode_selection(source, p.registry, json.dumps(raw))
    )
    assert normalized.conditions is None
    assert [(i.field, i.code) for i in normalized.issues] == [
        ("salary_cap", "missing_condition")
    ]


@pytest.mark.parametrize(
    "source,code",
    [
        (None, "invalid_text"),
        (123, "invalid_text"),
        ("", "empty_source"),
        (" \n\t", "empty_source"),
        ("x" * 4001, "source_too_long"),
    ],
)
def test_input_guard_has_no_partial_registry(source, code):
    p = prepare_candidates(source)
    assert p.registry is None and p.issues[0].code == code


def test_source_length_amount_length_and_unicode_offsets():
    # 앞 공백과 탭도 Python 코드포인트로 보존한다.
    source = " \t" + SOURCE + " \n"
    p = prepare_candidates(source)
    assert p.status == "ready"
    assert p.registry.candidates[0].span.start == source.index("1조")
    exact = SOURCE + " " * (4000 - len(SOURCE))
    assert prepare_candidates(exact).status == "ready"
    value100 = "0" * 98 + "BP"
    assert len(value100) == 100
    assert prepare_candidates(SOURCE.replace("1조 5천억", value100)).status == "ready"
    invalid = prepare_candidates(SOURCE.replace("1조 5천억", "0" * 99 + "BP"))
    assert any(i.code == "amount_too_long" for i in invalid.issues)
    emoji = prepare_candidates("😀, " + SOURCE)
    assert emoji.issues and emoji.registry.contexts[0].span.text == "😀"


def test_context_limit_is_inclusive_and_overflow_discards_whole_registry():
    assert prepare_candidates(", ".join(["리버풀"] * 64)).status == "ready"
    over = prepare_candidates(", ".join(["리버풀"] * 65))
    assert over.registry is None and over.issues[0].code == "context_limit"


def test_deterministic_frozen_registry_and_copy_isolation():
    first = prepare_candidates(SOURCE).registry
    assert first == prepare_candidates(SOURCE).registry
    assert (
        first.candidate_set_id == prepare_candidates(SOURCE).registry.candidate_set_id
    )
    with pytest.raises(ValidationError):
        first.original = "changed"
    with pytest.raises(ValidationError):
        first.candidates[0].span.text = "changed"
    dumped = first.model_dump()
    dumped["candidates"][0]["span"]["text"] = "changed"
    assert first.candidates[0].span.text == "1조 5천억"
    assert (
        prepare_candidates(" " + SOURCE).registry.candidate_set_id
        != first.candidate_set_id
    )


def test_tampered_registry_or_different_original_cannot_be_used():
    p = prepare_candidates(SOURCE)
    fake = p.registry.model_copy(update={"original": SOURCE + " "})
    for original, registry in [
        (SOURCE, fake),
        (SOURCE + " ", p.registry),
        (SOURCE, {}),
    ]:
        with pytest.raises(CandidateSelectionError) as error:
            decode_selection(original, registry, json.dumps(selection(p.registry)))
        assert error.value.code == "invalid_registry"


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda p: p.update(candidate_set_id="0" * 64), "candidate_set_mismatch"),
        (lambda p: p["price_cap"].append("v-999"), "unknown_candidate"),
        (lambda p: p["price_cap"].extend(p["price_cap"]), "duplicate_candidate"),
        (lambda p: p["price_cap"].clear(), "incomplete_coverage"),
        (
            lambda p: p.update(salary_cap=p["team_color"], team_color=p["salary_cap"]),
            "field_mismatch",
        ),
        (lambda p: p.update(unsupported=["ctx-001"]), "invalid_context_selection"),
        (lambda p: p.update(extra=True), "invalid_selection"),
        (lambda p: p.update(price_cap=[True]), "invalid_selection"),
        (lambda p: p.update(price_cap=["ctx-001"]), "invalid_selection"),
        (
            lambda p: p.update(price_cap=["v-001"] * (MAX_CANDIDATES + 1)),
            "invalid_selection",
        ),
    ],
)
def test_bad_selection_is_not_repaired(change, code):
    registry = prepare_candidates(SOURCE).registry
    raw = selection(registry)
    change(raw)
    with pytest.raises(CandidateSelectionError) as error:
        decode_selection(SOURCE, registry, json.dumps(raw))
    assert error.value.code == code


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        "{}",
        "[]",
        "NaN",
        '{"x":1,"x":2}',
        "x" * (MAX_SELECTION_BYTES + 1),
        "\ud800",
    ],
)
def test_invalid_json_types_duplicate_fields_and_size_are_safe(raw):
    registry = prepare_candidates(SOURCE).registry
    with pytest.raises(CandidateSelectionError) as error:
        decode_selection(SOURCE, registry, raw)
    assert error.value.code == "invalid_selection"


def test_specs_are_independent_copies_and_bind_versions():
    spec = candidate_spec()
    spec["prefixes"].append("추측")
    assert candidate_spec()["prefixes"] == ["예산", "급여"]
    assert (
        selection_spec()["candidate_version"] == candidate_spec()["candidate_version"]
    )
    assert (
        candidate_agent_spec()["selection_version"]
        == selection_spec()["selection_version"]
    )


@pytest.fixture(scope="module")
def material():
    return generate_synthetic_material(config())


def wrapper(material, *, selector=None, clock=lambda: OBSERVED, batches=None):
    if selector is None:
        selector = Mock()
        selector.select.side_effect = lambda registry: json.dumps(selection(registry))
    provider = Mock(wraps=material)
    graph = CandidateSquadAgent(
        selector, material=provider, batches=batches or material.batches, clock=clock
    )
    return graph, selector, provider


@pytest.mark.parametrize("formation", tuple(FORMATION_SLOTS))
@pytest.mark.parametrize("club", PRIORITY_CLUBS)
def test_mock_id_selection_reaches_existing_solver_validation_and_presentation(
    material, club, formation
):
    source = SOURCE.replace("리버풀", club).replace("4-2-3-1", formation)
    graph, selector, provider = wrapper(material)
    result = graph.run(source, t0=OBSERVED)
    assert result.outcome == "agent_result" and result.error is None
    assert result.agent_result.build.engine.status == "optimal"
    shown = present_agent_result(result.agent_result, clock=lambda: OBSERVED).to_dict()
    assert shown["status"] == "optimal" and len(shown["squad"]["rows"]) == 11
    selector.select.assert_called_once()
    provider.load.assert_called_once_with(material.batches)


@pytest.mark.parametrize(
    "source", [None, "", "😀", "리버풀 제외", "예산 1조5000", ",".join(["리버풀"] * 65)]
)
def test_preparation_issue_never_calls_selector_context_clock_or_engine(source):
    selector, material, clock = Mock(), Mock(), Mock()
    graph = CandidateSquadAgent(selector, material=material, batches=None, clock=clock)
    with patch("FRS.agent.candidate_graph.SquadAgent") as existing:
        result = graph.run(source, t0=None)
    assert result.outcome == "input_issues" and result.preparation.issues
    assert result.agent_result is None and result.error is None
    selector.select.assert_not_called()
    material.load.assert_not_called()
    clock.assert_not_called()
    existing.assert_not_called()


def test_provider_error_is_safe_and_never_retried_or_delegated():
    selector = Mock()
    selector.select.side_effect = ExtractionError("private model draft")
    material, clock = Mock(), Mock()
    graph = CandidateSquadAgent(selector, material=material, batches=None, clock=clock)
    with patch("FRS.agent.candidate_graph.SquadAgent") as existing:
        result = graph.run(SOURCE, t0=None)
    assert (
        result.outcome == "provider_error"
        and result.error == "selection_provider_error"
    )
    assert "private" not in repr(result)
    selector.select.assert_called_once()
    material.load.assert_not_called()
    clock.assert_not_called()
    existing.assert_not_called()


def test_invalid_model_coverage_has_no_material_or_engine_call():
    selector = Mock()
    selector.select.side_effect = lambda registry: json.dumps(
        {**selection(registry), "price_cap": []}
    )
    material, clock = Mock(), Mock()
    graph = CandidateSquadAgent(selector, material=material, batches=None, clock=clock)
    with patch("FRS.agent.candidate_graph.SquadAgent") as existing:
        result = graph.run(SOURCE, t0=None)
    assert result.outcome == "provider_error" and result.error == "incomplete_coverage"
    material.load.assert_not_called()
    clock.assert_not_called()
    existing.assert_not_called()


@pytest.mark.parametrize(
    "source,issue_code",
    [
        (SOURCE.replace(" 급여 999,", ""), "missing_condition"),
        (SOURCE.replace("예산 1조 5천억", "예산 1조, 예산 2조"), "conflicting_values"),
    ],
)
def test_existing_missing_and_conflict_results_block_material(
    material, source, issue_code
):
    clock = Mock(side_effect=AssertionError("시계 금지"))
    graph, selector, provider = wrapper(material, clock=clock)
    result = graph.run(source, t0=None)
    assert result.outcome == "agent_result"
    assert result.agent_result.build is None
    assert any(i.code == issue_code for i in result.agent_result.normalization.issues)
    provider.load.assert_not_called()
    clock.assert_not_called()
    selector.select.assert_called_once()


def test_late_expiry_and_no_request_history(material):
    graph, selector, provider = wrapper(
        material, clock=lambda: OBSERVED + timedelta(days=1, seconds=1)
    )
    result = graph.run(SOURCE, t0=OBSERVED)
    assert result.agent_result.build.engine.status == "data_expired"
    bad = graph.run("리버풀 제외", t0=None)
    assert bad.outcome == "input_issues" and bad.agent_result is None
    assert selector.select.call_count == 1 and provider.load.call_count == 1


def test_unexpected_provider_bug_propagates_once_without_material():
    selector = Mock()
    selector.select.side_effect = RuntimeError("implementation bug")
    material = Mock()
    graph = CandidateSquadAgent(selector, material=material, batches=None, clock=None)
    with pytest.raises(RuntimeError, match="implementation bug"):
        graph.run(SOURCE, t0=None)
    selector.select.assert_called_once()
    material.load.assert_not_called()


def test_fake_path_never_opens_network_and_disables_external_tracing(
    material, monkeypatch
):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    observed = []
    selector = Mock()

    def select(registry):
        observed.append(get_tracing_context()["enabled"])
        return json.dumps(selection(registry))

    selector.select.side_effect = select
    graph, _, _ = wrapper(material, selector=selector)
    with (
        patch(
            "httpx.Client.send", side_effect=AssertionError("HTTP 금지")
        ) as httpx_send,
        patch(
            "httpx2.Client.send", side_effect=AssertionError("HTTP 금지")
        ) as httpx2_send,
        patch(
            "requests.Session.send", side_effect=AssertionError("HTTP 금지")
        ) as requests_send,
    ):
        result = graph.run(SOURCE, t0=OBSERVED)
    assert result.agent_result.build.engine.status == "optimal" and observed == [False]
    httpx_send.assert_not_called()
    httpx2_send.assert_not_called()
    requests_send.assert_not_called()


def test_original_smoke_inputs_use_correct_candidates_or_preparation_issues():
    for case in smoke_cases():
        p = prepare_candidates(case.original)
        if case.case_id == "unsupported":
            assert p.issues
        else:
            assert p.status == "ready"
            extracted = decode_selection(
                case.original, p.registry, json.dumps(selection(p.registry))
            )
            assert extracted == case.expected


def test_invalid_unicode_input_and_excessively_nested_provider_json_are_safe():
    bad = prepare_candidates(SOURCE + "\ud800")
    assert bad.registry is None and bad.issues[0].code == "invalid_unicode"
    registry = prepare_candidates(SOURCE).registry
    with pytest.raises(CandidateSelectionError) as error:
        decode_selection(SOURCE, registry, "[" * 2000 + "]" * 2000)
    assert error.value.code == "invalid_selection"
