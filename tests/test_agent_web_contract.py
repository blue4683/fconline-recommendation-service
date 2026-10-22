"""웹 경계의 엄격한 값/근거/만료/SSE를 합성 자료로만 검증한다."""

import json
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError
from test_candidate_selection import SOURCE, selection
from test_schema import OBSERVED
from test_squad_build import inputs
from test_synthetic_material import config

from FRS.agent.candidate_graph import CandidateSquadAgent
from FRS.agent.material import generate_synthetic_material
from FRS.api.agent_contract import WebFinal
from FRS.api.agent_events import EventSequence, RequestError, event_spec, parse_request
from FRS.api.agent_projection import (
    SelectionProvenance,
    WebOutcome,
    error_outcome,
    project_result,
    projection_spec,
)


@pytest.fixture(scope="module")
def material():
    return generate_synthetic_material(config())


def run(source, material, selector=None):
    selector = selector or Mock(select=lambda registry: json.dumps(selection(registry)))
    graph = CandidateSquadAgent(
        selector, material=material, batches=material.batches, clock=lambda: OBSERVED
    )
    return graph.run(source, t0=OBSERVED)


@pytest.fixture(scope="module")
def successful(material):
    return run(SOURCE, material)


def project(source, result, material, **changes):
    return project_result(
        source,
        result,
        clock=lambda: OBSERVED,
        material_id=material.material_id,
        **changes,
    )


def payload(outcome, clock=lambda: OBSERVED):
    return outcome.to_dict(clock=clock)


def test_success_preserves_exact_table_without_internal_diagnostics(
    material, successful
):
    shown = payload(project(SOURCE, successful, material))
    assert shown["status"] == "optimal" and len(shown["squad"]["rows"]) == 11
    assert set(shown) == set(WebFinal.model_fields)
    assert not (
        {"kind", "coverage", "evaluated_at", "evidence", "model_metadata", "code"}
        & set(shown)
    )
    assert shown["versions"]["model"] == "unused"
    assert shown["versions"]["material"] == material.material_id
    assert shown["snapshot"]["id"] == successful.agent_result.build.snapshot.snapshot_id
    assert all(
        type(r[k]) is str
        for r in shown["squad"]["rows"]
        for k in ("spid", "person_id", "price", "salary")
    )
    shown["squad"]["rows"].clear()
    assert len(payload(project(SOURCE, successful, material))["squad"]["rows"]) == 11


def test_explicit_mock_execution_provenance_is_distinct_from_unused(
    material, successful
):
    provenance = SelectionProvenance("qwen3:8b", "a" * 64, "b" * 64)
    shown = payload(project(SOURCE, successful, material, provenance=provenance))
    assert shown["versions"]["model"] == "qwen3:8b"
    assert shown["versions"]["model_digest"] == "a" * 64
    assert shown["versions"]["selector"] == "b" * 64
    assert "not_provided" not in shown["versions"].values()


@pytest.mark.parametrize(
    "source,code,kind",
    [
        (SOURCE.replace("5강,11강", "4강"), "unsupported_reinforcement", "unsupported"),
        (SOURCE.replace("1조 5천억", "약 1조"), "ambiguous_amount", "ambiguous"),
        (SOURCE.replace("리버풀", "리버풀 제외"), "unrecognized_context", "invalid"),
        (SOURCE.replace("1조 5천억", "1조5000"), "missing_unit", "invalid"),
    ],
)
def test_preparation_issues_are_labeled_without_model_or_material(source, code, kind):
    selector, provider, clock = Mock(), Mock(), Mock()
    result = CandidateSquadAgent(
        selector, material=provider, batches=None, clock=clock
    ).run(source, t0=None)
    shown = payload(project_result(source, result, clock=clock))
    assert shown["status"] == "needs_clarification"
    assert shown["issues"][0]["code"] == "prepare." + code
    assert shown["issues"][0]["kind"] == kind
    assert shown["issues"][0]["occurrence"] is None
    assert shown["versions"]["normalizer"] == "unused"
    assert shown["versions"]["model"] == "unused"
    selector.select.assert_not_called()
    provider.load.assert_not_called()
    clock.assert_not_called()


def test_unknown_or_forged_preparation_code_is_internal_error():
    source = SOURCE.replace("리버풀", "리버풀 제외")
    result = CandidateSquadAgent(Mock(), material=None, batches=None, clock=None).run(
        source, t0=None
    )
    issue = result.preparation.issues[0].model_copy(update={"code": "private secret"})
    bad = replace(
        result, preparation=result.preparation.model_copy(update={"issues": (issue,)})
    )
    assert payload(project_result(source, bad, clock=Mock())) == payload(
        error_outcome()
    )


@pytest.mark.parametrize(
    "source", [SOURCE.replace("급여 999, ", ""), "예산 2조, " + SOURCE]
)
def test_post_selection_missing_and_conflict_keep_normalizer_origin(source, material):
    result = run(source, material)
    shown = payload(project(source, result, material))
    assert shown["status"] == "needs_clarification"
    assert all(not i["code"].startswith("prepare.") for i in shown["issues"])
    assert shown["versions"]["normalizer"] != "unused"
    assert shown["versions"]["material"] == "unused"
    assert shown["squad"] is None


def test_missing_selection_is_technical_error_not_clarification(material):
    result = run(
        SOURCE,
        material,
        Mock(select=lambda r: json.dumps({**selection(r), "salary_cap": []})),
    )
    outcome = project(SOURCE, result, material)
    assert outcome.kind == "error" and payload(outcome)["code"] == "incomplete_coverage"
    assert set(payload(outcome)) == {"code", "message"}


def test_foreign_original_tampered_wrapper_and_material_provenance_rejected(
    material, successful
):
    for source, value, material_id in (
        (SOURCE.replace("리버풀", "첼시"), successful, material.material_id),
        (SOURCE, replace(successful, adapter_version="forged"), material.material_id),
        (SOURCE, successful, None),
        (SOURCE, replace(successful, error="private"), material.material_id),
    ):
        out = project_result(
            source, value, clock=lambda: OBSERVED, material_id=material_id
        )
        assert out.kind == "error" and payload(out)["code"] == "internal_error"


def test_very_large_integers_preserve_all_digits_through_actual_engine():
    cost, wage = 10**30 + 7, 2**53 + 1
    _, _, kwargs = inputs(cost=cost, salary_cap=str(2**63 - 1))
    source = f"예산 {11 * cost}BP, 급여 {2**63 - 1}, 리버풀, 4-2-2-2, 5강"
    records = {
        key: kwargs[key]
        for key in ("cards", "salaries", "prices", "reinforcements", "team_colors")
    }
    records["salaries"] = [
        r.model_copy(update={"salary": wage}) for r in records["salaries"]
    ]
    provider = Mock(load=Mock(return_value=records))
    result = CandidateSquadAgent(
        Mock(select=lambda r: json.dumps(selection(r))),
        material=provider,
        batches=kwargs["batches"],
        clock=lambda: OBSERVED,
    ).run(source, t0=OBSERVED)
    shown = payload(
        project_result(source, result, clock=lambda: OBSERVED, material_id="a" * 64)
    )
    assert shown["squad"]["total_price"] == str(11 * cost)
    assert shown["squad"]["total_salary"] == str(11 * wage)
    assert shown["conditions"]["salary_cap"] == str(2**63 - 1)
    assert shown["squad"]["rows"][0]["salary"] == str(wage)


def test_expiry_between_presentation_and_wire_serialization_removes_success(
    material, successful
):
    outcome = project(SOURCE, successful, material)
    deadline = datetime.fromisoformat(
        successful.agent_result.build.snapshot.to_dict()["metadata"]["valid_until"]
    )
    with patch(
        "FRS.api.agent_projection.WebFinal.model_validate",
        wraps=WebFinal.model_validate,
    ) as checked:

        def clock():
            assert checked.call_count >= 1
            return deadline

        shown = payload(outcome, clock)
    assert shown["status"] == "data_expired" and shown["squad"] is None
    assert "최적성을 확인" not in shown["answer"]
    assert shown["snapshot"]["valid_until"] == deadline.isoformat()


@pytest.mark.parametrize(
    "path,value",
    [
        (("conditions", "price_cap"), 10**30),
        (("conditions", "salary_cap"), 300),
        (("conditions", "price_cap"), "01"),
        (("conditions", "price_cap"), "1" * 35),
        (("conditions", "salary_cap"), str(2**63)),
        (("conditions", "allowed_reinforcements"), [5, 5]),
        (("squad", "total_price"), "0"),
        (("squad", "total_salary"), "0"),
        (("squad", "performance"), 0),
        (("squad", "total_ovr"), True),
        (("snapshot", "t0"), "2026-01-01T00:00:00"),
        (("versions", "web_contract"), "wrong"),
        (("narration_source",), "llm_checked"),
        (("data_mode",), "real"),
        (("extra",), "debug"),
    ],
)
def test_wrong_values_or_extra_fields_cannot_be_sent_as_success(
    material, successful, path, value
):
    body = payload(project(SOURCE, successful, material))
    part = body
    for key in path[:-1]:
        part = part[key]
    part[path[-1]] = value
    with pytest.raises(ValidationError):
        WebFinal.model_validate(body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("person_id", "0"),
        ("spid", "0"),
        ("reinforce_level", 12),
        ("price", 100),
        ("salary", str(2**63)),
        ("performance", True),
        ("raw_ovr", 201),
        ("placement_type", ""),
    ],
)
def test_bad_row_numeric_type_duplicate_and_unsupported_grade(
    material, successful, field, value
):
    body = payload(project(SOURCE, successful, material))
    if field in ("person_id", "spid"):
        body["squad"]["rows"][0][field] = body["squad"]["rows"][1][field]
    else:
        body["squad"]["rows"][0][field] = value
    with pytest.raises(ValidationError):
        WebFinal.model_validate(body)


def test_terminal_revalidates_payload_and_does_not_leak_invalid_success(
    material, successful
):
    body = payload(project(SOURCE, successful, material))
    body["squad"]["rows"].clear()
    forged = WebOutcome(
        "final", json.dumps(body).encode(), successful.agent_result.build.snapshot
    )
    events = EventSequence("test")
    events.started()
    terminal = events.terminal(forged, clock=lambda: OBSERVED)
    assert (
        terminal.startswith(b"event: error\n")
        and b'"code":"internal_error"' in terminal
    )
    assert b'"rows"' not in terminal


def request_bytes(**changes):
    return json.dumps(
        dict(
            contract_version="web-agent.v1",
            request_id="test-1",
            message=SOURCE,
            **changes,
        ),
        ensure_ascii=False,
    ).encode()


def test_request_preserves_unicode_and_whitespace():
    original = "  " + SOURCE + "  "
    raw = json.dumps(
        dict(contract_version="web-agent.v1", request_id="id_1-A", message=original),
        ensure_ascii=False,
    ).encode()
    assert parse_request(raw).message == original
    assert (
        parse_request(
            json.dumps(
                dict(
                    contract_version="web-agent.v1", request_id="x", message="⚽" * 4000
                )
            ).encode()
        ).message
        == "⚽" * 4000
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b"NaN",
        b'{"message":"x","message":"y"}',
        b"\xff",
        b"[" * 2000,
        b"x" * 65537,
    ],
)
def test_invalid_json_and_byte_limit_are_safe_before_model(raw):
    with pytest.raises(RequestError) as error:
        parse_request(raw)
    assert error.value.status_code == (413 if len(raw) > 65536 else 422)
    assert str(error.value) == "요청 형식과 길이를 확인해 주세요."


@pytest.mark.parametrize(
    "changes",
    [
        dict(request_id="../x"),
        dict(request_id="x" * 65),
        dict(request_id="한글"),
        dict(message=" "),
        dict(message="x" * 4001),
        dict(message="\ud800"),
        dict(message=123),
        dict(contract_version="future"),
        dict(extra=True),
    ],
)
def test_invalid_request_fields_are_rejected(changes):
    values = dict(contract_version="web-agent.v1", request_id="x", message=SOURCE)
    values.update(changes)
    raw = json.dumps(values).encode()
    with pytest.raises(RequestError):
        parse_request(raw)


def test_sse_envelopes_are_exact_request_local_and_terminal_once(material, successful):
    events = EventSequence("first")
    output = [
        events.started(),
        events.progress("agent"),
        events.progress("render"),
        events.terminal(project(SOURCE, successful, material), clock=lambda: OBSERVED),
    ]
    data = [json.loads(part.split(b"data: ", 1)[1]) for part in output]
    assert [d["seq"] for d in data] == [0, 1, 2, 3]
    assert [d["type"] for d in data] == ["started", "progress", "progress", "final"]
    assert all(
        d["request_id"] == "first"
        and set(d) == {"contract_version", "request_id", "seq", "type", "payload"}
        for d in data
    )
    assert all(
        part.endswith(b"\n\n") and part.count(b"\ndata: ") == 1 for part in output
    )
    for action in (
        lambda: events.started(),
        lambda: events.progress("agent"),
        lambda: events.terminal(error_outcome(), clock=Mock()),
    ):
        with pytest.raises(ValueError):
            action()
    assert b'"seq":0' in EventSequence("second").started()
    assert event_spec() == event_spec() and projection_spec() == projection_spec()


def test_sse_cannot_claim_unobserved_internal_stages_or_finish_before_start():
    events = EventSequence("x")
    for action in (
        lambda: events.progress("agent"),
        lambda: events.terminal(error_outcome(), clock=Mock()),
    ):
        with pytest.raises(ValueError):
            action()
    events.started()
    with pytest.raises(ValueError):
        events.progress("narrate")
    assert b'"code":"internal_error"' in events.terminal(
        error_outcome("private diagnostics"), clock=Mock()
    )
