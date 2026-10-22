"""실수집 결과 MongoDB 적재기를 합성 실행 결과와 메모리 DB 대역으로 검증한다."""

import copy
import json

import pytest
from test_real_material import RUN_ID, finished  # noqa: F401  (fixture)

from FRS.agent.real_material import ObservedMaterial, RealMaterialError
from FRS.core.candidate_search import CandidateQuery
from FRS.core.candidate_search.mongo import MongoCandidateRepository
from FRS.utils.db.load_observed import (
    LOAD_LOG,
    LoadRefused,
    load_observed_run,
    load_plan,
    main,
)


def _matches(document, selector):
    return all(document.get(k) == v for k, v in selector.items())


class FakeCollection:
    def __init__(self, fail_on_insert=False):
        self.documents, self.indexes = [], []
        self.fail_on_insert = fail_on_insert

    def find(self, selector):
        return [copy.deepcopy(d) for d in self.documents if _matches(d, selector)]

    def count_documents(self, selector, limit=0):
        found = len(self.find(selector))
        return min(found, limit) if limit else found

    def insert_many(self, documents):
        if self.fail_on_insert:
            self.documents.extend(copy.deepcopy(documents[:1]))
            raise RuntimeError("synthetic insert failure")
        self.documents.extend(copy.deepcopy(documents))

    def insert_one(self, document):
        self.documents.append(copy.deepcopy(document))

    def delete_many(self, selector):
        self.documents = [d for d in self.documents if not _matches(d, selector)]

    def create_index(self, keys, name):
        self.indexes.append((tuple(keys), name))
        return name


class FakeDatabase(dict):
    def __missing__(self, name):
        self[name] = FakeCollection()
        return self[name]

    def with_options(self, **_):
        return self


def test_plan_counts_without_database(finished):  # noqa: F811
    plan = load_plan(finished, RUN_ID)
    assert plan["counts"] == {
        "player_card": 1,
        "card_salary": 7,
        "card_price": 7,
        "reinforce_stats": 6,
        "team_color": 1,
    }


def test_load_writes_every_collection_and_round_trips(finished):  # noqa: F811
    database = FakeDatabase()
    record = load_observed_run(database, finished, RUN_ID)
    assert record["counts"]["card_price"] == 7
    assert record["eligibility_basis"] == "club_career_search"
    assert database[LOAD_LOG].count_documents({"run_id": RUN_ID}) == 1
    assert all(c.indexes for n, c in database.items() if n != LOAD_LOG)
    # 저장한 문서가 공급기 결과와 같은 모델로 복원된다.
    expected = ObservedMaterial(finished, RUN_ID).load(
        ObservedMaterial(finished, RUN_ID).batches
    )
    from FRS.schema.models import DOCUMENT_MODELS

    cards = [
        DOCUMENT_MODELS["player_card"].from_document(d)
        for d in database["player_card"].find({"batch_id": RUN_ID})
    ]
    assert tuple(cards) == expected["cards"]


def test_loaded_batch_is_searchable_by_the_existing_repository(finished):  # noqa: F811
    database = FakeDatabase()
    load_observed_run(database, finished, RUN_ID)
    material = ObservedMaterial(finished, RUN_ID)
    card = material.load(material.batches)["cards"][0]
    query = CandidateQuery(
        position=card.positions[0],
        salary_cap=99,
        price_cap=10**30,
        reinforce_level=5,
        team_color="가상클럽",
        batches=material.batches,
        when=card.observed_at,
    )
    result = MongoCandidateRepository(database, supported_clubs=("가상클럽",)).search(
        query
    )
    assert not result.request_errors
    assert [c.card.spid for c in result.candidates] == [card.spid]


def test_second_load_of_the_same_run_is_refused(finished):  # noqa: F811
    database = FakeDatabase()
    load_observed_run(database, finished, RUN_ID)
    before = len(database["card_price"].documents)
    with pytest.raises(LoadRefused):
        load_observed_run(database, finished, RUN_ID)
    assert len(database["card_price"].documents) == before


def test_partial_failure_removes_only_this_run(finished):  # noqa: F811
    database = FakeDatabase()
    database["card_price"].insert_one({"batch_id": "other-batch", "spid": 1})
    database["card_price"].fail_on_insert = True
    with pytest.raises(RuntimeError):
        load_observed_run(database, finished, RUN_ID)
    for name in ("player_card", "card_salary", "card_price"):
        assert database[name].count_documents({"batch_id": RUN_ID}) == 0
    assert database["card_price"].count_documents({"batch_id": "other-batch"}) == 1
    assert database[LOAD_LOG].count_documents({}) == 0


def test_tampered_files_are_never_loaded(finished):  # noqa: F811
    path = finished / RUN_ID / "team_color.jsonl"
    path.write_text(path.read_text().replace("가상클럽", "다른클럽"))
    database = FakeDatabase()
    with pytest.raises(RealMaterialError):
        load_observed_run(database, finished, RUN_ID)
    assert not any(c.documents for c in database.values())


def test_cli_defaults_to_plan_and_never_connects(finished, capsys, monkeypatch):  # noqa: F811
    import FRS.utils.db.client as client

    monkeypatch.setattr(
        client, "Client", lambda *a, **k: pytest.fail("DB에 연결하면 안 됩니다.")
    )
    root = finished.parent / "root"
    (root / "data").mkdir(parents=True)
    (root / "data" / "processed").symlink_to(finished)
    assert main(["--run-id", RUN_ID, "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["player_card"] == 1


def test_every_document_survives_a_bson_round_trip(finished):  # noqa: F811
    """실제 MongoDB 없이 BSON 인코딩·tz-aware 복원을 확인한다."""
    import bson
    from bson.codec_options import CodecOptions

    from FRS.schema.models import DOCUMENT_MODELS
    from FRS.utils.db.load_observed import COLLECTIONS

    material = ObservedMaterial(finished, RUN_ID)
    options = CodecOptions(tz_aware=True)
    for key, rows in material.load(material.batches).items():
        model = DOCUMENT_MODELS[COLLECTIONS[key]]
        for row in rows:
            decoded = bson.decode(bson.encode(row.to_document()), codec_options=options)
            assert model.from_document(decoded) == row
