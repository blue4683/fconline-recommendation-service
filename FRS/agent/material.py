"""명시한 시드·시각의 가상 리그를 기존 5강 스냅샷 계약에 연결한다."""

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Annotated, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from FRS.core.candidate_search.search import BatchSelection
from FRS.core.freshness import RULES, Resource, specification
from FRS.core.reinforcement import (
    derive_abilities,
    reinforcement_delta,
    reinforcement_spec,
)
from FRS.core.squad_recommend.placement import placement_spec
from FRS.schema.abilities import STAT_NAMES
from FRS.schema.models import (
    CardPrice,
    CardSalary,
    ObservedDocument,
    PlayerCard,
    ReinforceStats,
    SourceKind,
    TeamColor,
)
from FRS.schema.scope import (
    FORMATION_SLOTS,
    PRIORITY_CLUBS,
    SUPPORTED_REINFORCEMENTS,
)

_RESOURCES = {
    "cards": (PlayerCard, "player_card", Resource.ABILITY),
    "salaries": (CardSalary, "card_salary", Resource.SALARY),
    "prices": (CardPrice, "card_price", Resource.PRICE),
    "reinforcements": (ReinforceStats, "reinforce_stats", Resource.ABILITY),
    "team_colors": (TeamColor, "team_color", Resource.TEAM_COLOR),
}


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


class SyntheticLeagueSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9-]{0,47}$")]
    seed: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]
    observed_at: AwareDatetime
    members_per_family: Annotated[int, Field(strict=True, ge=1, le=30)]

    @field_validator("observed_at")
    @classmethod
    def normalize_time(cls, value):
        return ObservedDocument.normalize_time(value)


def synthetic_material_spec():
    families = placement_spec()["families"]
    spec = dict(
        version=1,
        clubs=list(PRIORITY_CLUBS),
        families=sorted(
            {
                families.get(slot, slot)
                for slots in FORMATION_SLOTS.values()
                for slot in slots
            }
        ),
        season=950,
        base_level=1,
        delivery_base_level=5,
        delivered_levels=list(SUPPORTED_REINFORCEMENTS),
        base_stats="independent seeded integers 60..100 for each of the 34 keys",
        base_ovr="seeded integer 70..100 for the registered family only",
        prices="seeded base price 10..100 times 100000000 BP; multiplied by 2**(level-5)",
        salary="independent seeded integer 10..25; same across synthetic grades only",
        membership="positive synthetic club label, no activation or bonus",
        player_metadata="not_generated_not_model_input",
        freshness_policy=specification()["policy_hash"],
        reinforcement_rule=reinforcement_spec()["rule_version"],
        selection="explicit_per_resource_no_latest_or_auto_refresh",
    )
    return {**spec, "generator_version": _hash(spec)}


class MaterialProvider(Protocol):
    def load(self, batches: BatchSelection) -> dict: ...


@dataclass(frozen=True)
class SyntheticMaterial:
    """내부 생성 자료만 보관한다. HTTP 입력 payload를 이 타입으로 받지 않는다."""

    _payload: bytes

    def __post_init__(self):
        if type(self._payload) is not bytes:
            raise TypeError("합성 자료는 불변 bytes로 고정해야 합니다.")

    @property
    def material_id(self):
        return hashlib.sha256(self._payload).hexdigest()

    @property
    def batches(self):
        return BatchSelection.model_validate(self.to_dict()["batches"])

    def to_dict(self):
        return {**json.loads(self._payload), "material_id": self.material_id}

    def load(self, batches: BatchSelection) -> dict:
        if not isinstance(batches, BatchSelection):
            raise TypeError("자료 종류별 BatchSelection을 명시해야 합니다.")
        batches = BatchSelection.model_validate(batches.model_dump())
        data = self.to_dict()
        if batches != BatchSelection.model_validate(data["batches"]):
            raise ValueError("합성 자료의 명시 배치와 일치하지 않습니다.")
        result = {}
        for name, (model, resource, _) in _RESOURCES.items():
            rows = tuple(model.model_validate(row) for row in data["records"][name])
            if any(
                row.source != SourceKind.SYNTHETIC
                or row.batch_id != getattr(batches, resource)
                for row in rows
            ):
                raise ValueError("합성 출처/배치 계약과 일치하지 않습니다.")
            result[name] = rows
        return result


def generate_synthetic_material(spec: SyntheticLeagueSpec) -> SyntheticMaterial:
    if not isinstance(spec, SyntheticLeagueSpec):
        raise TypeError("합성 생성 계약을 명시해야 합니다.")
    spec = SyntheticLeagueSpec.model_validate(spec.model_dump())
    policy = synthetic_material_spec()
    generation_id = _hash({"inputs": spec.model_dump(mode="json"), "policy": policy})
    prefix = f"{spec.run_id}-{generation_id[:16]}"
    batches = BatchSelection(
        **{name: f"{prefix}-{name}" for name in BatchSelection.model_fields}
    )
    rng = random.Random(spec.seed)
    records = {name: [] for name in _RESOURCES}
    originals, derivations = [], []
    person = 0

    def context(resource, version, kind):
        return dict(
            source="synthetic",
            batch_id=getattr(batches, resource),
            observed_at=spec.observed_at,
            valid_until=spec.observed_at + RULES[kind].valid_for,
            delete_after=None,
            status="ok",
            version=version,
        )

    for club in PRIORITY_CLUBS:
        for family in policy["families"]:
            for _ in range(spec.members_per_family):
                person += 1
                spid = 950_000_000 + person
                one = PlayerCard(
                    **{
                        **context(
                            "player_card",
                            f"{generation_id}-base-1-{person}",
                            Resource.ABILITY,
                        ),
                        "batch_id": f"{prefix}-base-1",
                    },
                    spid=spid,
                    season_id=950,
                    person_id=person,
                    name=f"가상선수_{person:04d}",
                    base_reinforce_level=1,
                    positions=(family,),
                    stats={key: rng.randint(60, 100) for key in STAT_NAMES},
                    position_ovr={family: rng.randint(70, 100)},
                )
                originals.append(one.model_dump(mode="json"))
                fifth = derive_abilities(one, 5)
                base = PlayerCard.model_validate(
                    {
                        **one.model_dump(),
                        "batch_id": batches.player_card,
                        "version": fifth.version,
                        "base_reinforce_level": 5,
                        "stats": fifth.to_dict()["stats"],
                        "position_ovr": fifth.to_dict()["position_ovr"],
                    }
                )
                records["cards"].append(base)
                base_price, wage = (
                    rng.randint(10, 100) * 100_000_000,
                    rng.randint(10, 25),
                )
                records["team_colors"].append(
                    TeamColor(
                        **context(
                            "team_color",
                            f"{generation_id}-club-{person}",
                            Resource.TEAM_COLOR,
                        ),
                        spid=spid,
                        type="club",
                        name=club,
                    )
                )
                for level in SUPPORTED_REINFORCEMENTS:
                    derived = fifth if level == 5 else derive_abilities(one, level)
                    values = derived.to_dict()
                    derivations.append(
                        {
                            "spid": spid,
                            "target_level": level,
                            "baseline_card_version": base.version,
                            "derived_version": derived.version,
                            "source_payload_sha256": values["source_payload_sha256"],
                            "rule_version": values["rule_version"],
                        }
                    )
                    records["salaries"].append(
                        CardSalary(
                            **context("card_salary", derived.version, Resource.SALARY),
                            spid=spid,
                            reinforce_level=level,
                            salary=wage,
                        )
                    )
                    records["prices"].append(
                        CardPrice(
                            **context(
                                "card_price",
                                f"{generation_id}-price-{person}-{level}",
                                Resource.PRICE,
                            ),
                            spid=spid,
                            reinforce_level=level,
                            price_type="reference",
                            price=base_price * 2 ** (level - 5),
                        )
                    )
                    if level != 5:
                        records["reinforcements"].append(
                            ReinforceStats(
                                **context(
                                    "reinforce_stats", derived.version, Resource.ABILITY
                                ),
                                spid=spid,
                                reinforce_level=level,
                                baseline_reinforce_level=5,
                                baseline_card_version=base.version,
                                stats_delta={
                                    key: reinforcement_delta(5, level)
                                    for key in STAT_NAMES
                                },
                                position_ovr=values["position_ovr"],
                            )
                        )
    payload = dict(
        data_mode="synthetic",
        inputs=spec.model_dump(mode="json"),
        policy=policy,
        generation_id=generation_id,
        batches=batches.model_dump(),
        originals=originals,
        ability_provenance=derivations,
        records={
            name: [row.model_dump(mode="json") for row in rows]
            for name, rows in records.items()
        },
    )
    return SyntheticMaterial(_canonical(payload))
