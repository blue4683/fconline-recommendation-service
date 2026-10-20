"""새 수동 갱신 배치의 원본·요청·정규화·저장 해시를 오프라인으로 대조한다."""

import hashlib
import json
from collections import defaultdict
from datetime import datetime

from FRS.core.freshness import specification
from FRS.schema.models import DOCUMENT_MODELS, CrawlLog
from FRS.utils.crawling.ability_parser import (
    ParseContext,
    parse_ability,
    reinforcement_delta,
)
from FRS.utils.crawling.pilot import ABILITY_URL, ability_form
from FRS.utils.crawling.player_list_parser import PlayerListContext
from FRS.utils.crawling.refresh_observations import (
    adopt_ability,
    adopt_catalog,
)
from FRS.utils.crawling.verify_pilot import read_jsonl, require


def verify(raw_path, processed_path, *, expected_targets):
    # 지연 import로 실행기와 계획 명령 간 순환 초기화를 피한다.
    from FRS.utils.crawling.refresh_run import validate_forms, validate_run_id

    state = json.loads((raw_path / "manifest.json").read_text(encoding="utf-8"))
    validate_run_id(state["pilot_id"])
    require(state["status"] == "completed", "미완료 갱신 배치")
    require(state["selected_ids"] == list(expected_targets), "관리 대상 변경")
    require(state["policy"] == specification(), "정책 버전/해시 불일치")
    require(
        state.get("observation_contract")
        == dict(
            version=1, price_type="reference", eligibility_basis="official_club_filter"
        ),
        "관측 의미 계약 불일치",
    )
    targets = set(expected_targets)
    jobs = state["jobs"]
    require(
        set(jobs) <= {"catalog", "profiles"} and len(jobs) == len(set(jobs)) > 0,
        "작업 범위 변경",
    )
    total = (15 if "catalog" in jobs else 0) + (
        len(targets) * 7 if "profiles" in jobs else 0
    )
    require(
        state["attempts"]
        == state["normal_attempts"]
        == state["max_attempts_including_retries"]
        == total
        and state["retries"] == 0,
        "요청 수/상한 변경",
    )
    require(
        set(state["processed_hashes"])
        == {p.name for p in processed_path.glob("*.jsonl")},
        "가공 파일 목록 변경",
    )
    for name, digest in state["processed_hashes"].items():
        path = (processed_path / name).resolve()
        require(path.parent == processed_path.resolve(), "가공 경로 이탈")
        require(
            hashlib.sha256(path.read_bytes()).hexdigest() == digest, "가공 해시 불일치"
        )
    logs = read_jsonl(processed_path / "crawl_log.jsonl", CrawlLog)
    require(
        [r.attempt for r in logs] == list(range(1, total + 1)), "요청 로그 누락/중복"
    )
    require(
        len(state["responses"]) == total
        and all(
            r.outcome == "ok"
            and r.batch_id == state["pilot_id"]
            and r.source == "nexon_web"
            for r in logs
        ),
        "로그/응답 불일치",
    )
    gaps = [
        (b.observed_at - a.observed_at).total_seconds() for a, b in zip(logs, logs[1:])
    ]
    require(all(g >= 0.999 for g in gaps), "요청 간격 미달")
    expected = defaultdict(list)
    bases = {}
    forms = []
    seen_requests = set()
    started = datetime.fromisoformat(state["started_at"])
    finished = datetime.fromisoformat(state["finished_at"])
    for entry, log in zip(state["responses"], logs, strict=True):
        path = (raw_path / entry["path"]).resolve()
        require(path.parent == raw_path.resolve(), "원본 경로 이탈")
        body = path.read_bytes()
        require(
            hashlib.sha256(body).hexdigest() == entry["sha256"]
            and entry["status_code"] == 200,
            "원본 해시/상태 불일치",
        )
        observed = datetime.fromisoformat(entry["observed_at"])
        require(started <= observed <= finished, "새 갱신 실행 밖의 관측 시각")
        require(
            (entry["resource"], entry["spid"], entry["grade"])
            == (log.resource, log.spid, log.reinforce_level),
            "로그 식별 불일치",
        )
        require(
            ParseContext.normalize_time(observed) == log.observed_at,
            "로그 관측 시각 불일치",
        )
        request_key = entry["resource"], json.dumps(entry.get("form"), sort_keys=True)
        require(request_key not in seen_requests, "요청 재시도/중복")
        seen_requests.add(request_key)
        common = dict(
            batch_id=state["pilot_id"],
            source="nexon_web",
            observed_at=entry["observed_at"],
            delete_after=None,
            version=entry["sha256"],
        )
        if entry["resource"] == "player_list":
            require(
                "catalog" in jobs and entry["spid"] is None and entry["grade"] is None,
                "목록 작업 범위 변경",
            )
            forms.append(entry["form"])
            ctx = PlayerListContext(
                **common,
                reinforce_level=5,
                skill_fields=tuple(entry["form"][f"strSkill{i}"] for i in range(1, 5)),
            )
            adopted = adopt_catalog(body, ctx, entry, at=observed)
            expected["card_price"].extend(
                p for p in adopted.prices if p.spid in targets
            )
            expected["team_color"].extend(
                c for c in adopted.memberships if c.spid in targets
            )
        else:
            spid, grade = entry["spid"], entry["grade"]
            require(
                "profiles" in jobs
                and entry["resource"] == "ability"
                and spid in targets
                and grade in range(5, 12),
                "능력치 작업 범위 변경",
            )
            require(
                entry["url"] == ABILITY_URL
                and entry["method"] == "POST"
                and entry["form"] == ability_form(spid, grade),
                "능력치 요청 provenance 불일치",
            )
            parsed = adopt_ability(
                parse_ability(
                    body, ParseContext(**common, spid=spid, reinforce_level=grade)
                ),
                at=observed,
            )
            if grade == 5:
                bases[spid] = parsed.card
                expected["player_card"].append(parsed.card)
            require(spid in bases, "기준 카드 없는 강화")
            expected["card_salary"].append(parsed.salary)
            expected["reinforce_stats"].append(
                reinforcement_delta(bases[spid], parsed.card)
            )
    if "catalog" in jobs:
        validate_forms(forms)
        require(
            {p.spid for p in expected["card_price"]} == targets, "목록 관리 대상 누락"
        )
        values = {}
        for price in expected["card_price"]:
            key = price.spid, price.reinforce_level
            require(
                key not in values or values[key] == price.price, "가격 관측 값 충돌"
            )
            values[key] = price.price
        if not expected["team_color"]:
            raise ValueError("공식 자격 관측 누락")
    if "profiles" in jobs:
        require(
            set(bases) == targets and len(expected["card_salary"]) == len(targets) * 7,
            "능력치 관리 대상/강화 누락",
        )
    actual_files = {p.stem for p in processed_path.glob("*.jsonl")} - {"crawl_log"}
    require(actual_files == set(expected), "산출물 종류 불일치")
    for key, values in expected.items():
        actual = read_jsonl(processed_path / f"{key}.jsonl", DOCUMENT_MODELS[key])
        require(actual == values, "정규화/원본/강화 기준 불일치")
    return dict(expected)
