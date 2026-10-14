"""Durable run state: snapshot, versioned artifacts, audit log, sandbox.

Layout of a run directory::

    runs/<run_id>/
      state.json        resumable snapshot (atomically replaced on every transition)
      audit.jsonl       hash-chained event log
      artifacts/        every version of every artifact, with provenance in state.json
      workspace/        sandbox copy of the target codebase
      journal/          before-images for per-node rollback
      baseline.json     manifest of the target at the time the sandbox was seeded
      approvals/        human-readable review packets for pending checkpoints
      promotion/        backup of what a release replaced (enables release rollback)
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .models import ArtifactMeta, Decision, RunState, content_hash, now
from .workspace import Workspace, _write_text


class RunStore:
    def __init__(self, run_dir: Path, state: RunState):
        self.dir = run_dir.resolve()
        self.state = state
        self.audit = AuditLog(run_dir / "audit.jsonl", state.run_id)
        self.workspace = Workspace(run_dir / "workspace", run_dir / "journal")
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def create(
        cls,
        runs_root: Path,
        scenario: str,
        requirement: str,
        target_dir: Path,
        provider: str,
        run_id: str | None = None,
    ) -> RunStore:
        run_id = run_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{scenario}-{secrets.token_hex(2)}"
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        state = RunState(
            run_id=run_id,
            scenario=scenario,
            requirement=requirement,
            target_dir=str(target_dir.resolve()),
            provider=provider,
        )
        store = cls(run_dir, state)
        target_dir.mkdir(parents=True, exist_ok=True)
        baseline = store.workspace.seed_from(target_dir)
        _write_text(run_dir / "baseline.json", json.dumps(baseline, indent=2, sort_keys=True))
        store.save()
        return store

    @classmethod
    def load(cls, run_dir: Path) -> RunStore:
        state = RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))
        return cls(run_dir, state)

    def save(self) -> None:
        self.state.updated_at = now()
        _write_text(self.dir / "state.json", self.state.model_dump_json(indent=2))

    def baseline(self) -> dict[str, str]:
        return json.loads((self.dir / "baseline.json").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ artifacts

    def _artifact_path(self, name: str, version: int) -> Path:
        return self.dir / "artifacts" / re.sub(r"[^A-Za-z0-9_.-]", "__", name) / f"v{version}.json"

    def put_artifact(
        self, name: str, content: Any, produced_by: str, derived_from: dict[str, int]
    ) -> tuple[ArtifactMeta, bool]:
        """Store a new version unless the content is unchanged. Returns (meta, changed)."""
        digest = content_hash(content)
        with self._lock:
            versions = self.state.artifacts.setdefault(name, [])
            if versions and versions[-1].hash == digest:
                return versions[-1], False
            meta = ArtifactMeta(
                name=name,
                version=len(versions) + 1,
                hash=digest,
                produced_by=produced_by,
                created_at=now(),
                derived_from=derived_from,
            )
            _write_text(self._artifact_path(name, meta.version), json.dumps(content, indent=2, sort_keys=True))
            versions.append(meta)
        self.audit.append(
            "artifact.stored",
            actor=produced_by,
            node=produced_by,
            name=name,
            version=meta.version,
            hash=digest[:12],
            derived_from=derived_from,
        )
        return meta, True

    def latest(self, name: str) -> ArtifactMeta | None:
        versions = self.state.artifacts.get(name)
        return versions[-1] if versions else None

    def get_artifact(self, name: str, version: int | None = None) -> Any:
        meta = self.latest(name)
        if meta is None:
            return None
        return json.loads(self._artifact_path(name, version or meta.version).read_text(encoding="utf-8"))

    def hash_of(self, name: str) -> str | None:
        meta = self.latest(name)
        return meta.hash if meta else None

    # ------------------------------------------------------------------ decisions

    def record_decision(
        self, actor: str, title: str, rationale: str, node: str | None = None, alternatives: list[str] | None = None
    ) -> Decision:
        with self._lock:
            decision = Decision(
                id=f"D-{len(self.state.decisions) + 1:03d}",
                node=node,
                actor=actor,
                title=title,
                rationale=rationale,
                alternatives=alternatives or [],
                ts=now(),
            )
            self.state.decisions.append(decision)
        self.audit.append("decision.recorded", actor=actor, node=node, id=decision.id, title=title, rationale=rationale)
        return decision
