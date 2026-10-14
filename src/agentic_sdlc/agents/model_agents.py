"""Model-backed agents: requirements, architecture, planning, implementation.

Each agent is a role prompt, a context builder and an output schema. The same
classes drive a live model and the scripted replay; only the provider differs.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from ..providers.base import ProviderRequest
from ..schemas import ChangeSet, Design, Plan, RequirementSpec
from .base import AgentContext, AgentResult

_COMMON = (
    "You are one agent in a governed software-delivery pipeline. A human reviews your output at "
    "checkpoints and automated gates verify it, so be precise and concrete rather than persuasive. "
    "If the context contains feedback (human answers, review comments, failed checks, failing test "
    "output), it is authoritative: your output must resolve every item in it."
)

# Context files are sent whole or not at all; a silently truncated file would
# make the model edit code it has not fully seen.
_CONTEXT_BUDGET_BYTES = 120_000

# Learned from a live run: a connection-per-call design looks right in review and fails every test here.
_MEMORY_DB_NOTE = (
    ". With ':memory:' every new sqlite3 connection opens its own empty database, so the application must "
    "create the schema on one connection and reuse it; tests must not assume a database file exists"
)


def project_conventions(ctx: AgentContext) -> dict[str, Any]:
    """The constraints the deterministic stages will check, stated up front.

    A model cannot guess that the documentation stage imports a particular
    factory, or that coverage is measured on a particular package; telling it
    turns would-be gate failures into first-pass successes.
    """
    profile, policy = ctx.profile, ctx.policy
    module, factory = profile.app_factory.split(":")
    return {
        "source_package": f"all application code lives in the Python package '{profile.source_package}/'",
        "tests": f"pytest tests live in '{profile.test_dir}/' and are run from the repository root",
        "application_factory": (
            f"'{module}.{factory}()' must build and return the FastAPI application when called with no arguments"
        ),
        "environment": {
            name: f"must be honoured by the application; the documentation stage sets it to {value!r}"
            + (_MEMORY_DB_NOTE if value == ":memory:" else "")
            for name, value in profile.export_env.items()
        },
        "allowed_third_party_imports": policy.allowed_third_party,
        "acceptance": [
            f"the whole test-suite passes and line coverage of '{profile.source_package}' is at least "
            f"{policy.min_coverage}%",
            "every functional requirement id (e.g. FR-1) appears in the docstring of at least one test",
            "SQL uses bound parameters only; no eval/exec/shell; no secrets in code",
        ],
        "reserved_for_documentation_stage": profile.docs_paths,
    }


class ModelAgent:
    role: str = ""
    instructions: str = ""
    schema: type[BaseModel] = BaseModel

    def payload(self, ctx: AgentContext) -> dict[str, Any]:
        raise NotImplementedError

    def finish(self, ctx: AgentContext, output: Any) -> AgentResult:
        raise NotImplementedError

    def run(self, ctx: AgentContext) -> AgentResult:
        payload = self.payload(ctx)
        if ctx.feedback:
            payload["feedback"] = ctx.feedback
        response = ctx.provider.generate(
            ProviderRequest(
                role=self.role,
                node_id=ctx.spec.id,
                instructions=f"{_COMMON}\n\n{self.instructions}",
                payload=payload,
                schema=self.schema,
                invocation=ctx.invocation,
            )
        )
        result = self.finish(ctx, response.output)
        result.model, result.usage = response.model, response.usage
        return result


class RequirementsAnalyst(ModelAgent):
    role = "requirements-analyst"
    schema = RequirementSpec
    instructions = (
        "Role: requirements analyst. Turn the raw requirement into a normalised engineering problem.\n"
        "- Write functional requirements (FR-n) and non-functional requirements (NFR-n), each with "
        "testable acceptance criteria.\n"
        "- Do not guess silently. Anything a reasonable engineer would have to assume goes into "
        "open_questions with concrete options and the option you recommend, or into assumptions if it "
        "is low-risk.\n"
        "- When feedback answers a question, apply the answer, record it as an assumption with "
        "source 'human', and remove the question.\n"
        "- Requirement ids must be unique across the codebase: continue numbering after "
        "existing_requirement_ids, never reuse one.\n"
        "- State what is out of scope."
    )

    def payload(self, ctx: AgentContext) -> dict[str, Any]:
        ws = ctx.store.workspace
        existing: set[str] = set()
        for path in ws.files():
            if path.startswith(f"{ctx.profile.test_dir}/") and path.endswith(".py"):
                existing.update(re.findall(r"\bN?FR-\d+\b", ws.read(path)))
        return {
            "requirement": ctx.requirement,
            "scenario": ctx.store.state.scenario,
            "existing_codebase": bool(ctx.store.baseline()),
            "existing_requirement_ids": sorted(existing),
        }

    def finish(self, ctx: AgentContext, output: RequirementSpec) -> AgentResult:
        decisions = [
            {
                "title": f"Assumption {a.id}",
                "rationale": a.statement,
                "actor": "human" if a.source == "human" else f"agent:{self.role}",
            }
            for a in output.assumptions
        ]
        return AgentResult(artifacts={"requirement_spec": output.model_dump()}, decisions=decisions)


class Architect(ModelAgent):
    role = "architect"
    schema = Design
    instructions = (
        "Role: software architect. Design the smallest change that satisfies the requirement spec.\n"
        "- For an existing codebase, reason from codebase_map: list every existing file you will "
        "touch in impacted_modules, using paths that exist in the map.\n"
        "- Record each significant choice as a decision with the alternatives you rejected.\n"
        "- requirement_coverage must contain one entry per requirement id.\n"
        "- Every risk needs a mitigation; rollback_plan must say how the change is undone.\n"
        "- Never design storage of raw personal data (IP addresses, emails)."
    )

    def payload(self, ctx: AgentContext) -> dict[str, Any]:
        return {
            "requirement_spec": ctx.inputs["requirement_spec"],
            "codebase_map": ctx.inputs["codebase_map"],
            "project_conventions": project_conventions(ctx),
        }

    def finish(self, ctx: AgentContext, output: Design) -> AgentResult:
        decisions = [
            {
                "title": f"{d.id}: {d.title}",
                "rationale": f"{d.decision} Consequences: {d.consequences}",
                "alternatives": d.alternatives,
                "actor": f"agent:{self.role}",
            }
            for d in output.decisions
        ]
        impact = impact_report(output.impacted_modules, ctx.inputs["codebase_map"])
        return AgentResult(artifacts={"design": output.model_dump(), "impact_report": impact}, decisions=decisions)


def impact_report(impacted: list[str], codebase: dict[str, Any]) -> dict[str, Any]:
    """Blast radius computed from the real import graph, not from the model's claim."""
    known = {m["path"] for m in codebase.get("modules", [])}
    dependents: dict[str, list[str]] = codebase.get("dependents", {})
    radius: set[str] = set()
    frontier = [p for p in impacted if p in known]
    while frontier:
        for dependent in dependents.get(frontier.pop(), []):
            if dependent not in radius:
                radius.add(dependent)
                frontier.append(dependent)
    tests = set(codebase.get("tests", []))
    return {
        "impacted_modules": sorted(impacted),
        "unknown_modules": sorted(p for p in impacted if p not in known),
        "transitive_dependents": sorted(radius - tests - set(impacted)),
        "affected_tests": sorted(radius & tests),
        "affected_routes": sorted(
            f"{r['method']} {r['path']}" for r in codebase.get("routes", []) if r["file"] in set(impacted) | radius
        ),
    }


class Planner(ModelAgent):
    role = "planner"
    schema = Plan
    instructions = (
        "Role: delivery planner. Decompose the design into tasks that form a dependency graph.\n"
        "- Each task lists requirement_ids it serves, depends_on, and every file it writes. A task "
        "writes its production code together with the tests that prove it.\n"
        "- Tasks with no dependency between them run in parallel, so they must not write the same "
        "file; add a dependency where order matters.\n"
        "- Mark schema changes with kind 'migration'.\n"
        "- Do not plan writes to reserved_paths; documentation is produced by a later stage."
    )

    def payload(self, ctx: AgentContext) -> dict[str, Any]:
        return {
            "requirement_spec": ctx.inputs["requirement_spec"],
            "design": ctx.inputs["design"],
            "impact_report": ctx.inputs["impact_report"],
            "existing_files": ctx.inputs["codebase_map"].get("files", []),
            "reserved_paths": ctx.profile.docs_paths,
            "project_conventions": project_conventions(ctx),
        }

    def finish(self, ctx: AgentContext, output: Plan) -> AgentResult:
        return AgentResult(artifacts={"plan": output.model_dump()})


class Implementer(ModelAgent):
    role = "implementer"
    schema = ChangeSet
    instructions = (
        "Role: implementation engineer. Implement exactly one task as a change set.\n"
        "- Touch only the files in task.writes. Use 'write' with full content for new files and "
        "'edit' with exact search/replace pairs for existing ones.\n"
        "- Write production-quality code with tests; reference the requirement ids a test covers "
        "in its docstring.\n"
        "- Use bound parameters for SQL, no new third-party dependencies, no secrets in code.\n"
        "- If feedback contains failing test output, fix the cause rather than the test."
    )

    def payload(self, ctx: AgentContext) -> dict[str, Any]:
        task = ctx.spec.params["task"]
        spec = ctx.store.get_artifact("requirement_spec") or {}
        design = ctx.store.get_artifact("design") or {}
        wanted = set(task["requirement_ids"])
        files, omitted, used = {}, [], 0
        ws, package = ctx.store.workspace, ctx.profile.source_package
        # Most relevant first: the task's own files, the files its dependencies just
        # wrote (the interfaces it must call), files the design names, then the rest
        # of the package and shared test fixtures.
        dependency_files = [
            p for name, change in ctx.inputs.items() if name.startswith("changeset:") for p in change["files"]
        ]
        candidates = list(
            dict.fromkeys(
                list(task["writes"])
                + dependency_files
                + design.get("impacted_modules", [])
                + [p for p in ws.files() if p.startswith(f"{package}/") or p.endswith("conftest.py")]
            )
        )
        for path in candidates:
            if not ws.exists(path):
                continue
            content = ctx.store.workspace.read(path)
            if used + len(content) > _CONTEXT_BUDGET_BYTES:
                omitted.append(path)
                continue
            files[path] = content
            used += len(content)
        return {
            "task": task,
            "requirements": [
                r for r in spec.get("functional", []) + spec.get("non_functional", []) if r["id"] in wanted
            ],
            # decisions included: an approved ADR the implementer never sees is one it can silently contradict
            "design": {k: design.get(k) for k in ("overview", "components", "api", "data_model", "decisions")},
            "current_files": files,
            "files_not_shown": omitted,
            "repository_files": ws.files(),
            "project_conventions": project_conventions(ctx),
        }

    def finish(self, ctx: AgentContext, output: ChangeSet) -> AgentResult:
        return AgentResult(changeset=output)
