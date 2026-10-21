"""실모델 실행기의 전송/상한/중단을 합성 응답으로 검증한다. 외부 접속 없음."""

import json
import socket
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx2
import pytest

from FRS.agent.candidates import prepare_candidates
from FRS.agent.material import SyntheticLeagueSpec
from FRS.evaluation import model
from FRS.evaluation.corpus import load_cases
from FRS.evaluation.offline import OracleSelector


@pytest.fixture(scope="module", autouse=True)
def no_network():
    with pytest.MonkeyPatch.context() as guard:

        def reject(*args, **kwargs):
            raise AssertionError("합성 검증에서 외부 소켓 접속 금지")

        guard.setattr(socket.socket, "connect", reject)
        guard.setattr(socket, "create_connection", reject)
        yield


@pytest.fixture(scope="module")
def spec():
    return SyntheticLeagueSpec(
        run_id=model.RUN_ID,
        seed=123,
        members_per_family=3,
        observed_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
    )


class Fixtures:
    def __init__(self, root, *, metadata_error=None, chat_error=None):
        self.root = root
        self.metadata_error = metadata_error
        self.chat_error = chat_error
        self.metadata_requests = []
        self.chat_requests = []
        self.cases = {c.original: c for c in load_cases()}

    def saved(self):
        return json.loads((self.root / model.RUN_ID / "manifest.json").read_text())

    def metadata(self, request):
        self.metadata_requests.append(request)
        saved = self.saved()
        assert len(saved["metadata_attempts"]) == len(self.metadata_requests)
        assert saved["metadata_attempts"][-1]["status"] == "pending"
        assert request.method == "GET" and request.url.host == "127.0.0.1"
        if self.metadata_error in (302, 403, 429):
            return httpx2.Response(
                self.metadata_error, headers={"Location": "https://example.invalid"}
            )
        if self.metadata_error == "connect":
            raise httpx2.ConnectError("대역 연결 실패", request=request)
        if request.url.path == "/api/version":
            return httpx2.Response(200, json={"version": "fixture-version"})
        assert request.url.path == "/api/tags"
        return httpx2.Response(
            200,
            json={
                "models": [
                    {
                        "name": model.MODEL,
                        "digest": "0" * 64
                        if self.metadata_error == "digest"
                        else model.DIGEST,
                    }
                ]
            },
        )

    def chat(self, request):
        self.chat_requests.append(request)
        saved = self.saved()
        assert len(saved["inference_attempts"]) == len(self.chat_requests)
        assert saved["inference_attempts"][-1]["status"] == "pending"
        body = json.loads(request.content)
        assert request.url.host == "127.0.0.1" and request.url.path == "/api/chat"
        assert (
            body["model"] == model.MODEL
            and body["keep_alive"] == 0
            and body["think"] is False
        )
        data = json.loads(body["messages"][1]["content"])
        case = self.cases[data["original"]]
        assert case.stage != "prepare"
        if self.chat_error == "connect":
            raise httpx2.ConnectError("대역 연결 실패", request=request)
        if self.chat_error == "timeout":
            raise httpx2.ReadTimeout("대역 시간 초과", request=request)
        if self.chat_error == 429:
            return httpx2.Response(429)
        if self.chat_error == "envelope":
            return httpx2.Response(200, json={"done": False})
        raw = OracleSelector(case).select(prepare_candidates(case.original).registry)
        if self.chat_error == "drop_conflict" and case.case_id == "A01":
            payload = json.loads(raw)
            payload["price_cap"].pop()
            raw = json.dumps(payload)
        return httpx2.Response(
            200,
            json=dict(
                done=True,
                done_reason="stop",
                model=model.MODEL,
                message=dict(role="assistant", content=raw),
                prompt_eval_count=20,
                eval_count=10,
                load_duration=1_000_000,
                eval_duration=2_000_000,
                total_duration=3_000_000,
            ),
        )

    def run(self, spec):
        return model.run_model_evaluation(
            spec,
            approval_root=self.root,
            metadata_transport=httpx2.MockTransport(self.metadata),
            chat_transport=httpx2.MockTransport(self.chat),
            clock=lambda: spec.observed_at,
        )


def test_plan_is_explicit_without_request_or_lock(spec, tmp_path, monkeypatch):
    verify = Mock(side_effect=AssertionError("계획에서 상태 조회 금지"))
    monkeypatch.setattr(model, "verify_model", verify)
    plan = model.model_plan(spec)
    assert plan["inference_limit"] == 16 and plan["metadata_limit"] == 2
    assert plan["model"] == model.MODEL and plan["model_digest"] == model.DIGEST
    assert sum(p["request"] is None for p in plan["preparations"]) == 9
    assert all(
        p["request"] is None or p["request"]["model"] == model.MODEL
        for p in plan["preparations"]
    )
    assert not (tmp_path / "approval-consumed.lock").exists()
    verify.assert_not_called()


def test_unapproved_run_id_is_rejected(spec):
    with pytest.raises(ValueError, match="승인된"):
        model.model_plan(spec.model_copy(update={"run_id": "different-run"}))


def test_full_mocked_run_pre_reserves_attempts_and_retains_denominators(spec, tmp_path):
    fixture = Fixtures(tmp_path)
    report = fixture.run(spec)
    assert report["status"] == "completed"
    assert len(fixture.metadata_requests) == 2 and len(fixture.chat_requests) == 16
    assert report["summary"]["e2e"] == dict(passed=25, total=25, rate=1.0)
    assert (
        report["summary"]["actual_model_metrics"]["selected_semantics"]["total"] == 16
    )
    assert report["summary"]["constraints"]["passed"] == 8
    assert sum(c["actual_model_requests"] for c in report["cases"]) == 16
    for row in report["cases"]:
        if row["case_id"].startswith("U") or row["case_id"] in (
            "A02",
            "A03",
            "A04",
            "A05",
        ):
            assert row["actual_model_requests"] == row["material_loads"] == 0
    assert report["model_verification"]["digest"] == model.DIGEST
    assert report["summary"]["token_totals"]["eval_count"]["sum_available"] == 160
    text = (tmp_path / model.RUN_ID / "report.md").read_text()
    assert "실모델·합성 자료" in text and "코드 판단" in text
    before = (tmp_path / model.RUN_ID / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        fixture.run(spec)
    assert len(fixture.metadata_requests) == 2 and len(fixture.chat_requests) == 16
    assert (tmp_path / model.RUN_ID / "manifest.json").read_bytes() == before


@pytest.mark.parametrize("failure", [302, 403, 429, "connect", "digest"])
def test_metadata_error_stops_and_consumes_approval(failure, spec, tmp_path):
    fixture = Fixtures(tmp_path, metadata_error=failure)
    report = fixture.run(spec)
    assert report["status"] == "stopped_preflight"
    assert len(report["metadata_attempts"]) == (2 if failure == "digest" else 1)
    assert len(fixture.chat_requests) == 0 and report["summary"]["not_attempted"] == 25
    assert report["summary"]["schema"]["rate"] is None
    assert report["summary"]["token_totals"]["eval_count"]["sum_available"] is None
    with pytest.raises(FileExistsError):
        fixture.run(spec)


@pytest.mark.parametrize("failure", ["connect", "timeout", 429, "envelope"])
def test_fatal_model_error_counts_once_and_stops(failure, spec, tmp_path):
    fixture = Fixtures(tmp_path, chat_error=failure)
    report = fixture.run(spec)
    assert report["status"] == "stopped"
    assert len(report["inference_attempts"]) == len(fixture.chat_requests) == 1
    assert report["cases"][0]["execution"] == "technical_error"
    assert report["summary"]["not_attempted"] == 24
    assert report["summary"]["schema"] == dict(passed=0, total=1, rate=0.0)
    assert report["summary"]["selected_semantics"]["total"] == 16
    assert report["summary"]["e2e"]["total"] == 25
    assert report["summary"]["retries"] == report["summary"]["fallback"] == 0


def test_quality_failure_never_retries_or_discards_case(spec, tmp_path):
    fixture = Fixtures(tmp_path, chat_error="drop_conflict")
    report = fixture.run(spec)
    assert report["status"] == "completed" and len(fixture.chat_requests) == 16
    assert report["summary"]["e2e"] == dict(passed=24, total=25, rate=24 / 25)
    row = next(c for c in report["cases"] if c["case_id"] == "A01")
    assert row["execution"] == "technical_error" and row["actual_model_requests"] == 1
    assert (
        row["presentation"]["code"] == "incomplete_coverage"
        and row["material_loads"] == 0
    )
    assert report["summary"]["schema"]["passed"] == 16
    assert report["summary"]["evidence"]["passed"] == 15


def test_expired_observation_is_not_refreshed_or_queried(spec, tmp_path):
    report = model.run_model_evaluation(
        spec, approval_root=tmp_path, clock=lambda: spec.observed_at + timedelta(days=1)
    )
    assert report["status"] == "failed" and report["metadata_attempts"] == []
    assert report["summary"]["not_attempted"] == 25


def test_interrupt_keeps_unknown_attempt_in_denominator(spec, tmp_path, monkeypatch):
    fixture = Fixtures(tmp_path)
    monkeypatch.setattr(
        model.OllamaIDSelector, "sample", Mock(side_effect=KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run(spec)
    report = fixture.saved()
    assert report["status"] == "interrupted"
    assert (
        len(report["inference_attempts"]) == 1
        and report["inference_attempts"][0]["status"] == "pending"
    )
    assert report["cases"][0]["actual_model_requests"] == 1
    assert report["summary"]["schema"] == dict(passed=0, total=1, rate=0.0)
    assert (
        report["summary"]["pending"] == 1 and report["summary"]["not_attempted"] == 24
    )


def test_source_change_after_plan_stops_before_metadata(spec, tmp_path, monkeypatch):
    monkeypatch.setattr(model, "_unchanged", Mock(side_effect=ValueError("계획 변경")))
    report = model.run_model_evaluation(
        spec, approval_root=tmp_path, clock=lambda: spec.observed_at
    )
    assert (
        report["status"] == "failed"
        and report["metadata_attempts"] == report["inference_attempts"] == []
    )


def test_transport_limits_reject_an_extra_attempt_without_transmission(spec):
    inner = Mock(side_effect=AssertionError("상한 뒤 전송 금지"))
    report = dict(
        metadata_attempts=[{}, {}], inference_attempts=[{} for _ in range(16)]
    )
    transport = model._MetadataTransport(inner, report, lambda: None)
    with pytest.raises(ValueError, match="상태 요청"):
        transport.handle_request(
            httpx2.Request("GET", "http://127.0.0.1:11434/api/tags")
        )
    case = load_cases()[0]
    selector = model._ModelSelector(case, report, lambda: None, inner)
    with pytest.raises(ValueError, match="추론 상한"):
        selector.select(prepare_candidates(case.original).registry)
    assert (
        len(report["metadata_attempts"]) == 2
        and len(report["inference_attempts"]) == 16
    )
    inner.assert_not_called()
