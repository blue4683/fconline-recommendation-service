"""탐색기와 독립적으로 같은 스냅샷의 원래 관측과 11인 제약을 검증한다."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from FRS.core.squad_recommend.performance import PerformanceQuery, score_performance
from FRS.core.squad_recommend.placement import PlacementQuery, placement
from FRS.core.squad_recommend.snapshot import (
    RequestSnapshot,
    SnapshotQuery,
    snapshot_spec,
)
from FRS.schema.models import (
    CardId,
    CardPrice,
    CardSalary,
    PlayerCard,
    Position,
    ReinforceStats,
    TeamColor,
)
from FRS.schema.scope import FORMATION_SLOTS

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Grade = Annotated[int, Field(strict=True, ge=5, le=11)]


class SquadPick(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    spid: CardId
    reinforce_level: Grade
    slot: Position


class SquadSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_id: Hash
    picks: tuple[SquadPick, ...]


class SnapshotObservations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    player_card: PlayerCard
    card_salary: CardSalary
    card_price: CardPrice
    team_color: TeamColor
    reinforce_stats: ReinforceStats | None = None


class SnapshotCandidate(SquadPick):
    performance: Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)]
    raw_ovr: Annotated[int, Field(strict=True, ge=1, le=200)]
    ovr_position: Position
    placement_type: str
    matched_positions: tuple[Position, ...]
    observations: SnapshotObservations


class ValidatedPlayer(SquadPick):
    person_id: int
    name: str
    price: int
    salary: int
    performance: float
    raw_ovr: int
    placement_type: str


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_id: str
    valid: bool
    errors: tuple[str, ...]
    players: tuple[ValidatedPlayer, ...] = ()
    total_price: int | None = None
    total_salary: int | None = None
    total_ovr: int | None = None
    performance: float | None = None


class SnapshotError(ValueError):
    pass


class SnapshotExpired(SnapshotError):
    pass


@dataclass(frozen=True)
class PreparedSnapshot:
    query: SnapshotQuery
    snapshot_id: str
    candidates: tuple[SnapshotCandidate, ...]
    valid_until: datetime | None


def _key(candidate):
    return candidate.slot, candidate.spid, candidate.reinforce_level


def _encoded(row):
    return json.dumps(
        row.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def _aware(when):
    if (
        not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        raise ValueError("검증 시각에는 시간대가 필요합니다.")
    return when.astimezone(timezone.utc)


def _salary_provenance_valid(card, salary, reinforcement):
    ability = reinforcement or card
    if salary.version == ability.version:
        return True
    # 후보 검색 함수를 재사용하지 않고 동일 원본의 파생 연결을 다시 확인한다.
    if reinforcement is None or re.fullmatch(r"[0-9a-f]{64}", card.version) is None:
        return False
    grade = salary.reinforce_level
    if (
        card.source != "nexon_web"
        or card.base_reinforce_level != 5
        or grade not in range(6, 12)
        or reinforcement.reinforce_level != grade
        or reinforcement.baseline_reinforce_level != 5
        or reinforcement.baseline_card_version != card.version
        or (salary.version, reinforcement.version)
        != (f"{card.version}-same-salary-{grade}", f"{card.version}-derived-{grade}")
    ):
        return False
    fields = (
        "spid",
        "batch_id",
        "source",
        "observed_at",
        "valid_until",
        "delete_after",
    )
    origin = tuple(getattr(card, name) for name in fields)
    return (
        tuple(getattr(salary, name) for name in fields) == origin
        and tuple(getattr(reinforcement, name) for name in fields) == origin
    )


def _read(snapshot, when):
    if not isinstance(snapshot, RequestSnapshot):
        raise SnapshotError("invalid_snapshot")
    data = snapshot.to_dict()
    query = SnapshotQuery.model_validate(data["query"])
    if when < query.t0:
        raise SnapshotError("time_before_snapshot")
    candidates = tuple(
        SnapshotCandidate.model_validate(row) for row in data["candidates"]
    )
    keys, used, identities = set(), {}, {}
    for candidate in candidates:
        key = _key(candidate)
        if key in keys:
            raise SnapshotError("duplicate_snapshot_pair")
        keys.add(key)
        if (
            candidate.slot not in FORMATION_SLOTS[query.formation]
            or candidate.reinforce_level not in query.allowed_reinforcements
        ):
            raise SnapshotError("candidate_outside_request")
        observations = candidate.observations
        rows = {
            name: getattr(observations, name)
            for name in type(observations).model_fields
            if getattr(observations, name) is not None
        }
        for resource, row in rows.items():
            if (
                row.spid != candidate.spid
                or row.batch_id != getattr(query.batches, resource)
                or not row.is_usable_at(query.t0)
            ):
                raise SnapshotError("invalid_candidate_observation")
            grade = getattr(row, "reinforce_level", None)
            if grade is not None and grade != candidate.reinforce_level:
                raise SnapshotError("observation_grade_mismatch")
            identity = resource, row.spid, grade
            encoded = _encoded(row)
            if identity in identities and identities[identity] != encoded:
                raise SnapshotError("conflicting_snapshot_observation")
            identities[identity] = encoded
            used[(resource, encoded)] = row
        card, salary, price, color = (
            observations.player_card,
            observations.card_salary,
            observations.card_price,
            observations.team_color,
        )
        reinforcement = observations.reinforce_stats
        if candidate.reinforce_level == 5 and reinforcement is not None:
            raise SnapshotError("unused_reinforcement")
        if (
            not _salary_provenance_valid(card, salary, reinforcement)
            or salary.salary > query.salary_cap
            or price.price > query.price_cap
        ):
            raise SnapshotError("invalid_candidate_cost")
        if color.type != "club" or color.name != query.team_color:
            raise SnapshotError("club_mismatch")
        pair = dict(
            spid=candidate.spid,
            slot=candidate.slot,
            reinforce_level=candidate.reinforce_level,
            when=query.t0,
        )
        allowed = placement(PlacementQuery(**pair), card=card)
        score = score_performance(
            PerformanceQuery(**pair), card=card, reinforcement=reinforcement
        )
        if (
            not allowed.allowed
            or candidate.placement_type != "base"
            or candidate.matched_positions != allowed.matched_positions
        ):
            raise SnapshotError("placement_mismatch")
        if (
            score.exclusion is not None
            or candidate.raw_ovr != score.raw_ovr
            or candidate.performance != score.performance
            or candidate.ovr_position != score.ovr_position
        ):
            raise SnapshotError("performance_mismatch")
    rows = tuple(used.values())
    observed = [row.observed_at for row in rows]
    deadlines = [
        d for row in rows for d in (row.valid_until, row.delete_after) if d is not None
    ]
    expected = dict(
        t0=query.t0.isoformat(),
        observed_at_min=min(observed).isoformat() if observed else None,
        observed_at_max=max(observed).isoformat() if observed else None,
        valid_until=min(deadlines).isoformat() if deadlines else None,
        batches=sorted({r.batch_id for r in rows}),
        used_observation_count=len(rows),
        model_version=None,
        **snapshot_spec(),
    )
    if data["metadata"] != expected:
        raise SnapshotError("snapshot_metadata_mismatch")
    if deadlines and when >= min(deadlines):
        raise SnapshotExpired("data_expired")
    return PreparedSnapshot(
        query, snapshot.snapshot_id, candidates, min(deadlines) if deadlines else None
    )


def prepare_snapshot(snapshot, *, when):
    """모든 유효 후보의 관측/계약/메타를 재검증한다. 원본 저장소를 조회하지 않는다."""
    when = _aware(when)
    try:
        return _read(snapshot, when)
    except SnapshotError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise SnapshotError("invalid_snapshot") from error


def validate_squad(snapshot, selection: SquadSelection, *, when):
    """무효 스쿼드에는 선수 목록/합계를 반환하지 않는다. 가격·급여는 정수로 더한다."""
    when = _aware(when)
    snapshot_id = snapshot.snapshot_id if isinstance(snapshot, RequestSnapshot) else ""

    def invalid(*errors):
        return ValidationReport(snapshot_id=snapshot_id, valid=False, errors=errors)

    try:
        prepared = prepare_snapshot(snapshot, when=when)
    except SnapshotExpired:
        return invalid("data_expired")
    except SnapshotError as error:
        return invalid(str(error))
    if not isinstance(selection, SquadSelection):
        return invalid("invalid_selection")
    try:
        selection = SquadSelection.model_validate(selection.model_dump())
    except ValidationError:
        return invalid("invalid_selection")
    if selection.snapshot_id != prepared.snapshot_id:
        return invalid("snapshot_mismatch")
    errors = []
    expected_slots = FORMATION_SLOTS[prepared.query.formation]
    if len(selection.picks) != 11:
        errors.append("player_count")
    if sorted(p.slot for p in selection.picks) != sorted(expected_slots):
        errors.append("formation_slots")
    candidates = {_key(c): c for c in prepared.candidates}
    spids, people, players = set(), set(), []
    for index, pick in enumerate(selection.picks):
        candidate = candidates.get(_key(pick))
        if candidate is None:
            errors.append(f"pick_{index}:not_in_snapshot")
            continue
        card = candidate.observations.player_card
        if card.spid in spids:
            errors.append("duplicate_card")
        if card.person_id in people:
            errors.append("duplicate_person")
        spids.add(card.spid)
        people.add(card.person_id)
        players.append(
            ValidatedPlayer(
                **pick.model_dump(),
                person_id=card.person_id,
                name=card.name,
                price=candidate.observations.card_price.price,
                salary=candidate.observations.card_salary.salary,
                performance=candidate.performance,
                raw_ovr=candidate.raw_ovr,
                placement_type=candidate.placement_type,
            )
        )
    total_price = sum(p.price for p in players)
    total_salary = sum(p.salary for p in players)
    if total_price > prepared.query.price_cap:
        errors.append("over_budget")
    if total_salary > prepared.query.salary_cap:
        errors.append("over_salary")
    if errors:
        return invalid(*dict.fromkeys(errors))
    ordered = tuple(sorted(players, key=lambda p: expected_slots.index(p.slot)))
    total_ovr = sum(p.raw_ovr for p in ordered)
    return ValidationReport(
        snapshot_id=snapshot_id,
        valid=True,
        errors=(),
        players=ordered,
        total_price=total_price,
        total_salary=total_salary,
        total_ovr=total_ovr,
        performance=total_ovr / 2,
    )
