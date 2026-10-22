"""실데이터 요청의 전송 시작 간격과 시각을 기록한다. 네트워크를 호출하지 않는다."""

import time
from dataclasses import dataclass
from datetime import datetime, timezone

SECOND_NS = 1_000_000_000


@dataclass(frozen=True)
class SendStamp:
    sent_at: datetime
    started_ns: int

    def __post_init__(self):
        if not isinstance(self.sent_at, datetime) or self.sent_at.utcoffset() is None:
            raise ValueError("전송 시각에는 시간대가 필요합니다.")
        if type(self.started_ns) is not int or self.started_ns < 0:
            raise ValueError("단조 전송 시각은 비음수 정수 ns여야 합니다.")


@dataclass(frozen=True)
class TimedBody:
    body: bytes
    stamp: SendStamp

    def __post_init__(self):
        if not isinstance(self.body, bytes) or not isinstance(self.stamp, SendStamp):
            raise ValueError("응답 bytes와 검증된 전송 시각이 필요합니다.")


class SendStartPacer:
    """직전 Session.request 진입 직전부터 최소 1초를 보장한다. 순차 전용이다."""

    def __init__(self, *, monotonic_ns=None, sleep=None, clock=None):
        self.monotonic_ns = monotonic_ns or time.monotonic_ns
        self.sleep = sleep or time.sleep
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_started_ns = None

    def _now(self):
        value = self.monotonic_ns()
        if type(value) is not int or value < 0:
            raise ValueError("단조 시계는 비음수 정수 ns를 반환해야 합니다.")
        if self.last_started_ns is not None and value < self.last_started_ns:
            raise ValueError("단조 시계가 이전 전송 시각보다 뒤로 이동했습니다.")
        return value

    def wait(self):
        if self.last_started_ns is None:
            return
        # sleep이 일찍 반환해도 실제 단조 시각이 기한에 도달할 때까지 기다린다.
        while (remaining := self.last_started_ns + SECOND_NS - self._now()) > 0:
            self.sleep(remaining / SECOND_NS)

    def mark_started(self):
        sent_at = self.clock()
        started = self._now()
        if (
            self.last_started_ns is not None
            and started - self.last_started_ns < SECOND_NS
        ):
            raise ValueError("전송 시작 간격이 1초보다 짧습니다.")
        stamp = SendStamp(sent_at, started)
        self.last_started_ns = started
        return stamp
