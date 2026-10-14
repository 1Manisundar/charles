import pytest
from fastapi.testclient import TestClient

from urlshortener.config import Settings
from urlshortener.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "api.db"), base_url="http://sho.rt")


@pytest.fixture
def app(settings):
    application = create_app(settings)
    yield application
    application.state.db.close()


@pytest.fixture
def client(app):
    # Redirects are the product; tests must see them rather than follow them.
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def make_link(client):
    def _make(url: str = "https://example.com/landing", **extra) -> dict:
        response = client.post("/api/v1/links", json={"url": url, **extra})
        assert response.status_code == 201, response.text
        return response.json()

    return _make
