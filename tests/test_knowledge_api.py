import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx2
import pytest
from fastapi.testclient import TestClient
from langsmith.run_helpers import get_tracing_context

from FRS.agent.ollama_transport import ChatSample
from FRS.knowledge.api import create_knowledge_app
from FRS.knowledge.retrieval import load_index
from FRS.knowledge.service import KnowledgeService, OllamaAnswerGenerator


class Generator:
    def __init__(self):
        self.calls = 0

    def generate(self, question, hits, corpus_version):
        self.calls += 1
        return ChatSample(
            json.dumps(
                dict(
                    status="answered",
                    answer=hits[0].passage.text,
                    source_ids=[hits[0].passage.source_id],
                )
            ),
            0,
            (),
        )


def test_unconfigured_default_app_is_unavailable_without_network(monkeypatch):
    monkeypatch.delenv("RAG_ENABLED", raising=False)
    with TestClient(create_knowledge_app()) as client:
        assert client.get("/health").status_code == 503
        assert (
            client.post(
                "/knowledge/query",
                json=dict(request_id="q1", question="지원 포메이션은?"),
            ).status_code
            == 503
        )


def test_json_answer_and_health_do_not_run_model_on_readiness():
    gen = Generator()
    service = KnowledgeService(load_index(), gen)
    with TestClient(create_knowledge_app(service_factory=lambda: service)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health").json()["corpus_version"] == service.index.version
        assert gen.calls == 0
        response = client.post(
            "/knowledge/query", json=dict(request_id="q1", question="지원 포메이션은?")
        )
        assert response.status_code == 200
        data = response.json()
        assert (
            data["schema_version"] == "knowledge-answer.v1"
            and data["request_id"] == "q1"
        )
        assert data["status"] == "answered" and data["sources"]
        assert gen.calls == 1
        assert client.post("/agent/run", json={}).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        dict(request_id="bad id", question="hello"),
        dict(request_id="q", question=" "),
        dict(request_id="q", question="a" * 4001),
        dict(request_id="q", question=10),
        dict(request_id="q", question="hello", unknown=True),
        dict(request_id="q" * 65, question="hello"),
    ],
)
def test_invalid_body_rejected_before_model(body):
    gen = Generator()
    with TestClient(
        create_knowledge_app(
            service_factory=lambda: KnowledgeService(load_index(), gen)
        )
    ) as client:
        assert client.post("/knowledge/query", json=body).status_code == 422
    assert gen.calls == 0


def test_body_size_duplicate_json_and_content_type():
    gen = Generator()
    with TestClient(
        create_knowledge_app(
            service_factory=lambda: KnowledgeService(load_index(), gen)
        )
    ) as client:
        assert (
            client.post(
                "/knowledge/query",
                content="x" * 65537,
                headers={"Content-Type": "application/json"},
            ).status_code
            == 413
        )
        assert (
            client.post(
                "/knowledge/query",
                content='{"request_id":"q","question":"a","question":"b"}',
                headers={"Content-Type": "application/json"},
            ).status_code
            == 422
        )
        assert client.post("/knowledge/query", content="{}").status_code == 415
    assert gen.calls == 0


def test_cors_origins_match_existing_local_contract():
    with TestClient(
        create_knowledge_app(
            service_factory=lambda: KnowledgeService(load_index(), Generator())
        )
    ) as client:
        for origin, status in [
            ("http://localhost:5173", 200),
            ("http://127.0.0.1:5173", 200),
            ("https://example.com", 400),
        ]:
            r = client.options(
                "/knowledge/query",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type",
                },
            )
            assert r.status_code == status
            assert r.headers.get("access-control-allow-credentials") is None


def test_busy_has_no_queue_and_slot_releases_after_worker_finishes():
    entered, release = threading.Event(), threading.Event()

    class Blocking(Generator):
        def generate(self, *args):
            entered.set()
            assert release.wait(3)
            return super().generate(*args)

    gen = Blocking()
    service = KnowledgeService(load_index(), gen)
    with (
        TestClient(create_knowledge_app(service_factory=lambda: service)) as client,
        ThreadPoolExecutor() as executor,
    ):
        first = executor.submit(
            client.post,
            "/knowledge/query",
            json=dict(request_id="q1", question="지원 포메이션은?"),
        )
        try:
            assert entered.wait(2)
            assert (
                client.post(
                    "/knowledge/query",
                    json=dict(request_id="q2", question="지원 포메이션은?"),
                ).status_code
                == 409
            )
        finally:
            release.set()
        assert first.result(timeout=3).status_code == 200
        assert (
            client.post(
                "/knowledge/query",
                json=dict(request_id="q3", question="지원 포메이션은?"),
            ).status_code
            == 200
        )
    assert gen.calls == 2


def test_deadline_keeps_slot_until_inflight_model_finishes(monkeypatch):
    import FRS.knowledge.api as api

    monkeypatch.setattr(api, "REQUEST_DEADLINE_SECONDS", 0.1)
    release, finished = threading.Event(), threading.Event()

    class Blocking(Generator):
        def generate(self, *args):
            try:
                assert release.wait(3)
                return super().generate(*args)
            finally:
                finished.set()

    with TestClient(
        create_knowledge_app(
            service_factory=lambda: KnowledgeService(load_index(), Blocking())
        )
    ) as client:
        try:
            r = client.post(
                "/knowledge/query",
                json=dict(request_id="q1", question="지원 포메이션은?"),
            )
            assert r.status_code == 504
            assert (
                client.post(
                    "/knowledge/query",
                    json=dict(request_id="q2", question="지원 포메이션은?"),
                ).status_code
                == 409
            )
        finally:
            release.set()
            assert finished.wait(2)


def test_local_ollama_adapter_has_one_bounded_request_and_no_global_tracing(
    monkeypatch,
):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    calls = []

    def handler(request):
        assert get_tracing_context()["enabled"] is False
        calls.append(request)
        data = json.loads(request.content)
        source = json.loads(data["messages"][1]["content"])["sources"][0]
        content = json.dumps(
            dict(
                status="answered",
                answer=source["text"],
                source_ids=[source["source_id"]],
            )
        )
        return httpx2.Response(
            200,
            json=dict(
                model="qwen3:8b",
                done=True,
                done_reason="stop",
                message=dict(role="assistant", content=content),
            ),
        )

    service = KnowledgeService(
        load_index(), OllamaAnswerGenerator(transport=httpx2.MockTransport(handler))
    )
    result, _ = service.ask("지원 포메이션은?")
    assert result.status == "answered"
    assert len(calls) == 1 and str(calls[0].url) == "http://127.0.0.1:11434/api/chat"
