"""웹 web-agent.v2 대조용 합성 fixture 생성기. 저장소 루트에서 실행한다.

    PYTHONPATH=. python frontend/tests/fixtures/generate_backend_v2_fixture.py \
        > frontend/tests/fixtures/backend-agent-v2.json

실제 Agent 앱(FRS/api/agent_app.py)을 FastAPI TestClient로 호출해 v2 SSE 응답·
스트림 전 HTTP 거절·GET /objectives 본문을 그대로 기록한다. 선택 모델은 기존 테스트
대역이며 Ollama·네트워크·DB·상주 서버를 쓰지 않는다. 관측 형식 파일도 seed 123 합성
리그에서 임시로 만들고, 다요소 점수에 필요한 양발·신체는 백엔드 테스트
(tests/test_web_agent_v2.py)와 같은 가상 값으로 채운다. 정책은 저장소의 예제
정책 파일이다. 실제 수집 자료·선수명·수치를 읽지 않는다.
"""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

sys.path.insert(0, "tests")

from fastapi.testclient import TestClient  # noqa: E402
from test_candidate_selection import selection  # noqa: E402
from test_observed_agent_api import RUN_ID, write_run  # noqa: E402
from test_schema import OBSERVED  # noqa: E402
from test_synthetic_material import config  # noqa: E402

from FRS.agent.extraction import ExtractionError  # noqa: E402
from FRS.agent.material import generate_synthetic_material  # noqa: E402
from FRS.agent.real_material import ObservedMaterial  # noqa: E402
from FRS.api.agent_app import create_agent_app  # noqa: E402
from FRS.api.agent_runtime import (  # noqa: E402
    AgentService,
    AgentSettings,
    ModelVerification,
    ObjectiveSettings,
    load_objective,
)
from FRS.core.squad_recommend.multi_factor_policy import ScoringPolicy  # noqa: E402

ORIGIN = "http://localhost:5173"
READY = "예산 6조, 급여 310, 리버풀, 4-2-3-1, 5~8강으로 구성해줘"
POLICY_PATH = "docs/examples/multi-factor-core-policy.json"
POLICY = ScoringPolicy.model_validate(json.loads(Path(POLICY_PATH).read_text()))
MULTI = "gk_priority_multi_factor"

# (이름, 원문, 방식, 자료, 선택 대역 정상 여부)
STREAMS = [
    ("synthetic_ovr_success", READY, "ovr", "synthetic", True),
    ("observed_ovr_success", READY, "ovr", "observed", True),
    ("observed_multi_success", READY, MULTI, "observed", True),
    (
        "observed_multi_chelsea",
        "예산 5조 8000억, 첼시, 4-1-2-3, 5강,7강으로 구성해줘",
        MULTI,
        "observed",
        True,
    ),
    (
        "observed_multi_infeasible",
        READY.replace("예산 6조", "예산 0BP"),
        MULTI,
        "observed",
        True,
    ),
    (
        "observed_multi_missing_budget",
        READY.replace("예산 6조, ", ""),
        MULTI,
        "observed",
        True,
    ),
    (
        "observed_ovr_infeasible",
        READY.replace("예산 6조", "예산 0BP"),
        "ovr",
        "observed",
        True,
    ),
    (
        "observed_ovr_missing_budget",
        READY.replace("예산 6조, ", ""),
        "ovr",
        "observed",
        True,
    ),
    (
        "observed_ovr_conflict",
        READY.replace("예산 6조", "예산 6조, 예산 5조"),
        "ovr",
        "observed",
        True,
    ),
    ("observed_multi_provider_error", READY, MULTI, "observed", False),
]


def settings(mode):
    return AgentSettings(
        data_mode=mode,
        run_id=RUN_ID if mode == "observed" else "synthetic-test",
        observed_at=None if mode == "observed" else OBSERVED,
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


def client(material, objective_settings, *, ok=True, broken=False):
    mode = getattr(material, "data_mode", "synthetic")

    def factory():
        if broken:
            raise ValueError("fixture: not configured")
        objective, reason = load_objective(objective_settings, mode)
        return AgentService(
            settings(mode),
            material,
            selector(ok),
            verified(),
            objective=objective,
            objective_reason=reason,
        )

    return TestClient(create_agent_app(service_factory=factory, clock=lambda: OBSERVED))


def body(name, message, objective, contract="web-agent.v2"):
    request_id = "fixture-v2-" + name.replace("_", "-")
    data = dict(contract_version=contract, request_id=request_id, message=message)
    if objective is not None:
        data["objective"] = objective
    return request_id, data


def post(c, payload):
    headers = {"origin": ORIGIN, "content-type": "application/json"}
    return c.post(
        "/recommend/squad/stream", content=json.dumps(payload).encode(), headers=headers
    )


def build_fixture(synthetic, observed):
    configured = ObjectiveSettings(POLICY_PATH, POLICY.version)
    materials = dict(synthetic=synthetic, observed=observed)

    streams = []
    for name, message, objective, mode, ok in STREAMS:
        request_id, payload = body(name, message, objective)
        with client(materials[mode], configured, ok=ok) as c:
            r = post(c, payload)
        assert r.status_code == 200, (name, r.status_code, r.text)
        streams.append(
            dict(
                name=name,
                request_id=request_id,
                message=message,
                objective=objective,
                sse=r.text,
            )
        )

    rejections = []
    cases = [
        # 합성 자료는 다요소 방식을 제공하지 않으므로 실행 전 409다.
        ("objective_unavailable", synthetic, configured, MULTI, "web-agent.v2"),
        (
            "objective_not_configured",
            observed,
            ObjectiveSettings(),
            MULTI,
            "web-agent.v2",
        ),
        ("v2_without_objective", observed, configured, None, "web-agent.v2"),
        ("v1_with_objective", observed, configured, "ovr", "web-agent.v1"),
        ("v2_unknown_objective", observed, configured, "best", "web-agent.v2"),
    ]
    for name, material, objective_settings, objective, contract in cases:
        _, payload = body(name, READY, objective, contract)
        with client(material, objective_settings) as c:
            r = post(c, payload)
        rejections.append(dict(name=name, status=r.status_code, body=r.json()))

    objectives = []
    listings = [
        ("observed_available", observed, configured, False),
        ("observed_not_configured", observed, ObjectiveSettings(), False),
        (
            "observed_policy_mismatch",
            observed,
            ObjectiveSettings(POLICY_PATH, "0" * 64),
            False,
        ),
        ("synthetic_material", synthetic, configured, False),
        ("agent_unavailable", observed, configured, True),
    ]
    for name, material, objective_settings, broken in listings:
        with client(material, objective_settings, broken=broken) as c:
            r = c.get("/objectives", headers={"origin": ORIGIN})
        objectives.append(dict(name=name, status=r.status_code, body=r.json()))

    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True)
    json.dump(
        dict(
            backend_sha=sha.strip(),
            fixture_data="generated_synthetic_only_including_observed_mode",
            observed_at=OBSERVED.isoformat(),
            policy_path=POLICY_PATH,
            streams=streams,
            rejections=rejections,
            objectives=objectives,
        ),
        sys.stdout,
        ensure_ascii=False,
        indent=1,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    # 새 관측 파일도 전부 합성 리그에서 만든다. 실제 수집 자료를 읽지 않는다.
    with (
        TemporaryDirectory() as directory,
        patch(
            "socket.socket.connect", side_effect=AssertionError("fixture 네트워크 금지")
        ),
        patch(
            "socket.socket.connect_ex",
            side_effect=AssertionError("fixture 네트워크 금지"),
        ),
    ):
        synthetic = generate_synthetic_material(config())
        records = synthetic.to_dict()["records"]
        # 다요소 점수가 쓰는 양발·신체를 카드마다 다르게 채운다(백엔드 테스트와 같은 가상 값).
        for index, row in enumerate(records["cards"]):
            row.update(
                left_foot=1 + index % 5,
                right_foot=5,
                height=170 + index % 20,
                weight=65 + index % 15,
                physical="가상체형_A",
            )
        root = write_run(Path(directory), records)
        build_fixture(synthetic, ObservedMaterial(root, RUN_ID))
