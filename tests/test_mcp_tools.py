"""모델/외부 네트워크 없이 MCP 경계·실제 stdio·중단/정밀도를 검증한다."""

import asyncio
import json
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from mcp import Client, StdioServerParameters
from test_candidate_selection import SOURCE, selection
from test_schema import OBSERVED
from test_synthetic_material import config

from FRS.agent.candidates import prepare_candidates
from FRS.agent.material import generate_synthetic_material
from FRS.mcp.client import call_build
from FRS.mcp.contract import BuildRequest, MCPResponse, error_response
from FRS.mcp.server import create_server
from FRS.mcp.service import MCPService
from FRS.schema.scope import FORMATION_SLOTS, PRIORITY_CLUBS


@pytest.fixture(scope="module")
def material():
    return generate_synthetic_material(config())


@pytest.fixture
def service(material):
    svc = MCPService(
        config(), clock=Mock(return_value=OBSERVED), factory=Mock(return_value=material)
    )
    yield svc
    svc.close()


def request(original=SOURCE, request_id="mcp-1"):
    prepared = prepare_candidates(original)
    return dict(
        request_id=request_id,
        original=original,
        selection=selection(prepared.registry) if prepared.registry else {},
    )


def content(result):
    assert result.structured_content == json.loads(result.content[0].text)
    assert len(result.content) == 1
    return result.structured_content


async def call(service, body):
    async with Client(
        create_server(config(), service_factory=lambda: service), raise_exceptions=True
    ) as client:
        return await call_build(client, body)


@pytest.mark.parametrize("club", PRIORITY_CLUBS)
@pytest.mark.parametrize("formation", tuple(FORMATION_SLOTS))
def test_same_fixed_request_context_direct_and_mcp_return_identical_verified_results(
    service, club, formation
):
    original = SOURCE.replace("리버풀", club).replace("4-2-3-1", formation)
    body = request(original)
    direct = json.loads(
        service.build(BuildRequest.model_validate(body)).encode(clock=service.clock)
    )
    result = asyncio.run(call(service, body))
    actual = content(result)
    assert not result.is_error and actual == direct
    shown = actual["presentation"]
    assert shown["status"] == "optimal" and len(shown["squad"]["rows"]) == 11
    assert [r["slot"] for r in shown["squad"]["rows"]] == list(
        FORMATION_SLOTS[formation]
    )
    assert actual["versions"]["model"] == "not_invoked_client_selection"


@pytest.mark.parametrize(
    "original,code",
    [
        (SOURCE.replace("리버풀", "리버풀 제외"), "unrecognized_context"),
    ],
)
def test_preparation_blocks_material_clock_and_engine(service, original, code):
    result = asyncio.run(call(service, request(original)))
    body = content(result)
    assert not result.is_error and body["outcome"] == "input_issues"
    assert body["presentation"] is None and any(
        i["code"] == code for i in body["issues"]
    )
    service.clock.assert_not_called()
    service.factory.assert_not_called()


@pytest.mark.parametrize(
    "original,field,code",
    [
        (SOURCE.replace("급여 999, ", ""), "salary_cap", "missing_condition"),
        (
            SOURCE.replace("예산 1조 5천억", "예산 1조, 예산 2조"),
            "price_cap",
            "conflicting_values",
        ),
    ],
)
def test_normalization_problems_never_generate_or_load_material(
    service, original, field, code
):
    body = content(asyncio.run(call(service, request(original))))
    assert body["presentation"]["status"] == "needs_clarification"
    assert any(
        i["field"] == field and i["code"] == code
        for i in body["presentation"]["issues"]
    )
    assert body["presentation"]["squad"] is None
    service.clock.assert_not_called()
    service.factory.assert_not_called()


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda s: s.update(candidate_set_id="a" * 64), "candidate_set_mismatch"),
        (lambda s: s.update(salary_cap=[]), "incomplete_coverage"),
        (lambda s: s["salary_cap"].append(s["salary_cap"][0]), "duplicate_candidate"),
        (lambda s: s.update(salary_cap=["v-127"]), "unknown_candidate"),
        (
            lambda s: s.update(price_cap=s["salary_cap"], salary_cap=[]),
            "field_mismatch",
        ),
        (lambda s: s.update(unsupported=["ctx-000"]), "invalid_context_selection"),
        (lambda s: s.update(unknown=True), "invalid_selection"),
    ],
)
def test_client_selection_errors_are_not_repaired_or_sent_to_material(
    service, change, code
):
    body = request()
    change(body["selection"])
    result = asyncio.run(call(service, body))
    assert result.is_error and content(result)["error"]["code"] == code
    service.clock.assert_not_called()
    service.factory.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "contains space"},
        {"request_id": "x" * 65},
        {"original": 123},
        {"original": "\ud800"},
        {"selection": []},
        {"unknown": "no"},
        {"original": "가" * 24000},
        {"original": "a" * 4001},
    ],
)
def test_invalid_application_arguments_are_safe_and_do_not_claim_caller_id(
    service, changes
):
    body = {**request(), **changes}
    response = asyncio.run(service.invoke(body))
    actual = json.loads(response.encode(clock=service.clock))
    assert actual["error"]["code"] == "invalid_request"
    assert actual["request_id"] is None and not service.busy
    service.clock.assert_not_called()
    service.factory.assert_not_called()


def test_resource_and_prompt_use_existing_bound_ids_without_material_or_model(service):
    async def exercise():
        with patch(
            "FRS.agent.ollama_selection.OllamaIDSelector.sample",
            side_effect=AssertionError("model forbidden"),
        ):
            async with Client(
                create_server(config(), service_factory=lambda: service),
                raise_exceptions=True,
            ) as client:
                tools = (await client.list_tools()).tools
                assert [t.name for t in tools] == ["build_squad"]
                assert (
                    tools[0].output_schema["properties"]["contract_version"]["const"]
                    == "mcp-build.v1"
                )
                assert (
                    tools[0].annotations.read_only_hint
                    and not tools[0].annotations.idempotent_hint
                )
                assert [
                    str(r.uri) for r in (await client.list_resources()).resources
                ] == ["frs://supported-conditions"]
                resource = await client.read_resource("frs://supported-conditions")
                assert json.loads(resource.contents[0].text)["data_mode"] == "synthetic"
                assert [p.name for p in (await client.list_prompts()).prompts] == [
                    "select_conditions"
                ]
                prompt = await client.get_prompt(
                    "select_conditions", {"original": SOURCE}
                )
                value = json.loads(prompt.messages[0].content.text)
                registry = prepare_candidates(SOURCE).registry
                assert value["candidates"]["original"] == SOURCE
                assert value["output_schema"]["properties"]["candidate_set_id"][
                    "enum"
                ] == [registry.candidate_set_id]
                assert value["model_invoked"] is False
                blocked = await client.get_prompt(
                    "select_conditions", {"original": SOURCE + " 제외"}
                )
                assert (
                    json.loads(blocked.messages[0].content.text)["outcome"]
                    == "input_issues"
                )
        service.clock.assert_not_called()
        service.factory.assert_not_called()

    asyncio.run(exercise())


def test_final_expiry_removes_squad_without_extending_or_mutating_snapshot(service):
    result = asyncio.run(service.invoke(request()))
    initial = json.loads(result.encode(clock=lambda: OBSERVED))
    expired = json.loads(result.encode(clock=lambda: OBSERVED + timedelta(days=2)))
    assert initial["presentation"]["status"] == "optimal"
    assert (
        expired["presentation"]["status"] == "data_expired"
        and expired["presentation"]["squad"] is None
    )
    assert initial["presentation"]["snapshot"] == expired["presentation"]["snapshot"]
    assert json.loads(result.encode(clock=lambda: OBSERVED)) == initial


def test_large_exact_bp_is_a_decimal_string_and_infeasible_has_no_table(service):
    body = request(SOURCE.replace("1조 5천억", str(10**34 - 1) + "BP"))
    result = content(asyncio.run(call(service, body)))
    assert result["presentation"]["conditions"]["price_cap"] == str(10**34 - 1)
    # Client 종료 시 서비스가 닫히므로 별도 명시 인스턴스를 쓴다.
    other = MCPService(config(), clock=lambda: OBSERVED, factory=service.factory)
    try:
        shown = content(
            asyncio.run(call(other, request(SOURCE.replace("1조 5천억", "1BP"))))
        )["presentation"]
        assert shown["status"] == "infeasible" and shown["squad"] is None
    finally:
        other.close()


def test_internal_exception_has_safe_code_without_private_exception_text(service):
    service.factory.side_effect = RuntimeError("private password /local/path")
    result = asyncio.run(call(service, request()))
    assert result.is_error and content(result)["error"]["code"] == "internal_error"
    assert "private" not in json.dumps(result.model_dump()) and not service.busy


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("stop", ["cancel", "deadline", "close"])
def test_slot_remains_until_physical_worker_ends_and_later_stages_never_run(
    service, stop
):
    entered, release = threading.Event(), threading.Event()
    clock = [0.0]
    service.monotonic = lambda: clock[0]

    def blocked(body, *, check):
        entered.set()
        assert release.wait(3)
        check()
        raise AssertionError("cancelled worker must not proceed")

    service.build = blocked

    async def exercise():
        running = asyncio.create_task(service.invoke(request()))
        try:
            await wait_until(entered.is_set)
            if stop == "cancel":
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            elif stop == "deadline":
                clock[0] = 151
                output = await running
                assert (
                    json.loads(output.encode(clock=service.clock))["error"]["code"]
                    == "request_deadline"
                )
            else:
                service.close()
                await running
            assert service.busy
            if stop != "close":
                busy = await service.invoke(request(request_id="second"))
                assert (
                    json.loads(busy.encode(clock=service.clock))["error"]["code"]
                    == "busy"
                )
            release.set()
            await wait_until(lambda: not service.busy)
            service.factory.assert_not_called()
        finally:
            release.set()
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(exercise())


def test_sdk_client_cancellation_does_not_free_the_real_worker_slot(service):
    entered, release = threading.Event(), threading.Event()

    def blocked(body, *, check):
        entered.set()
        assert release.wait(3)
        check()
        raise AssertionError("must stop before material")

    service.build = blocked

    async def exercise():
        async with Client(
            create_server(config(), service_factory=lambda: service),
            raise_exceptions=True,
        ) as client:
            task = asyncio.create_task(call_build(client, request()))
            try:
                await wait_until(entered.is_set)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert service.busy
                assert (
                    content(await call_build(client, request(request_id="other")))[
                        "error"
                    ]["code"]
                    == "busy"
                )
                release.set()
                await wait_until(lambda: not service.busy)
                service.factory.assert_not_called()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_real_stdio_subprocess_negotiation_prompt_tool_and_exit_without_network():
    root = Path(__file__).resolve().parents[1]
    # 고정 과거 시각에서는 명확화만 확인한다. 시계 갱신/LLM 호출은 없다.
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "FRS.mcp",
            "--run-id",
            "mcp-stdio-test",
            "--observed-at",
            OBSERVED.isoformat(),
        ],
        cwd=root,
    )

    async def exercise():
        async with Client(parameters, read_timeout_seconds=10) as client:
            assert [t.name for t in (await client.list_tools()).tools] == [
                "build_squad"
            ]
            resource = await client.read_resource("frs://supported-conditions")
            assert (
                json.loads(resource.contents[0].text)["versions"]["model"]
                == "not_invoked_client_selection"
            )
            prompt = await client.get_prompt("select_conditions", {"original": SOURCE})
            assert json.loads(prompt.messages[0].content.text)["model_invoked"] is False
            result = await call_build(client, request(SOURCE.replace("급여 999, ", "")))
            assert content(result)["presentation"]["status"] == "needs_clarification"

    asyncio.run(exercise())
    invalid = subprocess.run(
        [
            sys.executable,
            "-m",
            "FRS.mcp",
            "--run-id",
            "mcp-invalid",
            "--observed-at",
            "no-date",
        ],
        cwd=root,
        capture_output=True,
        timeout=5,
    )
    assert invalid.returncode != 0 and not invalid.stdout


def test_same_request_id_runs_again_without_cache_or_model_fallback(service):
    calls = Mock(wraps=service.build)
    service.build = calls

    async def exercise():
        first = await service.invoke(request())
        second = await service.invoke(request())
        assert first.payload == second.payload and calls.call_count == 2
        assert not service.busy

    asyncio.run(exercise())


def test_safe_error_does_not_echo_unknown_code_or_call_clock():
    clock = Mock(side_effect=AssertionError)
    value = json.loads(error_response("private /path").encode(clock=clock))
    assert value["error"]["code"] == "internal_error" and "private" not in json.dumps(
        value
    )
    clock.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body["presentation"]["squad"].update(total_price="1"),
        lambda body: body["presentation"]["snapshot"].update(id="different"),
        lambda body: body["versions"].update(model="invented-real-model"),
        lambda body: body.update(context=None),
    ],
)
def test_response_boundary_rejects_mutated_totals_snapshot_and_model_claims(
    service, mutation
):
    result = asyncio.run(service.invoke(request()))
    body = json.loads(result.payload)
    mutation(body)
    with pytest.raises(ValueError):
        MCPResponse(json.dumps(body).encode(), result.snapshot).encode(
            clock=service.clock
        )


def test_unknown_status_has_no_squad(service):
    from FRS.core.squad_recommend.solver import EngineResult

    def unknown(snapshot, **options):
        return EngineResult(
            snapshot_id=snapshot.snapshot_id,
            method="cp_sat",
            status="unknown",
        )

    # 실제 스냅샷은 만들되 탐색기의 미확정 응답을 주입한다.
    with patch("FRS.core.squad_recommend.build.solve_squad", side_effect=unknown):
        result = asyncio.run(service.invoke(request()))
    shown = json.loads(result.encode(clock=service.clock))["presentation"]
    assert shown["status"] == "unknown" and shown["squad"] is None


def test_wrong_synthetic_spec_is_rejected_before_recommending(service):
    service.factory.return_value = generate_synthetic_material(
        config(run_id="different")
    )
    body = json.loads(
        asyncio.run(service.invoke(request())).encode(clock=service.clock)
    )
    assert body["error"]["code"] == "internal_error" and body["presentation"] is None


def test_success_requires_its_original_snapshot(service):
    result = asyncio.run(service.invoke(request()))
    with pytest.raises(ValueError):
        MCPResponse(result.payload).encode(clock=service.clock)


def test_real_stdio_fixed_context_matches_direct_success_and_exits(service, tmp_path):
    marker = tmp_path / "server-exited.txt"
    root = Path(__file__).resolve().parents[1]
    spec_json = config().model_dump_json()
    script = (
        "from pathlib import Path\n"
        "from FRS.agent.material import SyntheticLeagueSpec\n"
        "from FRS.mcp.server import create_server\n"
        "from FRS.mcp.service import MCPService\n"
        f"spec=SyntheticLeagueSpec.model_validate_json({spec_json!r})\n"
        "try:\n"
        " create_server(spec,service_factory=lambda:MCPService(spec,clock=lambda:spec.observed_at)).run(transport='stdio')\n"
        "finally:\n"
        f" Path({str(marker)!r}).write_text('exited')\n"
    )
    parameters = StdioServerParameters(
        command=sys.executable, args=["-c", script], cwd=root
    )
    body = request()
    expected = json.loads(
        service.build(BuildRequest.model_validate(body)).encode(clock=service.clock)
    )

    async def exercise():
        async with Client(parameters, read_timeout_seconds=15) as client:
            assert client.protocol_version == "2026-07-28"
            assert client.server_info.name == "FRS Synthetic Squad"
            actual = await call_build(client, body)
            assert not actual.is_error and content(actual) == expected

    asyncio.run(exercise())
    assert marker.read_text() == "exited"


@pytest.mark.parametrize(
    "error",
    [RuntimeError("transport failed"), TimeoutError(), ValueError("invalid result")],
)
def test_one_shot_client_never_retries_or_falls_back(error):
    once = AsyncMock(side_effect=error)
    client = SimpleNamespace(
        session=SimpleNamespace(call_tool=once), call_tool=AsyncMock()
    )
    with pytest.raises(type(error)):
        asyncio.run(call_build(client, request()))
    once.assert_awaited_once()
    client.call_tool.assert_not_called()


def test_schema_mismatch_is_not_relisted_or_resent():
    from mcp.shared.exceptions import MCPError
    from mcp.types import HEADER_MISMATCH

    once = AsyncMock(side_effect=MCPError(HEADER_MISMATCH, "schema changed"))
    client = SimpleNamespace(
        session=SimpleNamespace(call_tool=once),
        call_tool=AsyncMock(),
        list_tools=AsyncMock(),
    )
    with pytest.raises(MCPError):
        asyncio.run(call_build(client, request()))
    once.assert_awaited_once()
    client.call_tool.assert_not_called()
    client.list_tools.assert_not_called()
