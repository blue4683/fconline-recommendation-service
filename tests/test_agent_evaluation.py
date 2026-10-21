"""고정 정답·오류 채점·중단 분모·합성 실행을 검증하며 네트워크를 금지한다."""

import copy
import hashlib
import json
import socket
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from FRS.agent.candidate_graph import CandidateSquadAgent
from FRS.agent.extraction import ExtractionError
from FRS.agent.material import SyntheticLeagueSpec, generate_synthetic_material
from FRS.api.agent_projection import project_result
from FRS.evaluation import corpus, offline
from FRS.evaluation.corpus import load_cases
from FRS.evaluation.offline import (
    OracleSelector,
    assess_case,
    evaluate_case,
    evaluation_plan,
    latency,
    run_offline,
    summarize,
    verify_fixture,
)


@pytest.fixture(scope="module", autouse=True)
def no_network():
    def reject(*args, **kwargs):
        raise AssertionError("평가 단위 테스트의 네트워크 요청 금지")

    with pytest.MonkeyPatch.context() as guard:
        guard.setattr(socket.socket, "connect", reject)
        guard.setattr(socket, "create_connection", reject)
        yield


@pytest.fixture(scope="module")
def cases():
    return load_cases()


@pytest.fixture(scope="module")
def spec():
    return SyntheticLeagueSpec(
        run_id="w6-test-fixture",
        seed=123,
        members_per_family=3,
        observed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
    )


@pytest.fixture(scope="module")
def material(spec):
    return generate_synthetic_material(spec)


@pytest.fixture(scope="module")
def report(spec, tmp_path_factory):
    root = tmp_path_factory.mktemp("w6-reports")
    return run_offline(spec, artifact_root=root), root


def test_fixed_corpus_hash_and_independent_zero_salary_anchor(cases):
    assert len(cases) == 25
    assert (
        hashlib.sha256(corpus.CORPUS_PATH.read_bytes()).hexdigest()
        == corpus.CORPUS_SHA256
    )
    case = next(c for c in cases if c.case_id == "I02")
    span = case.expected_evidence().salary_cap[0]
    assert span.start == case.original.index("급여 0") + 3
    assert span.text == "0" and span.start > case.original.index("10조")
    conflict = next(c for c in cases if c.case_id == "A01")
    prices = conflict.expected_evidence().price_cap
    assert [s.text for s in prices] == ["1조", "2조"] and prices[0].end < prices[
        1
    ].start


@pytest.mark.parametrize("target", ["document", "corpus"])
def test_corpus_or_document_tamper_refuses_load(target, monkeypatch, tmp_path):
    original = corpus.DOCUMENT_PATH if target == "document" else corpus.CORPUS_PATH
    changed = tmp_path / "changed"
    changed.write_bytes(original.read_bytes() + b" ")
    monkeypatch.setattr(
        corpus, "DOCUMENT_PATH" if target == "document" else "CORPUS_PATH", changed
    )
    with pytest.raises(ValueError, match="고정 코퍼스"):
        load_cases()


def test_plan_is_not_an_execution_and_fixes_versions(spec, monkeypatch):
    generate = Mock(side_effect=AssertionError("계획에서 생성 금지"))
    monkeypatch.setattr(offline, "generate_synthetic_material", generate)
    plan = evaluation_plan(spec)
    assert plan["actual_model_requests"] == plan["external_requests"] == 0
    assert (
        plan["expected_selector_calls"] == 16 and plan["expected_prepare_blocks"] == 9
    )
    assert plan["pass_thresholds"] is None and len(plan["git"]["head"]) == 40
    assert plan["versions"]["solver"] == dict(
        max_time_seconds=10.0, workers=1, seed=123
    )
    assert (
        plan["source_sha256"]["FRS/agent/candidate_graph.py"]
        == hashlib.sha256(
            (corpus.ROOT / "FRS/agent/candidate_graph.py").read_bytes()
        ).hexdigest()
    )
    generate.assert_not_called()


@pytest.mark.parametrize("change", [{"seed": 124}, {"members_per_family": 2}])
def test_changed_fixture_contract_is_rejected(spec, change):
    with pytest.raises(ValueError, match="seed123"):
        evaluation_plan(spec.model_copy(update=change))


def test_preflight_proves_eight_valid_witnesses_and_three_lower_bounds(
    material, spec, cases
):
    proof = verify_fixture(material, spec, cases)
    assert len(proof["normal_witnesses"]) == 8
    for row in proof["normal_witnesses"]:
        v = row["validation"]
        assert v["valid"] and len(v["players"]) == 11
        assert len({p["person_id"] for p in v["players"]}) == 11
    assert len(proof["infeasible_proofs"]) == 3
    assert all(p["budget"] or p["salary"] for p in proof["infeasible_proofs"])


@pytest.mark.parametrize(
    "case_id",
    [
        f"{g}{i:02}"
        for g, n in dict(N=8, I=3, U=5, M=4, A=5).items()
        for i in range(1, n + 1)
    ],
)
def test_all_predetermined_outcomes_and_call_boundaries(case_id, report, cases):
    r, _ = report
    row = next(c for c in r["cases"] if c["case_id"] == case_id)
    case = next(c for c in cases if c.case_id == case_id)
    assert row["execution"] == "completed" and row["assessment"]["e2e_match"]
    assert row["actual_status"] in case.outcomes
    assert row["selector_attempts"] == (0 if case.stage == "prepare" else 1)
    assert row["material_loads"] == (1 if case.stage == "engine" else 0)
    assert row["actual_model_requests"] == 0 and row["tokens"] is None
    if case.group != "N":
        assert row["presentation"]["squad"] is None


def test_persisted_manifest_markdown_and_no_overwrite(report, spec):
    r, root = report
    destination = root / spec.run_id
    saved = json.loads((destination / "manifest.json").read_text())
    assert saved == r and r["summary"]["e2e"] == dict(passed=25, total=25, rate=1.0)
    assert r["summary"]["selected_semantics"]["total"] == 16
    assert r["summary"]["constraints"]["total"] == 8
    assert r["summary"]["actual_model_metrics"] is None
    assert sum(c["selector_attempts"] for c in r["cases"]) == 16
    text = (destination / "report.md").read_text()
    assert "실제 모델 정확도가 아닌" in text and "미측정" in text
    before = (destination / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_offline(spec, artifact_root=root)
    assert (destination / "manifest.json").read_bytes() == before


@pytest.fixture(scope="module")
def success(cases, material, spec):
    case = cases[0]
    selector = OracleSelector(case)
    result = CandidateSquadAgent(
        selector,
        material=material,
        batches=material.batches,
        clock=lambda: spec.observed_at,
    ).run(case.original, t0=spec.observed_at)
    body = project_result(
        case.original,
        result,
        clock=lambda: spec.observed_at,
        material_id=material.material_id,
    ).to_dict(clock=lambda: spec.observed_at)
    return case, result, body


@pytest.mark.parametrize(
    "mutation",
    [
        "price",
        "salary",
        "person",
        "grade",
        "slot",
        "total",
        "name",
        "conditions",
        "status",
        "snapshot",
    ],
)
def test_table_corruption_is_not_scored_as_success(mutation, success, spec):
    case, result, original = success
    body = copy.deepcopy(original)
    row = body["squad"]["rows"][0]
    if mutation == "price":
        row["price"] = "1"
    elif mutation == "salary":
        row["salary"] = "0"
    elif mutation == "person":
        row["person_id"] = body["squad"]["rows"][1]["person_id"]
    elif mutation == "grade":
        row["reinforce_level"] = 11
    elif mutation == "slot":
        row["slot"] = "ST"
    elif mutation == "total":
        body["squad"]["total_price"] = "1"
    elif mutation == "name":
        row["name"] = "없는 가상선수"
    elif mutation == "conditions":
        body["conditions"]["price_cap"] = "2"
    elif mutation == "status":
        body["status"] = "infeasible"
    else:
        body["snapshot"]["id"] = "0" * 64
    scored = assess_case(
        case, result, body, selector_attempts=1, material_loads=1, when=spec.observed_at
    )
    assert not scored["e2e_match"] and scored["constraints_valid"] is False


@pytest.mark.parametrize("calls,loads", [(0, 1), (2, 1), (1, 0), (1, 2)])
def test_hidden_retries_or_material_reads_fail_boundary_checks(
    calls, loads, success, spec
):
    case, result, body = success
    score = assess_case(
        case,
        result,
        body,
        selector_attempts=calls,
        material_loads=loads,
        when=spec.observed_at,
    )
    assert score["semantics_match"] and not score["e2e_match"]


@pytest.mark.parametrize(
    "kind",
    [
        "missing_id",
        "unknown_id",
        "bad_json",
        "wrong_set",
        "provider_error",
        "unexpected_error",
    ],
)
def test_selection_failures_count_once_without_retry_or_fake_clarification(
    kind, cases, material, spec, monkeypatch
):
    case = next(c for c in cases if c.case_id == "A01")

    class BadSelector:
        calls = 0

        def select(self, registry):
            self.calls += 1
            if kind == "provider_error":
                raise ExtractionError("대역 실패")
            if kind == "unexpected_error":
                raise RuntimeError("내부 값은 보고하지 않음")
            if kind == "bad_json":
                return "invalid-json"
            raw = json.loads(OracleSelector(case).select(registry))
            if kind == "missing_id":
                raw["price_cap"].pop()
            elif kind == "unknown_id":
                raw["price_cap"][0] = "v-999"
            else:
                raw["candidate_set_id"] = "0" * 64
            return json.dumps(raw)

    selector = BadSelector()
    monkeypatch.setattr(offline, "OracleSelector", lambda case: selector)
    row = evaluate_case(case, material, spec)
    assert selector.calls == row["selector_attempts"] == 1
    assert row["execution"] == "technical_error" and row["material_loads"] == 0
    assert (
        not row["assessment"]["e2e_match"] and not row["assessment"]["semantics_match"]
    )
    assert row["retries"] == row["fallback"] == 0
    assert row["schema_valid"] is (kind in ("missing_id", "unknown_id", "wrong_set"))
    assert "내부 값은" not in json.dumps(row, ensure_ascii=False)
    if kind == "provider_error":
        assert row["presentation"]["code"] == "selection_provider_error"


def test_final_expiry_is_not_success_or_infeasibility(cases, material, spec):
    row = evaluate_case(
        cases[0], material, spec, clock=lambda: spec.observed_at + timedelta(days=1)
    )
    assert row["actual_status"] == "data_expired"
    assert row["presentation"]["squad"] is None
    assert row["assessment"]["semantics_match"] and not row["assessment"]["e2e_match"]


def test_stopped_or_unattempted_cases_stay_in_fixed_denominators(report):
    r, _ = report
    rows = copy.deepcopy(r["cases"])
    rows[0] = dict(case_id="N01", group="N", execution="not_attempted")
    rows[1] = dict(
        case_id="N02",
        group="N",
        execution="technical_error",
        selector_attempts=1,
        schema_valid=False,
        evidence_valid=False,
        elapsed_seconds=120.0,
    )
    summary = summarize(rows)
    assert summary["e2e"] == dict(passed=23, total=25, rate=23 / 25)
    assert summary["selected_semantics"]["total"] == 16
    assert summary["valid_squads"]["total"] == 8
    assert summary["not_attempted"] == 1 and summary["technical_errors"] == 1
    assert summary["constraints"]["total"] == 6
    with pytest.raises(ValueError, match="고정25개"):
        summarize(rows[1:])


def test_zero_denominators_are_na_and_nearest_rank_is_explicit(cases):
    rows = [
        dict(case_id=c.case_id, group=c.group, execution="not_attempted") for c in cases
    ]
    summary = summarize(rows)
    assert summary["constraints"]["rate"] is None and summary["schema"]["rate"] is None
    assert summary["e2e"]["total"] == 25 and summary["e2e"]["rate"] == 0
    assert latency([]) == dict(n=0, p50_seconds=None, p95_seconds=None)
    assert latency([5.0, 1.0, 4.0, 2.0, 3.0]) == dict(
        n=5, p50_seconds=3.0, p95_seconds=5.0
    )


def test_preflight_failure_preserves_all_unattempted_cases(spec, tmp_path, monkeypatch):
    monkeypatch.setattr(
        offline, "verify_fixture", Mock(side_effect=ValueError("사전 검증 실패"))
    )
    evaluate = Mock(side_effect=AssertionError("사전 실패 후 실행 금지"))
    monkeypatch.setattr(offline, "evaluate_case", evaluate)
    report = run_offline(spec, artifact_root=tmp_path)
    assert report["status"] == "failed" and report["error_type"] == "ValueError"
    assert report["summary"]["not_attempted"] == 25
    assert report["summary"]["completeness"]["passed"] == 0
    evaluate.assert_not_called()


def test_interruption_persists_pending_and_unattempted_without_reuse(
    spec, tmp_path, monkeypatch
):
    monkeypatch.setattr(offline, "evaluate_case", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_offline(spec, artifact_root=tmp_path)
    saved = json.loads((tmp_path / spec.run_id / "manifest.json").read_text())
    assert saved["status"] == "interrupted"
    assert saved["summary"]["pending"] == 1 and saved["summary"]["not_attempted"] == 24
    assert saved["summary"]["e2e"]["total"] == 25


def test_wrong_meaning_is_failure_even_if_squad_is_valid_for_its_snapshot(
    success, spec
):
    case, result, body = success
    expected = case.model_copy(
        update={
            "conditions": case.conditions.model_copy(
                update={"price_cap": 2_000_000_000_000}
            )
        }
    )
    score = assess_case(
        expected,
        result,
        body,
        selector_attempts=1,
        material_loads=1,
        when=spec.observed_at,
    )
    assert score["constraints_valid"] is True
    assert score["semantics_match"] is False and score["e2e_match"] is False


def test_missing_support_list_does_not_pass_clarification(cases, material, spec):
    case = next(c for c in cases if c.case_id == "M01")
    result = CandidateSquadAgent(
        OracleSelector(case),
        material=material,
        batches=material.batches,
        clock=lambda: spec.observed_at,
    ).run(case.original, t0=spec.observed_at)
    body = project_result(
        case.original,
        result,
        clock=lambda: spec.observed_at,
        material_id=material.material_id,
    ).to_dict(clock=lambda: spec.observed_at)
    body["supported"] = {}
    score = assess_case(
        case, result, body, selector_attempts=1, material_loads=0, when=spec.observed_at
    )
    assert score["semantics_match"] and not score["e2e_match"]


def test_feasible_optimal_label_must_match_engine_and_snapshot_time(success, spec):
    case, result, original = success
    assert original["status"] == "optimal"
    body = copy.deepcopy(original)
    body["status"] = "feasible"
    scored = assess_case(
        case, result, body, selector_attempts=1, material_loads=1, when=spec.observed_at
    )
    assert not scored["e2e_match"]
    body = copy.deepcopy(original)
    body["snapshot"]["valid_until"] = "2099-01-01T00:00:00+00:00"
    scored = assess_case(
        case, result, body, selector_attempts=1, material_loads=1, when=spec.observed_at
    )
    assert not scored["e2e_match"]
