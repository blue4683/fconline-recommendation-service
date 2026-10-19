"""층화 선별·승인 상한·재시도 금지·중단/재개를 합성 응답으로 검증한다."""

import importlib
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_ability_parser import ability_html

from FRS.utils.crawling.enrich_cards import (
    COLOR_IDS,
    MAX_ATTEMPTS,
    MAX_CARDS,
    CardEnrichment,
    plan,
    select_candidates,
)
from FRS.utils.crawling.pilot import PilotStopped

http = importlib.import_module("FRS.utils.crawling.get_response")


def candidates():
    output = []
    for color in COLOR_IDS:
        for i, (lower, upper) in enumerate(
            ((4, 20), (21, 25), (26, 30), (31, 35), (36, 99))
        ):
            entry = {
                "form": dict(
                    teamcolorid=color,
                    n4SalaryMin=str(lower),
                    n4SalaryMax=str(upper),
                    n1Strong="5",
                    n4PageNo="1",
                    n4LeagueId="0",
                    n4TeamId="",
                    strTeamColorCategory="affiliation",
                )
            }
            rows = [
                SimpleNamespace(
                    spid=123000001 + i * 10 + j,
                    salary=lower,
                    listed_position_ovr={"ST": 100},
                )
                for j in range(3)
            ]
            output.append((entry, rows))
    return output


def sender(method, url, **options):
    if method == "POST":
        form = options["data"]
        return Mock(
            status_code=200,
            content=ability_html(form["n1Strong"], form["spid"]).encode(),
        )
    return Mock(
        status_code=200,
        content=b'<div class="data_detail_club"><div class="data_table"><div class="td club">synthetic-club</div></div></div>',
    )


@pytest.fixture
def collection(tmp_path, monkeypatch):
    monkeypatch.setattr(http.time, "sleep", Mock())
    ticks = count(0, 2)

    def clock():
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=next(ticks)
        )

    monkeypatch.setattr("FRS.utils.crawling.pilot.now", clock)
    monkeypatch.setattr("FRS.utils.crawling.enrich_cards.now", clock)
    session = Mock()
    session.request.side_effect = sender
    return CardEnrichment(
        tmp_path / "raw",
        tmp_path / "processed",
        session,
        selection={"selected_ids": [123000001]},
    )


def resume(collection):
    session = Mock()
    session.request.side_effect = sender
    return CardEnrichment(
        collection.raw_path,
        collection.processed_path,
        session,
        selection=collection.selection,
        resume=True,
    )


def test_selection_is_deterministic_and_distinct_with_explicit_scope():
    result = select_candidates(candidates())
    reversed_rows = [(e, list(reversed(rows))) for e, rows in reversed(candidates())]
    assert result == select_candidates(reversed_rows)
    assert len(result["selected_ids"]) == 15
    assert result["remaining_listed_target_gaps"] == 0
    assert result["listed_nonempty_cells"] == 15
    limited = select_candidates(candidates(), limit=5)
    assert (
        len(limited["selected_ids"]) == 5
        and limited["remaining_listed_target_gaps"] > 0
    )
    policy = plan(result)
    assert policy["normal_attempts"] == 120 and policy["retries"] == 0
    assert policy["max_attempts_including_retries"] == MAX_ATTEMPTS == 2100
    assert policy["max_cards"] == MAX_CARDS == 260


@pytest.mark.parametrize(
    "case",
    ["missing-query", "duplicate-query", "wrong-club", "out-of-band", "conflict"],
)
def test_selection_rejects_changed_or_conflicting_evidence(case):
    results = candidates()
    if case == "missing-query":
        results.pop()
    elif case == "duplicate-query":
        results.append(results[0])
    elif case == "wrong-club":
        results[0][0]["form"]["teamcolorid"] = "0"
    elif case == "out-of-band":
        results[0][1][0].salary = 99
    elif case == "conflict":
        results[5][1][0].listed_position_ovr = {"CM": 100}
    with pytest.raises(PilotStopped):
        select_candidates(results)


def test_complete_enrichment_has_seven_linked_grades_and_no_extra_requests(collection):
    report = collection.execute()
    assert report["status"] == "completed" and report["attempts"] == 8
    assert report["completed_cards"] == 1
    assert report["http_failure_attempts"] == report["parse_failures"] == 0
    data = collection.load_card(123000001)
    assert {key: len(values) for key, values in data.items()} == dict(
        player_card=1, card_salary=7, reinforce_stats=7, team_color=2
    )
    base = data["player_card"][0]
    for delta, salary in zip(data["reinforce_stats"], data["card_salary"], strict=True):
        assert delta.baseline_card_version == base.version
        assert delta.version == salary.version
        assert delta.reinforce_level == salary.reinforce_level
        assert set(delta.stats_delta.values()) == {delta.reinforce_level - 5}
    assert all(r.status == "unknown" for values in data.values() for r in values)
    requests = collection.session.request.call_count
    collection.fetch("ability", 123000001, 5)
    assert collection.session.request.call_count == requests
    with pytest.raises(PilotStopped, match="재개"):
        resume(collection)


@pytest.mark.parametrize("status", [302, 403, 404, 429, 500, 503])
def test_http_error_stops_once_without_retry_or_resume(collection, status):
    collection.session.request.side_effect = None
    collection.session.request.return_value = Mock(status_code=status, content=b"error")
    report = collection.execute()
    assert report["status"] == "stopped" and report["attempts"] == 1
    assert collection.session.request.call_count == 1
    with pytest.raises(PilotStopped):
        resume(collection)


def test_budget_is_reserved_before_request_and_never_exceeded(collection):
    collection.max_attempts = 1
    report = collection.execute()
    assert report["status"] == "stopped" and report["attempts"] == 1
    assert collection.session.request.call_count == 1
    assert collection.state["pending"]["spid"] == 123000001


def test_unknown_interrupted_request_cannot_be_retried(collection):
    collection.session.request.side_effect = KeyboardInterrupt
    report = collection.execute()
    assert report["status"] == "interrupted" and report["attempts"] == 1
    with pytest.raises(PilotStopped, match="결과 미상"):
        resume(collection)


def test_interrupt_between_requests_reuses_received_response(collection, monkeypatch):
    original = collection.ability

    def interrupt_after_first(spid, grade):
        if grade == 6:
            raise KeyboardInterrupt
        return original(spid, grade)

    monkeypatch.setattr(collection, "ability", interrupt_after_first)
    assert collection.execute()["status"] == "interrupted"
    assert collection.state["attempts"] == 1
    continued = resume(collection)
    report = continued.execute()
    assert report["status"] == "completed" and report["attempts"] == 8
    assert continued.session.request.call_count == 7


@pytest.mark.parametrize(
    "resource,spid,grade",
    [
        ("metadata", None, None),
        ("ability", 123000002, 5),
        ("ability", 123000001, 4),
        ("detail", 123000001, 5),
        ("ability", 123000001, 12),
    ],
)
def test_unapproved_request_is_rejected_before_network(
    collection, resource, spid, grade
):
    with pytest.raises(PilotStopped):
        collection.fetch(resource, spid, grade)
    assert (
        collection.state["attempts"] == 0 and collection.session.request.call_count == 0
    )


def test_changed_selection_or_processed_hash_is_rejected(collection):
    assert collection.execute()["status"] == "completed"
    path = collection.processed_path / "123000001.json"
    path.write_text(path.read_text().replace('"salary": 20', '"salary": 21'))
    with pytest.raises(PilotStopped, match="해시"):
        collection.load_card(123000001)
    collection.state["status"] = "interrupted"
    collection.flush()
    with pytest.raises(PilotStopped, match="선별 근거"):
        CardEnrichment(
            collection.raw_path,
            collection.processed_path,
            Mock(),
            selection={"selected_ids": [123000002]},
            resume=True,
        )


def test_offline_verifier_checks_all_grades_and_source_numbers(collection):
    from FRS.utils.crawling.verify_enrichment import verify

    collection.execute()
    result = verify(
        collection.raw_path,
        collection.processed_path,
        expected_selection=collection.selection,
    )
    assert result["verified"] and result["cards"] == 1
    assert result["source_stat_values_compared"] == 7 * 34
    assert result["recommendation_ready"] is False


@pytest.mark.parametrize("case", ["raw", "salary", "provenance", "log"])
def test_offline_verifier_detects_tampered_artifacts(collection, case):
    import hashlib
    import json

    from FRS.utils.crawling.verify_enrichment import verify
    from FRS.utils.crawling.verify_pilot import VerificationError

    collection.execute()
    if case == "raw":
        entry = collection.state["responses"][0]
        (collection.raw_path / entry["path"]).write_bytes(b"changed")
    elif case == "salary":
        path = collection.processed_path / "123000001.json"
        payload = json.loads(path.read_text())
        payload["card_salary"][1]["salary"] += 1
        path.write_text(json.dumps(payload))
        collection.state["completed_hashes"]["123000001"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        collection.flush()
    elif case == "provenance":
        collection.state["responses"][0]["form"]["n1Strong"] = 13
        collection.flush()
    else:
        path = collection.processed_path / "crawl_log.jsonl"
        lines = path.read_text().splitlines()
        value = json.loads(lines[1])
        value["attempt"] = 1
        lines[1] = json.dumps(value)
        path.write_text("\n".join(lines) + "\n")
    with pytest.raises((VerificationError, PilotStopped)):
        verify(
            collection.raw_path,
            collection.processed_path,
            expected_selection=collection.selection,
        )


@pytest.mark.parametrize("limit", [0, 261, True])
def test_selection_rejects_out_of_scope_limit(limit):
    with pytest.raises(ValueError):
        select_candidates(candidates(), limit=limit)
