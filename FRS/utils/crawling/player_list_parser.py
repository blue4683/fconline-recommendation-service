"""선수 목록의 부분 관측을 읽는다. 완전한 카드·자격·전체 목록으로 승격하지 않는다."""

import re
from typing import Literal

from bs4 import BeautifulSoup
from pydantic import ValidationError, model_validator

from FRS.schema.models import (
    CardObservation,
    CardPrice,
    DataStatus,
    NonNegative,
    ObservedDocument,
    Position,
    Positive,
    PriceType,
    ReinforceLevel,
    Text,
)
from FRS.schema.scope import SUPPORTED_REINFORCEMENTS
from FRS.utils.crawling.ability_parser import ParseError
from FRS.utils.crawling.price_parser import parse_bp

# 현재 확인한 네 열만 지원한다. 요청 순서와 응답 data-type을 모두 대조한다.
Skill = Literal["sprintspeed", "acceleration", "strength", "stamina"]
STAT_NAMES = {
    "sprintspeed": "speed",
    "acceleration": "accelartion",
    "strength": "struggle",
    "stamina": "stamina",
}


class PlayerListContext(ObservedDocument):
    version: Text
    reinforce_level: ReinforceLevel
    skill_fields: tuple[Skill, Skill, Skill, Skill]

    @model_validator(mode="after")
    def validate_skills(self):
        if len(set(self.skill_fields)) != 4:
            raise ValueError("선택 능력치 열은 중복될 수 없습니다.")
        return self

    def metadata(self):
        return {
            **self.model_dump(exclude={"reinforce_level", "skill_fields"}),
            "status": DataStatus.UNKNOWN,
            "valid_until": None,
        }


class PlayerListRow(CardObservation):
    """목록 전용 결과다. PlayerCard/CardSalary 컬렉션에 적재하지 않는다."""

    status: Literal[DataStatus.UNKNOWN] = DataStatus.UNKNOWN
    valid_until: None = None
    name: Text
    reinforce_level: ReinforceLevel
    primary_position: Position
    listed_position_ovr: dict[Position, Positive]
    displayed_ovr: Positive
    base_ovr: Positive | None
    salary: Positive
    displayed_stats: dict[str, NonNegative]
    prices: tuple[CardPrice, ...]


def _one(node, selector):
    found = node.select(selector)
    if len(found) != 1:
        raise ParseError(f"목록 필수 영역 누락/중복: {selector}")
    return found[0]


def _integer(value):
    if re.fullmatch(r"[0-9]{1,18}", value.strip()) is None:
        raise ParseError("목록 숫자 표기가 변경되었습니다.")
    return int(value.strip())


def _direct_integer(node):
    return _integer("".join(node.find_all(string=True, recursive=False)).strip())


def _parse_row(node, context):
    match = re.fullmatch(r"area_playerunit_([0-9]{7,9})", node["id"])
    if match is None:
        raise ParseError("목록 카드 ID 형식이 변경되었습니다.")
    spid = int(match[1])
    skill_class = f"skillData_{spid}"
    grade = _one(node, f'input[name="Strong{spid}"]').get("value", "")
    selected = _one(node, ".info_bottom .en_wrap > a.ability")
    if (
        re.fullmatch(rf"{context.reinforce_level}/[0-9]+", grade) is None
        or f"en_level{context.reinforce_level}" not in selected.get("class", [])
        or selected.get_text(strip=True) != str(context.reinforce_level)
    ):
        raise ParseError("목록의 강화 표시와 요청이 다릅니다.")
    positions = node.select(".info_middle .position")
    if not positions:
        raise ParseError("목록 표시 포지션이 없습니다.")
    listed_ovr = {}
    for position in positions:
        label = _one(position, ".txt").get_text(strip=True)
        if label in listed_ovr:
            raise ParseError("목록 표시 포지션이 중복되었습니다.")
        listed_ovr[label] = _direct_integer(_one(position, f".{skill_class}"))
    primary_position = next(iter(listed_ovr))
    position_ovr = listed_ovr[primary_position]
    cells = node.select(".tr > .td.td_ar")
    if len(cells) != 6:
        raise ParseError("급여/OVR/능력치 열 개수가 변경되었습니다.")
    salary = _integer(_one(cells[0], ".pay").get_text(strip=True))
    ovr = _one(cells[1], f".{skill_class}")
    displayed_ovr = _direct_integer(ovr)
    if displayed_ovr != position_ovr:
        raise ParseError("포지션 OVR과 목록 OVR이 다릅니다.")
    base_nodes = ovr.select("em")
    if len(base_nodes) > 1:
        raise ParseError("기본 OVR 표기가 중복되었습니다.")
    base_ovr = None
    if base_nodes:
        base = re.fullmatch(r"\(([0-9]+)\)", base_nodes[0].get_text(strip=True))
        if base is None:
            raise ParseError("기본 OVR 표기가 변경되었습니다.")
        base_ovr = _integer(base[1])
    stats = {}
    for cell, skill in zip(cells[2:], context.skill_fields, strict=True):
        value = _one(cell, f".{skill_class}")
        if value.get("data-type") != skill:
            raise ParseError("요청과 목록 능력치 열이 다릅니다.")
        stats[STAT_NAMES[skill]] = _direct_integer(value)
    prices = []
    area = _one(node, f".td_ar_bp.bp_{spid}")
    for level in SUPPORTED_REINFORCEMENTS:
        price = _one(area, f".span_bp{level}")
        title, alternate = parse_bp(price.get("title")), parse_bp(price.get("alt"))
        if title != alternate:
            raise ParseError("목록 가격의 두 정수 속성이 다릅니다.")
        prices.append(
            CardPrice(
                **context.metadata(),
                spid=spid,
                reinforce_level=level,
                price_type=PriceType.UNKNOWN,
                # 0은 유효 BP가 아니다. 거래 불가로 단정하지 않고 미상으로 둔다.
                price=title or None,
            )
        )
    return PlayerListRow(
        **context.metadata(),
        spid=spid,
        name=_one(node, ".info_top .name").get_text(strip=True),
        reinforce_level=context.reinforce_level,
        primary_position=primary_position,
        listed_position_ovr=listed_ovr,
        displayed_ovr=displayed_ovr,
        base_ovr=base_ovr,
        salary=salary,
        displayed_stats=stats,
        prices=tuple(prices),
    )


def parse_player_list(
    html: str | bytes, context: PlayerListContext
) -> tuple[PlayerListRow, ...]:
    """일부 목록만 반환한다. 빈 응답은 정상 빈 목록 구조 확인 전까지 거부한다."""
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select('[id^="area_playerunit_"]')
    if not nodes:
        raise ParseError("목록 카드 영역이 없습니다. 정상 빈 목록인지 미확인입니다.")
    try:
        rows = tuple(_parse_row(node, context) for node in nodes)
        if len({row.spid for row in rows}) != len(rows):
            raise ParseError("목록 카드 ID가 중복되었습니다.")
        return rows
    except ValidationError as error:
        raise ParseError("목록 값이 부분 관측 계약을 충족하지 않습니다.") from error
