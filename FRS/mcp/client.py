"""Tool 호출은 정확히 한 번 보낸다. 스키마 복구 재전송/다중 라운드를 하지 않는다."""

from mcp import Client
from mcp.types import CallToolResult


async def call_build(
    client: Client, request: dict, *, timeout_seconds=150
) -> CallToolResult:
    # SDK 고수준 Client.call_tool은 HEADER_MISMATCH에 자동 재전송한다.
    # 공개 세션 API는 같은 결과 스키마 검사를 유지하면서 한 번만 전송한다.
    return await client.session.call_tool(
        "build_squad",
        {"request": request},
        read_timeout_seconds=timeout_seconds,
        allow_input_required=False,
        allow_claimed=False,
    )
