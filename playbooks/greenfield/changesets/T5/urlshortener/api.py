"""HTTP layer: routes and the mapping from domain errors to problem responses."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .repository import Link
from .schemas import CreateLinkRequest, DailyClicks, LinkResponse, Problem, StatsResponse
from .service import CodeSpaceExhausted, InvalidUrl, LinkGone, LinkNotFound, LinkService

logger = logging.getLogger("urlshortener")

router = APIRouter(prefix="/api/v1", tags=["links"])
redirect_router = APIRouter(tags=["redirect"])

NOT_FOUND = {404: {"model": Problem, "description": "No link with this code"}}
INVALID = {422: {"model": Problem, "description": "The request was rejected by validation"}}


def get_service(request: Request) -> LinkService:
    return request.app.state.link_service


def problem(status: int, title: str, detail: str) -> JSONResponse:
    body = Problem(title=title, status=status, detail=detail)
    return JSONResponse(body.model_dump(), status_code=status, media_type="application/problem+json")


def _link_response(link: Link, request: Request) -> LinkResponse:
    base_url = request.app.state.settings.base_url
    return LinkResponse(
        code=link.code,
        short_url=f"{base_url}/{link.code}",
        long_url=link.long_url,
        created_at=link.created_at,
        is_active=link.is_active,
    )


@router.post(
    "/links",
    status_code=201,
    response_model=LinkResponse,
    responses={**INVALID, 503: {"model": Problem, "description": "No free short code could be allocated"}},
    summary="Create a short link",
)
def create_link(body: CreateLinkRequest, request: Request, service: LinkService = Depends(get_service)) -> LinkResponse:
    return _link_response(service.create(body.url), request)


@router.get("/links/{code}", response_model=LinkResponse, responses=NOT_FOUND, summary="Get a link's metadata")
def get_link(code: str, request: Request, service: LinkService = Depends(get_service)) -> LinkResponse:
    return _link_response(service.get(code), request)


@router.delete("/links/{code}", status_code=204, responses=NOT_FOUND, summary="Delete (deactivate) a link")
def delete_link(code: str, service: LinkService = Depends(get_service)) -> Response:
    service.delete(code)
    return Response(status_code=204)


@router.get(
    "/links/{code}/stats", response_model=StatsResponse, responses=NOT_FOUND, summary="Get a link's click analytics"
)
def link_stats(code: str, service: LinkService = Depends(get_service)) -> StatsResponse:
    stats = service.stats(code)
    return StatsResponse(
        code=stats.code,
        total_clicks=stats.total_clicks,
        last_clicked_at=stats.last_clicked_at,
        clicks_by_day=[DailyClicks(date=day, clicks=count) for day, count in stats.clicks_by_day],
    )


@redirect_router.get(
    "/{code}",
    status_code=307,
    response_class=RedirectResponse,
    responses={**NOT_FOUND, 410: {"model": Problem, "description": "The link is no longer available"}},
    summary="Follow a short link",
)
def follow_link(code: str, request: Request, service: LinkService = Depends(get_service)) -> RedirectResponse:
    destination = service.resolve(code, referrer=request.headers.get("referer"))
    # 307 + no-store: browsers must come back every time, so every visit is counted.
    return RedirectResponse(destination, status_code=307, headers={"Cache-Control": "no-store"})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LinkNotFound)
    async def not_found(request: Request, exc: LinkNotFound) -> JSONResponse:
        return problem(404, "Link not found", "No link exists for this code.")

    @app.exception_handler(LinkGone)
    async def gone(request: Request, exc: LinkGone) -> JSONResponse:
        return problem(410, "Link gone", "This link is no longer available.")

    @app.exception_handler(InvalidUrl)
    async def invalid_url(request: Request, exc: InvalidUrl) -> JSONResponse:
        return problem(422, "Invalid URL", str(exc))

    @app.exception_handler(CodeSpaceExhausted)
    async def exhausted(request: Request, exc: CodeSpaceExhausted) -> JSONResponse:
        return problem(503, "Temporarily unable to create links", "Please retry shortly.")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return problem(422, "Invalid request", errors)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Log the cause; never leak internals to the client.
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return problem(500, "Internal error", "An unexpected error occurred.")
