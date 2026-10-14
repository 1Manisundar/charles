"""Privacy-preserving analytics helpers.

Nothing in this module returns or stores personal data: visitors become opaque
daily identifiers and referrers are reduced to their host.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from datetime import date, datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

MAX_HOST_LENGTH = 253


class VisitorHasher:
    """Turns (address, user agent) into an identifier that is only meaningful for one UTC day.

    A plain hash of an IP address is not anonymous: the IPv4 space is small
    enough to enumerate. The identifier is therefore an HMAC under a random key
    that exists only in this process's memory and is replaced every day. It
    cannot be reversed, cannot be linked across days, and cannot be recomputed
    after a restart - at the cost of slightly over-counting visitors across a
    restart or across instances.
    """

    def __init__(self, key_factory: Callable[[], bytes] = lambda: secrets.token_bytes(32)):
        self._key_factory = key_factory
        self._day: date | None = None
        self._key = b""
        self._lock = threading.Lock()

    def visitor_id(self, address: str, user_agent: str, moment: datetime) -> str:
        day = moment.astimezone(timezone.utc).date()
        with self._lock:
            if day != self._day:
                self._day, self._key = day, self._key_factory()
            key = self._key
        message = f"{address}\n{user_agent}".encode("utf-8", "replace")
        return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def referrer_host(referrer: str | None) -> str | None:
    """Keep only the host of a referrer; paths and query strings can carry personal data."""
    if not referrer:
        return None
    try:
        host = urlsplit(referrer.strip()).hostname
    except ValueError:
        return None
    return host[:MAX_HOST_LENGTH].lower() if host else None
