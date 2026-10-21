"""MongoDB 관측 공급기의 Agent/HTTP 연결을 합성 자료·선택기·DB 대역으로 검증한다."""

import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_candidate_selection import SOURCE, selection
from test_load_observed import FakeDatabase
from test_mongo_material import Connection
from test_observed_agent_api import RUN_ID, settings, verified, write_run
from test_schema import OBSERVED
from test_synthetic_agent_api import parsed, request_body
from test_synthetic_material import config

from FRS.agent.material import generate_synthetic_material
from FRS.agent.real_material import ObservedMaterial
from FRS.api.agent_app import create_agent_app
from FRS.api.agent_runtime import AgentService, AgentSettings, build_service
from FRS.utils.db.load_observed import load_observed_run


@pytest.fixture(scope="module")
def processed_root(tmp_path_factory):
    records = generate_synthetic_material(config()).to_dict()["records"]
    return write_run(tmp_path_factory.mktemp("mongo-agent"), records)


@pytest.fixture
def bundle(processed_root):
    database = FakeDatabase()
    load_observed_run(database, processed_root, RUN_ID)
    connection = Connection(database)
    built = build_service(
        settings(material_source="mongodb"),
        clock=lambda: OBSERVED,
        verifier=verified,
        processed_root=processed_root,
        mongo_client_factory=connection.factory,
    )
    service = AgentService(
        built.settings,
        built.material,
        Mock(select=Mock(side_effect=lambda registry: json.dumps(selection(registry)))),
        built.verification,
    )
    return service, database, connection


def test_settings_default_to_files_and_reject_invalid_combinations(monkeypatch):
    assert settings().material_source == "file"
    with pytest.raises(ValidationError):
        settings(material_source="latest")
    with pytest.raises(ValidationError):
        settings(data_mode="synthetic", observed_at=OBSERVED, material_source="mongodb")
    monkeypatch.setattr("FRS.api.agent_runtime.load_dotenv", lambda *a, **k: None)
    for key, value in {
        "AGENT_DATA_MODE": "observed",
        "AGENT_MATERIAL_SOURCE": "mongodb",
        "AGENT_RUN_ID": RUN_ID,
        "AGENT_OBSERVED_AT": "",
        "AGENT_MODEL_DIGEST": "a" * 64,
    }.items():
        monkeypatch.setenv(key, value)
    assert AgentSettings.from_env().material_source == "mongodb"


def test_file_mode_never_connects_to_mongo(processed_root):
    forbidden = Mock(side_effect=AssertionError("파일 모드는 DB에 연결하지 않음"))
    built = build_service(
        settings(),
        clock=lambda: OBSERVED,
        verifier=verified,
        processed_root=processed_root,
        mongo_client_factory=forbidden,
    )
    assert type(built.material) is ObservedMaterial
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "message", [SOURCE, SOURCE.replace("급여 999, ", ""), SOURCE + ", 예산 1BP"]
)
def test_db_and_file_services_return_identical_wire_outcomes(
    bundle, processed_root, message
):
    mongo, _, connection = bundle
    file_service = AgentService(
        settings(),
        ObservedMaterial(processed_root, RUN_ID),
        mongo.selector,
        mongo.verification,
    )
    responses = []
    for service in (file_service, mongo):
        before = connection.opens
        with TestClient(
            create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
        ) as client:
            assert client.get("/health").json() == service.health(OBSERVED) | {
                "busy": False
            }
            events = parsed(
                client.post("/recommend/squad/stream", json=request_body(message))
            )
            responses.append(events[-1])
        if service is mongo:
            assert connection.opens - before == (1 if message == SOURCE else 0)
    assert responses[0] == responses[1]
    final = responses[-1]["payload"]
    assert final["status"] == (
        "optimal" if message == SOURCE else "needs_clarification"
    )
    assert (
        (len(final["squad"]["rows"]) == 11)
        if message == SOURCE
        else (final["squad"] is None)
    )
    assert connection.opens == connection.closes


def test_expired_startup_never_verifies_or_calls_model(processed_root):
    database = FakeDatabase()
    load_observed_run(database, processed_root, RUN_ID)
    connection = Connection(database)
    verifier = Mock(side_effect=AssertionError("만료된 자료는 모델을 확인하지 않음"))
    expired = OBSERVED + timedelta(hours=24)
    built = build_service(
        settings(material_source="mongodb"),
        clock=lambda: expired,
        verifier=verifier,
        processed_root=processed_root,
        mongo_client_factory=connection.factory,
    )
    with TestClient(
        create_agent_app(service_factory=lambda: built, clock=lambda: expired)
    ) as client:
        assert client.get("/health").status_code == 503
        assert client.get("/health").json()["reason"] == "data_expired"
        assert (
            client.post("/recommend/squad/stream", json=request_body()).status_code
            == 503
        )
    verifier.assert_not_called()
    assert connection.opens == connection.closes == 1


def test_missing_db_batch_leaves_app_unconfigured_without_model_access(processed_root):
    connection = Connection(FakeDatabase())
    verifier = Mock(side_effect=AssertionError("DB 준비 실패 후 모델 접근 금지"))

    def factory():
        return build_service(
            settings(material_source="mongodb"),
            clock=lambda: OBSERVED,
            verifier=verifier,
            processed_root=processed_root,
            mongo_client_factory=connection.factory,
        )

    with TestClient(
        create_agent_app(service_factory=factory, clock=lambda: OBSERVED)
    ) as client:
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json()["reason"] == "not_configured"
    verifier.assert_not_called()
    assert connection.opens == connection.closes == 1


def test_runtime_db_loss_returns_safe_error_without_a_squad_or_file_fallback(bundle):
    service, database, connection = bundle
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        database["player_card"].documents.clear()
        before = connection.opens
        response = client.post("/recommend/squad/stream", json=request_body())
        last = parsed(response)[-1]
        assert last["type"] == "error" and last["payload"]["code"] == "internal_error"
        assert "squad" not in last["payload"]
        assert connection.opens == before + 1
        assert client.get("/health").json()["busy"] is False
    assert connection.opens == connection.closes


def test_service_source_must_match_provider_type(bundle, processed_root):
    mongo, _, _ = bundle
    with pytest.raises(ValueError):
        AgentService(settings(), mongo.material, mongo.selector, mongo.verification)
    with pytest.raises(ValueError):
        AgentService(
            mongo.settings,
            ObservedMaterial(processed_root, RUN_ID),
            mongo.selector,
            mongo.verification,
        )
