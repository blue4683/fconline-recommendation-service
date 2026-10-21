"""고정 과거 합성 시각에서 직접/stdio 결과를 대조한다. 모델/HTTP 요청은 없다."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import sys
from datetime import datetime
from pathlib import Path

from mcp import Client, StdioServerParameters

from FRS.agent.candidates import FIELDS, prepare_candidates
from FRS.agent.comparison import _save, smoke_cases
from FRS.agent.material import SyntheticLeagueSpec
from FRS.mcp.client import call_build
from FRS.mcp.contract import BuildRequest, mcp_spec
from FRS.mcp.service import MCPService
from FRS.schema.scope import FORMATION_SLOTS, PRIORITY_CLUBS


def cases():
    normal = [
        (
            f"normal-{club_index}-{formation}",
            f"예산 2조, 급여 999, {club}, {formation}, 5강,11강으로 구성해줘.",
        )
        for club_index, club in enumerate(PRIORITY_CLUBS)
        for formation in FORMATION_SLOTS
    ]
    return normal + [
        (c.case_id, c.original)
        for c in smoke_cases()
        if c.case_id in ("missing", "unsupported", "conflicting")
    ]


def fixture_request(original, request_id):
    """대조 시험 전용 ID 정답이다. 운영 selector/실패 fallback으로 사용하지 않는다."""
    prepared = prepare_candidates(original)
    selected = {}
    if prepared.registry is not None:
        selected = dict(
            candidate_set_id=prepared.registry.candidate_set_id,
            **{
                field: [
                    v.candidate_id
                    for v in prepared.registry.candidates
                    if v.field == field
                ]
                for field in FIELDS
            },
            unsupported=[],
        )
    return dict(request_id=request_id, original=original, selection=selected)


async def compare(spec, out):
    report = dict(
        mode="offline_fixed_synthetic_clock",
        current_readiness=False,
        model_http_requests=0,
        retries=0,
        specification=spec.model_dump(mode="json"),
        sdk=importlib.metadata.version("mcp"),
        python=sys.version.split()[0],
        policy=mcp_spec()["mcp_policy_version"],
        cases=[],
        status="running",
    )
    _save(out / "manifest.json", report)
    marker = out / "subprocess-exited.txt"
    spec_json = spec.model_dump_json()
    code = (
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
    direct = MCPService(spec, clock=lambda: spec.observed_at)
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable, args=["-c", code], cwd=root
    )
    try:
        async with Client(parameters, read_timeout_seconds=30) as client:
            report["protocol_version"] = client.protocol_version
            report["tools"] = [t.name for t in (await client.list_tools()).tools]
            resource = await client.read_resource("frs://supported-conditions")
            _save(out / "resource.json", json.loads(resource.contents[0].text))
            for case_id, original in cases():
                request = fixture_request(original, case_id)
                expected = json.loads(
                    direct.build(BuildRequest.model_validate(request)).encode(
                        clock=direct.clock
                    )
                )
                tool = await call_build(client, request)
                actual = tool.structured_content
                assert actual == json.loads(tool.content[0].text)
                _save(
                    out / (case_id + ".json"),
                    dict(request=request, direct=expected, mcp=actual),
                )
                report["cases"].append(
                    dict(
                        case_id=case_id,
                        passed=actual == expected,
                        outcome=actual["outcome"],
                        status=None
                        if actual["presentation"] is None
                        else actual["presentation"]["status"],
                    )
                )
                _save(out / "manifest.json", report)
        report["subprocess_exited"] = marker.read_text() == "exited"
        report["status"] = (
            "completed"
            if all(c["passed"] for c in report["cases"]) and report["subprocess_exited"]
            else "failed"
        )
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
    finally:
        direct.close()
        _save(out / "manifest.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description="실모델 없는 고정 합성 stdio MCP 대조")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = SyntheticLeagueSpec(
        run_id=args.run_id,
        observed_at=datetime.fromisoformat(args.observed_at),
        seed=123,
        members_per_family=3,
    )
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    plan = dict(
        specification=spec.model_dump(mode="json"),
        cases=cases(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    _save(out / "plan.json", plan)
    report = asyncio.run(compare(spec, out))
    print(
        json.dumps(
            dict(
                status=report["status"],
                cases=len(report["cases"]),
                protocol=report.get("protocol_version"),
                model_http_requests=0,
            )
        )
    )
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
