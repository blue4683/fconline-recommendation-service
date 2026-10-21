"""승인된 qwen3:8b 1회 평가. 고정25문장·최대16추론/2상태·재시도0."""

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx2

from FRS.agent.candidate_graph import CandidateSquadAgent
from FRS.agent.candidate_selection import CandidateSelection
from FRS.agent.candidates import canonical, prepare_candidates
from FRS.agent.comparison import _save
from FRS.agent.json_contract import strict_json
from FRS.agent.material import SyntheticLeagueSpec, generate_synthetic_material
from FRS.agent.ollama_selection import (
    OllamaIDSelector,
    selection_request,
    selector_spec,
)
from FRS.agent.ollama_transport import OllamaExtractionError
from FRS.api.agent_projection import SelectionProvenance, project_result
from FRS.api.agent_runtime import AgentSettings, utc_now, verify_model
from FRS.evaluation.corpus import ROOT, load_cases
from FRS.evaluation.offline import (
    _MeasuredMaterial,
    assess_case,
    evaluation_plan,
    latency,
    rate,
    summarize,
    verify_fixture,
)

RUN_ID = "w6-model-20261021-001"
MODEL = "qwen3:8b"
DIGEST = "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"
INFERENCE_LIMIT = 16
METADATA_LIMIT = 2
APPROVAL_ROOT = ROOT / "artifacts/agent-model-evaluation/approval-20261021-001"


def model_plan(spec):
    if spec.run_id != RUN_ID:
        raise ValueError("승인된 새 실행 ID가 필요합니다.")
    base = evaluation_plan(spec)
    base.pop("plan_sha256")
    base.update(
        version="w6-model.v1",
        mode="real_model_on_synthetic_material",
        clock_mode="explicit_batch_observation_with_wall_response_clock",
        actual_model_requests="reserved_before_each_attempt",
        external_requests=0,
        model=MODEL,
        model_digest=DIGEST,
        inference_limit=INFERENCE_LIMIT,
        metadata_limit=METADATA_LIMIT,
        selector=selector_spec(),
        stopped_execution="preserve_lock_no_resume_no_unused_allowance_reuse",
        preparations=[],
    )
    for case in load_cases():
        prepared = prepare_candidates(case.original)
        base["preparations"].append(
            dict(
                case_id=case.case_id,
                preparation=prepared.model_dump(mode="json"),
                request=None
                if prepared.issues
                else selection_request(MODEL, prepared.registry),
            )
        )
    if sum(p["request"] is not None for p in base["preparations"]) != INFERENCE_LIMIT:
        raise ValueError("고정된 추론 상한과 준비 결과가 다릅니다.")
    return json.loads(
        canonical({**base, "plan_sha256": hashlib.sha256(canonical(base)).hexdigest()})
    )


def model_summary(report):
    result = summarize(report["cases"])
    result["metric_mode"] = "real_model_on_synthetic_material"
    attempts = report["inference_attempts"]
    for key, field in (("schema", "schema_valid"), ("evidence", "evidence_valid")):
        result[key] = rate(
            sum(r.get(field) is True for r in report["cases"]), len(attempts)
        )
    result["actual_model_metrics"] = {
        key: result[key] for key in ("schema", "evidence", "selected_semantics")
    }
    result["model_latency"] = latency(
        [a["elapsed_seconds"] for a in attempts if a.get("elapsed_seconds") is not None]
    )
    result["load_inclusive"] = True
    result["token_totals"] = {
        key: {
            "sum_available": sum(
                a["metrics"][key] for a in attempts if key in a.get("metrics", {})
            )
            if any(key in a.get("metrics", {}) for a in attempts)
            else None,
            "available_attempts": sum(key in a.get("metrics", {}) for a in attempts),
            "total_attempts": len(attempts),
        }
        for key in ("prompt_eval_count", "eval_count")
    }
    return result


def render_model_report(report):
    summary = report["summary"]
    final = report["status"] == "completed"
    lines = [
        "# W6 실모델·합성 자료 평가",
        "",
        f"실행 `{report['run_id']}` / 상태 `{report['status']}` / 모델 `{MODEL}`",
        "고정25문장 중 준비 차단9개는 코드 판단이며 모델이 이해한9개가 아닙니다.",
        "최종 측정 결과입니다."
        if final
        else "중단/진행 중 결과입니다. 미실행은 분모에서 제거하지 않으며 최종 정확도로 주장하지 않습니다.",
        f"상태 요청 {len(report['metadata_attempts'])}/{METADATA_LIMIT}, 추론 시도 {len(report['inference_attempts'])}/{INFERENCE_LIMIT}, 재시도/fallback0",
        "",
        "| 지표 | 결과 |",
        "|---|---|",
    ]
    for key in (
        "completeness",
        "schema",
        "evidence",
        "selected_semantics",
        "semantics",
        "e2e",
        "valid_squads",
        "constraints",
    ):
        value = summary[key]
        label = (
            "N/A"
            if value["rate"] is None
            else f"{value['passed']}/{value['total']} ({value['rate']:.1%})"
        )
        lines.append(f"| {key} | {label} |")
    lines.extend(["", "| 사례 | 실행 | 결과 | 의미 | E2E |", "|---|---|---|---|---|"])
    for row in report["cases"]:
        score = row.get("assessment", {})
        lines.append(
            f"| {row['case_id']} | {row['execution']} | {row.get('actual_status', '미실행')} | {score.get('semantics_match', False)} | {score.get('e2e_match', False)} |"
        )
    lines.extend(
        [
            "",
            "같은 공개 정형 회귀 세트의 측정이며 일반 한국어 이해/운영 SLA/실제 선수 추천 품질을 뜻하지 않습니다.",
            "시간은 로컬 기록/Agent/최종 직렬화 경로이며 사후 채점은 제외합니다. keep_alive=0이므로 모델 호출 시간은 매번 로드 포함입니다.",
            "모델 토큰·로드/추론 시간과 nearest-rank p50/p95·표본 수는 manifest.json에 기록합니다. 미제공 값은 null/별도 제공 표본 수이며0으로 보충하지 않습니다.",
            "서술은 코드 템플릿이며 LLM 서술 비교/통과율 목표/전력·장비 비용 측정은 없습니다.",
            f"계획 SHA256: `{report['plan']['plan_sha256']}`",
            "",
        ]
    )
    return "\n".join(lines)


class _MetadataTransport(httpx2.BaseTransport):
    def __init__(self, inner, report, save):
        self.inner, self.report, self.save = inner, report, save

    def handle_request(self, request):
        records = self.report["metadata_attempts"]
        expected_path = (
            ("/api/version", "/api/tags")[len(records)]
            if len(records) < METADATA_LIMIT
            else None
        )
        if (
            request.method != "GET"
            or request.url.host != "127.0.0.1"
            or request.url.port != 11434
            or request.url.path != expected_path
        ):
            raise ValueError("허용한 상태 요청 범위를 벗어났습니다.")
        record = dict(number=len(records) + 1, path=expected_path, status="pending")
        records.append(record)
        self.save()  # 연결 전에 미상 시도까지 예약하고 fsync한다.
        try:
            response = self.inner.handle_request(request)
        except Exception as exc:
            record.update(status="failed", error_type=type(exc).__name__)
            self.save()
            raise
        record.update(status="response_received", http_status=response.status_code)
        self.save()
        return response

    def close(self):
        self.inner.close()


class _ModelSelector:
    def __init__(self, case, report, save, selector):
        self.case, self.report, self.save, self.selector = case, report, save, selector
        self.attempts, self.schema_valid, self.raw = 0, None, None
        self.fatal = False

    def select(self, registry):
        attempts = self.report["inference_attempts"]
        if len(attempts) >= INFERENCE_LIMIT or self.attempts:
            raise ValueError("추론 상한/재시도 금지 범위를 벗어났습니다.")
        self.attempts += 1
        record = dict(
            number=len(attempts) + 1,
            case_id=self.case.case_id,
            candidate_set_id=registry.candidate_set_id,
            status="pending",
        )
        attempts.append(record)
        self.save()
        started = time.perf_counter()
        try:
            sample = self.selector.sample(registry)
        except OllamaExtractionError as exc:
            self.schema_valid = False
            self.raw = exc.content
            self.fatal = exc.stop
            record.update(
                status="failed",
                error=exc.code,
                stop=exc.stop,
                content=exc.content,
                elapsed_seconds=time.perf_counter() - started,
            )
            self.save()
            raise
        except Exception as exc:
            self.schema_valid, self.fatal = False, True
            record.update(
                status="failed",
                error_type=type(exc).__name__,
                stop=True,
                elapsed_seconds=time.perf_counter() - started,
            )
            self.save()
            raise OllamaExtractionError("connection_error", stop=True) from exc
        self.raw = sample.content
        record.update(
            content=sample.content,
            metrics=dict(sample.metrics),
            elapsed_seconds=sample.elapsed_seconds,
            status="completed",
        )
        try:
            CandidateSelection.model_validate(strict_json(sample.content))
            self.schema_valid = True
        except (ValueError, TypeError):
            self.schema_valid = False
            record["status"] = "invalid_selection_schema"
        self.save()
        return sample.content


def _evaluate(case, material, spec, report, save, *, selector, clock):
    selected = _ModelSelector(case, report, save, selector)
    measured = _MeasuredMaterial(material)
    t0 = clock()
    started = time.perf_counter()
    agent = CandidateSquadAgent(
        selected, material=measured, batches=material.batches, clock=clock
    )
    result = agent.run(case.original, t0=t0)
    provenance = (
        None
        if not selected.attempts
        else SelectionProvenance(MODEL, DIGEST, selector_spec()["selector_version"])
    )
    projected = project_result(
        case.original,
        result,
        clock=clock,
        provenance=provenance,
        material_id=material.material_id,
    )
    body = projected.to_dict(clock=clock)
    elapsed = time.perf_counter() - started
    score = assess_case(
        case,
        result,
        body,
        selector_attempts=selected.attempts,
        material_loads=measured.loads,
        when=clock(),
    )
    row = dict(
        case_id=case.case_id,
        group=case.group,
        execution="technical_error" if projected.kind == "error" else "completed",
        expected_statuses=case.outcomes,
        actual_status=body.get("status", "technical_error"),
        t0=t0.isoformat(),
        response_checked_at=clock().isoformat(),
        outcome=result.outcome,
        stages=list(result.stages),
        selector_kind="ollama_qwen3_8b",
        selector_attempts=selected.attempts,
        actual_model_requests=selected.attempts,
        schema_valid=selected.schema_valid,
        evidence_valid=None
        if not selected.attempts
        else result.outcome == "agent_result",
        material_loads=measured.loads,
        raw_selection=selected.raw,
        expected_evidence=case.expected_evidence().model_dump(mode="json"),
        preparation=result.preparation.model_dump(mode="json"),
        normalization=None
        if result.agent_result is None
        else result.agent_result.normalization.model_dump(mode="json"),
        presentation=body,
        assessment=score,
        elapsed_seconds=elapsed,
        material_load_seconds=measured.elapsed_seconds,
        prepare_seconds=None,
        normalize_seconds=None,
        solver_seconds=None,
        retries=0,
        fallback=0,
    )
    return row, selected.fatal


def _unchanged(plan):
    for name, expected in plan["source_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError("실행 계획 뒤 소스가 변경됐습니다.")
    # 고정 문서와 JSON도 매번 확인한다. 원문/정답을 바꾸어 남은 실행을 계속하지 않는다.
    load_cases()


def run_model_evaluation(
    spec,
    *,
    approval_root=APPROVAL_ROOT,
    metadata_transport=None,
    chat_transport=None,
    clock=utc_now,
):
    plan = model_plan(spec)
    cases = load_cases()
    root = Path(approval_root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "approval-consumed.lock").open("x") as lock:
        lock.write(spec.run_id)
        lock.flush()
        os.fsync(lock.fileno())
    out = root / spec.run_id
    out.mkdir()
    _save(out / "plan.json", plan)
    report = dict(
        run_id=spec.run_id,
        status="preflight",
        plan=plan,
        started_at=datetime.now(timezone.utc).isoformat(),
        metadata_attempts=[],
        inference_attempts=[],
        cases=[
            dict(case_id=c.case_id, group=c.group, execution="not_attempted")
            for c in cases
        ],
    )

    def save():
        report["summary"] = model_summary(report)
        _save(out / "manifest.json", report)
        (out / "report.md").write_text(render_model_report(report), encoding="utf-8")

    save()
    try:
        _unchanged(plan)
        material = generate_synthetic_material(spec)
        report["material_id"], report["batches"] = (
            material.material_id,
            material.batches.model_dump(),
        )
        report["preflight"] = verify_fixture(material, spec, cases)
        _save(out / "material.json", material.to_dict())
        now = clock()
        if now < spec.observed_at or now >= spec.observed_at + timedelta(days=1):
            raise ValueError("명시한 합성 관측 시각/유효기간을 벗어났습니다.")
        settings = AgentSettings(**spec.model_dump(), model=MODEL, model_digest=DIGEST)
        inner = (
            metadata_transport
            if metadata_transport is not None
            else httpx2.HTTPTransport(retries=0, trust_env=False)
        )
        try:
            verified = verify_model(
                settings, clock=clock, transport=_MetadataTransport(inner, report, save)
            )
        except Exception as exc:
            report.update(status="stopped_preflight", error_type=type(exc).__name__)
            return report
        report["model_verification"] = dict(
            model=verified.model,
            digest=verified.digest,
            checked_at=verified.checked_at.isoformat(),
            ollama_version=verified.ollama_version,
        )
        report["status"] = "running"
        save()
        selector = OllamaIDSelector(MODEL, transport=chat_transport)
        for index, case in enumerate(cases):
            _unchanged(plan)
            actual = prepare_candidates(case.original).model_dump(mode="json")
            if actual != plan["preparations"][index]["preparation"]:
                raise ValueError("준비 결과가 고정 계획과 다릅니다.")
            report["cases"][index].update(execution="pending", t0=clock().isoformat())
            save()
            before = len(report["inference_attempts"])
            try:
                row, fatal = _evaluate(
                    case, material, spec, report, save, selector=selector, clock=clock
                )
            except Exception as exc:
                attempts = len(report["inference_attempts"]) - before
                row = dict(
                    case_id=case.case_id,
                    group=case.group,
                    execution="technical_error",
                    error_type=type(exc).__name__,
                    selector_attempts=attempts,
                    actual_model_requests=attempts,
                    schema_valid=False,
                    evidence_valid=False,
                    elapsed_seconds=None,
                )
                fatal = True
            report["cases"][index] = row
            save()
            if fatal:
                report["status"] = "stopped"
                break
        else:
            report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
    except BaseException:
        report["status"] = "interrupted"
        raise
    finally:
        for row in report["cases"]:
            if row["execution"] == "pending":
                count = sum(
                    a["case_id"] == row["case_id"] for a in report["inference_attempts"]
                )
                row.update(
                    selector_attempts=count,
                    actual_model_requests=count,
                    schema_valid=False if count else None,
                    evidence_valid=False if count else None,
                )
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description="승인된 W6 qwen3:8b 새1회 평가")
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    spec = SyntheticLeagueSpec(
        run_id=RUN_ID,
        observed_at=datetime.fromisoformat(args.observed_at),
        seed=123,
        members_per_family=3,
    )
    if not args.run:
        print(json.dumps(model_plan(spec), ensure_ascii=False, indent=2))
        return 0
    report = run_model_evaluation(spec)
    print(
        json.dumps(
            dict(
                run_id=RUN_ID,
                status=report["status"],
                metadata_attempts=len(report["metadata_attempts"]),
                inference_attempts=len(report["inference_attempts"]),
                e2e=report["summary"]["e2e"],
            )
        )
    )
    return (
        0
        if report["status"] == "completed" and report["summary"]["e2e"]["passed"] == 25
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
