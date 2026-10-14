"""Runtime configuration, read once from the environment at start-up."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "urlshortener.db"
    base_url: str = "http://localhost:8000"
    code_length: int = 7
    max_url_length: int = 2048
    stats_days: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from ``URLSHORTENER_*`` variables, falling back to defaults."""
        defaults = cls()

        def read(name: str, default: str | int) -> str:
            return os.environ.get(f"URLSHORTENER_{name}", str(default))

        return cls(
            db_path=read("DB_PATH", defaults.db_path),
            base_url=read("BASE_URL", defaults.base_url).rstrip("/"),
            code_length=int(read("CODE_LENGTH", defaults.code_length)),
            max_url_length=int(read("MAX_URL_LENGTH", defaults.max_url_length)),
            stats_days=int(read("STATS_DAYS", defaults.stats_days)),
        )
