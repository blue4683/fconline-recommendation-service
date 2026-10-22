"""완료 실행 합치기(파생 배치)를 합성 실행으로 확인한다. 네트워크·DB 없음."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from test_realdata_catalog import (
    CLUB,
    CLUB_CARD,
    NATION,
    NATION_CARD,
    NationServer,
    Server,
    template,
)

from FRS.agent.real_material import ObservedMaterial
from FRS.utils.crawling.merge_realdata import (
    MergeRefused,
    main,
    merge,
    plan,
    verify_merged,
)
from FRS.utils.crawling.realdata_catalog import (
    MIXED_ELIGIBILITY_BASIS,
    CatalogStopped,
    RealDataRun,
)

FIRST, SECOND = "real-data-synthetic-a", "real-data-synthetic-b"
MERGED = "real-data-synthetic-merged"


def run(root, run_id, server, start, **options):
    ticks = count(0, 2)
    report = RealDataRun(
        root / "data/raw" / run_id,
        root / "data/processed" / run_id,
        server,
        run_id=run_id,
        template=template(n4NationId="0"),
        max_attempts=100,
        clock=lambda: start + timedelta(seconds=next(ticks)),
        **options,
    ).execute()
    assert report["status"] == "completed", report
    return report


@pytest.fixture
def root(tmp_path):
    # 첫 실행: 클럽 카드 하나. 둘째 실행(하루 뒤): 같은 클럽 카드와 국가 카드.
    run(
        tmp_path,
        FIRST,
        Server(),
        datetime(2026, 10, 21, tzinfo=timezone.utc),
        clubs=[CLUB],
    )
    run(
        tmp_path,
        SECOND,
        NationServer(),
        datetime(2026, 10, 22, tzinfo=timezone.utc),
        clubs=[CLUB, NATION],
    )
    return tmp_path


def rows(root, name, merged=MERGED):
    path = root / "data/processed" / merged / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_first_listed_run_owns_shared_cards_and_memberships_are_combined(root):
    processed = root / "data/processed"
    report = merge(processed, [FIRST, SECOND], MERGED)
    assert report["status"] == "completed" and report["source_mode"] == "derived"
    assert report["cards_by_source"] == {FIRST: 1, SECOND: 1}
    assert report["overlapping_cards"] == 1 and report["catalog_cards"] == 2
    assert report["eligibility_basis"] == MIXED_ELIGIBILITY_BASIS
    assert report["auto_activation"] is False and report["network_requests"] == 0
    first = {r["spid"]: r for r in rows(root, "player_card", FIRST)}
    merged = {r["spid"]: r for r in rows(root, "player_card")}
    # 공유 카드는 앞 실행의 관측 시각·버전을 그대로 쓰고 배치 ID만 바뀐다.
    shared = merged[CLUB_CARD]
    assert shared["observed_at"] == first[CLUB_CARD]["observed_at"]
    assert shared["version"] == first[CLUB_CARD]["version"]
    assert shared["batch_id"] == MERGED
    for name in ("card_salary", "card_price", "reinforce_stats"):
        source = {(r["spid"], r["reinforce_level"]): r for r in rows(root, name, FIRST)}
        for row in rows(root, name):
            if row["spid"] == CLUB_CARD:
                expected = {**source[(row["spid"], row["reinforce_level"])]}
                expected["batch_id"] = MERGED
                assert row == expected
    colors = {(r["spid"], r["type"], r["name"]) for r in rows(root, "team_color")}
    assert colors == {
        (CLUB_CARD, "club", "가상클럽"),
        (NATION_CARD, "nation", "가상국가"),
    }
    # 서비스 공급기가 새 배치를 그대로 읽는다.
    material = ObservedMaterial(processed, MERGED)
    data = material.load(material.batches)
    assert len(data["cards"]) == 2 and len(data["team_colors"]) == 2


def test_reversing_the_order_changes_the_owner_of_the_shared_card(root):
    processed = root / "data/processed"
    merge(processed, [SECOND, FIRST], MERGED)
    second = {r["spid"]: r for r in rows(root, "player_card", SECOND)}
    merged = {r["spid"]: r for r in rows(root, "player_card")}
    assert merged[CLUB_CARD]["observed_at"] == second[CLUB_CARD]["observed_at"]


def test_verify_remerges_and_rejects_changed_outputs_or_sources(root):
    processed = root / "data/processed"
    merge(processed, [FIRST, SECOND], MERGED)
    assert verify_merged(processed, MERGED)["verified"]

    # 가공 파일을 바꾸고 보고서 해시까지 맞춰도 다시 합친 결과와 다르면 거부한다.
    target = processed / MERGED
    path = target / "card_price.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    lines[0]["valid_until"] = None
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    report = json.loads((target / "report.json").read_text())
    report["processed_hashes"]["card_price.jsonl"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    (target / "report.json").write_text(json.dumps(report))
    with pytest.raises(MergeRefused, match="다시 합친 card_price"):
        verify_merged(processed, MERGED)


def test_a_changed_source_after_merging_is_rejected(root):
    processed = root / "data/processed"
    merge(processed, [FIRST, SECOND], MERGED)
    source = processed / FIRST / "report.json"
    source.write_text(source.read_text() + " ")
    with pytest.raises(MergeRefused, match="원본 실행이 합칠 때와 다릅니다"):
        verify_merged(processed, MERGED)


def test_unsafe_merges_are_refused(root):
    processed = root / "data/processed"
    with pytest.raises(MergeRefused, match="둘 이상"):
        plan(processed, [FIRST], MERGED)
    with pytest.raises(MergeRefused, match="둘 이상"):
        plan(processed, [FIRST, FIRST], MERGED)
    with pytest.raises(MergeRefused, match="원본 실행 ID와"):
        plan(processed, [FIRST, SECOND], FIRST)
    with pytest.raises(CatalogStopped):
        plan(processed, [FIRST, SECOND], "merged")
    with pytest.raises(MergeRefused, match="real-data-missing"):
        plan(processed, [FIRST, "real-data-missing"], MERGED)
    merge(processed, [FIRST, SECOND], MERGED)
    with pytest.raises(MergeRefused, match="이미 있습니다"):
        merge(processed, [FIRST, SECOND], MERGED)
    with pytest.raises(MergeRefused, match="파생 배치"):
        plan(processed, [MERGED, FIRST], "real-data-synthetic-merged-2")


def test_cli_plans_by_default_and_writes_only_with_run(root, capsys):
    argv = ["--source", FIRST, "--source", SECOND, "--merged-id", MERGED]
    argv += ["--root", str(root)]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["player_card"] == 2
    assert not (root / "data/processed" / MERGED).exists()
    assert main([*argv, "--run"]) == 0
    capsys.readouterr()
    assert main(["--merged-id", MERGED, "--root", str(root), "--verify"]) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
    assert main([*argv, "--run"]) == 1
    assert "이미 있습니다" in json.loads(capsys.readouterr().out)["error"]
