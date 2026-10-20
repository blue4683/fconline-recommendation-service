import importlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import Mock

import pytest
from test_ability_parser import ability_html, context

from FRS.utils.crawling.ability_parser import parse_ability
from FRS.utils.crawling.pilot import (
    MAX_ATTEMPTS,
    POOL_SIZE,
    REGRESSION_IDS,
    Pilot,
    PilotStopped,
    plan,
    screening_ids,
    stratified_ids,
)
from FRS.utils.crawling.verify_pilot import VerificationError, displayed_stats, verify

http = importlib.import_module("FRS.utils.crawling.get_response")


def page(status=200, body=b"synthetic"):
    return Mock(status_code=status, content=body)


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    monkeypatch.setattr(http.time, "sleep", Mock())
    return Pilot(tmp_path / "raw", tmp_path / "processed", Mock())


def test_plan_makes_no_requests_and_fits_approval():
    policy = plan()
    assert policy["normal_attempts"] == 933
    assert policy["normal_attempts"] < MAX_ATTEMPTS == 1000
    assert policy["reinforcement_levels"] == tuple(range(5, 12))
    assert policy["delete_after"] is None


def test_budget_is_reserved_on_disk_before_send_and_never_exceeded(pilot):
    pilot.active = ("metadata", None, None)
    pilot.state["attempts"] = MAX_ATTEMPTS - 1

    def send(*args, **kwargs):
        state = json.loads((pilot.raw_path / "manifest.json").read_text())
        assert state["attempts"] == MAX_ATTEMPTS
        return page()

    pilot.session.request.side_effect = send
    pilot.transport("GET", "https://example.invalid")
    with pytest.raises(PilotStopped, match="상한"):
        pilot.transport("GET", "https://example.invalid")
    assert pilot.session.request.call_count == 1


def test_same_pilot_path_cannot_be_executed_twice(pilot):
    with pytest.raises(FileExistsError):
        Pilot(pilot.raw_path, pilot.processed_path, Mock())


@pytest.mark.parametrize("status", [403, 429])
def test_block_is_terminal_and_logged_once(pilot, status):
    pilot.session.request.return_value = page(status)
    report = pilot.execute()
    assert report["status"] == "stopped"
    assert report["stop_reason"]["kind"] == "blocked"
    assert report["attempts"] == 1
    assert pilot.logs[0].outcome == "stopped"
    assert pilot.session.request.call_count == 1


def metadata():
    return [
        {"id": season * 1_000_000 + person, "name": f"가상선수_{person}"}
        for season in range(100, 110)
        for person in range(1, 31)
    ]


def test_season_selection_is_deterministic_unique_and_excludes_regressions():
    sample = screening_ids(metadata())
    assert len(sample) == len(set(sample)) == POOL_SIZE
    assert sample == screening_ids(list(reversed(metadata())))
    assert not set(sample) & set(REGRESSION_IDS)
    assert set(Counter(spid // 1_000_000 for spid in sample).values()) == {20}
    with pytest.raises(PilotStopped):
        screening_ids([{"id": True}])


def test_stratification_requires_position_coverage():
    cards = [parse_ability(ability_html(), context()).card]
    with pytest.raises(PilotStopped, match="포지션군"):
        stratified_ids(cards)


def test_full_offline_pilot_records_seven_levels_and_separate_colors(
    pilot, monkeypatch
):
    ticks = count(0, 2)
    monkeypatch.setattr(
        "FRS.utils.crawling.pilot.now",
        lambda: (
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=next(ticks))
        ),
    )
    positions = ("GK", "CB", "CM", "ST")

    def send(method, url, **options):
        if url.endswith("spid.json"):
            return page(body=json.dumps(metadata()).encode())
        if method == "POST":
            data = options["data"]
            spid, level = data["spid"], data["n1Strong"]
            html = ability_html(level, spid, positions[spid % 4])
            return page(body=html.encode())
        return page(
            body=b'<div class="data_detail_club"><div class="data_table">'
            b'<div class="td club">synthetic-club</div></div></div>'
        )

    pilot.session.request.side_effect = send
    report = pilot.execute()
    assert report["status"] == "completed"
    assert report["attempts"] == 933
    assert report["selected_sample"] == 100
    assert report["http_failure_rate"] == 0
    assert report["collection_counts"] == {
        "player_card": 104,
        "card_salary": 728,
        "reinforce_stats": 728,
        "team_color": 208,
    }
    assert all(
        record.status == "unknown"
        for records in pilot.records.values()
        for record in records
    )
    assert all(
        len(record.stats_delta) == 34 for record in pilot.records["reinforce_stats"]
    )
    assert len({card.spid for card in pilot.records["player_card"]}) == 104
    assert set(report["position_role_counts"]) == {"GK", "DEF", "MID", "FW"}

    result = verify(pilot.raw_path, pilot.processed_path)
    assert result["verified"]
    assert result["source_stat_values_compared"] == 828 * 34
    entry = pilot.state["responses"][0]
    (pilot.raw_path / entry["path"]).write_bytes(b"tampered")
    with pytest.raises(VerificationError, match="해시"):
        verify(pilot.raw_path, pilot.processed_path)


def test_independent_display_audit_ignores_bonus_text():
    stats = displayed_stats(ability_html().encode())
    assert len(stats) == 34 and set(stats.values()) == {105}
    with pytest.raises(VerificationError):
        displayed_stats(b"<html></html>")


def test_display_audit_handles_split_groups_and_ignores_summary_rows():
    html = (
        ability_html()
        .replace("</li>", '</li></ul><ul class="data_wrap data_wrap_playerinfo">', 1)
        .replace('class="value"', 'class="value over120"')
    )
    summary = '<li class="ab"><span class="txt">요약항목</span><span class="value">1</span></li>'
    stats = displayed_stats((summary + html).encode())
    assert len(stats) == 34 and set(stats.values()) == {105}
