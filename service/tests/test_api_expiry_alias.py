from datetime import datetime, timedelta

import pytest

PROBLEM = "application/problem+json"


def test_create_with_alias(client, make_link):
    """FR-12: POST with an alias returns that alias as the code, and it redirects."""
    link = make_link("https://example.com/spring", alias="spring-sale")

    assert link["code"] == "spring-sale"
    assert link["short_url"] == "http://sho.rt/spring-sale"
    assert link["is_custom"] is True
    assert client.get("/spring-sale").headers["location"] == "https://example.com/spring"


def test_taken_alias_is_409(client, make_link):
    """FR-13: a taken alias yields 409 Conflict and leaves the first link in place."""
    make_link("https://example.com/first", alias="spring-sale")

    response = client.post("/api/v1/links", json={"url": "https://example.com/second", "alias": "spring-sale"})

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM
    assert client.get("/spring-sale").headers["location"] == "https://example.com/first"


@pytest.mark.parametrize("alias", ["ab", "docs", "healthz", "has space", "x/y", "a" * 33])
def test_invalid_alias_is_422(client, alias):
    """FR-13: malformed and reserved aliases are rejected with a problem document."""
    response = client.post("/api/v1/links", json={"url": "https://example.com", "alias": alias})
    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["title"] == "Invalid alias"


def test_reserved_routes_are_not_shadowed(client):
    """FR-13: service routes keep working whatever aliases are requested."""
    client.post("/api/v1/links", json={"url": "https://example.com", "alias": "healthz"})
    assert client.get("/healthz").json()["status"] == "ok"


def test_create_with_expiry(make_link):
    """FR-10: expires_in_seconds is reflected as an absolute expires_at."""
    link = make_link(expires_in_seconds=3600)
    lifetime = datetime.fromisoformat(link["expires_at"]) - datetime.fromisoformat(link["created_at"])
    assert lifetime == timedelta(hours=1)


@pytest.mark.parametrize("ttl", [0, 59, 31536001])
def test_out_of_range_expiry_is_422(client, ttl):
    """FR-10: lifetimes outside 60 seconds to 365 days are rejected."""
    response = client.post("/api/v1/links", json={"url": "https://example.com", "expires_in_seconds": ttl})
    assert response.status_code == 422
    assert response.json()["title"] == "Invalid expiry"


@pytest.mark.parametrize("extra", [{"alias": 123}, {"expires_in_seconds": "soon"}, {"expires_in_seconds": 1.5}])
def test_wrongly_typed_fields_are_422(client, extra):
    """FR-10 FR-12: type errors in the new fields are validation errors, not server errors."""
    response = client.post("/api/v1/links", json={"url": "https://example.com", **extra})
    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM


def test_expired_link_answers_410_but_stays_inspectable(client, app, make_link):
    """FR-11: after expiry the redirect is 410 Gone; metadata and stats still work."""
    link = make_link(expires_in_seconds=60)
    assert client.get(f"/{link['code']}").status_code == 307

    expiry = datetime.fromisoformat(link["expires_at"])
    app.state.link_service._clock = lambda: expiry  # jump to the expiry instant

    gone = client.get(f"/{link['code']}")
    assert gone.status_code == 410
    assert gone.headers["content-type"] == PROBLEM
    assert client.get(f"/api/v1/links/{link['code']}").json()["expires_at"] == link["expires_at"]
    assert client.get(f"/api/v1/links/{link['code']}/stats").json()["total_clicks"] == 1


def test_requests_without_new_fields_are_unchanged(make_link):
    """NFR-10: existing clients that send only a url get the same link as before."""
    link = make_link("https://example.com/plain")
    assert len(link["code"]) == 7
    assert link["expires_at"] is None
    assert link["is_custom"] is False
