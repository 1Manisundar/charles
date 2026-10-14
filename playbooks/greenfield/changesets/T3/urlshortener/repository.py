"""Data access for links and click events. All SQL lives here and is always parameterised."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import Database


@dataclass(frozen=True)
class Link:
    code: str
    long_url: str
    created_at: datetime
    is_active: bool


class DuplicateCode(Exception):
    """The short code is already taken."""


def _ts(moment: datetime) -> str:
    """Fixed-width UTC ISO-8601, so that string order equals time order."""
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _to_link(row: sqlite3.Row) -> Link:
    return Link(
        code=row["code"],
        long_url=row["long_url"],
        created_at=datetime.fromisoformat(row["created_at"]),
        is_active=bool(row["is_active"]),
    )


class LinkRepository:
    def __init__(self, db: Database):
        self._db = db

    def insert(self, code: str, long_url: str, created_at: datetime) -> Link:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO links (code, long_url, created_at) VALUES (?, ?, ?)",
                    (code, long_url, _ts(created_at)),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateCode(code) from exc
        link = self.get(code)
        assert link is not None
        return link

    def get(self, code: str) -> Link | None:
        rows = self._db.query("SELECT * FROM links WHERE code = ?", (code,))
        return _to_link(rows[0]) if rows else None

    def deactivate(self, code: str) -> bool:
        """Soft-delete. Returns False if the link was already inactive or unknown."""
        with self._db.transaction() as conn:
            cursor = conn.execute("UPDATE links SET is_active = 0 WHERE code = ? AND is_active = 1", (code,))
        return cursor.rowcount == 1

    def record_click(self, code: str, clicked_at: datetime, referrer: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO clicks (code, clicked_at, referrer) VALUES (?, ?, ?)",
                (code, _ts(clicked_at), referrer),
            )

    def click_count(self, code: str) -> int:
        return self._db.query("SELECT COUNT(*) AS n FROM clicks WHERE code = ?", (code,))[0]["n"]

    def last_click(self, code: str) -> datetime | None:
        value = self._db.query("SELECT MAX(clicked_at) AS at FROM clicks WHERE code = ?", (code,))[0]["at"]
        return datetime.fromisoformat(value) if value else None

    def clicks_by_day(self, code: str, since: datetime) -> list[tuple[str, int]]:
        rows = self._db.query(
            "SELECT substr(clicked_at, 1, 10) AS day, COUNT(*) AS n FROM clicks "
            "WHERE code = ? AND clicked_at >= ? GROUP BY day ORDER BY day",
            (code, _ts(since)),
        )
        return [(row["day"], row["n"]) for row in rows]
