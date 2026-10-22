"""실데이터 실행 검증기를 합성 실행으로 확인한다. 네트워크 없음."""

import hashlib
import json
import shutil
from datetime import timedelta
from itertools import count

import pytest
from test_ability_parser import ability_html
from test_realdata_catalog import CLUB, OBSERVED, OTHER, Server, TwoClubServer, template

from FRS.utils.crawling.realdata_catalog import RealDataRun
from FRS.utils.crawling.verify_pilot import VerificationError
from FRS.utils.crawling.verify_realdata import (
    check_independently,
    check_records,
    main,
    verify,
)

RUN_ID = "real-data-synthetic"


class PayServer(Server):
    """여러 급여 칸에 카드를 두고, 상세 급여를 목록 급여(칸 하한)와 맞춘다."""

    def __call__(self, url, form, resource):
        if resource == "ability":
            pay = next(
                low for (low, _, _), ids in self.ids.items() if form["spid"] in ids
            )
            html = ability_html(form["n1Strong"], form["spid"])
            return html.replace("<span>20</span>", f"<span>{pay}</span>").encode()
        return super().__call__(url, form, resource)


def collect(root, server=None, *, step=2, **options):
    ticks = count(0, step)
    run = RealDataRun(
        root / "data/raw" / RUN_ID,
        root / "data/processed" / RUN_ID,
        server or PayServer({(4, 20, ""): 200, (4, 12, ""): 120, (13, 20, ""): 80}),
        run_id=RUN_ID,
        template=template(),
        clock=lambda: OBSERVED + timedelta(seconds=next(ticks)),
        **{"clubs": [CLUB], "max_attempts": 300, "allow_empty_cells": True, **options},
    )
    report = run.execute()
    return root / "data/raw" / RUN_ID, root / "data/processed" / RUN_ID, report


@pytest.fixture(scope="module")
def base_run(tmp_path_factory):
    """기본 합성 실행(카드 201장)은 모듈에서 한 번만 만들고 테스트마다 복사해 쓴다."""
    root = tmp_path_factory.mktemp("base")
    _, _, report = collect(root)
    return root, report


@pytest.fixture
def standard(base_run, tmp_path):
    def copy(root=tmp_path):
        source, report = base_run
        shutil.copytree(source / "data", root / "data")
        return root / "data/raw" / RUN_ID, root / "data/processed" / RUN_ID, report

    return copy


def rewrite(processed, name, change):
    """가공 파일을 바꾸고 보고서 해시도 맞춰 해시 대조만으로는 못 찾게 한다."""
    path = processed / f"{name}.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    change(lines)
    path.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
    )
    report_path = processed / "report.json"
    report = json.loads(report_path.read_text())
    report["processed_hashes"][path.name] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    report_path.write_text(json.dumps(report, ensure_ascii=False))


def test_a_completed_run_verifies_by_replay_and_independent_reads(standard):
    raw, processed, report = standard()
    assert report["status"] == "completed" and report["split_cells"] == 1
    result = verify(raw, processed, allow_empty_cells=True)
    assert result["verified"] and result["network_requests"] == 0
    assert result["catalog_cards"] == result["profile_cards"] == 201
    assert result["leaf_cells"] == 6  # 4~20을 둘로 나눈 칸 2개 + 나머지 4칸


def test_cli_reads_the_run_under_root_and_reports_failure(standard, tmp_path, capsys):
    standard()
    assert (
        main(["--run-id", RUN_ID, "--root", str(tmp_path), "--allow-empty-cells"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["verified"] is True
    # 수집 때 빈 칸을 허용했는데 재생에서 허용하지 않으면 재생이 멈춘다.
    assert main(["--run-id", RUN_ID, "--root", str(tmp_path)]) == 1
    assert "재생이 멈췄습니다" in json.loads(capsys.readouterr().out)["reason"]


def test_a_stopped_run_is_not_verified(tmp_path):
    raw, processed, report = collect(tmp_path, max_attempts=3)
    assert report["status"] == "stopped"
    with pytest.raises(VerificationError, match="완료한 실행만"):
        verify(raw, processed, allow_empty_cells=True)


def test_a_changed_raw_body_fails_the_hash(standard):
    raw, processed, _ = standard()
    body = sorted(raw.glob("*-ability-*.body"))[0]
    body.write_bytes(body.read_bytes() + b" ")
    with pytest.raises(VerificationError, match="원본 해시"):
        verify(raw, processed, allow_empty_cells=True)


def test_an_extra_raw_file_is_rejected(standard):
    raw, processed, _ = standard()
    (raw / "9999-ability-1-5.body").write_bytes(b"x")
    with pytest.raises(VerificationError, match="기록에 없는 원본"):
        verify(raw, processed, allow_empty_cells=True)


def test_requests_closer_than_one_second_are_rejected(tmp_path):
    raw, processed, _ = collect(
        tmp_path, Server(), step=0.5
    )  # 간격만 보므로 카드 1장 실행
    with pytest.raises(VerificationError, match="간격"):
        check_records(raw, processed)


def test_a_rewritten_processed_file_fails_the_replay(standard):
    raw, processed, _ = standard()

    def bump(lines):
        lines[0]["position_ovr"] = {
            k: v + 1 for k, v in lines[0]["position_ovr"].items()
        }

    rewrite(processed, "reinforce_stats", bump)
    with pytest.raises(VerificationError, match="재생한 보고서의 processed_hashes"):
        verify(raw, processed, allow_empty_cells=True)


def test_independent_reads_catch_a_wrong_derivation_or_stat(standard, tmp_path):
    raw, processed, _ = standard()
    state, report, bodies = check_records(raw, processed)
    assert check_independently(state, report, bodies, processed, None) == 6

    def bump(lines):
        lines[0]["position_ovr"] = {
            k: v + 1 for k, v in lines[0]["position_ovr"].items()
        }

    rewrite(processed, "reinforce_stats", bump)
    with pytest.raises(VerificationError, match="강화 표와 다릅니다"):
        check_independently(state, report, bodies, processed, None)

    raw2, processed2, _ = standard(tmp_path / "second")

    def stat(lines):
        name = next(iter(lines[0]["stats"]))
        lines[0]["stats"][name] += 1

    rewrite(processed2, "player_card", stat)
    state, report, bodies = check_records(raw2, processed2)
    with pytest.raises(VerificationError, match="직접 표시 숫자"):
        check_independently(state, report, bodies, processed2, None)


def test_an_unsplit_full_cell_is_caught(standard):
    raw, processed, _ = standard()
    state, report, bodies = check_records(raw, processed)
    state["split_log"] = []  # 200장 칸을 나눈 기록을 지우면 잘림을 숨긴 것이 된다.
    with pytest.raises(VerificationError, match="200장 칸"):
        check_independently(state, report, bodies, processed, None)


def test_two_clubs_with_a_price_conflict_verify(tmp_path):
    raw, processed, report = collect(
        tmp_path, TwoClubServer(cheap_on_second=True), clubs=[CLUB, OTHER]
    )
    assert report["status"] == "completed" and report["price_conflicts"] == 7
    result = verify(raw, processed, allow_empty_cells=True)
    assert result["catalog_cards"] == 3

    # 충돌 키를 다시 ok 가격으로 바꾸면(해시는 맞춰도) 재생·독립 대조에서 거부한다.
    def revive(lines):
        for line in lines:
            if line["status"] == "unknown":
                line.update(
                    status="ok", price=1000, valid_until=lines[0]["valid_until"]
                )

    state, report, bodies = check_records(raw, processed)
    rewrite(processed, "card_price", revive)
    with pytest.raises(VerificationError, match="unknown"):
        check_independently(state, report, bodies, processed, None)
