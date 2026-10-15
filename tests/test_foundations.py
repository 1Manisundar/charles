"""Audit log, sandboxed workspace, policy scanners, graph and plan gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_sdlc.agents.codebase import CodebaseAnalyst
from agentic_sdlc.agents.model_agents import impact_report
from agentic_sdlc.agents.verifier import _parse_junit
from agentic_sdlc.audit import AuditLog
from agentic_sdlc.faults import parse_fault
from agentic_sdlc.gates import GATES, GateContext
from agentic_sdlc.graph import GraphError, WorkflowGraph
from agentic_sdlc.models import AgentOutputError, NodeSpec, PolicyViolation
from agentic_sdlc.policy import Policy, ProjectProfile, scan_file
from agentic_sdlc.providers.scripted import parse_edit_blocks
from agentic_sdlc.schemas import ChangeSet
from agentic_sdlc.store import RunStore
from agentic_sdlc.workflow import build_sdlc_graph, task_node
from agentic_sdlc.workspace import Workspace, restore_promotion

from .conftest import plan, task

# ====================================================================== audit log


def _log(tmp_path) -> AuditLog:
    log = AuditLog(tmp_path / "audit.jsonl", "run-1")
    for index in range(5):
        log.append("node.started", node=f"n{index}", attempt=1)
    return log


def test_audit_chain_verifies_and_survives_reopening(tmp_path):
    log = _log(tmp_path)
    assert log.verify() == (True, "5 records, chain intact")

    reopened = AuditLog(tmp_path / "audit.jsonl", "run-1")
    record = reopened.append("run.finished", status="succeeded")

    assert record["seq"] == 6 and record["prev"] == log.read()[4]["hash"]
    assert reopened.verify()[0]


@pytest.mark.parametrize("tamper", ["edit", "delete", "reorder"])
def test_audit_tampering_is_detected(tmp_path, tamper):
    log = _log(tmp_path)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    if tamper == "edit":
        record = json.loads(lines[2])
        record["data"]["attempt"] = 99
        lines[2] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    elif tamper == "delete":
        del lines[2]
    else:
        lines[1], lines[2] = lines[2], lines[1]
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, reason = log.verify()

    assert not ok and reason


def test_audit_is_safe_under_concurrent_writers(tmp_path):
    import threading

    log = AuditLog(tmp_path / "audit.jsonl", "run-1")
    threads = [threading.Thread(target=lambda: [log.append("tick") for _ in range(50)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert log.verify() == (True, "400 records, chain intact")


# ====================================================================== workspace


@pytest.fixture
def ws(tmp_path) -> Workspace:
    workspace = Workspace(tmp_path / "ws", tmp_path / "journal")
    workspace.apply("seed", changes({"a.py": "one\ntwo\n", "b.py": "keep\n"}))
    return workspace


def changes(
    writes: dict[str, str] | None = None,
    edits: dict[str, list[tuple[str, str]]] | None = None,
    deletes: list[str] | None = None,
) -> ChangeSet:
    items = [{"path": p, "action": "write", "content": c} for p, c in (writes or {}).items()]
    items += [
        {"path": p, "action": "edit", "edits": [{"search": s, "replace": r} for s, r in e]}
        for p, e in (edits or {}).items()
    ]
    items += [{"path": p, "action": "delete"} for p in deletes or []]
    return ChangeSet.model_validate({"rationale": "test", "changes": items})


def test_rollback_restores_writes_edits_and_deletes(ws):
    ws.apply("task", changes({"new.py": "x\n"}, {"a.py": [("two", "2")]}, ["b.py"]))
    assert (ws.read("a.py"), ws.exists("b.py"), ws.exists("new.py")) == ("one\n2\n", False, True)

    restored = ws.rollback("task")

    assert restored == ["a.py", "b.py", "new.py"]
    assert (ws.read("a.py"), ws.read("b.py"), ws.exists("new.py")) == ("one\ntwo\n", "keep\n", False)
    assert ws.applied_owners() == ["seed"]


def test_a_bad_edit_leaves_the_workspace_untouched(ws):
    bad = changes({"new.py": "x\n"}, {"a.py": [("does not occur", "y")]})

    with pytest.raises(AgentOutputError, match="matched 0 times"):
        ws.apply("task", bad)

    assert not ws.exists("new.py") and ws.applied_owners() == ["seed"]


def test_ambiguous_edit_is_refused(ws):
    ws.apply("task", changes({"dup.py": "x = 1\nx = 1\n"}))
    with pytest.raises(AgentOutputError, match="matched 2 times"):
        ws.apply("task2", changes(edits={"dup.py": [("x = 1", "x = 2")]}))


@pytest.mark.parametrize(
    "path", ["../x", "a/../../x", "/abs", "C:\\x", "", ".git/config", "pkg/__pycache__/x.pyc", "data.db"]
)
def test_paths_outside_the_sandbox_are_refused(ws, path):
    with pytest.raises(PolicyViolation):
        ws.resolve(path)


def test_relative_locations_are_made_absolute(tmp_path, monkeypatch):
    """Tools run with the sandbox as working directory, so a relative root would be misread."""
    monkeypatch.chdir(tmp_path)
    workspace = Workspace(Path("runs/r1/workspace"), Path("runs/r1/journal"))
    store = RunStore.create(Path("runs"), "test", "requirement", Path("target"), "fake", run_id="r2")

    assert workspace.root.is_absolute() and workspace.journal_dir.is_absolute()
    assert store.dir.is_absolute() and store.workspace.root == (tmp_path / "runs/r2/workspace").resolve()


def test_line_endings_are_preserved_byte_for_byte(ws):
    ws.apply("task", changes({"unix.txt": "a\nb\n"}))
    assert (ws.root / "unix.txt").read_bytes() == b"a\nb\n"


def test_promotion_backs_up_and_can_be_undone(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "a.py").write_bytes(b"old\n")
    (target / "gone.py").write_bytes(b"bye\n")
    workspace = Workspace(tmp_path / "ws", tmp_path / "journal")
    baseline = workspace.seed_from(target)
    workspace.apply("task", changes({"a.py": "new\n", "added.py": "hi\n"}, deletes=["gone.py"]))

    delta = workspace.promote(target, baseline, tmp_path / "backup")

    assert delta == {"added": ["added.py"], "modified": ["a.py"], "deleted": ["gone.py"]}
    assert (target / "a.py").read_bytes() == b"new\n" and not (target / "gone.py").exists()

    restore_promotion(target, tmp_path / "backup")

    assert (target / "a.py").read_bytes() == b"old\n" and (target / "gone.py").read_bytes() == b"bye\n"
    assert not (target / "added.py").exists()


# ====================================================================== policy scanners


def rules(path: str, text: str) -> set[str]:
    return {f.rule for f in scan_file(path, text, Policy(), ProjectProfile())}


@pytest.mark.parametrize(
    "code, rule",
    [
        ("cur.execute(f\"SELECT * FROM links WHERE code = '{code}'\")", "injection/sql"),
        ('cur.execute("SELECT * FROM t WHERE c = %s" % code)', "injection/sql"),
        ('cur.execute("SELECT * FROM t WHERE c = " + code)', "injection/sql"),
        ('cur.execute("SELECT * FROM t WHERE c = {}".format(code))', "injection/sql"),
        ("eval(user_input)", "dangerous-call/eval"),
        ("import os\nos.system(cmd)", "dangerous-call/os.system"),
        ("import subprocess\nsubprocess.run(cmd, shell=True)", "dangerous-call/shell"),
        ("import pickle\npickle.loads(blob)", "dangerous-call/pickle.loads"),
        ("import requests", "supply-chain/unapproved-import"),
        ("from sqlalchemy import text", "supply-chain/unapproved-import"),
        ('AWS = "AKIAABCDEFGHIJKLMNOP"', "secret/aws-access-key"),
        ('password = "correct-horse-battery"', "secret/hardcoded-credential"),
        ('SCHEMA = "CREATE TABLE clicks (id INTEGER, ip TEXT)"', "compliance/raw-pii-column"),
        ('SCHEMA = "ALTER TABLE clicks ADD COLUMN client_ip TEXT"', "compliance/raw-pii-column"),
        ("def broken(:\n    pass", "python/syntax"),
    ],
)
def test_scanners_flag_violations(code, rule):
    assert rule in rules("urlshortener/x.py", code)


@pytest.mark.parametrize(
    "code",
    [
        'cur.execute("SELECT * FROM links WHERE code = ?", (code,))',
        "import sqlite3\nfrom fastapi import FastAPI\nfrom urlshortener.db import Database\nfrom . import codec",
        "import subprocess\nsubprocess.run(['ls'], shell=False)",
        'password = os.environ["DB_PASSWORD"]',
        'SCHEMA = "ALTER TABLE clicks ADD COLUMN visitor_hash TEXT"',
        'query = f"hello {name}"',
    ],
)
def test_scanners_accept_safe_code(code):
    assert rules("urlshortener/x.py", code) == set()


def test_only_changes_to_existing_protected_assets_are_high_impact():
    policy = Policy()
    migration = task("T1", ["urlshortener/migrations.py"], kind="migration")

    assert policy.high_impact_reasons("migration", migration["writes"], baseline=[]) == []  # greenfield
    reasons = policy.high_impact_reasons("migration", migration["writes"], baseline=["urlshortener/migrations.py"])
    assert len(reasons) == 2 and "protected path" in reasons[1]
    # the checkpoint is derived by the orchestrator from policy, not claimed by the planner
    assert task_node(migration, policy, ["urlshortener/migrations.py"]).approval.checkpoint == "change-control:T1"
    assert task_node(migration, policy, []).approval is None
    assert task_node(task("T2", ["urlshortener/api.py"]), policy, ["urlshortener/api.py"]).approval is None


# ====================================================================== graph and gates


def node(node_id: str, *deps: str) -> NodeSpec:
    return NodeSpec(id=node_id, title=node_id, stage="s", agent="a", depends_on=list(deps))


def test_graph_rejects_cycles_and_unknown_dependencies():
    with pytest.raises(GraphError, match="cycle"):
        WorkflowGraph([node("a", "b"), node("b", "a")])
    with pytest.raises(GraphError, match="unknown node"):
        WorkflowGraph([node("a", "ghost")])


def test_dynamic_expansion_is_validated_and_join_waits_for_children():
    graph = build_sdlc_graph()
    tasks = {
        f"impl:{t}": task_node(task(t, [f"{t}.py"], deps), Policy(), []) for t, deps in (("T1", ()), ("T2", ("T1",)))
    }
    graph.set_dynamic(tasks)

    assert set(graph.deps("implementation")) == {"planning", "impl:T1", "impl:T2"}
    assert graph.inputs("implementation") == ["changeset:T1", "changeset:T2"]
    order = graph.topological_order()
    assert order.index("impl:T1") < order.index("impl:T2") < order.index("implementation") < order.index("verification")
    assert graph.descendants("impl:T2")[0] == "implementation" and "promotion" in graph.descendants("impl:T2")

    cyclic = {**tasks, "impl:T1": task_node(task("T1", ["T1.py"], ("T2",)), Policy(), [])}
    with pytest.raises(GraphError):
        graph.set_dynamic(cyclic)
    assert set(graph.children("implementation")) == {"impl:T1", "impl:T2"}  # the previous shape is kept
    assert "rework" in graph.to_mermaid()


def run_gate(name: str, make_harness, **artifacts):
    harness = make_harness()
    for artifact, content in artifacts.items():
        harness.store.put_artifact(artifact, content, "test", {})
    graph = build_sdlc_graph()
    ctx = GateContext(graph["planning"], harness.store, graph, Policy(), ProjectProfile())
    return GATES[name](ctx)


def test_plan_gate_refuses_parallel_tasks_that_share_a_file(make_harness):
    racing = plan(task("T1", ["app/shared.py"]), task("T2", ["app/shared.py"]))
    ordered = plan(task("T1", ["app/shared.py"]), task("T2", ["app/shared.py"], ("T1",)))

    verdict = run_gate("plan_respects_ownership", make_harness, plan=racing)
    assert not verdict.passed and "may run in parallel but both write" in verdict.details
    assert run_gate("plan_respects_ownership", make_harness, plan=ordered).passed


def test_plan_gate_reserves_documentation_and_forbidden_paths(make_harness):
    verdict = run_gate(
        "plan_respects_ownership", make_harness, plan=plan(task("T1", ["README.md", ".env", "docs/x.md"]))
    )
    assert verdict.details.count("documentation stage") == 2 and "forbidden path .env" in verdict.details


def test_plan_gate_requires_two_way_traceability(make_harness):
    from .conftest import SPEC

    spec = {
        **SPEC,
        "functional": SPEC["functional"]
        + [{"id": "FR-2", "statement": "x", "acceptance_criteria": ["y"], "priority": "must"}],
    }
    orphan = plan(task("T1", ["a.py"]), {**task("T2", ["b.py"]), "requirement_ids": ["FR-9"]})

    verdict = run_gate("plan_traces_requirements", make_harness, requirement_spec=spec, plan=orphan)

    assert "T2 cites unknown requirement FR-9" in verdict.details and "requirement FR-2 has no task" in verdict.details


def test_design_gate_catches_hallucinated_modules(make_harness):
    verdict = run_gate("impacted_modules_exist", make_harness, impact_report={"unknown_modules": ["app/ghost.py"]})
    assert not verdict.passed and verdict.retryable


# ====================================================================== codebase reasoning


def test_codebase_analyst_maps_imports_routes_and_tables(make_harness):
    harness = make_harness(
        seed={
            "app/__init__.py": "",
            "app/db.py": 'SCHEMA = """CREATE TABLE links (code TEXT PRIMARY KEY, long_url TEXT NOT NULL);"""\n',
            "app/service.py": "from .db import SCHEMA\n\ndef create():\n    return SCHEMA\n",
            "app/api.py": (
                "from fastapi import APIRouter\nfrom app.service import create\n"
                'router = APIRouter(prefix="/api/v1")\n\n@router.post("/links")\ndef create_link():\n    return create()\n'
            ),
            "tests/conftest.py": "from app.api import router\n",
            "tests/test_api.py": "def test_it(client):\n    assert client\n",  # reaches the app only through fixtures
        }
    )
    ctx = type("Ctx", (), {"store": harness.store, "profile": ProjectProfile(source_package="app")})()

    codebase = CodebaseAnalyst().run(ctx).artifacts["codebase_map"]

    assert codebase["routes"] == [
        {"method": "POST", "path": "/api/v1/links", "handler": "create_link", "file": "app/api.py"}
    ]
    assert codebase["tables"] == [{"name": "links", "columns": ["code", "long_url"], "file": "app/db.py"}]
    assert codebase["dependents"]["app/db.py"] == ["app/service.py"]
    assert codebase["dependents"]["app/service.py"] == ["app/api.py"]

    impact = impact_report(["app/db.py", "app/ghost.py"], codebase)
    assert impact["transitive_dependents"] == ["app/api.py", "app/service.py"]  # blast radius via the import graph
    assert impact["affected_tests"] == [
        "tests/conftest.py",
        "tests/test_api.py",
    ]  # conftest fixtures count as a dependency
    assert impact["affected_routes"] == ["POST /api/v1/links"]
    assert impact["unknown_modules"] == ["app/ghost.py"]


# ====================================================================== small parsers


def test_edit_blocks_and_fault_specs_parse():
    text = "<<<<<<< SEARCH\nold\nline\n=======\nnew\n>>>>>>> REPLACE\n<<<<<<< SEARCH\ngone\n=======\n>>>>>>> REPLACE\n"
    assert parse_edit_blocks(text) == [{"search": "old\nline", "replace": "new"}, {"search": "gone", "replace": ""}]
    assert parse_fault("architecture:transient:2") == {"node": "architecture", "kind": "transient", "times": 2}
    assert parse_fault("impl:T1:permanent") == {"node": "impl:T1", "kind": "permanent", "times": 1}
    with pytest.raises(ValueError):
        parse_fault("architecture:explode")


def test_junit_failures_name_the_test_file_even_for_collection_errors(tmp_path):
    """Found in a live run: a module that failed to import was reported as '.py::tests.test_db'."""
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_api" name="test_ok"/>'
        '<testcase classname="tests.test_api" name="test_bad"><failure message="boom">trace</failure></testcase>'
        '<testcase classname="" name="tests.test_db"><error message="collection failure">ImportError</error></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    totals, failures = _parse_junit(junit)

    assert totals == {"tests": 3, "failures": 1, "errors": 1, "skipped": 0}
    assert [f["test"] for f in failures] == ["tests/test_api.py::test_bad", "tests/test_db.py"]
