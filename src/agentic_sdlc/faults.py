"""Fault injection for resilience drills.

Recovery paths (retry, fallback, rollback, rework) are only trustworthy if they
are exercised. Faults are declared as data, fire a bounded number of times,
and are always recorded in the audit log as ``fault.injected`` so a drill can
never be mistaken for a real failure.

Fault kinds:
  transient       raise a retryable error before the agent runs
  permanent       raise a non-retryable error before the agent runs
  corrupt_change  mutate the agent's change set (a seeded defect) on one invocation
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .agents.base import AgentResult
from .models import PermanentError, TransientError


def parse_fault(text: str) -> dict[str, Any]:
    """Parse the CLI form ``node:kind[:times]``, e.g. ``architecture:transient:2``.

    Task nodes contain a colon themselves (``impl:T1``), so the kind is located
    by name rather than by position.
    """
    parts = text.split(":")
    for index, part in enumerate(parts):
        if part in ("transient", "permanent") and index > 0:
            times = int(parts[index + 1]) if len(parts) > index + 1 else 1
            return {"node": ":".join(parts[:index]), "kind": part, "times": times}
    raise ValueError(f"cannot parse fault {text!r}; expected node:transient[:n] or node:permanent[:n]")


class FaultInjector:
    def __init__(
        self,
        faults: list[dict[str, Any]] | None = None,
        counts: dict[str, int] | None = None,
        on_fire: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.faults = faults or []
        self.counts = dict(counts or {})  # persisted in run state so a resume does not re-fire
        self.on_fire = on_fire or (lambda node, fault: None)
        self._lock = threading.Lock()

    def _take(self, index: int, fault: dict[str, Any]) -> bool:
        key = f"{index}:{fault['node']}:{fault['kind']}"
        with self._lock:
            if self.counts.get(key, 0) >= fault.get("times", 1):
                return False
            self.counts[key] = self.counts.get(key, 0) + 1
        self.on_fire(fault["node"], fault)
        return True

    def before(self, node_id: str) -> None:
        for index, fault in enumerate(self.faults):
            if fault["node"] != node_id or fault["kind"] not in ("transient", "permanent"):
                continue
            if self._take(index, fault):
                if fault["kind"] == "transient":
                    raise TransientError("injected fault: simulated provider timeout")
                raise PermanentError("injected fault: simulated provider outage")

    def after(self, node_id: str, invocation: int, result: AgentResult) -> AgentResult:
        for index, fault in enumerate(self.faults):
            if fault["node"] != node_id or fault["kind"] != "corrupt_change" or result.changeset is None:
                continue
            if invocation != fault.get("invocation", 0) or not self._take(index, fault):
                continue
            hit = False
            for change in result.changeset.changes:
                if change.path != fault["path"]:
                    continue
                if change.content and fault["search"] in change.content:
                    change.content, hit = change.content.replace(fault["search"], fault["replace"]), True
                for edit in change.edits:
                    if fault["search"] in edit.replace:
                        edit.replace, hit = edit.replace.replace(fault["search"], fault["replace"]), True
            if not hit:
                raise PermanentError(f"fault for {node_id} matched nothing in {fault['path']}")
        return result
