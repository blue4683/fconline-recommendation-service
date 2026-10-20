"""고정 loopback 채팅 전송. 원문 해석/재시도/다운로드를 수행하지 않는다."""

import asyncio
import time
from dataclasses import dataclass

import httpx2

from FRS.agent.extraction import ExtractionError
from FRS.agent.json_contract import strict_json

LOCAL_ORIGIN = "http://127.0.0.1:11434"
MODELS = ("qwen3.5:2b", "qwen3:8b")
MAX_RESPONSE_BYTES = 131072


class OllamaExtractionError(ExtractionError):
    def __init__(self, code, *, stop=False, content=None):
        self.code, self.stop, self.content = code, stop, content
        super().__init__("로컬 조건 추출에 실패했습니다.")


@dataclass(frozen=True)
class ChatSample:
    content: str
    elapsed_seconds: float
    metrics: tuple[tuple[str, int], ...]


async def local_chat(request, *, transport=None):
    if request.get("model") not in MODELS:
        raise ValueError("승인된 비교 모델이 필요합니다.")
    started = time.perf_counter()
    try:
        async with asyncio.timeout(120):
            async with httpx2.AsyncClient(
                base_url=LOCAL_ORIGIN,
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=120,
            ) as client:
                async with client.stream("POST", "/api/chat", json=request) as response:
                    if response.status_code != 200:
                        raise OllamaExtractionError("http_error", stop=True)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise OllamaExtractionError("response_too_large", stop=True)
    except (TimeoutError, httpx2.TimeoutException) as exc:
        raise OllamaExtractionError("deadline", stop=True) from exc
    except httpx2.HTTPError as exc:
        raise OllamaExtractionError("connection_error", stop=True) from exc
    try:
        payload = strict_json(raw.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("done") is not True
            or payload.get("model") != request["model"]
            or not isinstance(payload.get("message"), dict)
            or payload["message"].get("role") != "assistant"
            or not isinstance(payload["message"].get("content"), str)
        ):
            raise ValueError("invalid_envelope")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise OllamaExtractionError("invalid_envelope", stop=True) from exc
    content = payload["message"]["content"]
    if payload.get("done_reason") != "stop":
        raise OllamaExtractionError("incomplete_output", content=content)
    metrics = tuple(
        (key, value)
        for key in (
            "total_duration",
            "load_duration",
            "prompt_eval_count",
            "prompt_eval_duration",
            "eval_count",
            "eval_duration",
        )
        if type(value := payload.get(key)) is int and value >= 0
    )
    return ChatSample(content, time.perf_counter() - started, metrics)
