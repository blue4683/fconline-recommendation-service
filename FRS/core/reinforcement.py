"""1강의 상세 능력치·존재하는 OVR만 파생한다. I/O와 추천 상태 판단은 없다."""

import hashlib
import json
from dataclasses import dataclass

from FRS.schema.abilities import STAT_NAMES
from FRS.schema.models import PlayerCard

_BONUSES = (0, 1, 2, 4, 6, 8, 11, 15, 17, 19, 21, 24, 27)
_MAX_INTEGER = 2**63 - 1


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def reinforcement_bonus(level: int) -> int:
    if type(level) is not int or not 1 <= level <= 13:
        raise ValueError("계산 강화는 1~13의 정수여야 합니다.")
    return _BONUSES[level - 1]


def reinforcement_delta(baseline: int, target: int) -> int:
    return reinforcement_bonus(target) - reinforcement_bonus(baseline)


def reinforcement_spec():
    spec = dict(
        version=1,
        source="user_rule_with_partial_level_5_to_11_observation_comparison",
        source_image_sha256=(
            "e9f061d3f0c6aefb93631f28b5278757342e82b0d85d779151f73da9a7f78adc"
        ),
        bonuses=list(_BONUSES),
        stats=list(STAT_NAMES),
        baseline=1,
        formula="base_value + bonus(target)",
        position_ovr="absolute_existing_keys_only_without_clipping",
        input_hash="canonical_revalidated_player_card_not_html_or_manifest",
        metadata="preserve_source_status_times_without_other_feature_derivation",
        output="independent_abilities_not_recommendation_observation",
    )
    return {
        **spec,
        "rule_version": hashlib.sha256(_canonical(spec)).hexdigest(),
    }


@dataclass(frozen=True)
class DerivedAbilities:
    """내부 계산 결과를 bytes로 보관하고 독립 복사본만 반환한다."""

    _payload: bytes

    def __post_init__(self):
        if type(self._payload) is not bytes:
            raise TypeError("파생 결과는 불변 bytes로 고정해야 합니다.")

    @property
    def version(self):
        return hashlib.sha256(self._payload).hexdigest()

    def to_dict(self):
        return {**json.loads(self._payload), "version": self.version}


def derive_abilities(card: PlayerCard, target_level: int) -> DerivedAbilities:
    bonus = reinforcement_bonus(target_level)
    if not isinstance(card, PlayerCard):
        raise TypeError("기준 PlayerCard를 명시해야 합니다.")
    # 가변 사전·model_copy/construct로 변경된 객체도 경계에서 다시 검증한다.
    source = PlayerCard.model_validate(card.model_dump())
    if source.base_reinforce_level != 1:
        raise ValueError("기준 1강 원본이 필요합니다.")
    if set(source.stats) != set(STAT_NAMES):
        raise ValueError("명시한 34개 능력치 키가 정확히 필요합니다.")
    stats = {name: source.stats[name] + bonus for name in STAT_NAMES}
    ovr = {key: value + bonus for key, value in source.position_ovr.items()}
    if any(value > _MAX_INTEGER for value in (*stats.values(), *ovr.values())):
        raise ValueError("파생 능력치가 기존 정수 범위를 초과했습니다.")
    original = source.model_dump(mode="json")
    payload = dict(
        kind="derived_abilities",
        schema_version=1,
        spid=source.spid,
        source_reference={
            key: original[key]
            for key in (
                "batch_id",
                "version",
                "source",
                "base_reinforce_level",
                "status",
                "observed_at",
                "valid_until",
                "delete_after",
            )
        },
        source_payload_sha256=hashlib.sha256(_canonical(original)).hexdigest(),
        rule_version=reinforcement_spec()["rule_version"],
        target_level=target_level,
        bonus=bonus,
        stats=stats,
        position_ovr=ovr,
    )
    return DerivedAbilities(_canonical(payload))
