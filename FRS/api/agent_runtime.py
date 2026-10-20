"""명시 합성 배치와 시작 시 모델 검증 기록. import 중 외부 I/O는 없다."""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Literal

import httpx2
from dotenv import load_dotenv
from pydantic import AwareDatetime, Field

from FRS.agent.json_contract import strict_json
from FRS.agent.material import (
    SyntheticLeagueSpec,
    SyntheticMaterial,
    generate_synthetic_material,
)
from FRS.agent.ollama_selection import OllamaIDSelector, selector_spec
from FRS.agent.ollama_transport import LOCAL_ORIGIN
from FRS.api.agent_contract import WireModel
from FRS.api.agent_projection import SelectionProvenance
from FRS.core.candidate_search.search import BatchSelection
from FRS.schema.scope import ROOT


class AgentSettings(WireModel):
    run_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
    observed_at: AwareDatetime
    model: Literal["qwen3:8b"]
    model_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    seed: Literal[123]
    members_per_family: Literal[3]

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        return cls.model_validate(
            dict(
                run_id=os.environ.get("AGENT_RUN_ID", ""),
                observed_at=datetime.fromisoformat(
                    os.environ.get("AGENT_OBSERVED_AT", "")
                ),
                model="qwen3:8b",
                model_digest=os.environ.get("AGENT_MODEL_DIGEST", ""),
                seed=123,
                members_per_family=3,
            )
        )


@dataclass(frozen=True)
class ModelVerification:
    model: str
    digest: str
    checked_at: datetime
    ollama_version: str


def utc_now():
    return datetime.now(timezone.utc)


def verify_model(settings, *, clock=utc_now, transport=None):
    """시작 시 읽기 전용 최대2요청. 재시도/다운로드/추론은 하지 않는다."""
    with httpx2.Client(
        base_url=LOCAL_ORIGIN,
        timeout=10,
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    ) as client:

        def read(path):
            with client.stream("GET", path) as response:
                if response.status_code != 200:
                    raise ValueError("모델 상태 확인 실패")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > 131072:
                        raise ValueError("모델 상태 응답 상한 초과")
                    raw.extend(chunk)
                return strict_json(raw.decode("utf-8"))

        version = read("/api/version")
        if (
            not isinstance(version, dict)
            or not isinstance(version.get("version"), str)
            or not version["version"]
        ):
            raise ValueError("모델 실행 환경 미확인")
        tags = read("/api/tags")
        matching = [m for m in tags["models"] if m["name"] == settings.model]
        if len(matching) != 1 or matching[0].get("digest") != settings.model_digest:
            raise ValueError("모델 digest 불일치")
    return ModelVerification(
        settings.model, settings.model_digest, clock(), version["version"]
    )


@dataclass(frozen=True)
class AgentService:
    settings: AgentSettings
    material: object
    selector: object
    verification: ModelVerification | None

    _metadata: bytes = field(init=False, repr=False)

    def __post_init__(self):
        if (
            type(self.settings) is not AgentSettings
            or type(self.material) is not SyntheticMaterial
        ):
            raise ValueError("검증 가능한 합성 서비스 설정이 필요합니다.")
        if not callable(getattr(self.selector, "select", None)):
            raise ValueError("명시한 조건 추출기가 필요합니다.")
        data = self.material.to_dict()
        expected = SyntheticLeagueSpec(
            **self.settings.model_dump(exclude={"model", "model_digest"})
        )
        if data["data_mode"] != "synthetic" or data["inputs"] != expected.model_dump(
            mode="json"
        ):
            raise ValueError("시작 설정과 합성 배치가 일치하지 않습니다.")
        deadline = min(
            datetime.fromisoformat(r["valid_until"])
            for rows in data["records"].values()
            for r in rows
        )
        metadata = dict(
            id=self.material.material_id,
            batches=data["batches"],
            observed_at=self.settings.observed_at.isoformat(),
            valid_until=deadline.isoformat(),
        )
        object.__setattr__(self, "_metadata", json.dumps(metadata).encode())

    @property
    def material_id(self):
        return json.loads(self._metadata)["id"]

    @property
    def batches(self):
        return BatchSelection.model_validate(json.loads(self._metadata)["batches"])

    @property
    def provenance(self):
        if self.verification is None:
            raise ValueError("모델 검증 기록이 없습니다.")
        return SelectionProvenance(
            self.verification.model,
            self.verification.digest,
            selector_spec()["selector_version"],
        )

    def health(self, now):
        metadata = json.loads(self._metadata)
        deadline = datetime.fromisoformat(metadata["valid_until"])
        reason = None
        if now < self.settings.observed_at or now >= deadline:
            reason = "data_expired"
        verification = self.verification
        if type(verification) is not ModelVerification:
            verification = None
            reason = reason or "model_unverified"
        elif (
            verification.model != self.settings.model
            or verification.digest != self.settings.model_digest
            or verification.checked_at.tzinfo is None
            or verification.checked_at > now
            or not verification.ollama_version
        ):
            reason = reason or "model_unverified"
        return dict(
            schema_version="agent-health.v1",
            status="ready" if reason is None else "unavailable",
            reason=reason,
            data_mode="synthetic",
            model=dict(
                name=self.settings.model,
                digest=self.settings.model_digest,
                verification="startup_snapshot",
                checked_at=None
                if verification is None
                else verification.checked_at.isoformat(),
                ollama_version=None
                if verification is None
                else verification.ollama_version,
            ),
            material=metadata,
        )


def build_service(settings, *, clock=utc_now, verifier=verify_model):
    if type(settings) is not AgentSettings:
        raise ValueError("명시한 Agent 시작 설정이 필요합니다.")
    settings = AgentSettings.model_validate(settings.model_dump())
    material = generate_synthetic_material(
        SyntheticLeagueSpec(**settings.model_dump(exclude={"model", "model_digest"}))
    )
    service = AgentService(settings, material, OllamaIDSelector(settings.model), None)
    if service.health(clock())["reason"] == "data_expired":
        return service
    try:
        verified = verifier(settings, clock=clock)
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        httpx2.HTTPError,
    ):
        verified = None
    return AgentService(settings, material, service.selector, verified)
