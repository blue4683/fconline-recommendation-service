"""저장된 파일럿을 네트워크 없이 검증한다."""

import argparse
import hashlib
import json
import re
from collections import Counter

from FRS.schema.models import DOCUMENT_MODELS
from FRS.utils.crawling.ability_parser import (
    ParseContext,
    keyword_map,
    parse_ability,
    parse_club_history,
)
from FRS.utils.crawling.pilot import (
    LEVELS,
    MAX_ATTEMPTS,
    PILOT_ID,
    REGRESSION_IDS,
    ROOT,
    SAMPLE_SIZE,
    stratified_ids,
)


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def read_jsonl(path, model):
    return [
        model.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def displayed_stats(body):
    # 주 파서와 다른 방식으로 원본의 직접 표시 숫자를 대조한다. diff 자식값은 제외한다.
    html = body.decode("utf-8")
    blocks = re.findall(
        r'<ul\b[^>]*class="[^"]*\bdata_wrap_playerinfo\b[^"]*"[^>]*>(.*?)</ul>',
        html,
        re.S,
    )
    require(bool(blocks), "원본 능력치 표시 영역 누락")
    rows = re.findall(
        r'<li\b[^>]*class="ab(?:\s[^"]*)?"[^>]*>(.*?)</li>', "".join(blocks), re.S
    )
    output = {}
    for row in rows:
        label = re.search(r'class="txt"[^>]*>([^<]*)<', row)
        value = re.search(r'class="value(?:\s[^"]*)?"[^>]*>\s*([0-9,]+)\s*(?:<|$)', row)
        require(label is not None and value is not None, "원본 능력치 표시 구조 불일치")
        key = re.sub(r"\s+", "", label[1])
        require(key not in output, "원본 능력치 표시 중복")
        output[key] = int(value[1].replace(",", ""))
    require(len(output) == 34, "원본 능력치 표시 34개 불일치")
    return output


def verify(raw_path, processed_path):
    manifest = json.loads((raw_path / "manifest.json").read_text())
    require(manifest["pilot_id"] == PILOT_ID, "파일럿 ID 불일치")
    require(manifest["status"] == "completed", "완료되지 않은 파일럿")
    require(0 < manifest["attempts"] <= MAX_ATTEMPTS, "요청 상한 불일치")
    logs = read_jsonl(processed_path / "crawl_log.jsonl", DOCUMENT_MODELS["crawl_log"])
    require(
        [log.attempt for log in logs] == list(range(1, manifest["attempts"] + 1)),
        "요청 로그 누락·중복",
    )
    require(all(log.outcome != "stopped" for log in logs), "중단 뒤 완료 표기")
    gaps = [
        (b.observed_at - a.observed_at).total_seconds() for a, b in zip(logs, logs[1:])
    ]
    require(all(gap >= 0.999 for gap in gaps), "초당 최대 1회 조건 위반")
    entries = manifest["responses"]
    require(len(entries) == len(logs), "완료 파일럿의 응답 누락")
    bodies = {}
    parsed = {}
    for entry in entries:
        body = (raw_path / entry["path"]).read_bytes()
        require(hashlib.sha256(body).hexdigest() == entry["sha256"], "원본 해시 불일치")
        if entry["status_code"] != 200:
            continue
        key = (entry["resource"], entry["spid"], entry["grade"])
        bodies[key] = (body, entry)
        if entry["resource"] == "ability":
            context = ParseContext(
                batch_id=PILOT_ID,
                source="nexon_web",
                observed_at=entry["observed_at"],
                delete_after=None,
                spid=entry["spid"],
                reinforce_level=entry["grade"],
                version=entry["sha256"],
            )
            parsed[key] = parse_ability(body, context)
            expected = {
                keyword_map()[label]: value
                for label, value in displayed_stats(body).items()
            }
            require(
                parsed[key].card.stats == expected, "원본 표시 숫자와 능력치 불일치"
            )
    selected = manifest["selected_ids"]
    require(
        len(selected) == len(set(selected)) == SAMPLE_SIZE, "층화 표본 100명 불일치"
    )
    require(not set(selected) & set(REGRESSION_IDS), "회귀·층화 표본 중복")
    screened = [
        parsed[("ability", spid, 5)].card
        for spid in manifest["screening_ids"]
        if ("ability", spid, 5) in parsed
    ]
    require(stratified_ids(screened) == selected, "층화 재현 불일치")
    wanted = set(selected) | set(REGRESSION_IDS)
    data = {
        name: read_jsonl(processed_path / f"{name}.jsonl", DOCUMENT_MODELS[name])
        for name in ("player_card", "card_salary", "reinforce_stats", "team_color")
    }
    cards = {card.spid: card for card in data["player_card"]}
    require(
        len(data["player_card"]) == len(cards) == 104 and set(cards) == wanted,
        "기본 카드 표본 불일치",
    )
    for spid, card in cards.items():
        require(card == parsed[("ability", spid, 5)].card, "기본 카드와 원본 불일치")
    expected_pairs = {(spid, grade) for spid in wanted for grade in LEVELS}
    for collection in ("card_salary", "reinforce_stats"):
        records = data[collection]
        require(len(records) == len(expected_pairs), "강화 레코드 수 불일치")
        require(
            {(r.spid, r.reinforce_level) for r in records} == expected_pairs,
            "강화 단계 누락·중복",
        )
        for record in records:
            source = parsed[("ability", record.spid, record.reinforce_level)]
            if collection == "card_salary":
                require(record == source.salary, "급여와 원본 불일치")
            else:
                base = cards[record.spid]
                require(
                    record.baseline_card_version == base.version
                    and record.baseline_reinforce_level == 5,
                    "강화 기준 불일치",
                )
                require(
                    record.stats_delta
                    == {
                        key: source.card.stats[key] - base.stats[key]
                        for key in base.stats
                    },
                    "강화 차이값 불일치",
                )
                require(
                    record.position_ovr == source.card.position_ovr, "강화 OVR 불일치"
                )
                require(
                    record.version == source.card.version
                    and record.observed_at == source.card.observed_at,
                    "강화 관측 메타데이터 불일치",
                )
    expected_colors = []
    for spid in wanted:
        body, entry = bodies[("detail", spid, None)]
        expected_colors.extend(
            (spid, "club", club, entry["sha256"]) for club in parse_club_history(body)
        )
        nationality = parsed[("ability", spid, 5)].nationality
        if nationality:
            expected_colors.append((spid, "nation", nationality, cards[spid].version))
    require(
        Counter((r.spid, r.type, r.name, r.version) for r in data["team_color"])
        == Counter(expected_colors),
        "클럽·국적 분리 불일치",
    )
    require(
        all(
            r.status == "unknown" and r.valid_until is None and r.delete_after is None
            for records in data.values()
            for r in records
        ),
        "미확정 신선도·보관 정책 불일치",
    )
    return {
        "verified": True,
        "attempts": manifest["attempts"],
        "min_request_start_gap_seconds": min(gaps),
        "source_stat_values_compared": len(parsed) * 34,
        "regression_cards": len(REGRESSION_IDS),
        "stratified_cards": len(selected),
        "collection_counts": {name: len(records) for name, records in data.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = verify(ROOT / "data/raw" / PILOT_ID, ROOT / "data/processed" / PILOT_ID)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
