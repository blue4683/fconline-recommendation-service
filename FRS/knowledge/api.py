"""추천 API와 분리된 문서 Q&A 앱. 기본 설정은 모델/전송 비활성이다."""

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Annotated

import httpx2
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse

from FRS.agent.json_contract import strict_json
from FRS.api.agent_app import Gate, RunStopped
from FRS.api.agent_events import (
    ALLOWED_ORIGINS,
    MAX_BODY_BYTES,
    REQUEST_DEADLINE_SECONDS,
)
from FRS.api.agent_runtime import verify_model
from FRS.knowledge.retrieval import load_index
from FRS.knowledge.service import KnowledgeService, OllamaAnswerGenerator


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    question: Annotated[str, Field(min_length=1, max_length=4000)]


def build_local_service():
    if os.environ.get("RAG_ENABLED") != "1":
        raise ValueError("RAG 실행을 명시해야 합니다.")
    digest = os.environ.get("RAG_MODEL_DIGEST", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("모델 digest를 명시해야 합니다.")
    # 실제 시작을 명시한 경우에만 읽기 전용 최대2회 요청한다.
    verification = verify_model(SimpleNamespace(model="qwen3:8b", model_digest=digest))
    service = KnowledgeService(load_index(), OllamaAnswerGenerator())
    service.verification = verification
    return service


def create_knowledge_app(*, service_factory=None, monotonic=time.monotonic):
    @asynccontextmanager
    async def lifespan(app):
        app.state.gate = Gate()
        app.state.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="knowledge"
        )
        app.state.service = None
        try:
            service = (service_factory or build_local_service)()
            if not isinstance(service, KnowledgeService):
                raise ValueError("문서 Q&A 서비스가 필요합니다.")
            app.state.service = service
        except (ValueError, TypeError, OSError, KeyError, httpx2.HTTPError):
            pass
        try:
            yield
        finally:
            if app.state.gate.active:
                app.state.gate.active.close()
            app.state.executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="FRS Knowledge Q&A", version="1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    def health():
        service = app.state.service
        return JSONResponse(
            dict(
                schema_version="knowledge-health.v1",
                status="ready" if service else "unavailable",
                corpus_version=service.index.version if service else None,
            ),
            status_code=200 if service else 503,
        )

    @app.post("/knowledge/query")
    async def query(request: Request):
        def error(code, status):
            return JSONResponse(
                dict(
                    schema_version="knowledge-http-error.v1", status="error", code=code
                ),
                status_code=status,
            )

        if (
            request.headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            return error("invalid_content_type", 415)
        raw = bytearray()
        try:
            async for part in request.stream():
                if len(raw) + len(part) > MAX_BODY_BYTES:
                    return error("body_too_large", 413)
                raw.extend(part)
            body = Question.model_validate(strict_json(raw.decode("utf-8")))
            if not body.question.strip():
                raise ValueError("empty")
        except (ValueError, UnicodeError, ValidationError):
            return error("invalid_request", 422)
        if app.state.service is None:
            return error("not_configured", 503)
        lease = app.state.gate.acquire(
            monotonic() + REQUEST_DEADLINE_SECONDS, monotonic
        )
        if lease is None:
            return error("busy", 409)
        try:
            future = app.state.executor.submit(
                app.state.service.ask, body.question, check=lease.check
            )
            lease.future = future
            future.add_done_callback(lease.completed)
            wrapped = asyncio.wrap_future(future)
            while not future.done():
                if await request.is_disconnected():
                    return error("disconnected", 499)
                lease.check()
                await asyncio.wait([wrapped], timeout=0.05)
            result, observation = await wrapped
            lease.check()
            return JSONResponse(dict(request_id=body.request_id, **result.to_dict()))
        except RunStopped:
            return error("request_deadline", 504)
        except Exception:
            return error("internal_error", 500)
        finally:
            lease.close()

    return app


app = create_knowledge_app()
