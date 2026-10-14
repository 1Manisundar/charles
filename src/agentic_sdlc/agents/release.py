"""Synchronisation, release readiness and promotion."""

from __future__ import annotations

from pathlib import Path

from ..models import PermanentError
from ..report import release_checklist
from ..workspace import restore_promotion, tree_manifest
from .base import AgentContext, AgentResult


class SyncJoin:
    """Join point after the parallel implementation tasks: one consolidated change."""

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.store.workspace
        delta = ws.diff(ctx.store.baseline())
        manifest = tree_manifest(ws.root)
        summary = {
            **delta,
            # Content hashes, so that a rework which changes a file's content
            # (but not the list of files) still invalidates everything downstream.
            "content": {p: manifest[p] for p in delta["added"] + delta["modified"]},
            "tasks": {
                name.split(":", 1)[1]: sorted(change["files"])
                for name, change in ctx.inputs.items()
                if name.startswith("changeset:")
            },
        }
        return AgentResult(artifacts={"change_summary": summary})


class ReleaseManager:
    """Assembles the evidence a human needs to approve the release."""

    def run(self, ctx: AgentContext) -> AgentResult:
        checklist = release_checklist(ctx.store, ctx.policy.min_coverage)
        delta = ctx.store.workspace.diff(ctx.store.baseline())
        report = {"checklist": checklist, "delta": delta, "change_summary_hash": ctx.store.hash_of("change_summary")}
        return AgentResult(artifacts={"release_report": report})


class Promoter:
    """The one high-impact action: copy the approved sandbox onto the real target."""

    def run(self, ctx: AgentContext) -> AgentResult:
        target = Path(ctx.store.state.target_dir)
        backup = ctx.store.dir / "promotion"
        try:
            delta = ctx.store.workspace.promote(target, ctx.store.baseline(), backup)
        except OSError as exc:
            restore_promotion(target, backup)
            ctx.store.audit.append("release.rolled_back", node=ctx.spec.id, reason=f"promotion failed: {exc}")
            raise PermanentError(f"promotion failed and was rolled back: {exc}") from exc
        ctx.store.audit.append(
            "release.promoted", node=ctx.spec.id, target=str(target), **{k: len(v) for k, v in delta.items()}
        )
        return AgentResult(artifacts={"promotion": {**delta, "target": str(target)}})
