"""Application factory.

Run with ``uvicorn urlshortener.main:create_app --factory``.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response

from . import __version__
from .api import install_error_handlers, problem, redirect_router, router
from .config import Settings
from .db import Database
from .repository import LinkRepository
from .schemas import HealthResponse, Problem, ServiceInfo
from .service import LinkService

logger = logging.getLogger("urlshortener")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    db.migrate()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        db.close()

    app = FastAPI(
        title="URL Shortener",
        version=__version__,
        description="Create short links, follow them, and see how often they are used.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.link_service = LinkService(LinkRepository(db), settings)

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        # Accept a caller-supplied id only if it is safe to echo and to log.
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "%s %s -> %s in %.1fms request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request_id,
        )
        return response

    @app.get("/", response_model=ServiceInfo, tags=["ops"], summary="Service index")
    def index() -> ServiceInfo:
        # Someone opening the bare origin gets directions instead of a 404.
        return ServiceInfo(
            service=app.title, version=app.version, docs="/docs", openapi="/openapi.json", health="/healthz"
        )

    @app.get(
        "/healthz",
        response_model=HealthResponse,
        response_model_exclude_none=True,
        tags=["ops"],
        summary="Liveness probe",
    )
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/readyz",
        response_model=HealthResponse,
        responses={503: {"model": Problem, "description": "The database is not reachable"}},
        tags=["ops"],
        summary="Readiness probe",
    )
    def readyz():
        if not db.ping():
            return problem(503, "Not ready", "The database is not reachable.")
        return HealthResponse(status="ready", schema_version=db.schema_version())

    install_error_handlers(app)
    app.include_router(router)
    # Registered last: the catch-all /{code} must not shadow the routes above.
    app.include_router(redirect_router)
    return app
