"""확정한 의미·D8을 새 검증 배치의 관측에 적용한다. 기존 배치는 수정하지 않는다."""

import hashlib
from dataclasses import dataclass

from FRS.core.freshness import Resource, utc, window
from FRS.schema.models import CardPrice, DataStatus, SourceKind, TeamColor
from FRS.utils.crawling.ability_parser import ParsedAbility, ParseError
from FRS.utils.crawling.enrich_cards import COLOR_IDS
from FRS.utils.crawling.player_list_parser import PlayerListContext, parse_player_list

CATALOG_URL = "https://fconline.nexon.com/datacenter/PlayerList"


def _policy_values(resource, observed_at, at):
    timing = window(resource, observed_at, at=at)
    if timing.future_observation:
        raise ParseError("미래 관측을 추천 가능 상태로 만들 수 없습니다.")
    return dict(
        valid_until=timing.policy_expires_at,
        status=DataStatus.STALE if timing.expired_by_policy else DataStatus.OK,
    )


@dataclass(frozen=True)
class AdoptedCatalog:
    prices: tuple[CardPrice, ...]
    memberships: tuple[TeamColor, ...]


def adopt_catalog(
    body: bytes, context: PlayerListContext, request: dict, *, at
) -> AdoptedCatalog:
    """공식 필터의 양성 관측만 만든다. 결과 부재를 자격 상실로 바꾸지 않는다."""
    form = request.get("form", {})
    if (
        context.source != SourceKind.WEB
        or not isinstance(body, bytes)
        or context.version != hashlib.sha256(body).hexdigest()
        or request.get("url") != CATALOG_URL
        or request.get("method") != "POST"
        or request.get("status_code") != 200
        or form.get("teamcolorid") not in COLOR_IDS
        or form.get("strTeamColorCategory") != "affiliation"
        or form.get("n4LeagueId") != "0"
        or form.get("n4TeamId") != ""
        or form.get("n4PageNo") != "1"
        or form.get("n1Strong") != "5"
        or context.reinforce_level != 5
        or form.get("n1Grow") != "0"
        or form.get("n1TeamColor") != "0"
        or tuple(form.get(f"strSkill{i}") for i in range(1, 5)) != context.skill_fields
    ):
        raise ParseError("공식 소속 팀컬러 목록의 출처·필터·원본 해시가 다릅니다.")
    try:
        lower, upper = int(form["n4SalaryMin"]), int(form["n4SalaryMax"])
    except (KeyError, TypeError, ValueError):
        raise ParseError("급여 구간 요청 기록이 없습니다.") from None
    if (lower, upper) not in {(4, 20), (21, 25), (26, 30), (31, 35), (36, 99)}:
        raise ParseError("확정한 급여 구간이 아닙니다.")
    rows = parse_player_list(body, context)
    if any(not lower <= row.salary <= upper for row in rows):
        raise ParseError("요청 급여 구간과 응답이 다릅니다.")
    prices = []
    colors = []
    for row in rows:
        for price in row.prices:
            values = {**price.model_dump(), "price_type": "reference"}
            values.update(_policy_values(Resource.PRICE, price.observed_at, at))
            if price.price is None:
                values.update(status=DataStatus.UNKNOWN, valid_until=None)
            prices.append(CardPrice.model_validate(values))
        fields = row.model_dump(
            include={
                "batch_id",
                "source",
                "observed_at",
                "delete_after",
                "spid",
                "version",
            }
        )
        colors.append(
            TeamColor(
                **fields,
                type="club",
                name=COLOR_IDS[form["teamcolorid"]],
                **_policy_values(Resource.TEAM_COLOR, row.observed_at, at),
            )
        )
    return AdoptedCatalog(tuple(prices), tuple(colors))


def adopt_ability(parsed: ParsedAbility, *, at) -> ParsedAbility:
    """원본 파서로 검증한 관측을 새 배치에 적용한다. 이전 배치에 쓰지 않는다."""
    card, salary = parsed.card, parsed.salary
    if (
        card.source != SourceKind.WEB
        or salary.source != SourceKind.WEB
        or (
            card.spid,
            card.batch_id,
            card.version,
            card.base_reinforce_level,
            card.observed_at,
        )
        != (
            salary.spid,
            salary.batch_id,
            salary.version,
            salary.reinforce_level,
            salary.observed_at,
        )
    ):
        raise ParseError("새 능력치·급여 관측의 출처/식별/시각이 다릅니다.")
    # 평가 시각은 UTC 유효성만 확인하며 관측 시각을 바꾸지 않는다.
    at = utc(at)
    fresh_card = type(card).model_validate(
        {**card.model_dump(), **_policy_values(Resource.ABILITY, card.observed_at, at)}
    )
    fresh_salary = type(salary).model_validate(
        {
            **salary.model_dump(),
            **_policy_values(Resource.SALARY, salary.observed_at, at),
        }
    )
    return ParsedAbility(fresh_card, fresh_salary, parsed.nationality)
