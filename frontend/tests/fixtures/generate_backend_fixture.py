"""웹 대조용 합성 fixture 생성기. 저장소 루트에서 실행한다.

    PYTHONPATH=. python frontend/tests/fixtures/generate_backend_fixture.py \
        > frontend/tests/fixtures/backend-agent-app.json

실제 8001 합성 Agent 앱(FRS/api/agent_app.py)을 FastAPI TestClient로 호출해
SSE 응답·스트림 전 HTTP 거절·GET /health 본문을 그대로 기록한다. 선택 모델은 기존
테스트 대역이며 Ollama·네트워크·DB·상주 서버를 쓰지 않는다. 선수명·ID·수치는
seed 123 합성 생성기의 가상 값이다.
"""

import json
import subprocess
import sys
from datetime import timedelta
from unittest.mock import Mock

sys.path.insert(0, "tests")

from fastapi.testclient import TestClient  # noqa: E402
from test_candidate_selection import selection  # noqa: E402
from test_schema import OBSERVED  # noqa: E402
from test_synthetic_material import config  # noqa: E402

from FRS.agent.extraction import ExtractionError  # noqa: E402
from FRS.agent.material import generate_synthetic_material  # noqa: E402
from FRS.api.agent_app import create_agent_app  # noqa: E402
from FRS.api.agent_runtime import (  # noqa: E402
    AgentService,
    AgentSettings,
    ModelVerification,
)

ORIGIN = "http://localhost:5173"
READY = "예산 6조, 급여 210, 리버풀, 4-2-3-1, 5~8강으로 구성해줘"
STREAMS = [
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


def settings():
    return AgentSettings(
        run_id="synthetic-test",
        observed_at=OBSERVED,
        model="qwen3:8b",
        model_digest="a" * 64,
        seed=123,
        members_per_family=3,
    )


def selector(ok):
    if ok:
        return Mock(select=lambda registry: json.dumps(selection(registry)))
    failing = Mock()
    failing.select.side_effect = ExtractionError("fixture")
    return failing


def verified():
    return ModelVerification("qwen3:8b", "a" * 64, OBSERVED, "0.33.0")


def client(material, *, ok=True, verification=True, clock=OBSERVED, broken=False):
    def factory():
        if broken:
            raise ValueError("fixture: not configured")
        return AgentService(
            settings(), material, selector(ok), verified() if verification else None
        )

    return TestClient(create_agent_app(service_factory=factory, clock=lambda: clock))


def body(name, message):
    request_id = "fixture-" + name.replace("_", "-")
    return request_id, dict(
        contract_version="web-agent.v0", request_id=request_id, message=message
    )


def post(c, payload=None, *, content=None, content_type="application/json"):
    headers = {"origin": ORIGIN, "content-type": content_type}
    raw = content if content is not None else json.dumps(payload).encode()
    return c.post("/recommend/squad/stream", content=raw, headers=headers)


def main():
    material = generate_synthetic_material(config())
    streams = []
    for name, message, ok in STREAMS:
        request_id, payload = body(name, message)
        with client(material, ok=ok) as c:
            r = post(c, payload)
        assert r.status_code == 200, (name, r.status_code)
        streams.append(
            dict(name=name, request_id=request_id, message=message, sse=r.text)
        )

    _, valid = body("http", READY)
    rejections = []
    with client(material) as c:
        cases = [
            ("origin_not_allowed", dict(headers_origin="http://localhost:4173")),
            ("json_required", dict(content_type="text/plain")),
            ("body_too_large", dict(content=b"{" + b" " * 65536 + b"}")),
            ("invalid_request", dict(content=b'{"contract_version":"web-agent.v0"}')),
        ]
        for name, opts in cases:
            if "headers_origin" in opts:
                r = c.post(
                    "/recommend/squad/stream",
                    content=json.dumps(valid).encode(),
                    headers={
                        "origin": opts["headers_origin"],
                        "content-type": "application/json",
                    },
                )
            else:
                r = post(
                    c,
                    valid,
                    content=opts.get("content"),
                    content_type=opts.get("content_type", "application/json"),
                )
            rejections.append(dict(name=name, status=r.status_code, body=r.json()))
    with client(material, broken=True) as c:
        r = post(c, valid)
        rejections.append(
            dict(name="agent_unavailable", status=r.status_code, body=r.json())
        )

    health = []
    variants = [
        ("ready", {}),
        ("data_expired", dict(clock=OBSERVED + timedelta(days=2))),
        ("model_unverified", dict(verification=False)),
        ("not_configured", dict(broken=True)),
    ]
    for name, opts in variants:
        with client(material, **opts) as c:
            r = c.get("/health", headers={"origin": ORIGIN})
        health.append(dict(name=name, status=r.status_code, body=r.json()))

    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True)
    json.dump(
        dict(
            backend_sha=sha.strip(),
            observed_at=OBSERVED.isoformat(),
            streams=streams,
            rejections=rejections,
            health=health,
        ),
        sys.stdout,
        ensure_ascii=False,
        indent=1,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
