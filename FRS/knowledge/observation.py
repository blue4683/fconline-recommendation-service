"""원문/답변을 받지 않는 새 Q&A 전용 로컬 단계 관측."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

STAGES = ("retrieve", "generate", "validate")
STATUSES = ("ok", "insufficient_evidence", "provider_error", "invalid_output")


@dataclass
class Span:
    stage: str
    started_at: str
    finished_at: str | None = None
    elapsed_ms: float = 0
    status: str = "ok"
    input_tokens: int | None = None
    output_tokens: int | None = None


class Observation:
    def __init__(self, corpus_version, *, generator_kind="test_double"):
        self.trace_id = str(uuid4())
        if generator_kind not in ("test_double", "oracle", "ollama_qwen3_8b"):
            raise ValueError("생성기 종류를 명시해야 합니다.")
        self.generator_kind = generator_kind
        self.corpus_version = corpus_version
        self.spans = []
        self.status = "ok"

    @contextmanager
    def span(self, stage):
        if stage not in STAGES or stage in [s.stage for s in self.spans]:
            raise ValueError("지식 Q&A 단계만 한 번씩 관측합니다.")
        span = Span(stage, datetime.now(timezone.utc).isoformat())
        self.spans.append(span)
        start = perf_counter()
        try:
            yield span
        except BaseException:
            span.status = "provider_error"
            raise
        finally:
            span.finished_at = datetime.now(timezone.utc).isoformat()
            span.elapsed_ms = max(0, (perf_counter() - start) * 1000)

    def to_dict(self):
        if self.status not in STATUSES or any(
            s.status not in STATUSES or s.finished_at is None for s in self.spans
        ):
            raise ValueError("완료된 관측만 내보냅니다.")
        return dict(
            schema_version="knowledge-observation.v1",
            trace_id=self.trace_id,
            corpus_version=self.corpus_version,
            generator_kind=self.generator_kind,
            status=self.status,
            spans=[asdict(s) for s in self.spans],
        )
