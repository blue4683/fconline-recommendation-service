"""추출·정규화·기존 build를 연결하는 단일 요청 그래프. 웹/API 실행은 없다."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from FRS.agent.deps import AgentDependencies
from FRS.agent.extraction import ExtractionError
from FRS.core.candidate_search.search import BatchSelection
from FRS.core.request_normalization import (
    ConditionNormalizationResult,
    ExtractedConditions,
    normalize_conditions,
)
from FRS.core.request_normalization.amounts import MAX_SOURCE_LENGTH
from FRS.core.squad_recommend.build import (
    SquadBuildResult,
    build_spec,
    build_squad_request,
)


def agent_spec(*, fixed_salary_cap=None):
    spec = dict(
        version=1,
        graph="extract_then_normalize_then_conditional_build",
        build_version=build_spec(fixed_salary_cap=fixed_salary_cap)[
            "build_policy_version"
        ],
        extraction="injected_once_no_retry",
        invalid_original="normalize_without_extractor_or_material",
        invalid_conditions="return_without_context_material_clock_or_engine",
        context="explicit_batches_t0_and_response_clock",
        state="single_request_without_history_or_checkpoint",
        tracing="disabled_per_invocation_even_if_environment_enables_it",
        render_api_and_sse=False,
    )
    return {
        **spec,
        "graph_version": hashlib.sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


class _State(TypedDict, total=False):
    original: str
    t0: datetime
    extracted: ExtractedConditions
    normalization: ConditionNormalizationResult
    build: SquadBuildResult
    error: str
    stages: tuple[str, ...]


@dataclass(frozen=True)
class AgentRunResult:
    graph_version: str
    normalization: ConditionNormalizationResult | None = None
    build: SquadBuildResult | None = None
    error: str | None = None
    stages: tuple[str, ...] = ()


class SquadAgent:
    def __init__(self, dependencies: AgentDependencies):
        if not isinstance(dependencies, AgentDependencies):
            raise TypeError("AgentDependencies를 명시해야 합니다.")
        self._dependencies = dependencies

        def extract(state):
            try:
                result = dependencies.extractor.extract(state["original"])
            except ExtractionError:
                return {"error": "extraction_error", "stages": ("extract",)}
            return {"extracted": result, "stages": ("extract",)}

        def normalize(state):
            return {
                "normalization": normalize_conditions(
                    state["original"],
                    state["extracted"],
                    fixed_salary_cap=dependencies.fixed_salary_cap,
                ),
                "stages": state["stages"] + ("normalize",),
            }

        def build(state):
            batches, t0 = dependencies.batches, state["t0"]
            if not isinstance(batches, BatchSelection):
                raise TypeError("자료 종류별 BatchSelection을 명시해야 합니다.")
            batches = BatchSelection.model_validate(batches.model_dump())
            if (
                not isinstance(t0, datetime)
                or t0.tzinfo is None
                or t0.utcoffset() is None
            ):
                raise ValueError("기준 시각은 시간대 있는 datetime이어야 합니다.")
            if not callable(dependencies.clock):
                raise TypeError("응답 확인 시계를 명시해야 합니다.")
            material = dependencies.material.load(batches)
            return {
                "build": build_squad_request(
                    state["original"],
                    state["extracted"],
                    batches=batches,
                    t0=t0,
                    clock=dependencies.clock,
                    fixed_salary_cap=dependencies.fixed_salary_cap,
                    **material,
                ),
                "stages": state["stages"] + ("build",),
            }

        graph = StateGraph(_State)
        graph.add_node("extract", extract)
        graph.add_node("normalize", normalize)
        graph.add_node("build", build)
        graph.add_edge(START, "extract")
        graph.add_conditional_edges(
            "extract", lambda state: END if state.get("error") else "normalize"
        )
        graph.add_conditional_edges(
            "normalize",
            lambda state: END if state["normalization"].issues else "build",
        )
        graph.add_edge("build", END)
        with tracing_context(enabled=False):
            self._graph = graph.compile()

    def run(self, original: str, *, t0: datetime) -> AgentRunResult:
        version = agent_spec(fixed_salary_cap=self._dependencies.fixed_salary_cap)[
            "graph_version"
        ]
        # 모델 호출 전에 원문 타입/길이를 막는다. 정규화기는 기존 오류 계약을 반환한다.
        if (
            not isinstance(original, str)
            or len(original) > MAX_SOURCE_LENGTH
            or not original.strip()
        ):
            return AgentRunResult(
                graph_version=version,
                normalization=normalize_conditions(
                    original,
                    ExtractedConditions(),
                    fixed_salary_cap=self._dependencies.fixed_salary_cap,
                ),
                stages=("normalize",),
            )
        with tracing_context(enabled=False):
            state = self._graph.invoke({"original": original, "t0": t0})
        return AgentRunResult(
            graph_version=version,
            normalization=state.get("normalization"),
            build=state.get("build"),
            error=state.get("error"),
            stages=state["stages"],
        )
