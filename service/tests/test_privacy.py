from datetime import datetime, timedelta, timezone

import pytest

from urlshortener.db import Database
from urlshortener.privacy import VisitorHasher, referrer_host
from urlshortener.repository import LinkRepository

NOON = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def test_same_visitor_same_day_gets_the_same_id():
    """FR-23: repeat visits by one visitor on one day count as a single unique visitor."""
    hasher = VisitorHasher()
    first = hasher.visitor_id("203.0.113.7", "Firefox", NOON)
    assert hasher.visitor_id("203.0.113.7", "Firefox", NOON + timedelta(hours=5)) == first
    assert hasher.visitor_id("203.0.113.8", "Firefox", NOON) != first
    assert hasher.visitor_id("203.0.113.7", "Chrome", NOON) != first


def test_ids_cannot_be_linked_across_days():
    """NFR-20: the key rotates daily, so a visitor cannot be followed from one day to the next."""
    hasher = VisitorHasher()
    today = hasher.visitor_id("203.0.113.7", "Firefox", NOON)
    assert hasher.visitor_id("203.0.113.7", "Firefox", NOON + timedelta(days=1)) != today


def test_ids_cannot_be_recomputed_by_another_process():
    """NFR-20: without the in-memory key an address cannot be turned into its id (no enumeration)."""
    a, b = VisitorHasher(), VisitorHasher()
    assert a.visitor_id("203.0.113.7", "Firefox", NOON) != b.visitor_id("203.0.113.7", "Firefox", NOON)


def test_id_reveals_nothing_about_its_input():
    """NFR-20: the identifier is a fixed-length hex string, whatever the input looks like."""
    visitor = VisitorHasher().visitor_id("203.0.113.7", "Firefox édition", NOON)
    assert len(visitor) == 32
    assert set(visitor) <= set("0123456789abcdef")
    assert len(VisitorHasher().visitor_id("x" * 10_000, "", NOON)) == 32


def test_key_is_requested_once_per_day():
    """NFR-21: hashing is O(1); a new key is generated only when the UTC day changes."""
    calls = []

    def key_factory() -> bytes:
        calls.append(1)
        return b"k" * 32

    hasher = VisitorHasher(key_factory)
    for hours in (0, 1, 11):
        hasher.visitor_id("203.0.113.7", "Firefox", NOON + timedelta(hours=hours))
    assert len(calls) == 1
    hasher.visitor_id("203.0.113.7", "Firefox", NOON + timedelta(hours=12))  # past midnight UTC
    assert len(calls) == 2


@pytest.mark.parametrize(
    "referrer, expected",
    [
        ("https://News.Example/article/42?utm_user=jane@corp.example", "news.example"),
        ("http://blog.example:8080/", "blog.example"),
        ("  https://spaced.example/x  ", "spaced.example"),
        ("not a url", None),
        ("android-app://com.example.app", "com.example.app"),
        ("http://[::1", None),
        ("", None),
        (None, None),
    ],
)
def test_referrer_is_reduced_to_its_host(referrer, expected):
    """NFR-20: only the referring host is kept; paths and query strings are dropped."""
    assert referrer_host(referrer) == expected


def test_repository_counts_unique_visitors_and_referrers(tmp_path):
    """FR-23: the store aggregates distinct daily visitors and clicks per referrer."""
    database = Database(str(tmp_path / "links.db"))
    database.migrate()
    repo = LinkRepository(database)
    try:
        repo.insert("abc1234", "https://example.com", NOON)
        repo.record_click("abc1234", NOON, "news.example", "visitor-a")
        repo.record_click("abc1234", NOON + timedelta(hours=1), "news.example", "visitor-a")
        repo.record_click("abc1234", NOON + timedelta(hours=2), "blog.example", "visitor-b")
        repo.record_click("abc1234", NOON + timedelta(days=1), None, "visitor-c")
        repo.record_click("abc1234", NOON + timedelta(days=1), None, None)  # e.g. recorded before migration 3

        since = NOON - timedelta(days=1)
        assert repo.unique_visitors_by_day("abc1234", since) == {"2026-03-01": 2, "2026-03-02": 1}
        assert repo.unique_visitors_by_day("abc1234", NOON + timedelta(hours=3)) == {"2026-03-02": 1}
        assert sorted(repo.referrer_counts("abc1234")) == [("blog.example", 1), ("news.example", 2)]
        assert repo.click_count("abc1234") == 5  # totals still include clicks without a visitor id
    finally:
        database.close()
