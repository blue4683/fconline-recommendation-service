"""Stage 1-C 저장 결과와 원본을 네트워크 없이 대조한다."""

import argparse
import hashlib
import json
from collections import Counter

from FRS.schema.models import DOCUMENT_MODELS, TeamColor
from FRS.utils.crawling.ability_parser import (
    ParseContext,
    keyword_map,
    parse_ability,
    parse_club_history,
)
from FRS.utils.crawling.collect_cards import (
    COLLECTION_ID,
    MAX_ATTEMPTS,
    SAMPLE_SIZE,
    select_ids,
)
from FRS.utils.crawling.pilot import ROOT
from FRS.utils.crawling.verify_pilot import displayed_stats, read_jsonl, require


def verify(
    raw_path, processed_path, *, expected_count=SAMPLE_SIZE, max_attempts=MAX_ATTEMPTS
):
    state = json.loads((raw_path / "manifest.json").read_text(encoding="utf-8"))
    require(
        state["pilot_id"] == COLLECTION_ID and state["status"] == "completed",
        "미완료 또는 다른 실행의 데이터",
    )
    require(state["max_attempts_including_retries"] == max_attempts, "승인 상한 변경")
    require(0 < state["attempts"] <= max_attempts, "요청 상한 초과")
    selected = state["selected_ids"]
    require(
        len(selected) == len(set(selected)) == expected_count, "선택 표본 수 불일치"
    )
    require(
        set(selected) == set(state["completed_ids"])
        and len(state["completed_ids"]) == expected_count,
        "완료 표본 누락·중복",
    )
    require(not set(selected) & set(state["excluded_ids"]), "파일럿 선수 중복")
    logs = read_jsonl(processed_path / "crawl_log.jsonl", DOCUMENT_MODELS["crawl_log"])
    require(
        [log.attempt for log in logs] == list(range(1, state["attempts"] + 1)),
        "요청 로그 누락·중복",
    )
    require(
        all(log.outcome != "stopped" and log.batch_id == COLLECTION_ID for log in logs),
        "중단 로그·배치 불일치",
    )
    gaps = [
        (b.observed_at - a.observed_at).total_seconds() for a, b in zip(logs, logs[1:])
    ]
    require(all(gap >= 0.999 for gap in gaps), "요청 시작 간격 미달")
    bodies = {}
    for entry in state["responses"]:
        body = (raw_path / entry["path"]).read_bytes()
        require(hashlib.sha256(body).hexdigest() == entry["sha256"], "원본 해시 불일치")
        if entry["status_code"] == 200:
            bodies[(entry["resource"], entry["spid"], entry["grade"])] = (body, entry)
    metadata, _ = bodies[("metadata", None, None)]
    require(
        select_ids(json.loads(metadata), set(state["excluded_ids"]), expected_count)
        == selected,
        "시즌별 표본 재현 불일치",
    )
    counts = Counter()
    for spid in selected:
        path = processed_path / f"{spid}.json"
        require(
            hashlib.sha256(path.read_bytes()).hexdigest()
            == state["completed_hashes"][str(spid)],
            "가공 해시 불일치",
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        require(
            set(payload) == {"player_card", "card_salary", "team_color"},
            "가공 문서 목록 불일치",
        )
        data = {
            key: [DOCUMENT_MODELS[key].model_validate(value) for value in values]
            for key, values in payload.items()
        }
        require(
            len(data["player_card"]) == len(data["card_salary"]) == 1,
            "카드·급여 개수 불일치",
        )
        body, entry = bodies[("ability", spid, 5)]
        context = ParseContext(
            batch_id=COLLECTION_ID,
            source="nexon_web",
            observed_at=entry["observed_at"],
            delete_after=None,
            spid=spid,
            reinforce_level=5,
            version=entry["sha256"],
        )
        parsed = parse_ability(body, context)
        require(
            data["player_card"][0] == parsed.card
            and data["card_salary"][0] == parsed.salary,
            "기본 카드·급여 원본 불일치",
        )
        expected_stats = {
            keyword_map()[label]: value
            for label, value in displayed_stats(body).items()
        }
        require(parsed.card.stats == expected_stats, "능력치 직접 표시 숫자 불일치")
        detail, detail_entry = bodies[("detail", spid, None)]
        expected_colors = []
        for name in parse_club_history(detail):
            expected_colors.append(
                TeamColor(
                    batch_id=COLLECTION_ID,
                    source="nexon_web",
                    observed_at=detail_entry["observed_at"],
                    delete_after=None,
                    spid=spid,
                    version=detail_entry["sha256"],
                    status="unknown",
                    valid_until=None,
                    type="club",
                    name=name,
                )
            )
        if parsed.nationality:
            expected_colors.append(
                TeamColor(
                    **parsed.card.model_dump(
                        include={
                            "batch_id",
                            "source",
                            "observed_at",
                            "delete_after",
                            "spid",
                            "version",
                            "status",
                            "valid_until",
                        }
                    ),
                    type="nation",
                    name=parsed.nationality,
                )
            )
        require(data["team_color"] == expected_colors, "클럽·국적 원본 불일치")
        for key, values in data.items():
            counts[key] += len(values)
            require(
                all(
                    value.status == "unknown"
                    and value.valid_until is None
                    and value.delete_after is None
                    for value in values
                ),
                "신선도·보관 정책 불일치",
            )
    require(
        {int(path.stem) for path in processed_path.glob("[0-9]*.json")}
        == set(selected),
        "가공 표본 파일 불일치",
    )
    return {
        "verified": True,
        "attempts": state["attempts"],
        "cards": expected_count,
        "source_stat_values_compared": expected_count * 34,
        "min_request_start_gap_seconds": min(gaps),
        "collection_counts": dict(counts),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = verify(
        ROOT / "data/raw" / COLLECTION_ID, ROOT / "data/processed" / COLLECTION_ID
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
