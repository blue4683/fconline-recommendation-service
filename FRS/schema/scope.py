"""D2·D3의 확정값. 수집·디렉터리 생성·삭제를 실행하지 않는다."""

from pathlib import Path
from types import MappingProxyType

from FRS.schema.models import Position, TeamColorType

ROOT = Path(__file__).resolve().parents[2]
LOCAL_PATHS = MappingProxyType(
    {
        "raw": ROOT / "data/raw",
        "processed": ROOT / "data/processed",
        "backups": ROOT / "backups",
        "models": ROOT / "artifacts/models",
    }
)
PRIORITY_CLUBS = ("리버풀", "첼시", "바이에른 뮌헨")
SUPPORTED_REINFORCEMENTS = tuple(range(5, 12))
SUPPORTED_TEAM_COLOR_TYPES = frozenset({TeamColorType.CLUB})

FORMATION_SLOTS = MappingProxyType(
    {
        "4-2-3-1": tuple(
            Position(p)
            for p in (
                "GK",
                "RCB",
                "LCB",
                "RB",
                "LB",
                "RDM",
                "LDM",
                "CAM",
                "RAM",
                "LAM",
                "ST",
            )
        ),
        "4-1-2-3": tuple(
            Position(p)
            for p in (
                "GK",
                "RCB",
                "LCB",
                "RB",
                "LB",
                "CDM",
                "RCM",
                "LCM",
                "ST",
                "RW",
                "LW",
            )
        ),
        "4-2-2-2": tuple(
            Position(p)
            for p in (
                "GK",
                "RCB",
                "LCB",
                "RB",
                "LB",
                "RDM",
                "LDM",
                "RM",
                "LM",
                "RS",
                "LS",
            )
        ),
    }
)


def formation_slots(formation: str) -> tuple[Position, ...]:
    try:
        return FORMATION_SLOTS[formation]
    except KeyError:
        raise ValueError("MVP에서 지원하지 않는 포메이션입니다.") from None


def supports_reinforcement(level: int) -> bool:
    return type(level) is int and level in SUPPORTED_REINFORCEMENTS


def supports_team_color(kind: TeamColorType) -> bool:
    return kind in SUPPORTED_TEAM_COLOR_TYPES
