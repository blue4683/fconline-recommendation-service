"""선택 실행: 별도 Compose 프로젝트에서 합성 DB·모델로 API를 검증하고 정리한다."""

import json
import os
import secrets
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import requests
from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError

from FRS.core.position_recommend.artifacts import load_bundle
from FRS.core.position_recommend.data_contract import (
    STAT_FEATURES,
    InputSelection,
    ObservationKey,
)
from FRS.core.position_recommend.train import train_verified
from FRS.diagnostics.model_input_report import VerifiedCards
from FRS.schema.models import PlayerCard

ROOT = Path(__file__).resolve().parents[1]


def synthetic_cards():
    observed = datetime.now(timezone.utc) - timedelta(seconds=1)
    return tuple(
        PlayerCard(
            batch_id="synthetic-api-batch",
            source="synthetic",
            observed_at=observed,
            delete_after=None,
            status="ok",
            valid_until=observed + timedelta(hours=1),
            spid=123000001 + i,
            version="synthetic-v1",
            season_id=123,
            person_id=1 + i,
            name=f"가상선수_{i:04d}",
            base_reinforce_level=5,
            positions=[("CF", "ST", "GK")[i % 3]],
            stats={key: 10 + j + i % 3 for j, key in enumerate(STAT_FEATURES)},
            height=180,
            weight=70,
            physical="가상체형_A",
            dribble_skill=4,
            left_foot=3,
            right_foot=5,
        )
        for i in range(30)
    )


def selection(cards):
    return InputSelection(
        observations=tuple(
            ObservationKey(spid=c.spid, batch_id=c.batch_id, version=c.version)
            for c in cards
        ),
        base_reinforce_level=5,
    )


def main():
    project = "frs-api-validation-" + uuid4().hex[:12]
    output = ROOT / "artifacts/models" / project
    output.mkdir(parents=True, exist_ok=False)
    cards = synthetic_cards()
    verified = VerifiedCards(
        cards, selection(cards), {"synthetic-api-batch": {"manifest_sha256": "a" * 64}}
    )
    trained = train_verified(verified, output)
    version = trained["model_version"]
    environment = {
        **os.environ,
        "MONGO_USERNAME": "synthetic-validator",
        "MONGO_PASSWORD": secrets.token_urlsafe(32),
        "MONGO_DATABASE": "frs_api_validation",
        "MONGO_PORT": "0",
        "API_PORT": "0",
        "API_MODEL_DIR": f"/models/{project}/bundle",
        "API_MODEL_VERSION": version,
        "MONGO_EXPRESS_USERNAME": "synthetic-validator",
        "MONGO_EXPRESS_PASSWORD": secrets.token_urlsafe(32),
        "FRS_API_VALIDATION_READ_PASSWORD": secrets.token_urlsafe(32),
    }
    client = None
    with tempfile.TemporaryDirectory(prefix="frs-api-compose-") as temporary:
        override = Path(temporary) / "override.json"
        override.write_text(
            json.dumps(
                {
                    "services": {
                        "api": {
                            "environment": {
                                "MONGO_USERNAME": "synthetic-api-reader",
                                "MONGO_PASSWORD": "${FRS_API_VALIDATION_READ_PASSWORD:?}",
                            }
                        }
                    }
                }
            )
        )
        env_file = Path(temporary) / "empty.env"
        env_file.touch()
        command = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT / "docker-compose.yml"),
            "-f",
            str(override),
        ]

        def compose(*args):
            return subprocess.run(
                command + list(args),
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            ).stdout

        try:
            compose("up", "-d", "--no-build", "mongodb")
            mongo_port = int(
                compose("port", "mongodb", "27017").strip().rsplit(":", 1)[1]
            )
            client = MongoClient(
                "127.0.0.1",
                mongo_port,
                username=environment["MONGO_USERNAME"],
                password=environment["MONGO_PASSWORD"],
                authSource="admin",
                tz_aware=True,
                serverSelectionTimeoutMS=500,
                connectTimeoutMS=500,
            )
            deadline = time.monotonic() + 60
            while True:
                try:
                    client.admin.command("ping")
                    break
                except PyMongoError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("검증 MongoDB 준비 시간 초과") from None
                    time.sleep(0.5)
            client.admin.command(
                "createUser",
                "synthetic-api-reader",
                pwd=environment["FRS_API_VALIDATION_READ_PASSWORD"],
                roles=[{"role": "read", "db": "frs_api_validation"}],
            )
            database = client.frs_api_validation
            database.player_card.insert_many([c.to_document() for c in cards])
            unknown = PlayerCard.model_validate(
                {
                    **cards[0].model_dump(),
                    "version": "synthetic-unknown",
                    "status": "unknown",
                    "valid_until": None,
                }
            )
            database.player_card.insert_one(unknown.to_document())
            before = list(database.player_card.find())
            compose("up", "-d", "--no-build", "--no-deps", "api")
            port = int(compose("port", "api", "8000").strip().rsplit(":", 1)[1])
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 60
            session = requests.Session()
            session.trust_env = False
            while True:
                try:
                    response = session.get(base + "/health", timeout=5)
                    if response.status_code == 200:
                        break
                except requests.ConnectionError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("검증 API 준비 시간 초과")
                time.sleep(0.5)
            chosen = cards[:2]
            for c in chosen:
                response = session.get(
                    base + f"/cards/{c.spid}",
                    params={"batch_id": c.batch_id, "version": c.version},
                    timeout=10,
                )
                assert response.status_code == 200 and response.json()["usable"]
                assert PlayerCard.model_validate(response.json()["card"]) == c
            body = dict(
                model_version=version,
                selection=selection(chosen).model_dump(mode="json"),
            )
            response = session.post(base + "/recommend/position", json=body, timeout=10)
            assert response.status_code == 200
            predictor = load_bundle(output / "bundle", expected_model_version=version)
            expected = predictor.predict_proba(
                [c.spid for c in chosen], cards=chosen, selection=selection(chosen)
            )
            assert response.json() == expected.model_dump(mode="json")
            assert (
                session.get(
                    base + f"/cards/{chosen[0].spid}",
                    params={"batch_id": "missing", "version": chosen[0].version},
                    timeout=10,
                ).status_code
                == 404
            )
            assert (
                session.post(
                    base + "/recommend/position",
                    json={**body, "model_version": "f" * 64},
                    timeout=10,
                ).status_code
                == 409
            )
            assert (
                session.post(
                    base + "/recommend/position",
                    json={
                        **body,
                        "selection": selection([unknown]).model_dump(mode="json"),
                    },
                    timeout=10,
                ).status_code
                == 409
            )
            assert list(database.player_card.find()) == before
            with MongoClient(
                "127.0.0.1",
                mongo_port,
                username="synthetic-api-reader",
                password=environment["FRS_API_VALIDATION_READ_PASSWORD"],
                authSource="admin",
            ) as reader:
                try:
                    reader.frs_api_validation.player_card.insert_one(
                        {"unexpected": True}
                    )
                except OperationFailure as error:
                    assert error.code == 13
                else:
                    raise AssertionError("읽기 전용 검증 계정의 쓰기가 허용됐습니다.")
            summary = dict(
                status="completed",
                model_version=version,
                synthetic_cards=len(cards),
                database_unchanged=True,
                container_host_probabilities_identical=True,
                read_only_role_enforced=True,
                nexon_requests=0,
            )
            (output / "compose-verification.json").write_text(
                json.dumps(summary, indent=2) + "\n"
            )
            print(json.dumps(summary))
        finally:
            if client is not None:
                client.close()
            compose("down", "--volumes", "--remove-orphans")
            print("이번 검증 전용 Compose 컨테이너·네트워크·볼륨 정리 완료")


if __name__ == "__main__":
    main()
