"""환경은 시작 시 읽고 모델 경로/버전은 관리자가 함께 명시한다."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from pydantic import TypeAdapter

from FRS.core.position_recommend.model_contract import SHA256
from FRS.utils.db.client import ENV_PATH, DatabaseConfig


@dataclass(frozen=True)
class ApiSettings:
    database: DatabaseConfig
    model_dir: Path | None = None
    model_version: str | None = None

    def __post_init__(self):
        if (self.model_dir is None) != (self.model_version is None):
            raise ValueError("API_MODEL_DIR과 API_MODEL_VERSION을 함께 명시하세요.")
        if self.model_version is not None:
            TypeAdapter(SHA256).validate_python(self.model_version)

    @classmethod
    def from_env(cls, environ=None):
        if environ is None:
            load_dotenv(ENV_PATH, override=False)
            environ = os.environ
        directory = environ.get("API_MODEL_DIR", "").strip()
        version = environ.get("API_MODEL_VERSION", "").strip()
        return cls(
            database=DatabaseConfig.from_env(environ),
            model_dir=Path(directory) if directory else None,
            model_version=version or None,
        )
