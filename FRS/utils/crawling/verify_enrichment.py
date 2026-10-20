"""상세 보강의 원본·로그·강화 차이값·가공 해시를 외부 요청 없이 대조한다."""

import argparse
import hashlib
import json
from collections import Counter

from FRS.schema.models import CrawlLog
from FRS.schema.scope import SUPPORTED_REINFORCEMENTS
from FRS.utils.crawling.ability_parser import (
    ParseContext,
    keyword_map,
    parse_ability,
    parse_club_history,
    reinforcement_delta,
    unverified_colors,
)
from FRS.utils.crawling.enrich_cards import (
    MAX_ATTEMPTS,
    MAX_CARDS,
    RUN_ID,
    SOURCE_ID,
    CardEnrichment,
    source_selection,
)
from FRS.utils.crawling.pilot import ABILITY_URL, DETAIL_URL, ROOT, ability_form
from FRS.utils.crawling.verify_pilot import displayed_stats, read_jsonl, require


def verify(raw_path, processed_path, *, expected_selection):
    state = json.loads((raw_path / "manifest.json").read_text(encoding="utf-8"))
    require(
        state["pilot_id"] == RUN_ID and state["status"] == "completed",
        "실행 미완료/불일치",
    )
    require(state["selection"] == expected_selection, "선별 근거 변경")
    ids = state["selected_ids"]
    require(
        ids == expected_selection["selected_ids"] and 0 < len(ids) <= MAX_CARDS,
        "선별 범위 변경",
    )
    require(
        len(ids) == len(set(ids)) and set(ids) == set(state["completed_ids"]),
        "선별/완료 ID 누락·중복",
    )
    require(len(state["completed_ids"]) == len(ids), "완료 ID 중복")
    require(
        state["max_attempts_including_retries"] == MAX_ATTEMPTS
        and state["retries"] == 0,
        "요청 정책 변경",
    )
    require(state["attempts"] == len(ids) * 8 <= MAX_ATTEMPTS, "요청 수/상한 변경")
    logs = read_jsonl(processed_path / "crawl_log.jsonl", CrawlLog)
    require(
        [log.attempt for log in logs] == list(range(1, state["attempts"] + 1)),
        "요청 로그 누락·중복",
    )
    require(
        all(
            log.outcome == "ok"
            and log.http_status == 200
            and log.batch_id == RUN_ID
            and log.source == "nexon_web"
            and log.delete_after is None
            for log in logs
        ),
        "요청 실패/정책 불일치",
    )
    gaps = [
        (b.observed_at - a.observed_at).total_seconds() for a, b in zip(logs, logs[1:])
    ]
    require(all(gap >= 0.999 for gap in gaps), "요청 간격 미달")
    bodies = {}
    require(len(state["responses"]) == len(logs), "원본 응답 누락")
    for entry, log in zip(state["responses"], logs, strict=True):
        path = (raw_path / entry["path"]).resolve()
        require(path.parent == raw_path.resolve(), "원본 경로 이탈")
        body = path.read_bytes()
        require(hashlib.sha256(body).hexdigest() == entry["sha256"], "원본 해시 불일치")
        key = entry["resource"], entry["spid"], entry["grade"]
        require(key not in bodies and entry["status_code"] == 200, "응답 중복/실패")
        require(
            key == (log.resource, log.spid, log.reinforce_level),
            "요청 로그 식별 불일치",
        )
        if entry["resource"] == "ability":
            require(
                entry["url"] == ABILITY_URL
                and entry["method"] == "POST"
                and entry["form"] == ability_form(entry["spid"], entry["grade"]),
                "능력치 요청 provenance 불일치",
            )
        else:
            require(
                entry["resource"] == "detail"
                and entry["url"] == f"{DETAIL_URL}?spid={entry['spid']}"
                and entry["method"] == "GET"
                and entry["form"] is None,
                "상세 요청 provenance 불일치",
            )
        bodies[key] = body, entry
    # 파일을 수정/생성하는 생성자를 실행하지 않고 기존 스키마 검증 메서드만 사용한다.
    reader = object.__new__(CardEnrichment)
    reader.processed_path, reader.state = processed_path, state
    counts = Counter()
    source_numbers = 0
    mapping = keyword_map()
    for spid in ids:
        data = reader.load_card(spid)
        parsed = []
        for grade in SUPPORTED_REINFORCEMENTS:
            body, entry = bodies[("ability", spid, grade)]
            context = ParseContext(
                batch_id=RUN_ID,
                source="nexon_web",
                observed_at=entry["observed_at"],
                delete_after=None,
                spid=spid,
                reinforce_level=grade,
                version=entry["sha256"],
            )
            value = parse_ability(body, context)
            direct = {
                mapping[label]: number
                for label, number in displayed_stats(body).items()
            }
            require(value.card.stats == direct, "34개 능력치 직접 표시 불일치")
            source_numbers += len(direct)
            parsed.append(value)
        base = parsed[0]
        require(data["player_card"] == [base.card], "기본 카드 원본 불일치")
        require(
            data["card_salary"] == [p.salary for p in parsed], "강화별 급여 원본 불일치"
        )
        require(
            data["reinforce_stats"]
            == [reinforcement_delta(base.card, p.card) for p in parsed],
            "강화 차이값/기준 버전 불일치",
        )
        detail, entry = bodies[("detail", spid, None)]
        colors = []
        for color in unverified_colors(base, parse_club_history(detail)):
            if color.type == "club":
                color = type(color).model_validate(
                    {
                        **color.model_dump(),
                        "observed_at": entry["observed_at"],
                        "version": entry["sha256"],
                    }
                )
            colors.append(color)
        require(data["team_color"] == colors, "클럽 경력/국적 원본 불일치")
        counts.update({key: len(values) for key, values in data.items()})
    require(
        {int(p.stem) for p in processed_path.glob("[0-9]*.json")} == set(ids),
        "가공 파일 표본 불일치",
    )
    return dict(
        verified=True,
        cards=len(ids),
        attempts=state["attempts"],
        source_stat_values_compared=source_numbers,
        min_request_start_gap_seconds=min(gaps),
        collection_counts=dict(counts),
        recommendation_ready=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = verify(
        ROOT / "data/raw" / RUN_ID,
        ROOT / "data/processed" / RUN_ID,
        expected_selection=source_selection(ROOT / "data/raw" / SOURCE_ID),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
