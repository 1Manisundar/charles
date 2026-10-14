from datetime import datetime, timedelta, timezone

import pytest

from urlshortener.db import Database
from urlshortener.repository import DuplicateCode, LinkRepository

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "links.db"))
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def repo(db):
    return LinkRepository(db)


def test_migrate_is_idempotent(db):
    """NFR-1: migrations run on every start-up without re-applying."""
    version = db.schema_version()
    assert version >= 1
    assert db.migrate() == version


def test_insert_then_get_round_trips(repo):
    """FR-3: a stored link is returned exactly as written."""
    repo.insert("abc1234", "https://example.com/a?b=c", T0)

    link = repo.get("abc1234")

    assert link.long_url == "https://example.com/a?b=c"
    assert link.created_at == T0
    assert link.is_active is True
    assert repo.get("missing0") is None


def test_duplicate_code_is_reported(repo):
    """FR-1: uniqueness of codes is enforced by the database."""
    repo.insert("abc1234", "https://example.com", T0)
    with pytest.raises(DuplicateCode):
        repo.insert("abc1234", "https://other.example", T0)


def test_deactivate_is_a_soft_delete(repo):
    """FR-4: deleting keeps the row (and its analytics) but marks it inactive."""
    repo.insert("abc1234", "https://example.com", T0)

    assert repo.deactivate("abc1234") is True
    assert repo.deactivate("abc1234") is False  # already inactive
    assert repo.get("abc1234").is_active is False


def test_click_analytics(repo):
    """FR-5: clicks are counted in total and per day, newest timestamp retained."""
    repo.insert("abc1234", "https://example.com", T0)
    repo.record_click("abc1234", T0, "https://news.example/post")
    repo.record_click("abc1234", T0 + timedelta(hours=1), None)
    repo.record_click("abc1234", T0 + timedelta(days=1), None)

    assert repo.click_count("abc1234") == 3
    assert repo.last_click("abc1234") == T0 + timedelta(days=1)
    assert repo.clicks_by_day("abc1234", T0 - timedelta(days=1)) == [("2026-03-01", 2), ("2026-03-02", 1)]
    assert repo.clicks_by_day("abc1234", T0 + timedelta(hours=2)) == [("2026-03-02", 1)]
    assert repo.last_click("missing0") is None


def test_data_survives_a_restart(tmp_path):
    """NFR-1: links persist across process restarts."""
    path = str(tmp_path / "links.db")
    first = Database(path)
    first.migrate()
    LinkRepository(first).insert("abc1234", "https://example.com", T0)
    first.close()

    second = Database(path)
    second.migrate()
    try:
        assert LinkRepository(second).get("abc1234").long_url == "https://example.com"
    finally:
        second.close()


def test_hostile_input_is_stored_as_data_not_executed(repo, db):
    """NFR-2: parameterised SQL means injection attempts are inert."""
    hostile = "https://example.com/'; DROP TABLE links; --"
    repo.insert("abc1234", hostile, T0)

    assert repo.get("abc1234").long_url == hostile
    assert repo.get("x' OR '1'='1") is None
    assert db.query("SELECT COUNT(*) AS n FROM links")[0]["n"] == 1


def test_failed_transaction_rolls_back(db, repo):
    """NFR-1: a failing write leaves no partial state behind."""
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO links (code, long_url, created_at) VALUES (?, ?, ?)",
                ("abc1234", "https://example.com", T0.isoformat()),
            )
            raise RuntimeError("boom")
    assert repo.get("abc1234") is None


def test_ping_reports_a_closed_database(tmp_path):
    """NFR-1: readiness can detect an unusable database."""
    database = Database(str(tmp_path / "links.db"))
    assert database.ping() is True
    database.close()
    assert database.ping() is False
