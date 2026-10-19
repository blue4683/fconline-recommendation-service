"""원본과 관측 시각을 보존한 새 정책 검증 배치. HTTP/DB 갱신은 하지 않는다."""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime

from FRS.core.freshness import specification, utc
from FRS.schema.models import DOCUMENT_MODELS
from FRS.utils.crawling.ability_parser import (
    ParseContext,
    parse_ability,
    reinforcement_delta,
)
from FRS.utils.crawling.enrich_cards import RUN_ID, SOURCE_ID, source_selection
from FRS.utils.crawling.pilot import (
    ABILITY_URL,
    ROOT,
    PilotStopped,
    ability_form,
    now,
    write_json,
)
from FRS.utils.crawling.player_list_parser import PlayerListContext
from FRS.utils.crawling.refresh_observations import adopt_ability, adopt_catalog
from FRS.utils.crawling.verify_enrichment import verify as verify_enrichment
from FRS.utils.crawling.verify_pilot import read_jsonl, require


def validate_id(value):
    if (
        not isinstance(value, str)
        or len(value) > 80
        or re.fullmatch(r"stage-1-f-derived-[a-z0-9]+(?:-[a-z0-9]+)*", value) is None
    ):
        raise PilotStopped("파생 배치 ID가 유효하지 않습니다.")


def build_records(*, batch_id, targets, catalogs, profiles, at):
    """검증한 원본 응답에서 새 문서를 만든다. 원래 batch_id의 문서를 수정하지 않는다."""
    validate_id(batch_id)
    at = utc(at)
    targets = set(targets)
    result = defaultdict(list)
    bases = {}
    price_values = {}
    seen_profiles = set()
    for body, entry in catalogs:
        require(
            hashlib.sha256(body).hexdigest() == entry["sha256"], "목록 원본 해시 불일치"
        )
        ctx = PlayerListContext(
            batch_id=batch_id,
            source="nexon_web",
            observed_at=entry["observed_at"],
            delete_after=None,
            version=entry["sha256"],
            reinforce_level=5,
            skill_fields=tuple(entry["form"][f"strSkill{i}"] for i in range(1, 5)),
        )
        evidence = {
            **entry,
            "status_code": entry.get("status_code", entry.get("status")),
        }
        catalog = adopt_catalog(body, ctx, evidence, at=at)
        for kind, records in (
            ("card_price", catalog.prices),
            ("team_color", catalog.memberships),
        ):
            for record in records:
                if record.spid not in targets:
                    continue
                if kind == "card_price":
                    key = record.spid, record.reinforce_level
                    require(
                        key not in price_values or price_values[key] == record.price,
                        "가격 관측 값 충돌",
                    )
                    price_values[key] = record.price
                result[kind].append(record)
    for body, entry in profiles:
        spid, grade = entry["spid"], entry["grade"]
        require(
            entry["resource"] == "ability"
            and spid in targets
            and grade in range(5, 12),
            "프로필 관리 대상/강화 불일치",
        )
        require((spid, grade) not in seen_profiles, "프로필 요청 중복")
        seen_profiles.add((spid, grade))
        require(
            entry["url"] == ABILITY_URL
            and entry["method"] == "POST"
            and entry["status_code"] == 200
            and entry["form"] == ability_form(spid, grade),
            "프로필 요청 provenance 불일치",
        )
        require(
            hashlib.sha256(body).hexdigest() == entry["sha256"],
            "프로필 원본 해시 불일치",
        )
        ctx = ParseContext(
            batch_id=batch_id,
            source="nexon_web",
            observed_at=entry["observed_at"],
            delete_after=None,
            version=entry["sha256"],
            spid=spid,
            reinforce_level=grade,
        )
        parsed = adopt_ability(parse_ability(body, ctx), at=at)
        if grade == 5:
            bases[spid] = parsed.card
            result["player_card"].append(parsed.card)
        require(spid in bases, "기준 카드 없는 강화")
        result["card_salary"].append(parsed.salary)
        result["reinforce_stats"].append(reinforcement_delta(bases[spid], parsed.card))
    require(
        seen_profiles == {(spid, grade) for spid in targets for grade in range(5, 12)},
        "강화별 프로필 누락",
    )
    require(
        set(bases) == targets and {p.spid for p in result["card_price"]} == targets,
        "관리 대상 누락",
    )
    require(
        {m.spid for m in result["team_color"]} == targets, "공식 필터의 양성 관측 누락"
    )
    return dict(result)


def source_records(root):
    selection = source_selection(root / "data/raw" / SOURCE_ID)
    raw = root / "data/raw" / RUN_ID
    processed = root / "data/processed" / RUN_ID
    verify_enrichment(raw, processed, expected_selection=selection)
    profile = json.loads((raw / "manifest.json").read_text(encoding="utf-8"))
    catalog_root = root / "data/raw" / SOURCE_ID
    catalog = json.loads((catalog_root / "manifest.json").read_text(encoding="utf-8"))
    require(len(profile["selected_ids"]) == 259, "확정한 259개 관리 대상 불일치")
    catalogs = [
        ((catalog_root / e["path"]).read_bytes(), e) for e in catalog["responses"]
    ]
    profiles = [
        ((raw / e["path"]).read_bytes(), e)
        for e in profile["responses"]
        if e["resource"] == "ability"
    ]
    hashes = {
        batch: hashlib.sha256(
            (root / "data/raw" / batch / "manifest.json").read_bytes()
        ).hexdigest()
        for batch in (RUN_ID, SOURCE_ID)
    }
    return profile["selected_ids"], catalogs, profiles, hashes


def derive(root, batch_id, *, at):
    validate_id(batch_id)
    at = utc(at)
    targets, catalogs, profiles, hashes = source_records(root)
    # 검증 단계도 외부 요청은 없다. 새 배치 디렉터리는 기존 것을 재사용하지 않는다.
    raw = root / "data/raw" / batch_id
    processed = root / "data/processed" / batch_id
    raw.mkdir(parents=True, exist_ok=False)
    processed.mkdir(parents=True, exist_ok=False)
    state = dict(
        batch_id=batch_id,
        source_mode="derived",
        status="running",
        validation_at=at.isoformat(),
        source_manifest_hashes=hashes,
        selected_ids=targets,
        policy=specification(),
        observation_contract=dict(
            version=1, price_type="reference", eligibility_basis="official_club_filter"
        ),
        network_requests_sent=0,
        auto_activation=False,
    )
    write_json(raw / "manifest.json", state)
    try:
        records = build_records(
            batch_id=batch_id,
            targets=targets,
            catalogs=catalogs,
            profiles=profiles,
            at=at,
        )
        for key, values in records.items():
            temporary = processed / f"{key}.jsonl.tmp"
            temporary.write_text(
                "".join(r.model_dump_json() + "\n" for r in values), encoding="utf-8"
            )
            temporary.replace(processed / f"{key}.jsonl")
        state.update(
            status="completed",
            processed_hashes={
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in processed.glob("*.jsonl")
            },
            collection_counts={key: len(values) for key, values in records.items()},
            status_counts=dict(
                Counter(str(r.status) for values in records.values() for r in values)
            ),
        )
    except Exception as error:
        state.update(status="stopped", failure=str(error))
        raise
    finally:
        write_json(raw / "manifest.json", state)
    return state


def verify(root, batch_id, *, expected_targets):
    validate_id(batch_id)
    raw = root / "data/raw" / batch_id
    processed = root / "data/processed" / batch_id
    state = json.loads((raw / "manifest.json").read_text(encoding="utf-8"))
    require(
        state["source_mode"] == "derived"
        and state["status"] == "completed"
        and state["batch_id"] == batch_id,
        "파생 배치 미완료/불일치",
    )
    require(
        state["policy"] == specification() and state["network_requests_sent"] == 0,
        "정책/출처 모드 불일치",
    )
    require(
        state.get("observation_contract")
        == dict(
            version=1, price_type="reference", eligibility_basis="official_club_filter"
        ),
        "관측 의미 계약 불일치",
    )
    targets, catalogs, profiles, hashes = source_records(root)
    require(
        state["selected_ids"] == targets == list(expected_targets), "관리 대상 변경"
    )
    require(state["source_manifest_hashes"] == hashes, "기존 원본 manifest 변경")
    expected = build_records(
        batch_id=batch_id,
        targets=targets,
        catalogs=catalogs,
        profiles=profiles,
        at=datetime.fromisoformat(state["validation_at"]),
    )
    require(
        set(state["processed_hashes"])
        == {p.name for p in processed.glob("*.jsonl")}
        == {f"{key}.jsonl" for key in expected},
        "가공 파일 목록 불일치",
    )
    for key, values in expected.items():
        path = processed / f"{key}.jsonl"
        require(
            hashlib.sha256(path.read_bytes()).hexdigest()
            == state["processed_hashes"][path.name],
            "파생 가공 해시 불일치",
        )
        require(
            read_jsonl(path, DOCUMENT_MODELS[key]) == values,
            "파생 정규화/관측 시각 불일치",
        )
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--at", type=datetime.fromisoformat)
    args = parser.parse_args()
    try:
        result = derive(ROOT, args.batch_id, at=args.at or now())
    except (ValueError, OSError, PilotStopped) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
