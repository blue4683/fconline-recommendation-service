"""승인된 범위에서 수동 실행하는 갱신기. 기본 실행은 오프라인 계획 출력이다."""

import argparse
import hashlib
import json
import re
from collections import Counter

import requests

from FRS.core.freshness import specification
from FRS.schema.scope import SUPPORTED_REINFORCEMENTS
from FRS.utils.crawling.ability_parser import ParseError, reinforcement_delta
from FRS.utils.crawling.collect_cards import collection_lock
from FRS.utils.crawling.enrich_cards import (
    COLOR_IDS,
    RUN_ID,
    SOURCE_ID,
    source_selection,
)
from FRS.utils.crawling.get_response import CollectionStopped, RequestPolicy, fetch_url
from FRS.utils.crawling.pilot import (
    ABILITY_URL,
    ROOT,
    Pilot,
    PilotStopped,
    ability_form,
    now,
    write_json,
)
from FRS.utils.crawling.player_list_parser import PlayerListContext
from FRS.utils.crawling.refresh_observations import (
    CATALOG_URL,
    adopt_ability,
    adopt_catalog,
)
from FRS.utils.crawling.refresh_plan import load_plan

BANDS = ((4, 20), (21, 25), (26, 30), (31, 35), (36, 99))


def validate_run_id(value):
    if (
        not isinstance(value, str)
        or len(value) > 80
        or re.fullmatch(r"stage-1-f-refresh-[a-z0-9]+(?:-[a-z0-9]+)*", value) is None
    ):
        raise PilotStopped("갱신 실행 ID가 유효하지 않습니다.")


def validate_forms(forms):
    keys = set()
    for form in forms:
        try:
            key = (
                form["teamcolorid"],
                int(form["n4SalaryMin"]),
                int(form["n4SalaryMax"]),
            )
        except (KeyError, TypeError, ValueError):
            raise PilotStopped("급여 목록 폼이 유효하지 않습니다.") from None
        if key in keys or any(
            form.get(k) != v
            for k, v in {
                "strTeamColorCategory": "affiliation",
                "n4LeagueId": "0",
                "n4TeamId": "",
                "n4PageNo": "1",
                "n1Strong": "5",
                "n1Grow": "0",
                "n1TeamColor": "0",
                "strSkill1": "sprintspeed",
                "strSkill2": "acceleration",
                "strSkill3": "strength",
                "strSkill4": "stamina",
            }.items()
        ):
            raise PilotStopped("승인 경로와 다른 목록 요청입니다.")
        keys.add(key)
    if keys != {(color, lo, hi) for color in COLOR_IDS for lo, hi in BANDS}:
        raise PilotStopped("세 클럽의 다섯 급여 구간만 요청할 수 있습니다.")


class ManualRefresh(Pilot):
    def execution_plan(self):
        return dict(
            pilot_id=self.run_id,
            jobs=list(self.jobs),
            selected_ids=list(self.targets),
            normal_attempts=self.max_attempts,
            max_attempts_including_retries=self.max_attempts,
            retries=0,
            min_request_interval_seconds=1,
            delete_after=None,
            provider_permission="unconfirmed",
            policy=specification(),
            observation_contract=dict(
                version=1,
                price_type="reference",
                eligibility_basis="official_club_filter",
            ),
            cohort_batch=RUN_ID,
            scope="one_explicit_manual_refresh",
        )

    def __init__(
        self,
        raw_path,
        processed_path,
        session,
        *,
        run_id,
        targets,
        forms,
        jobs,
        max_attempts,
    ):
        validate_run_id(run_id)
        self.targets = tuple(targets)
        self.forms = [dict(f) for f in forms]
        self.jobs = tuple(jobs)
        if (
            not self.targets
            or len(self.targets) != len(set(self.targets))
            or len(self.targets) > 259
            or any(
                type(i) is not int or not 1_000_000 <= i <= 999_999_999
                for i in self.targets
            )
            or not self.jobs
            or len(self.jobs) != len(set(self.jobs))
            or set(self.jobs) - {"catalog", "profiles"}
        ):
            raise PilotStopped("명시한 갱신 대상/작업이 유효하지 않습니다.")
        validate_forms(self.forms)
        expected = (15 if "catalog" in self.jobs else 0) + (
            len(self.targets) * 7 if "profiles" in self.jobs else 0
        )
        if type(max_attempts) is not int or max_attempts != expected:
            raise PilotStopped(
                "요청 상한은 명시한 작업의 정확한 계획 요청 수와 같아야 합니다."
            )
        self.run_id, self.max_attempts = run_id, max_attempts
        self.current_form = None
        self.issued = set()
        self.catalog_seen = set()
        self.price_values = {}
        self.completed = []
        super().__init__(raw_path, processed_path, session)

    def transport(self, method, url, **options):
        self.state["pending"] = dict(
            resource=self.active[0],
            spid=self.active[1],
            grade=self.active[2],
            method=method,
            url=url,
            form=options.get("data"),
        )
        response = super().transport(method, url, **options)
        self.state["responses"][-1].update(
            method=method, url=url, form=options.get("data")
        )
        self.state.pop("pending", None)
        self.flush()
        return response

    def fetch(self, resource, spid=None, grade=None):
        if self.state["status"] != "running":
            raise PilotStopped("종료한 갱신은 요청할 수 없습니다.")
        if (
            resource == "catalog"
            and "catalog" in self.jobs
            and self.current_form in self.forms
            and spid is None
            and grade is None
        ):
            url, form = CATALOG_URL, self.current_form
        elif (
            resource == "ability"
            and "profiles" in self.jobs
            and spid in self.targets
            and grade in SUPPORTED_REINFORCEMENTS
        ):
            url, form = ABILITY_URL, ability_form(spid, grade)
        else:
            raise PilotStopped("명시한 작업/카드/강화 밖의 요청입니다.")
        key = resource, spid, grade, json.dumps(form, sort_keys=True)
        if key in self.issued:
            raise PilotStopped("이미 시도한 요청은 재시도하지 않습니다.")
        self.issued.add(key)
        self.active = (
            "player_list" if resource == "catalog" else resource,
            spid,
            grade,
        )
        result = fetch_url(
            (url, spid, None),
            policy=RequestPolicy(max_retries=0),
            method="POST",
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

    def collect_catalog(self):
        for form in self.forms:
            self.current_form = form
            body = self.fetch("catalog")
            entry = self.state["responses"][-1]
            context = PlayerListContext(
                batch_id=self.run_id,
                source="nexon_web",
                observed_at=entry["observed_at"],
                delete_after=None,
                version=entry["sha256"],
                reinforce_level=5,
                skill_fields=tuple(form[f"strSkill{i}"] for i in range(1, 5)),
            )
            self.parse_attempts += 1
            try:
                catalog = adopt_catalog(body, context, entry, at=now())
            except ParseError:
                self.parse_failures += 1
                raise
            for key, records in (
                ("card_price", catalog.prices),
                ("team_color", catalog.memberships),
            ):
                for record in records:
                    if record.spid in self.targets:
                        if key == "card_price":
                            price_key = (record.spid, record.reinforce_level)
                            if (
                                price_key in self.price_values
                                and self.price_values[price_key] != record.price
                            ):
                                raise ParseError("동일 가격 키의 관측 값이 충돌합니다.")
                            self.price_values[price_key] = record.price
                        self.save_record(key, record)
                        self.catalog_seen.add(record.spid)
        if self.catalog_seen != set(self.targets):
            raise PilotStopped(
                "목록 첫 응답에 관리 대상이 누락됐습니다. 추가 조회 없이 중단합니다."
            )

    def fresh_ability(self, spid, grade):
        parsed = self.ability(spid, grade)
        try:
            return adopt_ability(parsed, at=now())
        except ParseError:
            self.parse_failures += 1
            raise

    def collect_profiles(self):
        for spid in self.targets:
            base = self.fresh_ability(spid, 5)
            self.save_record("player_card", base.card)
            for grade in SUPPORTED_REINFORCEMENTS:
                parsed = base if grade == 5 else self.fresh_ability(spid, grade)
                self.save_record("card_salary", parsed.salary)
                self.save_record(
                    "reinforce_stats", reinforcement_delta(base.card, parsed.card)
                )
            self.completed.append(spid)
            self.state["completed_ids"] = list(self.completed)
            self.flush()

    def execute(self):
        if self.state["status"] != "running":
            raise PilotStopped("종료한 갱신은 재실행할 수 없습니다.")
        try:
            if "catalog" in self.jobs:
                self.collect_catalog()
            if "profiles" in self.jobs:
                self.collect_profiles()
            self.state["status"] = "completed"
        except KeyboardInterrupt:
            self.state.update(
                status="interrupted", stop_reason={"kind": "process_interrupted"}
            )
        except Exception as error:
            if isinstance(error, ParseError):
                self.parse_failures = max(1, self.parse_failures)
            self.state.update(
                status="stopped",
                stop_reason=getattr(
                    error,
                    "failure",
                    dict(
                        kind="parsing" if isinstance(error, ParseError) else "refresh",
                        message=str(error),
                    ),
                ),
            )
        finally:
            self.state.update(
                parse_attempts=self.parse_attempts,
                parse_failures=self.parse_failures,
                finished_at=now().isoformat(),
                processed_hashes={
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.processed_path.glob("*.jsonl")
                },
            )
            self.flush()
        records = [r for values in self.records.values() for r in values]
        price_keys = Counter(
            (r.spid, r.reinforce_level) for r in self.records.get("card_price", [])
        )
        result = dict(
            run_id=self.run_id,
            price_keys_with_multiple_observations=sum(
                n > 1 for n in price_keys.values()
            ),
            status=self.state["status"],
            attempts=self.state["attempts"],
            stop_reason=self.state.get("stop_reason"),
            completed_profile_cards=len(self.completed),
            catalog_cards=len(self.catalog_seen),
            parse_attempts=self.parse_attempts,
            parse_failures=self.parse_failures,
            http_failure_attempts=sum(log.outcome != "ok" for log in self.logs),
            collection_counts={k: len(v) for k, v in self.records.items()},
            status_counts=dict(Counter(str(r.status) for r in records)),
            auto_activation=False,
        )
        write_json(self.processed_path / "report.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", help="별도 승인된 범위에서만 새 갱신 실행"
    )
    parser.add_argument("--jobs", nargs="+", choices=["catalog", "profiles"])
    parser.add_argument("--run-id")
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--profile-batch-id", default=RUN_ID)
    parser.add_argument("--catalog-batch-id", default=SOURCE_ID)
    args = parser.parse_args()
    if not args.run:
        print(
            json.dumps(
                load_plan(
                    at=now(),
                    profile_batch=args.profile_batch_id,
                    catalog_batch=args.catalog_batch_id,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.jobs or not args.run_id or args.max_attempts is None:
        parser.error("--run은 --jobs, --run-id, --max-attempts를 모두 명시해야 합니다.")
    try:
        validate_run_id(args.run_id)
    except PilotStopped as error:
        parser.error(str(error))
    # 명시한 기존 완료 배치만 검증한다. 자동 최신 선택·승인 범위 확대는 하지 않는다.
    baseline_plan = load_plan(
        at=now(),
        profile_batch=args.profile_batch_id,
        catalog_batch=args.catalog_batch_id,
    )
    selection = source_selection(ROOT / "data/raw" / SOURCE_ID)
    if len(selection["selected_ids"]) != 259:
        parser.error("첫 관리 대상 259개가 아닙니다.")
    source = json.loads(
        (ROOT / "data/raw" / SOURCE_ID / "manifest.json").read_text(encoding="utf-8")
    )
    forms = [e["form"] for e in source["responses"] if e["method"] == "POST"]
    with (
        collection_lock(ROOT / "data/raw" / f"{args.run_id}.lock"),
        requests.Session() as session,
    ):
        session.trust_env = False
        run = ManualRefresh(
            ROOT / "data/raw" / args.run_id,
            ROOT / "data/processed" / args.run_id,
            session,
            run_id=args.run_id,
            targets=selection["selected_ids"],
            forms=forms,
            jobs=args.jobs,
            max_attempts=args.max_attempts,
        )
        run.state["input_manifest_hashes"] = baseline_plan["input_manifest_hashes"]
        run.flush()
        report = run.execute()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
