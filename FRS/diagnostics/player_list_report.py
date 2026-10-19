"""완료/중단한 소량 진단의 저장 응답을 검증한다. HTTP·DB 요청은 하지 않는다."""

import argparse
import hashlib
import json
from pathlib import Path

from FRS.utils.crawling.player_list_parser import PlayerListContext, parse_player_list

LIST_URL = "https://fconline.nexon.com/datacenter/PlayerList"


def saved_rows(root: Path):
    """해시와 요청 provenance가 확인된 목록만 원래 관측 시각으로 읽는다."""
    root = root.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] not in {"completed", "stopped"}:
        raise ValueError("진행 중이거나 상태가 미확인인 실행은 보고하지 않습니다.")
    results = []
    for entry in manifest["responses"]:
        if entry["url"] != LIST_URL:
            continue
        if entry["method"] != "POST" or entry["status"] != 200:
            raise ValueError("성공한 목록 POST 응답이 아닙니다.")
        path = (root / entry["path"]).resolve()
        if path.parent != root:
            raise ValueError("원본 경로가 실행 디렉터리를 벗어납니다.")
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError("저장된 원본 해시가 다릅니다.")
        form = entry["form"]
        context = PlayerListContext(
            batch_id=manifest["run_id"],
            source="nexon_web",
            observed_at=entry["observed_at"],
            delete_after=manifest["delete_after"],
            version=digest,
            reinforce_level=int(form["n1Strong"]),
            skill_fields=tuple(form[f"strSkill{i}"] for i in range(1, 5)),
        )
        results.append((entry, parse_player_list(body, context)))
    if not results:
        raise ValueError("검증할 목록 응답이 없습니다.")
    return results


def summary(results):
    all_rows = [row for _, rows in results for row in rows]
    return {
        "response_count": len(results),
        "row_observations": len(all_rows),
        "distinct_cards": len({row.spid for row in all_rows}),
        # 같은 카드가 여러 쿼리에 나와도 고유 가격 개수로 중복 집계하지 않는다.
        "distinct_card_price_keys": len(
            {(p.spid, p.reinforce_level) for row in all_rows for p in row.prices}
        ),
        "complete": False,
        "status": "unknown",
        "responses": [
            {
                "label": entry["label"],
                "rows": len(rows),
                "salary_min": min(row.salary for row in rows),
                "salary_max": max(row.salary for row in rows),
                "multiple_listed_positions": sum(
                    len(row.listed_position_ovr) > 1 for row in rows
                ),
                "unknown_prices": sum(
                    price.price is None for row in rows for price in row.prices
                ),
            }
            for entry, rows in results
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            summary(saved_rows(args.run_directory)), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
