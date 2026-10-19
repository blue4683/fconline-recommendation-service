"""조회·추론 API. import/요청 중 수집·학습·DB 쓰기를 수행하지 않는다."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from FRS.api.repository import MongoCardRepository, ObservationConflict
from FRS.api.schemas import CardQuery, CardResponse, HealthResponse, PositionRequest
from FRS.api.settings import ApiSettings
from FRS.core.position_recommend.artifacts import load_bundle
from FRS.core.position_recommend.data_contract import InputContractError, ObservationKey
from FRS.core.position_recommend.probabilities import ProbabilityResult
from FRS.utils.db.client import Client


def utc_now():
    return datetime.now(timezone.utc)


def create_app(
    *, settings=None, connection_factory=Client, model_loader=load_bundle, clock=utc_now
):
    @asynccontextmanager
    async def lifespan(application):
        config = settings or ApiSettings.from_env()
        connection = connection_factory(config=config.database)
        application.state.connection = connection
        application.state.repository = MongoCardRepository(connection.db)
        application.state.predictor = None
        application.state.model_status = "not_configured"
        try:
            if config.model_dir is not None:
                try:
                    application.state.predictor = model_loader(
                        config.model_dir, expected_model_version=config.model_version
                    )
                    application.state.model_status = "ready"
                except (OSError, ValueError, TypeError, EOFError):
                    # 경로·원본 예외를 응답/로그로 노출하거나 학습으로 복구하지 않는다.
                    application.state.model_status = "unavailable"
            yield
        finally:
            connection.close()

    application = FastAPI(title="FRS API", version="2.0", lifespan=lifespan)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "invalid_request",
                    "fields": [
                        {"location": list(item["loc"]), "type": item["type"]}
                        for item in error.errors()
                    ],
                }
            },
        )

    @application.exception_handler(PyMongoError)
    async def database_error(request, error):
        return JSONResponse(
            status_code=503, content={"detail": {"code": "database_unavailable"}}
        )

    @application.exception_handler(ValidationError)
    async def stored_data_error(request, error):
        return JSONResponse(
            status_code=503, content={"detail": {"code": "invalid_stored_data"}}
        )

    @application.exception_handler(ObservationConflict)
    async def observation_conflict(request, error):
        return JSONResponse(
            status_code=409, content={"detail": {"code": "observation_conflict"}}
        )

    def get_card(key):
        card = application.state.repository.get(key)
        if card is None:
            raise HTTPException(404, detail={"code": "observation_not_found"})
        return card

    @application.get("/health", response_model=HealthResponse)
    def health(response: Response):
        database_status = "ready"
        try:
            application.state.connection.ping()
        except PyMongoError:
            database_status = "unavailable"
        predictor = application.state.predictor
        model_status = application.state.model_status
        healthy = database_status == "ready" and model_status == "ready"
        response.status_code = 200 if healthy else 503
        return HealthResponse(
            status="ok" if healthy else "degraded",
            database=database_status,
            model=model_status,
            model_version=predictor.manifest.model_version if predictor else None,
        )

    @application.get("/cards/{spid}", response_model=CardResponse)
    def card(
        spid: Annotated[int, Path(ge=1_000_000, le=999_999_999)],
        query: Annotated[CardQuery, Query()],
    ):
        value = get_card(ObservationKey(spid=spid, **query.model_dump()))
        when = clock()
        return CardResponse(
            card=value, evaluated_at=when, usable=value.is_usable_at(when)
        )

    @application.post("/recommend/position", response_model=ProbabilityResult)
    def position(body: PositionRequest):
        predictor = application.state.predictor
        if predictor is None:
            raise HTTPException(503, detail={"code": "model_unavailable"})
        if body.model_version != predictor.manifest.model_version:
            raise HTTPException(409, detail={"code": "model_version_mismatch"})
        cards = tuple(get_card(key) for key in body.selection.observations)
        when = clock()
        if any(not value.is_usable_at(when) for value in cards):
            raise HTTPException(409, detail={"code": "unusable_observation"})
        try:
            return predictor.predict_proba(
                [key.spid for key in body.selection.observations],
                cards=cards,
                selection=body.selection,
            )
        except InputContractError as error:
            raise HTTPException(
                422, detail={"code": "input_contract_error", "message": str(error)}
            ) from None

    return application


app = create_app()
