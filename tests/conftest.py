"""Test harness for the orchestration engine.

The engine is exercised against the *real* SDLC graph, gates, policy, workspace
and audit log. Only the two slow or external pieces are replaced: the model
(by a programmable in-memory provider) and the agents that shell out to pytest
and to the application under construction (by stubs with the same contract).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentic_sdlc.agents import default_agents
from agentic_sdlc.agents.base import AgentContext, AgentResult
from agentic_sdlc.approvals import DeferredApprovals
from agentic_sdlc.engine import Orchestrator
from agentic_sdlc.faults import FaultInjector
from agentic_sdlc.models import ApprovalDecision, ApprovalRequest
from agentic_sdlc.policy import Policy, ProjectProfile
from agentic_sdlc.providers.base import ProviderRequest, ProviderResponse
from agentic_sdlc.schemas import ChangeSet, FileChange
from agentic_sdlc.store import RunStore
from agentic_sdlc.workflow import build_sdlc_graph

SPEC = {
    "title": "Sample change",
    "summary": "A minimal change used to exercise the engine.",
    "scenario_type": "greenfield",
    "functional": [{"id": "FR-1", "statement": "It works.", "acceptance_criteria": ["works"], "priority": "must"}],
    "non_functional": [],
    "open_questions": [],
    "assumptions": [{"id": "A1", "statement": "Small scope.", "source": "analyst"}],
    "out_of_scope": [],
}

DESIGN = {
    "overview": "Three small modules.",
    "components": [{"name": "a", "path": "app/a.py", "responsibility": "a", "change": "new"}],
    "api": [],
    "data_model": [],
    "impacted_modules": [],
    "decisions": [
        {
            "id": "ADR-1",
            "title": "Keep it small",
            "context": "c",
            "decision": "d",
            "alternatives": ["bigger"],
            "consequences": "none",
        }
    ],
    "risks": [{"id": "R1", "description": "r", "likelihood": "low", "impact": "low", "mitigation": "m"}],
    "requirement_coverage": [{"requirement_id": "FR-1", "approach": "modules a, b, c"}],
    "rollback_plan": "Delete the files.",
    "limitations": [],
}


def task(
    task_id: str, writes: list[str], depends_on: tuple[str, ...] = (), kind: str = "code", description: str = ""
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "description": description or f"Write {writes}",
        "kind": kind,
        "requirement_ids": ["FR-1"],
        "depends_on": list(depends_on),
        "writes": writes,
    }


def plan(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "s", "test_strategy": "t", "tasks": list(tasks)}


# T1 and T2 are independent (parallel); T3 needs both (join inside the plan).
PLAN = plan(task("T1", ["app/a.py"]), task("T2", ["app/b.py"]), task("T3", ["app/c.py"], ("T1", "T2")))


def write(path: str, content: str = "VALUE = 1\n") -> dict[str, Any]:
    return {"rationale": f"write {path}", "changes": [{"path": path, "action": "write", "content": content}]}


def default_script() -> dict[str, list[Any]]:
    return {
        "requirements": [SPEC],
        "architecture": [DESIGN],
        "planning": [PLAN],
        "impl:T1": [write("app/a.py")],
        "impl:T2": [write("app/b.py")],
        "impl:T3": [write("app/c.py")],
    }


class FakeProvider:
    """node id -> sequence of outputs. An item may be a dict, an exception to raise,
    or a callable taking the request. The n-th call for a node gets the n-th item
    (the last one repeats)."""

    name = "fake"

    def __init__(self, script: dict[str, list[Any]]):
        self.script = script
        self.calls: list[ProviderRequest] = []
        self._lock = threading.Lock()

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        with self._lock:
            index = sum(1 for call in self.calls if call.node_id == request.node_id)
            self.calls.append(request)
        sequence = self.script[request.node_id]
        item = sequence[min(index, len(sequence) - 1)]
        if callable(item):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        return ProviderResponse(output=request.schema.model_validate(item), model="fake-model")

    def calls_for(self, node_id: str) -> list[ProviderRequest]:
        return [call for call in self.calls if call.node_id == node_id]


class StubVerifier:
    """Stands in for the pytest runner: the 'suite' fails for every file containing BUG."""

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.store.workspace
        bad = [p for p in ws.files() if "BUG" in ws.read(p)]
        report = {
            "passed": not bad, "exit_code": 1 if bad else 0, "tests": 3, "failures": len(bad), "errors": 0, "skipped": 0,
            # like a real traceback, the detail quotes the failing source
            "failed_tests": [{"test": "tests/test_app.py::test_it", "kind": "failure", "message": f"assert failed in {p}", "detail": ws.read(p)} for p in bad],
            "coverage": {"percent": 100.0, "files": {}}, "output_tail": "",
        }  # fmt: skip
        trace = {
            "matrix": {"FR-1": ["tests/test_app.py::test_it"]},
            "untested_functional": [],
            "untested_non_functional": [],
        }
        return AgentResult(artifacts={"test_report": report, "traceability": trace})


class StubDocWriter:
    """Stands in for the documentation agent (which would import the app to export OpenAPI)."""

    def __init__(self, text: str = "# Sample\n"):
        self.text = text

    def run(self, ctx: AgentContext) -> AgentResult:
        return AgentResult(
            artifacts={"docs_report": {"openapi_error": "", "endpoints": ["GET /sample"]}},
            changeset=ChangeSet(
                rationale="docs", changes=[FileChange(path="README.md", action="write", content=self.text)]
            ),
        )


class Approver:
    """Approves everything in the right role, unless a scripted decision is queued for the checkpoint."""

    def __init__(self, script: dict[str, list[dict[str, Any] | None]] | None = None):
        self.script = script or {}
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> ApprovalDecision | None:
        self.requests.append(request)
        queue = self.script.get(request.checkpoint.split(":")[0], [])
        entry = queue.pop(0) if queue else {"decision": "approve"}
        if entry is None:
            return None
        return ApprovalDecision(**{"by": "tester", "role": request.role, **entry})


@dataclass
class Harness:
    store: RunStore
    provider: FakeProvider
    target: Path
    policy: Policy
    agents: dict[str, Any]
    faults: list[dict[str, Any]]
    slept: list[float] = field(default_factory=list)

    def orchestrator(self, approvals: Any = None) -> Orchestrator:
        return Orchestrator(
            self.store,
            build_sdlc_graph(),
            self.agents,
            self.provider,
            approvals or Approver(),
            policy=self.policy,
            profile=ProjectProfile(source_package="app"),
            faults=FaultInjector(self.faults, self.store.state.fault_counts),
            sleep=self.slept.append,
        )

    def run(self, approvals: Any = None):
        return self.orchestrator(approvals).run()

    def reload(self) -> Harness:
        """Drop all in-memory state, as a new process resuming the run would."""
        self.store = RunStore.load(self.store.dir)
        return self

    def decide(self, decision: str = "approve", role: str | None = None, **extra: Any) -> ApprovalRequest:
        """Record a human decision for the single pending checkpoint (what `sdlc approve` does)."""
        (request,) = [a for a in self.store.state.approvals if a.status == "pending"]
        self.store.state.inbox[request.id] = ApprovalDecision(
            decision=decision, by="tester", role=role or request.role, **extra
        )
        self.store.save()
        return request

    # ---- inspection helpers
    def events(self, prefix: str = "", node: str | None = None) -> list[dict[str, Any]]:
        return [
            e for e in self.store.audit.read() if e["type"].startswith(prefix) and (node is None or e["node"] == node)
        ]

    def status(self, node_id: str) -> str:
        return self.store.state.nodes[node_id].status.value

    def target_files(self) -> list[str]:
        return sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob("*") if p.is_file())


@pytest.fixture
def make_harness(tmp_path):
    made: list[int] = []

    def _make(
        script: dict[str, list[Any]] | None = None,
        policy: Policy | None = None,
        agents: dict[str, Any] | None = None,
        faults: list[dict[str, Any]] | None = None,
        seed: dict[str, str] | None = None,
    ) -> Harness:
        made.append(1)
        root = tmp_path / f"case{len(made)}"
        target = root / "target"
        for rel, content in (seed or {}).items():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            (target / rel).write_bytes(content.encode())
        store = RunStore.create(root / "runs", "test", "Do the sample change.", target, "fake", run_id="run")
        registry = {
            **default_agents(),
            "verifier": StubVerifier(),
            "doc_writer": StubDocWriter(),
            "doc_template": StubDocWriter("# Fallback\n"),
        }
        registry.update(agents or {})
        return Harness(
            store,
            FakeProvider({**default_script(), **(script or {})}),
            target,
            policy or Policy(),
            registry,
            faults or [],
        )

    return _make


@pytest.fixture
def deferred():
    return DeferredApprovals()
