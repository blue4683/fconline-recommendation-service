"""공식 SDK2의 로컬 stdio 기능. 호스트 등록/HTTP/모델 호출은 하지 않는다."""

import json
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context, UserMessage
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import JsonValue

from FRS.agent.candidates import canonical
from FRS.mcp.contract import BuildEnvelope, error_response, supported_resource
from FRS.mcp.service import MCPService


def create_server(spec, *, service_factory=None):
    @asynccontextmanager
    async def lifespan(server):
        service = service_factory() if service_factory else MCPService(spec)
        if not isinstance(service, MCPService):
            raise TypeError("명시 MCPService가 필요합니다.")
        try:
            yield service
        finally:
            service.close()

    server = MCPServer(
        "FRS Synthetic Squad",
        version="mcp-build.v1",
        lifespan=lifespan,
        log_level="ERROR",
        instructions="합성 자료 전용입니다. 원문/후보 ID를 바꾸지 않고 자동 재시도하지 않습니다.",
    )

    @server.tool(
        name="build_squad",
        description="원문과 후보 ID 선택을 검증해 고정 합성 자료에서 스쿼드를 구성합니다. 모델 호출 없음.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def build_squad(
        request: dict[str, JsonValue], ctx: Context
    ) -> Annotated[CallToolResult, BuildEnvelope]:
        service = ctx.request_context.lifespan_context
        result = await service.invoke(request)
        try:
            encoded = result.encode(clock=service.clock)
        except Exception:
            encoded = error_response("internal_error").encode(clock=service.clock)
        payload = json.loads(encoded)
        return CallToolResult(
            content=[TextContent(type="text", text=encoded.decode())],
            structured_content=payload,
            is_error=payload["error"] is not None,
        )

    @server.resource(
        "frs://supported-conditions",
        name="supported_conditions",
        description="정식 지원 조건과 후보 한도. 자료/모델/엔진을 조회하지 않습니다.",
        mime_type="application/json",
    )
    def supported_conditions() -> str:
        return canonical(supported_resource()).decode()

    @server.prompt(
        name="select_conditions",
        description="정형 원문의 후보 ID와 바인딩된 출력 스키마를 제공합니다. 모델 실행 없음.",
    )
    def select_conditions(original: str, ctx: Context) -> list[UserMessage]:
        return [UserMessage(ctx.request_context.lifespan_context.prompt(original))]

    return server
