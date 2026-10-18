"""클럽 경력 기반 관측 재고. 게임 내 자격·추천 가능 커버리지 판정과 구분한다."""

from collections import Counter, defaultdict
from datetime import datetime, timezone

from FRS.schema.models import CardSalary, PlayerCard, Position, TeamColor
from FRS.schema.scope import supports_reinforcement

PRIORITY_CLUBS = ("리버풀", "첼시", "바이에른 뮌헨")
SALARY_BANDS = (
    ("<=20", 1, 20),
    ("21-25", 21, 25),
    ("26-30", 26, 30),
    ("31-35", 31, 35),
    (">=36", 36, None),
)
MINIMUM_CARDS = 3


class CoverageError(ValueError):
    pass


def salary_band(salary: int) -> str:
    if type(salary) is not int or salary < 1:
        raise CoverageError("급여는 양의 정수여야 합니다.")
    return next(
        label
        for label, lower, upper in SALARY_BANDS
        if lower <= salary and (upper is None or salary <= upper)
    )


def _unique(records, key, label):
    result = {}
    for record in records:
        identity = key(record)
        if identity in result and result[identity] != record:
            raise CoverageError(f"{label} 관측이 충돌합니다. 스냅샷을 명시해야 합니다.")
        result[identity] = record
    return result


def _observable(record, when):
    # 추천 신선도가 지난 관측도 재고에는 남는다. 삭제 기한·미래 관측은 제외한다.
    return record.observed_at <= when and (
        record.delete_after is None or when < record.delete_after
    )


def build_history_coverage(
    cards: list[PlayerCard],
    salaries: list[CardSalary],
    club_history: list[TeamColor],
    *,
    when: datetime,
    reinforce_level: int = 5,
    clubs=PRIORITY_CLUBS,
    positions=tuple(Position),
    minimum: int = MINIMUM_CARDS,
):
    if when.tzinfo is None or when.utcoffset() is None:
        raise CoverageError("집계 시각에는 시간대가 필요합니다.")
    if not supports_reinforcement(reinforce_level):
        raise CoverageError("지원하지 않는 강화 단계입니다.")
    if type(minimum) is not int or minimum < 1:
        raise CoverageError("목표 카드 수는 양의 정수여야 합니다.")
    if (
        not clubs
        or len(set(clubs)) != len(clubs)
        or any(not isinstance(name, str) or not name.strip() for name in clubs)
    ):
        raise CoverageError("집계 클럽은 비어 있지 않은 중복 없는 이름이어야 합니다.")
    try:
        positions = tuple(Position(position) for position in positions)
    except ValueError:
        raise CoverageError("등록 포지션 코드가 유효하지 않습니다.") from None
    if not positions or len(set(positions)) != len(positions):
        raise CoverageError("집계 포지션은 비어 있지 않고 중복될 수 없습니다.")
    when = when.astimezone(timezone.utc)
    card_index = _unique(cards, lambda row: row.spid, "기본 카드")
    salary_index = _unique(
        salaries, lambda row: (row.batch_id, row.spid, row.reinforce_level), "급여"
    )
    histories = _unique(
        (row for row in club_history if row.type == "club"),
        lambda row: (row.batch_id, row.spid, row.name),
        "클럽 경력",
    )
    membership = defaultdict(set)
    for row in histories.values():
        if row.name in clubs and _observable(row, when):
            membership[(row.batch_id, row.spid)].add(row.name)
    counts = defaultdict(set)
    by_club = {name: set() for name in clubs}
    exclusions = Counter()
    for card in card_index.values():
        if not _observable(card, when):
            exclusions["card_not_observable"] += 1
            continue
        if card.base_reinforce_level != reinforce_level:
            exclusions["baseline_level_mismatch"] += 1
            continue
        salary = salary_index.get((card.batch_id, card.spid, reinforce_level))
        if salary is None:
            exclusions["missing_salary"] += 1
            continue
        if salary.version != card.version:
            raise CoverageError("기본 카드와 급여의 응답 버전이 다릅니다.")
        if not _observable(salary, when) or salary.salary is None:
            exclusions["salary_not_observable"] += 1
            continue
        names = membership[(card.batch_id, card.spid)]
        if not names:
            exclusions["missing_target_club_history"] += 1
            continue
        band = salary_band(salary.salary)
        for name in names:
            by_club[name].add(card.spid)
            for position in set(card.positions) & set(positions):
                counts[(position, band, name)].add(card.spid)
    cells = [
        {
            "position": position.value,
            "salary_band": label,
            "club": club,
            "observed_cards": len(counts[(position, label, club)]),
            "observation_gap": max(0, minimum - len(counts[(position, label, club)])),
        }
        for position in positions
        for label, _, _ in SALARY_BANDS
        for club in clubs
    ]
    return {
        "basis": "club_career_history",
        "coverage_status": "unverified",
        "recommendation_ready": False,
        "as_of": when.isoformat(),
        "reinforce_level": reinforce_level,
        "minimum_distinct_spids": minimum,
        "input_unique_cards": len(card_index),
        "history_candidate_cards": len(set().union(*by_club.values())),
        "per_club_cards": {name: len(ids) for name, ids in by_club.items()},
        "excluded_cards": dict(exclusions),
        "cell_count": len(cells),
        "cells_with_observations": sum(cell["observed_cards"] > 0 for cell in cells),
        "cells_below_observation_target": sum(
            cell["observation_gap"] > 0 for cell in cells
        ),
        "cells": cells,
    }
