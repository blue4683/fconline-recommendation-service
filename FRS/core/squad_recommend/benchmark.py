"""가상 자료만으로 승인한 60문제를 만들고 스쿼드 엔진의 D6 지표를 측정한다."""

import argparse
import hashlib
import json
import math
import platform
import random
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

import ortools

from FRS.core.candidate_search import BatchSelection
from FRS.core.squad_recommend.placement import placement_spec
from FRS.core.squad_recommend.snapshot import SnapshotQuery, build_snapshot
from FRS.core.squad_recommend.solver import (
    SolverConfig,
    engine_spec,
    greedy_squad,
    solve_squad,
)
from FRS.core.squad_recommend.validator import SquadPick, SquadSelection, validate_squad
from FRS.schema.models import (
    CardPrice,
    CardSalary,
    PlayerCard,
    ReinforceStats,
    TeamColor,
)
from FRS.schema.scope import FORMATION_SLOTS, ROOT

WHEN = datetime(2026, 1, 1, tzinfo=timezone.utc)
CLUB = "가상클럽_벤치마크"


@dataclass(frozen=True)
class CasePlan:
    case_id: str
    formation: str
    kind: str
    size: int
    seed: int
    margin: int
    price_scale: int


def case_plans(seed=123):
    plans = []
    for formation in FORMATION_SLOTS:
        entries = [
            ("feasible", (3, 10, 30)[i % 3], (0, 5, 20)[i % 3]) for i in range(10)
        ]
        entries += [
            (kind, size, 0)
            for kind, size in zip(
                (
                    "empty_slot",
                    "price_limit",
                    "salary_limit",
                    "person_shortage",
                    "forced_person",
                ),
                (3, 10, 30, 10, 3),
                strict=True,
            )
        ]
        entries += [("oracle", 3, (0, 5, 20)[i % 3]) for i in range(5)]
        for kind, size, margin in entries:
            index = len(plans)
            plans.append(
                CasePlan(
                    f"case-{index + 1:02d}",
                    formation,
                    kind,
                    size,
                    seed + index * 1009,
                    margin,
                    10**20 if index % 4 == 0 else 100,
                )
            )
    return tuple(plans)


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def build_case(plan):
    generation_started = perf_counter()
    rng = random.Random(plan.seed)
    slots = FORMATION_SLOTS[plan.formation]
    families = placement_spec()["families"]
    family_slots = {}
    for slot in slots:
        family_slots.setdefault(families.get(slot, slot), []).append(slot)
    batches = BatchSelection(
        **{
            name: f"synthetic-{plan.case_id}-{name}"
            for name in BatchSelection.model_fields
        }
    )
    data = dict(cards=[], salaries=[], prices=[], reinforcements=[], team_colors=[])
    witness, costs, wages = [], {}, {}
    for family, assigned_slots in family_slots.items():
        for offset in range(plan.size):
            index = len(data["cards"])
            person = index + 1
            if plan.kind == "person_shortage":
                person = index % 10 + 1
            elif plan.kind == "forced_person" and family in ("GK", "RB"):
                person = 1
            season = 200 + index
            spid = season * 1_000_000 + person
            level = 5 + index % 7
            base_ovr = rng.randint(70, 140)
            cost, wage = (
                rng.randint(5, 80) * plan.price_scale + rng.randint(0, 97),
                rng.randint(5, 40),
            )

            def observation(resource, version):
                return dict(
                    spid=spid,
                    source="synthetic",
                    batch_id=getattr(batches, resource),
                    version=version,
                    observed_at=WHEN,
                    valid_until=WHEN + timedelta(days=1),
                    delete_after=None,
                    status="ok",
                )

            base = PlayerCard(
                **observation("player_card", "base-v1"),
                season_id=season,
                person_id=person,
                name=f"가상선수_{index + 1:04d}",
                base_reinforce_level=5,
                positions=(family,),
                stats={"speed": base_ovr},
                position_ovr={family: base_ovr},
            )
            data["cards"].append(base)
            version = "base-v1" if level == 5 else f"ability-{level}"
            data["salaries"].append(
                CardSalary(
                    **observation("card_salary", version),
                    reinforce_level=level,
                    salary=wage,
                )
            )
            data["prices"].append(
                CardPrice(
                    **observation("card_price", "reference-v1"),
                    reinforce_level=level,
                    price_type="reference",
                    price=cost,
                )
            )
            data["team_colors"].append(
                TeamColor(
                    **observation("team_color", "club-v1"), type="club", name=CLUB
                )
            )
            if level != 5:
                data["reinforcements"].append(
                    ReinforceStats(
                        **observation("reinforce_stats", version),
                        reinforce_level=level,
                        baseline_reinforce_level=5,
                        baseline_card_version="base-v1",
                        stats_delta={"speed": level - 5},
                        position_ovr={family: base_ovr + level - 5},
                    )
                )
            costs[spid], wages[spid] = cost, wage
            if offset < len(assigned_slots):
                witness.append(
                    SquadPick(
                        spid=spid, slot=assigned_slots[offset], reinforce_level=level
                    )
                )
    price_cap = sum(costs[p.spid] for p in witness) * (100 + plan.margin) // 100
    salary_cap = sum(wages[p.spid] for p in witness) * (100 + plan.margin) // 100
    if plan.kind == "empty_slot":
        data["cards"] = [c for c in data["cards"] if "GK" not in c.positions]
    elif plan.kind in ("price_limit", "salary_limit"):
        values = costs if plan.kind == "price_limit" else wages
        lower = sum(
            min(
                values[c.spid]
                for c in data["cards"]
                if families.get(slot, slot) in c.positions
            )
            for slot in slots
        )
        if plan.kind == "price_limit":
            price_cap = lower - 1
        else:
            salary_cap = lower - 1
    query = SnapshotQuery(
        formation=plan.formation,
        team_color=CLUB,
        salary_cap=salary_cap,
        price_cap=price_cap,
        allowed_reinforcements=tuple(range(5, 12)),
        batches=batches,
        t0=WHEN,
    )
    generation_seconds = perf_counter() - generation_started
    started = perf_counter()
    snap = build_snapshot(query, **data, supported_clubs=(CLUB,))
    build_seconds = perf_counter() - started
    expected = plan.kind in ("feasible", "oracle")
    verification_started = perf_counter()
    if (
        expected
        and not validate_squad(
            snap,
            SquadSelection(snapshot_id=snap.snapshot_id, picks=tuple(witness)),
            when=WHEN,
        ).valid
    ):
        raise ValueError(f"{plan.case_id}: 기준 스쿼드가 유효하지 않습니다.")
    return (
        snap,
        expected,
        dict(
            generation_seconds=generation_seconds,
            build_seconds=build_seconds,
            witness_validation_seconds=perf_counter() - verification_started,
        ),
    )


def exhaustive_optimum(snapshot):
    """부분 제약을 위반한 가지 외에는 목적값으로 가지치기하지 않는 완전 열거다."""
    content = snapshot.to_dict()
    slots = FORMATION_SLOTS[content["query"]["formation"]]
    pools = [[c for c in content["candidates"] if c["slot"] == slot] for slot in slots]
    best, visited = None, 0

    def visit(depth, people, cards, cost, salary, ovr):
        nonlocal best, visited
        visited += 1
        if depth == len(slots):
            best = ovr if best is None else max(best, ovr)
            return
        for candidate in pools[depth]:
            rows = candidate["observations"]
            person, spid = rows["player_card"]["person_id"], candidate["spid"]
            next_cost, next_salary = (
                cost + rows["card_price"]["price"],
                salary + rows["card_salary"]["salary"],
            )
            if (
                person in people
                or spid in cards
                or next_cost > content["query"]["price_cap"]
                or next_salary > content["query"]["salary_cap"]
            ):
                continue
            visit(
                depth + 1,
                people | {person},
                cards | {spid},
                next_cost,
                next_salary,
                ovr + candidate["raw_ovr"],
            )

    visit(0, set(), set(), 0, 0, 0)
    return best, visited


def _verify(snapshot, result):
    if result.status not in ("optimal", "feasible"):
        return None
    if result.validation is None or not result.validation.valid:
        return False
    picks = tuple(
        SquadPick(**p.model_dump(include=set(SquadPick.model_fields)))
        for p in result.validation.players
    )
    checked = validate_squad(
        snapshot,
        SquadSelection(snapshot_id=snapshot.snapshot_id, picks=picks),
        when=WHEN,
    )
    return checked.valid and checked == result.validation


def measure_case(plan):
    snapshot, expected, timings = build_case(plan)
    build_seconds = timings["build_seconds"]
    oracle, nodes, oracle_seconds = None, None, None
    if plan.kind == "oracle":
        started = perf_counter()
        oracle, nodes = exhaustive_optimum(snapshot)
        oracle_seconds = perf_counter() - started
        if oracle is None:
            raise ValueError("전수 대조 문제의 기준 해가 없습니다.")
    methods = {}
    for method in ("cp_sat", "cheapest", "performance"):
        started = perf_counter()
        result = (
            solve_squad(snapshot, clock=lambda: WHEN)
            if method == "cp_sat"
            else greedy_squad(snapshot, method=method, clock=lambda: WHEN)
        )
        elapsed = perf_counter() - started
        valid = _verify(snapshot, result)
        if result.status in ("optimal", "feasible"):
            state_ok = expected and valid is True
            if method == "cp_sat" and result.status == "optimal" and oracle is not None:
                state_ok = state_ok and result.validation.total_ovr == oracle
        elif result.status == "infeasible":
            state_ok = not expected and method == "cp_sat"
        elif result.status == "unknown":
            state_ok = method == "cp_sat"
        elif result.status == "greedy_failed":
            state_ok = method != "cp_sat"
        else:
            state_ok = False
        methods[method] = dict(
            result=result.model_dump(mode="json"),
            elapsed_seconds=elapsed,
            request_seconds=build_seconds + elapsed,
            constraint_valid=valid,
            state_correct=state_ok,
        )
    content = snapshot.to_dict()
    return dict(
        case_id=plan.case_id,
        formation=plan.formation,
        kind=plan.kind,
        target_cards_per_slot=plan.size,
        generation_seed=plan.seed,
        price_scale=plan.price_scale,
        expected_feasible=expected,
        snapshot_id=snapshot.snapshot_id,
        query=content["query"],
        snapshot_metadata=content["metadata"],
        **timings,
        oracle_ovr=oracle,
        oracle_nodes=nodes,
        oracle_seconds=oracle_seconds,
        methods=methods,
    )


def _latencies(values):
    ordered = sorted(values)
    return (
        dict(
            count=len(values),
            p50=statistics.median(ordered),
            p95=ordered[math.ceil(0.95 * len(ordered)) - 1],
        )
        if ordered
        else dict(count=0, p50=None, p95=None)
    )


def summarize(rows):
    feasible = [r for r in rows if r["expected_feasible"]]
    successes = [
        r for r in feasible if r["methods"]["cp_sat"]["constraint_valid"] is True
    ]
    oracles = [r for r in rows if r["kind"] == "oracle"]
    oracle_matches = sum(
        r["methods"]["cp_sat"]["constraint_valid"] is True
        and r["methods"]["cp_sat"]["result"]["validation"]["total_ovr"]
        == r["oracle_ovr"]
        for r in oracles
    )
    returned = [
        v["constraint_valid"]
        for r in rows
        for v in r["methods"].values()
        if v["constraint_valid"] is not None
    ]
    states = [v["state_correct"] for r in rows for v in r["methods"].values()]
    comparisons = Counter()
    for row in rows:
        cp = row["methods"]["cp_sat"]
        if cp["result"]["status"] != "optimal" or cp["constraint_valid"] is not True:
            continue
        for method in ("cheapest", "performance"):
            baseline = row["methods"][method]
            if baseline["constraint_valid"] is True:
                delta = (
                    cp["result"]["validation"]["total_ovr"]
                    - baseline["result"]["validation"]["total_ovr"]
                )
                comparisons[
                    "improved" if delta > 0 else "tied" if delta == 0 else "worse"
                ] += 1
    rate = len(successes) / len(feasible) if feasible else 0
    counts = Counter(r["kind"] for r in rows)
    regular = [r for r in feasible if r["kind"] == "feasible"]
    checks = dict(
        complete_case_set=len(rows) == 60
        and counts["feasible"] == 30
        and counts["oracle"] == 15
        and sum(not r["expected_feasible"] for r in rows) == 15
        and len({r["case_id"] for r in rows}) == 60,
        valid_return_rate=rate >= 0.95,
        constraints=bool(returned) and all(returned),
        oracle=bool(oracles) and oracle_matches == len(oracles),
        statuses=bool(states) and all(states),
        no_baseline_regression=bool(comparisons) and comparisons["worse"] == 0,
    )
    methods = {}
    for method in ("cp_sat", "cheapest", "performance"):
        results = [r["methods"][method] for r in rows]
        positive_results = [r["methods"][method] for r in feasible]
        methods[method] = dict(
            status_counts=dict(Counter(v["result"]["status"] for v in results)),
            feasible_returned=sum(
                v["constraint_valid"] is True for v in positive_results
            ),
            feasible_total=len(feasible),
            valid_return_rate=sum(
                v["constraint_valid"] is True for v in positive_results
            )
            / len(feasible)
            if feasible
            else 0,
            regular_latency_by_size={
                str(size): _latencies(
                    [
                        r["methods"][method]["elapsed_seconds"]
                        for r in regular
                        if r["target_cards_per_slot"] == size
                    ]
                )
                for size in (3, 10, 30)
            },
            regular_request_by_size={
                str(size): _latencies(
                    [
                        r["methods"][method]["request_seconds"]
                        for r in regular
                        if r["target_cards_per_slot"] == size
                    ]
                )
                for size in (3, 10, 30)
            },
            latency_seconds=_latencies([v["elapsed_seconds"] for v in results]),
            request_seconds=_latencies([v["request_seconds"] for v in results]),
            absolute_gaps=[
                v["result"]["absolute_gap"]
                for v in results
                if v["result"]["status"] == "feasible"
                and v["result"]["absolute_gap"] is not None
            ],
        )
    return dict(
        case_count=len(rows),
        feasible_count=len(feasible),
        feasible_returned=len(successes),
        valid_return_rate=rate,
        constraint_valid=sum(returned),
        constraint_checked=len(returned),
        oracle_matches=oracle_matches,
        oracle_total=len(oracles),
        state_correct=sum(states),
        state_checked=len(states),
        comparisons=dict(comparisons),
        comparison_rates={
            key: comparisons[key] / sum(comparisons.values()) if comparisons else None
            for key in ("improved", "tied", "worse")
        },
        regular_feasible_count=len(regular),
        regular_feasible_returned=sum(
            r["methods"]["cp_sat"]["constraint_valid"] is True for r in regular
        ),
        methods=methods,
        snapshot_build_seconds=_latencies([r["build_seconds"] for r in rows]),
        checks=checks,
        passed=all(checks.values()),
        latency_target=None,
    )


def _save(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    )
    temporary.replace(path)


def run_benchmark(run_id, *, root=ROOT / "artifacts/benchmarks", seed=123):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", run_id):
        raise ValueError("실행 ID 형식이 올바르지 않습니다.")
    folder = Path(root) / run_id
    folder.mkdir(parents=True, exist_ok=False)
    sources = {
        name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in (
            "benchmark.py",
            "solver.py",
            "validator.py",
            "snapshot.py",
            "placement.py",
            "performance.py",
        )
    }
    report = dict(
        run_id=run_id,
        status="running",
        source_mode="synthetic",
        seed=seed,
        python=platform.python_version(),
        platform=platform.platform(),
        ortools=ortools.__version__,
        solver_config=SolverConfig().model_dump(),
        engine=engine_spec(),
        implementation_sha256=sources,
        cases=[],
    )
    target = folder / "report.json"
    _save(target, report)
    try:
        for plan in case_plans(seed):
            row = measure_case(plan)
            report["cases"].append(row)
            _save(target, report)
            print(
                json.dumps(
                    dict(
                        case_id=plan.case_id,
                        kind=plan.kind,
                        status=row["methods"]["cp_sat"]["result"]["status"],
                    )
                ),
                flush=True,
            )
        report.update(status="completed", summary=summarize(report["cases"]))
    except Exception:
        report["status"] = "stopped"
        _save(target, report)
        raise
    report["case_set_sha256"] = _hash(
        [(r["case_id"], r["snapshot_id"]) for r in report["cases"]]
    )
    _save(target, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="외부 자료 없는 합성 스쿼드 벤치마크")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    if not args.run:
        print(
            json.dumps(
                dict(
                    mode="plan",
                    cases=60,
                    seed=123,
                    config=SolverConfig().model_dump(),
                    requires_new_run_id=True,
                )
            )
        )
        return 0
    if not args.run_id:
        parser.error("--run에는 새 --run-id가 필요합니다.")
    report = run_benchmark(args.run_id)
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
