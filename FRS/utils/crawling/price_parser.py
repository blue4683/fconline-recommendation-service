"""현재가 표시의 정수 BP를 읽는다. 가격 의미·신선도는 확정하지 않는다."""

import re

from bs4 import BeautifulSoup
from pydantic import ValidationError

from FRS.schema.models import CardPrice, DataStatus, PriceType
from FRS.utils.crawling.ability_parser import ParseContext, ParseError


def _bp(value):
    if (
        value is None
        or re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", value.strip()) is None
    ):
        raise ParseError("현재가 속성이 정수 BP 형식이 아닙니다.")
    digits = value.strip().replace(",", "")
    if len(digits) > 34:
        raise ParseError("현재가가 지원하는 정수 BP 범위를 넘습니다.")
    return int(digits)


def parse_price(html: str | bytes, context: ParseContext) -> CardPrice:
    """카드/강화 식별은 요청 기록을 따른다. HTML의 식별자 재표시를 가정하지 않는다."""
    soup = BeautifulSoup(html, "html.parser")
    groups = [
        node
        for node in soup.select(".txt")
        if any(
            label.get_text(strip=True) == "현재가"
            for label in node.find_all("span", recursive=False)
        )
    ]
    if len(groups) != 1:
        raise ParseError("현재가 표시 영역이 없거나 중복되었습니다.")
    values = groups[0].find_all("strong", recursive=False)
    if len(values) != 1:
        raise ParseError("현재가 숫자 영역이 변경되었습니다.")
    node = values[0]
    title, alternate = _bp(node.get("title")), _bp(node.get("alt"))
    if title != alternate:
        raise ParseError("현재가의 두 정수 속성이 서로 다릅니다.")
    try:
        return CardPrice(
            **{
                **context.metadata(),
                "status": DataStatus.UNKNOWN,
                "valid_until": None,
            },
            reinforce_level=context.reinforce_level,
            price_type=PriceType.UNKNOWN,
            price=title,
        )
    except (ValidationError, ValueError):
        raise ParseError(
            "현재가가 가격 문서의 정수 범위를 충족하지 않습니다."
        ) from None
