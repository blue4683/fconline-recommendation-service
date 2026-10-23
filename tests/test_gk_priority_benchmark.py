"""합성 진단의 독립 오라클·분모·재현·실패 보존을 검증한다."""

import json
from collections import Counter
from pathlib import Path

import pytest

from FRS.core.squad_recommend.multi_factor_policy import ScoringPolicy
from FRS.core.squad_recommend.objective import MultiFactorObjective
from FRS.core.squad_recommend.snapshot import RequestSnapshot, build_snapshot
from FRS.evaluation import gk_priority_benchmark as bench


@pytest.fixture
def objective():
    path = Path(__file__).parents[1] / "docs/examples/multi-factor-core-policy.json"
    policy = ScoringPolicy.model_validate_json(path.read_text())
    return MultiFactorObjective(policy=policy, expected_policy_version=policy.version)


def test_plan_covers_formations_levels_budgets_without_quality_gate():
    cases = bench.case_plan()
    assert len(cases) == len({c.case_id for c in cases}) == 24
    assert Counter(c.kind for c in cases) == dict(
        feasible=18, salary_infeasible=3, oracle=3
    )
    for formation in bench.FORMATION_SLOTS:
        grid = [c for c in cases if c.formation == formation and c.kind == "feasible"]
        assert {(c.levels, c.budget) for c in grid} == {
            (levels, budget)
            for levels in ((5,), tuple(range(5, 12)), tuple(range(8, 12)))
            for budget in ("tight", "loose")
        }
    assert bench.plan_spec()["criteria"] is bench.plan_spec()["passed"] is None


def test_generation_is_reproducible_and_has_a_salary_certificate():
    first = bench.build_case(bench.case_plan()[0])
    assert first == bench.build_case(bench.case_plan()[0])
    _, data, witness, _, _ = first
    assert len(witness) == len({p.spid % 1_000_000 for p in witness}) == 11
    assert all(
        set(c.stats) == set(bench.STAT_NAMES) and c.left_foot and c.right_foot
        for c in data["cards"]
    )
    assert all(
        c.source == "synthetic" and c.name.startswith("가상선수") for c in data["cards"]
    )
    bad = next(c for c in bench.case_plan() if c.kind == "salary_infeasible")
    query, material, _, _, minimum = bench.build_case(bad)
    persons = {c.spid: c.person_id for c in material["cards"]}
    minima = {}
    for salary in material["salaries"]:
        person = persons[salary.spid]
        minima[person] = min(minima.get(person, salary.salary), salary.salary)
    assert len(minima) == 11 and sum(minima.values()) == minimum > query.salary_cap


@pytest.mark.parametrize("index", [7, 15, 23])
def test_actual_engines_match_independent_staged_and_price_oracles(objective, index):
    row = bench.measure_case(bench.case_plan()[index], objective)
    assert row["issues"] == [] and row["oracle"]["feasible_count"] > 1
    for method in row["methods"].values():
        assert method["result"]["status"] == "optimal"
        assert method["result"]["price_optimization_status"] == "optimal"


def test_manual_fraction_result_and_cached_score_tampering(objective):
    query, data, *_ = bench.build_case(bench.case_plan()[7])
    snap = build_snapshot(
        query, **data, supported_clubs=(bench.CLUB,), objective=objective
    )
    payload = snap.to_dict()
    row = next(
        r for r in payload["candidates"] if objective.profile_for(r["slot"]) == "st"
    )
    row["observations"]["player_card"].update(
        stats={name: 140 for name in bench.STAT_NAMES}, left_foot=3, right_foot=5
    )
    assert bench.independent_units(row, objective.policy) == 6378
    row["score_units"] = 6379
    payload.pop("snapshot_id")
    with pytest.raises(ValueError, match="independent_score_mismatch"):
        bench.exhaustive_oracle(
            RequestSnapshot(json.dumps(payload).encode()), objective.policy
        )


def test_oracle_mismatch_cannot_be_reported_as_clean(objective, monkeypatch):
    actual = bench.solve_squad

    def wrong_stage(*args, **kwargs):
        result = actual(*args, **kwargs)
        if result.objective != "ovr":
            stages = list(result.objective_stages)
            stages[1] = stages[1].model_copy(update={"value": stages[1].value + 1})
            result = result.model_copy(update={"objective_stages": tuple(stages)})
        return result

    monkeypatch.setattr(bench, "solve_squad", wrong_stage)
    row = bench.measure_case(bench.case_plan()[7], objective)
    assert "gk:oracle_stages" in row["issues"]
    assert bench.summarize([row])["issues"]


def test_summary_separates_negative_latency_and_unknown_proofs():
    def row(kind, status, latency):
        return dict(
            case=dict(case_id=kind, kind=kind),
            issues=[],
            oracle={} if kind == "oracle" else None,
            methods={
                mode: dict(
                    result=dict(
                        status=status,
                        price_optimization_status="optimal"
                        if status == "optimal"
                        else None,
                    ),
                    request_seconds=latency,
                )
                for mode in ("ovr", "gk_priority_multi_factor")
            },
        )

    summary = bench.summarize(
        [
            row("feasible", "optimal", 4),
            row("salary_infeasible", "infeasible", 0.01),
            row("oracle", "unknown", 0.1),
        ]
    )
    for method in summary["methods"].values():
        assert method["price_statuses"] == {"optimal": 1, "not_applicable": 2}
        assert method["request_latency_by_kind"]["feasible"]["p95"] == 4
        assert method["request_latency_by_kind"]["salary_infeasible"]["p95"] == 0.01
        assert method["proved_oracle_cases"] == 0 and method["statuses"]["unknown"] == 1
    assert summary["criteria"] is summary["passed"] is None


def test_runner_hashes_and_existing_run_preservation(objective, monkeypatch, tmp_path):
    case = bench.case_plan()[7]
    monkeypatch.setattr(bench, "case_plan", lambda: (case,))
    summary = bench.run_benchmark(tmp_path, "synthetic-test", objective)
    manifest = json.loads((tmp_path / "synthetic-test/manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["network"] == "blocked"
    assert manifest["summary_hash"] == bench.content_hash(summary)
    assert manifest["plan_hash"] == bench.content_hash(manifest["plan"])
    assert manifest["source_hashes"] and not summary["issues"]
    with pytest.raises(FileExistsError):
        bench.run_benchmark(tmp_path, "synthetic-test", objective)


def test_failed_runner_cannot_reuse_run_id(objective, monkeypatch, tmp_path):
    def fail(*args):
        raise RuntimeError("synthetic_failure")

    monkeypatch.setattr(bench, "measure_case", fail)
    with pytest.raises(RuntimeError, match="synthetic_failure"):
        bench.run_benchmark(tmp_path, "synthetic-failure", objective)
    manifest = json.loads((tmp_path / "synthetic-failure/manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["completed_cases"] == 0
    with pytest.raises(FileExistsError):
        bench.run_benchmark(tmp_path, "synthetic-failure", objective)


def test_cli_defaults_to_plan_and_requires_explicit_inputs(capsys, monkeypatch):
    def forbidden(*args):
        raise AssertionError("plan must not run")

    monkeypatch.setattr(bench, "run_benchmark", forbidden)
    assert bench.main([]) == 0
    assert len(json.loads(capsys.readouterr().out)["cases"]) == 24
    with pytest.raises(SystemExit):
        bench.main(["--run"])
