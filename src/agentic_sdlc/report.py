"""Engineering summary: the reviewable outcome of a run.

Everything in the report is read back from the run's artifacts, decisions,
approvals and audit log - it is a rendering of recorded facts, not a narrative
written by a model.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .metrics import run_metrics
from .store import RunStore


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return out + [""]


def _detail(data: dict[str, Any]) -> str:
    """One readable line for an audit event: the reason first, bulky lists left out."""
    keys = [k for k in ("reason", "error", "blame") if data.get(k)]
    keys += [k for k in data if k not in (*keys, "files", "downstream", "note") and data[k] not in (None, [], "")]
    return "; ".join(f"{k}={str(data[k])[:160]}" for k in keys)


def _waves(tasks: list[dict[str, Any]]) -> dict[str, int]:
    """Earliest parallel wave each task can run in."""
    by_id = {t["id"]: t for t in tasks}
    memo: dict[str, int] = {}

    def wave(task_id: str) -> int:
        if task_id not in memo:
            memo[task_id] = 1 + max((wave(d) for d in by_id[task_id]["depends_on"] if d in by_id), default=0)
        return memo[task_id]

    return {t["id"]: wave(t["id"]) for t in tasks}


def release_checklist(store: RunStore, min_coverage: float) -> list[dict[str, Any]]:
    tests = store.get_artifact("test_report") or {}
    trace = store.get_artifact("traceability") or {}
    security = store.get_artifact("security_report") or {}
    docs = store.get_artifact("docs_report") or {}
    plan = store.get_artifact("plan") or {"tasks": []}
    chain_ok, chain_detail = store.audit.verify()
    coverage = tests.get("coverage", {}).get("percent", 0.0)
    done = [t["id"] for t in plan["tasks"] if store.latest(f"changeset:{t['id']}")]
    signed = sorted({a.checkpoint for a in store.state.approvals if a.status == "approved"})
    return [
        {"item": "All planned tasks implemented", "ok": len(done) == len(plan["tasks"]), "evidence": f"{len(done)}/{len(plan['tasks'])} tasks"},
        {"item": "Test suite passes", "ok": bool(tests.get("passed")), "evidence": f"{tests.get('tests', 0)} tests, {tests.get('failures', 0)} failures, {tests.get('errors', 0)} errors"},
        {"item": f"Coverage at least {min_coverage}%", "ok": coverage >= min_coverage, "evidence": f"{coverage}%"},
        {"item": "Every functional requirement has a test", "ok": not trace.get("untested_functional", ["?"]), "evidence": f"untested: {trace.get('untested_functional', [])}"},
        {"item": "No blocking security or compliance findings", "ok": security.get("counts", {}).get("high", 1) == 0, "evidence": str(security.get("counts", {}))},
        {"item": "API schema exported and reference generated", "ok": bool(docs.get("endpoints")), "evidence": f"{len(docs.get('endpoints', []))} operations"},
        {"item": "Plan signed off by a human", "ok": "plan-signoff" in signed, "evidence": f"approved checkpoints: {signed}"},
        {"item": "Audit trail intact", "ok": chain_ok, "evidence": chain_detail},
    ]  # fmt: skip


def build_summary(store: RunStore) -> str:
    state = store.state
    spec = store.get_artifact("requirement_spec") or {}
    design = store.get_artifact("design") or {}
    plan = store.get_artifact("plan") or {}
    impact = store.get_artifact("impact_report") or {}
    tests = store.get_artifact("test_report") or {}
    trace = store.get_artifact("traceability") or {}
    security = store.get_artifact("security_report") or {}
    changes = store.get_artifact("change_summary") or {}
    release = store.get_artifact("release_report") or {}
    promotion = store.get_artifact("promotion")
    events = store.audit.read()
    metrics = run_metrics(events)
    chain_ok, chain_detail = store.audit.verify()

    def stamp(ts: float | None) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""

    try:  # show the target relative to where the command ran, when it is below it
        target = Path(state.target_dir).resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        target = state.target_dir

    md: list[str] = [
        f"# Engineering summary: {spec.get('title', state.scenario)}",
        "",
        f"- Run: `{state.run_id}` ({state.scenario} scenario, provider `{state.provider}`)",
        f"- Status: **{state.status.value}**" + (f" - {state.error or state.stop_reason}" if state.error or state.stop_reason else ""),
        f"- Started {stamp(state.created_at)}, last update {stamp(state.updated_at)}",
        f"- Target: `{target}`" + (" (promoted)" if promotion else " (not promoted)"),
        "",
        "## 1. Requirement and interpretation",
        "",
        "Raw requirement as received:",
        "",
        *[f"> {line}" for line in state.requirement.splitlines()],
        "",
    ]  # fmt: skip
    if spec:
        md += [f"**Normalised problem.** {spec['summary']}", ""]
        md += _table(
            ["ID", "Requirement", "Priority", "Acceptance criteria"],
            [
                [r["id"], r["statement"], r["priority"], "<br>".join(r["acceptance_criteria"])]
                for r in spec["functional"] + spec["non_functional"]
            ],
        )
        if spec["assumptions"]:
            md += ["**Assumptions and resolved ambiguities**", ""]
            md += _table(
                ["ID", "Source", "Statement"], [[a["id"], a["source"], a["statement"]] for a in spec["assumptions"]]
            )
        if spec["open_questions"]:
            md += ["**Open questions accepted at their recommended default**", ""]
            md += _table(
                ["ID", "Question", "Assumed"],
                [[q["id"], q["question"], q["recommended"]] for q in spec["open_questions"]],
            )
        if spec["out_of_scope"]:
            md += ["**Out of scope:** " + "; ".join(spec["out_of_scope"]), ""]

    md += ["## 2. Plan and rationale", ""]
    if design:
        md += [design["overview"], "", "**Design decisions**", ""]
        md += _table(
            ["ID", "Decision", "Alternatives rejected", "Consequences"],
            [
                [d["id"], f"**{d['title']}.** {d['decision']}", "; ".join(d["alternatives"]), d["consequences"]]
                for d in design["decisions"]
            ],
        )
    if plan:
        waves = _waves(plan["tasks"])
        md += [
            f"**Task graph** (plan version {state.plan_version}; tasks in the same wave ran in parallel). {plan['summary']}",
            "",
        ]
        md += _table(
            ["Task", "Wave", "Kind", "Depends on", "Requirements", "Writes"],
            [
                [
                    f"{t['id']} {t['title']}",
                    waves[t["id"]],
                    t["kind"],
                    ", ".join(t["depends_on"]) or "-",
                    ", ".join(t["requirement_ids"]),
                    "<br>".join(t["writes"]),
                ]
                for t in plan["tasks"]
            ],
        )
        md += [f"**Test strategy.** {plan['test_strategy']}", ""]

    if impact.get("impacted_modules"):
        md += ["## 3. Codebase impact (computed from the import graph)", ""]
        md += _table(
            ["Aspect", "Result"],
            [
                ["Modules changed", ", ".join(impact["impacted_modules"])],
                ["Transitive dependents", ", ".join(impact["transitive_dependents"]) or "none"],
                ["Existing tests exercising them", ", ".join(impact["affected_tests"]) or "none"],
                ["Routes affected", ", ".join(impact["affected_routes"]) or "none"],
            ],
        )
    else:
        md += ["## 3. Codebase impact", "", "Greenfield: no existing modules were affected.", ""]

    md += ["## 4. Artifacts", ""]
    if changes:
        md += _table(
            ["Change", "Files"],
            [[kind, "<br>".join(changes.get(kind, [])) or "-"] for kind in ("added", "modified", "deleted")],
        )
    md += _table(
        ["Artifact", "Versions", "Produced by", "Hash"],
        [[name, len(v), v[-1].produced_by, v[-1].hash[:12]] for name, v in sorted(state.artifacts.items())],
    )

    md += ["## 5. Validation", ""]
    if tests:
        md += [
            f"- Tests: **{tests['tests']} run, {tests['failures']} failed, {tests['errors']} errors** "
            f"(executed in the sandbox with pytest)",
            f"- Coverage: **{tests['coverage']['percent']}%**",
        ]
    if security:
        md += [f"- Security and compliance scan: {security['files_scanned']} files, findings {security['counts']}"]
    md += [
        f"- Gates: {metrics.get('gates_evaluated', 0)} evaluated, {metrics.get('gates_failed', 0)} failed and recovered",
        "",
    ]
    if trace:
        md += ["**Requirement traceability**", ""]
        md += _table(["Requirement", "Tests"], [[rid, "<br>".join(t) or "none"] for rid, t in trace["matrix"].items()])
    if release.get("checklist"):
        md += ["**Release checklist**", ""]
        md += _table(
            ["Check", "Result", "Evidence"],
            [[c["item"], "pass" if c["ok"] else "FAIL", c["evidence"]] for c in release["checklist"]],
        )
    failed_gates = [e for e in events if e["type"] == "gate.evaluated" and not e["data"]["passed"]]
    if failed_gates:
        md += ["**Gate failures during the run** (each was recovered or stopped the run)", ""]
        md += _table(
            ["Node", "Gate", "Reason"],
            [[e["node"], e["data"]["gate"], e["data"]["details"][:240]] for e in failed_gates],
        )

    md += ["## 6. Risks, trade-offs and rollback", ""]
    if design:
        md += _table(
            ["Risk", "Likelihood", "Impact", "Mitigation"],
            [[f"{r['id']} {r['description']}", r["likelihood"], r["impact"], r["mitigation"]] for r in design["risks"]],
        )
        md += [f"**Rollback plan.** {design['rollback_plan']}", ""]
        if design["limitations"]:
            md += ["**Known limitations**", "", *[f"- {item}" for item in design["limitations"]], ""]

    md += ["## 7. Governance trail", ""]
    if state.approvals:
        md += _table(
            ["Checkpoint", "Node", "Required role", "Outcome", "By", "Comment"],
            [
                [
                    a.checkpoint,
                    a.node,
                    a.role,
                    a.status,
                    a.decision.by if a.decision else "-",
                    a.decision.comment if a.decision else "",
                ]
                for a in state.approvals
            ],
        )
    recovery = [
        e for e in events
        if e["type"] in ("node.attempt_failed", "node.fallback", "rework.triggered", "workspace.rolled_back", "node.reset", "plan.expanded", "fault.injected", "safe_stop")
    ]  # fmt: skip
    if recovery:
        md += ["**Recovery and re-planning events**", ""]
        md += _table(
            ["Seq", "Event", "Node", "Detail"],
            [[e["seq"], e["type"], e["node"] or "-", _detail(e["data"])] for e in recovery],
        )
    md += [
        f"- Decisions recorded: {len(state.decisions)} (see `state.json` -> `decisions`, or `sdlc lineage`)",
        f"- Audit log: {chain_detail}" + ("" if chain_ok else " - **INTEGRITY FAILURE**"),
        "",
        "## 8. Reliability metrics",
        "",
    ]
    md += _table(
        ["Metric", "Value"],
        [[k, "n/a" if v is None else v] for k, v in metrics.items() if k != "stage_latency_s"],
    )
    if metrics.get("stage_latency_s"):
        md += _table(["Stage", "Agent time (s)"], [[k, v] for k, v in metrics["stage_latency_s"].items()])
    return "\n".join(md).rstrip() + "\n"
