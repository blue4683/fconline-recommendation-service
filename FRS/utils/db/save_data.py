"""기존 형식의 선수 파일을 명시적으로 적재한다. import 시 실행하지 않는다."""

import argparse
import json
from pathlib import Path

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from FRS.utils.db.client import Client

DEFAULT_DATA_PATH = (
    Path(__file__).resolve().parents[1] / "data/player_overall_data.json"
)


def save_players(players, collection):
    collection.create_index([("id", ASCENDING)], unique=True)
    inserted, duplicates = 0, 0
    for player_id, data in players.items():
        document = {**data, "id": str(player_id)}
        try:
            collection.insert_one(document)
        except DuplicateKeyError:
            duplicates += 1
        else:
            inserted += 1
    return {"inserted": inserted, "duplicates": duplicates}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="기존 선수 파일을 MongoDB에 적재합니다."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_DATA_PATH)
    args = parser.parse_args(argv)
    with args.input.open(encoding="utf-8") as source:
        players = json.load(source)
    with Client() as client:
        result = save_players(players, client.db.player_data)
    print(f"저장 {result['inserted']}건, 중복 {result['duplicates']}건")


if __name__ == "__main__":
    main()
