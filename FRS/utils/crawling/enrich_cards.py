"""저장된 세 클럽 목록에서 층화 선별한 카드의 승인된 1회 상세 보강."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import requests

from FRS.diagnostics.coverage import MINIMUM_CARDS, salary_band
from FRS.diagnostics.player_list_report import saved_rows
from FRS.schema.models import DOCUMENT_MODELS
from FRS.schema.scope import PRIORITY_CLUBS, SUPPORTED_REINFORCEMENTS
from FRS.utils.crawling.ability_parser import (
    ParseError,
    parse_club_history,
    reinforcement_delta,
    unverified_colors,
)
from FRS.utils.crawling.collect_cards import CardCollection, collection_lock
from FRS.utils.crawling.get_response import CollectionStopped, RequestPolicy, fetch_url
from FRS.utils.crawling.pilot import (
    ABILITY_URL,
    DETAIL_URL,
    ROOT,
    PilotStopped,
    ability_form,
    now,
    write_json,
)

RUN_ID = "stage-1-d-enrichment-20261019"
SOURCE_ID = "stage-1-d-salary-filter-20261019"
MAX_CARDS = 260
MAX_ATTEMPTS = 2100
COLOR_IDS = dict(zip(("1001", "1003", "1010"), PRIORITY_CLUBS, strict=True))
COLLECTIONS = {"player_card", "card_salary", "reinforce_stats", "team_color"}


def select_candidates(results, limit=MAX_CARDS):
    """표시 구간의 부족한 칸을 많이 보강하는 순서. 동률은 spid 오름차순이다."""
    if type(limit) is not int or not 1 <= limit <= MAX_CARDS:
        raise ValueError("선별 상한이 승인 범위를 벗어납니다.")
    by_card, by_cell = defaultdict(set), defaultdict(set)
    signatures = {}
    queries = set()
    for entry, rows in results:
        form = entry["form"]
        color = form["teamcolorid"]
        bounds = int(form["n4SalaryMin"]), int(form["n4SalaryMax"])
        if (
            color not in COLOR_IDS
            or bounds not in {(4, 20), (21, 25), (26, 30), (31, 35), (36, 99)}
            or form["n1Strong"] != "5"
            or form["n4PageNo"] != "1"
            or form["n4LeagueId"] != "0"
            or form["n4TeamId"] != ""
            or form["strTeamColorCategory"] != "affiliation"
            or (color, bounds) in queries
        ):
            raise PilotStopped("선별 근거의 클럽/급여/강화/페이지 조건이 다릅니다.")
        queries.add((color, bounds))
        for row in rows:
            if not bounds[0] <= row.salary <= bounds[1]:
                raise PilotStopped("선별 원본에 급여 구간 밖의 값이 있습니다.")
            signature = (row.salary, tuple(row.listed_position_ovr.items()))
            if row.spid in signatures and signatures[row.spid] != signature:
                raise PilotStopped("동일 카드의 목록 관측이 충돌합니다.")
            signatures[row.spid] = signature
            for position in row.listed_position_ovr:
                cell = COLOR_IDS[color], salary_band(row.salary), position
                by_card[row.spid].add(cell)
                by_cell[cell].add(row.spid)
    if len(queries) != 15 or not by_card:
        raise PilotStopped("세 클럽의 다섯 급여 구간 근거가 필요합니다.")
    targets = {cell: min(MINIMUM_CARDS, len(ids)) for cell, ids in by_cell.items()}
    selected, remaining, counts = [], set(by_card), Counter()
    while remaining and len(selected) < limit:
        best = min(
            remaining,
            key=lambda spid: (
                -sum(counts[cell] < targets[cell] for cell in by_card[spid]),
                spid,
            ),
        )
        if not any(counts[cell] < targets[cell] for cell in by_card[best]):
            break
        selected.append(best)
        counts.update(by_card[best])
        remaining.remove(best)
    return {
        "selected_ids": selected,
        "candidate_cards": len(by_card),
        "listed_nonempty_cells": len(by_cell),
        "listed_cells_with_at_least_three": sum(
            len(ids) >= 3 for ids in by_cell.values()
        ),
        "remaining_listed_target_gaps": sum(
            max(0, target - counts[cell]) for cell, target in targets.items()
        ),
        "basis": "listed_positions_only_not_verified_eligibility",
        "algorithm": "largest_unfilled_cell_gain_then_ascending_spid",
    }


def source_selection(source_path: Path):
    manifest_path = source_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["run_id"] != SOURCE_ID or manifest["status"] != "completed":
        raise PilotStopped("완료한 승인 급여 진단 근거가 필요합니다.")
    selection = select_candidates(saved_rows(source_path))
    return {
        **selection,
        "source_run_id": SOURCE_ID,
        "source_manifest_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
    }


def plan(selection):
    return {
        "pilot_id": RUN_ID,
        "selected_ids": selection["selected_ids"],
        "selection": selection,
        "max_cards": MAX_CARDS,
        "reinforcement_levels": list(SUPPORTED_REINFORCEMENTS),
        "normal_attempts": len(selection["selected_ids"]) * 8,
        "max_attempts_including_retries": MAX_ATTEMPTS,
        "min_request_interval_seconds": 1,
        "retries": 0,
        "delete_after": None,
        "provider_permission": "unconfirmed",
        "scope": "one_stage_1_d_enrichment_only",
    }


class CardEnrichment(CardCollection):
    run_id = RUN_ID
    max_attempts = MAX_ATTEMPTS

    def execution_plan(self):
        return plan(self.selection)

    def __init__(self, raw_path, processed_path, session, *, selection, resume=False):
        ids = selection["selected_ids"]
        if (
            not ids
            or len(ids) != len(set(ids))
            or len(ids) > MAX_CARDS
            or any(
                type(spid) is not int or not 1_000_000 <= spid <= 999_999_999
                for spid in ids
            )
        ):
            raise PilotStopped("승인 범위를 벗어난 카드 선별입니다.")
        self.selection = selection
        if resume:
            previous = json.loads(
                (raw_path / "manifest.json").read_text(encoding="utf-8")
            )
            if previous.get("selection") != selection or previous.get("retries") != 0:
                raise PilotStopped("재개 시 선별 근거·정책을 변경할 수 없습니다.")
            log_path = processed_path / "crawl_log.jsonl"
            logs = (
                log_path.read_text(encoding="utf-8").splitlines()
                if log_path.exists()
                else []
            )
            if previous["attempts"] != len(logs) or any(
                json.loads(line)["failure_kind"] is not None for line in logs
            ):
                raise PilotStopped("결과 미상/실패 시도가 있어 재요청 없이 중단합니다.")
        super().__init__(raw_path, processed_path, session, excluded=[], resume=resume)

    def transport(self, method, url, **options):
        response = super().transport(method, url, **options)
        self.state["responses"][-1].update(
            method=method, url=url, form=options.get("data")
        )
        self.flush()
        return response

    def fetch(self, resource, spid=None, grade=None):
        if spid not in self.selection["selected_ids"] or (
            (resource == "ability" and grade not in SUPPORTED_REINFORCEMENTS)
            or (resource == "detail" and grade is not None)
            or resource not in {"ability", "detail"}
        ):
            raise PilotStopped("승인된 카드·요청 종류·강화 범위가 아닙니다.")
        self.active = resource, spid, grade
        if self.active in self.responses:
            entry = self.responses[self.active]
            body = (self.raw_path / entry["path"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise PilotStopped("저장 응답 해시가 변경되었습니다.")
            return body
        if any(
            (log.resource, log.spid, log.reinforce_level) == self.active
            for log in self.logs
        ):
            raise PilotStopped("이미 시도한 요청은 재시도하지 않습니다.")
        url, method, form = (
            (ABILITY_URL, "POST", ability_form(spid, grade))
            if resource == "ability"
            else (f"{DETAIL_URL}?spid={spid}", "GET", None)
        )
        result = fetch_url(
            (url, spid, None),
            policy=RequestPolicy(max_retries=0),
            method=method,
            data=form,
            transport=self.transport,
        )
        if result[0] is None:
            raise CollectionStopped(result[1])
        response = result[0]
        try:
            return response.content
        finally:
            response.close()

    def execute(self):
        try:
            for spid in self.selection["selected_ids"]:
                if spid in self.state["completed_ids"]:
                    self.load_card(spid)
                    continue
                base = self.ability(spid, 5)
                salaries, deltas = [], []
                for grade in SUPPORTED_REINFORCEMENTS:
                    parsed = base if grade == 5 else self.ability(spid, grade)
                    salaries.append(parsed.salary)
                    deltas.append(reinforcement_delta(base.card, parsed.card))
                detail = self.fetch("detail", spid)
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
                records = {
                    "player_card": [base.card],
                    "card_salary": salaries,
                    "reinforce_stats": deltas,
                    "team_color": colors,
                }
                target = self.processed_path / f"{spid}.json"
                write_json(
                    target,
                    {
                        key: [r.model_dump(mode="json") for r in values]
                        for key, values in records.items()
                    },
                )
                self.state["completed_hashes"][str(spid)] = hashlib.sha256(
                    target.read_bytes()
                ).hexdigest()
                self.state["completed_ids"].append(spid)
                self.state.update(
                    parse_attempts=self.parse_attempts,
                    parse_failures=self.parse_failures,
                )
                self.flush()
            self.state["status"] = "completed"
        except KeyboardInterrupt:
            self.state.update(
                status="interrupted", stop_reason={"kind": "process_interrupted"}
            )
        except Exception as error:
            self.state.update(
                status="stopped",
                stop_reason=getattr(
                    error,
                    "failure",
                    {
                        "kind": "parsing"
                        if isinstance(error, ParseError)
                        else "collection",
                        "message": str(error),
                        "active": self.active,
                    },
                ),
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
        path = self.processed_path / f"{spid}.json"
        if hashlib.sha256(path.read_bytes()).hexdigest() != self.state[
            "completed_hashes"
        ].get(str(spid)):
            raise PilotStopped("가공 문서 해시가 변경되었습니다.")
        output = json.loads(path.read_text(encoding="utf-8"))
        if set(output) != COLLECTIONS:
            raise PilotStopped("가공 문서 종류가 다릅니다.")
        records = {
            key: [DOCUMENT_MODELS[key].model_validate(r) for r in values]
            for key, values in output.items()
        }
        if len(records["player_card"]) != 1 or any(
            {r.reinforce_level for r in records[key]} != set(SUPPORTED_REINFORCEMENTS)
            or len(records[key]) != 7
            for key in ("card_salary", "reinforce_stats")
        ):
            raise PilotStopped("기본 카드·강화별 문서가 누락/중복되었습니다.")
        if any(
            r.spid != spid
            or r.batch_id != self.run_id
            or r.status != "unknown"
            or r.valid_until is not None
            or r.delete_after is not None
            for values in records.values()
            for r in values
        ):
            raise PilotStopped("카드·배치·신선도·보관 정책이 다릅니다.")
        return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    selection = source_selection(ROOT / "data/raw" / SOURCE_ID)
    if not args.run and not args.resume:
        print(json.dumps(plan(selection), ensure_ascii=False, indent=2))
        return 0
    with (
        collection_lock(ROOT / "data/raw" / f"{RUN_ID}.lock"),
        requests.Session() as session,
    ):
        session.trust_env = False
        collection = CardEnrichment(
            ROOT / "data/raw" / RUN_ID,
            ROOT / "data/processed" / RUN_ID,
            session,
            selection=selection,
            resume=args.resume,
        )
        report = collection.execute()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
