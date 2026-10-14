"""Behaviour of the orchestration engine: scheduling, recovery, governance, re-planning."""

from __future__ import annotations

import threading

import pytest

from agentic_sdlc import gates
from agentic_sdlc.metrics import run_metrics
from agentic_sdlc.models import NodeStatus, PermanentError, RunStatus, TransientError
from agentic_sdlc.policy import Policy
from agentic_sdlc.schemas import ChangeSet
from agentic_sdlc.workspace import restore_promotion

from .conftest import DESIGN, PLAN, Approver, plan, task, write


def seq(harness, type_, node):
    """Audit sequence number of the first matching event."""
    return harness.events(type_, node)[0]["seq"]


# ====================================================================== orchestration


def test_happy_path_reaches_promotion(make_harness):
    harness = make_harness()

    state = harness.run()

    assert state.status is RunStatus.SUCCEEDED
    assert all(node.status.value == "succeeded" for node in state.nodes.values())
    assert harness.target_files() == ["README.md", "app/a.py", "app/b.py", "app/c.py"]
    assert harness.store.audit.verify()[0]


def test_independent_tasks_really_run_in_parallel(make_harness):
    """T1 and T2 each wait at a barrier that only opens when both are executing."""
    barrier = threading.Barrier(2, timeout=10)

    def meet(path):
        def respond(request):
            barrier.wait()
            return write(path)

        return respond

    harness = make_harness({"impl:T1": [meet("app/a.py")], "impl:T2": [meet("app/b.py")]})

    assert harness.run().status is RunStatus.SUCCEEDED
    assert not barrier.broken


def test_parallel_paths_synchronise_at_joins(make_harness):
    harness = make_harness()
    harness.run()

    # the plan's own dependency: T3 only after both T1 and T2
    assert seq(harness, "node.started", "impl:T3") > max(
        seq(harness, "node.succeeded", n) for n in ("impl:T1", "impl:T2")
    )
    # the implementation join waits for every task
    assert seq(harness, "node.started", "implementation") > seq(harness, "node.succeeded", "impl:T3")
    # verification, security and documentation fan out after the join ...
    fan_out = ("verification", "security_review", "documentation")
    assert all(seq(harness, "node.started", n) > seq(harness, "node.succeeded", "implementation") for n in fan_out)
    # ... and release readiness synchronises on all three
    assert seq(harness, "node.started", "release_readiness") > max(seq(harness, "node.succeeded", n) for n in fan_out)


def test_context_and_lineage_are_preserved_across_stages(make_harness):
    harness = make_harness()
    state = harness.run()

    plan_meta = state.artifacts["plan"][-1]
    assert plan_meta.produced_by == "planning"
    assert plan_meta.derived_from == {"requirement_spec": 1, "design": 1, "impact_report": 1, "codebase_map": 1}
    assert state.artifacts["release_report"][-1].derived_from["test_report"] == 1
    titles = [d.title for d in state.decisions]
    assert "ADR-1: Keep it small" in titles  # the architect's decision
    assert "plan-signoff: approved" in titles and "release: approved" in titles  # the humans' decisions
    # the implementer was given the upstream context, not just its task
    payload = harness.provider.calls_for("impl:T3")[0].payload
    assert payload["task"]["id"] == "T3" and payload["design"]["overview"] == DESIGN["overview"]


# ====================================================================== retries, fallback, guardrails


def test_transient_failures_are_retried_with_backoff(make_harness):
    harness = make_harness({"architecture": [TransientError("timeout"), TransientError("timeout"), DESIGN]})

    state = harness.run()

    assert state.status is RunStatus.SUCCEEDED
    assert harness.slept == [0.05, 0.1]  # exponential backoff
    assert state.nodes["architecture"].attempts == 3
    metrics = run_metrics(harness.store.audit.read())
    assert metrics["retries"] == 2 and metrics["incidents_recovered"] == 1 and metrics["mttr_s"] is not None


def test_retries_are_bounded(make_harness):
    harness = make_harness({"architecture": [TransientError("still down")]})

    state = harness.run()

    assert state.status is RunStatus.FAILED
    assert len(harness.events("node.started", "architecture")) == 3
    assert harness.target_files() == []  # nothing reached the target


def test_rejected_output_is_fed_back_to_the_agent(make_harness):
    cyclic = plan(task("T1", ["app/a.py"], ("T2",)), task("T2", ["app/b.py"], ("T1",)))
    harness = make_harness({"planning": [cyclic, PLAN]})

    state = harness.run()

    assert state.status is RunStatus.SUCCEEDED
    second_call = harness.provider.calls_for("planning")[1]
    assert any("dependency cycle" in item for item in second_call.payload["feedback"])


def test_fallback_agent_takes_over_when_the_primary_cannot(make_harness):
    class Broken:
        def run(self, ctx):
            raise PermanentError("model unavailable")

    harness = make_harness(agents={"doc_writer": Broken()})

    state = harness.run()

    assert state.status is RunStatus.SUCCEEDED
    assert state.nodes["documentation"].used_fallback is True
    assert harness.events("node.fallback", "documentation")
    assert (harness.target / "README.md").read_text() == "# Fallback\n"


def test_writing_outside_the_task_scope_is_blocked(make_harness):
    harness = make_harness({"impl:T1": [write("app/not_mine.py")]})

    state = harness.run()

    assert state.status is RunStatus.FAILED
    assert "change_within_scope" in state.error
    assert not harness.store.workspace.exists("app/not_mine.py")  # rolled back every time
    assert harness.events("policy.violation", "impl:T1")


@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", "C:/Windows/x.py", ".git/hooks/pre-commit"])
def test_agents_cannot_leave_the_sandbox(make_harness, path):
    harness = make_harness({"impl:T1": [write(path)]})

    state = harness.run()

    assert state.status is RunStatus.FAILED
    assert harness.events("node.attempt_failed", "impl:T1")[0]["data"]["kind"] == "policy"
    assert not (harness.store.workspace.root.parent / "escape.py").exists()


def test_policy_violation_in_code_is_rejected_then_corrected(make_harness):
    leaky = write("app/a.py", 'API_KEY = "sk_live_abcdefghijklmnop1234"\n')
    harness = make_harness({"impl:T1": [leaky, write("app/a.py")]})

    state = harness.run()

    assert state.status is RunStatus.SUCCEEDED
    violation = harness.events("policy.violation", "impl:T1")[0]
    assert violation["data"]["findings"][0]["rule"].startswith("secret/")
    assert "sk_live" not in (harness.target / "app/a.py").read_text()


def test_a_crashing_gate_fails_closed(make_harness, monkeypatch):
    monkeypatch.setitem(gates.GATES, "plan_is_dag", lambda ctx: 1 / 0)
    harness = make_harness()

    state = harness.run()

    assert state.status is RunStatus.FAILED
    assert "gate crashed" in state.error


# ====================================================================== human checkpoints


def test_run_pauses_for_humans_and_nothing_is_promoted_before_release_approval(make_harness, deferred):
    harness = make_harness()

    assert harness.run(deferred).status is RunStatus.PAUSED
    assert harness.status("planning") == "awaiting_approval"
    assert not harness.events("node.started", "impl:T1")  # no code is touched before the plan is signed off

    harness.decide("approve")
    assert harness.reload().run(deferred).status is RunStatus.PAUSED
    assert harness.status("release_readiness") == "awaiting_approval"
    assert harness.target_files() == []  # the sandbox is complete, the target untouched

    harness.decide("approve")
    assert harness.reload().run(deferred).status is RunStatus.SUCCEEDED
    assert "app/a.py" in harness.target_files()


def test_rejection_ends_the_run_without_changes(make_harness):
    harness = make_harness()

    state = harness.run(Approver({"plan-signoff": [{"decision": "reject", "comment": "wrong approach"}]}))

    assert state.status is RunStatus.REJECTED
    assert "wrong approach" in state.error
    assert harness.target_files() == []


def test_a_decision_in_the_wrong_role_is_refused(make_harness, deferred):
    harness = make_harness()
    harness.run(deferred)

    harness.decide("approve", role="intern")
    state = harness.reload().run(deferred)

    assert state.status is RunStatus.PAUSED
    assert harness.events("approval.refused")
    assert harness.status("planning") == "awaiting_approval"


def test_high_impact_task_needs_change_control_while_other_work_continues(make_harness, deferred):
    # requirements.txt is a protected path and already exists in the target
    risky_plan = plan(task("T1", ["requirements.txt"]), task("T2", ["app/b.py"]))
    harness = make_harness(
        {"planning": [risky_plan], "impl:T1": [write("requirements.txt", "fastapi\nuvicorn\n")]},
        seed={"requirements.txt": "fastapi\n"},
    )
    harness.run(deferred)
    harness.decide("approve")

    state = harness.reload().run(deferred)

    assert state.status is RunStatus.PAUSED
    (request,) = [a for a in state.approvals if a.status == "pending"]
    assert request.checkpoint == "change-control:T1" and request.phase == "entry"
    assert "protected path requirements.txt" in request.reason
    assert harness.status("impl:T2") == "succeeded"  # the independent task was not held up
    assert harness.store.workspace.read("requirements.txt") == "fastapi\n"  # the guarded change has not happened


# ====================================================================== re-planning


def test_change_request_reopens_upstream_and_replans(make_harness):
    design_v2 = {**DESIGN, "overview": "Three small modules, plus a fourth after review."}
    plan_v2 = plan(
        task("T1", ["app/a.py"]),
        task("T2", ["app/b.py"], description="Write b with input validation"),
        task("T3", ["app/c.py"], ("T1", "T2")),
        task("T4", ["app/d.py"], ("T3",)),
    )
    approver = Approver(
        {"plan-signoff": [{"decision": "changes", "target": "architecture", "comment": "validate inputs"}]}
    )
    harness = make_harness(
        {"architecture": [DESIGN, design_v2], "planning": [PLAN, plan_v2], "impl:T4": [write("app/d.py")]}
    )

    state = harness.run(approver)

    assert state.status is RunStatus.SUCCEEDED and state.plan_version == 2
    replan = harness.events("plan.expanded")[1]["data"]
    assert (replan["added"], replan["changed"], replan["kept"]) == (["impl:T4"], ["impl:T2"], ["impl:T1", "impl:T3"])
    # the reviewer's comment reached the architect
    assert "validate inputs" in harness.provider.calls_for("architecture")[1].payload["feedback"][0]
    # approval is bound to content: the changed plan had to be approved again
    signoffs = [a for a in state.approvals if a.checkpoint == "plan-signoff"]
    assert [a.status for a in signoffs] == ["changes_requested", "approved"]
    assert signoffs[0].subject_hash != signoffs[1].subject_hash
    assert "app/d.py" in harness.target_files()


def test_unchanged_result_is_revalidated_not_recomputed(make_harness):
    """The architect is asked to revise but returns the same design: planning must not re-run."""
    approver = Approver(
        {"plan-signoff": [{"decision": "changes", "target": "architecture", "comment": "please double-check"}]}
    )
    harness = make_harness()

    state = harness.run(approver)

    assert state.status is RunStatus.SUCCEEDED
    assert state.nodes["architecture"].executions == 2
    assert state.nodes["planning"].executions == 1  # kept: its inputs did not change
    assert harness.events("node.revalidated", "planning")
    assert len(harness.provider.calls_for("planning")) == 1
    assert state.plan_version == 1


def test_change_request_cannot_target_a_downstream_node(make_harness):
    approver = Approver({"plan-signoff": [{"decision": "changes", "target": "promotion", "comment": "nonsense"}]})
    harness = make_harness()

    state = harness.run(approver)

    assert state.status is RunStatus.SUCCEEDED
    assert harness.events("approval.target_ignored")
    assert state.nodes["planning"].executions == 2  # fell back to re-opening the checkpoint's own node


# ====================================================================== rework and rollback


def test_failed_verification_triggers_targeted_rework(make_harness):
    harness = make_harness({"impl:T2": [write("app/b.py", "VALUE = 'BUG'\n"), write("app/b.py")]})

    state = harness.run()

    assert state.status is RunStatus.SUCCEEDED
    rework = harness.events("rework.triggered")[0]["data"]
    assert rework["targets"] == ["impl:T2"] and rework["round"] == 1
    # only the blamed task and what depends on it were redone
    assert {n: state.nodes[n].executions for n in ("impl:T1", "impl:T2", "impl:T3")} == {
        "impl:T1": 1,
        "impl:T2": 2,
        "impl:T3": 2,
    }
    # their changes were rolled back newest-first before being redone
    rolled_back = [e["node"] for e in harness.events("workspace.rolled_back")]
    assert rolled_back.index("impl:T3") < rolled_back.index("impl:T2") and "impl:T1" not in rolled_back
    # the implementer saw the failing test output
    assert "assert failed in app/b.py" in harness.provider.calls_for("impl:T2")[1].payload["feedback"][0]
    assert "BUG" not in (harness.target / "app/b.py").read_text()
    metrics = run_metrics(harness.store.audit.read())
    assert metrics["rework_rounds"] == 1 and metrics["rollbacks"] >= 2 and metrics["mttr_s"] is not None


def test_blame_sees_every_failure_even_when_feedback_is_cut_to_budget(make_harness):
    """Found in a live run: a long failure early in the output hid a later failing file from blame."""
    noisy = "VALUE = 'BUG'\n" + "# a long traceback\n" * 400  # well past MAX_FEEDBACK_CHARS on its own
    harness = make_harness(
        {
            "impl:T1": [write("app/a.py", noisy), write("app/a.py")],
            "impl:T2": [write("app/b.py", "VALUE = 'BUG'\n"), write("app/b.py")],
        }
    )

    state = harness.run()

    assert state.status is RunStatus.SUCCEEDED
    assert harness.events("rework.triggered")[0]["data"]["targets"] == ["impl:T1", "impl:T2"]


def test_rework_is_bounded(make_harness):
    harness = make_harness({"impl:T2": [write("app/b.py", "VALUE = 'BUG'\n")]}, policy=Policy(max_rework_rounds=2))

    state = harness.run()

    assert state.status is RunStatus.FAILED
    assert len(harness.events("rework.triggered")) == 2
    assert "still failing after 2 rework round(s)" in state.error
    assert harness.target_files() == []


def test_blocking_security_finding_reopens_implementation(make_harness):
    """A finding that only the whole-workspace review can see (it is in a pre-existing file)."""
    harness = make_harness(
        {
            "impl:T1": [
                write("app/a.py"),
                {
                    "rationale": "remove legacy",
                    "changes": [{"path": "app/a.py", "action": "write", "content": "VALUE = 1\n"}],
                },
            ]
        },
        seed={"app/legacy.py": "import os\nos.system('rm -rf /tmp/x')\n"},
    )

    state = harness.run()

    # No task owns app/legacy.py, so every task is re-opened, and after the rework budget the run fails safely.
    assert state.status is RunStatus.FAILED
    assert harness.events("rework.triggered")[0]["data"]["blame"].startswith(
        "The failure output names no task-owned file"
    )
    assert harness.target_files() == ["app/legacy.py"]  # target untouched


def test_promotion_can_be_rolled_back(make_harness):
    harness = make_harness(seed={"app/a.py": "OLD = True\n", "keep.txt": "keep\n"})
    harness.run()
    assert (harness.target / "app/a.py").read_text() == "VALUE = 1\n"

    restore_promotion(harness.target, harness.store.dir / "promotion")

    assert harness.target_files() == ["app/a.py", "keep.txt"]
    assert (harness.target / "app/a.py").read_text() == "OLD = True\n"


# ====================================================================== safe-stop, budgets, recovery


def test_operator_safe_stop_then_resume(make_harness):
    harness = make_harness()

    def stop_while_running(request):
        (harness.store.dir / "STOP").write_text("stop")
        return write("app/a.py")

    harness.provider.script["impl:T1"] = [stop_while_running]

    state = harness.run()

    assert state.status is RunStatus.STOPPED and "safe-stop" in state.stop_reason
    assert harness.status("impl:T1") == "succeeded"  # in-flight work was allowed to finish
    assert harness.status("impl:T3") == "pending"  # nothing new was started
    assert harness.target_files() == []

    (harness.store.dir / "STOP").unlink()
    assert harness.reload().run().status is RunStatus.SUCCEEDED
    assert len(harness.provider.calls_for("impl:T1")) == 1  # completed work was not repeated


def test_exhausted_budget_is_a_safe_stop_not_a_failure(make_harness):
    harness = make_harness(policy=Policy(max_agent_invocations=4))

    state = harness.run()

    assert state.status is RunStatus.STOPPED
    assert "budget exhausted" in state.stop_reason
    assert all(node.status.value != "failed" for node in state.nodes.values())


def test_change_freeze_blocks_promotion(make_harness):
    harness = make_harness(policy=Policy(change_freeze=True))

    state = harness.run()

    assert state.status is RunStatus.STOPPED and "change freeze" in state.stop_reason
    assert harness.status("release_readiness") == "succeeded"
    assert harness.target_files() == []


def test_promotion_is_refused_if_the_target_drifted(make_harness, deferred):
    harness = make_harness()
    harness.run(deferred)
    harness.decide("approve")
    harness.reload().run(deferred)  # now waiting for release approval
    (harness.target / "hotfix.py").write_text("someone else changed the codebase\n")

    harness.decide("approve")
    state = harness.reload().run(deferred)

    assert state.status is RunStatus.STOPPED and "target changed" in state.stop_reason
    assert harness.target_files() == ["hotfix.py"]


def test_node_interrupted_by_a_crash_is_rolled_back_and_rerun(make_harness, deferred):
    harness = make_harness()
    harness.run(deferred)
    # Simulate a process that died while impl:T1 was half-way through writing.
    harness.store.workspace.apply("impl:T1", ChangeSet.model_validate(write("app/a.py", "half written")))
    harness.store.state.nodes["impl:T1"].status = NodeStatus.RUNNING
    harness.store.save()

    harness.decide("approve")
    state = harness.reload().run()

    assert state.status is RunStatus.SUCCEEDED
    assert harness.events("workspace.rolled_back", "impl:T1")[0]["data"]["reason"] == "recovering an interrupted node"
    assert (harness.target / "app/a.py").read_text() == "VALUE = 1\n"


def test_injected_faults_are_labelled_and_fire_once_across_resume(make_harness, deferred):
    harness = make_harness(faults=[{"node": "architecture", "kind": "transient", "times": 1}])
    harness.run(deferred)
    harness.decide("approve")
    harness.reload().run()

    assert len(harness.events("fault.injected")) == 1
    assert harness.store.state.status is RunStatus.SUCCEEDED
