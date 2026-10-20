"""승인된 고정 7사례의 1회 ID 선택 비교. 엔진/DB를 실행하지 않는다."""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from FRS.agent.candidate_selection import CandidateSelectionError, decode_selection
from FRS.agent.candidates import canonical, prepare_candidates
from FRS.agent.comparison import (
    LocalMetadataError,
    SmokeCase,
    _save,
    assess,
    local_metadata,
    smoke_cases,
)
from FRS.agent.ollama_selection import (
    OllamaIDSelector,
    selection_request,
    selector_spec,
)
from FRS.agent.ollama_transport import MODELS, OllamaExtractionError
from FRS.core.request_normalization import ExtractedConditions
from FRS.core.request_normalization.amounts import SourceSpan

INFERENCE_LIMIT = 12
APPROVAL_ROOT = Path("artifacts/candidate-comparisons/approval-20261020-001")


def candidate_cases():
    # 신규 정답은 후보 생성 함수에 의존하지 않고 원문 출현별로 고정한다.
    extra = (
        (
            "commas_order",
            "5강,7강, 리버풀, 예산 150,000BP, 4-2-3-1, 급여 300",
            dict(
                price_cap=("150,000BP",),
                salary_cap=("300",),
                team_color=("리버풀",),
                formation=("4-2-3-1",),
                allowed_reinforcements=("5강,7강",),
            ),
        ),
        (
            "same_budget_twice",
            "예산 1조, 예산 1조, 급여 300, 첼시, 4-2-2-2, 5강",
            dict(
                price_cap=("1조", "1조"),
                salary_cap=("300",),
                team_color=("첼시",),
                formation=("4-2-2-2",),
                allowed_reinforcements=("5강",),
            ),
        ),
    )
    cases = list(smoke_cases())
    for case_id, original, values in extra:
        evidence = {}
        for field, texts in values.items():
            cursor, spans = 0, []
            for text in texts:
                start = original.index(text, cursor)
                cursor = start + len(text)
                spans.append(SourceSpan(start=start, end=cursor, text=text))
            evidence[field] = tuple(spans)
        cases.append(SmokeCase(case_id, original, ExtractedConditions(**evidence)))
    return tuple(cases)


def comparison_plan():
    rows = []
    for case in candidate_cases():
        preparation = prepare_candidates(case.original)
        expected = "needs_clarification" if case.case_id == "unsupported" else "ready"
        issues = sorted({issue.code for issue in preparation.issues})
        if preparation.status != expected or (
            expected != "ready" and issues != ["unrecognized_context"]
        ):
            raise ValueError("고정 사례의 준비 계약이 변경됐습니다.")
        rows.append(
            dict(
                case_id=case.case_id,
                original=case.original,
                expected=case.expected.model_dump(mode="json"),
                expected_preparation=expected,
                preparation=preparation.model_dump(mode="json"),
                candidate_set_id=None
                if preparation.issues
                else preparation.registry.candidate_set_id,
                requests={}
                if preparation.issues
                else {
                    model: selection_request(model, preparation.registry)
                    for model in MODELS
                },
            )
        )
    if sum(row["expected_preparation"] == "ready" for row in rows) * len(MODELS) != 12:
        raise ValueError("승인된 사례 수/추론 상한이 달라졌습니다.")
    here = Path(__file__).parent
    files = [
        here / name
        for name in (
            "candidate_comparison.py",
            "candidates.py",
            "candidate_selection.py",
            "comparison.py",
            "ollama_selection.py",
            "ollama_transport.py",
            "ollama.py",
            "json_contract.py",
        )
    ] + sorted((here.parent / "core" / "request_normalization").glob("*.py"))
    spec = dict(
        inference_limit=INFERENCE_LIMIT,
        metadata_http_limit=4,
        models=list(MODELS),
        selector=selector_spec(),
        source_sha256={
            str(file.relative_to(here.parent)): hashlib.sha256(
                file.read_bytes()
            ).hexdigest()
            for file in files
        },
        cases=rows,
        scope="prepare_select_decode_normalize_only_no_engine_db_collection",
        mode="one_shot_sequential_no_resume_no_retry",
    )
    return {**spec, "plan_sha256": hashlib.sha256(canonical(spec)).hexdigest()}


def run_comparison(root, run_id, *, factory=OllamaIDSelector, metadata=local_metadata):
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", run_id) is None
        or len(run_id) > 64
    ):
        raise ValueError("명시한 안전한 실행 ID가 필요합니다.")
    plan = comparison_plan()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "approval-consumed.lock").open("x") as lock:
        lock.write(run_id)
        lock.flush()
        import os

        os.fsync(lock.fileno())
    destination = root / run_id
    destination.mkdir()
    path = destination / "manifest.json"
    manifest = dict(
        run_id=run_id,
        started_at=datetime.now(timezone.utc).isoformat(),
        status="preflight",
        metadata_status="pending_max_4_requests",
        plan=plan,
        cases=[],
        attempts=[],
    )
    _save(path, manifest)
    try:
        try:
            manifest["runtime"] = metadata()
            manifest["metadata_status"] = "completed"
        except LocalMetadataError as exc:
            manifest.update(
                status="stopped_preflight",
                error="metadata_error",
                metadata_status="failed",
                metadata_http_attempts=exc.attempts,
            )
            _save(path, manifest)
            return manifest
        manifest["status"] = "running"
        _save(path, manifest)
        for model in MODELS:
            selector = factory(model)
            for case, planned in zip(candidate_cases(), plan["cases"], strict=True):
                preparation = prepare_candidates(case.original)
                if preparation.model_dump(mode="json") != planned["preparation"]:
                    raise ValueError("고정 계획과 준비 결과가 달라졌습니다.")
                record = dict(model=model, case_id=case.case_id, status="pending")
                manifest["cases"].append(record)
                if preparation.issues:
                    record.update(
                        status="input_issues",
                        preparation_match=True,
                        issues=[
                            issue.model_dump(mode="json")
                            for issue in preparation.issues
                        ],
                        model_called=False,
                    )
                    _save(path, manifest)
                    continue
                number = len(manifest["attempts"]) + 1
                if number > INFERENCE_LIMIT:
                    raise ValueError("승인된 추론 상한을 초과했습니다.")
                attempt = dict(
                    number=number,
                    model=model,
                    case_id=case.case_id,
                    candidate_set_id=preparation.registry.candidate_set_id,
                    status="pending",
                )
                manifest["attempts"].append(attempt)
                record.update(model_called=True, attempt_number=number)
                _save(path, manifest)  # 전송 전에 미상 시도까지 영구 예약한다.
                try:
                    sample = selector.sample(preparation.registry)
                except OllamaExtractionError as exc:
                    attempt.update(status="failed", error=exc.code, content=exc.content)
                    record.update(status="provider_error", error=exc.code)
                    _save(path, manifest)
                    if exc.stop:
                        manifest["status"] = "stopped"
                        _save(path, manifest)
                        return manifest
                    continue
                attempt.update(
                    content=sample.content,
                    elapsed_seconds=sample.elapsed_seconds,
                    metrics=dict(sample.metrics),
                )
                try:
                    extracted = decode_selection(
                        case.original, preparation.registry, sample.content
                    )
                except CandidateSelectionError as exc:
                    attempt.update(status="invalid_selection", error=exc.code)
                    record.update(status="provider_error", error=exc.code)
                else:
                    assessment = assess(case, extracted)
                    attempt.update(
                        status="completed",
                        extracted=extracted.model_dump(mode="json"),
                        assessment=assessment,
                    )
                    record.update(
                        status="selected",
                        preparation_match=True,
                        evidence_exact=assessment["evidence_exact"],
                        semantics_match=assessment["semantics_match"],
                    )
                _save(path, manifest)
        manifest["status"] = "completed"
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        _save(path, manifest)
        return manifest
    except BaseException:
        manifest["status"] = "interrupted_or_failed"
        _save(path, manifest)
        raise


def main():
    parser = argparse.ArgumentParser(description="승인된 1회 로컬 ID 선택 비교")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(comparison_plan(), ensure_ascii=False, indent=2))
        return
    result = run_comparison(APPROVAL_ROOT, args.run_id)
    print(
        json.dumps(
            {
                "run_id": result["run_id"],
                "status": result["status"],
                "attempts": len(result["attempts"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
