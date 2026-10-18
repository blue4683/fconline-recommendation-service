"""승인된 Stage 1-C 축소 수집. 같은 실행의 체크포인트와 요청 상한을 유지한다."""

import argparse
import fcntl
import hashlib
import json
from collections import Counter, defaultdict
from contextlib import contextmanager

import requests

from FRS.schema.models import DOCUMENT_MODELS, CrawlLog
from FRS.utils.crawling.ability_parser import (
    ParseError,
    parse_club_history,
    unverified_colors,
)
from FRS.utils.crawling.get_response import CollectionStopped
from FRS.utils.crawling.pilot import (
    PILOT_ID,
    ROOT,
    Pilot,
    PilotStopped,
    _round_robin,
    now,
    position_role,
    write_json,
)

COLLECTION_ID = "stage-1-c-20261018"
SAMPLE_SIZE = 1000
MAX_ATTEMPTS = 2100


@contextmanager
def collection_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PilotStopped(
                "같은 실행의 수집 프로세스가 이미 동작 중입니다."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def plan():
    return {
        "pilot_id": COLLECTION_ID,
        "additional_cards": SAMPLE_SIZE,
        "reinforcement_level": 5,
        "normal_attempts": 1 + SAMPLE_SIZE * 2,
        "max_attempts_including_retries": MAX_ATTEMPTS,
        "min_request_interval_seconds": 1,
        "raw_path": str(ROOT / "data/raw" / COLLECTION_ID),
        "processed_path": str(ROOT / "data/processed" / COLLECTION_ID),
        "delete_after": None,
        "provider_permission": "unconfirmed",
        "scope": "one_stage_1_c_collection_only",
    }


def select_ids(metadata, excluded, count=SAMPLE_SIZE):
    if not isinstance(metadata, list):
        raise PilotStopped("메타데이터 형식이 변경되었습니다.")
    groups = defaultdict(set)
    seen = set()
    for item in metadata:
        spid = item.get("id") if isinstance(item, dict) else None
        if (
            type(spid) is not int
            or not 1_000_000 <= spid <= 999_999_999
            or spid in seen
        ):
            raise PilotStopped("메타데이터 선수 ID가 유효하지 않습니다.")
        seen.add(spid)
        if spid not in excluded:
            groups[spid // 1_000_000].add(spid)
    return _round_robin(groups, count)


def previous_ids(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest["pilot_id"] != PILOT_ID or manifest["status"] != "completed":
        raise PilotStopped("완료한 Stage 1-B 파일럿이 필요합니다.")
    return sorted(
        {entry["spid"] for entry in manifest["responses"] if entry["spid"] is not None}
    )


class CardCollection(Pilot):
    run_id = COLLECTION_ID
    max_attempts = MAX_ATTEMPTS
    sample_size = SAMPLE_SIZE

    def execution_plan(self):
        return plan()

    def __init__(self, raw_path, processed_path, session, *, excluded, resume=False):
        self.excluded = sorted(set(excluded))
        if not resume:
            super().__init__(raw_path, processed_path, session)
            self.state.update(
                excluded_ids=self.excluded, completed_ids=[], completed_hashes={}
            )
            self.flush()
            return
        self.raw_path, self.processed_path, self.session = (
            raw_path,
            processed_path,
            session,
        )
        self.state = json.loads(
            (raw_path / "manifest.json").read_text(encoding="utf-8")
        )
        if self.state["pilot_id"] != self.run_id or self.state["status"] not in {
            "running",
            "interrupted",
        }:
            raise PilotStopped("완료·차단·이상 중단 작업은 재개할 수 없습니다.")
        if (
            self.state["max_attempts_including_retries"] != self.max_attempts
            or self.state["excluded_ids"] != self.excluded
        ):
            raise PilotStopped("재개 시 승인 상한·제외 목록을 변경할 수 없습니다.")
        self.active = None
        self.responses, self.cards, self.records = {}, {}, defaultdict(list)
        self.failures = []
        self.parse_attempts = self.state.get("parse_attempts", 0)
        self.parse_failures = self.state.get("parse_failures", 0)
        log_path = processed_path / "crawl_log.jsonl"
        self.logs = []
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                try:
                    self.logs.append(CrawlLog.model_validate_json(line))
                except ValueError:
                    # 마지막 쓰기가 끊긴 경우도 아래 연속성·예약 시도로 검증한다.
                    raise PilotStopped(
                        "요청 로그가 손상되어 안전하게 재개할 수 없습니다."
                    ) from None
        if [log.attempt for log in self.logs] != list(range(1, len(self.logs) + 1)):
            raise PilotStopped("요청 로그 순서가 변경되었습니다.")
        difference = self.state["attempts"] - len(self.logs)
        if difference not in {0, 1}:
            raise PilotStopped("누적 시도와 로그 수가 일치하지 않습니다.")
        if difference:
            pending = self.state.get("pending")
            if pending is None:
                raise PilotStopped("미완료 요청의 예약 정보가 없습니다.")
            log = CrawlLog(
                batch_id=self.run_id,
                source="nexon_api"
                if pending["resource"] == "metadata"
                else "nexon_web",
                observed_at=pending["observed_at"],
                delete_after=None,
                spid=pending["spid"],
                resource=pending["resource"],
                reinforce_level=pending["grade"],
                attempt=self.state["attempts"],
                outcome="failed",
                failure_kind="interrupted",
                http_status=None,
                duration_ms=None,
            )
            self.logs.append(log)
            with log_path.open("a", encoding="utf-8") as target:
                target.write(log.model_dump_json() + "\n")
        for entry in self.state.get("responses", []):
            body = (raw_path / entry["path"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise PilotStopped("기존 원본 해시가 변경되어 재개하지 않습니다.")
            if entry["status_code"] == 200:
                self.responses[(entry["resource"], entry["spid"], entry["grade"])] = (
                    entry
                )
        if any(log.outcome == "stopped" for log in self.logs):
            raise PilotStopped("차단·이상 중단 로그가 있어 재개하지 않습니다.")
        self.state.update(status="running", resumed_at=now().isoformat())
        self.state.pop("stop_reason", None)
        self.flush()

    def transport(self, method, url, **options):
        resource, spid, grade = self.active
        self.state["pending"] = {
            "resource": resource,
            "spid": spid,
            "grade": grade,
            "observed_at": now().isoformat(),
        }
        return super().transport(method, url, **options)

    def fetch(self, resource, spid=None, grade=None):
        entry = self.responses.get((resource, spid, grade))
        if entry is not None:
            body = (self.raw_path / entry["path"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise PilotStopped("기존 응답 해시가 변경되었습니다.")
            return body
        return super().fetch(resource, spid, grade)

    def execute(self):
        try:
            metadata = self.fetch("metadata")
            if metadata is None:
                raise PilotStopped("메타데이터가 없습니다.")
            selected = select_ids(json.loads(metadata), self.excluded, self.sample_size)
            if self.state.get("selected_ids", selected) != selected:
                raise PilotStopped("체크포인트의 표본이 변경되었습니다.")
            self.state["selected_ids"] = selected
            self.flush()
            for spid in selected:
                if spid in self.state["completed_ids"]:
                    self.load_card(spid)
                    continue
                base = self.ability(spid, 5)
                if base is None:
                    raise PilotStopped("선택한 선수의 능력치가 없습니다.")
                detail = self.fetch("detail", spid)
                if detail is None:
                    raise PilotStopped("선택한 선수의 상세 응답이 없습니다.")
                self.parse_attempts += 1
                try:
                    clubs = parse_club_history(detail)
                except ParseError:
                    self.parse_failures += 1
                    raise
                entry = self.responses[("detail", spid, None)]
                colors = []
                for color in unverified_colors(base, clubs):
                    if color.type == "club":
                        color = type(color).model_validate(
                            {
                                **color.model_dump(),
                                "observed_at": entry["observed_at"],
                                "version": entry["sha256"],
                            }
                        )
                    colors.append(color)
                output = {
                    "player_card": [base.card.model_dump(mode="json")],
                    "card_salary": [base.salary.model_dump(mode="json")],
                    "team_color": [color.model_dump(mode="json") for color in colors],
                }
                write_json(self.processed_path / f"{spid}.json", output)
                self.state["completed_hashes"][str(spid)] = hashlib.sha256(
                    (self.processed_path / f"{spid}.json").read_bytes()
                ).hexdigest()
                self.state["completed_ids"].append(spid)
                self.state.update(
                    parse_attempts=self.parse_attempts,
                    parse_failures=self.parse_failures,
                )
                self.flush()
            self.state["status"] = "completed"
        except CollectionStopped as error:
            self.state.update(status="stopped", stop_reason=error.failure)
        except KeyboardInterrupt:
            self.state.update(
                status="interrupted", stop_reason={"kind": "process_interrupted"}
            )
        except (ParseError, PilotStopped, ValueError) as error:
            self.state.update(
                status="stopped",
                stop_reason={
                    "kind": "parsing"
                    if isinstance(error, ParseError)
                    else "collection",
                    "message": str(error),
                    "active": self.active,
                },
            )
        finally:
            self.state.update(
                parse_attempts=self.parse_attempts,
                parse_failures=self.parse_failures,
                updated_at=now().isoformat(),
            )
            self.flush()
        report = self.report()
        write_json(self.processed_path / "report.json", report)
        return report

    def load_card(self, spid):
        digest = hashlib.sha256(
            (self.processed_path / f"{spid}.json").read_bytes()
        ).hexdigest()
        if self.state["completed_hashes"].get(str(spid)) != digest:
            raise PilotStopped("가공 문서 해시가 변경되었습니다.")
        output = json.loads(
            (self.processed_path / f"{spid}.json").read_text(encoding="utf-8")
        )
        if set(output) != {"player_card", "card_salary", "team_color"}:
            raise PilotStopped("저장 문서 목록이 변경되었습니다.")
        records = {
            key: [DOCUMENT_MODELS[key].model_validate(value) for value in values]
            for key, values in output.items()
        }
        if len(records["player_card"]) != 1 or len(records["card_salary"]) != 1:
            raise PilotStopped("선수별 기본 카드·급여 개수가 잘못되었습니다.")
        if any(
            record.spid != spid or record.batch_id != self.run_id
            for values in records.values()
            for record in values
        ):
            raise PilotStopped("저장 문서의 카드·배치가 다릅니다.")
        return records

    def report(self):
        records = defaultdict(list)
        for spid in self.state["completed_ids"]:
            for key, values in self.load_card(spid).items():
                records[key].extend(values)
        cards = records["player_card"]
        return {
            "collection_id": self.run_id,
            "status": self.state["status"],
            "attempts": self.state["attempts"],
            "stop_reason": self.state.get("stop_reason"),
            "selected_cards": len(self.state.get("selected_ids", [])),
            "completed_cards": len(cards),
            "http_failure_attempts": sum(log.outcome != "ok" for log in self.logs),
            "http_failure_rate": sum(log.outcome != "ok" for log in self.logs)
            / max(1, len(self.logs)),
            "parse_attempts": self.parse_attempts,
            "parse_failures": self.parse_failures,
            "parse_failure_rate": self.parse_failures / max(1, self.parse_attempts),
            "season_counts": dict(Counter(str(card.season_id) for card in cards)),
            "position_counts": dict(Counter(card.positions[0] for card in cards)),
            "position_role_counts": dict(
                Counter(position_role(card.positions[0]) for card in cards)
            ),
            "collection_counts": {key: len(values) for key, values in records.items()},
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="승인된 추가 1,000명 수집")
    mode.add_argument(
        "--resume",
        action="store_true",
        help="같은 실행의 프로세스 중단 체크포인트 재개",
    )
    options = parser.parse_args()
    if not options.run and not options.resume:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return 0
    excluded = previous_ids(ROOT / "data/raw" / PILOT_ID / "manifest.json")
    with (
        collection_lock(ROOT / "data/raw" / f"{COLLECTION_ID}.lock"),
        requests.Session() as session,
    ):
        session.trust_env = False
        collection = CardCollection(
            ROOT / "data/raw" / COLLECTION_ID,
            ROOT / "data/processed" / COLLECTION_ID,
            session,
            excluded=excluded,
            resume=options.resume,
        )
        report = collection.execute()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
