"""순차 요청, 제한된 재시도와 중단 신호를 제공한다."""

import math
import time
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class RequestPolicy:
    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    request_interval: float = 1.0
    max_retries: int = 1
    retry_backoff: float = 2.0
    max_response_seconds: float = 30.0

    def __post_init__(self):
        for name in (
            "connect_timeout",
            "read_timeout",
            "request_interval",
            "retry_backoff",
            "max_response_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name}은 유한한 양수여야 합니다.")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise ValueError("max_retries는 0 이상의 정수여야 합니다.")
        if self.max_retries < 0:
            raise ValueError("max_retries는 0 이상의 정수여야 합니다.")


class CollectionStopped(RuntimeError):
    """차단·지연·비정상 응답을 감지하면 상위 수집 루프까지 중단한다."""

    def __init__(self, failure):
        self.failure = failure
        super().__init__(f"수집 중단: {failure['kind']}")


def fetch_url(chunked_url, *, policy=None, method="GET", data=None, transport=None):
    if method not in ("GET", "POST") or (method == "GET" and data is not None):
        raise ValueError("GET 또는 폼 POST 요청만 지원합니다.")
    policy = policy or RequestPolicy()
    url, player_id, name = chunked_url

    def failure(kind, status_code=None):
        return {"id": player_id, "name": name, "kind": kind, "status_code": status_code}

    for attempt in range(policy.max_retries + 1):
        # 모든 시도 사이에 간격을 둔다. UA·출처 헤더를 임의 생성하지 않는다.
        time.sleep(policy.request_interval)
        started = time.monotonic()
        try:
            options = {
                "timeout": (policy.connect_timeout, policy.read_timeout),
                "allow_redirects": False,
            }
            if data is not None:
                options["data"] = data
            if transport is not None:
                response = transport(method, url, **options)
            else:
                send = requests.get if method == "GET" else requests.post
                response = send(url, **options)
        except requests.Timeout:
            raise CollectionStopped(failure("timeout")) from None
        except requests.ConnectionError:
            raise CollectionStopped(failure("connection_error")) from None
        except requests.RequestException:
            return None, failure("request_error")

        status = response.status_code
        if status in (403, 429):
            response.close()
            raise CollectionStopped(failure("blocked", status))
        if time.monotonic() - started > policy.max_response_seconds:
            response.close()
            raise CollectionStopped(failure("slow_response", status))
        if status in (502, 503, 504):
            response.close()
            if attempt < policy.max_retries:
                time.sleep(policy.retry_backoff * (2**attempt))
                continue
            return None, failure("http_error", status)
        if status == 404:
            response.close()
            return None, failure("not_found", status)
        if status != 200 or not response.content.strip():
            response.close()
            raise CollectionStopped(failure("unexpected_response", status))
        return response, player_id, name


def get_response_one(chunk, *, policy=None):
    output, failed = [], []
    for item in chunk:
        result = fetch_url(item, policy=policy)
        if result[0] is None:
            failed.append(result[1])
        else:
            output.append(result)
    return output, failed


def get_response(chunks, *, policy=None):
    started = time.monotonic()
    output, failed = [], []
    # 차단 이후 새 요청이 시작되지 않도록 기존 프로세스·스레드 병렬 요청을 없앤다.
    for chunk in chunks:
        pages, failures = get_response_one(chunk, policy=policy)
        output.extend(pages)
        failed.extend(failures)
    return output, failed, (time.monotonic() - started) / 60
