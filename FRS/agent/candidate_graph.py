"""준비/선택 문제를 구분하고 정상 근거만 기존 요청별 그래프로 전달한다."""

import hashlib
from dataclasses import dataclass
from typing import Literal

from langsmith import tracing_context

from FRS.agent.candidate_selection import (
    CandidateSelectionError,
    CandidateSelector,
    decode_selection,
    selection_spec,
)
from FRS.agent.candidates import CandidatePreparation, canonical, prepare_candidates
from FRS.agent.deps import AgentDependencies
from FRS.agent.extraction import ExtractionError
from FRS.agent.graph import AgentRunResult, SquadAgent, agent_spec
from FRS.core.request_normalization import ExtractedConditions


def candidate_agent_spec(*, fixed_salary_cap=None):
    spec = dict(
        version=1,
        selection_version=selection_spec()["selection_version"],
        graph_version=agent_spec(fixed_salary_cap=fixed_salary_cap)["graph_version"],
        preparation_issues="stop_before_selector_context_material_clock_engine",
        provider_error="safe_error_no_retry_or_fallback",
        valid_selection="fixed_evidence_into_existing_squad_agent",
        state="single_request_without_mutable_binding_history",
        http_contract=False,
        builtin_model_transport=False,
        tracing="disabled_around_selector_and_graph",
    )
    return {**spec, "adapter_version": hashlib.sha256(canonical(spec)).hexdigest()}


@dataclass(frozen=True)
class CandidateRunResult:
    adapter_version: str
    outcome: Literal["input_issues", "provider_error", "agent_result"]
    preparation: CandidatePreparation
    agent_result: AgentRunResult | None = None
    error: str | None = None
    stages: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FixedExtractor:
    original: str
    extracted: ExtractedConditions

    def extract(self, original):
        if original != self.original:
            raise ExtractionError("요청 원문이 달라졌습니다.")
        return ExtractedConditions.model_validate(self.extracted.model_dump())


class CandidateSquadAgent:
    def __init__(
        self,
        selector: CandidateSelector,
        *,
        material,
        batches,
        clock,
        fixed_salary_cap=None,
        objective=None,
    ):
        if not callable(getattr(selector, "select", None)):
            raise TypeError("후보 ID selector를 명시해야 합니다.")
        self._selector, self._material = selector, material
        self._batches, self._clock = batches, clock
        self._fixed_salary_cap = fixed_salary_cap
        self._objective = objective

    def run(self, original, *, t0):
        with tracing_context(enabled=False):
            return self._run(original, t0=t0)

    def _run(self, original, *, t0):
        version = candidate_agent_spec(fixed_salary_cap=self._fixed_salary_cap)[
            "adapter_version"
        ]
        preparation = prepare_candidates(
            original, fixed_salary_cap=self._fixed_salary_cap
        )
        if preparation.issues:
            return CandidateRunResult(
                version, "input_issues", preparation, stages=("prepare",)
            )
        try:
            raw = self._selector.select(preparation.registry)
        except ExtractionError:
            return CandidateRunResult(
                version,
                "provider_error",
                preparation,
                error="selection_provider_error",
                stages=("prepare", "select"),
            )
        try:
            extracted = decode_selection(original, preparation.registry, raw)
        except CandidateSelectionError as exc:
            return CandidateRunResult(
                version,
                "provider_error",
                preparation,
                error=exc.code,
                stages=("prepare", "select", "validate"),
            )
        graph = SquadAgent(
            AgentDependencies(
                _FixedExtractor(original, extracted),
                self._material,
                self._batches,
                self._clock,
                self._fixed_salary_cap,
                self._objective,
            )
        )
        result = graph.run(original, t0=t0)
        return CandidateRunResult(
            version,
            "agent_result",
            preparation,
            agent_result=result,
            stages=("prepare", "select", "validate") + result.stages,
        )
