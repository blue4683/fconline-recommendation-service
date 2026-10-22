"""한 그래프에 명시적으로 주입하는 추출기·자료·배치·응답 시계."""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from FRS.agent.extraction import ConditionExtractor
from FRS.agent.material import MaterialProvider
from FRS.core.candidate_search.search import BatchSelection


@dataclass(frozen=True)
class AgentDependencies:
    extractor: ConditionExtractor
    material: MaterialProvider
    batches: BatchSelection
    clock: Callable[[], datetime]
    fixed_salary_cap: int | None = None
