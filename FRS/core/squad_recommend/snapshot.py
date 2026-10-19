"""명시 자료를 한 번 복사해 순수 후보·배치·성능 결과를 불변 스냅샷에 고정한다."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Iterable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from FRS.core.candidate_search.search import (
    BatchSelection,
    CandidateQuery,
    candidate_search,
)
from FRS.core.squad_recommend.performance import (
    PerformanceQuery,
    performance_spec,
    score_performance,
)
from FRS.core.squad_recommend.placement import (
    PlacementQuery,
    placement,
    placement_spec,
)
from FRS.schema.models import (
    CardPrice,
    CardSalary,
    NonNegative,
    PlayerCard,
    Position,
    ReinforceStats,
    TeamColor,
    Text,
)
from FRS.schema.scope import FORMATION_SLOTS, PRIORITY_CLUBS


class SnapshotQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    formation: Text
    team_color: Text
    salary_cap: NonNegative
    price_cap: Annotated[int, Field(strict=True, ge=0, le=10**34 - 1)]
    allowed_reinforcements: Annotated[
        tuple[Annotated[int, Field(strict=True, ge=5, le=11)], ...],
        Field(min_length=1),
    ]
    batches: BatchSelection
    t0: AwareDatetime

    @field_validator("formation")
    @classmethod
    def supported_formation(cls, value):
        if value not in FORMATION_SLOTS:
            raise ValueError("MVP에서 지원하지 않는 포메이션입니다.")
        return value

    @field_validator("allowed_reinforcements")
    @classmethod
    def unique_levels(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("허용 강화 집합에 중복이 있습니다.")
        return tuple(sorted(value))

    @field_validator("t0")
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc)


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def snapshot_spec():
    spec = dict(
        version=1,
        formations={name: list(slots) for name, slots in FORMATION_SLOTS.items()},
        supported_reinforcements=list(range(5, 12)),
        batch_selection="explicit_per_resource",
        material="single_read_validated_copy",
        candidate_search="exact_registered_position_union_by_placement_family",
        reinforcements="explicit_allowed_set_per_card",
        expiry="earliest_used_observation_deadline_inclusive",
        expired_response="data_expired_without_candidates_or_recomputation",
        empty="no_candidates_in_provided_material_not_global_infeasibility",
        model_extension="disabled",
        scorer_version=performance_spec()["scorer_version"],
        placement_version=placement_spec()["placement_version"],
    )
    return {
        **spec,
        "snapshot_policy_version": hashlib.sha256(_canonical(spec)).hexdigest(),
    }


@dataclass(frozen=True)
class RequestSnapshot:
    """변경 가능한 사전 대신 bytes를 보관하고 읽을 때 독립 복사본을 반환한다."""

    _payload: bytes

    def __post_init__(self):
        if type(self._payload) is not bytes:
            raise TypeError("스냅샷은 불변 bytes로 고정해야 합니다.")

    @property
    def snapshot_id(self):
        return hashlib.sha256(self._payload).hexdigest()

    def to_dict(self):
        return {**json.loads(self._payload), "snapshot_id": self.snapshot_id}

    def check_response(self, when: datetime):
        """응답 시각만 확인한다. 만료된 후보를 반환하거나 자료를 다시 읽지 않는다."""
        if (
            not isinstance(when, datetime)
            or when.tzinfo is None
            or when.utcoffset() is None
        ):
            raise ValueError("응답 확인 시각에는 시간대가 필요합니다.")
        when = when.astimezone(timezone.utc)
        content = self.to_dict()
        if when < datetime.fromisoformat(content["query"]["t0"]):
            raise ValueError("응답 확인 시각은 t0 이상이어야 합니다.")
        deadline = content["metadata"]["valid_until"]
        expired = deadline is not None and when >= datetime.fromisoformat(deadline)
        status = (
            "data_expired"
            if expired
            else ("ready" if content["candidates"] else "no_candidates")
        )
        return dict(
            status=status,
            evaluated_at=when.isoformat(),
            snapshot_id=self.snapshot_id,
            metadata=content["metadata"],
            candidates=[] if expired else content["candidates"],
            exclusions=content["exclusions"],
        )


def _copy_rows(rows, model):
    copied = []
    for row in rows:
        if not isinstance(row, model):
            raise TypeError(f"{model.__name__} 관측이 필요합니다.")
        copied.append(model.model_validate(row.model_dump()))
    return tuple(copied)


def _documents(candidate):
    return {
        "player_card": candidate.card,
        "card_salary": candidate.salary,
        "card_price": candidate.price,
        "team_color": candidate.team_color,
        **(
            {"reinforce_stats": candidate.reinforcement}
            if candidate.reinforcement
            else {}
        ),
    }


def build_snapshot(
    query: SnapshotQuery,
    *,
    cards: Iterable[PlayerCard],
    salaries: Iterable[CardSalary],
    prices: Iterable[CardPrice],
    reinforcements: Iterable[ReinforceStats],
    team_colors: Iterable[TeamColor],
    supported_clubs=PRIORITY_CLUBS,
):
    """전달 자료만 사용한다. DB 일관 읽기·팀 합계 제약·11인 탐색은 수행하지 않는다."""
    if not isinstance(query, SnapshotQuery):
        raise TypeError("SnapshotQuery로 요청을 명시해야 합니다.")
    query = SnapshotQuery.model_validate(query.model_dump())
    if query.team_color not in supported_clubs:
        raise ValueError("지원하지 않는 클럽입니다.")
    # 호출자의 generator는 각각 한 번만 순회하고 중첩 필드까지 재검증·복사한다.
    material = dict(
        cards=_copy_rows(cards, PlayerCard),
        salaries=_copy_rows(salaries, CardSalary),
        prices=_copy_rows(prices, CardPrice),
        reinforcements=_copy_rows(reinforcements, ReinforceStats),
        team_colors=_copy_rows(team_colors, TeamColor),
    )
    policy = snapshot_spec()
    families = placement_spec()["families"]
    cache = {}
    candidates, exclusions = [], []
    used = {}
    for slot in FORMATION_SLOTS[query.formation]:
        family = families.get(slot, slot)
        registered = tuple(p for p in Position if families.get(p, p) == family)
        for level in query.allowed_reinforcements:
            successes, failures = {}, {}
            for position in registered:
                key = (position, level)
                if key not in cache:
                    search_query = CandidateQuery(
                        position=position,
                        salary_cap=query.salary_cap,
                        price_cap=query.price_cap,
                        team_color=query.team_color,
                        reinforce_level=level,
                        when=query.t0,
                        batches=query.batches,
                    )
                    cache[key] = candidate_search(
                        search_query, **material, supported_clubs=supported_clubs
                    )
                result = cache[key]
                for candidate in result.candidates:
                    successes[candidate.card.spid] = candidate
                for excluded in result.exclusions:
                    failures.setdefault(excluded.spid, set()).add(
                        (excluded.resource, excluded.reason)
                    )
            for spid in sorted(failures.keys() - successes.keys()):
                reasons = failures[spid] - {("player_card", "position_mismatch")}
                for resource, reason in sorted(reasons or failures[spid]):
                    exclusions.append(
                        dict(
                            spid=spid,
                            reinforce_level=level,
                            slot=slot,
                            resource=resource,
                            reason=reason,
                        )
                    )
            for spid, candidate in sorted(successes.items()):
                pair = dict(spid=spid, slot=slot, reinforce_level=level, when=query.t0)
                allowed = placement(PlacementQuery(**pair), card=candidate.card)
                score = score_performance(
                    PerformanceQuery(**pair),
                    card=candidate.card,
                    reinforcement=candidate.reinforcement,
                )
                if not allowed.allowed or score.exclusion is not None:
                    exclusions.append(
                        dict(
                            spid=spid,
                            reinforce_level=level,
                            slot=slot,
                            resource="placement"
                            if not allowed.allowed
                            else "performance",
                            reason=allowed.exclusion
                            if not allowed.allowed
                            else score.exclusion,
                        )
                    )
                    continue
                documents = _documents(candidate)
                for resource, row in documents.items():
                    used[(resource, _canonical(row.model_dump(mode="json")))] = row
                candidates.append(
                    dict(
                        spid=spid,
                        reinforce_level=level,
                        slot=slot,
                        performance=score.performance,
                        raw_ovr=score.raw_ovr,
                        ovr_position=score.ovr_position,
                        placement_type=allowed.placement_type,
                        matched_positions=list(allowed.matched_positions),
                        observations={
                            r: row.model_dump(mode="json")
                            for r, row in documents.items()
                        },
                    )
                )
    rows = tuple(used.values())
    observed = [row.observed_at for row in rows]
    deadlines = [
        d for row in rows for d in (row.valid_until, row.delete_after) if d is not None
    ]
    metadata = dict(
        t0=query.t0.isoformat(),
        observed_at_min=min(observed).isoformat() if observed else None,
        observed_at_max=max(observed).isoformat() if observed else None,
        valid_until=min(deadlines).isoformat() if deadlines else None,
        batches=sorted({row.batch_id for row in rows}),
        used_observation_count=len(rows),
        model_version=None,
        **policy,
    )
    return RequestSnapshot(
        _canonical(
            dict(
                query=query.model_dump(mode="json"),
                metadata=metadata,
                candidates=candidates,
                exclusions=exclusions,
            )
        )
    )
