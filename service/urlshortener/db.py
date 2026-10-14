"""SQLite access: one serialised connection, WAL journal, versioned migrations."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Sequence

from .migrations import MIGRATIONS


class Database:
    """A single connection guarded by a lock.

    SQLite allows one writer at a time anyway; serialising access in-process
    keeps the behaviour simple and correct. The repository is the only caller,
    so swapping this for a pooled client/server database touches two modules.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        # isolation_level=None: we issue BEGIN/COMMIT ourselves, so DDL is transactional too.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 5000")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def migrate(self) -> int:
        """Apply pending migrations; safe to call on every start-up."""
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, script in MIGRATIONS:
                if version in applied:
                    continue
                for statement in (part.strip() for part in script.split(";")):
                    if statement:
                        conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                    (version,),
                )
        return self.schema_version()

    def schema_version(self) -> int:
        return self.query("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations")[0]["v"]

    def ping(self) -> bool:
        try:
            self.query("SELECT 1")
        except sqlite3.Error:
            return False
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()
