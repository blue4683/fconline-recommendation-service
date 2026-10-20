"""모델 없는 ID 선택 연결과 물리 작업 종료를 추적하는 단일 MCP 실행 슬롯."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

from FRS.agent.candidate_graph import CandidateSquadAgent
from FRS.agent.candidate_selection import CandidateSelectionError, decode_selection
from FRS.agent.candidates import canonical, prepare_candidates
from FRS.agent.material import (
    SyntheticLeagueSpec,
    SyntheticMaterial,
    generate_synthetic_material,
)
from FRS.agent.ollama_selection import selection_request
from FRS.agent.presentation import present_agent_result
from FRS.api.agent_app import Gate, RunStopped
from FRS.api.agent_runtime import utc_now
from FRS.core.request_normalization import normalize_conditions
from FRS.mcp.contract import BuildRequest, MaterialContext, error_response, response

DEADLINE_SECONDS = 150


class _Selection:
    def __init__(self, raw):
        self.raw = raw

    def select(self, registry):
        return self.raw


class MCPService:
    def __init__(
        self,
        spec,
        *,
        clock=utc_now,
        monotonic=time.monotonic,
        factory=generate_synthetic_material,
    ):
        if (
            type(spec) is not SyntheticLeagueSpec
            or spec.seed != 123
            or spec.members_per_family != 3
        ):
            raise ValueError("명시 합성 설정이 필요합니다.")
        self.spec = SyntheticLeagueSpec.model_validate(spec.model_dump())
        self.clock, self.monotonic, self.factory = clock, monotonic, factory
        self.material = None
        self.gate = Gate()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="frs-mcp")
        self.closed = False

    def close(self):
        self.closed = True
        if self.gate.active is not None:
            self.gate.active.close()
        self.executor.shutdown(wait=False, cancel_futures=True)

    @property
    def busy(self):
        return self.gate.active is not None

    def prompt(self, original):
        prepared = prepare_candidates(original)
        if prepared.issues:
            return canonical(
                dict(
                    outcome="input_issues",
                    issues=[i.model_dump(mode="json") for i in prepared.issues],
                )
            ).decode()
        request = selection_request("qwen3:8b", prepared.registry)
        return canonical(
            dict(
                instructions=request["messages"][0]["content"],
                candidates=json.loads(request["messages"][1]["content"]),
                output_schema=request["format"],
                model_invoked=False,
            )
        ).decode()

    def build(self, body, *, check=lambda: None):
        if type(body) is not BuildRequest:
            raise ValueError("검증된 MCP 요청이 필요합니다.")
        check()
        prepared = prepare_candidates(body.original)
        if prepared.issues:
            return response(
                request_id=body.request_id,
                outcome="input_issues",
                issues=prepared.issues,
            )
        raw = canonical(body.selection).decode()
        try:
            extracted = decode_selection(body.original, prepared.registry, raw)
        except CandidateSelectionError as exc:
            return error_response(exc.code, body.request_id, outcome="selection_error")
        normalized = normalize_conditions(body.original, extracted)
        check()
        # 오류 조건은 합성 자료/시계/엔진에 접근하지 않는다.
        batches, t0, material = None, None, None
        if not normalized.issues:
            t0 = self.clock()
            check()
            if self.material is None:
                made = self.factory(self.spec)
                if type(made) is not SyntheticMaterial or made.to_dict()[
                    "inputs"
                ] != self.spec.model_dump(mode="json"):
                    raise ValueError("명시한 합성 context와 자료가 일치하지 않습니다.")
                check()
                self.material = made
            material, batches = self.material, self.material.batches

        def guarded_clock():
            check()
            return self.clock()

        class CheckedMaterial:
            def load(self, selected):
                check()
                result = material.load(selected)
                check()
                return result

        result = CandidateSquadAgent(
            _Selection(raw),
            material=CheckedMaterial(),
            batches=batches,
            clock=guarded_clock,
        ).run(body.original, t0=t0)
        check()
        if result.outcome != "agent_result":
            return error_response("internal_error", body.request_id)
        shown = present_agent_result(result.agent_result, clock=guarded_clock)
        snapshot = (
            None
            if result.agent_result.build is None
            else result.agent_result.build.snapshot
        )
        return response(
            request_id=body.request_id,
            outcome="agent_result",
            presentation=shown.to_dict(),
            context=None
            if material is None
            else MaterialContext(
                material_id=material.material_id,
                specification=self.spec,
                batches=batches,
            ),
            snapshot=snapshot,
        )

    def _execute(self, body, lease):
        try:
            return self.build(body, check=lease.check)
        except RunStopped:
            return error_response("request_deadline", body.request_id)
        except Exception:
            return error_response("internal_error", body.request_id)

    async def invoke(self, raw):
        deadline = self.monotonic() + DEADLINE_SECONDS
        try:
            if len(canonical(raw)) > 65536:
                raise ValueError
            body = BuildRequest.model_validate(raw)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return error_response("invalid_request")
        if self.closed:
            return error_response("internal_error", body.request_id)
        lease = self.gate.acquire(deadline, self.monotonic)
        if lease is None:
            return error_response("busy", body.request_id)
        try:
            future = self.executor.submit(self._execute, body, lease)
            lease.future = future
            future.add_done_callback(lease.completed)
            while not future.done():
                lease.check()
                await asyncio.sleep(min(0.01, max(0, deadline - self.monotonic())))
            lease.check()
            return future.result()
        except RunStopped:
            return error_response("request_deadline", body.request_id)
        except Exception:
            return error_response("internal_error", body.request_id)
        finally:
            lease.close()
