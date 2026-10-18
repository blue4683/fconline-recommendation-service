import importlib
from unittest.mock import MagicMock, Mock

import pytest
from pymongo.errors import DuplicateKeyError, OperationFailure

from FRS.utils.db import client, save_data


def test_environment_config_uses_shared_names():
    config = client.DatabaseConfig.from_env(
        {
            "MONGO_HOST": "localhost",
            "MONGO_PORT": "27018",
            "MONGO_USERNAME": "synthetic-user",
            "MONGO_PASSWORD": "synthetic:@/$value",
            "MONGO_DATABASE": "synthetic_db",
        }
    )
    assert config.port == 27018
    assert config.database == "synthetic_db"
    assert "synthetic-user" not in repr(config)
    assert "synthetic:@/$value" not in repr(config)


@pytest.mark.parametrize("port", ["invalid", "0", "65536", ""])
def test_bad_port_is_rejected(port):
    with pytest.raises(ValueError, match="MONGO_PORT"):
        client.DatabaseConfig.from_env(
            {
                "MONGO_PORT": port,
                "MONGO_USERNAME": "synthetic",
                "MONGO_PASSWORD": "synthetic",
            }
        )


@pytest.mark.parametrize(
    "environment",
    [{}, {"MONGO_USERNAME": "synthetic"}, {"MONGO_PASSWORD": "synthetic"}],
)
def test_missing_credentials_fail_before_client_creation(environment, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(client, "MongoClient", factory)
    with pytest.raises(ValueError, match="MONGO_USERNAME"):
        client.DatabaseConfig.from_env(environment)
    factory.assert_not_called()


def test_client_handles_special_characters_and_closes_on_failure(monkeypatch):
    mongo = MagicMock()
    factory = Mock(return_value=mongo)
    monkeypatch.setattr(client, "MongoClient", factory)
    config = client.DatabaseConfig("localhost", 27017, "synthetic", "synthetic:@/$")
    with pytest.raises(RuntimeError):
        with client.Client("synthetic_db", config=config) as connection:
            assert connection.db is mongo["synthetic_db"]
            mongo.admin.command.assert_not_called()
            connection.ping()
            mongo.admin.command.assert_called_once_with("ping")
            raise RuntimeError("합성 실패")
    factory.assert_called_once_with(
        host="localhost",
        port=27017,
        username="synthetic",
        password="synthetic:@/$",
        authSource="admin",
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=10000,
        tz_aware=True,
        connect=False,
    )
    mongo.close.assert_called_once()


def test_default_database_comes_from_config(monkeypatch):
    mongo = MagicMock()
    monkeypatch.setattr(client, "MongoClient", Mock(return_value=mongo))
    config = client.DatabaseConfig(
        "localhost", 27017, "synthetic", "synthetic", "custom"
    )
    with client.Client(config=config):
        pass
    mongo.__getitem__.assert_called_once_with("custom")


def test_dotenv_load_is_explicit_and_does_not_override_environment(monkeypatch):
    loader = Mock()
    monkeypatch.setattr(client, "load_dotenv", loader)
    monkeypatch.setenv("MONGO_USERNAME", "synthetic")
    monkeypatch.setenv("MONGO_PASSWORD", "synthetic")
    monkeypatch.setenv("MONGO_PORT", "27017")
    monkeypatch.setenv("MONGO_HOST", "localhost")
    monkeypatch.setenv("MONGO_DATABASE", "synthetic_db")
    client.DatabaseConfig.from_env()
    loader.assert_called_once_with(client.ENV_PATH, override=False)


def test_save_module_import_does_not_connect_or_read_data(monkeypatch):
    factory = Mock(side_effect=AssertionError("import 중 접속 금지"))
    with monkeypatch.context() as patch:
        patch.setattr(client, "Client", factory)
        importlib.reload(save_data)
        factory.assert_not_called()
    importlib.reload(save_data)


def test_save_counts_only_duplicate_errors_and_does_not_mutate_input():
    collection = Mock()
    collection.insert_one.side_effect = [None, DuplicateKeyError("합성 중복")]
    players = {"fake-1": {"name": "가상선수_0001"}, "fake-2": {"name": "가상선수_0002"}}
    assert save_data.save_players(players, collection) == {
        "inserted": 1,
        "duplicates": 1,
    }
    assert all("id" not in record for record in players.values())
    collection.create_index.assert_called_once_with([("id", 1)], unique=True)


def test_database_failures_are_not_reported_as_duplicates():
    collection = Mock()
    collection.insert_one.side_effect = OperationFailure("합성 권한 오류")
    with pytest.raises(OperationFailure):
        save_data.save_players({"fake": {"name": "가상선수_0001"}}, collection)
