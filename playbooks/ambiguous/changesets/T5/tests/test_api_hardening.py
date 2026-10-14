from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from urlshortener.config import Settings
from urlshortener.main import create_app

PROBLEM = "application/problem+json"


@pytest.fixture
def make_client(tmp_path):
    """Build a client for an app with specific hardening settings."""
    apps = []

    def _make(**overrides) -> TestClient:
        settings = Settings(db_path=str(tmp_path / f"app{len(apps)}.db"), base_url="http://sho.rt", **overrides)
        app = create_app(settings)
        apps.append(app)
        return TestClient(app, follow_redirects=False)

    yield _make
    for app in apps:
        app.state.db.close()


def _create(client, url="https://example.com/landing", **headers):
    return client.post("/api/v1/links", json={"url": url}, headers=headers)


# ---------------------------------------------------------------- rate limiting


def test_link_creation_is_rate_limited(make_client):
    """FR-20: beyond the per-minute budget, creation answers 429 with Retry-After."""
    client = make_client(rate_limit_create_per_minute=3)

    assert [_create(client).status_code for _ in range(3)] == [201, 201, 201]
    throttled = _create(client)

    assert throttled.status_code == 429
    assert throttled.headers["content-type"] == PROBLEM
    assert int(throttled.headers["retry-after"]) >= 1
    assert throttled.json()["title"] == "Too many requests"


def test_redirects_have_their_own_budget(make_client):
    """FR-21: redirects are limited separately, and probes and metadata are never limited."""
    client = make_client(rate_limit_create_per_minute=100, rate_limit_redirect_per_minute=2)
    code = _create(client).json()["code"]

    assert [client.get(f"/{code}").status_code for _ in range(3)] == [307, 307, 429]
    assert _create(client).status_code == 201  # the creation budget is untouched
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get(f"/api/v1/links/{code}/stats").json()["total_clicks"] == 2  # the throttled visit was not counted


def test_limits_are_per_client_behind_a_trusted_proxy(make_client):
    """FR-20: with a trusted proxy, each forwarded client address gets its own budget."""
    client = make_client(rate_limit_create_per_minute=1, trust_forwarded_for=True)

    assert _create(client, **{"X-Forwarded-For": "198.51.100.1"}).status_code == 201
    assert _create(client, **{"X-Forwarded-For": "198.51.100.1"}).status_code == 429
    assert _create(client, **{"X-Forwarded-For": "198.51.100.2"}).status_code == 201
    # Only the entry added by our proxy (the last one) counts; a forged prefix changes nothing.
    assert _create(client, **{"X-Forwarded-For": "10.9.9.9, 198.51.100.2"}).status_code == 429


def test_forwarded_header_is_ignored_unless_trusted(make_client):
    """FR-20 NFR-20: by default a client cannot escape its limit by inventing X-Forwarded-For."""
    client = make_client(rate_limit_create_per_minute=1)

    assert _create(client, **{"X-Forwarded-For": "198.51.100.1"}).status_code == 201
    assert _create(client, **{"X-Forwarded-For": "198.51.100.2"}).status_code == 429


def test_limiter_can_be_disabled(make_client):
    """FR-20: a limit of 0 turns throttling off."""
    client = make_client(rate_limit_create_per_minute=0)
    assert {_create(client).status_code for _ in range(50)} == {201}


# ---------------------------------------------------------------- destination safety


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8080/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://2130706433/",
        "http://printer.local/",
        "https://user:pw@example.com/",
        "http://sho.rt/abc1234",
        "https://evil.example/x",
        "https://login.evil.example/",
    ],
)
def test_unsafe_destinations_are_422(make_client, url):
    """FR-22: internal, disguised, self-referencing and blocked destinations are refused."""
    client = make_client(blocked_domains=("evil.example",))

    response = _create(client, url)

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["title"] == "Unsafe destination"


@pytest.mark.parametrize("url", ["https://example.com/", "http://8.8.8.8/", "https://notevil.example/"])
def test_public_destinations_are_still_accepted(make_client, url):
    """NFR-22: the safety checks do not reject ordinary links."""
    assert _create(make_client(blocked_domains=("evil.example",)), url).status_code == 201


# ---------------------------------------------------------------- privacy-preserving analytics


def test_stats_report_unique_visitors_and_top_referrers(make_client):
    """FR-23: stats distinguish clicks from unique visitors and rank referring hosts."""
    client = make_client()
    # Pin the clock so the visits cannot straddle midnight and split across two days.
    client.app.state.link_service._clock = lambda: datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    code = _create(client).json()["code"]
    for _ in range(3):
        client.get(f"/{code}", headers={"User-Agent": "Firefox", "Referer": "https://news.example/a?id=1"})
    for _ in range(2):
        client.get(f"/{code}", headers={"User-Agent": "Chrome", "Referer": "https://blog.example/post"})

    stats = client.get(f"/api/v1/links/{code}/stats").json()

    assert stats["total_clicks"] == 5
    assert [(day["clicks"], day["unique_visitors"]) for day in stats["clicks_by_day"]] == [(5, 2)]
    assert stats["top_referrers"] == [{"host": "news.example", "clicks": 3}, {"host": "blog.example", "clicks": 2}]


def test_no_personal_data_reaches_the_database(make_client):
    """NFR-20: neither the address, the user agent nor the referrer path is persisted."""
    client = make_client(trust_forwarded_for=True)
    code = _create(client).json()["code"]
    client.get(
        f"/{code}",
        headers={
            "X-Forwarded-For": "203.0.113.77",
            "User-Agent": "TrackMeBrowser/9.9",
            "Referer": "https://mail.example/inbox?user=jane.doe@corp.example",
        },
    )

    rows = client.app.state.db.query("SELECT * FROM clicks")
    stored = " ".join(str(value) for row in rows for value in tuple(row))

    assert len(rows) == 1
    assert "203.0.113.77" not in stored
    assert "TrackMeBrowser" not in stored
    assert "jane.doe" not in stored and "inbox" not in stored
    assert rows[0]["referrer"] == "mail.example"
    assert len(rows[0]["visitor_hash"]) == 32


def test_existing_stats_fields_are_unchanged(make_client):
    """NFR-22: the new analytics fields are additive; existing consumers keep working."""
    client = make_client()
    code = _create(client).json()["code"]
    client.get(f"/{code}")

    stats = client.get(f"/api/v1/links/{code}/stats").json()

    assert {"code", "total_clicks", "last_clicked_at", "clicks_by_day"} <= set(stats)
    assert {"date", "clicks"} <= set(stats["clicks_by_day"][0])
