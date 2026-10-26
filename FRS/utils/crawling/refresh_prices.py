"""가격만 새로 받은 실행과 기존 완료 배치를 오프라인으로 합쳐 파생 배치 하나를 만든다.

설계: docs/real-data-branch-design.md §12.23. 사용자 결정(2026-10-26):

- 응답 기한은 지금처럼 쓰인 관측 중 가장 이른 기한이다. 가격은 새 관측(24시간),
  카드·급여·강화는 기존 배치의 원래 관측(7일)을 그대로 쓴다. 기한을 늘리지 않는다.
- 두 배치에 모두 있는 카드만 쓴다. 새 목록에만 있는 카드(능력치 없음)와 기존 배치에만
  있는 카드(새 가격 없음)는 빼고 보고서에 남긴다.
- 같은 카드의 목록 급여(5강)가 기존 배치 급여와 다르면 그 카드만 빼고 남긴다.
- 팀컬러 자격은 새 목록 관측을 쓴다.

관측 시각·기한·버전은 원래 값을 유지하고 `batch_id`만 새 배치 ID로 바꾼다. 원본은 읽기만
하고, 새 넥슨 요청·DB 쓰기는 없다. 기본 CLI는 계획만 출력한다.
"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from FRS.agent.real_material import ObservedMaterial, RealMaterialError
from FRS.schema.models import CardPrice, CardSalary, SourceKind, TeamColor
from FRS.utils.crawling.merge_realdata import FILES, ORDER
from FRS.utils.crawling.realdata_catalog import (
    PRICE_FILES,
    PRICES_COLLECTED,
    CatalogStopped,
    validate_run_id,
)

DERIVATION = "price_refresh"
_PRICE_MODELS = dict(
    card_price=CardPrice, team_color=TeamColor, catalog_salary=CardSalary
)


class RefreshRefused(ValueError):
    pass


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _base(processed_root, base_id):
    try:
        material = ObservedMaterial(processed_root, base_id)
    except RealMaterialError as error:
        raise RefreshRefused(f"{base_id}: {error}") from None
    if material.report.get("derivation") == DERIVATION:
        raise RefreshRefused(
            f"{base_id}: 가격 갱신 파생 배치는 기준으로 쓰지 않습니다."
        )
    return material, material.load(material.batches)


def _prices(processed_root, prices_id):
    """가격 갱신 실행의 보고서·해시·출처를 확인하고 행을 읽는다."""
    path = Path(processed_root) / prices_id
    report_path = path / "report.json"
    if not report_path.is_file():
        raise RefreshRefused(f"{prices_id}: 가격 갱신 보고서가 없습니다.")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (
        report.get("run_id") != prices_id
        or report.get("status") != PRICES_COLLECTED
        or report.get("mode") != "prices_only"
        or report.get("auto_activation") is not False
    ):
        raise RefreshRefused(f"{prices_id}: 완료한 가격 갱신 실행이 아닙니다.")
    hashes = report.get("processed_hashes")
    if not isinstance(hashes, dict) or set(hashes) != {
        f"{name}.jsonl" for name in PRICE_FILES
    }:
        raise RefreshRefused(f"{prices_id}: 가공 파일 해시 목록이 다릅니다.")
    for name, digest in hashes.items():
        if _sha256(path / name) != digest:
            raise RefreshRefused(f"{prices_id}: {name}의 해시가 보고서와 다릅니다.")
    rows = {}
    for name, model in _PRICE_MODELS.items():
        lines = (path / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
        rows[name] = [model.model_validate(json.loads(line)) for line in lines if line]
        if any(
            row.source != SourceKind.WEB or row.batch_id != prices_id
            for row in rows[name]
        ):
            raise RefreshRefused(f"{prices_id}: 관측 출처/배치가 다릅니다.")
    return path, report, rows


def refresh_records(base, prices):
    """기존 카드·급여·강화에 새 가격·자격을 붙인다. (합친 행, 제외 목록)을 돌려준다."""
    listed = {row.spid: row.salary for row in prices["catalog_salary"]}
    if len(listed) != len(prices["catalog_salary"]):
        raise RefreshRefused("목록 급여에 같은 카드가 두 번 있습니다.")
    base_cards = {card.spid for card in base["cards"]}
    base_salary = {
        row.spid: row.salary for row in base["salaries"] if row.reinforce_level == 5
    }
    mismatched = [
        dict(spid=spid, base_salary=base_salary.get(spid), listed_salary=listed[spid])
        for spid in sorted(base_cards & set(listed))
        if base_salary.get(spid) != listed[spid]
    ]
    kept = (base_cards & set(listed)) - {row["spid"] for row in mismatched}
    merged = {
        key: [row for row in base[key] if row.spid in kept]
        for key in ("cards", "salaries", "reinforcements")
    }
    merged["prices"] = [row for row in prices["card_price"] if row.spid in kept]
    merged["team_colors"] = [row for row in prices["team_color"] if row.spid in kept]
    excluded = dict(
        missing_profile=sorted(set(listed) - base_cards),
        missing_price=sorted(base_cards - set(listed)),
        salary_mismatch=mismatched,
    )
    return merged, excluded


def plan(processed_root, base_id, prices_id, refreshed_id):
    validate_run_id(refreshed_id)
    if refreshed_id in (base_id, prices_id) or base_id == prices_id:
        raise RefreshRefused("기준·가격·새 배치 ID는 서로 달라야 합니다.")
    _, base = _base(processed_root, base_id)
    _, _, prices = _prices(processed_root, prices_id)
    merged, excluded = refresh_records(base, prices)
    return dict(
        refreshed_id=refreshed_id,
        derivation=DERIVATION,
        base=base_id,
        prices=prices_id,
        counts={FILES[key]: len(rows) for key, rows in merged.items()},
        excluded={key: len(value) for key, value in excluded.items()},
        network_requests=0,
    )


def refresh(processed_root, base_id, prices_id, refreshed_id, *, clock=None):
    """새 파생 배치를 `processed_root/refreshed_id`에 쓴다. 이미 있으면 거부한다."""
    processed_root = Path(processed_root)
    plan(processed_root, base_id, prices_id, refreshed_id)
    target = processed_root / refreshed_id
    if target.exists():
        raise RefreshRefused(f"{refreshed_id}가 이미 있습니다. 덮어쓰지 않습니다.")
    material, base = _base(processed_root, base_id)
    prices_path, prices_report, prices = _prices(processed_root, prices_id)
    merged, excluded = refresh_records(base, prices)
    target.mkdir(parents=True)
    for key, rows in merged.items():
        lines = "".join(
            row.model_copy(update={"batch_id": refreshed_id}).model_dump_json() + "\n"
            for row in sorted(rows, key=ORDER[key])
        )
        (target / f"{FILES[key]}.jsonl").write_text(lines, encoding="utf-8")
    report = dict(
        run_id=refreshed_id,
        status="completed",
        source_mode="derived",
        derivation=DERIVATION,
        sources=[
            dict(
                role=role,
                run_id=run_id,
                report_sha256=_sha256(path / "report.json"),
                processed_hashes=hashes,
            )
            for role, run_id, path, hashes in (
                ("base", base_id, material.path, material.report["processed_hashes"]),
                ("prices", prices_id, prices_path, prices_report["processed_hashes"]),
            )
        ],
        eligibility_basis=prices_report["eligibility_basis"],
        catalog_cards=len(merged["cards"]),
        profile_cards=len(merged["cards"]),
        cards_by_club=dict(Counter(row.name for row in merged["team_colors"])),
        excluded=excluded,
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


def verify_refreshed(processed_root, refreshed_id):
    """원본 두 배치로 같은 갱신을 다시 해 가공 파일 바이트·보고서가 같은지 본다."""
    import tempfile

    processed_root = Path(processed_root)
    report = json.loads(
        (processed_root / refreshed_id / "report.json").read_text(encoding="utf-8")
    )
    if (
        report.get("run_id") != refreshed_id
        or report.get("source_mode") != "derived"
        or report.get("derivation") != DERIVATION
        or [s.get("role") for s in report.get("sources", [])] != ["base", "prices"]
    ):
        raise RefreshRefused("가격 갱신으로 만든 파생 배치의 보고서가 아닙니다.")
    try:
        ObservedMaterial(processed_root, refreshed_id)  # 상태·해시·출처·배치 대조
    except RealMaterialError as error:
        raise RefreshRefused(f"{refreshed_id}: {error}") from None
    for source in report["sources"]:
        path = processed_root / source["run_id"]
        if (
            _sha256(path / "report.json") != source["report_sha256"]
            or {name: _sha256(path / name) for name in source["processed_hashes"]}
            != source["processed_hashes"]
        ):
            raise RefreshRefused(f"{source['run_id']}: 원본이 갱신할 때와 다릅니다.")
    base_id, prices_id = (source["run_id"] for source in report["sources"])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for run_id in (base_id, prices_id):
            # 원본은 읽기만 한다. 임시 경로에 같은 파일을 복사해 다시 만든다.
            (root / run_id).mkdir()
            for path in (processed_root / run_id).iterdir():
                (root / run_id / path.name).write_bytes(path.read_bytes())
        again = refresh(root, base_id, prices_id, refreshed_id)
        for name in again["processed_hashes"]:
            if (root / refreshed_id / name).read_bytes() != (
                processed_root / refreshed_id / name
            ).read_bytes():
                raise RefreshRefused(f"다시 만든 {name}이 다릅니다.")
        for key in (
            "sources",
            "eligibility_basis",
            "catalog_cards",
            "cards_by_club",
            "excluded",
            "processed_hashes",
        ):
            if again[key] != report[key]:
                raise RefreshRefused(f"다시 만든 보고서의 {key}가 다릅니다.")
    return dict(
        refreshed_id=refreshed_id,
        verified=True,
        base=base_id,
        prices=prices_id,
        catalog_cards=report["catalog_cards"],
        network_requests=0,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="능력치·급여를 쓸 완료 배치 ID")
    parser.add_argument("--prices", help="가격만 새로 받은 실행 ID(--prices-only)")
    parser.add_argument("--refreshed-id", required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--run", action="store_true", help="파생 배치 파일을 쓴다")
    action.add_argument(
        "--verify",
        action="store_true",
        help="이미 만든 파생 배치를 다시 만들어 대조한다",
    )
    args = parser.parse_args(argv)
    processed_root = args.root / "data/processed"
    if not args.verify and not (args.base and args.prices):
        parser.error("--base와 --prices를 모두 명시해야 합니다.")
    try:
        if args.verify:
            result = verify_refreshed(processed_root, args.refreshed_id)
        elif args.run:
            result = refresh(processed_root, args.base, args.prices, args.refreshed_id)
        else:
            result = plan(processed_root, args.base, args.prices, args.refreshed_id)
    except (RefreshRefused, CatalogStopped) as error:
        print(json.dumps(dict(error=str(error)), ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
