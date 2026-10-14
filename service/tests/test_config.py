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


def test_hardening_defaults_are_safe():
    """NFR-22: with no configuration, limits are on and forwarded headers are not trusted."""
    settings = Settings()
    assert settings.rate_limit_create_per_minute == 30
    assert settings.rate_limit_redirect_per_minute == 600
    assert settings.trust_forwarded_for is False
    assert settings.blocked_domains == ()


def test_hardening_settings_from_environment(monkeypatch):
    """FR-20 FR-22: limits, proxy trust and the domain blocklist are configurable."""
    monkeypatch.setenv("URLSHORTENER_RATE_LIMIT_CREATE_PER_MINUTE", "5")
    monkeypatch.setenv("URLSHORTENER_RATE_LIMIT_REDIRECT_PER_MINUTE", "0")
    monkeypatch.setenv("URLSHORTENER_TRUST_FORWARDED_FOR", "TRUE")
    monkeypatch.setenv("URLSHORTENER_BLOCKED_DOMAINS", " Evil.example , ,phish.test ")

    settings = Settings.from_env()

    assert settings.rate_limit_create_per_minute == 5
    assert settings.rate_limit_redirect_per_minute == 0
    assert settings.trust_forwarded_for is True
    assert settings.blocked_domains == ("evil.example", "phish.test")
