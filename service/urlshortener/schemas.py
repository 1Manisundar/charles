"""Request and response models: the public API contract."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreateLinkRequest(BaseModel):
    url: str = Field(
        description="Absolute http(s) URL to shorten.",
        examples=["https://example.com/a/very/long/path?with=query"],
    )
    alias: str | None = Field(
        default=None,
        description="Custom short code: 3-32 letters, digits, '-' or '_'. Omit for a random code.",
        examples=["spring-sale"],
    )
    expires_in_seconds: int | None = Field(
        default=None,
        description="Lifetime in seconds (60 to 31536000). Omit for a link that never expires.",
        examples=[86400],
    )


class LinkResponse(BaseModel):
    code: str = Field(description="Short code identifying the link.")
    short_url: str = Field(description="Public URL that redirects to long_url.")
    long_url: str = Field(description="Destination URL.")
    created_at: datetime = Field(description="Creation time (UTC).")
    is_active: bool = Field(description="False once the link has been deleted.")
    expires_at: datetime | None = Field(default=None, description="When the link stops redirecting; null if never.")
    is_custom: bool = Field(default=False, description="True if the code was chosen by the caller.")


class DailyClicks(BaseModel):
    date: str = Field(description="Calendar day (UTC), YYYY-MM-DD.")
    clicks: int = Field(description="Redirects served on that day.")
    unique_visitors: int = Field(
        default=0,
        description="Distinct visitors that day, counted with anonymous identifiers that change daily.",
    )


class ReferrerCount(BaseModel):
    host: str = Field(description="Host of the referring page.")
    clicks: int = Field(description="Redirects that arrived from that host.")


class StatsResponse(BaseModel):
    code: str = Field(description="Short code the statistics belong to.")
    total_clicks: int = Field(description="Redirects served since the link was created.")
    last_clicked_at: datetime | None = Field(description="Time of the most recent redirect, if any.")
    clicks_by_day: list[DailyClicks] = Field(description="Daily redirect counts for the recent window.")
    top_referrers: list[ReferrerCount] = Field(
        default_factory=list, description="Most frequent referring hosts since the link was created."
    )


class Problem(BaseModel):
    """Error body (RFC 7807, served as application/problem+json)."""

    type: str = Field(default="about:blank", description="URI identifying the problem type.")
    title: str = Field(description="Short, stable summary of the problem.")
    status: int = Field(description="HTTP status code.")
    detail: str = Field(description="Explanation specific to this occurrence.")


class ServiceInfo(BaseModel):
    service: str = Field(description="Name of the service.")
    version: str = Field(description="Running version.")
    docs: str = Field(description="Path of the interactive API documentation.")
    openapi: str = Field(description="Path of the OpenAPI schema.")
    health: str = Field(description="Path of the liveness probe.")


class HealthResponse(BaseModel):
    status: str = Field(description="'ok' when the process is alive, 'ready' when it can serve traffic.")
    schema_version: int | None = Field(default=None, description="Applied database schema version.")
