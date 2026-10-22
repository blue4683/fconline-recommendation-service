"""가상 자료로 관측 파생 급여 연결과 강화 선택의 회귀를 검증한다."""

from datetime import timedelta

import pytest
from test_candidate_search import CLUBS, changed
from test_schema import OBSERVED, VALID
from test_squad_engine import data_set, forge

from FRS.core.candidate_search import BatchSelection, CandidateQuery, candidate_search
from FRS.core.reinforcement import reinforcement_delta
from FRS.core.squad_recommend.snapshot import SnapshotQuery, build_snapshot
from FRS.core.squad_recommend.solver import solve_squad
from FRS.core.squad_recommend.validator import SnapshotError, prepare_snapshot
from FRS.schema.models import ReinforceStats

BATCH = "real-data-synthetic-derived"
BATCHES = BatchSelection(**dict.fromkeys(BatchSelection.model_fields, BATCH))


def material():
    """실제 선수가 없는 11카드에 수집기의 파생 버전 형식을 재현한다."""
    base = data_set(cost=10, salary=20)
    result = {name: [] for name in base}
    for index, card in enumerate(base["cards"]):
        root = f"{index + 1:064x}"
        source = dict(batch_id=BATCH, source="nexon_web")
        card = changed(card, **source, version=root)
        result["cards"].append(card)
        result["team_colors"].append(changed(base["team_colors"][index], **source))
        for grade in range(5, 12):
            result["salaries"].append(
                changed(
                    base["salaries"][index],
                    **source,
                    reinforce_level=grade,
                    version=root if grade == 5 else f"{root}-same-salary-{grade}",
                )
            )
            result["prices"].append(
                changed(
                    base["prices"][index],
                    **source,
                    reinforce_level=grade,
                    price=10 * (grade - 4),
                )
            )
            if grade != 5:
                delta = reinforcement_delta(5, grade)
                result["reinforcements"].append(
                    ReinforceStats(
                        **{
                            **card.model_dump(
                                include={
                                    "spid",
                                    "batch_id",
                                    "source",
                                    "observed_at",
                                    "valid_until",
                                    "delete_after",
                                    "status",
                                }
                            ),
                            "version": f"{root}-derived-{grade}",
                            "reinforce_level": grade,
                            "baseline_reinforce_level": 5,
                            "baseline_card_version": root,
                            "stats_delta": {name: delta for name in card.stats},
                            "position_ovr": {
                                name: value + delta
                                for name, value in card.position_ovr.items()
                            },
                        }
                    )
                )
    return result


def snapshot(data=None, *, budget=1000, grades=tuple(range(5, 12))):
    return build_snapshot(
        SnapshotQuery(
            formation="4-2-2-2",
            team_color=CLUBS[0],
            salary_cap=310,
            price_cap=budget,
            allowed_reinforcements=grades,
            batches=BATCHES,
            t0=OBSERVED,
        ),
        **(material() if data is None else data),
        supported_clubs=CLUBS,
    )


@pytest.mark.parametrize("grade", range(5, 12))
def test_search_keeps_every_requested_grade_without_changing_original_rows(grade):
    data = material()
    result = candidate_search(
        CandidateQuery(
            position="ST",
            salary_cap=310,
            price_cap=1000,
            team_color=CLUBS[0],
            reinforce_level=grade,
            when=OBSERVED,
            batches=BATCHES,
        ),
        **data,
        supported_clubs=CLUBS,
    )
    assert len(result.candidates) == 2
    assert all(c.salary.reinforce_level == grade for c in result.candidates)
    if grade != 5:
        for row in result.candidates:
            assert row.salary.version.endswith(f"-same-salary-{grade}")
            assert row.reinforcement.version.endswith(f"-derived-{grade}")
            assert row.salary.version != row.reinforcement.version


@pytest.mark.parametrize(
    "budget,ovr,highest,cost",
    [(110, 1100, 5, 110), (120, 1102, 6, 120), (1000, 1265, 11, 770)],
)
def test_solver_spends_available_budget_when_an_upgrade_improves_ovr(
    budget, ovr, highest, cost
):
    snap = snapshot(budget=budget)
    result = solve_squad(snap, clock=lambda: OBSERVED)
    assert result.status == "optimal"
    report = result.validation
    assert report.valid and len(report.players) == 11
    assert len({p.person_id for p in report.players}) == 11
    assert report.total_ovr == ovr
    assert report.total_price == cost <= budget
    assert report.total_salary == 220 <= 310
    assert max(p.reinforce_level for p in report.players) == highest


def test_only_explicitly_allowed_grades_are_used():
    result = solve_squad(snapshot(grades=(5,)), clock=lambda: OBSERVED)
    assert result.status == "optimal"
    assert {p.reinforce_level for p in result.validation.players} == {5}


@pytest.mark.parametrize(
    "resource,updates",
    [
        ("card_salary", {"version": "b" * 64 + "-same-salary-6"}),
        ("card_salary", {"version": "a" * 64 + "-same-salary-7"}),
        ("card_salary", {"source": "synthetic"}),
        ("card_salary", {"observed_at": (OBSERVED - timedelta(seconds=1)).isoformat()}),
        ("card_salary", {"valid_until": (VALID - timedelta(seconds=1)).isoformat()}),
        ("card_salary", {"delete_after": (VALID + timedelta(seconds=1)).isoformat()}),
        ("reinforce_stats", {"source": "nexon_api"}),
        ("reinforce_stats", {"version": "b" * 64 + "-derived-6"}),
        ("reinforce_stats", {"baseline_card_version": "b" * 64}),
        ("reinforce_stats", {"reinforce_level": 7}),
        ("reinforce_stats", {"batch_id": "other"}),
    ],
)
def test_independent_validator_rejects_wrong_derivation_origin(resource, updates):
    snap = snapshot(grades=(6,))
    assert prepare_snapshot(snap, when=OBSERVED).candidates

    def alter(data):
        # 두 공격수 슬롯에 등장하는 동일 카드 관측을 모두 바꿔 충돌 검사만으로
        # 거절되지 않게 한다. 원본 연결 자체를 독립 검증해야 한다.
        spid = data["candidates"][0]["spid"]
        for row in data["candidates"]:
            if row["spid"] == spid:
                row["observations"][resource].update(updates)

    with pytest.raises(SnapshotError):
        prepare_snapshot(forge(snap, alter), when=OBSERVED)


@pytest.mark.parametrize(
    "field", ["batch_id", "source", "observed_at", "valid_until", "delete_after"]
)
def test_search_rejects_a_derived_salary_from_a_different_origin(field):
    data = material()
    updates = dict(
        batch_id="other-batch",
        source="synthetic",
        observed_at=OBSERVED - timedelta(seconds=1),
        valid_until=VALID - timedelta(seconds=1),
        delete_after=VALID + timedelta(seconds=1),
    )
    data["salaries"] = [
        changed(row, **{field: updates[field]}) for row in data["salaries"]
    ]
    batches = (
        BATCHES.model_copy(update={"card_salary": "other-batch"})
        if field == "batch_id"
        else BATCHES
    )
    result = candidate_search(
        CandidateQuery(
            position="ST",
            salary_cap=310,
            price_cap=1000,
            team_color=CLUBS[0],
            reinforce_level=6,
            when=OBSERVED,
            batches=batches,
        ),
        **data,
        supported_clubs=CLUBS,
    )
    assert not result.candidates
    assert result.exclusion_counts["card_salary:version_mismatch"] == 2


def test_unknown_expired_and_conflicting_derived_observations_still_fail_closed():
    for updates in (
        dict(status="unknown", valid_until=None),
        dict(observed_at=OBSERVED - timedelta(seconds=1), valid_until=OBSERVED),
    ):
        data = material()
        data["salaries"] = [changed(row, **updates) for row in data["salaries"]]
        assert not snapshot(data, grades=(6,)).to_dict()["candidates"]
    data = material()
    row = next(r for r in data["salaries"] if r.reinforce_level == 6)
    data["salaries"].append(changed(row, salary=row.salary + 1))
    snap = snapshot(data, grades=(6,))
    assert not any(r["spid"] == row.spid for r in snap.to_dict()["candidates"])
