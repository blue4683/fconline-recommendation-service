"""사용자가 승인한 Stage 1-B 파일럿을 한 번 실행한다. 기본 실행은 계획 출력이다."""

import argparse
import hashlib
import json
import random
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import requests

from FRS.schema.models import CrawlLog
from FRS.utils.crawling.ability_parser import (
    ParseContext,
    ParseError,
    parse_ability,
    parse_club_history,
    reinforcement_delta,
    unverified_colors,
)
from FRS.utils.crawling.get_response import CollectionStopped, RequestPolicy, fetch_url

PILOT_ID = "stage-1-b-20261018"
SEED = 20261018
MAX_ATTEMPTS = 1000
POOL_SIZE = 200
SAMPLE_SIZE = 100
REGRESSION_IDS = (100000250, 100214100, 825045661, 804270673)
LEVELS = tuple(range(5, 12))
METADATA_URL = "https://open.api.nexon.com/static/fconline/meta/spid.json"
ABILITY_URL = "https://fconline.nexon.com/datacenter/PlayerAbility"
DETAIL_URL = "https://fconline.nexon.com/datacenter/PlayerInfo"
ROOT = Path(__file__).resolve().parents[3]


class PilotStopped(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def plan():
    return {
        "pilot_id": PILOT_ID,
        "regression_cards": len(REGRESSION_IDS),
        "screening_pool": POOL_SIZE,
        "stratified_cards": SAMPLE_SIZE,
        "reinforcement_levels": LEVELS,
        "normal_attempts": 1 + len(REGRESSION_IDS) + POOL_SIZE + 104 * 7,
        "max_attempts_including_retries": MAX_ATTEMPTS,
        "min_request_interval_seconds": 1,
        "raw_path": str(ROOT / "data/raw" / PILOT_ID),
        "processed_path": str(ROOT / "data/processed" / PILOT_ID),
        "delete_after": None,
        "provider_permission": "unconfirmed",
        "scope": "one_stage_1_b_pilot_only",
    }


def _round_robin(groups, count, seed=SEED):
    rng = random.Random(seed)
    keys = sorted(groups)
    rng.shuffle(keys)
    queues = {}
    for key in keys:
        values = sorted(groups[key])
        rng.shuffle(values)
        queues[key] = deque(values)
    output = []
    while len(output) < count:
        added = False
        for key in keys:
            if queues[key]:
                output.append(queues[key].popleft())
                added = True
                if len(output) == count:
                    break
        if not added:
            raise PilotStopped("표본을 구성할 데이터가 부족합니다.")
    return output


def screening_ids(metadata):
    if not isinstance(metadata, list):
        raise PilotStopped("선수 메타데이터 형식이 변경되었습니다.")
    groups = defaultdict(set)
    all_ids = set()
    for item in metadata:
        spid = item.get("id") if isinstance(item, dict) else None
        if (
            type(spid) is not int
            or not 1_000_000 <= spid <= 999_999_999
            or spid in all_ids
        ):
            raise PilotStopped("선수 메타데이터 ID가 유효하지 않습니다.")
        all_ids.add(spid)
        if spid not in REGRESSION_IDS:
            groups[spid // 1_000_000].add(spid)
    return _round_robin(groups, POOL_SIZE)


def position_role(position):
    if position == "GK":
        return "GK"
    if position in {"SW", "RWB", "RB", "RCB", "CB", "LCB", "LB", "LWB"}:
        return "DEF"
    if position in {
        "RDM",
        "CDM",
        "LDM",
        "RM",
        "RCM",
        "CM",
        "LCM",
        "LM",
        "RAM",
        "CAM",
        "LAM",
    }:
        return "MID"
    if position in {"RF", "CF", "LF", "RW", "RS", "ST", "LS", "LW"}:
        return "FW"
    raise PilotStopped("층화할 수 없는 포지션입니다.")


def stratified_ids(cards):
    groups = defaultdict(set)
    for card in cards:
        groups[(position_role(card.positions[0]), card.season_id)].add(card.spid)
    if {key[0] for key in groups} != {"GK", "DEF", "MID", "FW"}:
        raise PilotStopped("선별 후보에서 네 포지션군을 모두 확보하지 못했습니다.")
    selected = _round_robin(groups, SAMPLE_SIZE)
    by_id = {card.spid: card for card in cards}
    if {position_role(by_id[spid].positions[0]) for spid in selected} != {
        "GK",
        "DEF",
        "MID",
        "FW",
    }:
        raise PilotStopped("층화 표본에서 네 포지션군을 모두 확보하지 못했습니다.")
    return selected


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


class Pilot:
    run_id = PILOT_ID
    max_attempts = MAX_ATTEMPTS

    def execution_plan(self):
        return plan()

    def __init__(self, raw_path, processed_path, session):
        # 같은 승인으로 새 수집을 반복하지 않도록 기존 디렉터리는 거부한다.
        raw_path.mkdir(parents=True, exist_ok=False)
        processed_path.mkdir(parents=True, exist_ok=False)
        self.raw_path = raw_path
        self.processed_path = processed_path
        self.session = session
        self.state = {
            **self.execution_plan(),
            "attempts": 0,
            "status": "running",
            "started_at": now().isoformat(),
        }
        self.active = None
        self.responses = {}
        self.logs = []
        self.failures = []
        self.cards = {}
        self.records = defaultdict(list)
        self.parse_attempts = 0
        self.parse_failures = 0
        self.flush()

    def flush(self):
        write_json(self.raw_path / "manifest.json", self.state)

    def transport(self, method, url, **options):
        if self.state["attempts"] >= self.max_attempts:
            raise PilotStopped("승인된 요청 상한에 도달했습니다.")
        self.state["attempts"] += 1
        attempt = self.state["attempts"]
        self.flush()  # 요청 전에 예약하여 중단·예외도 상한에서 제외하지 않는다.
        observed = now()
        started = time.monotonic()
        response = None
        failure = None
        resource, spid, grade = self.active
        try:
            response = self.session.request(method, url, **options)
            status = response.status_code
            if status in (403, 429):
                failure = "blocked"
            elif time.monotonic() - started > 30:
                failure = "slow_response"
            elif status == 404:
                failure = "not_found"
            elif status in (502, 503, 504):
                failure = "http_error"
            elif status != 200 or not response.content.strip():
                failure = "unexpected_response"
            body_path = (
                self.raw_path
                / f"{attempt:04d}-{resource}-{spid or 0}-{grade or 0}.body"
            )
            body_path.write_bytes(response.content)
            entry = {
                "resource": resource,
                "spid": spid,
                "grade": grade,
                "path": body_path.name,
                "observed_at": observed.isoformat(),
                "status_code": status,
                "sha256": hashlib.sha256(response.content).hexdigest(),
            }
            self.state.setdefault("responses", []).append(entry)
            if failure is None:
                self.responses[self.active] = entry
        except requests.Timeout:
            failure = "timeout"
            raise
        except requests.ConnectionError:
            failure = "connection_error"
            raise
        except requests.RequestException:
            failure = "request_error"
            raise
        except (KeyboardInterrupt, SystemExit):
            failure = "interrupted"
            raise
        finally:
            elapsed = max(0, int((time.monotonic() - started) * 1000))
            stopped = failure in {
                "blocked",
                "slow_response",
                "timeout",
                "connection_error",
                "unexpected_response",
            }
            log = CrawlLog(
                batch_id=self.run_id,
                source="nexon_api" if resource == "metadata" else "nexon_web",
                observed_at=observed,
                delete_after=None,
                spid=spid,
                resource=resource,
                reinforce_level=grade,
                outcome="stopped" if stopped else "failed" if failure else "ok",
                attempt=attempt,
                duration_ms=elapsed,
                http_status=response.status_code if response is not None else None,
                failure_kind=failure,
            )
            self.logs.append(log)
            with (self.processed_path / "crawl_log.jsonl").open(
                "a", encoding="utf-8"
            ) as target:
                target.write(log.model_dump_json() + "\n")
            self.flush()
            print(
                json.dumps(
                    {
                        "attempts": attempt,
                        "resource": resource,
                        "spid": spid,
                        "grade": grade,
                        "failure": failure,
                    }
                ),
                flush=True,
            )
        return response

    def fetch(self, resource, spid=None, grade=None):
        self.active = (resource, spid, grade)
        if resource == "metadata":
            url, method, data = METADATA_URL, "GET", None
        elif resource == "ability":
            url, method, data = ABILITY_URL, "POST", ability_form(spid, grade)
        else:
            url, method, data = f"{DETAIL_URL}?spid={spid}", "GET", None
        result = fetch_url(
            (url, spid, None),
            policy=RequestPolicy(),
            method=method,
            data=data,
            transport=self.transport,
        )
        if result[0] is None:
            failure = result[1]
            self.failures.append(failure)
            if failure["kind"] != "not_found":
                raise PilotStopped("HTTP 오류로 파일럿을 중단합니다.")
            return None
        response = result[0]
        try:
            return response.content
        finally:
            response.close()

    def ability(self, spid, grade):
        html = self.fetch("ability", spid, grade)
        if html is None:
            return None
        entry = self.responses[("ability", spid, grade)]
        context = ParseContext(
            batch_id=self.run_id,
            source="nexon_web",
            observed_at=entry["observed_at"],
            delete_after=None,
            spid=spid,
            reinforce_level=grade,
            version=entry["sha256"],
        )
        self.parse_attempts += 1
        try:
            return parse_ability(html, context)
        except ParseError:
            self.parse_failures += 1
            raise

    def save_record(self, collection, record):
        with (self.processed_path / f"{collection}.jsonl").open(
            "a", encoding="utf-8"
        ) as target:
            target.write(record.model_dump_json() + "\n")
        self.records[collection].append(record)

    def execute(self):
        try:
            metadata_body = self.fetch("metadata")
            if metadata_body is None:
                raise PilotStopped("선수 메타데이터가 없습니다.")
            candidates = screening_ids(json.loads(metadata_body))
            for spid in (*REGRESSION_IDS, *candidates):
                parsed = self.ability(spid, 5)
                if parsed is not None:
                    self.cards[spid] = parsed
            if not all(spid in self.cards for spid in REGRESSION_IDS):
                raise PilotStopped("회귀 샘플 응답이 없습니다.")
            selected = stratified_ids(
                [self.cards[spid].card for spid in candidates if spid in self.cards]
            )
            self.state["selected_ids"] = selected
            self.state["screening_ids"] = candidates
            self.flush()
            for spid in (*REGRESSION_IDS, *selected):
                base = self.cards[spid]
                self.save_record("player_card", base.card)
                for grade in LEVELS:
                    parsed = base if grade == 5 else self.ability(spid, grade)
                    if parsed is None:
                        raise PilotStopped("표본의 강화 응답이 없습니다.")
                    self.save_record("card_salary", parsed.salary)
                    self.save_record(
                        "reinforce_stats", reinforcement_delta(base.card, parsed.card)
                    )
                detail = self.fetch("detail", spid)
                if detail is None:
                    raise PilotStopped("표본의 클럽 이력 응답이 없습니다.")
                self.parse_attempts += 1
                try:
                    clubs = parse_club_history(detail)
                except ParseError:
                    self.parse_failures += 1
                    raise
                detail_entry = self.responses[("detail", spid, None)]
                for color in unverified_colors(base, clubs):
                    if color.type == "club":
                        color = type(color).model_validate(
                            {
                                **color.model_dump(),
                                "observed_at": detail_entry["observed_at"],
                                "version": detail_entry["sha256"],
                            }
                        )
                    self.save_record("team_color", color)
            self.state["status"] = "completed"
        except CollectionStopped as error:
            self.state.update(status="stopped", stop_reason=error.failure)
        except (ParseError, PilotStopped, ValueError) as error:
            self.state.update(
                status="stopped",
                stop_reason={
                    "kind": "parsing" if isinstance(error, ParseError) else "pilot",
                    "message": str(error),
                    "active": self.active,
                },
            )
        finally:
            self.state["finished_at"] = now().isoformat()
            self.flush()
            report = self.report()
            write_json(self.processed_path / "report.json", report)
        return report

    def report(self):
        sample = [self.cards[spid].card for spid in self.state.get("selected_ids", [])]
        return {
            "pilot_id": self.run_id,
            "status": self.state["status"],
            "attempts": self.state["attempts"],
            "stop_reason": self.state.get("stop_reason"),
            "http_failure_attempts": sum(log.outcome != "ok" for log in self.logs),
            "http_failure_rate": sum(log.outcome != "ok" for log in self.logs)
            / max(1, len(self.logs)),
            "parse_attempts": self.parse_attempts,
            "parse_failures": self.parse_failures,
            "parse_failure_rate": self.parse_failures / max(1, self.parse_attempts),
            "screened_success": len(self.cards),
            "selected_sample": len(sample),
            "season_counts": dict(Counter(str(card.season_id) for card in sample)),
            "position_counts": dict(Counter(card.positions[0] for card in sample)),
            "position_role_counts": dict(
                Counter(position_role(card.positions[0]) for card in sample)
            ),
            "collection_counts": {
                key: len(values) for key, values in self.records.items()
            },
            "regression_values": [
                {
                    "spid": spid,
                    "name": self.cards[spid].card.name,
                    "positions": self.cards[spid].card.positions,
                    "salary": self.cards[spid].salary.salary,
                    "stats": self.cards[spid].card.stats,
                    "ovr": self.cards[spid].card.position_ovr,
                }
                for spid in REGRESSION_IDS
                if spid in self.cards
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="승인된 1회 파일럿 실행")
    options = parser.parse_args()
    if not options.run:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return 0
    with requests.Session() as session:
        # 환경 프록시·netrc 인증을 사용하지 않고 HTTP 클라이언트 기본 UA를 유지한다.
        session.trust_env = False
        pilot = Pilot(
            ROOT / "data/raw" / PILOT_ID, ROOT / "data/processed" / PILOT_ID, session
        )
        report = pilot.execute()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
