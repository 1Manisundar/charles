"""Business rules for short links. No HTTP and no SQL in this module."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit

from .codec import generate_code, is_valid_code, validate_alias
from .config import Settings
from .privacy import VisitorHasher, referrer_host
from .repository import DuplicateCode, Link, LinkRepository
from .safety import UnsafeUrl, check_destination

MAX_CODE_ATTEMPTS = 5
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 365 * 24 * 3600
TOP_REFERRERS = 5


class LinkNotFound(LookupError):
    """No link exists for the code."""


class LinkGone(LookupError):
    """The link exists but may no longer be followed."""


class InvalidUrl(ValueError):
    """The submitted URL cannot be shortened."""


class UnsafeDestination(InvalidUrl):
    """The URL is well-formed but points somewhere visitors must not be sent."""


class CodeSpaceExhausted(RuntimeError):
    """No free short code was found within the retry budget."""


class AliasTaken(Exception):
    """The requested alias already identifies another link."""


class InvalidExpiry(ValueError):
    """The requested lifetime is outside the allowed range."""


@dataclass(frozen=True)
class LinkStats:
    code: str
    total_clicks: int
    last_clicked_at: datetime | None
    clicks_by_day: list[tuple[str, int]]
    unique_visitors_by_day: dict[str, int] = field(default_factory=dict)
    top_referrers: list[tuple[str, int]] = field(default_factory=list)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_expired(link: Link, now: datetime) -> bool:
    """A link is expired from its expiry instant onwards, the instant itself included."""
    return link.expires_at is not None and now >= link.expires_at


class LinkService:
    def __init__(
        self,
        repository: LinkRepository,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
        code_factory: Callable[[int], str] = generate_code,
        visitor_hasher: VisitorHasher | None = None,
    ):
        self._repository = repository
        self._settings = settings
        self._clock = clock
        self._code_factory = code_factory
        self._visitor_hasher = visitor_hasher or VisitorHasher()
        self._own_host = urlsplit(settings.base_url).hostname

    def create(self, url: str, alias: str | None = None, expires_in_seconds: int | None = None) -> Link:
        long_url = self._validate_url(url)
        now = self._clock()
        expires_at = self._expiry(now, expires_in_seconds)
        if alias is not None:
            validate_alias(alias)
            try:
                return self._repository.insert(alias, long_url, now, expires_at=expires_at, is_custom=True)
            except DuplicateCode:
                # Never retry or substitute: the caller asked for exactly this code.
                raise AliasTaken(alias) from None
        # Random codes can collide; the database decides, and we retry a bounded number of times.
        for _ in range(MAX_CODE_ATTEMPTS):
            code = self._code_factory(self._settings.code_length)
            try:
                return self._repository.insert(code, long_url, now, expires_at=expires_at)
            except DuplicateCode:
                continue
        raise CodeSpaceExhausted(f"no free code after {MAX_CODE_ATTEMPTS} attempts")

    def get(self, code: str) -> Link:
        link = self._repository.get(code) if is_valid_code(code) else None
        if link is None:
            raise LinkNotFound(code)
        return link

    def resolve(
        self,
        code: str,
        referrer: str | None = None,
        client_address: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        """Return the destination for a visit and record the click.

        Nothing that identifies the visitor is stored: address and user agent
        become an opaque daily identifier, and the referrer is cut down to its host.
        """
        link = self.get(code)
        now = self._clock()
        if not link.is_active or is_expired(link, now):
            raise LinkGone(code)
        visitor = self._visitor_hasher.visitor_id(client_address, user_agent or "", now) if client_address else None
        self._repository.record_click(code, now, referrer_host(referrer), visitor)
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
            unique_visitors_by_day=self._repository.unique_visitors_by_day(code, since),
            top_referrers=self._top_referrers(code),
        )

    def _top_referrers(self, code: str) -> list[tuple[str, int]]:
        # Clicks recorded by earlier releases hold full referrer URLs; fold everything down to hosts.
        totals: dict[str, int] = {}
        for referrer, count in self._repository.referrer_counts(code):
            host = referrer_host(referrer) or referrer
            totals[host] = totals.get(host, 0) + count
        return sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:TOP_REFERRERS]

    @staticmethod
    def _expiry(now: datetime, expires_in_seconds: int | None) -> datetime | None:
        if expires_in_seconds is None:
            return None
        if not MIN_TTL_SECONDS <= expires_in_seconds <= MAX_TTL_SECONDS:
            raise InvalidExpiry(f"expires_in_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}")
        return now + timedelta(seconds=expires_in_seconds)

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
        try:
            check_destination(candidate, self._own_host, self._settings.blocked_domains)
        except UnsafeUrl as exc:
            raise UnsafeDestination(str(exc)) from None
        return candidate
