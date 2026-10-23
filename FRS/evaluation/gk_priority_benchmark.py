"""GK 우선 모드의 합성 진단. 운영 엔진·정책·기존 D6 판정을 변경하지 않는다."""

import argparse
import hashlib
import json
import math
import platform
import random
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import ortools

from FRS.core.candidate_search.search import BatchSelection
from FRS.core.squad_recommend.multi_factor_policy import ScoringPolicy, content_hash
from FRS.core.squad_recommend.objective import MultiFactorObjective, objective_spec
from FRS.core.squad_recommend.placement import placement_spec
from FRS.core.squad_recommend.snapshot import SnapshotQuery, build_snapshot
from FRS.core.squad_recommend.solver import solve_squad
from FRS.core.squad_recommend.validator import SquadPick, SquadSelection, validate_squad
from FRS.schema.abilities import STAT_NAMES
from FRS.schema.models import (
    CardPrice,
    CardSalary,
    PlayerCard,
    ReinforceStats,
    TeamColor,
)
from FRS.schema.scope import FORMATION_SLOTS

WHEN = datetime(2026, 1, 1, tzinfo=timezone.utc)
CLUB = "가상클럽_GK진단"
PRICE_UNIT = 10**20 + 37
SCHEMA = "gk-priority-synthetic-benchmark.v1"


@dataclass(frozen=True)
class Case:
    case_id: str
    formation: str
    levels: tuple[int, ...]
    budget: str
    kind: str
    seed: int


def case_plan():
    """18개 조건 조합 + 해 없음3 + 작은 전수3. 품질/지연 통과 기준은 없다."""
    cases = []
    for formation in FORMATION_SLOTS:
        for levels in ((5,), tuple(range(5, 12)), tuple(range(8, 12))):
            for budget in ("tight", "loose"):
                cases.append((formation, levels, budget, "feasible"))
        cases.append((formation, (5,), "tight", "salary_infeasible"))
        cases.append((formation, (5,), "loose", "oracle"))
    return tuple(
        Case(f"gk-{i + 1:02d}", *values, seed=123 + i) for i, values in enumerate(cases)
    )


def plan_spec():
    return dict(
        schema_version=SCHEMA,
        cases=[asdict(case) for case in case_plan()],
        generator_version=1,
        price_unit=PRICE_UNIT,
        salary_cap=310,
        data="synthetic_only_no_observed_material_or_model",
        timers="snapshot_plus_solver_including_core_validation; extra_audit_separate",
        oracle="independent_fraction_score_and_exhaustive_staged_objective",
        criteria=None,
        passed=None,
    )


def build_case(case):
    rng = random.Random(case.seed)
    families = placement_spec()["families"]
    batches = BatchSelection(
        **{
            name: f"synthetic-{case.case_id}-{name}"
            for name in BatchSelection.model_fields
        }
    )
    data = dict(cards=[], salaries=[], prices=[], reinforcements=[], team_colors=[])
    witness = []
    witness_cost = 0
    minimum_salary = 0
    for index, slot in enumerate(FORMATION_SLOTS[case.formation]):
        family = families.get(slot, slot)
        person = index + 1
        # 두 시즌의 같은 인물은 동시에 고를 수 없다. 작은 오라클은 GK/ST만 대체한다.
        variants = 2 if case.kind != "oracle" or family in ("GK", "ST") else 1
        wage = rng.randint(18, 22)
        base_ovr = rng.randint(100, 135)
        for variant in range(variants):
            season = 940 + variant
            spid = season * 1_000_000 + person

            def observation(resource):
                return dict(
                    spid=spid,
                    source="synthetic",
                    batch_id=getattr(batches, resource),
                    version=f"synthetic-{resource}-v1",
                    observed_at=WHEN,
                    valid_until=WHEN + timedelta(days=1),
                    delete_after=None,
                    status="ok",
                )

            card = PlayerCard(
                **observation("player_card"),
                season_id=season,
                person_id=person,
                name=f"가상선수_GK진단_{person:02d}_{variant}",
                base_reinforce_level=5,
                positions=(family,),
                stats={name: rng.randint(75, 150) for name in STAT_NAMES},
                position_ovr={family: base_ovr + variant * rng.randint(0, 5)},
                left_foot=rng.randint(2, 5),
                right_foot=5,
            )
            data["cards"].append(card)
            data["team_colors"].append(
                TeamColor(**observation("team_color"), type="club", name=CLUB)
            )
            for level in case.levels:
                price = PRICE_UNIT * (1 + variant * 2) * 2 ** (level - 5)
                salary = wage + variant * 9 + level - 5
                data["salaries"].append(
                    CardSalary(
                        **{
                            **observation("card_salary"),
                            "version": card.version
                            if level == 5
                            else "synthetic-reinforce_stats-v1",
                        },
                        reinforce_level=level,
                        salary=salary,
                    )
                )
                data["prices"].append(
                    CardPrice(
                        **observation("card_price"),
                        reinforce_level=level,
                        price_type="reference",
                        price=price,
                    )
                )
                if level != 5:
                    data["reinforcements"].append(
                        ReinforceStats(
                            **observation("reinforce_stats"),
                            reinforce_level=level,
                            baseline_reinforce_level=5,
                            baseline_card_version=card.version,
                            stats_delta={name: level - 5 for name in STAT_NAMES},
                            position_ovr={
                                family: card.position_ovr[family] + level - 5
                            },
                        )
                    )
                if variant == 0 and level == min(case.levels):
                    witness.append(
                        SquadPick(spid=spid, slot=slot, reinforce_level=level)
                    )
                    witness_cost += price
                    minimum_salary += salary
    cap = witness_cost * (6 if case.budget == "tight" else 40) // 5
    salary_cap = minimum_salary - 1 if case.kind == "salary_infeasible" else 310
    query = SnapshotQuery(
        formation=case.formation,
        team_color=CLUB,
        salary_cap=salary_cap,
        price_cap=cap,
        allowed_reinforcements=case.levels,
        batches=batches,
        t0=WHEN,
    )
    material_hash = content_hash(
        {
            name: [row.model_dump(mode="json") for row in rows]
            for name, rows in data.items()
        }
    )
    return query, data, tuple(witness), material_hash, minimum_salary


def _key(row):
    return row["slot"], row["spid"], row["reinforce_level"]


def independent_units(candidate, policy):
    """합성 core-stat-foot 산술 오라클. 운영 scorer/목적/solver를 호출하지 않는다."""
    role_id = next(s.profile_id for s in policy.slots if s.slot == candidate["slot"])
    role = next(p for p in policy.profiles if p.name == role_id)
    observations = candidate["observations"]
    card = observations["player_card"]
    delta = (observations.get("reinforce_stats") or {}).get("stats_delta", {})
    groups = {g.name: g for g in policy.groups}
    total = Fraction(0)
    weights = sum(w.weight for w in role.weights if w.name != "body")
    for component in role.weights:
        if component.name == "body" or not component.weight:
            continue
        if component.name == "weak_foot":
            value = Fraction(
                policy.weak_foot_scores[min(card["left_foot"], card["right_foot"]) - 1]
            )
        else:
            value = sum(
                (
                    Fraction(
                        (
                            card["stats"][stat.name]
                            + (
                                delta[stat.name]
                                if observations.get("reinforce_stats")
                                else 0
                            )
                        )
                        * stat.weight,
                        stat.upper,
                    )
                    for stat in groups[component.name].stats
                    if stat.weight
                ),
                Fraction(0),
            )
        total += value * component.weight / weights
    scaled = total * 100
    return math.floor(scaled + Fraction(1, 2))


def exhaustive_oracle(snapshot, policy):
    """작은 문제 전체 조합에서 S1→S2(G,F)→S3→S4를 순서대로 제한한다."""
    data = snapshot.to_dict()
    slots = FORMATION_SLOTS[data["query"]["formation"]]
    pools = [[r for r in data["candidates"] if r["slot"] == slot] for slot in slots]
    scores = {_key(r): independent_units(r, policy) for r in data["candidates"]}
    if any(scores[_key(r)] != r["score_units"] for r in data["candidates"]):
        raise ValueError("independent_score_mismatch")
    feasible = []
    visited = 0

    def visit(index, people, cost, salary, outfield, gk, units):
        nonlocal visited
        visited += 1
        if visited > 1_000_000:
            raise ValueError("oracle_visit_limit")
        if index == len(slots):
            feasible.append((outfield, gk, units, cost))
            return
        for row in pools[index]:
            obs = row["observations"]
            person = obs["player_card"]["person_id"]
            next_cost = cost + obs["card_price"]["price"]
            next_salary = salary + obs["card_salary"]["salary"]
            if (
                person in people
                or next_cost > data["query"]["price_cap"]
                or next_salary > data["query"]["salary_cap"]
            ):
                continue
            visit(
                index + 1,
                people | {person},
                next_cost,
                next_salary,
                outfield + (row["raw_ovr"] if row["slot"] != "GK" else 0),
                row["raw_ovr"] if row["slot"] == "GK" else gk,
                units + scores[_key(row)],
            )

    visit(0, set(), 0, 0, 0, 0, 0)
    if not feasible:
        return dict(feasible=False, visited=visited)
    f1 = max(r[0] for r in feasible)
    # 운영 상수를 읽지 않고 확정 계약의 숫자를 써서 상수의 잘못된 변경도 검출한다.
    g2, f2 = max((r[1], r[0]) for r in feasible if r[0] >= f1 - 15)
    allowed = [r for r in feasible if r[0] >= f2 - 5 and r[1] >= g2 - 2]
    units, price = max((r[2], r[3]) for r in allowed)
    return dict(
        feasible=True,
        visited=visited,
        feasible_count=len(feasible),
        stages=[f1, g2, units],
        gk_stage_outfield=f2,
        total_price=price,
        ovr_optimum=max((r[0] + r[1], r[3]) for r in feasible),
    )


def measure_case(case, objective):
    if objective.policy.mode != "core-stat-foot":
        raise ValueError("이 진단의 독립 산술은 core-stat-foot만 지원합니다.")
    query, material, witness, digest, minimum_salary = build_case(case)
    snapshots, results, timings = {}, {}, {}
    for mode in ("ovr", "gk_priority_multi_factor"):
        start = perf_counter()
        snap = build_snapshot(
            query,
            **material,
            supported_clubs=(CLUB,),
            objective=objective if mode != "ovr" else None,
        )
        snapshot_time = perf_counter() - start
        if case.kind != "salary_infeasible":
            checked = validate_squad(
                snap,
                SquadSelection(snapshot_id=snap.snapshot_id, picks=witness),
                when=WHEN,
            )
            if not checked.valid:
                raise ValueError("invalid_generated_witness")
        start = perf_counter()
        result = solve_squad(snap, clock=lambda: WHEN)
        solve_time = perf_counter() - start
        snapshots[mode], results[mode] = snap, result
        timings[mode] = dict(
            snapshot_seconds=snapshot_time,
            solve_seconds=solve_time,
            request_seconds=snapshot_time + solve_time,
        )
    plain, scored = (snapshots[m].to_dict() for m in snapshots)
    if {_key(r) for r in plain["candidates"]} != {
        _key(r) for r in scored["candidates"]
    }:
        raise ValueError("candidate_pool_mismatch")
    oracle = (
        exhaustive_oracle(snapshots["gk_priority_multi_factor"], objective.policy)
        if case.kind == "oracle"
        else None
    )
    issues = []
    methods = {}
    scores = {_key(r): r["score_units"] for r in scored["candidates"]}
    for mode, result in results.items():
        comparison = None
        start = perf_counter()
        if result.status in ("optimal", "feasible"):
            picks = tuple(
                SquadPick(slot=p.slot, spid=p.spid, reinforce_level=p.reinforce_level)
                for p in result.validation.players
            )
            checked = validate_squad(
                snapshots[mode],
                SquadSelection(snapshot_id=result.snapshot_id, picks=picks),
                when=WHEN,
            )
            if checked != result.validation or not checked.valid:
                issues.append(f"{mode}:independent_validation")
            comparison = dict(
                outfield_ovr=sum(p.raw_ovr for p in checked.players if p.slot != "GK"),
                gk_ovr=next(p.raw_ovr for p in checked.players if p.slot == "GK"),
                total_score_units=sum(
                    scores[(p.slot, p.spid, p.reinforce_level)] for p in checked.players
                ),
                total_price=checked.total_price,
            )
        if case.kind == "salary_infeasible" and result.status != "infeasible":
            issues.append(f"{mode}:infeasibility_certificate")
        elif case.kind != "salary_infeasible" and result.status == "infeasible":
            issues.append(f"{mode}:contradicts_witness")
        if result.status in ("internal_error", "data_expired"):
            issues.append(f"{mode}:{result.status}")
        if oracle and result.status == "optimal":
            if mode == "ovr":
                if (
                    comparison["outfield_ovr"] + comparison["gk_ovr"]
                    != oracle["ovr_optimum"][0]
                ):
                    issues.append("ovr:oracle_objective")
                price = oracle["ovr_optimum"][1]
            else:
                if [s.value for s in result.objective_stages] != oracle["stages"]:
                    issues.append("gk:oracle_stages")
                if comparison["total_score_units"] != oracle["stages"][2]:
                    issues.append("gk:oracle_score")
                price = oracle["total_price"]
            if (
                result.price_optimization_status == "optimal"
                and comparison["total_price"] != price
            ):
                issues.append(f"{mode}:oracle_price")
        methods[mode] = dict(
            result=result.model_dump(mode="json"),
            comparison=comparison,
            extra_audit_seconds=perf_counter() - start,
            **timings[mode],
        )
    return dict(
        case=asdict(case),
        query=query.model_dump(mode="json"),
        material_hash=digest,
        candidates=len(scored["candidates"]),
        snapshots={mode: snap.snapshot_id for mode, snap in snapshots.items()},
        certificate=dict(minimum_salary=minimum_salary, person_count=11)
        if case.kind == "salary_infeasible"
        else dict(witness_validated=True),
        oracle=oracle,
        methods=methods,
        issues=issues,
    )


def _latencies(values):
    ordered = sorted(values)
    return (
        dict(
            n=len(ordered),
            p50=ordered[math.ceil(len(ordered) * 0.5) - 1],
            p95=ordered[math.ceil(len(ordered) * 0.95) - 1],
            maximum=ordered[-1],
        )
        if ordered
        else dict(n=0, p50=None, p95=None, maximum=None)
    )


def summarize(rows):
    return dict(
        criteria=None,
        passed=None,
        completed_cases=len(rows),
        issues=[
            dict(case_id=r["case"]["case_id"], issues=r["issues"])
            for r in rows
            if r["issues"]
        ],
        methods={
            mode: dict(
                statuses=dict(
                    Counter(r["methods"][mode]["result"]["status"] for r in rows)
                ),
                price_statuses=dict(
                    Counter(
                        r["methods"][mode]["result"]["price_optimization_status"]
                        or "not_applicable"
                        for r in rows
                    )
                ),
                request_latency_by_kind={
                    kind: _latencies(
                        [
                            r["methods"][mode]["request_seconds"]
                            for r in rows
                            if r["case"]["kind"] == kind
                        ]
                    )
                    for kind in ("feasible", "oracle", "salary_infeasible")
                },
                proved_oracle_cases=sum(
                    r["oracle"] is not None
                    and r["methods"][mode]["result"]["status"] == "optimal"
                    for r in rows
                ),
            )
            for mode in ("ovr", "gk_priority_multi_factor")
        },
    )


def _write(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_benchmark(root, run_id, objective):
    import re

    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", run_id):
        raise ValueError("유효한 새 실행 ID가 필요합니다.")
    folder = Path(root) / run_id
    folder.mkdir(parents=True, exist_ok=False)
    plan = plan_spec()
    source_root = Path(__file__).resolve().parents[2]
    files = [Path(__file__)] + [
        source_root / "FRS/core/squad_recommend" / name
        for name in (
            "snapshot.py",
            "solver.py",
            "validator.py",
            "objective.py",
            "multi_factor.py",
            "multi_factor_policy.py",
            "performance.py",
            "placement.py",
        )
    ]
    files += [
        source_root / name
        for name in (
            "FRS/core/candidate_search/search.py",
            "FRS/schema/models.py",
            "FRS/schema/scope.py",
            "FRS/schema/abilities.py",
        )
    ]
    manifest = dict(
        schema_version=SCHEMA,
        run_id=run_id,
        status="running",
        plan=plan,
        plan_hash=content_hash(plan),
        objective=objective_spec(objective),
        python=platform.python_version(),
        platform=platform.platform(),
        ortools=ortools.__version__,
        network="blocked",
        source_hashes={
            str(p.relative_to(source_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files
        },
    )
    _write(folder / "manifest.json", manifest)
    rows = []
    try:
        with (
            patch("socket.socket", side_effect=RuntimeError("network_disabled")),
            patch(
                "socket.create_connection", side_effect=RuntimeError("network_disabled")
            ),
        ):
            for case in case_plan():
                row = measure_case(case, objective)
                _write(folder / f"{case.case_id}.json", row)
                rows.append(row)
        summary = summarize(rows)
        _write(folder / "summary.json", summary)
    except BaseException as error:
        manifest.update(
            status="failed", completed_cases=len(rows), error_type=type(error).__name__
        )
        _write(folder / "manifest.json", manifest)
        raise
    manifest.update(
        status="completed",
        completed_cases=len(rows),
        summary_hash=content_hash(summary),
    )
    _write(folder / "manifest.json", manifest)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--expected-policy-version")
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps(plan_spec(), ensure_ascii=False, indent=2))
        return 0
    if any(
        value is None
        for value in (
            args.policy,
            args.expected_policy_version,
            args.run_id,
            args.output_dir,
        )
    ):
        parser.error(
            "실행에는 policy·expected-policy-version·run-id·output-dir가 모두 필요합니다."
        )
    objective = MultiFactorObjective(
        policy=ScoringPolicy.model_validate_json(args.policy.read_text()),
        expected_policy_version=args.expected_policy_version,
    )
    result = run_benchmark(args.output_dir, args.run_id, objective)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
