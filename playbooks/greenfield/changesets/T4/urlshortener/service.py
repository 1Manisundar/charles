"""Business rules for short links. No HTTP and no SQL in this module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit

from .codec import generate_code, is_valid_code
from .config import Settings
from .repository import DuplicateCode, Link, LinkRepository

MAX_CODE_ATTEMPTS = 5
MAX_REFERRER_LENGTH = 512


class LinkNotFound(LookupError):
    """No link exists for the code."""


class LinkGone(LookupError):
    """The link exists but may no longer be followed."""


class InvalidUrl(ValueError):
    """The submitted URL cannot be shortened."""


class CodeSpaceExhausted(RuntimeError):
    """No free short code was found within the retry budget."""


@dataclass(frozen=True)
class LinkStats:
    code: str
    total_clicks: int
    last_clicked_at: datetime | None
    clicks_by_day: list[tuple[str, int]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LinkService:
    def __init__(
        self,
        repository: LinkRepository,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
        code_factory: Callable[[int], str] = generate_code,
    ):
        self._repository = repository
        self._settings = settings
        self._clock = clock
        self._code_factory = code_factory

    def create(self, url: str) -> Link:
        long_url = self._validate_url(url)
        now = self._clock()
        # Random codes can collide; the database decides, and we retry a bounded number of times.
        for _ in range(MAX_CODE_ATTEMPTS):
            code = self._code_factory(self._settings.code_length)
            try:
                return self._repository.insert(code, long_url, now)
            except DuplicateCode:
                continue
        raise CodeSpaceExhausted(f"no free code after {MAX_CODE_ATTEMPTS} attempts")

    def get(self, code: str) -> Link:
        link = self._repository.get(code) if is_valid_code(code) else None
        if link is None:
            raise LinkNotFound(code)
        return link

    def resolve(self, code: str, referrer: str | None = None) -> str:
        """Return the destination for a visit and record the click."""
        link = self.get(code)
        now = self._clock()
        if not link.is_active:
            raise LinkGone(code)
        self._repository.record_click(code, now, referrer[:MAX_REFERRER_LENGTH] if referrer else None)
        return link.long_url

    def delete(self, code: str) -> None:
        """Deactivate a link. Idempotent: deleting twice is not an error."""
        self.get(code)
        self._repository.deactivate(code)

    def stats(self, code: str) -> LinkStats:
        link = self.get(code)
        since = self._clock() - timedelta(days=self._settings.stats_days)
        return LinkStats(
            code=link.code,
            total_clicks=self._repository.click_count(code),
            last_clicked_at=self._repository.last_click(code),
            clicks_by_day=self._repository.clicks_by_day(code, since),
        )

    def _validate_url(self, url: str) -> str:
        candidate = url.strip()
        if not candidate:
            raise InvalidUrl("url must not be empty")
        if len(candidate) > self._settings.max_url_length:
            raise InvalidUrl(f"url is longer than {self._settings.max_url_length} characters")
        if any(ch.isspace() or ord(ch) < 32 for ch in candidate):
            raise InvalidUrl("url must not contain whitespace or control characters")
        try:
            parts = urlsplit(candidate)
            host = parts.hostname
        except ValueError as exc:
            raise InvalidUrl(f"url is malformed: {exc}") from exc
        if parts.scheme not in ("http", "https"):
            raise InvalidUrl("only http and https URLs can be shortened")
        if not host:
            raise InvalidUrl("url has no host")
        return candidate
