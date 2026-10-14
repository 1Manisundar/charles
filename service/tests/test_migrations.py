import sqlite3

import pytest

from urlshortener import db as db_module
from urlshortener.db import Database
from urlshortener.migrations import MIGRATIONS


def _database_at_version(path: str, version: int, monkeypatch) -> Database:
    """Build a database as an older release would have left it."""
    monkeypatch.setattr(db_module, "MIGRATIONS", [m for m in MIGRATIONS if m[0] <= version])
    database = Database(path)
    database.migrate()
    monkeypatch.setattr(db_module, "MIGRATIONS", MIGRATIONS)
    return database


def test_migration_versions_are_contiguous():
    """NFR-11: migrations form an unbroken, ordered sequence starting at 1."""
    assert [version for version, _ in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))


def test_upgrade_from_v1_preserves_existing_data(tmp_path, monkeypatch):
    """NFR-10 NFR-11: a populated v1 database upgrades in place without losing links or clicks."""
    path = str(tmp_path / "legacy.db")
    legacy = _database_at_version(path, 1, monkeypatch)
    with legacy.transaction() as conn:
        conn.execute(
            "INSERT INTO links (code, long_url, created_at) VALUES (?, ?, ?)",
            ("legacy1", "https://example.com/old", "2026-01-01T00:00:00.000000+00:00"),
        )
        conn.execute(
            "INSERT INTO clicks (code, clicked_at, referrer) VALUES (?, ?, ?)",
            ("legacy1", "2026-01-02T00:00:00.000000+00:00", None),
        )
    assert legacy.schema_version() == 1
    legacy.close()

    upgraded = Database(path)
    try:
        assert upgraded.migrate() == MIGRATIONS[-1][0]
        row = upgraded.query("SELECT * FROM links WHERE code = ?", ("legacy1",))[0]
        assert row["long_url"] == "https://example.com/old"
        assert row["is_active"] == 1
        assert row["expires_at"] is None  # existing links never expire
        assert row["is_custom"] == 0
        assert upgraded.query("SELECT COUNT(*) AS n FROM clicks")[0]["n"] == 1
    finally:
        upgraded.close()


def test_failed_migration_changes_nothing(tmp_path, monkeypatch):
    """NFR-11: a migration is all-or-nothing; a failure leaves the previous schema intact."""
    database = Database(str(tmp_path / "links.db"))
    database.migrate()
    before = database.schema_version()
    broken = (before + 1, "ALTER TABLE links ADD COLUMN scratch TEXT; THIS IS NOT SQL;")
    monkeypatch.setattr(db_module, "MIGRATIONS", [*MIGRATIONS, broken])
    try:
        with pytest.raises(sqlite3.Error):
            database.migrate()
        assert database.schema_version() == before
        columns = {row["name"] for row in database.query("PRAGMA table_info(links)")}
        assert "scratch" not in columns  # the first statement was rolled back with the second
    finally:
        database.close()
