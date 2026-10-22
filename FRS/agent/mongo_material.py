"""명시한 로컬 MongoDB 배치를 원본 파일과 대조한 뒤 기존 Agent에 공급한다.

파일은 무결성 기준이며 반환할 관측은 DB에서 읽는다. 장애/불일치 시 파일로
대체하거나 적재·인덱스 생성·최신 선택을 하지 않는다. import/생성 시 DB I/O는 없다.
"""

import hashlib
import json

from bson.codec_options import CodecOptions
from pymongo.errors import PyMongoError

from FRS.agent.real_material import ObservedMaterial, RealMaterialError
from FRS.core.candidate_search.search import BatchSelection
from FRS.schema.models import DOCUMENT_MODELS
from FRS.utils.db.client import Client, DatabaseConfig
from FRS.utils.db.load_observed import COLLECTIONS, LOAD_LOG


def _digest(rows):
    """Mongo _id·문서 순서 외에는 typed model의 모든 값을 대조한다."""
    encoded = sorted(
        json.dumps(
            row.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        for row in rows
    )
    return hashlib.sha256(json.dumps(encoded, ensure_ascii=False).encode()).hexdigest()


def local_client():
    config = DatabaseConfig.from_env()
    if config.host not in ("127.0.0.1", "localhost", "::1"):
        raise RealMaterialError("관측 Agent의 MongoDB는 로컬 호스트만 허용합니다.")
    return Client(config=config, retry_reads=False)


class MongoObservedMaterial:
    """매 load마다 연결을 닫고 원본과 같은 DB 행만 반환한다."""

    data_mode = "observed"

    def __init__(self, processed_root, run_id, *, client_factory=local_client):
        self._reference = ObservedMaterial(processed_root, run_id)
        self.run_id = run_id
        self._client_factory = client_factory
        source = self._reference.load(self.batches)
        self._counts = {COLLECTIONS[key]: len(rows) for key, rows in source.items()}
        self._digests = {key: _digest(rows) for key, rows in source.items()}
        self._hashes = dict(self._reference.report["processed_hashes"])
        self._report_digest = hashlib.sha256(
            (self._reference.path / "report.json").read_bytes()
        ).hexdigest()

    @property
    def batches(self):
        return self._reference.batches

    @property
    def material_id(self):
        return self._reference.material_id

    def _audit(self, database):
        with database[LOAD_LOG].find({"run_id": self.run_id}).limit(2) as cursor:
            logs = list(cursor)
        if len(logs) != 1:
            raise RealMaterialError("명시한 실행의 단일 적재 기록이 필요합니다.")
        record = logs[0]
        counts = record.get("counts")
        if (
            record.get("material_id") != self.material_id
            or record.get("processed_hashes") != self._hashes
            or counts != self._counts
            or any(type(value) is not int for value in counts.values())
        ):
            raise RealMaterialError("DB 적재 기록이 원본 계약과 다릅니다.")

    def load(self, batches: BatchSelection) -> dict:
        if not isinstance(batches, BatchSelection):
            raise TypeError("자료 종류별 BatchSelection을 명시해야 합니다.")
        if BatchSelection.model_validate(batches.model_dump()) != self.batches:
            raise RealMaterialError("명시한 배치가 이 실행과 일치하지 않습니다.")
        if (
            hashlib.sha256(
                (self._reference.path / "report.json").read_bytes()
            ).hexdigest()
            != self._report_digest
        ):
            raise RealMaterialError("시작 후 원본 보고서가 변경됐습니다.")
        self._reference.verify_hashes()
        try:
            with self._client_factory() as client:
                database = client.db.with_options(
                    codec_options=CodecOptions(tz_aware=True)
                )
                self._audit(database)
                result = {}
                for key, name in COLLECTIONS.items():
                    model = DOCUMENT_MODELS[name]
                    # 정상/기한 필터로 unknown·충돌·만료 행을 숨기지 않는다.
                    with (
                        database[name]
                        .find({"batch_id": self.run_id})
                        .limit(self._counts[name] + 1) as cursor
                    ):
                        rows = tuple(model.from_document(doc) for doc in cursor)
                    if (
                        len(rows) != self._counts[name]
                        or _digest(rows) != self._digests[key]
                    ):
                        raise RealMaterialError("DB 관측이 원본 행과 다릅니다.")
                    result[key] = rows
                self._audit(database)
                return result
        except PyMongoError:
            # 드라이버 예외의 주소/자격 정보는 외부에 전달하지 않는다. 재시도 없음.
            raise RealMaterialError("로컬 관측 DB를 읽을 수 없습니다.") from None
