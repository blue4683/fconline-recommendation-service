"""전송 시작 페이싱과 새 기록/기존 기록 호환을 작은 합성 실행으로 확인한다."""

import importlib
import json
import math
import socket
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests
from test_realdata_catalog import CLUB, OBSERVED, Server, template

from FRS.agent.real_material import ObservedMaterial
from FRS.utils.crawling.realdata_catalog import (
    ABILITY_URL,
    CatalogStopped,
    RealDataRun,
    live_fetch,
)
from FRS.utils.crawling.request_timing import (
    SECOND_NS,
    SendStamp,
    SendStartPacer,
    TimedBody,
)
from FRS.utils.crawling.verify_pilot import VerificationError
from FRS.utils.crawling.verify_realdata import check_intervals, check_records, verify

http = importlib.import_module("FRS.utils.crawling.get_response")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("합성 페이싱 테스트는 소켓 연결을 금지합니다.")

    monkeypatch.setattr(socket.socket, "connect", refused)


class Clock:
    def __init__(self):
        self.ns = 0
        self.sleeps = []

    def monotonic_ns(self):
        return self.ns

    def utc(self):
        return OBSERVED + timedelta(microseconds=self.ns // 1000)

    def advance(self, seconds):
        self.ns += math.ceil(seconds * SECOND_NS)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)

    def pacer(self, **options):
        return SendStartPacer(
            monotonic_ns=self.monotonic_ns,
            sleep=options.get("sleep", self.sleep),
            clock=options.get("clock", self.utc),
        )


def page(status=200, content=b"synthetic"):
    return Mock(status_code=status, content=content)


def test_response_and_recording_fit_inside_the_one_second_interval():
    clock = Clock()
    pacer = clock.pacer()
    starts = []
    for _ in range(4):
        pacer.wait()
        starts.append(pacer.mark_started().started_ns)
        clock.advance(0.3)  # 응답
        clock.advance(0.1)  # 파일·파싱
    assert starts == [0, SECOND_NS, 2 * SECOND_NS, 3 * SECOND_NS]
    assert clock.sleeps == [0.6] * 3


def test_slow_response_needs_no_extra_sleep():
    clock = Clock()
    pacer = clock.pacer()
    pacer.wait()
    pacer.mark_started()
    clock.advance(1.5)
    pacer.wait()
    assert pacer.mark_started().started_ns == 1_500_000_000
    assert clock.sleeps == []


def test_early_sleep_wakeup_is_rechecked_before_dispatch():
    clock = Clock()

    def early_sleep(seconds):
        clock.advance(seconds / 2)

    pacer = clock.pacer(sleep=early_sleep)
    pacer.mark_started()
    pacer.wait()
    assert pacer.mark_started().started_ns >= SECOND_NS


def test_wall_clock_changes_do_not_shorten_the_monotonic_wait():
    clock = Clock()
    walls = iter([OBSERVED, OBSERVED - timedelta(hours=1)])
    pacer = clock.pacer(clock=lambda: next(walls))
    first = pacer.mark_started()
    pacer.wait()
    second = pacer.mark_started()
    assert second.sent_at < first.sent_at
    assert second.started_ns - first.started_ns == SECOND_NS


def test_dispatch_rejects_a_missing_wait_and_a_reversed_monotonic_clock():
    clock = Clock()
    pacer = clock.pacer()
    pacer.mark_started()
    clock.advance(0.999)
    with pytest.raises(ValueError, match="1초"):
        pacer.mark_started()
    clock.ns = -1
    with pytest.raises(ValueError, match="비음수"):
        pacer.wait()


def test_shared_fetch_keeps_its_original_default_wait(monkeypatch):
    sleeps = Mock()
    monkeypatch.setattr(http.time, "sleep", sleeps)
    transport = Mock(return_value=page())
    http.fetch_url(("https://example.invalid", None, None), transport=transport)
    sleeps.assert_called_once_with(1.0)
    transport.assert_called_once()
    with pytest.raises(ValueError):
        http.RequestPolicy(request_interval=0)


def test_live_adapter_stamps_dispatch_and_does_not_add_the_default_sleep(monkeypatch):
    clock = Clock()
    stamps = []

    def request(*args, **kwargs):
        stamps.append(clock.ns)
        clock.advance(0.3)
        return page()

    sleep = Mock(side_effect=AssertionError("공용 기본 sleep을 사용하면 안 됩니다"))
    monkeypatch.setattr(http.time, "sleep", sleep)
    session = Mock(request=request)
    fetch = live_fetch(session, pacer=clock.pacer())
    first = fetch("https://example.invalid", {"spid": "123000001"}, "ability")
    second = fetch("https://example.invalid", {"spid": "123000002"}, "ability")
    assert stamps == [0, SECOND_NS]
    assert first.stamp.sent_at == OBSERVED
    assert second.stamp.sent_at == OBSERVED + timedelta(seconds=1)
    assert first.body == second.body == b"synthetic"
    sleep.assert_not_called()


@pytest.mark.parametrize("status", [302, 403, 429, 500, 502, 503, 504])
def test_http_errors_stop_after_one_attempt(status):
    response = page(status)
    session = Mock()
    session.request.return_value = response
    with pytest.raises(CatalogStopped):
        live_fetch(session, pacer=Clock().pacer())("https://example.invalid", {}, "x")
    assert session.request.call_count == 1
    response.close.assert_called_once()


@pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectionError()])
def test_connection_errors_are_not_retried(error):
    session = Mock()
    session.request.side_effect = error
    with pytest.raises(CatalogStopped):
        live_fetch(session, pacer=Clock().pacer())("https://example.invalid", {}, "x")
    assert session.request.call_count == 1


def test_thirty_second_response_limit_still_stops_slow_responses(monkeypatch):
    clock = Clock()
    response = page()

    def request(*args, **kwargs):
        clock.advance(30.01)
        return response

    monkeypatch.setattr(http.time, "monotonic", lambda: clock.ns / SECOND_NS)
    session = Mock(request=request)
    with pytest.raises(CatalogStopped, match="slow_response"):
        live_fetch(session, pacer=clock.pacer())("https://example.invalid", {}, "x")
    response.close.assert_called_once()


def test_pacing_wait_is_not_counted_as_network_response_time(monkeypatch):
    clock = Clock()
    delays = iter([0.3, 29.8])
    responses = []

    def request(*args, **kwargs):
        clock.advance(next(delays))
        result = page()
        responses.append(result)
        return result

    monkeypatch.setattr(http.time, "monotonic", lambda: clock.ns / SECOND_NS)
    fetch = live_fetch(Mock(request=request), pacer=clock.pacer())
    fetch("https://example.invalid", {}, "x")
    fetch("https://example.invalid", {}, "x")
    assert clock.sleeps == [0.7]
    assert len(responses) == 2
    for response in responses:
        response.close.assert_called_once()


def collect_small(root, *, version=2):
    clock = Clock()
    server = Server()
    reservations = []

    def request(method, url, **options):
        state = json.loads((root / "raw/real-data-synthetic/manifest.json").read_text())
        reservations.append((state["attempts"], state["pending"]["attempt"]))
        body = server(url, options["data"], "ability" if url == ABILITY_URL else "x")
        clock.advance(0.3)
        return page(content=body)

    session = Mock(request=request)
    fetch = live_fetch(session, pacer=clock.pacer()) if version == 2 else server

    # 기존 형식은 실제로 대기하지 않고 합성 시계를 요청마다 2초 움직인다.
    def legacy_clock():
        clock.advance(2)
        return clock.utc()

    run = RealDataRun(
        root / "raw/real-data-synthetic",
        root / "processed/real-data-synthetic",
        fetch,
        run_id="real-data-synthetic",
        clubs=[CLUB],
        template=template(),
        max_attempts=20,
        allow_empty_cells=True,
        clock=clock.utc if version == 2 else legacy_clock,
        timing_version=version,
    )
    report = run.execute()
    assert report["status"] == "completed"
    return run, report, reservations


def test_new_small_run_replays_and_verifies_without_network(tmp_path):
    run, report, reservations = collect_small(tmp_path)
    assert report["timing_version"] == run.state["timing_version"] == 2
    assert all(a == b for a, b in reservations)
    assert verify(run.raw_path, run.processed_path, allow_empty_cells=True)["verified"]
    assert all(r["sent_at"] == r["observed_at"] for r in run.state["responses"])
    material = ObservedMaterial(run.processed_path.parent, run.run_id)
    assert len(material.load(material.batches)["cards"]) == 1


def test_legacy_run_keeps_the_original_format_and_replays(tmp_path):
    root = tmp_path / "legacy"
    run, report, _ = collect_small(root, version=1)
    assert "timing_version" not in report and "timing_version" not in run.state
    assert all("send_started_ns" not in e for e in run.state["responses"])
    assert verify(run.raw_path, run.processed_path, allow_empty_cells=True)["verified"]


def test_manifest_and_report_versions_must_match(tmp_path):
    run, report, _ = collect_small(tmp_path)
    report.pop("timing_version")
    (run.processed_path / "report.json").write_text(json.dumps(report))
    with pytest.raises(VerificationError, match="시각 형식 불일치"):
        check_records(run.raw_path, run.processed_path)


@pytest.mark.parametrize("delta", [999_000_000, 999_999_999])
def test_new_interval_rejects_even_one_nanosecond_below_one_second(delta):
    records = [
        dict(
            observed_at=OBSERVED.isoformat(),
            sent_at=OBSERVED.isoformat(),
            send_started_ns=n,
        )
        for n in (0, delta)
    ]
    with pytest.raises(VerificationError, match="간격"):
        check_intervals(dict(timing_version=2, min_request_interval_seconds=1), records)


def test_legacy_tolerance_is_not_retroactively_changed():
    check_intervals(
        dict(min_request_interval_seconds=1),
        [
            dict(observed_at=OBSERVED.isoformat()),
            dict(observed_at=(OBSERVED + timedelta(seconds=0.999)).isoformat()),
        ],
    )


@pytest.mark.parametrize("version", [None, True, 0, 3, "2"])
def test_unknown_timing_versions_cannot_fall_back_to_legacy(version):
    with pytest.raises(VerificationError, match="형식"):
        check_intervals(
            dict(timing_version=version, min_request_interval_seconds=1), []
        )


def test_new_timing_requires_every_timestamp():
    with pytest.raises(VerificationError, match="전송 시작"):
        check_intervals(
            dict(timing_version=2, min_request_interval_seconds=1),
            [dict(observed_at=OBSERVED.isoformat())],
        )


@pytest.mark.parametrize(
    "stamp,started",
    [
        ("not-a-time", 0),
        (OBSERVED.replace(tzinfo=None).isoformat(), 0),
        (OBSERVED.isoformat(), True),
        (OBSERVED.isoformat(), -1),
        (OBSERVED.isoformat(), "0"),
    ],
)
def test_malformed_new_timestamps_produce_a_verification_refusal(stamp, started):
    with pytest.raises(VerificationError, match="전송 시작"):
        check_intervals(
            dict(timing_version=2, min_request_interval_seconds=1),
            [dict(observed_at=stamp, sent_at=stamp, send_started_ns=started)],
        )


def test_new_mode_does_not_silently_accept_legacy_bodies(tmp_path):
    run = RealDataRun(
        tmp_path / "raw",
        tmp_path / "out",
        lambda *args: b"synthetic",
        run_id="real-data-synthetic",
        clubs=[CLUB],
        template=template(),
        max_attempts=2,
        timing_version=2,
    )
    report = run.execute()
    assert report["status"] == "stopped" and report["stop_reason"]["kind"] == "timing"
    assert run.state["attempts"] == run.state["pending"]["attempt"] == 1


def test_timing_objects_reject_invalid_metadata():
    with pytest.raises(ValueError):
        SendStamp(OBSERVED.replace(tzinfo=None), 0)
    with pytest.raises(ValueError):
        SendStamp(OBSERVED, True)
    with pytest.raises(ValueError):
        TimedBody(b"x", None)
