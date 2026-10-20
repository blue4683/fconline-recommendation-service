"""새 관측의 의미 채택과 수동 갱신의 요청 상한·중단을 합성 응답으로 검증한다."""

import hashlib
import importlib
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import Mock

import pytest
from test_ability_parser import ability_html, context
from test_player_list_parser import synthetic_row

from FRS.utils.crawling.ability_parser import ParseError, parse_ability
from FRS.utils.crawling.pilot import PilotStopped
from FRS.utils.crawling.player_list_parser import PlayerListContext
from FRS.utils.crawling.refresh_observations import (
    CATALOG_URL,
    adopt_ability,
    adopt_catalog,
)
from FRS.utils.crawling.refresh_run import BANDS, ManualRefresh

http = importlib.import_module("FRS.utils.crawling.get_response")
OBSERVED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def forms():
    return [
        dict(
            teamcolorid=color,
            n4SalaryMin=str(lo),
            n4SalaryMax=str(hi),
            strTeamColorCategory="affiliation",
            n4LeagueId="0",
            n4TeamId="",
            n4PageNo="1",
            n1Strong="5",
            n1Grow="0",
            n1TeamColor="0",
            strSkill1="sprintspeed",
            strSkill2="acceleration",
            strSkill3="strength",
            strSkill4="stamina",
        )
        for color in ("1001", "1003", "1010")
        for lo, hi in BANDS
    ]


def catalog_evidence(html=None):
    body = (html or synthetic_row()).encode()
    form = forms()[1]
    ctx = PlayerListContext(
        batch_id="synthetic-new-refresh",
        source="nexon_web",
        observed_at=OBSERVED,
        delete_after=None,
        version=hashlib.sha256(body).hexdigest(),
        reinforce_level=5,
        skill_fields=("sprintspeed", "acceleration", "strength", "stamina"),
    )
    req = dict(url=CATALOG_URL, method="POST", status_code=200, form=form)
    return body, ctx, req


def test_reference_price_and_membership_have_separate_periods():
    body, ctx, req = catalog_evidence()
    result = adopt_catalog(body, ctx, req, at=OBSERVED)
    assert len(result.prices) == 7 and len(result.memberships) == 1
    assert all(
        p.price_type == "reference"
        and p.status == "ok"
        and p.valid_until == OBSERVED + timedelta(hours=24)
        for p in result.prices
    )
    member = result.memberships[0]
    assert member.name == "리버풀" and member.type == "club" and member.status == "ok"
    assert member.valid_until == OBSERVED + timedelta(days=7)
    assert member.observed_at == ctx.observed_at and member.version == ctx.version
    expired = adopt_catalog(body, ctx, req, at=OBSERVED + timedelta(hours=24))
    assert all(p.status == "stale" for p in expired.prices)
    assert expired.memberships[0].status == "ok"


def test_zero_prices_are_unknown_and_no_negative_memberships_are_invented():
    body, ctx, req = catalog_evidence(
        synthetic_row().replace("9,999,999,999,999,999,999", "0")
    )
    result = adopt_catalog(body, ctx, req, at=OBSERVED)
    assert all(
        p.price is None and p.status == "unknown" and p.valid_until is None
        for p in result.prices
    )
    assert len(result.memberships) == 1
    assert not any(m.name in {"첼시", "바이에른 뮌헨"} for m in result.memberships)


@pytest.mark.parametrize(
    "change",
    ["hash", "method", "status", "category", "color", "club", "salary", "future"],
)
def test_catalog_contract_rejects_unverified_evidence(change):
    body, ctx, req = catalog_evidence()
    at = OBSERVED
    if change == "hash":
        body += b"changed"
    elif change == "method":
        req["method"] = "GET"
    elif change == "status":
        req["status_code"] = 302
    elif change == "category":
        req["form"]["strTeamColorCategory"] = "nation"
    elif change == "color":
        req["form"]["teamcolorid"] = "0"
    elif change == "club":
        req["form"]["n4TeamId"] = "9"
    elif change == "salary":
        req["form"]["n4SalaryMin"] = "31"
        req["form"]["n4SalaryMax"] = "35"
    else:
        at -= timedelta(seconds=1)
    with pytest.raises(ParseError):
        adopt_catalog(body, ctx, req, at=at)


def test_ability_adoption_returns_new_objects_and_preserves_observation():
    raw = parse_ability(ability_html(), context(source="nexon_web"))
    before = raw.card.model_dump()
    result = adopt_ability(raw, at=OBSERVED)
    assert result.card.status == result.salary.status == "ok"
    assert result.card.valid_until == OBSERVED + timedelta(days=7)
    assert (
        result.card.observed_at == raw.card.observed_at
        and result.card.version == raw.card.version
    )
    assert raw.card.model_dump() == before and raw.card.status == "unknown"
    with pytest.raises(ParseError):
        adopt_ability(raw, at=OBSERVED - timedelta(seconds=1))
    with pytest.raises(ParseError):
        adopt_ability(parse_ability(ability_html(), context()), at=OBSERVED)


def sender(method, url, **options):
    assert method == "POST" and options["allow_redirects"] is False
    form = options["data"]
    if url == CATALOG_URL:
        selected = form["teamcolorid"] == "1001" and form["n4SalaryMin"] == "21"
        spid = (
            123000001
            if selected
            else 123100000 + int(form["teamcolorid"]) * 10 + int(form["n4SalaryMin"])
        )
        html = synthetic_row(spid).replace(
            ">24</span>", f">{form['n4SalaryMin']}</span>"
        )
    else:
        html = ability_html(form["n1Strong"], form["spid"])
    return Mock(status_code=200, content=html.encode())


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(http.time, "sleep", Mock())
    ticks = count(0, 2)

    def clock():
        return OBSERVED + timedelta(seconds=next(ticks))

    monkeypatch.setattr("FRS.utils.crawling.pilot.now", clock)
    monkeypatch.setattr("FRS.utils.crawling.refresh_run.now", clock)
    session = Mock()
    session.request.side_effect = sender
    return ManualRefresh(
        tmp_path / "raw",
        tmp_path / "processed",
        session,
        run_id="stage-1-f-refresh-synthetic",
        targets=[123000001],
        forms=forms(),
        jobs=["catalog", "profiles"],
        max_attempts=22,
    )


def test_complete_run_has_fresh_new_records_and_preserved_hashes(run):
    result = run.execute()
    assert result["status"] == "completed" and result["attempts"] == 22
    assert result["http_failure_attempts"] == result["parse_failures"] == 0
    assert result["collection_counts"] == dict(
        card_price=7, team_color=1, player_card=1, card_salary=7, reinforce_stats=7
    )
    assert result["auto_activation"] is False
    assert all(r.status == "ok" for values in run.records.values() for r in values)
    for name, digest in run.state["processed_hashes"].items():
        assert (
            hashlib.sha256((run.processed_path / name).read_bytes()).hexdigest()
            == digest
        )
    assert all(e["method"] == "POST" and e["form"] for e in run.state["responses"])
    with pytest.raises(PilotStopped):
        run.execute()
    assert run.session.request.call_count == 22
    with pytest.raises(FileExistsError):
        ManualRefresh(
            run.raw_path,
            run.processed_path,
            Mock(),
            run_id=run.run_id,
            targets=run.targets,
            forms=run.forms,
            jobs=run.jobs,
            max_attempts=22,
        )


@pytest.mark.parametrize("status", [302, 403, 404, 429, 500, 503])
def test_http_failure_stops_without_retry(run, status):
    run.session.request.side_effect = None
    run.session.request.return_value = Mock(status_code=status, content=b"error")
    result = run.execute()
    assert result["status"] == "stopped" and result["attempts"] == 1
    assert run.session.request.call_count == 1


def test_missing_target_does_not_expand_queries_or_start_profiles(run):
    def missing(method, url, **options):
        response = sender(method, url, **options)
        response.content = response.content.replace(b"123000001", b"123000002")
        return response

    run.session.request.side_effect = missing
    result = run.execute()
    assert result["status"] == "stopped" and result["attempts"] == 15
    assert result["completed_profile_cards"] == 0
    assert result["catalog_cards"] == 0


def test_budget_and_interruption_never_trigger_retries(run):
    run.max_attempts = 1
    assert run.execute()["attempts"] == 1
    assert run.state["status"] == "stopped"
    assert run.session.request.call_count == 1


def test_process_interruption_is_not_restarted(run):
    run.session.request.side_effect = KeyboardInterrupt
    assert run.execute()["status"] == "interrupted"
    assert run.state["attempts"] == 1
    with pytest.raises(PilotStopped):
        run.execute()


@pytest.mark.parametrize("change", ["path", "budget", "duplicate-job", "page"])
def test_invalid_execution_is_rejected_before_request(tmp_path, change):
    kwargs = dict(
        run_id="stage-1-f-refresh-synthetic",
        targets=[123000001],
        forms=forms(),
        jobs=["catalog", "profiles"],
        max_attempts=22,
    )
    if change == "path":
        kwargs["run_id"] = "../../outside"
    elif change == "budget":
        kwargs["max_attempts"] = 23
    elif change == "duplicate-job":
        kwargs["jobs"] = ["catalog", "catalog"]
    else:
        kwargs["forms"][0]["n4PageNo"] = "2"
    session = Mock()
    with pytest.raises(PilotStopped):
        ManualRefresh(tmp_path / "raw", tmp_path / "processed", session, **kwargs)
    assert not (tmp_path / "raw").exists() and session.request.call_count == 0


def test_new_batch_can_be_reverified_without_network(run):
    from FRS.utils.crawling.verify_refresh import verify

    assert run.execute()["status"] == "completed"
    calls = run.session.request.call_count
    data = verify(run.raw_path, run.processed_path, expected_targets=run.targets)
    assert len(data["card_price"]) == 7 and len(data["reinforce_stats"]) == 7
    assert run.session.request.call_count == calls


@pytest.mark.parametrize("case", ["hash", "derived", "policy", "target", "incomplete"])
def test_new_batch_verifier_rejects_tampering(run, case):
    import json

    from FRS.utils.crawling.verify_pilot import VerificationError
    from FRS.utils.crawling.verify_refresh import verify

    run.execute()
    if case == "hash":
        entry = run.state["responses"][0]
        (run.raw_path / entry["path"]).write_bytes(b"changed")
    elif case == "derived":
        path = run.processed_path / "card_salary.jsonl"
        lines = path.read_text().splitlines()
        value = json.loads(lines[0])
        value["salary"] += 1
        lines[0] = json.dumps(value)
        path.write_text("\n".join(lines) + "\n")
        run.state["processed_hashes"][path.name] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        run.flush()
    elif case == "policy":
        run.state["policy"]["rules"]["price"]["valid_for_seconds"] = 1
        run.flush()
    elif case == "target":
        run.state["selected_ids"] = [123000002]
        run.flush()
    else:
        run.state["status"] = "stopped"
        run.flush()
    with pytest.raises((VerificationError, PilotStopped)):
        verify(run.raw_path, run.processed_path, expected_targets=run.targets)


def test_price_conflict_stops_instead_of_picking_latest(run):
    def conflicting(method, url, **options):
        response = sender(method, url, **options)
        form = options["data"]
        if (
            url == CATALOG_URL
            and form["teamcolorid"] == "1003"
            and form["n4SalaryMin"] == "21"
        ):
            response.content = (
                synthetic_row()
                .replace(">24</span>", ">21</span>")
                .replace("9,999,999,999,999,999,999", "1,000")
                .encode()
            )
        return response

    run.session.request.side_effect = conflicting
    result = run.execute()
    assert result["status"] == "stopped" and result["attempts"] == 7
    assert result["parse_failures"] == 1
    assert result["completed_profile_cards"] == 0


@pytest.mark.parametrize(
    "arguments",
    [
        ["--run"],
        [
            "--run",
            "--jobs",
            "catalog",
            "--run-id",
            "../../outside",
            "--max-attempts",
            "15",
        ],
    ],
)
def test_cli_requires_explicit_scope_before_loading_or_writing(monkeypatch, arguments):
    import sys

    from FRS.utils.crawling import refresh_run

    load = Mock(
        side_effect=AssertionError("실행 범위 오류에서 자료를 읽으면 안 됩니다.")
    )
    monkeypatch.setattr(refresh_run, "load_plan", load)
    monkeypatch.setattr(sys, "argv", ["refresh_run", *arguments])
    with pytest.raises(SystemExit) as error:
        refresh_run.main()
    assert error.value.code == 2 and load.call_count == 0


def test_cli_defaults_to_plan_and_never_creates_executor(monkeypatch, capsys):
    import sys

    from FRS.utils.crawling import refresh_run

    monkeypatch.setattr(sys, "argv", ["refresh_run"])
    monkeypatch.setattr(
        refresh_run,
        "load_plan",
        Mock(return_value=dict(mode="plan_only", network_requests_sent=0)),
    )
    executor = Mock(
        side_effect=AssertionError("기본 명령은 실행기를 만들면 안 됩니다.")
    )
    monkeypatch.setattr(refresh_run, "ManualRefresh", executor)
    assert refresh_run.main() == 0 and executor.call_count == 0
    assert "plan_only" in capsys.readouterr().out
