"""원본은 합성 관측 형식, DB는 읽기 전용 대역이다. 외부 요청은 없다."""

import copy
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from pymongo.errors import AutoReconnect
from test_load_observed import FakeDatabase
from test_real_material import RUN_ID, finished, selection

from FRS.agent.mongo_material import MongoObservedMaterial, local_client
from FRS.agent.real_material import ObservedMaterial, RealMaterialError
from FRS.utils.db.client import DatabaseConfig
from FRS.utils.db.load_observed import LOAD_LOG, load_observed_run


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def limit(self, number):
        self.rows = self.rows[:number]
        return self

    def __iter__(self):
        return iter(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class ReadOnlyDatabase:
    """공급기에 find/with_options만 노출한다. 쓰기·인덱스 접근이면 실패한다."""

    def __init__(self, underlying):
        self.underlying = underlying
        self.reads = []
        self.cursors = []

    def with_options(self, *, codec_options):
        assert codec_options.tz_aware is True
        return self

    def __getitem__(self, name):
        def find(selector):
            self.reads.append((name, dict(selector)))
            cursor = Cursor(self.underlying[name].find(selector))
            self.cursors.append(cursor)
            return cursor

        return type("ReadOnlyCollection", (), {"find": staticmethod(find)})()


class Connection:
    def __init__(self, database):
        self.db = ReadOnlyDatabase(database)
        self.opens = 0
        self.closes = 0

    @contextmanager
    def factory(self):
        self.opens += 1
        try:
            yield self
        finally:
            self.closes += 1


@pytest.fixture
def loaded(tmp_path):
    root = finished.__wrapped__(tmp_path)
    database = FakeDatabase()
    load_observed_run(database, root, RUN_ID)
    connection = Connection(database)
    material = MongoObservedMaterial(root, RUN_ID, client_factory=connection.factory)
    return root, database, connection, material


def test_returns_all_db_rows_and_preserves_unknown_and_large_bp(loaded):
    root, database, _, _ = loaded
    # 두 수정은 원본/적재기를 함께 거쳐 공급기 기준을 새로 만든다.
    import hashlib
    import json

    path = root / RUN_ID
    rows = [
        json.loads(line)
        for line in (path / "card_price.jsonl").read_text().splitlines()
    ]
    rows[0].update(status="unknown", valid_until=None, price=None)
    rows[1]["price"] = 10**33 + 123456789
    raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
    (path / "card_price.jsonl").write_bytes(raw)
    report = json.loads((path / "report.json").read_text())
    report["processed_hashes"]["card_price.jsonl"] = hashlib.sha256(raw).hexdigest()
    (path / "report.json").write_text(json.dumps(report))
    database = FakeDatabase()
    load_observed_run(database, root, RUN_ID)
    connection = Connection(database)
    material = MongoObservedMaterial(root, RUN_ID, client_factory=connection.factory)
    expected = ObservedMaterial(root, RUN_ID)
    for collection in database.values():
        collection.documents.reverse()
    data = material.load(material.batches)
    for key, values in expected.load(expected.batches).items():
        assert sorted(map(repr, data[key])) == sorted(map(repr, values))
    assert sum(row.status == "unknown" for row in data["prices"]) == 1
    assert max(row.price or 0 for row in data["prices"]) == 10**33 + 123456789
    assert material.material_id == expected.material_id
    assert connection.opens == connection.closes == 1
    assert all(cursor.closed for cursor in connection.db.cursors)
    assert all(
        q == {"run_id": RUN_ID} if name == LOAD_LOG else q == {"batch_id": RUN_ID}
        for name, q in connection.db.reads
    )


@pytest.mark.parametrize(
    "change", ["missing", "extra", "price", "expiry", "source", "invalid"]
)
def test_db_loss_duplication_or_tampering_is_refused_and_connection_closed(
    loaded, change
):
    _, database, connection, material = loaded
    rows = database["card_price"].documents
    if change == "missing":
        rows.pop()
    elif change == "extra":
        rows.append(copy.deepcopy(rows[0]))
    elif change == "price":
        rows[0]["price"] = 1
    elif change == "expiry":
        from datetime import timedelta

        rows[0]["valid_until"] += timedelta(days=1)
    elif change == "source":
        rows[0]["source"] = "synthetic"
    else:
        rows[0]["price"] = "bad"
    with pytest.raises(ValueError):
        material.load(material.batches)
    assert connection.opens == connection.closes == 1
    assert all(cursor.closed for cursor in connection.db.cursors)


@pytest.mark.parametrize("change", ["missing", "duplicate", "id", "hash", "counts"])
def test_missing_or_inconsistent_audit_is_refused_before_material_reads(loaded, change):
    _, database, connection, material = loaded
    logs = database[LOAD_LOG].documents
    if change == "missing":
        logs.clear()
    elif change == "duplicate":
        logs.append(copy.deepcopy(logs[0]))
    elif change == "id":
        logs[0]["material_id"] = "0" * 64
    elif change == "hash":
        logs[0]["processed_hashes"]["card_price.jsonl"] = "0" * 64
    else:
        logs[0]["counts"]["player_card"] = 2
    with pytest.raises(RealMaterialError):
        material.load(material.batches)
    assert all(name == LOAD_LOG for name, _ in connection.db.reads)
    assert connection.opens == connection.closes == 1


def test_material_is_checked_on_every_load_and_never_replaced_from_files(loaded):
    _, database, connection, material = loaded
    material.load(material.batches)
    database["team_color"].documents.clear()
    with pytest.raises(RealMaterialError):
        material.load(material.batches)
    assert connection.opens == connection.closes == 2


def test_batch_mismatch_or_file_change_does_not_open_database(loaded):
    root, _, connection, material = loaded
    with pytest.raises(RealMaterialError):
        material.load(selection("real-data-other"))
    with pytest.raises(TypeError):
        material.load(material.batches.model_dump())
    path = root / RUN_ID / "card_price.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(RealMaterialError):
        material.load(material.batches)
    assert connection.opens == 0


def test_changed_report_is_rejected_before_db_access(loaded):
    root, _, connection, material = loaded
    path = root / RUN_ID / "report.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(RealMaterialError):
        material.load(material.batches)
    assert connection.opens == 0


def test_driver_failure_is_safe_without_retry_or_fallback(loaded):
    root, _, _, _ = loaded
    factory = Mock(side_effect=AutoReconnect("private password/path"))
    material = MongoObservedMaterial(root, RUN_ID, client_factory=factory)
    with pytest.raises(RealMaterialError) as exc:
        material.load(material.batches)
    assert "private" not in str(exc.value)
    factory.assert_called_once_with()


def test_driver_disconnect_during_read_closes_the_connection_without_retry(loaded):
    _, database, connection, material = loaded
    read = Mock(side_effect=AutoReconnect("private password/path"))
    database["card_price"].find = read
    with pytest.raises(RealMaterialError) as exc:
        material.load(material.batches)
    assert "private" not in str(exc.value)
    assert connection.opens == connection.closes == 1
    read.assert_called_once_with({"batch_id": RUN_ID})


def test_audit_removal_during_read_is_rejected(loaded):
    _, database, connection, material = loaded
    original = database["team_color"].find

    def read(query):
        rows = original(query)
        database[LOAD_LOG].documents.clear()
        return rows

    database["team_color"].find = read
    with pytest.raises(RealMaterialError):
        material.load(material.batches)
    assert connection.opens == connection.closes == 1


def test_other_batches_are_not_selected(loaded):
    _, database, _, material = loaded
    row = copy.deepcopy(database["player_card"].documents[0])
    row["batch_id"] = "real-data-newer"
    database["player_card"].documents.append(row)
    assert len(material.load(material.batches)["cards"]) == 1


def test_default_connection_rejects_remote_and_disables_driver_retry(monkeypatch):
    config = DatabaseConfig("remote.example", 27017, "synthetic", "synthetic")
    monkeypatch.setattr(DatabaseConfig, "from_env", lambda: config)
    factory = Mock()
    monkeypatch.setattr("FRS.agent.mongo_material.Client", factory)
    with pytest.raises(RealMaterialError):
        local_client()
    factory.assert_not_called()
    config = DatabaseConfig("127.0.0.1", 27017, "synthetic", "synthetic")
    local_client()
    factory.assert_called_once_with(config=config, retry_reads=False)
