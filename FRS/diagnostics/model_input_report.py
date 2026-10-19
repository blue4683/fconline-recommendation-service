"""명시한 완료 배치의 원본 대조 후 원시 입력·라벨 계약만 검증한다."""

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from FRS.core.position_recommend.data_contract import (
    InputContractError,
    InputSelection,
    ObservationKey,
    feature_spec,
    to_labeled_input,
    to_model_input,
)
from FRS.schema.models import PlayerCard
from FRS.utils.crawling.collect_cards import COLLECTION_ID
from FRS.utils.crawling.enrich_cards import RUN_ID, SOURCE_ID, source_selection
from FRS.utils.crawling.pilot import PILOT_ID, ROOT
from FRS.utils.crawling.verify_collection import verify as verify_collection
from FRS.utils.crawling.verify_enrichment import verify as verify_enrichment
from FRS.utils.crawling.verify_pilot import verify as verify_pilot

SUPPORTED_BATCHES = (PILOT_ID, COLLECTION_ID, RUN_ID)


@dataclass(frozen=True)
class VerifiedCards:
    cards: tuple[PlayerCard, ...]
    selection: InputSelection
    source_hashes: dict


def _fingerprints(raw, processed):
    paths = [
        raw / "manifest.json",
        *sorted(processed.glob("*.json")),
        *sorted(processed.glob("*.jsonl")),
    ]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def load_verified_cards(root: Path, *, batch_ids) -> VerifiedCards:
    """허용한 기존 실행의 완료/출처/원본/가공을 대조한다. 파일은 수정하지 않는다."""
    batch_ids = tuple(batch_ids)
    if (
        not batch_ids
        or len(batch_ids) != len(set(batch_ids))
        or any(batch not in SUPPORTED_BATCHES for batch in batch_ids)
    ):
        raise InputContractError("지원하는 완료 배치 ID를 중복 없이 명시해야 합니다.")
    cards, hashes = [], {}
    for batch in batch_ids:
        raw, processed = root / "data/raw" / batch, root / "data/processed" / batch
        before = _fingerprints(raw, processed)
        state = json.loads((raw / "manifest.json").read_text(encoding="utf-8"))
        if state.get("status") != "completed" or state.get("pilot_id") != batch:
            raise InputContractError("명시한 배치가 완료되지 않았습니다.")
        if batch == PILOT_ID:
            verify_pilot(raw, processed)
            values = [
                PlayerCard.model_validate_json(line)
                for line in (processed / "player_card.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
        else:
            if batch == COLLECTION_ID:
                verify_collection(raw, processed)
            else:
                verify_enrichment(
                    raw,
                    processed,
                    expected_selection=source_selection(root / "data/raw" / SOURCE_ID),
                )
            values = []
            for spid in state["selected_ids"]:
                payload = json.loads(
                    (processed / f"{spid}.json").read_text(encoding="utf-8")
                )
                values.extend(
                    PlayerCard.model_validate(value) for value in payload["player_card"]
                )
        if _fingerprints(raw, processed) != before:
            raise InputContractError(
                "검증 중 원본 manifest 또는 가공 파일이 바뀌었습니다."
            )
        if not values or any(card.batch_id != batch for card in values):
            raise InputContractError("기본 카드의 배치가 다릅니다.")
        cards.extend(values)
        # 경로/데이터는 출력하지 않고 파일 집합과 manifest의 해시만 보관한다.
        relative = {
            str(Path(path).relative_to(root)): value for path, value in before.items()
        }
        encoded = json.dumps(relative, sort_keys=True, separators=(",", ":")).encode()
        hashes[batch] = dict(
            manifest_sha256=before[str(raw / "manifest.json")],
            processed_file_set_sha256=hashlib.sha256(encoded).hexdigest(),
        )
    selection = InputSelection(
        observations=tuple(
            ObservationKey(spid=c.spid, batch_id=c.batch_id, version=c.version)
            for c in cards
        ),
        base_reinforce_level=5,
    )
    return VerifiedCards(tuple(cards), selection, hashes)


def report(verified: VerifiedCards):
    X = to_model_input(verified.cards, selection=verified.selection)
    labeled = to_labeled_input(verified.cards, selection=verified.selection)
    encoded = json.dumps(
        [card.model_dump(mode="json") for card in verified.cards],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return dict(
        stage="2-0-input-contract",
        cards=len(X),
        labeled_rows=len(labeled.X),
        feature_spec=feature_spec(),
        classes=sorted(set(labeled.y)),
        class_row_counts=dict(Counter(labeled.y)),
        card_groups=labeled.card_groups.nunique(),
        persons=labeled.metadata["person_id"].nunique(),
        source_status_counts=dict(Counter(c.status.value for c in verified.cards)),
        source_hashes=verified.source_hashes,
        input_cards_sha256=hashlib.sha256(encoded).hexdigest(),
        base_reinforce_level=verified.selection.base_reinforce_level,
        observations_modified=False,
        network_requests_sent=0,
        training_executed=False,
        split_performed=False,
        model_loaded=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-id", choices=SUPPORTED_BATCHES, nargs="+", required=True
    )
    args = parser.parse_args()
    try:
        result = report(load_verified_cards(ROOT, batch_ids=args.batch_id))
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
