import importlib
import json
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import Mock

import pytest
from test_ability_parser import ability_html

from FRS.utils.crawling.collect_cards import (
    COLLECTION_ID,
    CardCollection,
    collection_lock,
    plan,
    select_ids,
)
from FRS.utils.crawling.pilot import PilotStopped
from FRS.utils.crawling.verify_collection import verify
from FRS.utils.crawling.verify_pilot import VerificationError

http = importlib.import_module("FRS.utils.crawling.get_response")


class SmallCollection(CardCollection):
    sample_size = 8
    max_attempts = 21

    def execution_plan(self):
        return {**plan(), "max_attempts_including_retries": self.max_attempts}


def metadata():
    return [
        {"id": season * 1_000_000 + person, "name": "가상선수"}
        for season in range(100, 104)
        for person in range(1, 6)
    ]


def sender(method, url, **options):
    if url.endswith("spid.json"):
        content = json.dumps(metadata()).encode()
    elif method == "POST":
        data = options["data"]
        assert data["n1Strong"] == 5
        content = ability_html(5, data["spid"]).encode()
    else:
        content = b'<div class="data_detail_club"><div class="data_table"><div class="td club">synthetic-club</div></div></div>'
    return Mock(status_code=200, content=content)


@pytest.fixture
def collection(tmp_path, monkeypatch):
    monkeypatch.setattr(http.time, "sleep", Mock())
    ticks = count(0, 2)

    def clock():
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=next(ticks)
        )

    monkeypatch.setattr("FRS.utils.crawling.pilot.now", clock)
    monkeypatch.setattr("FRS.utils.crawling.collect_cards.now", clock)
    session = Mock()
    session.request.side_effect = sender
    return SmallCollection(
        tmp_path / "raw", tmp_path / "processed", session, excluded=[100000001]
    )


def resume(collection):
    session = Mock()
    session.request.side_effect = sender
    return SmallCollection(
        collection.raw_path,
        collection.processed_path,
        session,
        excluded=collection.excluded,
        resume=True,
    )


def test_approved_scope_and_season_selection():
    policy = plan()
    assert policy["additional_cards"] == 1000
    assert policy["normal_attempts"] == 2001
    assert policy["max_attempts_including_retries"] == 2100
    assert policy["reinforcement_level"] == 5
    selected = select_ids(metadata(), {100000001}, 8)
    assert selected == select_ids(list(reversed(metadata())), {100000001}, 8)
    assert len(set(selected)) == 8
    assert 100000001 not in selected


def test_complete_collection_is_local_idempotent_and_single_grade(collection):
    report = collection.execute()
    assert report["status"] == "completed"
    assert report["attempts"] == 17
    assert report["completed_cards"] == 8
    assert report["http_failure_rate"] == report["parse_failure_rate"] == 0
    assert report["collection_counts"] == {
        "player_card": 8,
        "card_salary": 8,
        "team_color": 16,
    }
    for spid in collection.state["completed_ids"]:
        rows = collection.load_card(spid)
        assert rows["player_card"][0].batch_id == COLLECTION_ID
        assert all(
            row.status == "unknown" for values in rows.values() for row in values
        )
    result = verify(
        collection.raw_path,
        collection.processed_path,
        expected_count=8,
        max_attempts=21,
    )
    assert result["verified"] and result["source_stat_values_compared"] == 8 * 34
    with pytest.raises(PilotStopped, match="재개"):
        resume(collection)
    spid = collection.state["completed_ids"][0]
    path = collection.processed_path / f"{spid}.json"
    path.write_text(path.read_text().replace('"speed": 105', '"speed": 106'))
    with pytest.raises(VerificationError, match="가공 해시"):
        verify(
            collection.raw_path,
            collection.processed_path,
            expected_count=8,
            max_attempts=21,
        )


def test_process_interruption_reuses_previous_responses_and_budget(collection):
    calls = 0

    def interrupt(method, url, **options):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise KeyboardInterrupt
        return sender(method, url, **options)

    collection.session.request.side_effect = interrupt
    report = collection.execute()
    assert report["status"] == "interrupted"
    assert report["attempts"] == 5
    assert report["completed_cards"] == 1
    continued = resume(collection)
    report = continued.execute()
    assert report["status"] == "completed"
    assert report["attempts"] == 18
    assert continued.session.request.call_count == 13
    assert [log.attempt for log in continued.logs] == list(range(1, 19))


def test_unfinished_reserved_attempt_is_preserved_as_unknown_outcome(collection):
    collection.state.update(
        attempts=1,
        status="running",
        pending={
            "resource": "metadata",
            "spid": None,
            "grade": None,
            "observed_at": "2026-01-01T00:00:00Z",
        },
    )
    collection.flush()
    continued = resume(collection)
    assert continued.logs[0].failure_kind == "interrupted"
    assert continued.logs[0].duration_ms is continued.logs[0].http_status is None
    report = continued.execute()
    assert report["status"] == "completed" and report["attempts"] == 18


@pytest.mark.parametrize("status", [403, 429])
def test_block_cannot_be_resumed(collection, status):
    collection.session.request.return_value = Mock(
        status_code=status, content=b"blocked"
    )
    collection.session.request.side_effect = None
    report = collection.execute()
    assert report["status"] == "stopped" and report["attempts"] == 1
    assert report["stop_reason"]["kind"] == "blocked"
    with pytest.raises(PilotStopped, match="재개"):
        resume(collection)


def test_retry_cannot_exceed_total_budget(collection):
    collection.state["attempts"] = 20
    collection.session.request.side_effect = None
    collection.session.request.return_value = Mock(
        status_code=503, content=b"temporary"
    )
    report = collection.execute()
    assert report["status"] == "stopped" and report["attempts"] == 21
    assert collection.session.request.call_count == 1


def test_changed_raw_response_is_rejected_on_resume(collection):
    collection.state["status"] = "interrupted"
    collection.fetch("metadata")
    collection.flush()
    entry = collection.state["responses"][0]
    (collection.raw_path / entry["path"]).write_bytes(b"changed")
    with pytest.raises(PilotStopped, match="해시"):
        resume(collection)


def test_collection_lock_prevents_concurrent_resume(tmp_path):
    path = tmp_path / "same-run.lock"
    with collection_lock(path):
        with pytest.raises(PilotStopped, match="동작 중"):
            with collection_lock(path):
                pass
    with collection_lock(path):
        pass
