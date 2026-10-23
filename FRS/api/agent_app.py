"""별도 Agent HTTP 앱(합성/명시 관측 자료). 서버를 실행하기 전까지 모델/자료를 준비하지 않는다."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, StreamingResponse

from FRS.agent.candidate_graph import CandidateSquadAgent
from FRS.api.agent_events import (
    ALLOWED_ORIGINS,
    MAX_BODY_BYTES,
    REQUEST_DEADLINE_SECONDS,
    EventSequence,
    RequestError,
    parse_request,
)
from FRS.api.agent_projection import error_outcome, project_result
from FRS.api.agent_runtime import (
    HEALTH_SCHEMA,
    AgentService,
    AgentSettings,
    build_service,
    objectives_listing,
    utc_now,
)
from FRS.core.squad_recommend.objective import GK_PRIORITY


class RunStopped(RuntimeError):
    pass


class Lease:
    def __init__(self, owner, deadline, monotonic):
        self.owner, self.deadline, self.monotonic = owner, deadline, monotonic
        self.stopped = threading.Event()
        self.rendering = threading.Event()
        self.closed, self.future = False, None

    def check(self):
        if self.stopped.is_set() or self.monotonic() >= self.deadline:
            raise RunStopped("요청 작업 중단")

    def close(self):
        self.stopped.set()
        with self.owner.lock:
            self.closed = True
            if self.future is None or self.future.done():
                if self.owner.active is self:
                    self.owner.active = None

    def completed(self, future):
        with self.owner.lock:
            if self.closed and self.owner.active is self:
                self.owner.active = None


class Gate:
    def __init__(self):
        self.lock, self.active = threading.Lock(), None

    def acquire(self, deadline, monotonic):
        with self.lock:
            if self.active is not None:
                return None
            self.active = Lease(self, deadline, monotonic)
            return self.active


class GuardedSelector:
    def __init__(self, selector, lease):
        self.selector, self.lease = selector, lease

    def select(self, registry):
        self.lease.check()
        raw = self.selector.select(registry)
        self.lease.check()
        return raw


class GuardedMaterial:
    def __init__(self, material, lease):
        self.material, self.lease = material, lease

    def load(self, batches):
        self.lease.check()
        rows = self.material.load(batches)
        self.lease.check()
        return rows


def requested_objective(service, body):
    """v2가 다요소를 고른 경우에만 서비스가 고정한 목적을 쓴다. v1은 항상 OVR이다."""
    if getattr(body, "objective", "ovr") == GK_PRIORITY:
        return service.objective
    return None


def execute(service, body, lease, clock, t0):
    objective = requested_objective(service, body)

    def guarded_clock():
        lease.check()
        return clock()

    try:
        lease.check()
        result = CandidateSquadAgent(
            GuardedSelector(service.selector, lease),
            material=GuardedMaterial(service.material, lease),
            batches=service.batches,
            clock=guarded_clock,
            fixed_salary_cap=service.fixed_salary_cap,
            objective=objective,
        ).run(body.message, t0=t0)
        lease.check()
        lease.rendering.set()
        return project_result(
            body.message,
            result,
            clock=guarded_clock,
            provenance=None if result.outcome == "input_issues" else service.provenance,
            material_id=service.material_id,
            data_mode=service.data_mode,
            fixed_salary_cap=service.fixed_salary_cap,
            contract=body.contract_version,
            objective=objective,
        )
    except RunStopped:
        return error_outcome("request_deadline")
    except Exception:
        # 외부 응답에는 구현 예외/원문/경로를 내보내지 않는다.
        return error_outcome()


class LeaseResponse(StreamingResponse):
    def __init__(self, content, lease):
        self.lease = lease
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.lease.close()


def create_agent_app(
    *, service_factory=None, settings=None, clock=utc_now, monotonic=time.monotonic
):
    @asynccontextmanager
    async def lifespan(app):
        app.state.gate = Gate()
        app.state.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="squad"
        )
        app.state.service = None
        try:
            try:
                if service_factory is not None:
                    service = service_factory()
                else:
                    service = build_service(
                        settings or AgentSettings.from_env(), clock=clock
                    )
                if type(service) is not AgentService:
                    raise ValueError("잘못된 Agent 서비스")
                app.state.service = service
            except (ValueError, TypeError, KeyError, OSError):
                pass
            yield
        finally:
            active = app.state.gate.active
            if active is not None:
                active.close()
            app.state.executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="FRS Agent", version="1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Accept"],
    )

    def readiness():
        service = app.state.service
        if service is None:
            return dict(
                schema_version=HEALTH_SCHEMA,
                status="unavailable",
                reason="not_configured",
                data_mode=None,
                model=None,
                material=None,
            )
        return service.health(clock())

    @app.get("/health")
    async def health():
        state = readiness()
        state["busy"] = app.state.gate.active is not None
        return JSONResponse(
            state, status_code=200 if state["status"] == "ready" else 503
        )

    @app.get("/objectives")
    async def objectives():
        service = app.state.service
        if service is None:
            return JSONResponse(
                objectives_listing(None, "agent_unavailable"), status_code=503
            )
        return JSONResponse(service.objectives())

    @app.post("/recommend/squad/stream")
    async def recommend(request: Request):
        deadline = monotonic() + REQUEST_DEADLINE_SECONDS
        if request.headers.get("origin") not in (None, *ALLOWED_ORIGINS):
            return JSONResponse(
                {"detail": {"code": "origin_not_allowed"}}, status_code=403
            )
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse({"detail": {"code": "json_required"}}, status_code=415)
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                content = bytearray()
                async for chunk in request.stream():
                    if len(content) + len(chunk) > MAX_BODY_BYTES:
                        raise RequestError("body_too_large", 413)
                    content.extend(chunk)
                body = parse_request(bytes(content))
        except ClientDisconnect:
            return JSONResponse(
                {"detail": {"code": "client_disconnected"}}, status_code=400
            )
        except RequestError as exc:
            return JSONResponse(
                {"detail": {"code": exc.code}}, status_code=exc.status_code
            )
        except TimeoutError:
            return JSONResponse(
                {"detail": {"code": "request_deadline"}}, status_code=408
            )
        if readiness()["status"] != "ready":
            return JSONResponse(
                {"detail": {"code": "agent_unavailable"}}, status_code=503
            )
        service = app.state.service
        if (
            getattr(body, "objective", "ovr") == GK_PRIORITY
            and service.objective is None
        ):
            return JSONResponse(
                {"detail": {"code": "objective_unavailable"}}, status_code=409
            )
        t0 = clock()
        lease = app.state.gate.acquire(deadline, monotonic)
        if lease is None:
            return JSONResponse({"detail": {"code": "agent_busy"}}, status_code=409)
        service = app.state.service

        async def events():
            sequence = EventSequence(
                body.request_id, service.data_mode, body.contract_version
            )
            try:
                yield sequence.started()
                lease.check()
                future = app.state.executor.submit(
                    execute, service, body, lease, clock, t0
                )
                lease.future = future
                future.add_done_callback(lease.completed)
                yield sequence.progress("agent")
                rendered = False
                while True:
                    if await request.is_disconnected():
                        return
                    lease.check()
                    if lease.rendering.is_set() and not rendered:
                        yield sequence.progress("render")
                        rendered = True
                    if future.done():
                        outcome = future.result()
                        lease.check()
                        yield sequence.terminal(outcome, clock=clock)
                        return
                    await asyncio.sleep(min(0.025, max(0, deadline - monotonic())))
            except RunStopped:
                yield sequence.terminal(error_outcome("request_deadline"), clock=clock)
            finally:
                lease.close()

        return LeaseResponse(events(), lease)

    return app


app = create_agent_app()
