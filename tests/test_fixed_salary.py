"""새 제품 급여 정책과 기존 내부 재현을 합성 자료/선택 대역으로 검증한다."""

import json
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from test_candidate_selection import selection
from test_condition_normalization import extracted_request
from test_observed_agent_api import settings, verified, write_run
from test_schema import OBSERVED
from test_synthetic_agent_api import parsed, request_body
from test_synthetic_material import config

from FRS.agent.candidate_selection import decode_selection
from FRS.agent.candidates import prepare_candidates
from FRS.agent.material import generate_synthetic_material
from FRS.agent.ollama_selection import selection_request
from FRS.api.agent_app import create_agent_app
from FRS.api.agent_runtime import AgentService, build_service
from FRS.core.request_normalization import conditions_spec, normalize_conditions
from FRS.mcp.contract import BuildRequest, supported_resource
from FRS.mcp.service import MCPService

MESSAGE = "예산 6조, 리버풀, 4-2-3-1, 5강으로 구성해줘"


def test_omitted_and_explicit_salary_use_policy_without_fabricating_evidence():
    omitted = extracted_request(omitted=("salary_cap",))
    explicit = extracted_request({"salary_cap": "310"})
    a, b = (
        normalize_conditions(*case, fixed_salary_cap=310)
        for case in (omitted, explicit)
    )
    assert a.conditions == b.conditions and a.conditions.salary_cap == 310
    assert not a.evidence.salary_cap and b.evidence.salary_cap[0].text == "310"
    assert a.normalizer_version == b.normalizer_version
    assert "salary_cap" not in a.supported.required_fields
    assert len(a.supported.required_fields) == 4
    assert conditions_spec(fixed_salary_cap=310)["salary"]["source"] == "system_policy"


def test_legacy_contract_and_recorded_normalizer_hash_are_preserved():
    assert (
        conditions_spec()["normalizer_version"]
        == "c83756caefaa6c33bee73d5b5c8b57ac1646187d6d4d5b446e93147054763e33"
    )
    assert normalize_conditions(*extracted_request()).conditions.salary_cap == 300
    assert (
        normalize_conditions(*extracted_request(omitted=("salary_cap",)))
        .issues[0]
        .field
        == "salary_cap"
    )
    assert (
        conditions_spec(fixed_salary_cap=310)["normalizer_version"]
        != conditions_spec()["normalizer_version"]
    )


@pytest.mark.parametrize("value", ["0", "200", "300", "309", "311", "999"])
def test_other_explicit_salaries_stop_at_preparation(value):
    message = MESSAGE.replace("예산 6조", f"예산 6조, 급여 {value}")
    preparation = prepare_candidates(message, fixed_salary_cap=310)
    assert preparation.status == "needs_clarification"
    assert any(
        i.code == "fixed_salary_cap" and "310" in i.message for i in preparation.issues
    )


@pytest.mark.parametrize("value", ["310", "000310"])
def test_explicit_310_is_a_valid_original_value(value):
    p = prepare_candidates(
        MESSAGE.replace("예산 6조", f"예산 6조, 급여 {value}"), fixed_salary_cap=310
    )
    assert not p.issues


def test_bad_original_span_is_not_replaced_with_policy_value():
    original, evidence = extracted_request({"salary_cap": "310"})
    altered = original.replace("310", "300")
    result = normalize_conditions(altered, evidence, fixed_salary_cap=310)
    assert result.conditions is None and any(
        i.field == "salary_cap" for i in result.issues
    )


def test_selector_requires_empty_salary_ids_for_omission():
    registry = prepare_candidates(MESSAGE, fixed_salary_cap=310).registry
    request = selection_request("qwen3:8b", registry)
    prop = request["format"]["properties"]["salary_cap"]
    assert prop["minItems"] == prop["maxItems"] == 0
    decoded = decode_selection(MESSAGE, registry, json.dumps(selection(registry)))
    assert not decoded.salary_cap
    assert (
        normalize_conditions(
            MESSAGE, decoded, fixed_salary_cap=310
        ).conditions.salary_cap
        == 310
    )


@pytest.fixture
def service(tmp_path):
    records = generate_synthetic_material(config()).to_dict()["records"]
    built = build_service(
        settings(),
        clock=lambda: OBSERVED,
        verifier=verified,
        processed_root=write_run(tmp_path, records),
    )
    return AgentService(
        built.settings,
        built.material,
        Mock(select=Mock(side_effect=lambda r: json.dumps(selection(r)))),
        built.verification,
    )


def test_api_omission_and_explicit_310_return_same_conditions_and_squad(service):
    assert service.fixed_salary_cap == 310
    outputs = []
    with TestClient(
        create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
    ) as client:
        for message in (MESSAGE, MESSAGE.replace("예산 6조", "예산 6조, 급여 310")):
            final = parsed(
                client.post("/recommend/squad/stream", json=request_body(message))
            )[-1]
            assert final["type"] == "final"
            outputs.append(final["payload"])
    assert outputs[0]["status"] == outputs[1]["status"] == "optimal"
    assert outputs[0]["conditions"] == outputs[1]["conditions"]
    assert outputs[0]["squad"] == outputs[1]["squad"]
    assert outputs[0]["conditions"]["salary_cap"] == "310"
    assert int(outputs[0]["squad"]["total_salary"]) <= 310
    assert "salary_cap" not in outputs[0]["supported"]["required_fields"]


def test_wrong_salary_blocks_model_and_material_before_api_execution(service):
    with patch(
        "FRS.api.agent_app.GuardedMaterial.load",
        side_effect=AssertionError("자료 접근 금지"),
    ) as load:
        with TestClient(
            create_agent_app(service_factory=lambda: service, clock=lambda: OBSERVED)
        ) as client:
            final = parsed(
                client.post(
                    "/recommend/squad/stream",
                    json=request_body(
                        MESSAGE.replace("예산 6조", "예산 6조, 급여 300")
                    ),
                )
            )[-1]["payload"]
    assert final["status"] == "needs_clarification" and final["squad"] is None
    assert any(i["code"] == "prepare.fixed_salary_cap" for i in final["issues"])
    service.selector.select.assert_not_called()
    load.assert_not_called()


@pytest.mark.parametrize(
    "st_salary,status,total", [(30, "optimal", "310"), (31, "infeasible", None)]
)
def test_exact_310_total_is_allowed_and_311_minimum_cannot_return_a_table(
    tmp_path, st_salary, status, total
):
    records = generate_synthetic_material(config()).to_dict()["records"]
    st = {r["spid"] for r in records["cards"] if "ST" in r["positions"]}
    for row in records["salaries"]:
        row["salary"] = st_salary if row["spid"] in st else 28
    built = build_service(
        settings(),
        clock=lambda: OBSERVED,
        verifier=verified,
        processed_root=write_run(tmp_path, records),
    )
    svc = AgentService(
        built.settings,
        built.material,
        Mock(select=lambda r: json.dumps(selection(r))),
        built.verification,
    )
    with TestClient(
        create_agent_app(service_factory=lambda: svc, clock=lambda: OBSERVED)
    ) as client:
        final = parsed(
            client.post("/recommend/squad/stream", json=request_body(MESSAGE))
        )[-1]["payload"]
    assert final["status"] == status
    assert final["conditions"]["salary_cap"] == "310"
    assert (None if final["squad"] is None else final["squad"]["total_salary"]) == total


def test_mcp_prompt_resource_and_execution_use_the_same_fixed_policy():
    svc = MCPService(config(), clock=lambda: OBSERVED)
    try:
        resource = supported_resource()
        assert (
            "salary_cap" not in resource["supported"]["required_fields"]
            and "310" in resource["input"]
        )
        prompt = json.loads(svc.prompt(MESSAGE))
        assert prompt["output_schema"]["properties"]["salary_cap"]["maxItems"] == 0
        registry = prepare_candidates(MESSAGE, fixed_salary_cap=310).registry
        body = BuildRequest(
            request_id="fixed-310", original=MESSAGE, selection=selection(registry)
        )
        response = json.loads(svc.build(body).encode(clock=svc.clock))
        assert response["presentation"]["conditions"]["salary_cap"] == "310"
        assert response["presentation"]["status"] == "optimal"
    finally:
        svc.close()
