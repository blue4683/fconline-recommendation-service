"""현재 능력치 조각과 클럽 이력을 읽는 순수 파서."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from pydantic import AwareDatetime, ValidationError

from FRS.schema.models import (
    CardId,
    CardSalary,
    DataStatus,
    ObservedDocument,
    PlayerCard,
    Position,
    ReinforceLevel,
    ReinforceStats,
    TeamColor,
    Text,
)

KEYWORD_PATH = Path(__file__).resolve().parents[2] / "schema/keyword.json"


class ParseError(ValueError):
    pass


class ParseContext(ObservedDocument):
    spid: CardId
    reinforce_level: ReinforceLevel
    version: Text
    valid_until: AwareDatetime | None = None

    def metadata(self):
        return {
            **self.model_dump(exclude={"reinforce_level"}),
            "status": DataStatus.OK
            if self.valid_until is not None
            else DataStatus.UNKNOWN,
        }


@dataclass(frozen=True)
class ParsedAbility:
    card: PlayerCard
    salary: CardSalary
    nationality: str | None


def keyword_map():
    with KEYWORD_PATH.open(encoding="utf-8") as source:
        return {
            re.sub(r"\s+", "", key): value for key, value in json.load(source).items()
        }


def _text(node):
    return node.get_text(" ", strip=True) if node else None


def _integer(text, label):
    if text is None or not re.fullmatch(r"\d+(?:,\d{3})*", text.strip()):
        raise ParseError(f"{label} 정수 표기를 확인할 수 없습니다.")
    return int(text.replace(",", ""))


def _number_with_unit(soup, selector, unit):
    node = soup.select_one(selector)
    if node is None:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*" + unit + r"\s*", _text(node), re.I)
    if not match:
        raise ParseError("신체 수치 형식이 변경되었습니다.")
    return int(match[1])


def parse_ability(html: str | bytes, context: ParseContext) -> ParsedAbility:
    soup = BeautifulSoup(html, "html.parser")
    name = _text(soup.select_one(".info_name .name"))
    header = soup.select_one(".info_ab")
    if not name or header is None:
        raise ParseError("능력치 응답의 필수 구조가 없습니다.")
    ids = {
        int(match[1])
        for node in header.select("[class]")
        for value in node.get("class", [])
        if (match := re.fullmatch(r"skillData_(\d+)", value))
    }
    if ids != {context.spid}:
        raise ParseError("요청과 응답의 선수 ID가 다릅니다.")
    grades = {
        int(match[1])
        for node in soup.select(".ability[class]")
        for value in node.get("class", [])
        if (match := re.fullmatch(r"en_level(\d+)", value))
    }
    if grades != {context.reinforce_level}:
        raise ParseError("요청과 응답의 강화 단계가 다릅니다.")
    positions = []
    for node in header.select(".position .txt"):
        label = _text(node).upper()
        # HTML의 숫자 보정 라벨만 제외한다. 메타데이터 숫자 ID 변환과는 별개다.
        if re.fullmatch(r"[+-]?\d+", label):
            continue
        try:
            position = Position(label)
        except ValueError:
            raise ParseError("등록 포지션 코드가 변경되었습니다.") from None
        if position not in positions:
            positions.append(position)
    if not positions:
        raise ParseError("등록 포지션이 없습니다.")
    mapping = keyword_map()
    stats = {}
    for node in soup.select(".content_bottom .data_wrap_playerinfo > li.ab"):
        label = re.sub(r"\s+", "", _text(node.select_one(".txt")) or "")
        if label not in mapping:
            raise ParseError("능력치 이름 목록이 변경되었습니다.")
        value = node.select_one(".value")
        direct = (
            "".join(value.find_all(string=True, recursive=False)).strip()
            if value
            else None
        )
        number = _integer(direct, "능력치")
        key = mapping[label]
        if key in stats and stats[key] != number:
            raise ParseError("중복 능력치 값이 다릅니다.")
        stats[key] = number
    if set(stats) != set(mapping.values()):
        raise ParseError("전체 능력치 중 누락된 값이 있습니다.")
    ovr = {}
    for node in soup.select(".ovr_set .position.value"):
        labels = [
            value.upper()
            for value in node.get("class", [])
            if value.upper() in Position._value2member_map_
        ]
        if len(labels) != 1:
            raise ParseError("포지션 OVR 구조가 변경되었습니다.")
        number = _integer(_text(node), "OVR")
        if labels[0] in ovr and ovr[labels[0]] != number:
            raise ParseError("중복 포지션 OVR이 다릅니다.")
        ovr[labels[0]] = number
    if not ovr:
        raise ParseError("포지션 OVR이 없습니다.")
    salary_nodes = soup.select(".pay span, .pay_side span")
    salaries = {_integer(_text(node), "급여") for node in salary_nodes}
    if len(salaries) != 1:
        raise ParseError("급여가 없거나 표시값이 서로 다릅니다.")
    foot = _text(soup.select_one(".info_etc .foot"))
    feet = re.search(r"L\s*(\d+)\s*[–—-]\s*R\s*(\d+)", foot or "")
    if foot and not feet:
        raise ParseError("주발 표기가 변경되었습니다.")
    skill = _text(soup.select_one(".info_etc .skill"))
    traits = soup.select_one(".skill_wrap")
    skills = (
        None
        if traits is None
        else tuple(
            dict.fromkeys(
                node["alt"].strip()
                for node in traits.select("img[alt]")
                if node["alt"].strip()
            )
        )
    )
    nationality = _text(soup.select_one(".etc.nation .txt"))
    try:
        card = PlayerCard(
            **context.metadata(),
            season_id=context.spid // 1_000_000,
            person_id=context.spid % 1_000_000,
            name=name,
            base_reinforce_level=context.reinforce_level,
            positions=tuple(positions),
            stats=stats,
            position_ovr=ovr,
            height=_number_with_unit(soup, ".info_etc .height", "cm"),
            weight=_number_with_unit(soup, ".info_etc .weight", "kg"),
            physical=_text(soup.select_one(".info_etc .physical")),
            dribble_skill=skill.count("★") if skill else None,
            left_foot=int(feet[1]) if feet else None,
            right_foot=int(feet[2]) if feet else None,
            skills=skills,
        )
        salary = CardSalary(
            **context.metadata(),
            reinforce_level=context.reinforce_level,
            salary=next(iter(salaries)),
        )
    except ValidationError:
        raise ParseError("능력치 응답이 문서 계약을 충족하지 않습니다.") from None
    return ParsedAbility(card, salary, nationality)


def parse_club_history(html: str | bytes) -> tuple[str, ...]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".data_detail_club .data_table")
    if root is None:
        raise ParseError("클럽 이력 구조가 없습니다.")
    return tuple(
        dict.fromkeys(name for node in root.select(".td.club") if (name := _text(node)))
    )


def unverified_colors(
    parsed: ParsedAbility, clubs: tuple[str, ...]
) -> tuple[TeamColor, ...]:
    # 국가·클럽 이력을 분리하되 게임 내 발동 자격을 확인한 것으로 승격하지 않는다.
    metadata = parsed.card.model_dump(
        include={
            "batch_id",
            "source",
            "observed_at",
            "delete_after",
            "valid_until",
            "spid",
            "version",
        }
    )
    metadata["status"] = DataStatus.UNKNOWN
    colors = [TeamColor(**metadata, type="club", name=name) for name in clubs]
    if parsed.nationality:
        colors.append(TeamColor(**metadata, type="nation", name=parsed.nationality))
    return tuple(colors)


def reinforcement_delta(base: PlayerCard, enhanced: PlayerCard) -> ReinforceStats:
    if (base.spid, base.batch_id, base.source, set(base.stats)) != (
        enhanced.spid,
        enhanced.batch_id,
        enhanced.source,
        set(enhanced.stats),
    ):
        raise ParseError("강화 비교의 카드·배치·출처·능력치 목록이 다릅니다.")
    metadata = enhanced.model_dump(
        include={
            "batch_id",
            "source",
            "observed_at",
            "delete_after",
            "valid_until",
            "status",
            "spid",
            "version",
        }
    )
    if base.valid_until is None or enhanced.valid_until is None:
        metadata.update(status=DataStatus.UNKNOWN, valid_until=None)
    else:
        metadata["valid_until"] = min(base.valid_until, enhanced.valid_until)
        if base.status != DataStatus.OK or enhanced.status != DataStatus.OK:
            metadata["status"] = DataStatus.UNKNOWN
        if metadata["valid_until"] <= enhanced.observed_at:
            metadata.update(status=DataStatus.STALE, valid_until=None)
    deadlines = [
        value
        for value in (base.delete_after, enhanced.delete_after)
        if value is not None
    ]
    metadata["delete_after"] = min(deadlines) if deadlines else None
    return ReinforceStats(
        **metadata,
        reinforce_level=enhanced.base_reinforce_level,
        baseline_reinforce_level=base.base_reinforce_level,
        baseline_card_version=base.version,
        stats_delta={key: enhanced.stats[key] - base.stats[key] for key in base.stats},
        position_ovr=enhanced.position_ovr,
    )
