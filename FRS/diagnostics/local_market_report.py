"""저장된 로컬 자료만으로 가격 관측과 클럽 경력 재고를 만든다."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from FRS.diagnostics.coverage import build_history_coverage
from FRS.schema.models import CardSalary, PlayerCard, TeamColor
from FRS.schema.scope import LOCAL_PATHS
from FRS.utils.crawling.ability_parser import ParseContext, ParseError
from FRS.utils.crawling.collect_cards import COLLECTION_ID
from FRS.utils.crawling.pilot import PILOT_ID, write_json
from FRS.utils.crawling.price_parser import parse_price
from FRS.utils.crawling.verify_collection import verify as verify_collection
from FRS.utils.crawling.verify_pilot import read_jsonl
from FRS.utils.crawling.verify_pilot import verify as verify_pilot

INSPECTION_ID = "stage-1-d-inspection-20261018"
PRICE_URL = "https://fconline.nexon.com/datacenter/PlayerPriceGraph"


def load_price_observations(raw_path: Path):
    manifest = json.loads((raw_path / "manifest.json").read_text(encoding="utf-8"))
    output = []
    for entry in manifest["responses"]:
        if entry["url"] != PRICE_URL or entry["status"] != 200:
            continue
        form = entry["form"]
        if not isinstance(form, dict) or set(form) != {"spid", "n1strong"}:
            raise ParseError("가격 요청의 식별 기록이 없습니다.")
        if Path(entry["path"]).name != entry["path"]:
            raise ParseError("원본 경로가 유효하지 않습니다.")
        body = (raw_path / entry["path"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise ParseError("가격 원본 해시가 변경되었습니다.")
        context = ParseContext(
            batch_id=raw_path.name,
            source="nexon_web",
            observed_at=entry["observed_at"],
            delete_after=None,
            spid=form["spid"],
            reinforce_level=form["n1strong"],
            version=entry["sha256"],
        )
        output.append(parse_price(body, context))
    return output


def build_local_report(
    raw_root: Path, processed_root: Path, output_dir: Path, *, when: datetime
):
    # 기존 완료 데이터의 해시·스키마·원본 대조를 통과한 뒤 집계한다. 네트워크는 없다.
    verification = {
        PILOT_ID: verify_pilot(raw_root / PILOT_ID, processed_root / PILOT_ID),
        COLLECTION_ID: verify_collection(
            raw_root / COLLECTION_ID, processed_root / COLLECTION_ID
        ),
    }
    pilot = processed_root / PILOT_ID
    cards = read_jsonl(pilot / "player_card.jsonl", PlayerCard)
    salaries = read_jsonl(pilot / "card_salary.jsonl", CardSalary)
    colors = read_jsonl(pilot / "team_color.jsonl", TeamColor)
    collection = json.loads(
        (raw_root / COLLECTION_ID / "manifest.json").read_text(encoding="utf-8")
    )
    for spid in collection["completed_ids"]:
        payload = json.loads(
            (processed_root / COLLECTION_ID / f"{spid}.json").read_text(
                encoding="utf-8"
            )
        )
        cards.extend(
            PlayerCard.model_validate(value) for value in payload["player_card"]
        )
        salaries.extend(
            CardSalary.model_validate(value) for value in payload["card_salary"]
        )
        colors.extend(
            TeamColor.model_validate(value) for value in payload["team_color"]
        )
    report = build_history_coverage(cards, salaries, colors, when=when)
    report["input_verification"] = verification
    prices = load_price_observations(raw_root / INSPECTION_ID)
    report["price_observations_parsed"] = len(prices)
    report["price_meaning"] = "unknown"
    inspection = json.loads(
        (raw_root / INSPECTION_ID / "manifest.json").read_text(encoding="utf-8")
    )
    report["inspection"] = {
        "run_id": INSPECTION_ID,
        "status": inspection["status"],
        "attempts": inspection["attempts"],
    }
    report["stage_1_d_complete"] = False
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "coverage.json", report)
    target = output_dir / "price_observations.jsonl"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        "".join(price.model_dump_json() + "\n" for price in prices), encoding="utf-8"
    )
    temporary.replace(target)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=LOCAL_PATHS["processed"] / "stage-1-d-local-report",
    )
    parser.add_argument(
        "--at", type=datetime.fromisoformat, help="시간대가 포함된 ISO 8601 집계 시각"
    )
    options = parser.parse_args()
    report = build_local_report(
        LOCAL_PATHS["raw"],
        LOCAL_PATHS["processed"],
        options.output,
        when=options.at or datetime.now(timezone.utc),
    )
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"cells", "input_verification"}
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
