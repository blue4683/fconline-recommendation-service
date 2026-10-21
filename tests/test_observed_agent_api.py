"""관측 자료 모드의 시작 설정·상태·HTTP 응답을 합성 리그로 만든 관측 실행 형식에서 검증한다.

실제 관측 자료·네트워크·모델을 쓰지 않는다. 합성 리그의 출처/배치만 관측 실행 형식으로 바꿔
`ObservedMaterial`의 해시·배치 검증을 그대로 통과시킨다.
"""

import hashlib
import json
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_candidate_selection import SOURCE, selection
from test_schema import OBSERVED
from test_synthetic_agent_api import parsed, request_body
from test_synthetic_material import config

from FRS.agent.material import generate_synthetic_material
from FRS.agent.presentation import presentation_spec
from FRS.agent.real_material import ObservedMaterial
from FRS.api.agent_app import create_agent_app
from FRS.api.agent_runtime import (
    AgentService,
    AgentSettings,
    ModelVerification,
    build_service,
)

RUN_ID = "real-data-observed-test"
FILES = {
    "cards": "player_card",
    "salaries": "card_salary",
    "prices": "card_price",
    "reinforcements": "reinforce_stats",
    "team_colors": "team_color",
}


def write_run(root, records):
    path = root / RUN_ID
    path.mkdir()
    hashes = {}
    for key, name in FILES.items():
        lines = "".join(
            json.dumps(
                {**row, "source": "nexon_web", "batch_id": RUN_ID},
                ensure_ascii=False,
            )
            + "\n"
            for row in records[key]
        ).encode()
        (path / f"{name}.jsonl").write_bytes(lines)
        hashes[f"{name}.jsonl"] = hashlib.sha256(lines).hexdigest()
    (path / "report.json").write_text(
        json.dumps(
            dict(
                run_id=RUN_ID,
                status="completed",
                auto_activation=False,
                processed_hashes=hashes,
            )
        )
    )
    return root


@pytest.fixture(scope="module")
def processed(tmp_path_factory):
    records = generate_synthetic_material(config()).to_dict()["records"]
    return write_run(tmp_path_factory.mktemp("processed"), records)


def settings(**changes):
    return AgentSettings(
        **{
            "data_mode": "observed",
            "run_id": RUN_ID,
            "observed_at": None,
            "model": "qwen3:8b",
            "model_digest": "a" * 64,
            "seed": 123,
            "members_per_family": 3,
            **changes,
        }
    )


def verified(settings, *, clock):
    return ModelVerification("qwen3:8b", "a" * 64, clock(), "observed-test")


@pytest.fixture
def service(processed):
    built = build_service(
        settings(),
        clock=lambda: OBSERVED,
        verifier=verified,
        processed_root=processed,
    )
    return AgentService(
        built.settings,
        built.material,
        Mock(select=Mock(side_effect=lambda r: json.dumps(selection(r)))),
        built.verification,
    )


def test_settings_require_mode_specific_inputs():
    with pytest.raises(ValidationError):
        settings(observed_at=OBSERVED)
    with pytest.raises(ValidationError):
        settings(run_id="synthetic-test")
    with pytest.raises(ValidationError):
        settings(data_mode="synthetic", run_id="synthetic-test")
    assert settings(data_mode="synthetic", observed_at=OBSERVED).data_mode == (
        "synthetic"
    )


def test_from_env_reads_observed_mode_without_a_configured_time(monkeypatch):
    monkeypatch.setattr("FRS.api.agent_runtime.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("AGENT_DATA_MODE", "observed")
    monkeypatch.setenv("AGENT_RUN_ID", RUN_ID)
    monkeypatch.setenv("AGENT_OBSERVED_AT", "")
    monkeypatch.setenv("AGENT_MODEL_DIGEST", "a" * 64)
    loaded = AgentSettings.from_env()
    assert (loaded.data_mode, loaded.run_id, loaded.observed_at) == (
        "observed",
        RUN_ID,
        None,
    )
    monkeypatch.setenv("AGENT_OBSERVED_AT", OBSERVED.isoformat())
    with pytest.raises(ValidationError):
        AgentSettings.from_env()


def test_health_reports_the_explicit_observed_run(service):
    state = service.health(OBSERVED)
    assert state["schema_version"] == "agent-health.v2"
    assert (state["status"], state["reason"], state["data_mode"]) == (
        "ready",
        None,
        "observed",
    )
    material = state["material"]
    assert set(material["batches"].values()) == {RUN_ID}
    assert material["id"] == service.material.material_id
    assert datetime.fromisoformat(material["observed_at"]) == OBSERVED
    assert datetime.fromisoformat(material["valid_until"]) == OBSERVED + timedelta(
        hours=24
    )


def test_observed_material_expires_at_the_earliest_deadline(service):
    state = service.health(OBSERVED + timedelta(hours=24))
    assert (state["status"], state["reason"]) == ("unavailable", "data_expired")
    assert service.health(OBSERVED - timedelta(seconds=1))["reason"] == "data_expired"


def test_mode_and_material_must_match(service, processed):
    synthetic = generate_synthetic_material(config())
    with pytest.raises(ValueError):
        AgentService(service.settings, synthetic, service.selector, None)
    with pytest.raises(ValueError):
        AgentService(
            settings(
                data_mode="synthetic", run_id="synthetic-test", observed_at=OBSERVED
            ),
            ObservedMaterial(processed, RUN_ID),
            service.selector,
            None,
        )


def test_missing_run_leaves_the_app_unconfigured(tmp_path):
    with TestClient(
        create_agent_app(
            service_factory=lambda: build_service(
                settings(), clock=lambda: OBSERVED, processed_root=tmp_path
            ),
            clock=lambda: OBSERVED,
        )
    ) as client:
        response = client.get("/health")
        assert response.status_code == 503
        body = response.json()
        assert (body["schema_version"], body["reason"], body["data_mode"]) == (
            "agent-health.v2",
            "not_configured",
            None,
        )


def test_http_success_labels_observed_material_on_every_event(service):
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        assert client.get("/health").json()["data_mode"] == "observed"
        events = parsed(client.post("/recommend/squad/stream", json=request_body()))
        assert events[0]["payload"] == {"data_mode": "observed"}
        assert all(e["contract_version"] == "web-agent.v1" for e in events)
        final = events[-1]["payload"]
        assert final["status"] == "optimal" and len(final["squad"]["rows"]) == 11
        assert final["data_mode"] == "observed"
        assert final["answer"] == presentation_spec()["observed_messages"]["optimal"]
        assert final["versions"]["material"] == service.material_id
        assert final["versions"]["web_contract"] == "web-agent.v1"


def test_http_clarification_keeps_the_observed_label(service):
    message = SOURCE.replace("리버풀", "리버풀 제외")
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        events = parsed(
            client.post("/recommend/squad/stream", json=request_body(message))
        )
        final = events[-1]["payload"]
        assert final["status"] == "needs_clarification"
        assert final["data_mode"] == "observed" and final["squad"] is None
        service.selector.select.assert_not_called()


def test_v0_requests_are_rejected_on_this_branch(service):
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        response = client.post(
            "/recommend/squad/stream",
            json={**request_body(), "contract_version": "web-agent.v0"},
        )
        assert response.status_code == 422
        assert response.json() == {"detail": {"code": "invalid_request"}}


def test_unknown_observations_do_not_set_the_deadline(tmp_path):
    records = generate_synthetic_material(config()).to_dict()["records"]
    first = records["prices"][0]
    records["prices"][0] = {
        **first,
        "status": "unknown",
        "valid_until": None,
        "price": None,
    }
    built = build_service(
        settings(),
        clock=lambda: OBSERVED,
        verifier=verified,
        processed_root=write_run(tmp_path, records),
    )
    state = built.health(OBSERVED)
    assert (state["status"], state["data_mode"]) == ("ready", "observed")
    assert datetime.fromisoformat(state["material"]["valid_until"]) == (
        OBSERVED + timedelta(hours=24)
    )
