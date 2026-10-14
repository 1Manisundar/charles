"""In-memory token-bucket rate limiting."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from typing import Callable


class RateLimited(Exception):
    """The caller has exhausted its budget; retry after ``retry_after_seconds``."""

    def __init__(self, retry_after: float):
        self.retry_after_seconds = max(1, math.ceil(retry_after))
        super().__init__(f"rate limit exceeded; retry in {self.retry_after_seconds}s")


class RateLimiter:
    """One token bucket per key: bursts up to ``per_minute``, refilled continuously.

    State lives in this process, so the limit is per instance. Memory is bounded:
    beyond ``max_keys`` the least recently seen key is forgotten (and so starts
    again with a full bucket - an acceptable trade for a hard memory ceiling).
    """

    def __init__(self, per_minute: int, max_keys: int = 10_000, clock: Callable[[], float] = time.monotonic):
        self.per_minute = per_minute
        self._refill_per_second = per_minute / 60
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()  # key -> (tokens, last seen)
        self._lock = threading.Lock()

    def check(self, key: str) -> float:
        """Take one token for ``key``. Returns 0.0 if allowed, else seconds until one is available."""
        if self.per_minute <= 0:
            return 0.0  # limiter disabled
        now = self._clock()
        with self._lock:
            tokens, seen = self._buckets.pop(key, (float(self.per_minute), now))
            tokens = min(float(self.per_minute), tokens + (now - seen) * self._refill_per_second)
            wait = 0.0
            if tokens >= 1:
                tokens -= 1
            else:
                wait = (1 - tokens) / self._refill_per_second
            self._buckets[key] = (tokens, now)  # re-inserted last: most recently seen
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
            return wait

    def __len__(self) -> int:
        return len(self._buckets)
