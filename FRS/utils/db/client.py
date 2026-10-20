"""환경 설정을 검증하고 명시적 생명주기로 MongoDB를 사용한다."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    database: str = "frs"

    def __post_init__(self):
        if not self.host.strip() or not self.database.strip():
            raise ValueError("MongoDB 호스트와 DB 이름은 비어 있을 수 없습니다.")
        if not self.username.strip() or not self.password:
            raise ValueError("MONGO_USERNAME과 MONGO_PASSWORD를 설정하세요.")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("MONGO_PORT는 1~65535 범위의 정수여야 합니다.")

    @classmethod
    def from_env(cls, environ=None):
        if environ is None:
            load_dotenv(ENV_PATH, override=False)
            environ = os.environ
        try:
            port = int(environ.get("MONGO_PORT", "27017"))
        except ValueError:
            raise ValueError("MONGO_PORT는 1~65535 범위의 정수여야 합니다.") from None
        return cls(
            host=environ.get("MONGO_HOST", "127.0.0.1"),
            port=port,
            username=environ.get("MONGO_USERNAME", ""),
            password=environ.get("MONGO_PASSWORD", ""),
            database=environ.get("MONGO_DATABASE", "frs"),
        )


class Client:
    def __init__(self, dbname=None, *, config=None):
        config = config or DatabaseConfig.from_env()
        database = config.database if dbname is None else dbname
        if not database.strip():
            raise ValueError("DB 이름은 비어 있을 수 없습니다.")
        # 자격 증명을 URI에 이어 붙이지 않아 특수문자 인코딩 문제를 피한다.
        self.client = MongoClient(
            host=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
            authSource="admin",
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            tz_aware=True,
            connect=False,
        )
        self.db = self.client[database]

    def ping(self):
        return self.client.admin.command("ping")

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
