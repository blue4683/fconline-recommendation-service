"""고정 25문장을 정답 선택 대역으로 실행·채점한다. 모델/네트워크 경로는 없다."""

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from FRS.agent.candidate_graph import CandidateSquadAgent, candidate_agent_spec
from FRS.agent.candidate_selection import CandidateSelection, selection_spec
from FRS.agent.candidates import FIELDS, candidate_spec, canonical
from FRS.agent.extraction import ExtractionError
from FRS.agent.json_contract import strict_json
from FRS.agent.material import SyntheticLeagueSpec, generate_synthetic_material
from FRS.api.agent_projection import project_result, projection_spec
from FRS.core.request_normalization import SupportedConditions, conditions_spec
from FRS.core.squad_recommend.snapshot import SnapshotQuery, build_snapshot
from FRS.core.squad_recommend.solver import SolverConfig, engine_spec
from FRS.core.squad_recommend.validator import (
    SquadPick,
    SquadSelection,
    prepare_snapshot,
    validate_squad,
)
from FRS.evaluation.corpus import (
    CORPUS_PATH,
    DOCUMENT_SHA256,
    ROOT,
    EvaluationCase,
    load_cases,
)
from FRS.schema.scope import FORMATION_SLOTS

ARTIFACT_ROOT = ROOT / "artifacts/agent-evaluations"


def _save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical(value))
    temporary.replace(path)


def _conditions(value):
    if value is None:
        return None
    data = value.model_dump(mode="json")
    return {
        **data,
        "price_cap": str(data["price_cap"]),
        "salary_cap": str(data["salary_cap"]),
    }


def _spans(case):
    evidence = case.expected_evidence()
    return sorted(
        (field, span.start, span.end, span.text)
        for field in FIELDS
        for span in getattr(evidence, field)
    )


def registry_matches(case, preparation):
    registry = preparation.registry
    return registry is not None and sorted(
        (value.field, value.span.start, value.span.end, value.span.text)
        for value in registry.candidates
    ) == _spans(case)


@dataclass
class OracleSelector:
    """평가 전용 사전 근거 선택 대역이며 운영 fallback으로 사용하지 않는다."""

    case: EvaluationCase
    attempts: int = 0
    schema_valid: bool | None = None
    elapsed_seconds: float = 0.0

    def select(self, registry):
        self.attempts += 1
        started = time.perf_counter()
        try:
            if registry.original != self.case.original:
                raise ExtractionError("고정한 평가 원문과 일치하지 않습니다.")
            lookup = {
                (v.field, v.span.start, v.span.end, v.span.text): v.candidate_id
                for v in registry.candidates
            }
            evidence = self.case.expected_evidence()
            raw = canonical(
                dict(
                    candidate_set_id=registry.candidate_set_id,
                    **{
                        field: [
                            lookup[(field, span.start, span.end, span.text)]
                            for span in getattr(evidence, field)
                        ]
                        for field in FIELDS
                    },
                    unsupported=[],
                )
            ).decode()
            self.schema_valid = True
            return raw
        finally:
            self.elapsed_seconds += time.perf_counter() - started


@dataclass
class _MeasuredMaterial:
    material: object
    loads: int = 0
    elapsed_seconds: float = 0.0

    def load(self, batches):
        self.loads += 1
        started = time.perf_counter()
        try:
            return self.material.load(batches)
        finally:
            self.elapsed_seconds += time.perf_counter() - started


def verify_fixture(material, spec, cases):
    """해 존재는 고정 유효11명, 해 없음은 양수 하한으로 사전 검증한다."""
    if spec.seed != 123 or spec.members_per_family != 3:
        raise ValueError("평가용 seed123/계열당3명이 필요합니다.")
    raw = material.to_dict()
    if raw["inputs"] != spec.model_dump(mode="json"):
        raise ValueError("고정한 합성 입력과 일치하지 않습니다.")
    records = material.load(material.batches)
    prices = records["prices"]
    salaries = records["salaries"]
    if not all(
        1_000_000_000 * 2 ** (p.reinforce_level - 5)
        <= p.price
        <= 10_000_000_000 * 2 ** (p.reinforce_level - 5)
        for p in prices
    ) or not all(10 <= row.salary <= 25 for row in salaries):
        raise ValueError("사전 증명의 가격/급여 범위와 일치하지 않습니다.")
    witnesses, proofs = [], []
    for case in cases:
        if case.stage != "engine":
            continue
        query = SnapshotQuery(
            **case.conditions.model_dump(),
            batches=material.batches,
            t0=spec.observed_at,
        )
        snapshot = build_snapshot(query, **records)
        if case.group == "I":
            budget = all(p.price > case.conditions.price_cap for p in prices)
            salary = all(s.salary > case.conditions.salary_cap for s in salaries)
            if not (budget or salary):
                raise ValueError("해 없음의 사전 하한을 증명할 수 없습니다.")
            proofs.append(dict(case_id=case.case_id, budget=budget, salary=salary))
            continue
        prepared = prepare_snapshot(snapshot, when=spec.observed_at)
        people, picks = set(), []
        level = min(query.allowed_reinforcements)
        for slot in FORMATION_SLOTS[query.formation]:
            candidates = sorted(
                (
                    c
                    for c in prepared.candidates
                    if c.slot == slot
                    and c.reinforce_level == level
                    and c.observations.player_card.person_id not in people
                ),
                key=lambda c: c.spid,
            )
            if not candidates:
                raise ValueError("정상 사례의 사전11명을 구성할 수 없습니다.")
            choice = candidates[0]
            people.add(choice.observations.player_card.person_id)
            picks.append(SquadPick(slot=slot, spid=choice.spid, reinforce_level=level))
        selection = SquadSelection(snapshot_id=snapshot.snapshot_id, picks=tuple(picks))
        report = validate_squad(snapshot, selection, when=spec.observed_at)
        if not report.valid:
            raise ValueError("사전11명이 독립 검증에 실패했습니다.")
        witnesses.append(
            dict(case_id=case.case_id, validation=report.model_dump(mode="json"))
        )
    return dict(normal_witnesses=witnesses, infeasible_proofs=proofs)


def _issues_match(case, issues, *, preparation):
    actual = [(i.field, i.code) for i in issues]
    expected = [(i.field, i.code) for i in case.issues]
    if actual != expected:
        return False
    if preparation:
        return all(
            i.span is not None and i.span.text == expected.span_text
            for i, expected in zip(issues, case.issues, strict=True)
        )
    return True


def _validate_table(result, body, *, when):
    """출력의 행/합계를 원래 스냅샷의 독립 검증값과 대조한다."""
    if not body.get("squad"):
        return None
    try:
        build = result.agent_result.build
        snapshot = build.snapshot
        rows = body["squad"]["rows"]
        selection = SquadSelection(
            snapshot_id=body["snapshot"]["id"],
            picks=tuple(
                SquadPick(
                    spid=int(row["spid"]),
                    slot=row["slot"],
                    reinforce_level=row["reinforce_level"],
                )
                for row in rows
            ),
        )
        report = validate_squad(snapshot, selection, when=when)
        if not report.valid:
            return False
        expected_rows = [
            {
                **p.model_dump(mode="json"),
                "spid": str(p.spid),
                "person_id": str(p.person_id),
                "price": str(p.price),
                "salary": str(p.salary),
            }
            for p in report.players
        ]
        return (
            rows == expected_rows
            and body["squad"]
            == dict(
                rows=expected_rows,
                total_price=str(report.total_price),
                total_salary=str(report.total_salary),
                total_ovr=report.total_ovr,
                performance=report.performance,
            )
            and body["conditions"] == _conditions(build.normalization.conditions)
            and body["status"] in ("optimal", "feasible")
        )
    except (AttributeError, ValueError, TypeError, KeyError):
        return False


def assess_case(case, result, body, *, selector_attempts, material_loads, when):
    """결과에서 정답을 생성하지 않는다. 기술 실패·만료를 기대 결과로 바꾸지 않는다."""
    preparation = result.preparation
    registry_valid = registry_matches(case, preparation)
    normalized = result.agent_result.normalization if result.agent_result else None
    if case.stage == "prepare":
        semantic = (
            result.outcome == "input_issues"
            and registry_valid
            and _issues_match(case, preparation.issues, preparation=True)
        )
    else:
        semantic = (
            result.outcome == "agent_result"
            and not preparation.issues
            and registry_valid
            and normalized is not None
            and normalized.evidence == case.expected_evidence()
            and normalized.conditions == case.conditions
            and _issues_match(case, normalized.issues, preparation=False)
        )
    expected_selector = 0 if case.stage == "prepare" else 1
    expected_loads = 1 if case.stage == "engine" else 0
    calls_valid = (
        selector_attempts == expected_selector and material_loads == expected_loads
    )
    constraints = _validate_table(result, body, when=when)
    expected_squad = case.group == "N"
    shape_valid = (
        body.get("conditions") == _conditions(case.conditions)
        and (constraints is True if expected_squad else body.get("squad") is None)
        and body.get("status") in case.outcomes
        and body.get("data_mode") == "synthetic"
        and body.get("narration_source") == "template"
    )
    if case.stage == "engine" and result.agent_result and result.agent_result.build:
        build = result.agent_result.build
        snapshot = build.snapshot.to_dict()
        shape_valid = (
            shape_valid
            and build.engine is not None
            and body.get("status") == build.engine.status
            and body.get("issues") == []
            and body.get("supported") == SupportedConditions().model_dump(mode="json")
            and body.get("snapshot")
            == dict(
                id=build.snapshot.snapshot_id,
                t0=snapshot["query"]["t0"],
                valid_until=snapshot["metadata"]["valid_until"],
            )
        )
        if expected_squad:
            shape_valid = (
                shape_valid
                and build.engine.validation is not None
                and body["squad"] is not None
                and body["squad"]["total_ovr"] == build.engine.validation.total_ovr
            )
    if case.stage in ("prepare", "normalize"):
        expected_issues = [
            (i.field, ("prepare." if case.stage == "prepare" else "") + i.code)
            for i in case.issues
        ]
        shape_valid = (
            shape_valid
            and [(i["field"], i["code"]) for i in body.get("issues", [])]
            == expected_issues
            and body.get("supported") == SupportedConditions().model_dump(mode="json")
            and body.get("snapshot") is None
        )
    return dict(
        semantics_match=bool(semantic),
        e2e_match=bool(semantic and calls_valid and shape_valid),
        registry_matches=registry_valid,
        call_boundaries_match=calls_valid,
        returned_squad=body.get("squad") is not None,
        constraints_valid=constraints,
    )


def evaluate_case(case, material, spec, *, clock=None):
    # 이 실행 경로는 정답 대역만 생성한다. 실모델 주입/자동 전환 옵션이 아니다.
    selector = OracleSelector(case)
    clock = clock if clock is not None else lambda: spec.observed_at
    measured = _MeasuredMaterial(material)
    # 계측용 selector를 감싼다. 예외도 시도로 남기며 재시도하지 않는다.
    attempts, schema, raw_selection = 0, None, None

    class TrackedSelector:
        def select(self, registry):
            nonlocal attempts, schema, raw_selection
            attempts += 1
            try:
                raw = selector.select(registry)
                raw_selection = raw if isinstance(raw, str) else None
            except Exception:
                schema = False
                raise
            try:
                CandidateSelection.model_validate(strict_json(raw))
                schema = True
            except (ValueError, TypeError):
                schema = False
            return raw

    started = time.perf_counter()
    agent = CandidateSquadAgent(
        TrackedSelector(), material=measured, batches=material.batches, clock=clock
    )
    result, error_type, projection_seconds = None, None, None
    try:
        result = agent.run(case.original, t0=spec.observed_at)
        agent_seconds = time.perf_counter() - started
        projection_started = time.perf_counter()
        projected = project_result(
            case.original, result, clock=clock, material_id=material.material_id
        )
        body = projected.to_dict(clock=clock)
        projection_seconds = time.perf_counter() - projection_started
        technical_error = projected.kind == "error"
    except Exception as exc:
        error_type = type(exc).__name__
        agent_seconds = time.perf_counter() - started
        body = dict(code="internal_error")
        technical_error = True
    elapsed = time.perf_counter() - started
    assessment = (
        assess_case(
            case,
            result,
            body,
            selector_attempts=attempts,
            material_loads=measured.loads,
            when=clock(),
        )
        if result is not None and not error_type
        else dict(
            semantics_match=False,
            e2e_match=False,
            registry_matches=False,
            call_boundaries_match=False,
            returned_squad=False,
            constraints_valid=None,
        )
    )
    return dict(
        case_id=case.case_id,
        group=case.group,
        execution="technical_error" if technical_error else "completed",
        error_type=error_type,
        expected_statuses=case.outcomes,
        actual_status=body.get("status", "technical_error"),
        outcome=result.outcome if result else "technical_error",
        stages=list(result.stages) if result else [],
        selector_attempts=attempts,
        selector_kind="oracle_fixture",
        actual_model_requests=0,
        schema_valid=schema,
        evidence_valid=None
        if not attempts
        else bool(result and result.outcome == "agent_result"),
        material_loads=measured.loads,
        raw_selection=raw_selection,
        expected_evidence=case.expected_evidence().model_dump(mode="json"),
        observed_normalization=None
        if result is None
        or result.agent_result is None
        or result.agent_result.normalization is None
        else result.agent_result.normalization.model_dump(mode="json"),
        preparation=result.preparation.model_dump(mode="json") if result else None,
        presentation=body,
        assessment=assessment,
        elapsed_seconds=elapsed,
        timing=dict(
            agent_seconds=agent_seconds,
            projection_seconds=projection_seconds,
            material_load_seconds=measured.elapsed_seconds,
            prepare_seconds=None,
            normalize_seconds=None,
            solver_seconds=None,
        ),
        tokens=None,
        retries=0,
        fallback=0,
    )


def rate(passed, total):
    return dict(passed=passed, total=total, rate=passed / total if total else None)


def latency(values):
    values = sorted(values)
    return dict(
        n=len(values),
        **{
            name: values[math.ceil(p * len(values)) - 1] if values else None
            for name, p in (("p50_seconds", 0.5), ("p95_seconds", 0.95))
        },
    )


def summarize(rows):
    expected = {c.case_id: c.group for c in load_cases()}
    if len(rows) != 25 or {r["case_id"]: r["group"] for r in rows} != expected:
        raise ValueError("지표 분모의 고정25개를 제거하거나 바꿀 수 없습니다.")

    def passed(row, key):
        return row.get("assessment", {}).get(key) is True

    attempts = [r for r in rows if r.get("selector_attempts", 0)]
    selected = [r for r in rows if r["case_id"][0] in "NIM" or r["case_id"] == "A01"]
    normal = [r for r in rows if r["group"] == "N"]
    returned = [r for r in rows if r.get("assessment", {}).get("returned_squad")]
    terminal = [r for r in rows if r["execution"] in ("completed", "technical_error")]
    return dict(
        metric_mode="synthetic_oracle_contract_only",
        actual_model_metrics=None,
        completeness=rate(len(terminal), len(rows)),
        schema=rate(sum(r["schema_valid"] is True for r in attempts), len(attempts)),
        evidence=rate(
            sum(r["evidence_valid"] is True for r in attempts), len(attempts)
        ),
        selected_semantics=rate(
            sum(passed(r, "semantics_match") for r in selected), len(selected)
        ),
        semantics=rate(sum(passed(r, "semantics_match") for r in rows), len(rows)),
        e2e=rate(sum(passed(r, "e2e_match") for r in rows), len(rows)),
        valid_squads=rate(sum(passed(r, "e2e_match") for r in normal), len(normal)),
        constraints=rate(
            sum(passed(r, "constraints_valid") for r in returned), len(returned)
        ),
        groups={
            g: rate(
                sum(passed(r, "e2e_match") for r in rows if r["group"] == g),
                sum(r["group"] == g for r in rows),
            )
            for g in "NIUMA"
        },
        not_attempted=sum(r["execution"] == "not_attempted" for r in rows),
        pending=sum(r["execution"] == "pending" for r in rows),
        technical_errors=sum(r["execution"] == "technical_error" for r in rows),
        latency=dict(
            all_terminal=latency(
                [
                    r["elapsed_seconds"]
                    for r in terminal
                    if r.get("elapsed_seconds") is not None
                ]
            ),
            selector_requests=latency(
                [
                    r["elapsed_seconds"]
                    for r in attempts
                    if r.get("elapsed_seconds") is not None
                ]
            ),
            preparation_only=latency(
                [
                    r["elapsed_seconds"]
                    for r in terminal
                    if not r.get("selector_attempts")
                    and r.get("elapsed_seconds") is not None
                ]
            ),
            successful=latency(
                [r["elapsed_seconds"] for r in terminal if passed(r, "e2e_match")]
            ),
        ),
        retries=sum(r.get("retries", 0) for r in rows),
        fallback=sum(r.get("fallback", 0) for r in rows),
    )


def evaluation_plan(spec):
    if spec.seed != 123 or spec.members_per_family != 3:
        raise ValueError("평가용 seed123/계열당3명을 유지해야 합니다.")
    cases = load_cases()
    sources = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "FRS").rglob("*.py"))
    }
    plan = dict(
        version="w6-offline.v1",
        mode="synthetic_oracle_contract_only",
        current_readiness=False,
        actual_model_requests=0,
        external_requests=0,
        clock_mode="fixed_at_explicit_observed_at_not_wall_time",
        specification=spec.model_dump(mode="json"),
        corpus_sha256=hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest(),
        document_sha256=DOCUMENT_SHA256,
        cases=[c.model_dump(mode="json") for c in cases],
        expected_selector_calls=16,
        expected_prepare_blocks=9,
        pass_thresholds=None,
        retries=0,
        fallback=0,
        versions=dict(
            candidate=candidate_spec(),
            selection=selection_spec(),
            adapter=candidate_agent_spec(),
            normalizer=conditions_spec(),
            projection=projection_spec(),
            engine=engine_spec(),
            solver=SolverConfig().model_dump(),
        ),
        source_sha256=sources,
        git=dict(
            head=subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            tracked_dirty=bool(
                subprocess.check_output(
                    ["git", "diff", "HEAD", "--name-only"], cwd=ROOT, text=True
                ).strip()
            ),
            untracked_sources=bool(
                subprocess.check_output(
                    ["git", "ls-files", "--others", "--exclude-standard", "FRS/"],
                    cwd=ROOT,
                    text=True,
                ).strip()
            ),
        ),
        runtime=dict(
            python=platform.python_version(),
            packages={
                name: importlib.metadata.version(name)
                for name in ("pydantic", "ortools", "langgraph")
            },
        ),
    )
    return json.loads(
        canonical({**plan, "plan_sha256": hashlib.sha256(canonical(plan)).hexdigest()})
    )


def render_report(report):
    metrics = report["summary"]
    lines = [
        "# W6 합성 대역 평가 보고서",
        "",
        "실제 모델 정확도가 아닌 사전 정답 선택 대역의 계약 검증입니다.",
        f"실행 ID: `{report['run_id']}` / 상태: `{report['status']}`",
        "실제 모델·외부 요청: 0회. 실제 모델 지표: 미측정. 고정 과거 합성 시계이며 현재 서버 준비 상태가 아닙니다.",
        "",
        "| 항목 | 대역 계약 결과 |",
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
        item = metrics[key]
        value = (
            "N/A"
            if item["rate"] is None
            else f"{item['passed']}/{item['total']} ({item['rate']:.1%})"
        )
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "| 사례 | 실행 | 결과 | 의미 | E2E |", "|---|---|---|---|---|"])
    for row in report["cases"]:
        a = row.get("assessment", {})
        lines.append(
            f"| {row['case_id']} | {row['execution']} | {row.get('actual_status', '미실행')} | {a.get('semantics_match', False)} | {a.get('e2e_match', False)} |"
        )
    lines.extend(
        [
            "",
            "통과율 목표는 미정입니다. 오류·미실행은 원래 분모에 남깁니다. 토큰/모델 지연/비용은 미측정입니다.",
            "세부 지연의 nearest-rank p50/p95·표본 수와 실패 단계는 manifest.json을 참조합니다. 노드별 미계측값은 null입니다.",
            f"계획 SHA256: `{report['plan']['plan_sha256']}`",
            "",
        ]
    )
    return "\n".join(lines)


def run_offline(spec, *, artifact_root=ARTIFACT_ROOT):
    plan = evaluation_plan(spec)
    cases = load_cases()
    destination = Path(artifact_root) / spec.run_id
    destination.mkdir(parents=True, exist_ok=False)
    _save(destination / "plan.json", plan)
    report = dict(
        run_id=spec.run_id,
        status="preflight",
        plan=plan,
        started_at=datetime.now(timezone.utc).isoformat(),
        cases=[
            dict(case_id=c.case_id, group=c.group, execution="not_attempted")
            for c in cases
        ],
    )

    def save():
        report["summary"] = summarize(report["cases"])
        _save(destination / "manifest.json", report)
        (destination / "report.md").write_text(render_report(report))

    save()
    try:
        material = generate_synthetic_material(spec)
        report["material_id"] = material.material_id
        report["batches"] = material.batches.model_dump()
        report["preflight"] = verify_fixture(material, spec, cases)
        _save(destination / "material.json", material.to_dict())
        report["status"] = "running"
        save()
        for index, case in enumerate(cases):
            report["cases"][index]["execution"] = "pending"
            save()
            try:
                row = evaluate_case(case, material, spec)
            except Exception as exc:
                row = dict(
                    case_id=case.case_id,
                    group=case.group,
                    execution="technical_error",
                    error_type=type(exc).__name__,
                    elapsed_seconds=None,
                )
            report["cases"][index] = row
            save()
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
    except BaseException:
        report["status"] = "interrupted"
        save()
        raise
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description="실제 모델 없는 W6 합성 대역 평가")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    spec = SyntheticLeagueSpec(
        run_id=args.run_id,
        observed_at=datetime.fromisoformat(args.observed_at),
        seed=123,
        members_per_family=3,
    )
    if not args.run:
        print(json.dumps(evaluation_plan(spec), ensure_ascii=False, indent=2))
        return 0
    report = run_offline(spec)
    print(
        json.dumps(
            dict(
                run_id=report["run_id"],
                status=report["status"],
                mode="synthetic_oracle_contract_only",
                actual_model_requests=0,
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
