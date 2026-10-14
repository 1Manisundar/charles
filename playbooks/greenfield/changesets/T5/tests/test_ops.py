from fastapi.testclient import TestClient

from urlshortener.service import CodeSpaceExhausted


def test_liveness(client):
    """NFR-1: /healthz answers without touching the database."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_schema_version(client, app):
    """NFR-1: /readyz confirms the database is reachable and migrated."""
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "schema_version": app.state.db.schema_version()}


def test_readiness_fails_when_database_is_down(client, app):
    """NFR-1: an unusable database takes the instance out of rotation."""
    app.state.db.close()
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"


def test_request_id_is_generated(client):
    """NFR-3: every response carries a request id for log correlation."""
    first, second = client.get("/healthz"), client.get("/healthz")
    assert len(first.headers["x-request-id"]) == 32
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_safe_request_id_is_echoed(client):
    """NFR-3: a well-formed caller id is propagated."""
    response = client.get("/healthz", headers={"X-Request-ID": "trace-123.abc"})
    assert response.headers["x-request-id"] == "trace-123.abc"


def test_unsafe_request_id_is_replaced(client):
    """NFR-3 NFR-2: ids that could forge log lines are not echoed."""
    response = client.get("/healthz", headers={"X-Request-ID": "x" * 200})
    assert response.headers["x-request-id"] != "x" * 200


def test_unexpected_errors_do_not_leak_internals(app, monkeypatch):
    """NFR-1 NFR-2: an unhandled error becomes a generic 500 problem."""

    def explode(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(app.state.link_service, "get", explode)
    response = TestClient(app, raise_server_exceptions=False).get("/api/v1/links/abc1234")

    assert response.status_code == 500
    assert response.json()["title"] == "Internal error"
    assert "secret" not in response.text


def test_code_exhaustion_is_a_retryable_503(client, app, monkeypatch):
    """NFR-1: running out of codes is reported as temporary, not as a server bug."""

    def exhausted(*args, **kwargs):
        raise CodeSpaceExhausted("no free code")

    monkeypatch.setattr(app.state.link_service, "create", exhausted)
    response = client.post("/api/v1/links", json={"url": "https://example.com"})
    assert response.status_code == 503


def test_service_is_self_describing(client, app):
    """NFR-4: the root path points at the documentation, and the published schema lists every endpoint."""
    index = client.get("/").json()
    assert index == {
        "service": "URL Shortener",
        "version": app.version,
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/healthz",
    }
    assert client.get(index["docs"]).status_code == 200

    paths = client.get(index["openapi"]).json()["paths"]
    assert {"/", "/api/v1/links", "/api/v1/links/{code}", "/api/v1/links/{code}/stats", "/{code}", "/healthz", "/readyz"} <= set(paths)


def test_database_is_closed_on_shutdown(app):
    """NFR-1: the connection is released when the application stops."""
    with TestClient(app):
        assert app.state.db.ping() is True
    assert app.state.db.ping() is False
