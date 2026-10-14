"""Forward-only schema migrations.

Append new entries; never edit one that has shipped. Each entry is applied
once, inside a transaction, and recorded in ``schema_migrations``.
"""

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE links (
            code TEXT PRIMARY KEY,
            long_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL REFERENCES links(code),
            clicked_at TEXT NOT NULL,
            referrer TEXT
        );
        CREATE INDEX idx_clicks_code_time ON clicks(code, clicked_at);
        """,
    ),
    (
        2,
        # Additive and nullable: existing rows stay valid and the previous release
        # keeps working against the upgraded schema.
        """
        ALTER TABLE links ADD COLUMN expires_at TEXT;
        ALTER TABLE links ADD COLUMN is_custom INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        3,
        # An opaque daily identifier (see privacy.py), never an address. Nullable:
        # clicks recorded before this migration simply have no visitor.
        """
        ALTER TABLE clicks ADD COLUMN visitor_hash TEXT;
        """,
    ),
]
