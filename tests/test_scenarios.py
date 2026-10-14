"""End-to-end: the three scenario playbooks through the real pipeline.

Nothing is stubbed here. The greenfield run is driven through the CLI with real
(deferred) human checkpoints; brownfield and ambiguous use the playbooks'
simulated reviewers. The generated service's own test-suite is executed by the
verification stage each time, so this module takes about fifteen seconds.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_sdlc.cli import REPO_ROOT, UsageError, _drive, main, start_run
from agentic_sdlc.metrics import aggregate, run_metrics
from agentic_sdlc.store import RunStore
from agentic_sdlc.workspace import list_tree, restore_promotion

SCENARIOS = ("greenfield", "brownfield", "ambiguous")


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    root = tmp_path_factory.mktemp("pipeline")
    runs, target = root / "runs", root / "service"

    def sdlc(*args: str) -> int:
        return main(["--runs", str(runs), *args])

    codes = [sdlc("run", "--scenario", "greenfield", "--target", str(target), "--run-id", "greenfield")]
    sdlc("approve", "greenfield", "--decision", "approve", "--by", "pytest", "--comment", "plan ok")
    codes.append(sdlc("resume", "greenfield"))
    target_before_release = list_tree(target)
    sdlc("approve", "greenfield", "--decision", "approve", "--by", "pytest", "--role", "release-manager")
    codes.append(sdlc("resume", "greenfield"))

    for scenario in ("brownfield", "ambiguous"):
        store = start_run(runs, target, scenario, run_id=scenario)
        codes.append(_drive(store, "scripted", "pytest", verbose=False, quiet=True))

    stores = {name: RunStore.load(runs / name) for name in SCENARIOS}
    return SimpleNamespace(root=root, runs=runs, target=target, codes=codes, stores=stores, sdlc=sdlc,
                           target_before_release=target_before_release)  # fmt: skip


def events(store: RunStore, prefix: str, node: str | None = None):
    return [e for e in store.audit.read() if e["type"].startswith(prefix) and (node is None or e["node"] == node)]


def test_cli_run_pauses_at_every_human_checkpoint(pipeline):
    assert pipeline.codes[:3] == [3, 3, 0]  # paused for plan sign-off, paused for release, done
    assert pipeline.target_before_release == []  # nothing reached the target before release approval
    approvals = pipeline.stores["greenfield"].state.approvals
    assert [(a.checkpoint, a.status, a.decision.by) for a in approvals] == [
        ("plan-signoff", "approved", "pytest"),
        ("release", "approved", "pytest"),
    ]


def test_all_three_scenarios_succeed_with_intact_audit_trails(pipeline):
    assert pipeline.codes[3:] == [0, 0]
    for name, store in pipeline.stores.items():
        assert store.state.status.value == "succeeded", name
        assert store.audit.verify()[0], name
        summary = (store.dir / "ENGINEERING_SUMMARY.md").read_text(encoding="utf-8")
        for heading in (
            "## 1. Requirement",
            "## 2. Plan",
            "## 5. Validation",
            "## 6. Risks",
            "## 7. Governance",
            "## 8. Reliability",
        ):
            assert heading in summary, (name, heading)


def test_greenfield_decomposes_and_recovers_from_a_transient_fault(pipeline):
    store = pipeline.stores["greenfield"]
    assert sorted(store.state.dynamic_nodes) == [f"impl:T{i}" for i in range(1, 7)]
    assert events(store, "fault.injected", "architecture")
    assert events(store, "node.attempt_failed", "architecture")[0]["data"]["will_retry"] is True
    # T2, T3 and T6 only depend on T1, so all three were started before T4 (which needs T2 and T3)
    started = [e["node"] for e in events(store, "node.started") if e["node"].startswith("impl:")]
    assert set(started[1:4]) == {"impl:T2", "impl:T3", "impl:T6"} and started.index("impl:T4") > 3
    assert store.get_artifact("codebase_map")["empty"] is True


def test_brownfield_reasons_about_the_codebase_and_guards_the_migration(pipeline):
    store = pipeline.stores["brownfield"]
    impact = store.get_artifact("impact_report")
    assert impact["unknown_modules"] == []  # every module the design names really exists
    assert (
        "urlshortener/main.py" in impact["transitive_dependents"]
    )  # found via the import graph, not named by the design
    assert "POST /api/v1/links" in impact["affected_routes"] and "tests/test_api.py" in impact["affected_tests"]

    (control,) = [a for a in store.state.approvals if a.checkpoint.startswith("change-control")]
    assert (control.node, control.phase, control.role, control.status) == (
        "impl:T1",
        "entry",
        "change-advisory",
        "approved",
    )
    assert events(store, "approval.decided", "impl:T1")[0]["seq"] < events(store, "node.started", "impl:T1")[0]["seq"]


def test_brownfield_seeded_defect_is_caught_rolled_back_and_reworked(pipeline):
    store = pipeline.stores["brownfield"]
    rejected, final = store.get_artifact("rejected:test_report"), store.get_artifact("test_report")
    assert rejected["passed"] is False  # the failing report is kept as evidence
    assert any("test_link_is_gone_at_the_exact_expiry_instant" in f["test"] for f in rejected["failed_tests"])
    assert final["passed"] is True and final["failures"] == 0

    (rework,) = events(store, "rework.triggered")
    assert "impl:T3" in rework["data"]["targets"] and "impl:T1" not in rework["data"]["targets"]
    assert {e["node"] for e in events(store, "workspace.rolled_back")} >= {"impl:T3", "impl:T4", "documentation"}
    assert store.state.nodes["impl:T1"].executions == 1  # the approved migration was not redone
    metrics = run_metrics(store.audit.read())
    assert metrics["rework_rounds"] == 1 and metrics["incidents_recovered"] >= 1 and metrics["mttr_s"] > 0


def test_ambiguous_requirement_is_clarified_before_design(pipeline):
    store = pipeline.stores["ambiguous"]
    draft, final = store.get_artifact("requirement_spec", 1), store.get_artifact("requirement_spec")
    assert [q["id"] for q in draft["open_questions"]] == ["Q1", "Q2", "Q3"]
    assert final["open_questions"] == []
    assert [a["id"] for a in final["assumptions"] if a["source"] == "human"] == ["A21", "A22", "A23"]

    clarification = store.state.approvals[0]
    assert (clarification.checkpoint, clarification.kind, clarification.status) == (
        "clarification",
        "clarification",
        "changes_requested",
    )
    assert set(clarification.decision.answers) == {"Q1", "Q2", "Q3"}
    # the architect did not start until the product owner had answered
    assert (
        events(store, "node.started", "architecture")[0]["seq"]
        > events(store, "approval.decided", "requirements")[0]["seq"]
    )


def test_ambiguous_review_feedback_changes_the_design_and_replans(pipeline):
    store = pipeline.stores["ambiguous"]
    assert len(store.state.artifacts["design"]) == 2 and store.state.plan_version == 2
    replan = events(store, "plan.expanded")[1]["data"]
    assert replan["changed"] == ["impl:T1", "impl:T4", "impl:T5"] and replan["kept"] == ["impl:T2", "impl:T3"]
    signoffs = [a.status for a in store.state.approvals if a.checkpoint == "plan-signoff"]
    assert signoffs == ["changes_requested", "approved"]
    # no implementation started before the revised plan was approved
    approved_at = [e for e in events(store, "approval.decided", "planning") if e["data"]["decision"] == "approve"][0][
        "seq"
    ]
    assert min(e["seq"] for e in events(store, "node.started") if e["node"].startswith("impl:")) > approved_at
    # and the reviewer's privacy objection is what the final code implements
    assert "hmac" in (pipeline.target / "urlshortener" / "privacy.py").read_text(encoding="utf-8")


def test_final_service_meets_the_release_bar(pipeline):
    store = pipeline.stores["ambiguous"]
    tests, security = store.get_artifact("test_report"), store.get_artifact("security_report")
    assert tests["passed"] and tests["tests"] >= 200 and tests["coverage"]["percent"] >= 85
    assert security["counts"]["high"] == 0
    assert store.get_artifact("traceability")["untested_functional"] == []
    assert all(item["ok"] for item in store.get_artifact("release_report")["checklist"])
    assert "GET /{code}" in store.get_artifact("docs_report")["endpoints"]


def test_fleet_metrics_cover_the_required_measures(pipeline):
    fleet = aggregate([run_metrics(store.audit.read()) for store in pipeline.stores.values()])
    assert fleet["runs"] == 3 and fleet["success_rate"] == 1.0
    assert fleet["retry_frequency"] > 0 and fleet["rollbacks_per_run"] > 0
    assert fleet["mttr_s"] > 0 and fleet["latency_p50_s"] > 0 and fleet["latency_p95_s"] >= fleet["latency_p50_s"]


def test_release_can_be_rolled_back_to_the_previous_version(pipeline, tmp_path):
    copy = tmp_path / "service"
    shutil.copytree(pipeline.target, copy)

    restore_promotion(copy, pipeline.stores["ambiguous"].dir / "promotion")

    assert not (copy / "urlshortener" / "ratelimit.py").exists()
    assert '__version__ = "1.1.0"' in (copy / "urlshortener" / "__init__.py").read_text(encoding="utf-8")


def test_scenarios_refuse_the_wrong_baseline(pipeline, tmp_path):
    with pytest.raises(UsageError, match="missing"):
        start_run(tmp_path / "runs", tmp_path / "empty", "brownfield")
    with pytest.raises(UsageError, match="not empty"):
        start_run(tmp_path / "runs", pipeline.target, "greenfield")
    with pytest.raises(UsageError, match="already"):
        start_run(tmp_path / "runs", pipeline.target, "ambiguous")


def test_answers_are_refused_with_a_decision_that_would_drop_them(tmp_path, capsys):
    runs = tmp_path / "runs"
    store = start_run(runs, tmp_path / "service", "greenfield", run_id="g")
    assert _drive(store, "deferred", "pytest", verbose=False, quiet=True) == 3  # paused at plan sign-off

    code = main(["--runs", str(runs), "approve", "g", "--decision", "approve", "--by", "x", "--answer", "Q1=a"])

    assert code == 1 and "--decision changes" in capsys.readouterr().err
    assert RunStore.load(runs / "g").state.inbox == {}  # nothing was recorded


def test_resume_can_extend_budgets_and_the_extension_is_audited(tmp_path):
    runs = tmp_path / "runs"
    store = start_run(runs, tmp_path / "service", "greenfield", run_id="g")
    assert _drive(store, "deferred", "pytest", verbose=False, quiet=True) == 3

    code = main(["--runs", str(runs), "resume", "g", "--max-invocations", "70", "--max-wall-clock", "3600",
                 "--approver", "ops"])  # fmt: skip

    assert code == 3  # still waiting for plan sign-off
    store = RunStore.load(runs / "g")
    assert store.state.options["policy"] == {"max_agent_invocations": 70, "max_wall_clock_s": 3600.0}
    [extension] = events(store, "budget.extended")
    assert extension["actor"] == "human:ops"
    assert extension["data"] == {"max_agent_invocations": 70, "max_wall_clock_s": 3600.0}


def test_inspection_commands(pipeline, capsys):
    assert pipeline.sdlc("audit", "brownfield", "--verify") == 0
    assert pipeline.sdlc("status", "ambiguous") == 0
    assert pipeline.sdlc("lineage", "ambiguous", "release_report") == 0
    assert pipeline.sdlc("metrics") == 0
    assert pipeline.sdlc("graph", "greenfield") == 0
    assert pipeline.sdlc("report", "latest") == 0
    assert pipeline.sdlc("list") == 0
    output = capsys.readouterr().out
    assert "audit chain: OK" in output
    assert (
        "requirement_spec v2" in output and "produced by requirements" in output
    )  # lineage reaches the clarified spec
    assert '"success_rate": 1.0' in output
    assert "impl_T2 --> impl_T4" in output and "impl_T3 --> impl_T4" in output  # the run's dynamic task graph


def test_committed_service_is_exactly_what_the_pipeline_produces(pipeline):
    """The service/ directory in the repository is the pipeline's output, not hand-edited code."""
    committed = REPO_ROOT / "service"
    if not committed.exists():
        pytest.skip("service/ has not been generated yet")
    # The OpenAPI export (and the reference rendered from it) varies with the FastAPI version installed.
    derived = {"openapi.json", "docs/API.md"}

    def tree(root: Path) -> dict[str, bytes]:
        return {p: (root / p).read_bytes().replace(b"\r\n", b"\n") for p in list_tree(root) if p not in derived}

    produced, expected = tree(pipeline.target), tree(committed)
    assert sorted(produced) == sorted(expected)
    assert [p for p in produced if produced[p] != expected[p]] == []
