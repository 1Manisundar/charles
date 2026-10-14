"""Request and response models: the public API contract."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreateLinkRequest(BaseModel):
    url: str = Field(
        description="Absolute http(s) URL to shorten.",
        examples=["https://example.com/a/very/long/path?with=query"],
    )


class LinkResponse(BaseModel):
    code: str = Field(description="Short code identifying the link.")
    short_url: str = Field(description="Public URL that redirects to long_url.")
    long_url: str = Field(description="Destination URL.")
    created_at: datetime = Field(description="Creation time (UTC).")
    is_active: bool = Field(description="False once the link has been deleted.")


class DailyClicks(BaseModel):
    date: str = Field(description="Calendar day (UTC), YYYY-MM-DD.")
    clicks: int = Field(description="Redirects served on that day.")


class StatsResponse(BaseModel):
    code: str = Field(description="Short code the statistics belong to.")
    total_clicks: int = Field(description="Redirects served since the link was created.")
    last_clicked_at: datetime | None = Field(description="Time of the most recent redirect, if any.")
    clicks_by_day: list[DailyClicks] = Field(description="Daily redirect counts for the recent window.")


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
