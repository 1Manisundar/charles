from datetime import datetime, timedelta, timezone

import pytest

from urlshortener.config import Settings
from urlshortener.db import Database
from urlshortener.repository import LinkRepository
from urlshortener.service import (
    MAX_CODE_ATTEMPTS,
    CodeSpaceExhausted,
    InvalidUrl,
    LinkGone,
    LinkNotFound,
    LinkService,
)


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
def repo(tmp_path):
    database = Database(str(tmp_path / "links.db"))
    database.migrate()
    yield LinkRepository(database)
    database.close()


@pytest.fixture
def service(repo, clock):
    return LinkService(repo, Settings(stats_days=7), clock=clock)


def test_create_returns_a_resolvable_link(service):
    """FR-1 FR-2: a created link resolves to the URL it was created with."""
    link = service.create("  https://example.com/some/long/path?q=1  ")

    assert len(link.code) == 7
    assert link.long_url == "https://example.com/some/long/path?q=1"  # surrounding whitespace trimmed
    assert service.resolve(link.code) == link.long_url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "example.com/no-scheme",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "https://",
        "https://exa mple.com",
        "https://example.com/\x00",
        "http://[::1",
        "https://example.com/" + "a" * 2048,
    ],
)
def test_unusable_urls_are_rejected(service, url):
    """FR-1 NFR-2: only well-formed http(s) URLs within the length limit are accepted."""
    with pytest.raises(InvalidUrl):
        service.create(url)


def test_identical_urls_get_distinct_codes(service):
    """FR-1: shortening is not deduplicated, so each caller owns its own link."""
    first, second = service.create("https://example.com"), service.create("https://example.com")
    assert first.code != second.code


def test_code_collisions_are_retried(repo, clock):
    """NFR-1: a collision is retried with a fresh code instead of failing the request."""
    codes = iter(["taken00", "taken00", "fresh00"])
    service = LinkService(repo, Settings(), clock=clock, code_factory=lambda length: next(codes))
    service.create("https://example.com/1")

    assert service.create("https://example.com/2").code == "fresh00"


def test_retry_budget_is_bounded(repo, clock):
    """NFR-1: persistent collisions fail fast instead of looping forever."""
    attempts = []

    def always_the_same(length: int) -> str:
        attempts.append(length)
        return "taken00"

    service = LinkService(repo, Settings(), clock=clock, code_factory=always_the_same)
    service.create("https://example.com/1")
    attempts.clear()

    with pytest.raises(CodeSpaceExhausted):
        service.create("https://example.com/2")
    assert len(attempts) == MAX_CODE_ATTEMPTS


@pytest.mark.parametrize("code", ["nope000", "", "../../etc/passwd", "a b"])
def test_unknown_or_malformed_codes_are_not_found(service, code):
    """FR-2 FR-3: unknown and malformed codes behave identically."""
    with pytest.raises(LinkNotFound):
        service.get(code)
    with pytest.raises(LinkNotFound):
        service.resolve(code)


def test_deleted_links_are_gone_but_still_described(service):
    """FR-4: a deleted link no longer redirects; its metadata and stats remain."""
    link = service.create("https://example.com")
    service.resolve(link.code)

    service.delete(link.code)
    service.delete(link.code)  # idempotent

    with pytest.raises(LinkGone):
        service.resolve(link.code)
    assert service.get(link.code).is_active is False
    assert service.stats(link.code).total_clicks == 1  # the refused visit was not counted
    with pytest.raises(LinkNotFound):
        service.delete("nope000")


def test_stats_count_clicks_per_day_within_the_window(service, clock):
    """FR-5: totals cover all time; the daily breakdown covers the configured window."""
    link = service.create("https://example.com")
    service.resolve(link.code, referrer="https://news.example/post")
    clock.advance(days=10)
    service.resolve(link.code)
    service.resolve(link.code)

    stats = service.stats(link.code)

    assert stats.total_clicks == 3
    assert stats.last_clicked_at == clock.now
    assert stats.clicks_by_day == [("2026-03-11", 2)]  # the click 10 days ago is outside the 7-day window


def test_stats_for_a_link_without_clicks(service):
    """FR-5: a new link reports zero clicks rather than an error."""
    stats = service.stats(service.create("https://example.com").code)
    assert (stats.total_clicks, stats.last_clicked_at, stats.clicks_by_day) == (0, None, [])


def test_only_the_referrer_host_is_stored(service, repo):
    """NFR-2 NFR-20: a hostile Referer header can neither bloat the table nor smuggle personal data into it."""
    link = service.create("https://example.com")
    service.resolve(link.code, referrer="https://ref.example/" + "x" * 5000 + "?email=jane@corp.example")

    stored = repo._db.query("SELECT referrer FROM clicks WHERE code = ?", (link.code,))[0]["referrer"]
    assert stored == "ref.example"


def test_unsafe_destinations_are_invalid_urls(service):
    """FR-22: the safety check is part of URL validation, so no unsafe link is ever stored."""
    for url in ("http://127.0.0.1/admin", "https://user:pw@example.com/", "http://localhost:8000/abc1234"):
        with pytest.raises(InvalidUrl):
            service.create(url)


def test_referrers_recorded_by_older_releases_are_folded_into_hosts(service, repo):
    """NFR-22 FR-23: full referrer URLs stored before this change still aggregate correctly."""
    link = service.create("https://example.com")
    now = service._clock()
    repo.record_click(link.code, now, "https://news.example/old/full/url?x=1")  # as release 1.x stored it
    service.resolve(link.code, referrer="https://news.example/another/page")
    service.resolve(link.code, referrer="https://blog.example/")

    assert service.stats(link.code).top_referrers == [("news.example", 2), ("blog.example", 1)]
