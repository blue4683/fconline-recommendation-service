"""실데이터 브랜치의 클럽 경력 검색 수집기.

설계: docs/real-data-branch-design.md §4. 이 모듈은 요청 계획·분할 규칙·관측 변환·
실행 상태를 담당한다. 네트워크 전송은 주입한 `fetch`가 맡으므로 단위 테스트는
합성 응답만 쓴다. 기본 CLI 실행은 계획 출력이며 `--run`은 별도 승인 범위에서만 쓴다.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from FRS.core.freshness import Resource
from FRS.schema.models import (
    CardPrice,
    CardSalary,
    DataStatus,
    ReinforceStats,
    TeamColor,
)
from FRS.schema.scope import SUPPORTED_REINFORCEMENTS
from FRS.utils.crawling.ability_parser import ParseContext, ParseError, parse_ability
from FRS.utils.crawling.player_list_parser import PlayerListContext, parse_player_list
from FRS.utils.crawling.refresh_observations import (
    CATALOG_URL,
    _policy_values,
    adopt_ability,
)
from FRS.utils.crawling.request_timing import SECOND_NS, SendStartPacer, TimedBody

LIST_CAP = 200
BANDS = ((4, 20), (21, 25), (26, 30), (31, 35), (36, 99))
SALARY_MIN, SALARY_MAX = BANDS[0][0], BANDS[-1][1]
ABILITY_URL = "https://fconline.nexon.com/datacenter/PlayerAbility"
ELIGIBILITY_BASIS = "club_career_search"
SKILLS = ("sprintspeed", "acceleration", "strength", "stamina")
RUN_ID_PATTERN = r"real-data-[a-z0-9]+(?:-[a-z0-9]+)*"
# 코드의 안전 상한이다. 실행마다 승인받은 --max-attempts가 실제 상한이다.
# 3클럽 한 실행(측정 기준 약 5,530회)을 담도록 2026-10-22 사용자 결정으로 올렸다(RD-4).
MAX_ATTEMPTS_LIMIT = 8000
# 템플릿에 이미 있어야 하는 키다. 없으면 저장된 공식 폼이 아니므로 거부한다.
REQUIRED_KEYS = (
    "n4LeagueId",
    "n4TeamId",
    "n1History",
    "teamcolorid",
    "n4SalaryMin",
    "n4SalaryMax",
    "n4PageNo",
    "n1Strong",
    *(f"strSkill{i}" for i in range(1, 5)),
)
BONUSES = (0, 1, 2, 4, 6, 8, 11, 15, 17, 19, 21, 24, 27)


class CatalogStopped(RuntimeError):
    """요청 상한·차단·구조 변경 등으로 실행을 멈춘다. 재개하지 않는다."""

    def __init__(self, kind, message=""):
        self.failure = dict(kind=kind, message=message)
        super().__init__(f"{kind}: {message}" if message else kind)


class CannotSplit(ValueError):
    pass


@dataclass(frozen=True)
class ClubSpec:
    name: str
    league_id: int
    team_id: int

    def __post_init__(self):
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or type(self.league_id) is not int
            or type(self.team_id) is not int
            or self.league_id <= 0
            or self.team_id <= 0
        ):
            raise ValueError(
                "클럽 이름과 리그·클럽 ID는 확인한 값으로 명시해야 합니다."
            )


@dataclass(frozen=True)
class Cell:
    club: ClubSpec
    low: int
    high: int
    position: str | None = None

    def key(self):
        return (self.club.name, self.low, self.high, self.position)

    def describe(self):
        return dict(
            club=self.club.name, low=self.low, high=self.high, position=self.position
        )


def initial_cells(clubs):
    clubs = tuple(clubs)
    if not clubs or len({c.name for c in clubs}) != len(clubs):
        raise ValueError("서로 다른 클럽을 하나 이상 명시해야 합니다.")
    return tuple(Cell(club, lo, hi) for club in clubs for lo, hi in BANDS)


def split_cell(cell, positions=()):
    """정확히 200장이라 잘렸을 수 있는 칸만 나눈다. 더 나눌 수 없으면 CannotSplit."""
    if cell.position is not None:
        raise CannotSplit("포지션까지 나눈 칸은 더 나눌 수 없습니다.")
    if cell.low < cell.high:
        mid = (cell.low + cell.high) // 2
        return (
            Cell(cell.club, cell.low, mid),
            Cell(cell.club, mid + 1, cell.high),
        )
    if not positions:
        raise CannotSplit("급여 한 값 칸이며 확인된 포지션 필터가 없습니다.")
    return tuple(Cell(cell.club, cell.low, cell.high, p) for p in positions)


def validate_template(template):
    if not isinstance(template, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in template.items()
    ):
        raise ValueError("폼 템플릿은 문자열 키·값 사전이어야 합니다.")
    missing = [k for k in REQUIRED_KEYS if k not in template]
    if missing:
        raise ValueError(f"저장된 공식 폼에 필요한 키가 없습니다: {missing}")
    if template["teamcolorid"] != "0" or template["n1History"] != "0":
        raise ValueError(
            "클럽 경력 검색 폼(n1History=0, teamcolorid=0)이 아닙니다. 소속 팀컬러 폼은 쓰지 않습니다."
        )
    if sorted(template[f"strSkill{i}"] for i in range(1, 5)) != sorted(SKILLS):
        raise ValueError("선택 능력치 열이 확인된 네 열과 다릅니다.")
    return dict(template)


def build_form(template, cell, *, level=5, position_field=None):
    form = dict(template)
    form.update(
        n4LeagueId=str(cell.club.league_id),
        n4TeamId=str(cell.club.team_id),
        n4SalaryMin=str(cell.low),
        n4SalaryMax=str(cell.high),
        n4PageNo="1",
        n1Strong=str(level),
    )
    if cell.position is not None:
        if not position_field:
            raise ValueError(
                "확인된 포지션 필터 필드 없이 포지션으로 나눌 수 없습니다."
            )
        if position_field not in template:
            raise ValueError("포지션 필드가 저장된 공식 폼에 없습니다.")
        form[position_field] = cell.position
    return form


def ability_form(spid, level):
    return {
        "spid": spid,
        "n1Strong": level,
        "n1Grow": 0,
        "n4TeamColorId": 0,
        "n4TeamColorLv": 0,
        "n4TeamColorId_Enhance": 0,
        "n4TeamColorLv_Enhance": 0,
        "n4TeamColorId_Feature": 0,
        "n1Change": 0,
        "strPlayerImg": "",
    }


def bonus_delta(level):
    return BONUSES[level - 1] - BONUSES[4]


def validate_run_id(value):
    if (
        not isinstance(value, str)
        or len(value) > 80
        or re.fullmatch(RUN_ID_PATTERN, value) is None
    ):
        raise CatalogStopped("invalid_run", "실행 ID는 real-data-로 시작해야 합니다.")


def _now():
    return datetime.now(timezone.utc)


def _write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


class RealDataRun:
    """목록 → 급여 대조 → 능력치 순서로 한 번 실행한다.

    `fetch(url, form, resource)`는 응답 본문(bytes)을 돌려주거나 `CatalogStopped`를
    일으킨다. 요청 상한은 요청 전에 예약하므로 실패·중단도 상한에 포함된다.
    """

    def __init__(
        self,
        raw_path,
        processed_path,
        fetch,
        *,
        run_id,
        clubs,
        template,
        max_attempts,
        positions=(),
        position_field=None,
        allow_empty_cells=False,
        catalog_only=False,
        clock=_now,
        timing_version=1,
    ):
        validate_run_id(run_id)
        if type(timing_version) is not int or timing_version not in (1, 2):
            raise ValueError("지원하는 요청 시각 형식은 1 또는 2입니다.")
        self.timing_version = timing_version
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
            raise CatalogStopped(
                "invalid_run", f"요청 상한은 1~{MAX_ATTEMPTS_LIMIT}의 정수여야 합니다."
            )
        self.cells = initial_cells(clubs)
        self.template = validate_template(template)
        self.positions = tuple(positions)
        self.position_field = position_field
        if self.positions and not position_field:
            raise ValueError("포지션 목록에는 확인된 필드 이름이 필요합니다.")
        self.allow_empty_cells = allow_empty_cells
        self.catalog_only = catalog_only
        self.run_id, self.max_attempts = run_id, max_attempts
        self.fetch_body, self.clock = fetch, clock
        raw_path.mkdir(parents=True, exist_ok=False)
        processed_path.mkdir(parents=True, exist_ok=False)
        self.raw_path, self.processed_path = raw_path, processed_path
        self.rows = {}  # spid -> 5강 목록 행
        # 여러 클럽 경력 카드는 클럽마다 자격 관측을 따로 남긴다.
        self.memberships = {}  # (spid, 클럽 이름) -> TeamColor
        self.prices = {}  # (spid, level) -> CardPrice
        # 같은 가격 키가 다른 값으로 관측되면 최신값을 고르지 않고 unknown으로 둔다.
        self.price_conflicts = {}  # (spid, level) -> 관측한 가격 목록
        self.accepted = []  # (Cell, rows)
        self.split_log, self.truncated = [], []
        self.records = {
            k: []
            for k in (
                "player_card",
                "card_salary",
                "card_price",
                "reinforce_stats",
                "team_color",
            )
        }
        self.state = dict(
            run_id=run_id,
            status="running",
            attempts=0,
            max_attempts=max_attempts,
            retries=0,
            min_request_interval_seconds=1,
            eligibility_basis=ELIGIBILITY_BASIS,
            provider_permission="unconfirmed",
            user_decisions=["RD-1", "RD-2"],
            clubs=[c.club.name for c in self.cells[:: len(BANDS)]],
            responses=[],
            started_at=clock().isoformat(),
        )
        if timing_version == 2:
            self.state["timing_version"] = 2
        self.flush()

    def flush(self):
        _write_json(self.raw_path / "manifest.json", self.state)

    def request(self, url, form, resource, spid=None, level=None):
        if self.state["status"] != "running":
            raise CatalogStopped("finished", "종료한 실행은 요청할 수 없습니다.")
        if self.state["attempts"] >= self.max_attempts:
            raise CatalogStopped("budget_exhausted", "승인된 요청 상한에 도달했습니다.")
        self.state["attempts"] += 1
        attempt = self.state["attempts"]
        self.state["pending"] = dict(
            attempt=attempt, resource=resource, url=url, form=form
        )
        self.flush()  # 요청 전에 예약해 중단·예외도 상한에 포함한다.
        observed = self.clock()
        body = self.fetch_body(url, form, resource)
        timing = {}
        if self.timing_version == 2:
            if not isinstance(body, TimedBody):
                raise CatalogStopped("timing", "새 형식의 전송 시각이 없습니다.")
            stamp = body.stamp
            previous = self.state["responses"]
            if previous and (
                stamp.started_ns - previous[-1]["send_started_ns"] < SECOND_NS
            ):
                raise CatalogStopped("timing", "전송 시작 간격이 1초보다 짧습니다.")
            observed = stamp.sent_at
            timing = dict(
                sent_at=observed.isoformat(), send_started_ns=stamp.started_ns
            )
            body = body.body
        if not isinstance(body, bytes) or not body.strip():
            raise CatalogStopped("unexpected_response", "빈 응답 또는 잘못된 형식")
        name = f"{attempt:04d}-{resource}-{spid or 0}-{level or 0}.body"
        (self.raw_path / name).write_bytes(body)
        entry = dict(
            attempt=attempt,
            resource=resource,
            url=url,
            method="POST",
            form=form,
            path=name,
            observed_at=observed.isoformat(),
            status_code=200,
            sha256=hashlib.sha256(body).hexdigest(),
            **timing,
        )
        self.state["responses"].append(entry)
        self.state.pop("pending", None)
        self.flush()
        return body, entry

    def list_rows(self, form, level):
        body, entry = self.request(CATALOG_URL, form, "player_list", level=level)
        context = PlayerListContext(
            batch_id=self.run_id,
            source="nexon_web",
            observed_at=entry["observed_at"],
            delete_after=None,
            version=entry["sha256"],
            reinforce_level=level,
            skill_fields=tuple(form[f"strSkill{i}"] for i in range(1, 5)),
        )
        if b"area_playerunit_" not in body:
            if not self.allow_empty_cells:
                raise CatalogStopped(
                    "empty_list_unverified",
                    "빈 목록 구조가 확인되기 전에는 빈 칸을 허용하지 않습니다.",
                )
            return (), entry
        try:
            return parse_player_list(body, context), entry
        except ParseError as error:
            raise CatalogStopped("parsing", str(error)) from None

    def collect_catalog(self):
        queue = list(self.cells)
        seen = set()
        while queue:
            cell = queue.pop(0)
            if cell.key() in seen:
                raise CatalogStopped(
                    "duplicate_cell", "같은 칸을 두 번 요청할 수 없습니다."
                )
            seen.add(cell.key())
            form = build_form(self.template, cell, position_field=self.position_field)
            rows, entry = self.list_rows(form, 5)
            if len(rows) > LIST_CAP:
                raise CatalogStopped("over_cap", f"{len(rows)}장은 상한을 넘습니다.")
            if any(not cell.low <= row.salary <= cell.high for row in rows):
                raise CatalogStopped(
                    "salary_range", "요청 급여 구간과 응답이 다릅니다."
                )
            if len(rows) == LIST_CAP:
                try:
                    children = split_cell(cell, self.positions)
                except CannotSplit as error:
                    # 잘림 가능 칸은 버리지 않고 표시해 둔다(설계 §10).
                    self.truncated.append(dict(cell.describe(), reason=str(error)))
                    self.accept(cell, rows, entry)
                    continue
                self.split_log.append(
                    dict(cell.describe(), rows=len(rows), into=len(children))
                )
                queue[0:0] = children
                continue
            self.accept(cell, rows, entry)
        self.state["split_log"] = self.split_log
        self.state["truncated_cells"] = self.truncated
        self.state["price_conflicts"] = [
            dict(spid=spid, reinforce_level=level, prices=prices)
            for (spid, level), prices in sorted(self.price_conflicts.items())
        ]
        self.flush()

    def accept(self, cell, rows, entry):
        self.accepted.append((cell, rows))
        at = self.clock()
        for row in rows:
            first = self.rows.setdefault(row.spid, row)
            if first.salary != row.salary:
                raise CatalogStopped("conflict", "같은 카드의 급여 관측이 충돌합니다.")
            self.memberships.setdefault(
                (row.spid, cell.club.name),
                TeamColor(
                    batch_id=self.run_id,
                    source="nexon_web",
                    observed_at=entry["observed_at"],
                    delete_after=None,
                    spid=row.spid,
                    version=entry["sha256"],
                    type="club",
                    name=cell.club.name,
                    **_policy_values(Resource.TEAM_COLOR, row.observed_at, at),
                ),
            )
            for price in row.prices:
                key = (price.spid, price.reinforce_level)
                values = {**price.model_dump(), "price_type": "reference"}
                values.update(_policy_values(Resource.PRICE, price.observed_at, at))
                if price.price is None:
                    values.update(status=DataStatus.UNKNOWN, valid_until=None)
                fresh = CardPrice.model_validate(values)
                known = self.prices.get(key)
                if known is None:
                    self.prices[key] = fresh
                elif key in self.price_conflicts:
                    self.price_conflicts[key].append(fresh.price)
                elif known.price != fresh.price:
                    # 사용자 결정(2026-10-21): 이 카드·강화의 가격만 unknown으로 둔다.
                    self.price_conflicts[key] = [known.price, fresh.price]
                    self.prices[key] = CardPrice.model_validate(
                        {
                            **known.model_dump(),
                            "price": None,
                            "status": DataStatus.UNKNOWN,
                            "valid_until": None,
                        }
                    )

    def check_salary_parity(self):
        """클럽마다 11강 목록을 1회 받아 5강과 급여가 같은지 확인한다."""
        for club in dict.fromkeys(cell.club for cell, _ in self.accepted):
            candidates = [
                (cell, rows)
                for cell, rows in self.accepted
                if cell.club == club and cell.position is None and rows
            ]
            if not candidates:
                raise CatalogStopped("parity", f"{club.name}: 대조할 칸이 없습니다.")
            cell, rows = max(candidates, key=lambda item: len(item[1]))
            form = build_form(
                self.template, cell, level=11, position_field=self.position_field
            )
            high_rows, _ = self.list_rows(form, 11)
            low = {row.spid: row.salary for row in rows}
            high = {row.spid: row.salary for row in high_rows}
            if low != high:
                raise CatalogStopped(
                    "parity",
                    f"{club.name}: 5강과 11강의 카드·급여가 다릅니다. "
                    "강화별 급여 수집으로 설계를 바꿔야 합니다.",
                )
        self.state["salary_parity"] = "confirmed_per_club"
        self.flush()

    def collect_abilities(self):
        for spid in sorted(self.rows):
            body, entry = self.request(
                ABILITY_URL, ability_form(spid, 5), "ability", spid=spid, level=5
            )
            context = ParseContext(
                batch_id=self.run_id,
                source="nexon_web",
                observed_at=entry["observed_at"],
                delete_after=None,
                spid=spid,
                reinforce_level=5,
                version=entry["sha256"],
            )
            try:
                parsed = adopt_ability(parse_ability(body, context), at=self.clock())
            except ParseError as error:
                raise CatalogStopped("parsing", f"{spid}: {error}") from None
            if parsed.salary.salary != self.rows[spid].salary:
                raise CatalogStopped(
                    "conflict", f"{spid}: 목록과 상세 급여가 다릅니다."
                )
            self.add_card(parsed)

    def add_card(self, parsed):
        card, salary = parsed.card, parsed.salary
        self.records["player_card"].append(card)
        for level in SUPPORTED_REINFORCEMENTS:
            self.records["card_salary"].append(
                salary
                if level == 5
                else CardSalary.model_validate(
                    {
                        **salary.model_dump(),
                        "reinforce_level": level,
                        "version": f"{salary.version}-same-salary-{level}",
                    }
                )
            )
            if level == 5:
                continue
            delta = bonus_delta(level)
            self.records["reinforce_stats"].append(
                ReinforceStats(
                    **card.model_dump(
                        include={
                            "batch_id",
                            "source",
                            "observed_at",
                            "delete_after",
                            "spid",
                            "status",
                            "valid_until",
                        }
                    ),
                    version=f"{card.version}-derived-{level}",
                    reinforce_level=level,
                    baseline_reinforce_level=5,
                    baseline_card_version=card.version,
                    stats_delta={name: delta for name in card.stats},
                    position_ovr={
                        position: value + delta
                        for position, value in card.position_ovr.items()
                    },
                )
            )

    def save(self):
        self.records["card_price"] = list(self.prices.values())
        self.records["team_color"] = list(self.memberships.values())
        for name, rows in self.records.items():
            path = self.processed_path / f"{name}.jsonl"
            path.write_text(
                "".join(row.model_dump_json() + "\n" for row in rows),
                encoding="utf-8",
            )

    def execute(self):
        try:
            self.collect_catalog()
            self.check_salary_parity()
            if self.catalog_only:
                # 측정 실행: 목록·급여 대조까지만 하고 능력치·가공 파일은 만들지 않는다.
                self.state["status"] = "catalog_measured"
            else:
                self.collect_abilities()
                self.save()
                self.state["status"] = "completed"
        except KeyboardInterrupt:
            self.state.update(
                status="interrupted", stop_reason={"kind": "process_interrupted"}
            )
        except CatalogStopped as error:
            self.state.update(status="stopped", stop_reason=error.failure)
        finally:
            self.state["finished_at"] = self.clock().isoformat()
            self.flush()
        report = dict(
            run_id=self.run_id,
            status=self.state["status"],
            stop_reason=self.state.get("stop_reason"),
            attempts=self.state["attempts"],
            max_attempts=self.max_attempts,
            eligibility_basis=ELIGIBILITY_BASIS,
            catalog_cards=len(self.rows),
            cards_by_club=dict(
                Counter(color.name for color in self.memberships.values())
            ),
            profile_cards=len(self.records["player_card"]),
            split_cells=len(self.split_log),
            truncated_cells=self.truncated,
            salary_parity=self.state.get("salary_parity"),
            price_conflicts=len(self.price_conflicts),
            auto_activation=False,
            processed_hashes={
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(self.processed_path.glob("*.jsonl"))
            },
        )
        if self.timing_version == 2:
            report["timing_version"] = 2
        _write_json(self.processed_path / "report.json", report)
        return report


def plan(clubs, *, max_attempts=None, catalog_only=False):
    cells = initial_cells(clubs)
    return dict(
        scope="real_data_branch_single_run",
        mode="catalog_only" if catalog_only else "full",
        eligibility_basis=ELIGIBILITY_BASIS,
        initial_list_requests=len(cells),
        split_rule="only_when_exactly_200_rows",
        parity_requests=len(tuple(dict.fromkeys(c.club for c in cells))),
        ability_requests=0 if catalog_only else "one_per_unique_card_at_reinforce_5",
        price_conflict="mark_key_unknown_and_report",
        retries=0,
        min_request_interval_seconds=1,
        max_attempts=max_attempts,
        approval_required="exact run-id and max-attempts before --run",
    )


def _club(value):
    parts = value.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("클럽은 이름:리그ID:클럽ID 형식이어야 합니다.")
    try:
        return ClubSpec(parts[0], int(parts[1]), int(parts[2]))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def live_fetch(session, *, pacer=None):
    """기본 UA, 리디렉션 금지, 재시도 0회, 초당 1회로 전송한다."""
    from FRS.utils.crawling.get_response import (
        CollectionStopped,
        RequestPolicy,
        fetch_url,
    )

    pacer = pacer or SendStartPacer()

    def send(url, form, resource):
        stamp = None

        def transport(method, target, **options):
            nonlocal stamp
            # 실제 세션 호출 경계에서 기록한다. 응답/파일 기록 시간은 다음 대기에 흡수된다.
            stamp = pacer.mark_started()
            return session.request(method, target, **options)

        try:
            result = fetch_url(
                (url, None, None),
                policy=RequestPolicy(max_retries=0),
                method="POST",
                data=form,
                transport=transport,
                wait_before_attempt=pacer.wait,
            )
        except CollectionStopped as error:
            raise CatalogStopped(error.failure["kind"], str(error.failure)) from None
        except ValueError as error:
            raise CatalogStopped("timing", str(error)) from None
        if result[0] is None:
            raise CatalogStopped(result[1]["kind"], "요청 실패")
        response = result[0]
        try:
            return TimedBody(response.content, stamp)
        finally:
            response.close()

    return send


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--club", action="append", type=_club, required=True)
    parser.add_argument(
        "--run", action="store_true", help="별도 승인된 범위에서만 실행"
    )
    parser.add_argument("--run-id")
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--template", type=Path, help="저장된 공식 검색 폼(JSON)")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--position-field", help="확인된 포지션 필터 필드 이름")
    parser.add_argument(
        "--position",
        action="append",
        default=[],
        help="확인된 포지션 필터 값. 급여 한 값 칸이 200장일 때만 사용",
    )
    parser.add_argument(
        "--allow-empty-cells",
        action="store_true",
        help="빈 목록 HTML 구조를 확인한 뒤에만 지정",
    )
    parser.add_argument(
        "--catalog-only",
        action="store_true",
        help="목록·급여 대조만 요청해 카드 수를 잰다. 능력치·가공 파일 없음",
    )
    args = parser.parse_args(argv)
    if not args.run:
        print(
            json.dumps(
                plan(
                    args.club,
                    max_attempts=args.max_attempts,
                    catalog_only=args.catalog_only,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.run_id or args.max_attempts is None or not args.template:
        parser.error(
            "--run은 --run-id, --max-attempts, --template을 모두 명시해야 합니다."
        )
    import requests

    template = json.loads(args.template.read_text(encoding="utf-8"))
    with requests.Session() as session:
        session.trust_env = False
        run = RealDataRun(
            args.root / "data/raw" / args.run_id,
            args.root / "data/processed" / args.run_id,
            live_fetch(session),
            run_id=args.run_id,
            clubs=args.club,
            template=template,
            max_attempts=args.max_attempts,
            positions=args.position,
            position_field=args.position_field,
            allow_empty_cells=args.allow_empty_cells,
            catalog_only=args.catalog_only,
            timing_version=2,
        )
        report = run.execute()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in ("completed", "catalog_measured") else 1


if __name__ == "__main__":
    raise SystemExit(main())
