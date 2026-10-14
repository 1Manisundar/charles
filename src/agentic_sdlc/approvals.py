"""Human checkpoints.

The engine raises an ``ApprovalRequest`` with a review packet and asks an
``ApprovalProvider`` for a decision. Agents have no way to answer a request:
only a provider (a person at the terminal, or the ``sdlc approve`` command
writing to the run's inbox) can.

Providers:
  DeferredApprovals     never answers; the run pauses until ``sdlc approve`` (default)
  InteractiveApprovals  prompts the operator at the terminal
  ScriptedApprovals     replays decisions from a playbook - for unattended demos
                        and tests only; decisions are attributed to a named
                        simulated reviewer so the audit trail stays honest
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .models import ApprovalDecision, ApprovalRequest, RunState
from .store import RunStore


class ApprovalProvider(Protocol):
    def decide(self, request: ApprovalRequest) -> ApprovalDecision | None: ...


class DeferredApprovals:
    def decide(self, request: ApprovalRequest) -> ApprovalDecision | None:
        return None


class ScriptedApprovals:
    """Stateless replay: the n-th request for a checkpoint gets the n-th scripted decision."""

    def __init__(self, decisions: list[dict[str, Any]], state: RunState):
        self.decisions = decisions
        self.state = state

    @staticmethod
    def _matches(entry: dict[str, Any], checkpoint: str) -> bool:
        return entry["checkpoint"] in (checkpoint, checkpoint.split(":")[0])

    def decide(self, request: ApprovalRequest) -> ApprovalDecision | None:
        scripted = [d for d in self.decisions if self._matches(d, request.checkpoint)]
        already = sum(1 for a in self.state.approvals if a.checkpoint == request.checkpoint and a.decision is not None)
        if already >= len(scripted):
            return None
        entry = {k: v for k, v in scripted[already].items() if k != "checkpoint"}
        entry.setdefault("by", "demo-reviewer (simulated)")
        entry.setdefault("role", request.role)
        return ApprovalDecision(**entry)


class InteractiveApprovals:
    def __init__(self, approver: str, ask: Callable[[str], str] = input, show: Callable[[str], None] = print):
        # Some shells prepend a byte-order mark to piped input; it is not part of the answer.
        self.approver, self.ask, self.show = approver, (lambda prompt: ask(prompt).lstrip("﻿")), show

    def decide(self, request: ApprovalRequest) -> ApprovalDecision | None:
        self.show(
            "\n"
            + "=" * 78
            + f"\nHUMAN CHECKPOINT {request.id}: {request.checkpoint} (role: {request.role})\n"
            + "=" * 78
        )
        self.show(request.summary)
        base = {"by": self.approver, "role": request.role}
        if request.kind == "clarification":
            answers = {}
            for q in request.questions:
                self.show(f"\n{q['id']}: {q['question']}")
                for i, option in enumerate(q["options"], 1):
                    self.show(f"   {i}. {option}" + ("   <- recommended" if option == q["recommended"] else ""))
                raw = self.ask(
                    "   choose a number, type your own answer, or press Enter for the recommendation: "
                ).strip()
                answers[q["id"]] = (
                    q["options"][int(raw) - 1]
                    if raw.isdigit() and 0 < int(raw) <= len(q["options"])
                    else (raw or q["recommended"])
                )
            return ApprovalDecision(decision="changes", answers=answers, comment="clarified interactively", **base)
        while True:
            choice = self.ask("\n[a]pprove / [r]eject / [c]hanges requested / [d]efer: ").strip().lower()[:1]
            if choice == "a":
                return ApprovalDecision(decision="approve", comment=self.ask("comment (optional): ").strip(), **base)
            if choice == "r":
                return ApprovalDecision(decision="reject", comment=self.ask("reason: ").strip(), **base)
            if choice == "c":
                target = self.ask(f"node to re-open (Enter for '{request.node}'): ").strip() or None
                return ApprovalDecision(
                    decision="changes", target=target, comment=self.ask("what must change: ").strip(), **base
                )
            if choice == "d":
                return None


# --------------------------------------------------------------------------- review packets


def _lines(title: str, rows: list[str]) -> list[str]:
    return [f"{title}:", *[f"  - {row}" for row in rows], ""] if rows else []


def build_packet(
    store: RunStore, request_kind: str, checkpoint: str, node_title: str, reason: str, task: dict[str, Any] | None
) -> tuple[str, list[dict[str, Any]]]:
    """What the reviewer sees. Returns (summary text, clarification questions)."""
    spec = store.get_artifact("requirement_spec") or {}
    out = [f"Node: {node_title}", f"Why a human is needed: {reason}", ""]
    questions: list[dict[str, Any]] = []

    if request_kind == "clarification":
        questions = spec.get("open_questions", [])
        out += [f"Interpretation so far: {spec.get('summary', '')}", ""]
        for q in questions:
            out += [
                f"{q['id']}: {q['question']}",
                f"  why it matters: {q['why_it_matters']}",
                f"  recommended: {q['recommended']}",
                "",
            ]
    elif checkpoint == "plan-signoff":
        design, plan = store.get_artifact("design") or {}, store.get_artifact("plan") or {}
        impact = store.get_artifact("impact_report") or {}
        out += [f"Design: {design.get('overview', '')}", ""]
        out += _lines("Decisions", [f"{d['id']} {d['title']}: {d['decision']}" for d in design.get("decisions", [])])
        out += _lines(
            "Data model changes",
            [
                f"{d['entity']}: {d['change']}" + (" [MIGRATION]" if d["requires_migration"] else "")
                for d in design.get("data_model", [])
            ],
        )
        out += _lines(
            "Risks",
            [
                f"{r['id']} ({r['likelihood']}/{r['impact']}) {r['description']} -> {r['mitigation']}"
                for r in design.get("risks", [])
            ],
        )
        out += _lines("Existing modules impacted", impact.get("impacted_modules", []))
        out += _lines(
            "Tasks",
            [
                f"{t['id']} [{t['kind']}] {t['title']} (after: {', '.join(t['depends_on']) or '-'}) writes {t['writes']}"
                for t in plan.get("tasks", [])
            ],
        )
        out += [f"Rollback plan: {design.get('rollback_plan', '')}"]
    elif task is not None:
        design = store.get_artifact("design") or {}
        out += [f"Task {task['id']}: {task['title']}", task["description"], ""]
        out += _lines("Files", task["writes"])
        out += [f"Rollback plan: {design.get('rollback_plan', '')}"]
    elif checkpoint == "release":
        report = store.get_artifact("release_report") or {}
        out += _lines(
            "Release checklist",
            [f"[{'x' if c['ok'] else ' '}] {c['item']} - {c['evidence']}" for c in report.get("checklist", [])],
        )
        delta = report.get("delta", {})
        out += _lines(
            "Files that will change in the target",
            [f"{kind}: {path}" for kind in ("added", "modified", "deleted") for path in delta.get(kind, [])],
        )
    return "\n".join(out).rstrip(), questions
