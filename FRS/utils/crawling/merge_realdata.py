"""완료한 실데이터 실행 여럿을 오프라인으로 합쳐 파생 배치 하나를 만든다.

설계: docs/real-data-branch-design.md RD-6·§12.20. 사용자 결정(2026-10-22):

- 합치는 순서는 사용자가 `--source`로 명시한다. 같은 카드가 여러 실행에 있으면 앞
  실행의 카드·급여·가격·강화 관측을 그대로 쓴다(카드 단위, 실행끼리 섞지 않음).
  최신 관측을 자동으로 고르지 않는다.
- 팀컬러 자격은 실행마다 받은 팀이 다르므로 모든 실행의 자격을 합친다. 같은
  (카드, 유형, 팀) 자격은 앞 실행 것을 쓴다.
- 관측 시각·기한·버전은 원래 값을 유지하고 `batch_id`만 새 배치 ID로 바꾼다.
  엔진은 지금처럼 쓰인 관측의 가장 이른 기한으로 만료를 판정한다.

원본 실행은 읽기만 하고, 새 넥슨 요청·DB 쓰기는 없다. 기본 CLI는 계획만 출력한다.
"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from FRS.agent.real_material import ObservedMaterial, RealMaterialError
from FRS.utils.crawling.realdata_catalog import (
    ELIGIBILITY_BASIS,
    MIXED_ELIGIBILITY_BASIS,
    CatalogStopped,
    validate_run_id,
)

DERIVATION = "merge_priority_order"
# ObservedMaterial.load()의 키 → 가공 파일 이름. 줄 순서는 아래 정렬 키로 고정한다.
FILES = {
    "cards": "player_card",
    "salaries": "card_salary",
    "prices": "card_price",
    "reinforcements": "reinforce_stats",
    "team_colors": "team_color",
}
ORDER = {
    "cards": lambda row: row.spid,
    "salaries": lambda row: (row.spid, row.reinforce_level),
    "prices": lambda row: (row.spid, row.reinforce_level),
    "reinforcements": lambda row: (row.spid, row.reinforce_level),
    "team_colors": lambda row: (row.spid, row.type, row.name),
}


class MergeRefused(ValueError):
    pass


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sources(processed_root, run_ids):
    if len(run_ids) < 2 or len(set(run_ids)) != len(run_ids):
        raise MergeRefused("서로 다른 실행을 둘 이상, 합칠 순서대로 명시해야 합니다.")
    sources = []
    for run_id in run_ids:
        try:
            material = ObservedMaterial(processed_root, run_id)
        except RealMaterialError as error:
            raise MergeRefused(f"{run_id}: {error}") from None
        if material.report.get("source_mode") == "derived":
            raise MergeRefused(f"{run_id}: 파생 배치는 다시 합치지 않습니다.")
        sources.append((run_id, material, material.load(material.batches)))
    return sources


def merge_records(sources):
    """앞 실행 우선으로 카드 단위 관측과 모든 실행의 자격을 고른다."""
    owner = {}
    for run_id, _, data in sources:
        for card in data["cards"]:
            owner.setdefault(card.spid, run_id)
    merged = {key: [] for key in FILES}
    for run_id, _, data in sources:
        for key in ("cards", "salaries", "prices", "reinforcements"):
            merged[key] += [row for row in data[key] if owner.get(row.spid) == run_id]
    memberships = {}
    for _, _, data in sources:
        for row in data["team_colors"]:
            memberships.setdefault((row.spid, row.type, row.name), row)
    merged["team_colors"] = list(memberships.values())
    if any(row.spid not in owner for row in merged["team_colors"]):
        raise MergeRefused("카드 관측이 없는 팀컬러 자격이 있습니다.")
    return merged, owner


def plan(processed_root, run_ids, merged_id):
    validate_run_id(merged_id)
    if merged_id in run_ids:
        raise MergeRefused("새 배치 ID는 원본 실행 ID와 달라야 합니다.")
    sources = _sources(processed_root, run_ids)
    merged, owner = merge_records(sources)
    seen = Counter(card.spid for _, _, data in sources for card in data["cards"])
    return dict(
        merged_id=merged_id,
        derivation=DERIVATION,
        priority=list(run_ids),
        counts={FILES[key]: len(rows) for key, rows in merged.items()},
        cards_by_source=dict(Counter(owner.values())),
        overlapping_cards=sum(1 for n in seen.values() if n > 1),
        network_requests=0,
    )


def merge(processed_root, run_ids, merged_id, *, clock=None):
    """새 파생 배치를 `processed_root/merged_id`에 쓴다. 이미 있으면 거부한다."""
    processed_root = Path(processed_root)
    summary = plan(processed_root, run_ids, merged_id)
    target = processed_root / merged_id
    if target.exists():
        raise MergeRefused(f"{merged_id}가 이미 있습니다. 덮어쓰지 않습니다.")
    sources = _sources(processed_root, run_ids)
    merged, _ = merge_records(sources)
    target.mkdir(parents=True)
    for key, rows in merged.items():
        lines = "".join(
            row.model_copy(update={"batch_id": merged_id}).model_dump_json() + "\n"
            for row in sorted(rows, key=ORDER[key])
        )
        (target / f"{FILES[key]}.jsonl").write_text(lines, encoding="utf-8")
    bases = {material.report.get("eligibility_basis") for _, material, _ in sources}
    report = dict(
        run_id=merged_id,
        status="completed",
        source_mode="derived",
        derivation=DERIVATION,
        priority=list(run_ids),
        sources=[
            dict(
                run_id=run_id,
                report_sha256=_sha256(material.path / "report.json"),
                processed_hashes=material.report["processed_hashes"],
            )
            for run_id, material, _ in sources
        ],
        eligibility_basis=(
            MIXED_ELIGIBILITY_BASIS
            if MIXED_ELIGIBILITY_BASIS in bases
            else ELIGIBILITY_BASIS
        ),
        catalog_cards=len(merged["cards"]),
        profile_cards=len(merged["cards"]),
        cards_by_club=dict(Counter(row.name for row in merged["team_colors"])),
        cards_by_source=summary["cards_by_source"],
        overlapping_cards=summary["overlapping_cards"],
        network_requests=0,
        auto_activation=False,
        created_at=(clock or (lambda: datetime.now(timezone.utc)))().isoformat(),
        processed_hashes={
            f"{name}.jsonl": _sha256(target / f"{name}.jsonl")
            for name in sorted(FILES.values())
        },
    )
    (target / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def verify_merged(processed_root, merged_id):
    """원본 실행으로 같은 순서의 합치기를 다시 해 가공 파일 바이트가 같은지 본다."""
    import tempfile

    processed_root = Path(processed_root)
    report = json.loads(
        (processed_root / merged_id / "report.json").read_text(encoding="utf-8")
    )
    if (
        report.get("run_id") != merged_id
        or report.get("source_mode") != "derived"
        or report.get("derivation") != DERIVATION
    ):
        raise MergeRefused("합치기로 만든 파생 배치의 보고서가 아닙니다.")
    ObservedMaterial(processed_root, merged_id)  # 상태·해시·출처·배치 대조
    run_ids = report["priority"]
    for source in report["sources"]:
        path = processed_root / source["run_id"]
        if (
            _sha256(path / "report.json") != source["report_sha256"]
            or {name: _sha256(path / name) for name in source["processed_hashes"]}
            != source["processed_hashes"]
        ):
            raise MergeRefused(f"{source['run_id']}: 원본 실행이 합칠 때와 다릅니다.")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for run_id in run_ids:
            # 원본은 읽기만 한다. 임시 경로에 같은 파일을 복사해 다시 합친다.
            (root / run_id).mkdir()
            for path in (processed_root / run_id).iterdir():
                (root / run_id / path.name).write_bytes(path.read_bytes())
        again = merge(
            root, run_ids, merged_id, clock=lambda: datetime.now(timezone.utc)
        )
        for name in again["processed_hashes"]:
            if (root / merged_id / name).read_bytes() != (
                processed_root / merged_id / name
            ).read_bytes():
                raise MergeRefused(f"다시 합친 {name}이 다릅니다.")
        for key in (
            "priority",
            "sources",
            "eligibility_basis",
            "catalog_cards",
            "cards_by_club",
            "cards_by_source",
            "overlapping_cards",
            "processed_hashes",
        ):
            if again[key] != report[key]:
                raise MergeRefused(f"다시 합친 보고서의 {key}가 다릅니다.")
    return dict(
        merged_id=merged_id,
        verified=True,
        sources=run_ids,
        catalog_cards=report["catalog_cards"],
        network_requests=0,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="합칠 완료 실행 ID. 앞에 둔 실행의 관측이 우선한다",
    )
    parser.add_argument("--merged-id", required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--run", action="store_true", help="파생 배치 파일을 쓴다")
    action.add_argument(
        "--verify", action="store_true", help="이미 만든 파생 배치를 다시 합쳐 대조한다"
    )
    args = parser.parse_args(argv)
    processed_root = args.root / "data/processed"
    try:
        if args.verify:
            result = verify_merged(processed_root, args.merged_id)
        elif args.run:
            result = merge(processed_root, args.source, args.merged_id)
        else:
            result = plan(processed_root, args.source, args.merged_id)
    except (MergeRefused, CatalogStopped) as error:
        print(json.dumps(dict(error=str(error)), ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
