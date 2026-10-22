"""관측 자료 공급기. 명시한 실행의 가공 결과만 읽는다.

최신 실행을 자동으로 고르지 않는다. 완료 보고서의 해시와 출처·배치 계약이 맞지
않으면 거절한다. 합성 경로(`FRS.agent.material`)는 바꾸지 않는다.
"""

import hashlib
import json
import re
from pathlib import Path

from FRS.core.candidate_search.search import BatchSelection
from FRS.schema.models import (
    CardPrice,
    CardSalary,
    PlayerCard,
    ReinforceStats,
    SourceKind,
    TeamColor,
)

RUN_ID_PATTERN = r"real-data-[a-z0-9]+(?:-[a-z0-9]+)*"
_FILES = {
    "cards": (PlayerCard, "player_card"),
    "salaries": (CardSalary, "card_salary"),
    "prices": (CardPrice, "card_price"),
    "reinforcements": (ReinforceStats, "reinforce_stats"),
    "team_colors": (TeamColor, "team_color"),
}


class RealMaterialError(ValueError):
    pass


class ObservedMaterial:
    """`MaterialProvider` 프로토콜을 따른다. 읽기 전용이며 네트워크를 쓰지 않는다."""

    data_mode = "observed"

    def __init__(self, processed_root, run_id):
        if not isinstance(run_id, str) or re.fullmatch(RUN_ID_PATTERN, run_id) is None:
            raise RealMaterialError("실행 ID가 실데이터 실행 형식이 아닙니다.")
        self.run_id = run_id
        self.path = Path(processed_root) / run_id
        report_path = self.path / "report.json"
        if not report_path.is_file():
            raise RealMaterialError("완료 보고서가 없습니다.")
        self.report = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            self.report.get("status") != "completed"
            or self.report.get("run_id") != run_id
            or self.report.get("auto_activation") is not False
        ):
            raise RealMaterialError("완료된 실행의 보고서가 아닙니다.")
        self.verify_hashes()

    @property
    def batches(self):
        return BatchSelection(**{name: self.run_id for _, name in _FILES.values()})

    @property
    def material_id(self):
        return hashlib.sha256(
            json.dumps(self.report["processed_hashes"], sort_keys=True).encode()
        ).hexdigest()

    def verify_hashes(self):
        expected = self.report.get("processed_hashes")
        if not isinstance(expected, dict) or set(expected) != {
            f"{name}.jsonl" for _, name in _FILES.values()
        }:
            raise RealMaterialError("보고서의 가공 파일 해시 목록이 다릅니다.")
        for name, digest in expected.items():
            actual = hashlib.sha256((self.path / name).read_bytes()).hexdigest()
            if actual != digest:
                raise RealMaterialError(f"{name}의 해시가 보고서와 다릅니다.")

    def load(self, batches: BatchSelection) -> dict:
        if not isinstance(batches, BatchSelection):
            raise TypeError("자료 종류별 BatchSelection을 명시해야 합니다.")
        if BatchSelection.model_validate(batches.model_dump()) != self.batches:
            raise RealMaterialError("명시한 배치가 이 실행과 일치하지 않습니다.")
        self.verify_hashes()
        result = {}
        for key, (model, name) in _FILES.items():
            lines = (self.path / f"{name}.jsonl").read_text(encoding="utf-8")
            rows = tuple(
                model.model_validate(json.loads(line))
                for line in lines.splitlines()
                if line
            )
            if any(
                row.source != SourceKind.WEB or row.batch_id != self.run_id
                for row in rows
            ):
                raise RealMaterialError("관측 출처/배치 계약과 일치하지 않습니다.")
            result[key] = rows
        return result
