"""네트워크 없는 HTTP 경계 검증. 선수·DB·모델은 합성 자료만 사용한다."""

import importlib
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import OperationFailure
from test_model_input_contract import complete, selection
from test_schema import OBSERVED

from FRS.api.app import create_app
from FRS.api.settings import ApiSettings
from FRS.core.position_recommend.train import train_verified
from FRS.diagnostics.model_input_report import VerifiedCards
from FRS.utils.db.client import DatabaseConfig


class ReadCollection:
    def __init__(self, documents):
        self.documents = documents
        self.queries = []

    def find(self, selector):
        assert set(selector) == {"spid", "batch_id", "version"}
        assert all(not isinstance(v, dict) for v in selector.values())
        self.queries.append(deepcopy(selector))
        return [
            deepcopy(d)
            for d in self.documents
            if all(d[k] == v for k, v in selector.items())
        ]


class ReadDatabase:
    def __init__(self, documents):
        self.player_card = ReadCollection(documents)

    def with_options(self, *, codec_options):
        assert codec_options.tz_aware
        return self


@pytest.fixture(scope="module")
def model_bundle(tmp_path_factory):
    cards = tuple(
        complete(
            spid=123000001 + i, person_id=1 + i, positions=[("CF", "ST", "GK")[i % 3]]
        )
        for i in range(30)
    )
    verified = VerifiedCards(
        cards, selection(*cards), {"synthetic-batch": {"manifest_sha256": "a" * 64}}
    )
    output = tmp_path_factory.mktemp("api-model")
    result = train_verified(verified, output)
    return output / "bundle", result["model_version"]


@pytest.fixture
def service(model_bundle):
    card = complete()
    documents = [card.to_document()]
    database = ReadDatabase(documents)
    connection = SimpleNamespace(
        db=database, ping=Mock(return_value={"ok": 1}), close=Mock()
    )
    config = ApiSettings(
        DatabaseConfig("localhost", 27017, "synthetic", "synthetic"), *model_bundle
    )
    application = create_app(
        settings=config,
        connection_factory=lambda **kw: connection,
        clock=lambda: OBSERVED,
    )
    with TestClient(application) as client:
        yield SimpleNamespace(
            client=client,
            app=application,
            connection=connection,
            documents=documents,
            card=card,
            version=model_bundle[1],
        )
    connection.close.assert_called_once()


def payload(service):
    return dict(
        model_version=service.version,
        selection=selection(service.card).model_dump(mode="json"),
    )


def test_health_card_and_full_class_probabilities_are_read_only(service):
    before = deepcopy(service.documents)
    response = service.client.get("/health")
    assert (
        response.status_code == 200
        and response.json()["model_version"] == service.version
    )
    response = service.client.get(
        f"/cards/{service.card.spid}",
        params={"batch_id": service.card.batch_id, "version": service.card.version},
    )
    assert response.status_code == 200 and response.json()["usable"]
    assert response.json()["card"] == service.card.model_dump(mode="json")
    response = service.client.post("/recommend/position", json=payload(service))
    assert response.status_code == 200
    result = response.json()
    assert result["classes"] == ["CF", "GK", "ST"]
    assert set(result["probabilities"][str(service.card.spid)]) == {"CF", "GK", "ST"}
    assert sum(
        result["probabilities"][str(service.card.spid)].values()
    ) == pytest.approx(1)
    assert service.documents == before


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"batch_id": "synthetic-batch"},
        {"batch_id": " ", "version": "base"},
        {"batch_id": "synthetic-batch", "version": "base", "when": "2026-01-01"},
    ],
)
def test_card_requires_exact_selection_without_time_override(service, params):
    assert (
        service.client.get(f"/cards/{service.card.spid}", params=params).status_code
        == 422
    )


def test_missing_observation_and_model_mismatch_do_not_select_latest(service):
    response = service.client.get(
        f"/cards/{service.card.spid}",
        params={"batch_id": service.card.batch_id, "version": "missing"},
    )
    assert response.status_code == 404
    request = payload(service)
    request["model_version"] = "f" * 64
    assert service.client.post("/recommend/position", json=request).status_code == 409
    assert len(service.connection.db.player_card.queries) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"status": "unknown", "valid_until": None},
        {"status": "stale"},
        {"observed_at": OBSERVED + timedelta(minutes=1)},
        {"valid_until": OBSERVED},
    ],
)
def test_unusable_observations_are_visible_but_not_recommended(service, change):
    # 만료 경계는 원래 관측보다 뒤의 시각에서 평가한다.
    if change.get("valid_until") == OBSERVED:
        service.documents[0]["observed_at"] = OBSERVED - timedelta(hours=1)
    service.documents[0].update(change)
    response = service.client.get(
        f"/cards/{service.card.spid}",
        params={"batch_id": service.card.batch_id, "version": service.card.version},
    )
    assert response.status_code == 200 and not response.json()["usable"]
    assert (
        service.client.post("/recommend/position", json=payload(service)).json()[
            "detail"
        ]["code"]
        == "unusable_observation"
    )


def test_identical_duplicates_are_deduplicated_and_conflicts_are_rejected(service):
    service.documents.append(deepcopy(service.documents[0]))
    assert (
        service.client.post("/recommend/position", json=payload(service)).status_code
        == 200
    )
    service.documents[1]["stats"]["speed"] += 1
    response = service.client.post("/recommend/position", json=payload(service))
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "observation_conflict"
    )


@pytest.mark.parametrize("kind", ["missing_feature", "unknown_body", "wrong_grade"])
def test_input_contract_failures_request_correction(service, kind):
    if kind == "missing_feature":
        del service.documents[0]["stats"]["speed"]
    elif kind == "unknown_body":
        service.documents[0]["physical"] = "가상미학습체형"
    request = payload(service)
    if kind == "wrong_grade":
        request["selection"]["base_reinforce_level"] = 6
    response = service.client.post("/recommend/position", json=request)
    assert (
        response.status_code == 422
        and response.json()["detail"]["code"] == "input_contract_error"
    )


def test_invalid_stored_data_and_database_errors_are_sanitized(service, monkeypatch):
    service.documents[0]["height"] = "private-value"
    response = service.client.get(
        f"/cards/{service.card.spid}",
        params={"batch_id": service.card.batch_id, "version": service.card.version},
    )
    assert response.status_code == 503 and "private-value" not in response.text
    service.connection.ping.side_effect = OperationFailure("private-connection")
    response = service.client.get("/health")
    assert response.status_code == 503 and response.json()["database"] == "unavailable"
    monkeypatch.setattr(
        service.connection.db.player_card,
        "find",
        Mock(side_effect=OperationFailure("private-connection")),
    )
    response = service.client.post("/recommend/position", json=payload(service))
    assert response.status_code == 503 and "private-connection" not in response.text


@pytest.mark.parametrize("mutation", ["duplicate", "extra", "string_id"])
def test_invalid_requests_do_not_echo_values_or_query_database(service, mutation):
    request = payload(service)
    if mutation == "duplicate":
        request["selection"]["observations"] *= 2
    elif mutation == "extra":
        request["secret"] = "private-value"
    else:
        request["selection"]["observations"][0]["spid"] = str(service.card.spid)
    response = service.client.post("/recommend/position", json=request)
    assert response.status_code == 422 and "private-value" not in response.text
    assert service.connection.db.player_card.queries == []


@pytest.mark.parametrize("mode", ["not_configured", "missing_bundle"])
def test_model_unavailable_is_not_training_or_latest_fallback(tmp_path, mode):
    connection = SimpleNamespace(
        db=ReadDatabase([]), ping=Mock(return_value={"ok": 1}), close=Mock()
    )
    base = DatabaseConfig("localhost", 27017, "synthetic", "synthetic")
    config = (
        ApiSettings(base)
        if mode == "not_configured"
        else ApiSettings(base, tmp_path / "missing", "a" * 64)
    )
    with TestClient(
        create_app(settings=config, connection_factory=lambda **kw: connection)
    ) as client:
        response = client.get("/health")
        assert response.status_code == 503 and response.json()["model_version"] is None
        assert (
            client.post(
                "/recommend/position",
                json=dict(
                    model_version="a" * 64,
                    selection=selection(complete()).model_dump(mode="json"),
                ),
            ).status_code
            == 503
        )
    assert list(tmp_path.iterdir()) == []
    connection.close.assert_called_once()


def test_settings_require_explicit_model_pair_and_import_has_no_io(monkeypatch):
    environ = dict(MONGO_USERNAME="synthetic", MONGO_PASSWORD="synthetic")
    assert ApiSettings.from_env(environ).model_dir is None
    for change in (
        {"API_MODEL_DIR": "/models"},
        {"API_MODEL_VERSION": "a" * 64},
        {"API_MODEL_DIR": "/models", "API_MODEL_VERSION": "invalid"},
    ):
        with pytest.raises(ValueError):
            ApiSettings.from_env({**environ, **change})
    import FRS.api.app as module

    monkeypatch.setattr(
        ApiSettings,
        "from_env",
        Mock(side_effect=AssertionError("import 중 설정/접속 금지")),
    )
    importlib.reload(module)
