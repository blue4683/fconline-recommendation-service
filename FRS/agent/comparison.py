"""고정 합성 요청의 1회 로컬 추출 비교. 실제 실행은 명시 옵션으로만 한다."""

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx2

from FRS.agent.ollama import (
    LOCAL_ORIGIN,
    MODELS,
    OllamaExtractionError,
    OllamaExtractor,
    extraction_spec,
)
from FRS.core.request_normalization import ExtractedConditions, normalize_conditions
from FRS.core.request_normalization.amounts import SourceSpan, validate_source_span


@dataclass(frozen=True)
class SmokeCase:
    case_id: str
    original: str
    expected: ExtractedConditions


def smoke_cases():
    """정답을 먼저 고정한다. 모델 응답을 본 뒤 문장/근거를 조정하지 않는다."""
    rows = (
        (
            "complete",
            "예산 1조 5천억, 급여 300, 리버풀, 4-2-3-1, 5~11강으로 구성해줘.",
            ("1조 5천억",),
            "300",
            "리버풀",
            "4-2-3-1",
            "5~11강",
            None,
        ),
        (
            "missing",
            "예산 2조, 첼시, 4-2-2-2, 5강으로 구성해줘.",
            ("2조",),
            None,
            "첼시",
            "4-2-2-2",
            "5강",
            None,
        ),
        (
            "unsupported",
            "예산 2조, 급여 300, 첼시, 4-2-2-2, 5강. 가상선수A 제외.",
            ("2조",),
            "300",
            "첼시",
            "4-2-2-2",
            "5강",
            "가상선수A 제외",
        ),
        (
            "conflicting",
            "예산 1조, 예산 2조, 급여 300, 리버풀, 4-2-3-1, 5강으로 구성해줘.",
            ("1조", "2조"),
            "300",
            "리버풀",
            "4-2-3-1",
            "5강",
            None,
        ),
        (
            "decimal_list",
            "예산 1.5조, 급여 300, 바이에른 뮌헨, 4-1-2-3, 5강,7강으로 구성해줘.",
            ("1.5조",),
            "300",
            "바이에른 뮌헨",
            "4-1-2-3",
            "5강,7강",
            None,
        ),
    )
    result = []
    for name, original, prices, salary, club, formation, levels, unsupported in rows:

        def span(text):
            start = original.index(text)
            return SourceSpan(start=start, end=start + len(text), text=text)

        evidence = ExtractedConditions(
            price_cap=tuple(span(text) for text in prices),
            salary_cap=() if salary is None else (span(salary),),
            team_color=(span(club),),
            formation=(span(formation),),
            allowed_reinforcements=(span(levels),),
            unsupported=() if unsupported is None else (span(unsupported),),
        )
        result.append(SmokeCase(name, original, evidence))
    return tuple(result)


def _signature(result):
    return {
        "status": result.status,
        "conditions": None
        if result.conditions is None
        else result.conditions.model_dump(mode="json"),
        "issues": sorted(
            (issue.field, issue.kind, issue.code) for issue in result.issues
        ),
    }


def assess(case, extracted):
    spans_valid = True
    try:
        for field in ExtractedConditions.model_fields:
            for span in getattr(extracted, field):
                validate_source_span(case.original, span)
    except ValueError:
        spans_valid = False
    actual = normalize_conditions(case.original, extracted)
    expected = normalize_conditions(case.original, case.expected)
    return {
        "spans_valid": spans_valid,
        "evidence_exact": extracted == case.expected,
        "semantics_match": _signature(actual) == _signature(expected),
        "actual": _signature(actual),
        "expected": _signature(expected),
    }


def comparison_plan():
    cases = smoke_cases()
    spec = {
        "inference_limit": 10,
        "source_sha256": {
            name: hashlib.sha256(
                Path(__file__).with_name(name).read_bytes()
            ).hexdigest()
            for name in ("comparison.py", "ollama.py")
        },
        "models": list(MODELS),
        "extraction": extraction_spec(),
        "cases": [
            {
                "case_id": c.case_id,
                "original": c.original,
                "expected": c.expected.model_dump(mode="json"),
            }
            for c in cases
        ],
    }
    encoded = json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()
    return {**spec, "plan_sha256": hashlib.sha256(encoded).hexdigest()}


class LocalMetadataError(RuntimeError):
    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__("로컬 모델 상태 확인에 실패했습니다.")


def local_metadata():
    """읽기 전용 최대 4요청. 자동 다운로드·서버 종료·재시도 없음."""
    attempts = 0
    try:
        with httpx2.Client(
            base_url=LOCAL_ORIGIN, timeout=10, trust_env=False, follow_redirects=False
        ) as client:
            attempts += 1
            version = client.get("/api/version")
            version.raise_for_status()
            attempts += 1
            tags = client.get("/api/tags")
            tags.raise_for_status()
            installed = {item["name"]: item for item in tags.json()["models"]}
            models = {}
            for model in MODELS:
                item = installed[model]
                attempts += 1
                response = client.post("/api/show", json={"model": model})
                response.raise_for_status()
                shown = response.json()
                models[model] = {
                    "digest": item["digest"],
                    "size": item["size"],
                    "capabilities": shown.get("capabilities"),
                    "details": item.get("details"),
                }
        return {
            "version": version.json()["version"],
            "models": models,
            "metadata_http_attempts": attempts,
        }
    except (httpx2.HTTPError, KeyError, ValueError, TypeError) as exc:
        raise LocalMetadataError(attempts) from exc


def _save(path, payload):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        import os

        os.fsync(stream.fileno())
    temporary.replace(path)


def run_comparison(root, run_id, *, factory=OllamaExtractor, metadata=local_metadata):
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", run_id) is None
        or len(run_id) > 64
    ):
        raise ValueError("명시한 안전한 실행 ID가 필요합니다.")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    # 승인된 1회 실행을 다른 ID로 실수로 재시작하는 것도 막는다.
    with (root / "approval-consumed.lock").open("x") as lock:
        lock.write(run_id)
        lock.flush()
        import os

        os.fsync(lock.fileno())
    destination = root / run_id
    destination.mkdir()
    path = destination / "manifest.json"
    manifest = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "preflight",
        "plan": comparison_plan(),
        "attempts": [],
    }
    _save(path, manifest)
    try:
        try:
            manifest["runtime"] = metadata()
        except LocalMetadataError as exc:
            manifest.update(
                status="stopped_preflight",
                error="metadata_error",
                metadata_http_attempts=exc.attempts,
            )
            _save(path, manifest)
            return manifest
        manifest["status"] = "running"
        _save(path, manifest)
        for model in MODELS:
            extractor = factory(model)
            for case in smoke_cases():
                attempt = {
                    "number": len(manifest["attempts"]) + 1,
                    "model": model,
                    "case_id": case.case_id,
                    "status": "pending",
                }
                assert attempt["number"] <= 10
                manifest["attempts"].append(attempt)
                _save(path, manifest)  # 불확실한 시도도 상한에서 제외하지 않는다.
                try:
                    sample = extractor.sample(case.original)
                except OllamaExtractionError as exc:
                    attempt.update(status="failed", error=exc.code, content=exc.content)
                    _save(path, manifest)
                    if exc.stop:
                        manifest["status"] = "stopped"
                        _save(path, manifest)
                        return manifest
                    continue
                attempt.update(
                    status="completed",
                    content=sample.content,
                    extracted=sample.extracted.model_dump(mode="json"),
                    elapsed_seconds=sample.elapsed_seconds,
                    metrics=dict(sample.metrics),
                    assessment=assess(case, sample.extracted),
                )
                _save(path, manifest)
        manifest["status"] = "completed"
        _save(path, manifest)
        return manifest
    except BaseException:
        manifest["status"] = "interrupted_or_failed"
        _save(path, manifest)
        raise


def main():
    parser = argparse.ArgumentParser(description="승인된 1회 로컬 추출 비교")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(comparison_plan(), ensure_ascii=False, indent=2))
        return
    root = Path(__file__).resolve().parents[2] / "artifacts" / "llm-comparisons"
    result = run_comparison(root, args.run_id)
    print(
        json.dumps(
            {
                "run_id": result["run_id"],
                "status": result["status"],
                "inference_attempts": len(result["attempts"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
