"""완료한 실데이터 실행(`real-data-*`)의 원본·요청·분할·재파싱을 네트워크 없이 대조한다.

설계: docs/real-data-branch-design.md §8. 세 겹으로 본다.

1. manifest·원본: 시도 번호 연속, 상한·재시도 0, 같은 요청 반복 없음, 1초 간격,
   원본 해시와 보고서의 가공 해시.
2. 수집기 재생: 저장한 응답을 같은 순서로 수집기에 다시 넣어 임시 경로에서 실행하고,
   요청(주소·자원·폼)·관측 시각·분할 기록·가공 파일 바이트가 같은지 본다.
3. 독립 대조: 주 파서를 거치지 않고 원본에서 카드 ID·직접 표시 능력치를 읽어
   분할 칸의 급여 범위, 칸별 200장 미만, 카드 수, 능력치, 6~11강 파생 OVR을 대조한다.

검증은 읽기 전용이다. 실행 기록·가공 파일을 고치거나 다시 만들지 않는다.
"""

import argparse
import hashlib
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from FRS.core.reinforcement import reinforcement_delta
from FRS.schema.models import (
    CardPrice,
    CardSalary,
    PlayerCard,
    ReinforceStats,
    TeamColor,
)
from FRS.utils.crawling.ability_parser import ParseContext, keyword_map
from FRS.utils.crawling.realdata_catalog import (
    ABILITY_URL,
    BANDS,
    CATALOG_URL,
    ELIGIBILITY_BASIS,
    LIST_CAP,
    MIXED_ELIGIBILITY_BASIS,
    OVR_MAX,
    OVR_MIN,
    PRICE_FILES,
    PRICES_COLLECTED,
    ClubSpec,
    NationSpec,
    RealDataRun,
    ability_form,
    validate_run_id,
)
from FRS.utils.crawling.request_timing import SECOND_NS, SendStamp, TimedBody
from FRS.utils.crawling.verify_pilot import (
    VerificationError,
    displayed_stats,
    read_jsonl,
    require,
)

PROCESSED_FILES = (
    "player_card",
    "card_salary",
    "card_price",
    "reinforce_stats",
    "team_color",
)
ROW_ID = re.compile(rb'id="area_playerunit_([0-9]{7,9})"')
# 수집기의 국기 정규식과 독립으로 읽는다. 카드 머리의 국기만 본다.
# 수집기의 결과 없음 정규식과 따로, 표시 문구와 빈 행 표시를 각각 센다.
NO_RESULT = "검색 결과가 없습니다.".encode()
EMPTY_ROW = b'class="tr empty"'
FLAG = re.compile(rb'class="nation">\s*<img[^>]*/countries/smallflags/([0-9]+)\.png')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _time(value):
    return datetime.fromisoformat(value)


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timing_version(state):
    version = state.get("timing_version", 1)
    require(type(version) is int and version in (1, 2), "지원하지 않는 요청 시각 형식")
    return version


def check_intervals(state, responses):
    version = timing_version(state)
    interval = state["min_request_interval_seconds"]
    require(type(interval) is int and interval >= 1, "요청 간격 설정이 잘못됐습니다")
    if version == 1:
        require(
            all("sent_at" not in e and "send_started_ns" not in e for e in responses),
            "기존 형식에 새 전송 시각이 섞여 있습니다",
        )
        times = [_time(e["observed_at"]) for e in responses]
        # 이미 종료한 실행의 기록·원래 허용 오차는 바꾸지 않는다.
        valid = all(
            (b - a).total_seconds() >= interval - 0.001
            for a, b in zip(times, times[1:])
        )
    else:
        for entry in responses:
            require(
                isinstance(entry.get("sent_at"), str)
                and entry["sent_at"] == entry.get("observed_at")
                and type(entry.get("send_started_ns")) is int
                and entry["send_started_ns"] >= 0,
                "전송 시작 시각이 없거나 잘못됐습니다",
            )
            try:
                sent_at = _time(entry["sent_at"])
            except ValueError:
                raise VerificationError(
                    "전송 시작 시각의 ISO 형식이 잘못됐습니다"
                ) from None
            require(
                sent_at.utcoffset() is not None, "전송 시작 시각에 시간대가 없습니다"
            )
        starts = [e["send_started_ns"] for e in responses]
        valid = all(b - a >= interval * SECOND_NS for a, b in zip(starts, starts[1:]))
    require(valid, "요청 간격이 1초보다 짧습니다")


def prices_only(state):
    return state.get("mode") == "prices_only"


def expected_files(state):
    return PRICE_FILES if prices_only(state) else PROCESSED_FILES


def check_records(raw_path, processed_path):
    """1단계. manifest·원본·보고서의 형식과 해시를 대조하고 원본 본문을 돌려준다."""
    state = json.loads((raw_path / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((processed_path / "report.json").read_text(encoding="utf-8"))
    run_id = state.get("run_id")
    validate_run_id(run_id)
    require(
        raw_path.name == processed_path.name == run_id == report.get("run_id"),
        "실행 ID와 경로가 다릅니다",
    )
    require(
        state.get("mode") == report.get("mode")
        and state["status"]
        == report["status"]
        == (PRICES_COLLECTED if prices_only(state) else "completed"),
        "완료한 실행만 검증합니다",
    )
    require(timing_version(state) == timing_version(report), "요청 시각 형식 불일치")
    require("pending" not in state, "결과를 모르는 요청이 남아 있습니다")
    require(
        state["eligibility_basis"] == report["eligibility_basis"]
        and state["eligibility_basis"]
        == (MIXED_ELIGIBILITY_BASIS if state.get("nations") else ELIGIBILITY_BASIS),
        "자격 근거가 클럽 경력·국적 검색 기록과 다릅니다",
    )
    require(report["auto_activation"] is False, "자동 활성화 표시가 있습니다")
    responses = state["responses"]
    require(
        state["retries"] == 0
        and state["attempts"] == report["attempts"] == len(responses)
        and 0 < len(responses) <= state["max_attempts"] == report["max_attempts"],
        "요청 수·상한·재시도 기록이 다릅니다",
    )
    require(
        [entry["attempt"] for entry in responses] == list(range(1, len(responses) + 1)),
        "시도 번호가 연속하지 않습니다",
    )
    seen = set()
    bodies = []
    for entry in responses:
        key = (entry["url"], entry["resource"], _canonical(entry["form"]))
        require(key not in seen, "같은 요청을 다시 보냈습니다")
        seen.add(key)
        require(
            entry["method"] == "POST" and entry["status_code"] == 200,
            "요청 방식·상태 기록이 다릅니다",
        )
        require(
            entry["path"].startswith(f"{entry['attempt']:04d}-{entry['resource']}-"),
            "원본 파일 이름이 시도 번호와 다릅니다",
        )
        path = (raw_path / entry["path"]).resolve()
        require(path.parent == raw_path.resolve(), "원본 경로가 실행 폴더 밖입니다")
        body = path.read_bytes()
        require(hashlib.sha256(body).hexdigest() == entry["sha256"], "원본 해시 불일치")
        bodies.append(body)
    require(
        {p.name for p in raw_path.glob("*.body")} == {e["path"] for e in responses},
        "기록에 없는 원본 파일이 있습니다",
    )
    check_intervals(state, responses)
    times = [_time(entry["observed_at"]) for entry in responses]
    require(
        _time(state["started_at"]) <= times[0]
        and times[-1] <= _time(state["finished_at"]),
        "관측 시각이 실행 시간 밖입니다",
    )
    hashes = report["processed_hashes"]
    require(
        set(hashes) == {f"{name}.jsonl" for name in expected_files(state)}
        and {p.name for p in processed_path.glob("*.jsonl")} == set(hashes),
        "가공 파일 목록이 다릅니다",
    )
    for name, digest in hashes.items():
        require(_sha256(processed_path / name) == digest, f"{name} 해시 불일치")
    return state, report, bodies


def replay(
    state,
    report,
    bodies,
    processed_path,
    *,
    positions,
    position_field,
    allow_empty_cells,
):
    """2단계. 저장한 응답으로 수집기를 다시 실행해 요청·가공 결과가 같은지 본다."""
    responses = state["responses"]
    lists = [e for e in responses if e["resource"] == "player_list"]
    require(bool(lists) and lists[0]["url"] == CATALOG_URL, "목록 요청이 없습니다")
    template = dict(lists[0]["form"])
    ids = list(dict.fromkeys(_team_ids(e["form"]) for e in lists))
    require(len(ids) == len(state["clubs"]), "클럽 기록과 목록 요청이 다릅니다")
    clubs = [_team(name, key) for name, key in zip(state["clubs"], ids, strict=True)]
    require(
        [c.name for c in clubs if c.kind == "nation"] == state.get("nations", []),
        "국가 기록과 목록 요청이 다릅니다",
    )
    # 클럽 칸의 폼은 템플릿 값을, 국가 칸의 폼은 리그·클럽 0을 쓴다. 클럽 칸에서 다시 만든다.
    first = next((e for e in lists if _team_ids(e["form"])[2] == "0"), None)
    if first is not None:
        template = dict(first["form"])
    else:
        template.update(n4LeagueId="0", n4TeamId="0", n4NationId="0")
    holder = {}

    def clock():
        run = holder.get("run")
        if run is None:
            return _time(state["started_at"])
        pending = run.state.get("pending")
        if pending:
            return _time(responses[pending["attempt"] - 1]["observed_at"])
        done = run.state["responses"]
        return _time(done[-1]["observed_at"]) if done else _time(state["started_at"])

    def fetch(url, form, resource):
        attempt = holder["run"].state["attempts"]
        require(attempt <= len(responses), "재생이 기록보다 많은 요청을 보냅니다")
        entry = responses[attempt - 1]
        require(
            (url, resource, _canonical(form))
            == (entry["url"], entry["resource"], _canonical(entry["form"])),
            f"{attempt}번째 요청이 기록과 다릅니다",
        )
        if timing_version(state) == 2:
            return TimedBody(
                bodies[attempt - 1],
                SendStamp(_time(entry["sent_at"]), entry["send_started_ns"]),
            )
        return bodies[attempt - 1]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = RealDataRun(
            root / "raw" / state["run_id"],
            root / "processed" / state["run_id"],
            fetch,
            run_id=state["run_id"],
            clubs=clubs,
            template=template,
            max_attempts=state["max_attempts"],
            positions=positions,
            position_field=position_field,
            allow_empty_cells=allow_empty_cells,
            prices_only=prices_only(state),
            split_ovr=state.get("split_ovr", False),
            clock=clock,
            timing_version=timing_version(state),
        )
        holder["run"] = run
        replayed = run.execute()
        require(
            replayed["status"] == state["status"],
            f"재생이 멈췄습니다: {replayed['stop_reason']}",
        )
        require(
            _canonical(run.state["responses"]) == _canonical(responses),
            "재생한 요청·관측 기록이 다릅니다",
        )
        # 가격 충돌 기록이 생기기 전 실행(예: real-data-20261021-b)은 충돌 0건으로 본다.
        defaults = dict(price_conflicts=[])
        for key in ("split_log", "truncated_cells", "salary_parity", "price_conflicts"):
            require(
                _canonical(run.state.get(key, defaults.get(key)))
                == _canonical(state.get(key, defaults.get(key))),
                f"재생한 {key}가 다릅니다",
            )
        for key in (
            "eligibility_basis",
            "catalog_cards",
            "cards_by_club",
            "profile_cards",
            "split_cells",
            "empty_cells",
            "truncated_cells",
            "salary_parity",
            "price_conflicts",
            "processed_hashes",
        ):
            require(
                _canonical(replayed[key]) == _canonical(report.get(key, 0)),
                f"재생한 보고서의 {key}가 다릅니다",
            )
        for name in expected_files(state):
            require(
                (root / "processed" / state["run_id"] / f"{name}.jsonl").read_bytes()
                == (processed_path / f"{name}.jsonl").read_bytes(),
                f"재생한 {name}.jsonl이 다릅니다",
            )


def _team_ids(form):
    # 국적 조건이 생기기 전 실행의 폼에도 n4NationId=0이 들어 있다.
    return (form["n4LeagueId"], form["n4TeamId"], form.get("n4NationId", "0"))


def _team(name, key):
    league, team, nation = key
    if league == team == "0":
        return NationSpec(name, int(nation))
    require(nation == "0", f"{name}: 클럽 칸에 국적 조건이 있습니다")
    return ClubSpec(name, int(league), int(team))


def _club_names(state):
    lists = [e for e in state["responses"] if e["resource"] == "player_list"]
    ids = list(dict.fromkeys(_team_ids(e["form"]) for e in lists))
    return dict(zip(ids, state["clubs"], strict=True))


def check_ovr_leaves(state, leaves, split_rows):
    """OVR로 나눈 칸: 범위가 0~200을 빈틈·겹침 없이 덮고, 카드가 겹치지 않으며,
    나눈 칸의 카드는 모두 그 범위 안의 마지막 칸에서 다시 보여야 한다."""
    groups = {}
    for cell, rows in leaves.items():
        if cell[4] is not None:
            groups.setdefault(cell[:4], []).append((cell[4], rows))
    require(
        bool(groups) <= bool(state.get("split_ovr")),
        "OVR 분할을 쓰지 않은 실행에 OVR 범위 칸이 있습니다",
    )
    for group, items in groups.items():
        items.sort()
        edge = OVR_MIN
        seen = set()
        for (low, high), rows in items:
            require(low == edge and low <= high, f"{group}: OVR 칸이 비거나 겹칩니다")
            require(not seen & rows, f"{group}: OVR 칸끼리 같은 카드가 있습니다")
            seen |= rows
            edge = high + 1
        require(edge == OVR_MAX + 1, f"{group}: OVR 범위를 다 덮지 않습니다")
    for cell, rows in split_rows.items():
        if cell[3] is None:
            continue  # 급여 칸 분할은 포지션 칸의 잘림 때문에 포함 관계를 보장하지 않는다.
        low, high = cell[4] or (OVR_MIN, OVR_MAX)
        inner = set()
        for (low2, high2), found in groups.get(cell[:4], ()):
            if low <= low2 and high2 <= high:
                inner |= found
        require(rows <= inner, f"{cell[:4]}: 나눈 OVR 칸의 카드가 아래 칸에 없습니다")


def check_independently(
    state, report, bodies, processed_path, position_field, allow_empty_cells=False
):
    """3단계. 주 파서 없이 원본에서 읽은 값으로 분할·카드·능력치·파생을 대조한다."""
    names = _club_names(state)

    def logged(item):
        # OVR 범위는 범위 칸의 기록에만 있다. 없으면 범위 없는 칸이다.
        ovr = (item["ovr_low"], item["ovr_high"]) if "ovr_low" in item else None
        return (item["club"], item["low"], item["high"], item["position"], ovr)

    split = {logged(s) for s in state["split_log"]}
    truncated = {logged(t) for t in state["truncated_cells"]}
    split_rows = {}
    leaves = {}
    catalog = set()
    memberships = set()  # (클럽 이름, spid). 여러 클럽 경력 카드는 클럽마다 하나씩
    abilities = []
    lists = [e for e in state["responses"] if e["resource"] == "player_list"]
    # 포지션 칸이 아니면 폼의 포지션 값은 저장된 템플릿 값 그대로다.
    blank = lists[0]["form"].get(position_field) if position_field else None
    full = (lists[0]["form"].get("n4OvrMin"), lists[0]["form"].get("n4OvrMax"))
    for entry, body in zip(state["responses"], bodies, strict=True):
        form = entry["form"]
        if entry["resource"] == "ability":
            abilities.append((entry, body))
            continue
        require(entry["url"] == CATALOG_URL, "목록 주소가 다릅니다")
        if form["n1Strong"] != "5":
            continue  # 11강 급여 대조 요청. 카드 목록에 넣지 않는다.
        position = form.get(position_field) if position_field else None
        span = (form.get("n4OvrMin"), form.get("n4OvrMax"))
        cell = (
            names[_team_ids(form)],
            int(form["n4SalaryMin"]),
            int(form["n4SalaryMax"]),
            None if position == blank else position,
            None if span == full else (int(span[0]), int(span[1])),
        )
        rows = set(ROW_ID.findall(body))
        if not rows and not allow_empty_cells:
            require(
                body.count(EMPTY_ROW) == 1 and body.count(NO_RESULT) == 1,
                "행 없는 목록이 결과 없음 화면이 아닙니다",
            )
        if cell in split:
            require(len(rows) == LIST_CAP, "200장이 아닌 칸을 나눴습니다")
            split_rows[cell] = rows
            continue
        require(
            len(rows) < LIST_CAP or cell in truncated,
            "200장 칸을 나누지도 잘림으로 표시하지도 않았습니다",
        )
        leaves[cell] = rows
        catalog |= {int(spid) for spid in rows}
        memberships |= {(cell[0], int(spid)) for spid in rows}
    for club in state["clubs"]:
        # 포지션으로 나눈 칸은 같은 급여 한 값을 공유하므로 급여 범위로만 덮음을 본다.
        spans = sorted({(low, high) for name, low, high, *_ in leaves if name == club})
        edge = BANDS[0][0]
        for low, high in spans:
            require(low == edge and low <= high, f"{club}: 급여 칸이 비거나 겹칩니다")
            edge = high + 1
        require(edge == BANDS[-1][1] + 1, f"{club}: 급여 범위를 다 덮지 않습니다")
    check_ovr_leaves(state, leaves, split_rows)
    require(len(catalog) == report["catalog_cards"], "목록 카드 수가 다릅니다")
    if prices_only(state):
        require(not abilities, "가격 갱신 실행에 능력치 요청이 있습니다")
        salaries = read_jsonl(processed_path / "catalog_salary.jsonl", CardSalary)
        require(
            sorted(s.spid for s in salaries) == sorted(catalog)
            and all(s.reinforce_level == 5 for s in salaries)
            and report["profile_cards"] == 0,
            "목록 급여가 목록 카드와 다릅니다",
        )
    else:
        check_profiles(report, abilities, catalog, memberships, names, processed_path)
    check_memberships_and_prices(state, report, processed_path, memberships)
    return len(leaves)


def check_profiles(report, abilities, catalog, memberships, names, processed_path):
    """능력치 상세·강화 파생을 원본에서 읽은 값과 대조한다(전체 수집 실행)."""
    cards = {
        c.spid: c for c in read_jsonl(processed_path / "player_card.jsonl", PlayerCard)
    }
    require(
        [e["form"]["spid"] for e, _ in abilities] == sorted(catalog) == sorted(cards)
        and len(cards) == report["profile_cards"],
        "능력치 요청 카드가 목록 카드와 다릅니다",
    )
    labels = keyword_map()
    for entry, body in abilities:
        spid = entry["form"]["spid"]
        require(
            entry["url"] == ABILITY_URL
            and _canonical(entry["form"]) == _canonical(ability_form(spid, 5)),
            f"{spid}: 능력치 요청 폼이 다릅니다",
        )
        card = cards[spid]
        require(
            card.version == entry["sha256"]
            and card.observed_at
            == ParseContext.normalize_time(_time(entry["observed_at"]))
            and card.base_reinforce_level == 5,
            f"{spid}: 카드 출처가 원본과 다릅니다",
        )
        shown = {labels[label]: value for label, value in displayed_stats(body).items()}
        require(card.stats == shown, f"{spid}: 능력치 직접 표시 숫자와 다릅니다")
        nations = {
            int(nation)
            for (league, team, nation), name in names.items()
            if league == team == "0" and (name, spid) in memberships
        }
        if nations:
            flags = {int(flag) for flag in FLAG.findall(body)}
            require(flags == nations, f"{spid}: 상세 국적이 국적 검색 조건과 다릅니다")

    derived = read_jsonl(processed_path / "reinforce_stats.jsonl", ReinforceStats)
    require(len(derived) == len(cards) * 6, "6~11강 파생 행 수가 다릅니다")
    for row in derived:
        card = cards[row.spid]
        delta = reinforcement_delta(5, row.reinforce_level)
        require(
            row.baseline_reinforce_level == 5
            and row.baseline_card_version == card.version
            and row.stats_delta == {name: delta for name in card.stats}
            and row.position_ovr
            == {p: v + delta for p, v in card.position_ovr.items()},
            f"{row.spid}: {row.reinforce_level}강 파생이 강화 표와 다릅니다",
        )


def check_memberships_and_prices(state, report, processed_path, memberships):
    colors = read_jsonl(processed_path / "team_color.jsonl", TeamColor)
    require(
        {(c.name, c.spid) for c in colors} == memberships
        and len(colors) == len(memberships)
        and all(
            c.type == ("nation" if c.name in state.get("nations", []) else "club")
            for c in colors
        ),
        "클럽 자격 관측이 클럽별 목록 카드와 다릅니다",
    )
    prices = {
        (p.spid, p.reinforce_level): p
        for p in read_jsonl(processed_path / "card_price.jsonl", CardPrice)
    }
    conflicts = state.get("price_conflicts", [])
    require(
        report.get("price_conflicts", 0) == len(conflicts)
        and all(
            len(set(c["prices"])) > 1
            and prices[(c["spid"], c["reinforce_level"])].price is None
            and prices[(c["spid"], c["reinforce_level"])].status == "unknown"
            for c in conflicts
        ),
        "가격 충돌 키가 unknown으로 남지 않았습니다",
    )


def verify(
    raw_path,
    processed_path,
    *,
    positions=(),
    position_field=None,
    allow_empty_cells=False,
):
    raw_path, processed_path = Path(raw_path), Path(processed_path)
    state, report, bodies = check_records(raw_path, processed_path)
    replay(
        state,
        report,
        bodies,
        processed_path,
        positions=tuple(positions),
        position_field=position_field,
        allow_empty_cells=allow_empty_cells,
    )
    leaves = check_independently(
        state,
        report,
        bodies,
        processed_path,
        position_field,
        allow_empty_cells=allow_empty_cells,
    )
    return dict(
        run_id=state["run_id"],
        verified=True,
        attempts=state["attempts"],
        list_requests=sum(e["resource"] == "player_list" for e in state["responses"]),
        ability_requests=sum(e["resource"] == "ability" for e in state["responses"]),
        leaf_cells=leaves,
        catalog_cards=report["catalog_cards"],
        profile_cards=report["profile_cards"],
        replayed_processed_files=len(expected_files(state)),
        network_requests=0,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--position-field", help="수집 때 쓴 포지션 필터 필드 이름")
    parser.add_argument("--position", action="append", default=[])
    parser.add_argument("--allow-empty-cells", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify(
            args.root / "data/raw" / args.run_id,
            args.root / "data/processed" / args.run_id,
            positions=args.position,
            position_field=args.position_field,
            allow_empty_cells=args.allow_empty_cells,
        )
    except VerificationError as error:
        print(json.dumps(dict(verified=False, reason=str(error)), ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
