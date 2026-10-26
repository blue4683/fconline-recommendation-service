"""가격만 갱신하는 실행과 파생 배치를 합성 응답으로 확인한다. 네트워크·DB 없음."""

import json
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from test_realdata_catalog import (
    CLUB,
    EMPTY_LIST_HTML,
    Server,
    rows_html,
    template,
)

from FRS.agent.real_material import ObservedMaterial, RealMaterialError
from FRS.utils.crawling.realdata_catalog import (
    PRICES_COLLECTED,
    CatalogStopped,
    RealDataRun,
)
from FRS.utils.crawling.realdata_catalog import (
    plan as collection_plan,
)
from FRS.utils.crawling.refresh_prices import (
    RefreshRefused,
    main,
    plan,
    refresh,
    verify_refreshed,
)
from FRS.utils.crawling.verify_pilot import VerificationError
from FRS.utils.crawling.verify_realdata import verify

BASE, PRICES = "real-data-synthetic-base", "real-data-synthetic-prices"
REFRESHED = "real-data-synthetic-refreshed"
DAY1 = datetime(2026, 10, 21, tzinfo=timezone.utc)
DAY2 = DAY1 + timedelta(days=1)
# 기준 실행의 카드 3장(급여 21). 가격 실행은 1번을 그대로, 2번을 급여 26 칸에,
# 기준에 없는 99번을 새로 보여 주고 3번은 보여 주지 않는다.
KEPT, MOVED, GONE, NEW = 123000001, 123000002, 123000003, 123000099


class ListServer:
    """급여 칸마다 정한 카드만 돌려주는 목록 전용 합성 서버."""

    def __init__(self, cells):
        self.cells = cells
        self.calls = []

    def __call__(self, url, form, resource):
        self.calls.append(resource)
        assert resource == "player_list"
        low, high = int(form["n4SalaryMin"]), int(form["n4SalaryMax"])
        spids = self.cells.get((low, high))
        if not spids:
            return EMPTY_LIST_HTML
        return rows_html(spids, low, int(form["n1Strong"])).encode()


def collect(root, run_id, server, start, **options):
    ticks = count(0, 2)
    return RealDataRun(
        root / "data/raw" / run_id,
        root / "data/processed" / run_id,
        server,
        run_id=run_id,
        clubs=[CLUB],
        template=template(),
        max_attempts=100,
        allow_empty_cells=True,
        clock=lambda: start + timedelta(seconds=next(ticks)),
        **options,
    ).execute()


def price_server():
    return ListServer({(21, 25): [KEPT, NEW], (26, 30): [MOVED]})


@pytest.fixture
def root(tmp_path):
    assert (
        collect(tmp_path, BASE, Server({(21, 25, ""): 3}), DAY1)["status"]
        == "completed"
    )
    report = collect(tmp_path, PRICES, price_server(), DAY2, prices_only=True)
    assert report["status"] == PRICES_COLLECTED, report
    return tmp_path


def rows(root, name, run_id=REFRESHED):
    path = root / "data/processed" / run_id / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_prices_only_run_requests_lists_only_and_keeps_prices_and_list_salary(
    tmp_path,
):
    server = price_server()
    report = collect(tmp_path, PRICES, server, DAY2, prices_only=True)
    # 목록 5칸 + 급여 대조 1, 능력치 요청 없음
    assert server.calls == ["player_list"] * 6 and report["attempts"] == 6
    assert report["mode"] == "prices_only" and report["profile_cards"] == 0
    assert set(report["processed_hashes"]) == {
        "card_price.jsonl",
        "team_color.jsonl",
        "catalog_salary.jsonl",
    }
    salaries = rows(tmp_path, "catalog_salary", PRICES)
    assert {(s["spid"], s["salary"], s["reinforce_level"]) for s in salaries} == {
        (KEPT, 21, 5),
        (NEW, 21, 5),
        (MOVED, 26, 5),
    }
    manifest = json.loads(
        (tmp_path / "data/raw" / PRICES / "manifest.json").read_text()
    )
    assert manifest["status"] == PRICES_COLLECTED and manifest["mode"] == "prices_only"
    # 가격 실행만으로는 공급기가 읽지 않는다.
    with pytest.raises(RealMaterialError):
        ObservedMaterial(tmp_path / "data/processed", PRICES)


def test_plan_reports_the_prices_only_mode_and_rejects_mixed_modes():
    planned = collection_plan([CLUB], prices_only=True)
    assert planned["mode"] == "prices_only" and planned["ability_requests"] == 0
    with pytest.raises(ValueError):
        collection_plan([CLUB], catalog_only=True, prices_only=True)


def test_verifier_replays_a_prices_only_run_and_rejects_changed_list_salary(root):
    raw, processed = root / "data/raw" / PRICES, root / "data/processed" / PRICES
    result = verify(raw, processed, allow_empty_cells=True)
    assert result["verified"] and result["ability_requests"] == 0
    assert result["replayed_processed_files"] == 3
    path = processed / "catalog_salary.jsonl"
    path.write_text(path.read_text().replace('"salary":26', '"salary":27'))
    with pytest.raises(VerificationError):
        verify(raw, processed, allow_empty_cells=True)


def test_refresh_keeps_shared_cards_and_reports_every_exclusion(root):
    processed = root / "data/processed"
    report = refresh(processed, BASE, PRICES, REFRESHED)
    assert report["derivation"] == "price_refresh" and report["catalog_cards"] == 1
    assert report["excluded"] == dict(
        missing_profile=[NEW],
        missing_price=[GONE],
        salary_mismatch=[dict(spid=MOVED, base_salary=21, listed_salary=26)],
    )
    assert [s["role"] for s in report["sources"]] == ["base", "prices"]
    cards, prices = rows(root, "player_card"), rows(root, "card_price")
    colors, stats = rows(root, "team_color"), rows(root, "reinforce_stats")
    assert {c["spid"] for c in cards} == {KEPT}
    assert {p["spid"] for p in prices} == {KEPT} and len(prices) == 7
    assert len(stats) == 6 and len(rows(root, "card_salary")) == 7
    # 원래 관측 시각·기한을 유지한다: 카드는 기준 실행, 가격·자격은 새 목록 관측이다.
    base_cards = {c["spid"]: c for c in rows(root, "player_card", BASE)}
    new_prices = {
        (p["spid"], p["reinforce_level"]): p for p in rows(root, "card_price", PRICES)
    }
    assert cards[0] == {**base_cards[KEPT], "batch_id": REFRESHED}
    assert all(
        p == {**new_prices[(p["spid"], p["reinforce_level"])], "batch_id": REFRESHED}
        for p in prices
    )
    assert colors == [
        {**c, "batch_id": REFRESHED}
        for c in rows(root, "team_color", PRICES)
        if c["spid"] == KEPT
    ]
    # 응답 기한은 쓰인 관측 중 가장 이른 것이며, 여기서는 하루 뒤 받은 가격의 24시간이다.
    material = ObservedMaterial(processed, REFRESHED)
    loaded = material.load(material.batches)
    earliest = min(
        row.valid_until
        for key in ("cards", "salaries", "prices", "reinforcements", "team_colors")
        for row in loaded[key]
        if row.valid_until is not None
    )
    assert min(p.valid_until for p in loaded["prices"] if p.valid_until) == earliest
    assert earliest < min(c.valid_until for c in loaded["cards"])


def test_verify_rebuilds_and_rejects_changed_outputs_or_sources(root):
    processed = root / "data/processed"
    refresh(processed, BASE, PRICES, REFRESHED)
    assert verify_refreshed(processed, REFRESHED)["verified"]
    target = processed / REFRESHED / "card_price.jsonl"
    original = target.read_text()
    target.write_text(original.replace('"price":', '"price": ', 1))
    with pytest.raises(RefreshRefused):
        verify_refreshed(processed, REFRESHED)
    target.write_text(original)
    source = processed / PRICES / "report.json"
    source.write_text(source.read_text() + " ")
    with pytest.raises(RefreshRefused):
        verify_refreshed(processed, REFRESHED)


def test_unsafe_refreshes_are_refused(root):
    processed = root / "data/processed"
    for base, prices, target in (
        (BASE, BASE, REFRESHED),
        (BASE, PRICES, BASE),
        (PRICES, PRICES, REFRESHED),
        (BASE, PRICES, "not-a-run-id"),
    ):
        with pytest.raises((RefreshRefused, CatalogStopped)):
            plan(processed, base, prices, target)
    # 가격 실행 자리에 전체 수집 실행, 기준 자리에 가격 실행은 쓰지 않는다.
    other = "real-data-synthetic-other"
    collect(root, other, Server(), DAY2)
    with pytest.raises(RefreshRefused):
        plan(processed, BASE, other, REFRESHED)
    with pytest.raises(RefreshRefused):
        plan(processed, PRICES, BASE, REFRESHED)
    # 가격 갱신 파생 배치를 다시 기준으로 쓰지 않고, 같은 ID에 덮어쓰지 않는다.
    refresh(processed, BASE, PRICES, REFRESHED)
    with pytest.raises(RefreshRefused):
        plan(processed, REFRESHED, PRICES, "real-data-synthetic-again")
    with pytest.raises(RefreshRefused):
        refresh(processed, BASE, PRICES, REFRESHED)
    # 갱신 뒤 가격 실행 파일이 바뀌면 읽지 않는다.
    path = processed / PRICES / "card_price.jsonl"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(RefreshRefused):
        plan(processed, BASE, PRICES, "real-data-synthetic-again")


def test_cli_plans_by_default_and_writes_only_with_run(root, capsys):
    args = ["--root", str(root), "--base", BASE, "--prices", PRICES]
    assert main([*args, "--refreshed-id", REFRESHED]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["excluded"] == dict(
        missing_profile=1, missing_price=1, salary_mismatch=1
    )
    assert not (root / "data/processed" / REFRESHED).exists()
    assert main([*args, "--refreshed-id", REFRESHED, "--run"]) == 0
    capsys.readouterr()
    assert main(["--root", str(root), "--refreshed-id", REFRESHED, "--verify"]) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
    assert main([*args, "--refreshed-id", REFRESHED, "--run"]) == 1
