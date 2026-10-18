"""선택 실행: 캐시된 MongoDB 이미지의 임시 컨테이너에서 합성 검색을 검증한다."""

import json
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from FRS.core.candidate_search import BatchSelection, CandidateQuery, candidate_search
from FRS.core.candidate_search.mongo import (
    MongoCandidateRepository,
    create_candidate_indexes,
)
from FRS.schema.models import (
    CardPrice,
    CardSalary,
    PlayerCard,
    ReinforceStats,
    TeamColor,
)


def synthetic_data():
    observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    batches = BatchSelection(
        **{name: "synthetic-" + name for name in BatchSelection.model_fields}
    )
    common = dict(
        source="synthetic",
        observed_at=observed,
        valid_until=observed + timedelta(hours=1),
        delete_after=None,
        status="ok",
        spid=123000001,
    )
    card = PlayerCard(
        **common,
        batch_id=batches.player_card,
        version="base",
        season_id=123,
        person_id=1,
        name="가상선수_0001",
        base_reinforce_level=5,
        positions=["ST"],
        stats={"speed": 100},
    )
    salaries = [
        CardSalary(
            **common,
            batch_id=batches.card_salary,
            version=version,
            reinforce_level=level,
            salary=20,
        )
        for level, version in ((5, "base"), (6, "enhanced"))
    ]
    prices = [
        CardPrice(
            **common,
            batch_id=batches.card_price,
            version="quote",
            reinforce_level=level,
            price_type="reference",
            price=10**34 - 1,
        )
        for level in (5, 6)
    ]
    reinforcement = ReinforceStats(
        **common,
        batch_id=batches.reinforce_stats,
        version="enhanced",
        reinforce_level=6,
        baseline_reinforce_level=5,
        baseline_card_version="base",
        stats_delta={"speed": 3},
    )
    color = TeamColor(
        **common,
        batch_id=batches.team_color,
        version="eligibility",
        type="club",
        name="가상클럽_알파",
    )
    return (
        batches,
        observed,
        dict(
            cards=[card],
            salaries=salaries,
            prices=prices,
            reinforcements=[reinforcement],
            team_colors=[color],
        ),
    )


def verify(database):
    batches, when, data = synthetic_data()
    resources = dict(
        cards="player_card",
        salaries="card_salary",
        prices="card_price",
        reinforcements="reinforce_stats",
        team_colors="team_color",
    )
    for arg, resource in resources.items():
        database[resource].insert_many([row.to_document() for row in data[arg]])
    create_candidate_indexes(database)
    create_candidate_indexes(database)
    clubs = ("가상클럽_알파",)
    repository = MongoCandidateRepository(database, supported_clubs=clubs)
    q = CandidateQuery(
        position="ST",
        salary_cap=20,
        price_cap=10**34 - 1,
        team_color=clubs[0],
        reinforce_level=5,
        when=when,
        batches=batches,
    )
    for level in (5, 6):
        request = CandidateQuery(**{**q.model_dump(), "reinforce_level": level})
        actual = repository.search(request)
        assert actual == candidate_search(request, **data, supported_clubs=clubs)
        assert len(actual.candidates) == 1
        assert actual.candidates[0].price.price == 10**34 - 1
    expired = CandidateQuery(**{**q.model_dump(), "when": when + timedelta(hours=1)})
    assert repository.search(expired).exclusion_counts == {"player_card:expired": 1}
    unsupported = CandidateQuery(**{**q.model_dump(), "team_color": "가상클럽_미지원"})
    assert repository.search(unsupported).request_errors == ("unsupported_team_color",)
    # MongoDB _id만 다른 동일 관측은 후보를 중복시키지 않는다.
    database.card_price.insert_one(data["prices"][0].to_document())
    assert len(repository.search(q).candidates) == 1
    conflict = data["prices"][0].model_dump()
    conflict["price"] -= 1
    database.card_price.insert_one(CardPrice(**conflict).to_document())
    assert repository.search(q).exclusion_counts == {"card_price:conflict": 1}
    assert all(
        not database[name].index_information()["candidate_lookup"].get("unique", False)
        for name in resources.values()
    )
    print(
        "MongoDB 합성 통합 검증 통과: 5/6강 결합·34자리 BP·만료·미지원·중복·충돌·인덱스"
    )


def main():
    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    image = re.search(r"image: (mongo:[^\s]+)", compose.read_text()).group(1)
    name = "frs-candidate-validation-" + uuid4().hex[:12]
    username, password = "synthetic-validator", secrets.token_urlsafe(32)
    environment = {
        **os.environ,
        "MONGO_INITDB_ROOT_USERNAME": username,
        "MONGO_INITDB_ROOT_PASSWORD": password,
    }
    container, client = None, None
    try:
        # 이미지 다운로드·기존 DB/볼륨/포트 재사용을 하지 않는다.
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--detach",
                "--pull=never",
                "--name",
                name,
                "--publish",
                "127.0.0.1::27017",
                "--env",
                "MONGO_INITDB_ROOT_USERNAME",
                "--env",
                "MONGO_INITDB_ROOT_PASSWORD",
                image,
            ],
            env=environment,
            text=True,
        ).strip()
        details = json.loads(
            subprocess.check_output(["docker", "inspect", container], text=True)
        )[0]
        port = int(details["NetworkSettings"]["Ports"]["27017/tcp"][0]["HostPort"])
        client = MongoClient(
            "127.0.0.1",
            port,
            username=username,
            password=password,
            authSource="admin",
            tz_aware=True,
            serverSelectionTimeoutMS=500,
            connectTimeoutMS=500,
            socketTimeoutMS=5000,
        )
        deadline = time.monotonic() + 45
        while True:
            try:
                client.admin.command("ping")
                break
            except PyMongoError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("임시 MongoDB 준비 시간 초과") from None
                time.sleep(0.5)
        verify(client.frs_candidate_validation)
    finally:
        if client is not None:
            client.close()
        if container is not None:
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", container],
                check=True,
                capture_output=True,
            )
            print("임시 MongoDB 컨테이너·볼륨 정리 완료")


if __name__ == "__main__":
    main()
