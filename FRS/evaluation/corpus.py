"""고정 문서/사전 정답을 검증하고 후보 생성과 독립된 원문 근거를 구성한다."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from FRS.agent.candidates import FIELDS
from FRS.core.request_normalization import ExtractedConditions, NormalizedConditions
from FRS.core.request_normalization.amounts import SourceSpan

ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = Path(__file__).with_name("cases.v1.json")
DOCUMENT_PATH = ROOT / "docs/stage-w6-evaluation-cases.md"
CORPUS_SHA256 = "bbe7f3b26384e0c274e871b87c8fb489f240e8574049425d70ab1c35b6005b0e"
DOCUMENT_SHA256 = "3eaac390ea8536b413d83d50e37fe8f7c387d38e844988185a905a8864a8db9e"


class ExpectedIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    field: str
    code: str
    span_text: str | None


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    case_id: str
    group: Literal["N", "I", "U", "M", "A"]
    original: str
    evidence: dict[str, list[str]]
    conditions: NormalizedConditions | None
    stage: Literal["prepare", "normalize", "engine"]
    outcomes: list[str]
    issues: list[ExpectedIssue]

    def expected_evidence(self):
        # 정답 토큰의 출현 위치만 찾는다. 후보 준비/모델 출력으로 정답을 만들지 않는다.
        evidence = {}
        if set(self.evidence) != set(FIELDS):
            raise ValueError("사전 정답의 필드 집합이 달라졌습니다.")
        for field, texts in self.evidence.items():
            cursor, spans = 0, []
            for text in texts:
                # 숫자 0을 '10조' 내부에서 찾지 않도록 문서의 접두사까지 대조한다.
                prefix = {"price_cap": "예산 ", "salary_cap": "급여 "}.get(field, "")
                start = self.original.index(prefix + text, cursor) + len(prefix)
                cursor = start + len(text)
                spans.append(SourceSpan(start=start, end=cursor, text=text))
            evidence[field] = tuple(spans)
        return ExtractedConditions(**evidence)


def load_cases():
    raw = CORPUS_PATH.read_bytes()
    data = json.loads(raw)
    document = DOCUMENT_PATH.read_bytes()
    if (
        hashlib.sha256(raw).hexdigest() != CORPUS_SHA256
        or set(data) != {"version", "document_sha256", "cases"}
        or data["version"] != "w6-cases.v1"
        or data["document_sha256"] != DOCUMENT_SHA256
        or hashlib.sha256(document).hexdigest() != DOCUMENT_SHA256
    ):
        raise ValueError("고정 코퍼스/사전 문서 버전이 일치하지 않습니다.")
    cases = tuple(EvaluationCase.model_validate(row) for row in data["cases"])
    expected_ids = {
        f"{group}{index:02}"
        for group, count in dict(N=8, I=3, U=5, M=4, A=5).items()
        for index in range(1, count + 1)
    }
    if (
        len(cases) != 25
        or {case.case_id for case in cases} != expected_ids
        or Counter(c.group for c in cases) != dict(N=8, I=3, U=5, M=4, A=5)
    ):
        raise ValueError("고정 25사례의 ID/그룹이 달라졌습니다.")
    originals = re.findall(
        r"^\| ([NIUMA]\d\d) \| `([^`]+)` \|", document.decode(), re.M
    )
    if originals != [(c.case_id, c.original) for c in cases]:
        raise ValueError("사전 문서의 입력/순서와 일치하지 않습니다.")
    for case in cases:
        case.expected_evidence()
        if (
            case.group != case.case_id[0]
            or (case.stage == "engine") != (case.conditions is not None)
            or (case.stage == "engine") == bool(case.issues)
        ):
            raise ValueError("사전 정답의 단계/조건/사유가 일치하지 않습니다.")
    return cases
