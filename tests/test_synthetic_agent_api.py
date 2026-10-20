"""실제 서버/모델 없이 HTTP·단일 작업·기한/연결 종료를 검증한다."""

import asyncio
import json
import threading
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

import httpx2
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from test_candidate_selection import SOURCE, selection
from test_schema import OBSERVED
from test_synthetic_material import config

from FRS.agent.material import generate_synthetic_material
from FRS.api.agent_app import create_agent_app
from FRS.api.agent_runtime import (
    AgentService,
    AgentSettings,
    ModelVerification,
    build_service,
    verify_model,
)


@pytest.fixture(scope="module")
def material():
    return generate_synthetic_material(config())


def settings():
    return AgentSettings(
        run_id="synthetic-test",
        observed_at=OBSERVED,
        model="qwen3:8b",
        model_digest="a" * 64,
        seed=123,
        members_per_family=3,
    )


@pytest.fixture
def service(material):
    return AgentService(
        settings(),
        material,
        Mock(select=Mock(side_effect=lambda r: json.dumps(selection(r)))),
        ModelVerification("qwen3:8b", "a" * 64, OBSERVED, "synthetic-test"),
    )


def request_body(message=SOURCE, request_id="request-1"):
    return dict(contract_version="web-agent.v0", request_id=request_id, message=message)


def parsed(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for event in response.text.strip().split("\n\n"):
        name, line = event.split("\n")
        row = json.loads(line.removeprefix("data: "))
        assert name == "event: " + row["type"]
        assert row["seq"] == len(events)
        events.append(row)
    assert events[0]["type"] == "started"
    assert sum(e["type"] in ("final", "error") for e in events) == 1
    return events


def test_http_success_uses_real_synthetic_engine_and_exact_wire(service):
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        events = parsed(client.post("/recommend/squad/stream", json=request_body()))
        assert events[-1]["type"] == "final"
        shown = events[-1]["payload"]
        assert shown["status"] == "optimal" and len(shown["squad"]["rows"]) == 11
        assert shown["versions"]["model"] == "qwen3:8b"
        assert shown["versions"]["model_digest"] == "a" * 64
        assert [e["payload"]["stage"] for e in events if e["type"] == "progress"] == [
            "agent",
            "render",
        ]
        assert client.get("/health").json()["busy"] is False


@pytest.mark.parametrize(
    "message,expected",
    [
        (SOURCE.replace("리버풀", "리버풀 제외"), "prepare.unrecognized_context"),
        (SOURCE.replace("급여 999, ", ""), "missing_condition"),
    ],
)
def test_http_clarification_preserves_source_and_blocks_irrelevant_work(
    service, message, expected
):
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        events = parsed(
            client.post("/recommend/squad/stream", json=request_body(message))
        )
        body = events[-1]["payload"]
        assert body["status"] == "needs_clarification" and body["squad"] is None
        assert any(i["code"] == expected for i in body["issues"])
        if expected.startswith("prepare."):
            service.selector.select.assert_not_called()
            assert body["versions"]["model"] == "unused"
        else:
            service.selector.select.assert_called_once()


def test_incomplete_model_selection_is_one_error_without_fallback(service):
    service.selector.select = Mock(
        side_effect=lambda r: json.dumps({**selection(r), "salary_cap": []})
    )
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        events = parsed(client.post("/recommend/squad/stream", json=request_body()))
        assert (
            events[-1]["type"] == "error"
            and events[-1]["payload"]["code"] == "incomplete_coverage"
        )
        service.selector.select.assert_called_once()


def test_unexpected_worker_exception_is_safe_and_releases_slot(service):
    service.selector.select = Mock(side_effect=RuntimeError("private password/path"))
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        response = client.post("/recommend/squad/stream", json=request_body())
        assert parsed(response)[-1]["payload"]["code"] == "internal_error"
        assert "private" not in response.text
        assert client.get("/health").json()["busy"] is False


@pytest.mark.parametrize(
    "change,status",
    [
        (dict(message=" "), 422),
        (dict(message="x" * 4001), 422),
        (dict(request_id="../x"), 422),
        (dict(contract_version="future"), 422),
        (dict(extra=True), 422),
    ],
)
def test_bad_request_never_reaches_selector(service, change, status):
    body = request_body()
    body.update(change)
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        response = client.post("/recommend/squad/stream", json=body)
        assert response.status_code == status and response.json() == {
            "detail": {"code": "invalid_request"}
        }
        service.selector.select.assert_not_called()


@pytest.mark.parametrize(
    "raw,status",
    [(b"x" * 65537, 413), (b'{"message":"x","message":"y"}', 422), (b"[" * 2000, 422)],
)
def test_byte_and_strict_json_limits(service, raw, status):
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        assert (
            client.post(
                "/recommend/squad/stream",
                content=raw,
                headers={"Content-Type": "application/json"},
            ).status_code
            == status
        )
        service.selector.select.assert_not_called()


def test_json_content_type_and_origin_rejected_before_work(service):
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        assert client.post("/recommend/squad/stream", content="{}").status_code == 415
        assert (
            client.post(
                "/recommend/squad/stream",
                json=request_body(),
                headers={"Origin": "https://other.example"},
            ).status_code
            == 403
        )
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            response = client.options(
                "/recommend/squad/stream",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert (
                response.status_code == 200
                and response.headers["access-control-allow-origin"] == origin
            )
            assert "access-control-allow-credentials" not in response.headers
        assert (
            client.options(
                "/recommend/squad/stream",
                headers={
                    "Origin": "https://other.example",
                    "Access-Control-Request-Method": "POST",
                },
            ).status_code
            == 400
        )
        service.selector.select.assert_not_called()


def test_health_is_cached_startup_verification_and_no_inference():
    verifier = Mock(
        side_effect=lambda cfg, clock: ModelVerification(
            cfg.model, cfg.model_digest, clock(), "synthetic"
        )
    )
    factory = Mock(
        side_effect=lambda: build_service(
            settings(), clock=lambda: OBSERVED, verifier=verifier
        )
    )
    with TestClient(
        create_agent_app(service_factory=factory, clock=lambda: OBSERVED)
    ) as client:
        for _ in range(3):
            response = client.get("/health")
            assert response.status_code == 200
            body = response.json()
            assert body["model"]["verification"] == "startup_snapshot"
            assert body["model"]["checked_at"] == OBSERVED.isoformat()
            assert body["material"]["batches"]
        verifier.assert_called_once()
        factory.assert_called_once()


@pytest.mark.parametrize("reason", ["model_unverified", "data_expired"])
def test_unavailable_health_and_requests_are_503_without_selector(service, reason):
    clock = (
        (lambda: OBSERVED + timedelta(days=2))
        if reason == "data_expired"
        else (lambda: OBSERVED)
    )
    if reason == "model_unverified":
        service = replace(service, verification=None)
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=clock)
    ) as client:
        assert client.get("/health").json()["reason"] == reason
        assert (
            client.post("/recommend/squad/stream", json=request_body()).status_code
            == 503
        )
        service.selector.select.assert_not_called()


def test_missing_configuration_has_safe_health_without_loading_database_or_model():
    with TestClient(
        create_agent_app(service_factory=Mock(side_effect=ValueError("private")))
    ) as client:
        result = client.get("/health")
        assert result.status_code == 503 and result.json()["reason"] == "not_configured"
        assert "private" not in result.text


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_startup_metadata_failure_stops_without_second_request(status):
    calls = []

    def handle(req):
        calls.append(str(req.url))
        return httpx2.Response(status, headers={"location": "https://other.example"})

    with pytest.raises(ValueError):
        verify_model(
            settings(), clock=lambda: OBSERVED, transport=httpx2.MockTransport(handle)
        )
    assert len(calls) == 1


def test_startup_metadata_exact_digest_and_two_readonly_requests():
    calls = []

    def handle(req):
        calls.append((req.method, str(req.url)))
        return httpx2.Response(
            200,
            json={"version": "synthetic"}
            if req.url.path == "/api/version"
            else {"models": [{"name": "qwen3:8b", "digest": "a" * 64}]},
        )

    verified = verify_model(
        settings(), clock=lambda: OBSERVED, transport=httpx2.MockTransport(handle)
    )
    assert (
        verified.digest == "a" * 64
        and len(calls) == 2
        and all(m == "GET" for m, _ in calls)
    )
    assert verified.checked_at == OBSERVED


def test_expired_startup_batch_never_calls_model_verifier():
    verifier = Mock(side_effect=AssertionError("metadata 금지"))
    service = build_service(
        settings(), clock=lambda: OBSERVED + timedelta(days=2), verifier=verifier
    )
    assert service.health(OBSERVED + timedelta(days=2))["status"] == "unavailable"
    verifier.assert_not_called()


async def asgi_request(app, body, *, queue=None, send_hook=None):
    received = queue or asyncio.Queue()
    if queue is None:
        await received.put(
            {
                "type": "http.request",
                "body": json.dumps(body).encode(),
                "more_body": False,
            }
        )
    output = []

    async def send(message):
        output.append(message)
        if send_hook is not None:
            await send_hook(message)

    scope = dict(
        type="http",
        asgi={"version": "3.0", "spec_version": "2.4"},
        http_version="1.1",
        method="POST",
        scheme="http",
        path="/recommend/squad/stream",
        raw_path=b"/recommend/squad/stream",
        query_string=b"",
        root_path="",
        headers=[(b"content-type", b"application/json")],
        server=("test", 8001),
        client=("test", 1),
    )
    await app(scope, received.get, send)
    return output


@pytest.mark.parametrize("mode", ["disconnect", "deadline"])
def test_inflight_slot_survives_disconnect_or_timeout_until_real_worker_finishes(
    service, mode
):
    entered, released = threading.Event(), threading.Event()

    def blocking(registry):
        entered.set()
        assert released.wait(3)
        return json.dumps(selection(registry))

    service.selector.select = Mock(side_effect=blocking)
    timer = [0.0]
    app = create_agent_app(
        service_factory=lambda: service,
        clock=lambda: OBSERVED,
        monotonic=lambda: timer[0],
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            queue = asyncio.Queue()
            await queue.put(
                {
                    "type": "http.request",
                    "body": json.dumps(request_body()).encode(),
                    "more_body": False,
                }
            )
            first = asyncio.create_task(asgi_request(app, request_body(), queue=queue))
            assert await asyncio.to_thread(entered.wait, 2)
            if mode == "disconnect":
                await queue.put({"type": "http.disconnect"})
            else:
                timer[0] = 151.0
            messages = await asyncio.wait_for(first, 1)
            if mode == "deadline":
                assert b'"code":"request_deadline"' in b"".join(
                    m.get("body", b"") for m in messages
                )
            assert app.state.gate.active is not None
            second = await asgi_request(app, request_body(request_id="second"))
            assert second[0]["status"] == 409
            released.set()
            for _ in range(100):
                if app.state.gate.active is None:
                    break
                await asyncio.sleep(0.01)
            assert app.state.gate.active is None

    with patch(
        "FRS.api.agent_app.GuardedMaterial.load",
        side_effect=AssertionError("후속 자료 금지"),
    ) as load:
        try:
            asyncio.run(exercise())
        finally:
            released.set()
        load.assert_not_called()
    service.selector.select.assert_called_once()


def test_response_header_send_failure_does_not_leak_execution_slot(service):
    app = create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)

    async def fail(message):
        if message["type"] == "http.response.start":
            raise OSError("closed")

    async def exercise():
        async with app.router.lifespan_context(app):
            with pytest.raises(ClientDisconnect):
                await asgi_request(app, request_body(), send_hook=fail)
            assert app.state.gate.active is None
            service.selector.select.assert_not_called()

    asyncio.run(exercise())


def test_body_read_deadline_is_pre_stream_and_does_not_reserve_slot(
    service, monkeypatch
):
    monkeypatch.setattr("FRS.api.agent_app.REQUEST_DEADLINE_SECONDS", 0.01)
    app = create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)

    async def exercise():
        async with app.router.lifespan_context(app):
            queue = asyncio.Queue()
            await queue.put({"type": "http.request", "body": b"{", "more_body": True})
            output = await asyncio.wait_for(
                asgi_request(app, request_body(), queue=queue), 1
            )
            assert output[0]["status"] == 408
            assert app.state.gate.active is None
            service.selector.select.assert_not_called()

    asyncio.run(exercise())


def test_digest_mismatch_fails_closed_without_chat_or_retry():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx2.Response(
            200,
            json={"version": "synthetic"}
            if len(paths) == 1
            else {"models": [{"name": "qwen3:8b", "digest": "b" * 64}]},
        )

    with pytest.raises(ValueError):
        verify_model(
            settings(), clock=lambda: OBSERVED, transport=httpx2.MockTransport(handle)
        )
    assert paths == ["/api/version", "/api/tags"]


def test_health_and_preparation_rejection_do_not_reread_candidate_material(service):
    app = create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    with patch(
        "FRS.api.agent_runtime.SyntheticMaterial.to_dict",
        side_effect=AssertionError("후보 원문 재조회 금지"),
    ) as read:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            response = client.post(
                "/recommend/squad/stream",
                json=request_body(SOURCE.replace("리버풀", "리버풀 제외")),
            )
            assert parsed(response)[-1]["payload"]["status"] == "needs_clarification"
        read.assert_not_called()
    service.selector.select.assert_not_called()


def test_expiry_after_render_before_final_drops_the_success_table(service):
    from FRS.api.agent_projection import project_result

    now = [OBSERVED]
    deadline = OBSERVED + timedelta(days=1)

    def rendered(*args, **kwargs):
        outcome = project_result(*args, **kwargs)
        now[0] = deadline
        return outcome

    with patch("FRS.api.agent_app.project_result", side_effect=rendered):
        with TestClient(
            create_agent_app(service_factory=lambda: service, clock=lambda: now[0])
        ) as client:
            shown = parsed(client.post("/recommend/squad/stream", json=request_body()))[
                -1
            ]
    assert shown["type"] == "final"
    assert shown["payload"]["status"] == "data_expired"
    assert shown["payload"]["squad"] is None
