"""Entry and exit gates.

A gate is a named, deterministic predicate over the run's artifacts and
workspace. Entry gates decide whether a node may start; exit gates decide
whether what it produced may be consumed downstream. A failing gate also says
how the engine may recover: retry the agent with the failure as feedback,
re-open upstream work, or stop safely.
"""

from __future__ import annotations

import ast
import fnmatch
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agents.base import AgentResult
from .graph import WorkflowGraph
from .models import Finding, GateResult, NodeSpec, NodeStatus
from .policy import Policy, ProjectProfile, check_change_scope, scan_file
from .store import RunStore
from .workspace import tree_manifest


@dataclass
class GateContext:
    spec: NodeSpec
    store: RunStore
    graph: WorkflowGraph
    policy: Policy
    profile: ProjectProfile
    result: AgentResult | None = None  # exit gates: what this attempt produced
    applied: dict[str, str | None] | None = None  # exit gates: files this attempt changed

    def artifact(self, name: str) -> Any:
        if self.result is not None and name in self.result.artifacts:
            return self.result.artifacts[name]
        return self.store.get_artifact(name)


Gate = Callable[[GateContext], GateResult]
GATES: dict[str, Gate] = {}
CONDITIONS: dict[str, Callable[[GateContext], bool]] = {}


def gate(name: str) -> Callable[[Gate], Gate]:
    def register(fn: Gate) -> Gate:
        GATES[name] = fn
        return fn

    return register


def _ok(name: str, details: str = "") -> GateResult:
    return GateResult(gate=name, passed=True, details=details)


def _fail(name: str, details: str, **how: Any) -> GateResult:
    return GateResult(gate=name, passed=False, details=details, **how)


def _bullets(items: list[str], limit: int = 12) -> str:
    shown = items[:limit] + ([f"... and {len(items) - limit} more"] if len(items) > limit else [])
    return "; ".join(shown)


# --------------------------------------------------------------------------- entry gates


@gate("inputs_available")
def inputs_available(ctx: GateContext) -> GateResult:
    missing = [n for n in ctx.graph.inputs(ctx.spec.id) if ctx.store.latest(n) is None]
    return _fail("inputs_available", f"missing artifacts: {missing}") if missing else _ok("inputs_available")


@gate("budget_available")
def budget_available(ctx: GateContext) -> GateResult:
    state, limit = ctx.store.state, ctx.policy.max_agent_invocations
    used = state.agent_invocations + state.reserved_invocations  # in-flight nodes count too
    if used >= limit:
        return _fail("budget_available", f"agent invocation budget exhausted ({used}/{limit})", safe_stop=True)
    return _ok("budget_available", f"{used}/{limit} invocations used")


@gate("change_freeze_clear")
def change_freeze_clear(ctx: GateContext) -> GateResult:
    if ctx.policy.change_freeze:
        return _fail("change_freeze_clear", "a change freeze is in effect; promotion is blocked", safe_stop=True)
    return _ok("change_freeze_clear")


@gate("target_matches_baseline")
def target_matches_baseline(ctx: GateContext) -> GateResult:
    """Refuse to promote onto a target that changed since the sandbox was seeded."""
    drift = ctx.store.baseline() != tree_manifest(Path(ctx.store.state.target_dir))
    if drift:
        return _fail(
            "target_matches_baseline",
            "the target changed after this run started; re-run against the new baseline",
            safe_stop=True,
        )
    return _ok("target_matches_baseline")


# --------------------------------------------------------------------------- requirements


@gate("requirements_well_formed")
def requirements_well_formed(ctx: GateContext) -> GateResult:
    spec = ctx.artifact("requirement_spec")
    reqs = spec["functional"] + spec["non_functional"]
    ids = [r["id"] for r in reqs]
    problems = []
    if not spec["functional"]:
        problems.append("no functional requirements")
    problems += [f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    problems += [f"{r['id']} has no acceptance criteria" for r in reqs if not r["acceptance_criteria"]]
    problems += [f"{q['id']} offers no options" for q in spec["open_questions"] if not q["options"]]
    if problems:
        return _fail("requirements_well_formed", _bullets(problems), retryable=True)
    return _ok(
        "requirements_well_formed",
        f"{len(spec['functional'])} functional, {len(spec['non_functional'])} non-functional",
    )


def has_open_questions(ctx: GateContext) -> bool:
    """Approval condition: only ask a human to clarify when something is unclear."""
    return bool(ctx.artifact("requirement_spec")["open_questions"])


CONDITIONS["has_open_questions"] = has_open_questions


# --------------------------------------------------------------------------- design


def _requirement_ids(ctx: GateContext) -> list[str]:
    spec = ctx.artifact("requirement_spec")
    return [r["id"] for r in spec["functional"] + spec["non_functional"]]


@gate("design_covers_requirements")
def design_covers_requirements(ctx: GateContext) -> GateResult:
    design = ctx.artifact("design")
    covered = {c["requirement_id"] for c in design["requirement_coverage"]}
    missing = [r for r in _requirement_ids(ctx) if r not in covered]
    if missing:
        return _fail("design_covers_requirements", f"design does not address: {missing}", retryable=True)
    return _ok("design_covers_requirements", f"{len(covered)} requirements covered")


@gate("design_is_reviewable")
def design_is_reviewable(ctx: GateContext) -> GateResult:
    design = ctx.artifact("design")
    problems = []
    if not design["decisions"]:
        problems.append("no design decisions recorded")
    problems += [f"risk {r['id']} has no mitigation" for r in design["risks"] if not r["mitigation"].strip()]
    if not design["rollback_plan"].strip():
        problems.append("no rollback plan")
    if problems:
        return _fail("design_is_reviewable", _bullets(problems), retryable=True)
    return _ok("design_is_reviewable", f"{len(design['decisions'])} decisions, {len(design['risks'])} risks")


@gate("impacted_modules_exist")
def impacted_modules_exist(ctx: GateContext) -> GateResult:
    """Catch hallucinated file references before anyone plans against them."""
    unknown = ctx.artifact("impact_report")["unknown_modules"]
    if unknown:
        return _fail("impacted_modules_exist", f"design references files that do not exist: {unknown}", retryable=True)
    return _ok("impacted_modules_exist")


# --------------------------------------------------------------------------- plan


def task_ancestors(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    deps = {t["id"]: set(t["depends_on"]) for t in tasks}
    closure: dict[str, set[str]] = {}

    def visit(task_id: str, trail: tuple[str, ...]) -> set[str]:
        if task_id in trail:
            raise ValueError(f"dependency cycle: {' -> '.join(trail + (task_id,))}")
        if task_id not in closure:
            found: set[str] = set()
            for dep in deps.get(task_id, ()):
                found |= {dep} | visit(dep, trail + (task_id,))
            closure[task_id] = found
        return closure[task_id]

    for task_id in deps:
        visit(task_id, ())
    return closure


@gate("plan_is_dag")
def plan_is_dag(ctx: GateContext) -> GateResult:
    tasks = ctx.artifact("plan")["tasks"]
    ids = [t["id"] for t in tasks]
    problems = []
    if not tasks:
        problems.append("plan has no tasks")
    problems += [f"duplicate task id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    problems += [f"{t['id']} depends on unknown task {d}" for t in tasks for d in t["depends_on"] if d not in ids]
    if not problems:
        try:
            task_ancestors(tasks)
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        return _fail("plan_is_dag", _bullets(problems), retryable=True)
    return _ok("plan_is_dag", f"{len(tasks)} tasks")


@gate("plan_traces_requirements")
def plan_traces_requirements(ctx: GateContext) -> GateResult:
    tasks = ctx.artifact("plan")["tasks"]
    known = set(_requirement_ids(ctx))
    problems = [f"{t['id']} traces to no requirement" for t in tasks if not t["requirement_ids"]]
    problems += [
        f"{t['id']} cites unknown requirement {r}" for t in tasks for r in t["requirement_ids"] if r not in known
    ]
    planned = {r for t in tasks for r in t["requirement_ids"]}
    problems += [f"requirement {r} has no task" for r in sorted(known - planned)]
    if problems:
        return _fail("plan_traces_requirements", _bullets(problems), retryable=True)
    return _ok("plan_traces_requirements", f"{len(known)} requirements traced to {len(tasks)} tasks")


@gate("plan_respects_ownership")
def plan_respects_ownership(ctx: GateContext) -> GateResult:
    """Tasks that may run in parallel must not share files; some paths are off limits."""
    tasks = ctx.artifact("plan")["tasks"]
    try:
        ancestors = task_ancestors(tasks)
    except ValueError:
        return _ok("plan_respects_ownership", "skipped: plan is not a DAG")
    problems = []
    for i, a in enumerate(tasks):
        for path in a["writes"]:
            if ctx.policy.is_forbidden(path):
                problems.append(f"{a['id']} writes forbidden path {path}")
            if any(fnmatch.fnmatch(path, pat) for pat in ctx.profile.docs_paths):
                problems.append(f"{a['id']} writes {path}, which belongs to the documentation stage")
        for b in tasks[i + 1 :]:
            shared = sorted(set(a["writes"]) & set(b["writes"]))
            ordered = a["id"] in ancestors[b["id"]] or b["id"] in ancestors[a["id"]]
            if shared and not ordered:
                problems.append(f"{a['id']} and {b['id']} may run in parallel but both write {shared}")
    if problems:
        return _fail("plan_respects_ownership", _bullets(problems), retryable=True)
    return _ok("plan_respects_ownership")


# --------------------------------------------------------------------------- implementation


def _changed_text(ctx: GateContext) -> dict[str, str]:
    ws = ctx.store.workspace
    return {p: ws.read(p) for p, digest in (ctx.applied or {}).items() if digest is not None}


@gate("change_within_scope")
def change_within_scope(ctx: GateContext) -> GateResult:
    """An agent may only touch the files its task declared (or its stage owns)."""
    task = ctx.spec.params.get("task")
    allowed = task["writes"] if task else ctx.profile.docs_paths
    outside = sorted(p for p in (ctx.applied or {}) if not any(fnmatch.fnmatch(p, pat) for pat in allowed))
    if outside:
        finding = Finding(
            rule="change/out-of-scope",
            category="change_control",
            severity="high",
            message=f"wrote outside the declared scope: {outside}",
        )
        return _fail(
            "change_within_scope",
            f"changed files outside the declared scope {allowed}: {outside}",
            findings=[finding],
            retryable=True,
        )
    return _ok("change_within_scope", f"{len(ctx.applied or {})} files, all in scope")


@gate("change_passes_policy")
def change_passes_policy(ctx: GateContext) -> GateResult:
    texts = _changed_text(ctx)
    findings = check_change_scope(list(ctx.applied or {}), {p: len(t.encode()) for p, t in texts.items()}, ctx.policy)
    for path, text in texts.items():
        findings += scan_file(path, text, ctx.policy, ctx.profile)
    blocking = [f for f in findings if f.severity == "high"]
    if blocking:
        details = _bullets([f"{f.path}:{f.line or ''} {f.rule} - {f.message}" for f in blocking])
        return _fail("change_passes_policy", details, findings=findings, retryable=True)
    return _ok("change_passes_policy", f"{len(texts)} files scanned, {len(findings)} non-blocking findings")


@gate("workspace_consistent")
def workspace_consistent(ctx: GateContext) -> GateResult:
    """Synchronisation check after the parallel tasks join."""
    plan = ctx.store.get_artifact("plan")
    missing = [t["id"] for t in plan["tasks"] if ctx.store.latest(f"changeset:{t['id']}") is None]
    broken = []
    ws = ctx.store.workspace
    for path in ws.files():
        if path.endswith(".py"):
            try:
                ast.parse(ws.read(path))
            except SyntaxError as exc:
                broken.append(f"{path}:{exc.lineno} {exc.msg}")
    if missing or broken:
        return _fail(
            "workspace_consistent", _bullets([f"task {m} produced no change" for m in missing] + broken), rework=True
        )
    return _ok("workspace_consistent", f"{len(plan['tasks'])} tasks merged")


# --------------------------------------------------------------------------- verification


@gate("tests_pass")
def tests_pass(ctx: GateContext) -> GateResult:
    report = ctx.artifact("test_report")
    if report["passed"]:
        return _ok("tests_pass", f"{report['tests']} tests passed")
    if report["tests"] == 0:
        return _fail(
            "tests_pass", f"no tests ran (exit code {report['exit_code']}):\n{report['output_tail']}", rework=True
        )
    failed = report["failed_tests"]
    # Every failing test id comes first, so feedback cut to a budget still names all of them.
    index = "\n".join(f"- {f['test']}" for f in failed)
    lines = [f"{f['test']}: {f['message']}\n{f['detail']}" for f in failed]
    return _fail(
        "tests_pass",
        f"{len(failed)} of {report['tests']} tests failed:\n{index}\n\nDetails:\n" + "\n".join(lines),
        rework=True,
    )


@gate("coverage_threshold")
def coverage_threshold(ctx: GateContext) -> GateResult:
    percent, minimum = ctx.artifact("test_report")["coverage"]["percent"], ctx.policy.min_coverage
    if percent < minimum:
        return _fail("coverage_threshold", f"coverage {percent}% is below the required {minimum}%", rework=True)
    return _ok("coverage_threshold", f"{percent}% (minimum {minimum}%)")


@gate("requirements_have_tests")
def requirements_have_tests(ctx: GateContext) -> GateResult:
    untested = ctx.artifact("traceability")["untested_functional"]
    if untested and ctx.policy.require_requirement_tests:
        return _fail("requirements_have_tests", f"no test references functional requirements {untested}", rework=True)
    return _ok("requirements_have_tests")


@gate("no_blocking_findings")
def no_blocking_findings(ctx: GateContext) -> GateResult:
    report = ctx.artifact("security_report")
    blocking = [Finding(**f) for f in report["findings"] if f["severity"] == "high"]
    if blocking:
        details = _bullets([f"{f.path}:{f.line or ''} {f.rule} - {f.message}" for f in blocking])
        return _fail("no_blocking_findings", details, findings=blocking, rework=True)
    return _ok("no_blocking_findings", f"{report['files_scanned']} files scanned, {report['counts']}")


# --------------------------------------------------------------------------- documentation & release


@gate("openapi_exported")
def openapi_exported(ctx: GateContext) -> GateResult:
    report = ctx.artifact("docs_report")
    if report["openapi_error"]:
        # The application does not import: an implementation problem, not a docs problem.
        return _fail("openapi_exported", f"could not export the API schema:\n{report['openapi_error']}", rework=True)
    return _ok("openapi_exported", f"{len(report['endpoints'])} operations documented")


@gate("release_checklist_green")
def release_checklist_green(ctx: GateContext) -> GateResult:
    red = [c["item"] for c in ctx.artifact("release_report")["checklist"] if not c["ok"]]
    if red:
        return _fail("release_checklist_green", f"release checklist items not satisfied: {red}")
    return _ok("release_checklist_green")


@gate("upstream_fresh")
def upstream_fresh(ctx: GateContext) -> GateResult:
    """A release may only be assembled from results that are current."""
    pending = [
        n for n in ctx.graph.ancestors(ctx.spec.id) if ctx.store.state.nodes[n].status is not NodeStatus.SUCCEEDED
    ]
    return (
        _fail("upstream_fresh", f"upstream nodes are not complete: {sorted(pending)}")
        if pending
        else _ok("upstream_fresh")
    )
