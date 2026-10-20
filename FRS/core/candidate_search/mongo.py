"""명시적 배치 조회를 순수 검색 계약에 연결하는 읽기 전용 MongoDB 어댑터."""

from bson.codec_options import CodecOptions

from FRS.core.candidate_search.search import (
    CandidateQuery,
    SearchResult,
    candidate_search,
    request_errors,
)
from FRS.schema.models import DOCUMENT_MODELS
from FRS.schema.scope import PRIORITY_CLUBS

# 충돌 관측도 읽어야 하므로 unique·status·valid_until 필터를 적용하지 않는다.
CANDIDATE_INDEXES = {
    "player_card": (("batch_id", 1), ("spid", 1)),
    "card_salary": (("batch_id", 1), ("reinforce_level", 1), ("spid", 1)),
    "card_price": (("batch_id", 1), ("reinforce_level", 1), ("spid", 1)),
    "reinforce_stats": (("batch_id", 1), ("reinforce_level", 1), ("spid", 1)),
    "team_color": (("batch_id", 1), ("type", 1), ("name", 1), ("spid", 1)),
}


class MongoCandidateRepository:
    def __init__(self, database, *, supported_clubs=PRIORITY_CLUBS):
        self.database = database.with_options(codec_options=CodecOptions(tz_aware=True))
        self.supported_clubs = tuple(supported_clubs)

    def search(self, query: CandidateQuery) -> SearchResult:
        errors = request_errors(query, self.supported_clubs)
        if errors:
            return SearchResult(request_errors=errors)
        loaded = {}
        for resource, batch_id in query.batches.model_dump().items():
            selector = {"batch_id": batch_id}
            if resource in ("card_salary", "card_price", "reinforce_stats"):
                selector["reinforce_level"] = query.reinforce_level
            if resource == "team_color":
                selector.update(type=query.team_color_type.value, name=query.team_color)
            model = DOCUMENT_MODELS[resource]
            loaded[resource] = [
                model.from_document(document)
                for document in self.database[resource].find(selector)
            ]
        # 잘못된 문서나 DB 장애는 빈 결과로 위장하지 않고 호출자에게 전달한다.
        return candidate_search(
            query,
            cards=loaded["player_card"],
            salaries=loaded["card_salary"],
            prices=loaded["card_price"],
            reinforcements=loaded["reinforce_stats"],
            team_colors=loaded["team_color"],
            supported_clubs=self.supported_clubs,
        )


def create_candidate_indexes(database):
    """호출할 때만 비유일 인덱스를 만든다. 검색·import 시 DB를 변경하지 않는다."""
    return {
        name: database[name].create_index(list(keys), name="candidate_lookup")
        for name, keys in CANDIDATE_INDEXES.items()
    }
