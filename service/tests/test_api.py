from datetime import datetime, timezone

import pytest

PROBLEM = "application/problem+json"


def test_create_link(client):
    """FR-1: POST /api/v1/links returns 201 with the short link."""
    response = client.post("/api/v1/links", json={"url": "https://example.com/landing"})

    assert response.status_code == 201
    body = response.json()
    assert len(body["code"]) == 7
    assert body["short_url"] == f"http://sho.rt/{body['code']}"
    assert body["long_url"] == "https://example.com/landing"
    assert body["is_active"] is True
    assert body["created_at"]


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"url": "ftp://example.com"}, "http and https"),
        ({"url": "not a url"}, "whitespace"),
        ({"url": ""}, "empty"),
        ({}, "url"),
        ({"url": 42}, "url"),
    ],
)
def test_create_link_rejects_bad_input(client, payload, fragment):
    """FR-1 NFR-1: invalid input yields a 422 problem document, never a 500."""
    response = client.post("/api/v1/links", json=payload)

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    problem = response.json()
    assert problem["status"] == 422
    assert fragment in problem["detail"]


def test_follow_link_redirects(client, make_link):
    """FR-2: GET /{code} answers 307 to the destination and is never cached."""
    link = make_link("https://example.com/landing?utm=1")

    response = client.get(f"/{link['code']}")

    assert response.status_code == 307
    assert response.headers["location"] == "https://example.com/landing?utm=1"
    assert response.headers["cache-control"] == "no-store"


def test_unknown_code_is_404(client):
    """FR-2 FR-3: unknown codes give a 404 problem on every endpoint."""
    for path in ("/nope000", "/api/v1/links/nope000", "/api/v1/links/nope000/stats"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"] == PROBLEM
    assert client.delete("/api/v1/links/nope000").status_code == 404


def test_get_link_metadata(client, make_link):
    """FR-3: GET /api/v1/links/{code} returns what was created."""
    link = make_link()
    response = client.get(f"/api/v1/links/{link['code']}")
    assert response.status_code == 200
    assert response.json() == link


def test_delete_link(client, make_link):
    """FR-4: a deleted link answers 410 Gone; deleting again is still 204."""
    link = make_link()

    assert client.delete(f"/api/v1/links/{link['code']}").status_code == 204
    assert client.delete(f"/api/v1/links/{link['code']}").status_code == 204

    gone = client.get(f"/{link['code']}")
    assert gone.status_code == 410
    assert gone.headers["content-type"] == PROBLEM
    assert client.get(f"/api/v1/links/{link['code']}").json()["is_active"] is False


def test_stats_reflect_redirects(client, app, make_link):
    """FR-5: each redirect is counted and reported by the stats endpoint."""
    # Pin the clock so the three visits cannot straddle midnight and split across two days.
    app.state.link_service._clock = lambda: datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    link = make_link()
    for _ in range(3):
        client.get(f"/{link['code']}", headers={"Referer": "https://news.example/post"})

    stats = client.get(f"/api/v1/links/{link['code']}/stats").json()

    assert stats["code"] == link["code"]
    assert stats["total_clicks"] == 3
    assert stats["last_clicked_at"] is not None
    assert [day["clicks"] for day in stats["clicks_by_day"]] == [3]


def test_stats_of_unused_link(client, make_link):
    """FR-5: a link nobody followed reports zero clicks."""
    stats = client.get(f"/api/v1/links/{make_link()['code']}/stats").json()
    assert stats["total_clicks"] == 0
    assert stats["last_clicked_at"] is None
    assert stats["clicks_by_day"] == []
