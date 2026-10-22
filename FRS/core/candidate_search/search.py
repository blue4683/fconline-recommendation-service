"""I/O 없이 유효한 관측을 결합한다. 최신 배치 선택·점수 계산은 하지 않는다."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Annotated, Iterable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from FRS.schema.models import (
    CardPrice,
    CardSalary,
    DataStatus,
    NonNegative,
    PlayerCard,
    Position,
    ReinforceStats,
    SourceKind,
    TeamColor,
    TeamColorType,
    Text,
)
from FRS.schema.scope import PRIORITY_CLUBS


class BatchSelection(BaseModel):
    """호출자가 자료 종류별로 고정한 배치. 빈 문자열·암묵 선택을 허용하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    player_card: Text
    card_salary: Text
    card_price: Text
    reinforce_stats: Text
    team_color: Text


class CandidateQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: Position
    salary_cap: NonNegative
    price_cap: Annotated[int, Field(strict=True, ge=0, le=10**34 - 1)]
    team_color: Text
    team_color_type: TeamColorType = TeamColorType.CLUB
    reinforce_level: Annotated[int, Field(strict=True, ge=5, le=11)]
    when: AwareDatetime
    batches: BatchSelection


@dataclass(frozen=True)
class Candidate:
    card: PlayerCard
    salary: CardSalary
    price: CardPrice
    team_color: TeamColor
    reinforcement: ReinforceStats | None


@dataclass(frozen=True)
class Exclusion:
    spid: int
    resource: str
    reason: str


@dataclass(frozen=True)
class SearchResult:
    candidates: tuple[Candidate, ...] = ()
    exclusions: tuple[Exclusion, ...] = ()
    request_errors: tuple[str, ...] = ()

    @property
    def excluded_count(self) -> int:
        return len(self.exclusions)

    @property
    def exclusion_counts(self) -> dict[str, int]:
        return dict(Counter(f"{e.resource}:{e.reason}" for e in self.exclusions))

    @property
    def inspected_count(self) -> int:
        return len(self.candidates) + self.excluded_count


def request_errors(query, supported_clubs):
    if (
        query.team_color_type != TeamColorType.CLUB
        or query.team_color not in supported_clubs
    ):
        return ("unsupported_team_color",)
    return ()


def _group(rows, batch_id, predicate=lambda row: True):
    grouped = defaultdict(list)
    for row in rows:
        if row.batch_id == batch_id and predicate(row):
            # 완전히 동일한 관측만 중복 제거하며, 최신 시각으로 선택하지 않는다.
            if row not in grouped[row.spid]:
                grouped[row.spid].append(row)
    return grouped


def _one(rows, when):
    if not rows:
        return None, "missing"
    if len(rows) != 1:
        return None, "conflict"
    row = rows[0]
    if row.status != DataStatus.OK:
        return None, f"status_{row.status.value}"
    if row.observed_at > when:
        return None, "not_yet_observed"
    if row.delete_after is not None and when >= row.delete_after:
        return None, "retention_expired"
    if row.valid_until is None or when >= row.valid_until:
        return None, "expired"
    return row, None


def _salary_matches_ability(card, salary, reinforcement):
    ability = reinforcement or card
    if salary.version == ability.version:
        return True
    # 완료된 관측 파일은 수정하지 않는다. 수집기가 같은 원본에서 만든 두
    # 파생 버전만 연결하며, 단순 suffix 제거/기본 급여 대입은 허용하지 않는다.
    if (
        reinforcement is None
        or card.source != SourceKind.WEB
        or card.base_reinforce_level != 5
        or not 6 <= salary.reinforce_level <= 11
        or re.fullmatch(r"[0-9a-f]{64}", card.version) is None
        or reinforcement.reinforce_level != salary.reinforce_level
        or reinforcement.baseline_reinforce_level != 5
        or reinforcement.baseline_card_version != card.version
        or salary.version != f"{card.version}-same-salary-{salary.reinforce_level}"
        or reinforcement.version != f"{card.version}-derived-{salary.reinforce_level}"
    ):
        return False
    return all(
        getattr(row, field) == getattr(card, field)
        for row in (salary, reinforcement)
        for field in (
            "spid",
            "batch_id",
            "source",
            "observed_at",
            "valid_until",
            "delete_after",
        )
    )


def candidate_search(
    query: CandidateQuery,
    *,
    cards: Iterable[PlayerCard],
    salaries: Iterable[CardSalary],
    prices: Iterable[CardPrice],
    reinforcements: Iterable[ReinforceStats],
    team_colors: Iterable[TeamColor],
    supported_clubs=PRIORITY_CLUBS,
) -> SearchResult:
    """선택한 기본 카드 배치의 spid마다 첫 제외 사유 하나를 반환한다."""
    errors = request_errors(query, supported_clubs)
    if errors:
        return SearchResult(request_errors=errors)
    batches = query.batches
    groups = {
        "player_card": _group(cards, batches.player_card),
        "card_salary": _group(
            salaries,
            batches.card_salary,
            lambda row: row.reinforce_level == query.reinforce_level,
        ),
        "card_price": _group(
            prices,
            batches.card_price,
            lambda row: row.reinforce_level == query.reinforce_level,
        ),
        "reinforce_stats": _group(
            reinforcements,
            batches.reinforce_stats,
            lambda row: row.reinforce_level == query.reinforce_level,
        ),
        "team_color": _group(
            team_colors,
            batches.team_color,
            lambda row: (
                row.type == query.team_color_type and row.name == query.team_color
            ),
        ),
    }
    candidates, exclusions = [], []
    for spid in sorted(groups["player_card"]):
        selected = {}
        failed = None
        # 모든 후보에 공통 순서를 적용하여 제외 건수의 중복 계산을 막는다.
        for resource in (
            "player_card",
            "reinforce_stats",
            "card_salary",
            "card_price",
            "team_color",
        ):
            card = selected.get("player_card")
            if (
                resource == "reinforce_stats"
                and query.reinforce_level == card.base_reinforce_level
            ):
                continue
            row, reason = _one(groups[resource].get(spid, []), query.when)
            if reason:
                failed = Exclusion(spid, resource, reason)
                break
            selected[resource] = row
            if resource == "player_card" and query.position not in row.positions:
                failed = Exclusion(spid, resource, "position_mismatch")
                break
            if resource == "reinforce_stats" and (
                row.baseline_card_version != card.version
                or row.baseline_reinforce_level != card.base_reinforce_level
            ):
                failed = Exclusion(spid, resource, "baseline_mismatch")
                break
            if resource == "card_salary":
                if not _salary_matches_ability(
                    card, row, selected.get("reinforce_stats")
                ):
                    failed = Exclusion(spid, resource, "version_mismatch")
                    break
                if row.salary > query.salary_cap:
                    failed = Exclusion(spid, resource, "over_cap")
                    break
            if resource == "card_price" and row.price > query.price_cap:
                failed = Exclusion(spid, resource, "over_cap")
                break
        if failed:
            exclusions.append(failed)
        else:
            candidates.append(
                Candidate(
                    card=selected["player_card"],
                    salary=selected["card_salary"],
                    price=selected["card_price"],
                    team_color=selected["team_color"],
                    reinforcement=selected.get("reinforce_stats"),
                )
            )
    return SearchResult(tuple(candidates), tuple(exclusions))
