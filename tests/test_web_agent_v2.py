"""web-agent.v2 방식 선택·다요소 응답과 v1 불변을 합성 리그의 관측 실행 형식으로 검증한다.

실제 관측 자료·네트워크·모델을 쓰지 않는다. 다요소 점수에 필요한 양발·신체는 가상 값으로 채운다.
"""

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_candidate_selection import selection
from test_observed_agent_api import SOURCE, settings, verified, write_run
from test_schema import OBSERVED
from test_synthetic_agent_api import parsed
from test_synthetic_material import config

from FRS.agent.candidate_graph import CandidateSquadAgent
from FRS.agent.material import generate_synthetic_material
from FRS.api.agent_app import create_agent_app
from FRS.api.agent_contract import WebFinalV2
from FRS.api.agent_projection import project_result
from FRS.api.agent_runtime import (
    AgentService,
    ObjectiveSettings,
    build_service,
    load_objective,
)
from FRS.core.squad_recommend.multi_factor_policy import ScoringPolicy
from FRS.core.squad_recommend.objective import GK_PRIORITY

POLICY_PATH = "docs/examples/multi-factor-core-policy.json"
POLICY = ScoringPolicy.model_validate(
    json.loads((Path(__file__).resolve().parents[1] / POLICY_PATH).read_text())
)
CONFIGURED = ObjectiveSettings(POLICY_PATH, POLICY.version)


def body(objective=None, contract="web-agent.v2", message=SOURCE):
    data = dict(contract_version=contract, request_id="request-1", message=message)
    if objective is not None:
        data["objective"] = objective
    return data


@pytest.fixture(scope="module")
def processed(tmp_path_factory):
    records = generate_synthetic_material(config()).to_dict()["records"]
    # 다요소 점수가 쓰는 양발·신체를 카드마다 다르게 채운다(가상 값).
    for index, row in enumerate(records["cards"]):
        row.update(
            left_foot=1 + index % 5,
            right_foot=5,
            height=170 + index % 20,
            weight=65 + index % 15,
            physical="가상체형_A",
        )
    return write_run(tmp_path_factory.mktemp("processed"), records)


def make_service(processed, objective_settings=CONFIGURED):
    built = build_service(
        settings(),
        clock=lambda: OBSERVED,
        verifier=verified,
        processed_root=processed,
        objective_settings=objective_settings,
    )
    return AgentService(
        built.settings,
        built.material,
        Mock(select=Mock(side_effect=lambda r: json.dumps(selection(r)))),
        built.verification,
        objective=built.objective,
        objective_reason=built.objective_reason,
    )


@pytest.fixture(scope="module")
def service(processed):
    return make_service(processed)


@pytest.fixture(scope="module")
def plain(processed):
    return make_service(processed, ObjectiveSettings())


def post(service, data):
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        return client.post("/recommend/squad/stream", json=data)


def final(response):
    events = parsed(response)
    return events, events[-1]["payload"]


@pytest.fixture(scope="module")
def results(service, plain):
    return dict(
        v1=final(post(service, body(contract="web-agent.v1")))[1],
        v1_plain=final(post(plain, body(contract="web-agent.v1")))[1],
        ovr=final(post(service, body("ovr"))),
        multi=final(post(service, body(GK_PRIORITY))),
    )


def test_objectives_listing_reports_the_pinned_policy(service, plain):
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        listing = client.get("/objectives").json()
    assert listing["schema_version"] == "agent-objectives.v1"
    assert listing["default"] == "ovr"
    ovr, multi = listing["objectives"]
    assert (ovr["id"], ovr["available"], ovr["reason"]) == ("ovr", True, None)
    assert (multi["id"], multi["available"], multi["reason"]) == (
        GK_PRIORITY,
        True,
        None,
    )
    assert multi["policy"] == dict(
        policy_id=POLICY.policy_id, version=POLICY.version, status="experimental"
    )
    assert multi["parameters"] == dict(
        gk_exchange=15, outfield_tolerance=5, gk_tolerance=2, search_seconds=30.0
    )
    unavailable = plain.objectives()["objectives"][1]
    assert (unavailable["available"], unavailable["reason"]) == (
        False,
        "not_configured",
    )


def test_unconfigured_app_lists_nothing_as_available(tmp_path):
    with TestClient(
        create_agent_app(
            service_factory=lambda: build_service(
                settings(),
                clock=lambda: OBSERVED,
                processed_root=tmp_path,
                objective_settings=CONFIGURED,
            ),
            clock=lambda: OBSERVED,
        )
    ) as client:
        response = client.get("/objectives")
    assert response.status_code == 503
    assert all(
        (o["available"], o["reason"]) == (False, "agent_unavailable")
        for o in response.json()["objectives"]
    )


@pytest.mark.parametrize(
    "objective_settings, reason",
    [
        (ObjectiveSettings(), "not_configured"),
        (ObjectiveSettings(POLICY_PATH, None), "not_configured"),
        (ObjectiveSettings(POLICY_PATH, "0" * 64), "policy_mismatch"),
        (
            ObjectiveSettings("docs/examples/missing.json", POLICY.version),
            "policy_mismatch",
        ),
    ],
)
def test_policy_must_be_explicit_and_match_its_hash(objective_settings, reason):
    assert load_objective(objective_settings, "observed") == (None, reason)


def test_synthetic_material_never_offers_the_multi_factor_objective():
    assert load_objective(CONFIGURED, "synthetic") == (None, "synthetic_material")


def test_v1_response_is_unchanged_by_configuring_the_objective(results):
    assert results["v1"] == results["v1_plain"]
    assert results["v1"]["versions"]["web_contract"] == "web-agent.v1"
    assert "objective" not in results["v1"]


def test_v2_ovr_adds_only_ovr_split_and_empty_scores(results):
    events, shown = results["ovr"]
    assert {e["contract_version"] for e in events} == {"web-agent.v2"}
    v1 = results["v1"]
    assert shown["status"] == v1["status"] and shown["answer"] == v1["answer"]
    assert shown["objective"] == dict(id="ovr", policy=None, stages=[])
    squad = shown["squad"]
    assert squad["total_score_units"] is None and squad["total_score"] is None
    assert all(r["score_units"] is None and r["score"] is None for r in squad["rows"])
    assert squad["outfield_ovr"] + squad["gk_ovr"] == squad["total_ovr"]
    stripped = [
        {k: v for k, v in r.items() if k not in ("score_units", "score")}
        for r in squad["rows"]
    ]
    assert stripped == v1["squad"]["rows"]


def test_v2_multi_factor_reports_scores_stages_and_policy(results):
    events, shown = results["multi"]
    assert shown["status"] in ("optimal", "feasible")
    assert shown["versions"]["web_contract"] == "web-agent.v2"
    objective = shown["objective"]
    assert objective["id"] == GK_PRIORITY
    assert objective["policy"]["version"] == POLICY.version
    assert [s["name"] for s in objective["stages"]] == [
        "outfield_ovr",
        "gk_ovr",
        "multi_factor",
    ]
    squad = shown["squad"]
    units = [r["score_units"] for r in squad["rows"]]
    assert all(isinstance(u, int) for u in units)
    assert squad["total_score_units"] == sum(units)
    assert squad["total_score"] == f"{sum(units) // 100}.{sum(units) % 100:02d}"
    assert all(
        r["score"] == f"{r['score_units'] // 100}.{r['score_units'] % 100:02d}"
        for r in squad["rows"]
    )
    assert all(r["performance"] == r["raw_ovr"] / 2 for r in squad["rows"])
    assert "실험 다요소 점수" in shown["answer"] and POLICY.policy_id in shown["answer"]
    assert objective["stages"][2]["value"] == squad["total_score_units"]


def test_unavailable_objective_is_rejected_before_running(processed):
    fresh = make_service(processed, ObjectiveSettings())
    response = post(fresh, body(GK_PRIORITY))
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "objective_unavailable"}}
    fresh.selector.select.assert_not_called()


@pytest.mark.parametrize(
    "data",
    [
        body(None),
        body("best"),
        {**body(contract="web-agent.v1"), "objective": "ovr"},
    ],
)
def test_request_objective_is_required_only_and_exactly_in_v2(service, data):
    response = post(service, data)
    assert response.status_code == 422


def run_projection(service, objective, contract="web-agent.v2"):
    result = CandidateSquadAgent(
        service.selector,
        material=service.material,
        batches=service.batches,
        clock=lambda: OBSERVED,
        fixed_salary_cap=service.fixed_salary_cap,
        objective=objective,
    ).run(SOURCE, t0=OBSERVED)
    return project_result(
        SOURCE,
        result,
        clock=lambda: OBSERVED,
        provenance=service.provenance,
        material_id=service.material_id,
        data_mode="observed",
        fixed_salary_cap=service.fixed_salary_cap,
        contract=contract,
        objective=objective,
    )


def test_expired_v2_result_drops_the_squad_and_stage_summary(service):
    outcome = run_projection(service, service.objective)
    shown = outcome.to_dict(clock=lambda: OBSERVED + timedelta(hours=24))
    assert shown["status"] == "data_expired" and shown["squad"] is None
    assert shown["objective"]["stages"] == []
    assert shown["objective"]["policy"]["version"] == POLICY.version


def test_v1_projection_never_carries_a_multi_factor_objective(service):
    outcome = run_projection(service, service.objective, contract="web-agent.v1")
    assert outcome.kind == "error"


def test_snapshot_policy_must_match_the_service_policy(service, processed):
    other = POLICY.model_copy(update={"policy_id": "other-policy"})
    other = ScoringPolicy.model_validate(other.model_dump())
    result = CandidateSquadAgent(
        service.selector,
        material=service.material,
        batches=service.batches,
        clock=lambda: OBSERVED,
        fixed_salary_cap=service.fixed_salary_cap,
        objective=service.objective,
    ).run(SOURCE, t0=OBSERVED)
    swapped = type(service.objective)(
        policy=other, expected_policy_version=other.version
    )
    outcome = project_result(
        SOURCE,
        result,
        clock=lambda: OBSERVED,
        provenance=service.provenance,
        material_id=service.material_id,
        data_mode="observed",
        fixed_salary_cap=service.fixed_salary_cap,
        contract="web-agent.v2",
        objective=swapped,
    )
    assert outcome.kind == "error"


def mutate(shown, change):
    data = json.loads(json.dumps(shown))
    change(data)
    return data


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["squad"]["rows"][0].update(score="0.00"),
        lambda d: d["squad"].update(
            total_score_units=d["squad"]["total_score_units"] + 1
        ),
        lambda d: d["squad"].update(gk_ovr=d["squad"]["gk_ovr"] + 1),
        lambda d: d["objective"].update(stages=[]),
        lambda d: d["objective"].update(policy=None),
        lambda d: d["squad"]["rows"][0].update(score_units=None, score=None),
        lambda d: d["versions"].update(web_contract="web-agent.v1"),
        lambda d: d["objective"].update(stages=d["objective"]["stages"][::-1]),
        lambda d: d["objective"].update(stages=[d["objective"]["stages"][0]] * 3),
    ],
)
def test_v2_dto_rejects_inconsistent_scores_and_objective(results, change):
    shown = results["multi"][1]
    WebFinalV2.model_validate(shown)
    with pytest.raises(ValidationError):
        WebFinalV2.model_validate(mutate(shown, change))


def test_ovr_v2_dto_rejects_scores_or_policy(results):
    shown = results["ovr"][1]
    WebFinalV2.model_validate(shown)
    with pytest.raises(ValidationError):
        WebFinalV2.model_validate(
            mutate(
                shown,
                lambda d: d["objective"].update(
                    policy=results["multi"][1]["objective"]["policy"]
                ),
            )
        )
