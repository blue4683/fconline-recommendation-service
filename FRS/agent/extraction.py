"""추출기 주입 계약. 실제 모델 호출은 어댑터가 별도로 구현한다."""

from typing import Protocol

from FRS.core.request_normalization import ExtractedConditions


class ExtractionError(ValueError):
    """호출/응답 실패. 원문·비밀값·모델 초안을 외부 오류로 복사하지 않는다."""


class ConditionExtractor(Protocol):
    def extract(self, original: str) -> ExtractedConditions: ...
