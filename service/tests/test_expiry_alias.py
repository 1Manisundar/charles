from datetime import datetime, timedelta, timezone

import pytest

from urlshortener.codec import InvalidAlias
from urlshortener.config import Settings
from urlshortener.db import Database
from urlshortener.repository import LinkRepository
from urlshortener.service import (
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    AliasTaken,
    InvalidExpiry,
    LinkGone,
    LinkService,
)

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return FakeClock(T0)


@pytest.fixture
def service(tmp_path, clock):
    database = Database(str(tmp_path / "links.db"))
    database.migrate()
    yield LinkService(LinkRepository(database), Settings(), clock=clock)
    database.close()


# ---------------------------------------------------------------- expiry


def test_ttl_sets_an_absolute_expiry(service):
    """FR-10: expires_in_seconds is converted to an absolute expires_at at creation."""
    link = service.create("https://example.com/campaign", expires_in_seconds=3600)
    assert link.expires_at == T0 + timedelta(hours=1)
    assert service.get(link.code).expires_at == T0 + timedelta(hours=1)  # persisted, not just returned


def test_link_redirects_until_just_before_expiry(service, clock):
    """FR-11: a link with a lifetime keeps working for all of it."""
    link = service.create("https://example.com/campaign", expires_in_seconds=3600)
    clock.now = T0 + timedelta(hours=1) - timedelta(microseconds=1)
    assert service.resolve(link.code) == "https://example.com/campaign"


def test_link_is_gone_at_the_exact_expiry_instant(service, clock):
    """FR-11: expiry is inclusive - at expires_at the link is already gone."""
    link = service.create("https://example.com/campaign", expires_in_seconds=3600)
    clock.now = T0 + timedelta(hours=1)
    with pytest.raises(LinkGone):
        service.resolve(link.code)


def test_expired_link_keeps_metadata_and_stats(service, clock):
    """FR-11: expiry stops redirects only; the link can still be inspected."""
    link = service.create("https://example.com/campaign", expires_in_seconds=60)
    service.resolve(link.code)
    clock.now = T0 + timedelta(days=30)

    with pytest.raises(LinkGone):
        service.resolve(link.code)
    assert service.get(link.code).is_active is True  # expired, not deleted
    assert service.stats(link.code).total_clicks == 1  # the refused visit was not counted


def test_links_without_ttl_never_expire(service, clock):
    """NFR-10: links created the old way behave exactly as before."""
    link = service.create("https://example.com/evergreen")
    assert link.expires_at is None and link.is_custom is False
    clock.now = T0 + timedelta(days=3650)
    assert service.resolve(link.code) == "https://example.com/evergreen"


@pytest.mark.parametrize("ttl", [MIN_TTL_SECONDS, MAX_TTL_SECONDS])
def test_ttl_limits_are_inclusive(service, ttl):
    """FR-10: the shortest and longest allowed lifetimes are accepted."""
    assert service.create("https://example.com", expires_in_seconds=ttl).expires_at == T0 + timedelta(seconds=ttl)


@pytest.mark.parametrize("ttl", [-1, 0, MIN_TTL_SECONDS - 1, MAX_TTL_SECONDS + 1])
def test_ttl_outside_the_limits_is_rejected(service, ttl):
    """FR-10: nonsensical or unbounded lifetimes are refused."""
    with pytest.raises(InvalidExpiry):
        service.create("https://example.com", expires_in_seconds=ttl)


# ---------------------------------------------------------------- custom aliases


def test_alias_becomes_the_code(service):
    """FR-12: a requested alias is used verbatim as the short code."""
    link = service.create("https://example.com/spring", alias="spring-sale")
    assert link.code == "spring-sale"
    assert link.is_custom is True
    assert service.resolve("spring-sale") == "https://example.com/spring"


def test_alias_can_be_combined_with_expiry(service, clock):
    """FR-10 FR-12: the two features compose."""
    service.create("https://example.com/spring", alias="spring-sale", expires_in_seconds=600)
    clock.now = T0 + timedelta(seconds=600)
    with pytest.raises(LinkGone):
        service.resolve("spring-sale")


def test_taken_alias_is_refused_not_substituted(service):
    """FR-13: a second request for the same alias fails; the first link is untouched."""
    service.create("https://example.com/first", alias="spring-sale")
    with pytest.raises(AliasTaken):
        service.create("https://example.com/second", alias="spring-sale")
    assert service.resolve("spring-sale") == "https://example.com/first"


def test_alias_of_a_deleted_link_stays_taken(service):
    """FR-13: codes are never reused, so an old link can never point somewhere new."""
    service.create("https://example.com/first", alias="spring-sale")
    service.delete("spring-sale")
    with pytest.raises(AliasTaken):
        service.create("https://example.com/second", alias="spring-sale")


@pytest.mark.parametrize("alias", ["ab", "docs", "API", "has space", "x/y", "-dash-first"])
def test_malformed_or_reserved_alias_is_rejected(service, alias):
    """FR-13: invalid and reserved aliases are refused before anything is stored."""
    with pytest.raises(InvalidAlias):
        service.create("https://example.com", alias=alias)


def test_invalid_url_wins_over_alias(service):
    """NFR-10: existing URL validation still applies when new fields are present."""
    with pytest.raises(ValueError, match="http and https"):
        service.create("ftp://example.com", alias="spring-sale")
    service.create("https://example.com", alias="spring-sale")  # the alias was not consumed by the failed attempt
