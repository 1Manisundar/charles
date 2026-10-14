"""HTTP layer: routes and the mapping from domain errors to problem responses."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .codec import InvalidAlias
from .ratelimit import RateLimited
from .repository import Link
from .schemas import CreateLinkRequest, DailyClicks, LinkResponse, Problem, ReferrerCount, StatsResponse
from .service import (
    AliasTaken,
    CodeSpaceExhausted,
    InvalidExpiry,
    InvalidUrl,
    LinkGone,
    LinkNotFound,
    LinkService,
    UnsafeDestination,
)

logger = logging.getLogger("urlshortener")

router = APIRouter(prefix="/api/v1", tags=["links"])
redirect_router = APIRouter(tags=["redirect"])

NOT_FOUND = {404: {"model": Problem, "description": "No link with this code"}}
INVALID = {422: {"model": Problem, "description": "The request was rejected by validation"}}
TOO_MANY = {429: {"model": Problem, "description": "Rate limit exceeded; retry after the Retry-After delay"}}


def get_service(request: Request) -> LinkService:
    return request.app.state.link_service


def client_address(request: Request) -> str:
    """The address that rate limits and visitor counts are keyed on.

    X-Forwarded-For is believed only when the deployment declares one trusted
    proxy in front of the service, and then only its last entry - the address
    that proxy itself saw. Earlier entries, and the whole header without a
    proxy, are chosen by the client and would let it dodge the limits.
    """
    if request.app.state.settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def rate_limited(bucket: str):
    """Dependency factory: spend one token from the named limiter for the calling client."""

    def dependency(request: Request) -> None:
        wait = request.app.state.rate_limiters[bucket].check(client_address(request))
        if wait > 0:
            raise RateLimited(wait)

    return dependency


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
        expires_at=link.expires_at,
        is_custom=link.is_custom,
    )


@router.post(
    "/links",
    status_code=201,
    response_model=LinkResponse,
    dependencies=[Depends(rate_limited("create"))],
    responses={
        **INVALID,
        **TOO_MANY,
        409: {"model": Problem, "description": "The requested alias is already taken"},
        503: {"model": Problem, "description": "No free short code could be allocated"},
    },
    summary="Create a short link",
)
def create_link(body: CreateLinkRequest, request: Request, service: LinkService = Depends(get_service)) -> LinkResponse:
    link = service.create(body.url, alias=body.alias, expires_in_seconds=body.expires_in_seconds)
    return _link_response(link, request)


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
        clicks_by_day=[
            DailyClicks(date=day, clicks=count, unique_visitors=stats.unique_visitors_by_day.get(day, 0))
            for day, count in stats.clicks_by_day
        ],
        top_referrers=[ReferrerCount(host=host, clicks=count) for host, count in stats.top_referrers],
    )


@redirect_router.get(
    "/{code}",
    status_code=307,
    response_class=RedirectResponse,
    dependencies=[Depends(rate_limited("redirect"))],
    responses={
        **NOT_FOUND,
        **TOO_MANY,
        410: {"model": Problem, "description": "The link was deleted or has expired"},
    },
    summary="Follow a short link",
)
def follow_link(code: str, request: Request, service: LinkService = Depends(get_service)) -> RedirectResponse:
    destination = service.resolve(
        code,
        referrer=request.headers.get("referer"),
        client_address=client_address(request),
        user_agent=request.headers.get("user-agent"),
    )
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

    @app.exception_handler(UnsafeDestination)
    async def unsafe_destination(request: Request, exc: UnsafeDestination) -> JSONResponse:
        return problem(422, "Unsafe destination", str(exc))

    @app.exception_handler(RateLimited)
    async def too_many_requests(request: Request, exc: RateLimited) -> JSONResponse:
        response = problem(429, "Too many requests", "Rate limit exceeded; retry after the indicated delay.")
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response

    @app.exception_handler(InvalidAlias)
    async def invalid_alias(request: Request, exc: InvalidAlias) -> JSONResponse:
        return problem(422, "Invalid alias", str(exc))

    @app.exception_handler(InvalidExpiry)
    async def invalid_expiry(request: Request, exc: InvalidExpiry) -> JSONResponse:
        return problem(422, "Invalid expiry", str(exc))

    @app.exception_handler(AliasTaken)
    async def alias_taken(request: Request, exc: AliasTaken) -> JSONResponse:
        return problem(409, "Alias already taken", "Choose a different alias.")

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
