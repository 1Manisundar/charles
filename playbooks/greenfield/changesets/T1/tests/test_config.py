from urlshortener.config import Settings


def test_defaults_are_usable_without_any_environment():
    """NFR-4: the service starts with zero configuration."""
    settings = Settings()
    assert settings.db_path == "urlshortener.db"
    assert settings.code_length == 7
    assert settings.max_url_length == 2048


def test_environment_overrides_defaults(monkeypatch):
    """NFR-4: every setting is configurable through URLSHORTENER_* variables."""
    monkeypatch.setenv("URLSHORTENER_DB_PATH", "/data/links.db")
    monkeypatch.setenv("URLSHORTENER_BASE_URL", "https://sho.rt/")
    monkeypatch.setenv("URLSHORTENER_CODE_LENGTH", "9")
    monkeypatch.setenv("URLSHORTENER_STATS_DAYS", "7")

    settings = Settings.from_env()

    assert settings.db_path == "/data/links.db"
    assert settings.base_url == "https://sho.rt"  # trailing slash removed
    assert settings.code_length == 9
    assert settings.stats_days == 7
    assert settings.max_url_length == 2048  # untouched settings keep their default
