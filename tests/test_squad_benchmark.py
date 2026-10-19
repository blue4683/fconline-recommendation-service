"""생성 문제의 진위와 D6 집계가 실패·미반환을 숨기지 않는지 확인한다."""

from collections import Counter
from copy import deepcopy
from unittest.mock import patch

import pytest

from FRS.core.squad_recommend import benchmark
from FRS.core.squad_recommend.solver import solve_squad


def test_approved_sixty_case_plan_is_balanced_and_deterministic():
    plans = benchmark.case_plans()
    assert len(plans) == 60 and len({p.case_id for p in plans}) == 60
    assert Counter(p.kind for p in plans) == {
        "feasible": 30,
        "oracle": 15,
        "empty_slot": 3,
        "price_limit": 3,
        "salary_limit": 3,
        "person_shortage": 3,
        "forced_person": 3,
    }
    assert set(p.size for p in plans) == {3, 10, 30}
    assert len({p.formation for p in plans}) == 3
    assert plans == benchmark.case_plans(123) and plans != benchmark.case_plans(124)


@pytest.mark.parametrize("index", [0, 1, 2, 20, 40, 15])
def test_case_generation_uses_synthetic_cards_and_replays_content_hash(index):
    plan = benchmark.case_plans()[index]
    first, expected, _ = benchmark.build_case(plan)
    second, again, _ = benchmark.build_case(plan)
    assert expected and again and first.snapshot_id == second.snapshot_id
    content = first.to_dict()
    assert all(
        c["observations"]["player_card"]["source"] == "synthetic"
        for c in content["candidates"]
    )
    pools = {
        slot: {c["spid"] for c in content["candidates"] if c["slot"] == slot}
        for slot in benchmark.FORMATION_SLOTS[plan.formation]
    }
    assert set(map(len, pools.values())) == {plan.size}


@pytest.mark.parametrize("index", list(range(10, 15)))
def test_negative_cases_have_no_feasible_squad_without_external_data(index):
    snap, expected, _ = benchmark.build_case(benchmark.case_plans()[index])
    assert not expected
    result = solve_squad(snap, clock=lambda: benchmark.WHEN)
    assert result.status == "infeasible" and result.validation is None


def test_oracle_matches_solver_and_all_methods_use_same_snapshot():
    row = benchmark.measure_case(benchmark.case_plans()[15])
    assert row["oracle_nodes"] > 0 and row["oracle_ovr"] is not None
    cp = row["methods"]["cp_sat"]
    assert (
        cp["constraint_valid"]
        and cp["result"]["validation"]["total_ovr"] == row["oracle_ovr"]
    )
    assert {v["result"]["snapshot_id"] for v in row["methods"].values()} == {
        row["snapshot_id"]
    }
    summary = benchmark.summarize([row])
    assert summary["checks"]["oracle"] and summary["oracle_matches"] == 1


def test_metrics_fail_for_unknown_oracle_false_status_and_constraint_violation():
    original = benchmark.measure_case(benchmark.case_plans()[15])
    row = deepcopy(original)
    row["methods"]["cp_sat"]["constraint_valid"] = None
    row["methods"]["cp_sat"]["result"]["status"] = "unknown"
    failed = benchmark.summarize([row])
    assert (
        not failed["passed"]
        and failed["valid_return_rate"] == 0
        and not failed["checks"]["oracle"]
    )
    row = deepcopy(original)
    row["methods"]["cheapest"]["constraint_valid"] = False
    row["methods"]["cheapest"]["state_correct"] = False
    failed = benchmark.summarize([row])
    assert not failed["checks"]["constraints"] and not failed["checks"]["statuses"]


def test_baseline_regression_and_missing_oracles_cannot_pass_vacuously():
    row = benchmark.measure_case(benchmark.case_plans()[15])
    for method in ("cheapest", "performance"):
        row["methods"][method]["constraint_valid"] = True
        row["methods"][method]["result"] = deepcopy(row["methods"]["cp_sat"]["result"])
        row["methods"][method]["result"]["validation"]["total_ovr"] = 10**6
    summary = benchmark.summarize([row])
    assert (
        not summary["checks"]["no_baseline_regression"]
        and summary["comparisons"]["worse"] > 0
    )
    row["kind"] = "feasible"
    assert not benchmark.summarize([row])["checks"]["oracle"]


def test_nearest_rank_p95_and_empty_samples_are_explicit():
    assert benchmark._latencies(list(range(1, 21))) == {
        "count": 20,
        "p50": 10.5,
        "p95": 19,
    }
    assert benchmark._latencies([]) == {"count": 0, "p50": None, "p95": None}


def test_plan_does_not_measure_or_write_files(capsys):
    with patch.object(
        benchmark, "run_benchmark", side_effect=AssertionError("계획만 출력")
    ):
        assert benchmark.main([]) == 0
    assert '"cases": 60' in capsys.readouterr().out


def test_new_run_id_and_failed_execution_are_preserved(tmp_path):
    with patch.object(
        benchmark, "case_plans", return_value=(benchmark.case_plans()[15],)
    ):
        report = benchmark.run_benchmark("synthetic-test", root=tmp_path)
    assert report["status"] == "completed" and report["summary"]["checks"]["oracle"]
    with pytest.raises(FileExistsError):
        benchmark.run_benchmark("synthetic-test", root=tmp_path)
    with patch.object(benchmark, "measure_case", side_effect=ValueError("가상 실패")):
        with pytest.raises(ValueError):
            benchmark.run_benchmark("synthetic-failure", root=tmp_path)
    import json

    assert (
        json.loads((tmp_path / "synthetic-failure/report.json").read_text())["status"]
        == "stopped"
    )


def test_generation_and_measurement_do_not_use_network():
    with patch("socket.socket", side_effect=AssertionError("네트워크 금지")):
        row = benchmark.measure_case(benchmark.case_plans()[15])
    assert row["methods"]["cp_sat"]["constraint_valid"]
