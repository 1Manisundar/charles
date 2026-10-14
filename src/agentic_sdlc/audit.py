"""Append-only, hash-chained audit log.

Every record embeds the hash of the previous one, so any edit, deletion or
reordering after the fact breaks the chain and is detected by ``verify``. The
log is the single source of truth for traceability and for reliability metrics.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import canonical_json, content_hash, now

GENESIS = "0" * 64
Listener = Callable[[dict[str, Any]], None]


class AuditLog:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []
        self._seq = 0
        self._prev = GENESIS
        if path.exists():
            for record in self.read():
                self._seq, self._prev = record["seq"], record["hash"]

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def append(self, type: str, actor: str = "orchestrator", node: str | None = None, **data: Any) -> dict[str, Any]:
        """Write one event. Safe to call from worker threads."""
        with self._lock:
            body = {
                "seq": self._seq + 1,
                "ts": now(),
                "run_id": self.run_id,
                "type": type,
                "actor": actor,
                "node": node,
                "data": json.loads(canonical_json(data)),
                "prev": self._prev,
            }
            record = {**body, "hash": content_hash(body)}
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(canonical_json(record) + "\n")
            self._seq, self._prev = record["seq"], record["hash"]
        for listener in self._listeners:
            listener(record)
        return record

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def verify(self) -> tuple[bool, str]:
        """Recompute the chain. Returns (ok, human-readable reason)."""
        prev, expected_seq = GENESIS, 1
        records = self.read()
        for record in records:
            body = {k: v for k, v in record.items() if k != "hash"}
            if record["seq"] != expected_seq:
                return False, f"sequence gap at record {expected_seq}"
            if record["prev"] != prev:
                return False, f"broken link at seq {record['seq']}"
            if content_hash(body) != record["hash"]:
                return False, f"record {record['seq']} was modified"
            prev, expected_seq = record["hash"], expected_seq + 1
        return True, f"{len(records)} records, chain intact"
