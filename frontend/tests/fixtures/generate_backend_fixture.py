"""웹 대조용 합성 fixture 생성기. 저장소 루트에서 실행한다.

    PYTHONPATH=. python frontend/tests/fixtures/generate_backend_fixture.py \
        > frontend/tests/fixtures/backend-w4-1.json

백엔드 W4-1 순수 투영/SSE 코드(FRS/api/agent_projection.py, agent_events.py)를
읽기 전용으로 호출한다. 선택 모델은 테스트 대역이며 Ollama·네트워크·DB·서버를 쓰지 않는다.
선수명·ID·수치는 seed 123 합성 생성기의 가상 값이다.
"""

import json
import subprocess
import sys
from unittest.mock import Mock

sys.path.insert(0, "tests")

from test_candidate_selection import selection  # noqa: E402
from test_schema import OBSERVED  # noqa: E402
from test_synthetic_material import config  # noqa: E402

from FRS.agent.candidate_graph import CandidateSquadAgent  # noqa: E402
from FRS.agent.extraction import ExtractionError  # noqa: E402
from FRS.agent.material import generate_synthetic_material  # noqa: E402
from FRS.api.agent_events import EventSequence  # noqa: E402
from FRS.api.agent_projection import error_outcome, project_result  # noqa: E402

READY = "예산 6조, 급여 210, 리버풀, 4-2-3-1, 5~8강으로 구성해줘"
CASES = [
    ("success_liverpool", READY, True),
    (
        "success_chelsea",
        "예산 5조 8000억, 급여 205, 첼시, 4-1-2-3, 5강,7강으로 구성해줘",
        True,
    ),
    (
        "success_munich",
        "예산 3조, 급여 250, 바이에른 뮌헨, 4-2-2-2, 6~11강으로 구성해줘",
        True,
    ),
    (
        "prepare_unstructured",
        "리버풀 4-2-3-1, 예산 6조 BP, 급여 210 이하, 5~8강으로 구성해줘",
        True,
    ),
    (
        "prepare_unsupported",
        "예산 6조, 급여 210, 리버풀, 4-2-3-1, 4강으로 구성해줘",
        True,
    ),
    (
        "normalizer_missing_salary",
        "예산 6조, 리버풀, 4-2-3-1, 5~8강으로 구성해줘",
        True,
    ),
    ("provider_error", READY, False),
]


def clock():
    return OBSERVED


def selector(ok):
    if ok:
        return Mock(select=lambda registry: json.dumps(selection(registry)))
    failing = Mock()
    failing.select.side_effect = ExtractionError("fixture")
    return failing


def stream(name, outcome, stages):
    seq = EventSequence("fixture-" + name.replace("_", "-"))
    chunks = [seq.started(), *(seq.progress(s) for s in stages)]
    chunks.append(seq.terminal(outcome, clock=clock))
    return b"".join(chunks).decode("utf-8")


def main():
    material = generate_synthetic_material(config())
    cases = []
    for name, source, ok in CASES:
        result = CandidateSquadAgent(
            selector(ok), material=material, batches=material.batches, clock=clock
        ).run(source, t0=OBSERVED)
        outcome = project_result(
            source, result, clock=clock, material_id=material.material_id
        )
        cases.append(
            dict(
                name=name,
                message=source,
                kind=outcome.kind,
                payload=outcome.to_dict(clock=clock),
                sse=stream(name, outcome, ("agent", "render")),
            )
        )
    deadline = error_outcome("request_deadline")
    cases.append(
        dict(
            name="request_deadline",
            message=READY,
            kind=deadline.kind,
            payload=deadline.to_dict(clock=clock),
            sse=stream("request_deadline", deadline, ("agent",)),
        )
    )
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True)
    json.dump(
        dict(backend_sha=sha.strip(), observed_at=OBSERVED.isoformat(), cases=cases),
        sys.stdout,
        ensure_ascii=False,
        indent=1,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
