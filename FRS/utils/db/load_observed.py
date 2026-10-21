"""완료된 실데이터 실행의 가공 결과를 로컬 MongoDB에 적재한다.

원본 기준은 `data/raw`, `data/processed`의 파일이다. DB는 파일에서 다시 만들 수 있는
사본이다. 보고서 해시·출처·배치를 검증한 뒤에만 적재하고, 같은 batch_id가 이미
있으면 거부한다(덮어쓰기·최신 자동 선택 없음). 기본 CLI 실행은 적재 계획만 출력한다.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from FRS.agent.real_material import ObservedMaterial
from FRS.core.candidate_search.mongo import create_candidate_indexes

# ObservedMaterial.load()의 키 → MongoDB 컬렉션
COLLECTIONS = {
    "cards": "player_card",
    "salaries": "card_salary",
    "prices": "card_price",
    "reinforcements": "reinforce_stats",
    "team_colors": "team_color",
}
LOAD_LOG = "observed_load"


class LoadRefused(RuntimeError):
    pass


def load_plan(processed_root, run_id):
    """네트워크·DB 없이 적재할 건수만 계산한다."""
    material = ObservedMaterial(processed_root, run_id)
    data = material.load(material.batches)
    return dict(
        run_id=run_id,
        material_id=material.material_id,
        counts={COLLECTIONS[key]: len(rows) for key, rows in data.items()},
        processed_hashes=material.report["processed_hashes"],
    )


def _existing(database, run_id):
    return [
        name
        for name in COLLECTIONS.values()
        if database[name].count_documents({"batch_id": run_id}, limit=1)
    ]


def load_observed_run(database, processed_root, run_id, *, clock=None):
    material = ObservedMaterial(processed_root, run_id)
    data = material.load(material.batches)
    if _existing(database, run_id) or database[LOAD_LOG].count_documents(
        {"run_id": run_id}, limit=1
    ):
        raise LoadRefused(f"{run_id}는 이미 적재됐습니다. 덮어쓰지 않습니다.")
    create_candidate_indexes(database)
    written = []
    try:
        for key, name in COLLECTIONS.items():
            rows = data[key]
            written.append(name)
            if rows:
                database[name].insert_many([row.to_document() for row in rows])
        counts = {}
        for key, name in COLLECTIONS.items():
            stored = database[name].count_documents({"batch_id": run_id})
            if stored != len(data[key]):
                raise LoadRefused(
                    f"{name}: 파일 {len(data[key])}건, DB {stored}건으로 다릅니다."
                )
            counts[name] = stored
    except BaseException:
        # 적재 전 이 batch_id가 없음을 확인했으므로, 이번 호출이 쓴 것만 지운다.
        for name in written:
            database[name].delete_many({"batch_id": run_id})
        raise
    record = dict(
        run_id=run_id,
        material_id=material.material_id,
        counts=counts,
        processed_hashes=material.report["processed_hashes"],
        eligibility_basis=material.report.get("eligibility_basis"),
        loaded_at=(clock or (lambda: datetime.now(timezone.utc)))(),
    )
    database[LOAD_LOG].insert_one(dict(record))
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--write", action="store_true", help="검증 후 로컬 MongoDB에 실제로 적재"
    )
    args = parser.parse_args(argv)
    processed = args.root / "data/processed"
    if not args.write:
        print(
            json.dumps(load_plan(processed, args.run_id), ensure_ascii=False, indent=2)
        )
        return 0
    from FRS.utils.db.client import Client

    with Client() as client:
        client.ping()
        record = load_observed_run(client.db, processed, args.run_id)
    print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
