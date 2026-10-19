"""관측 키의 정확 일치만 조회한다. 인덱스·컬렉션 생성과 쓰기는 하지 않는다."""

from bson.codec_options import CodecOptions

from FRS.core.position_recommend.data_contract import ObservationKey
from FRS.schema.models import PlayerCard


class ObservationConflict(ValueError):
    pass


class MongoCardRepository:
    def __init__(self, database):
        self.database = database.with_options(codec_options=CodecOptions(tz_aware=True))

    def get(self, key: ObservationKey):
        values = [
            PlayerCard.from_document(document)
            for document in self.database.player_card.find(key.model_dump())
        ]
        if not values:
            return None
        # 같은 키/다른 _id의 완전 동일 관측만 중복 제거한다. 최신 선택은 없다.
        if any(value != values[0] for value in values[1:]):
            raise ObservationConflict("같은 관측 키의 문서가 충돌합니다.")
        if any(
            ObservationKey(spid=v.spid, batch_id=v.batch_id, version=v.version) != key
            for v in values
        ):
            raise ObservationConflict("조회한 관측 키가 요청과 다릅니다.")
        return values[0]
