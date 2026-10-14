"""The SDLC workflow as an explicit graph.

Static shape (``*`` marks a human checkpoint)::

    requirements* ─┐
                   ├─> architecture ─> planning* ─> implementation ─┬─> verification ───┐
    codebase_scan ─┘                                 (fan-out/join) ├─> security_review ├─> release_readiness* ─> promotion
                                                                    └─> documentation ──┘

``implementation`` is a join node: the approved plan is expanded at runtime
into one node per task (``impl:T1`` ...) wired with the plan's own dependency
edges, so independent tasks run in parallel and the join waits for all of them.
Verification, security review and documentation then run in parallel and
synchronise again at release readiness. Dotted "rework" edges make the
execution non-linear: a failed test or security gate re-opens implementation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .graph import WorkflowGraph
from .models import ApprovalSpec, NodeSpec, RetryPolicy, ReworkSpec
from .policy import Policy

IMPLEMENTATION = "implementation"


def build_sdlc_graph() -> WorkflowGraph:
    rework = ReworkSpec(target=IMPLEMENTATION)
    return WorkflowGraph(
        [
            NodeSpec(
                id="requirements",
                title="Requirements analysis",
                stage="requirements",
                agent="requirements_analyst",
                outputs=["requirement_spec"],
                entry_gates=["budget_available"],
                exit_gates=["requirements_well_formed"],
                approval=ApprovalSpec(
                    checkpoint="clarification",
                    kind="clarification",
                    role="product-owner",
                    reason="The requirement is ambiguous; a human must confirm how it is interpreted.",
                    condition="has_open_questions",
                ),
            ),
            NodeSpec(
                id="codebase_scan",
                title="Codebase analysis",
                stage="design",
                agent="codebase_analyst",
                outputs=["codebase_map"],
            ),
            NodeSpec(
                id="architecture",
                title="Architecture and design",
                stage="design",
                agent="architect",
                depends_on=["requirements", "codebase_scan"],
                inputs=["requirement_spec", "codebase_map"],
                outputs=["design", "impact_report"],
                entry_gates=["inputs_available", "budget_available"],
                exit_gates=["design_covers_requirements", "design_is_reviewable", "impacted_modules_exist"],
            ),
            NodeSpec(
                id="planning",
                title="Task decomposition",
                stage="planning",
                agent="planner",
                depends_on=["architecture"],
                inputs=["requirement_spec", "design", "impact_report", "codebase_map"],
                outputs=["plan"],
                entry_gates=["inputs_available", "budget_available"],
                exit_gates=["plan_is_dag", "plan_traces_requirements", "plan_respects_ownership"],
                expands_plan=True,
                approval=ApprovalSpec(
                    checkpoint="plan-signoff",
                    role="tech-lead",
                    reason="The design and plan must be approved before any code is changed.",
                ),
            ),
            NodeSpec(
                id=IMPLEMENTATION,
                title="Implementation (join)",
                stage="implementation",
                agent="sync_join",
                join=True,
                depends_on=["planning"],
                outputs=["change_summary"],
                exit_gates=["workspace_consistent"],
                rework=rework,
            ),
            NodeSpec(
                id="verification",
                title="Test and verify",
                stage="testing",
                agent="verifier",
                depends_on=[IMPLEMENTATION],
                inputs=["change_summary", "requirement_spec"],
                outputs=["test_report", "traceability"],
                entry_gates=["inputs_available"],
                exit_gates=["tests_pass", "coverage_threshold", "requirements_have_tests"],
                rework=rework,
            ),
            NodeSpec(
                id="security_review",
                title="Security and compliance review",
                stage="security",
                agent="security_reviewer",
                depends_on=[IMPLEMENTATION],
                inputs=["change_summary"],
                outputs=["security_report"],
                entry_gates=["inputs_available"],
                exit_gates=["no_blocking_findings"],
                rework=rework,
            ),
            NodeSpec(
                id="documentation",
                title="Documentation",
                stage="documentation",
                agent="doc_writer",
                fallback_agent="doc_template",
                depends_on=[IMPLEMENTATION],
                inputs=["change_summary", "requirement_spec", "design", "plan"],
                outputs=["docs_report", "docs_change"],
                entry_gates=["inputs_available", "budget_available"],
                exit_gates=["change_within_scope", "change_passes_policy", "openapi_exported"],
                mutates_workspace=True,
                params={"change_artifact": "docs_change"},
                rework=rework,
            ),
            NodeSpec(
                id="release_readiness",
                title="Release readiness",
                stage="release",
                agent="release_manager",
                depends_on=["verification", "security_review", "documentation"],
                inputs=[
                    "test_report",
                    "traceability",
                    "security_report",
                    "docs_report",
                    "docs_change",
                    "change_summary",
                ],
                outputs=["release_report"],
                entry_gates=["inputs_available", "upstream_fresh"],
                exit_gates=["release_checklist_green"],
                approval=ApprovalSpec(
                    checkpoint="release",
                    role="release-manager",
                    reason="Promoting the change onto the target codebase is a high-impact action.",
                ),
            ),
            NodeSpec(
                id="promotion",
                title="Promote to target",
                stage="release",
                agent="promoter",
                depends_on=["release_readiness"],
                inputs=["release_report"],
                outputs=["promotion"],
                entry_gates=["inputs_available", "change_freeze_clear", "target_matches_baseline"],
                retry=RetryPolicy(max_attempts=1),
            ),
        ]
    )


def task_node(task: dict[str, Any], policy: Policy, baseline: Iterable[str]) -> NodeSpec:
    """Turn one planned task into an executable node.

    Whether the task is high impact is decided here from policy and the real
    baseline - never from the planning agent's own claim.
    """
    reasons = policy.high_impact_reasons(task["kind"], task["writes"], baseline)
    return NodeSpec(
        id=f"impl:{task['id']}",
        title=f"{task['id']} {task['title']}",
        stage="implementation",
        agent="implementer",
        parent=IMPLEMENTATION,
        depends_on=["planning"] + [f"impl:{dep}" for dep in task["depends_on"]],
        inputs=[f"changeset:{dep}" for dep in task["depends_on"]],
        outputs=[f"changeset:{task['id']}"],
        entry_gates=["inputs_available", "budget_available"],
        exit_gates=["change_within_scope", "change_passes_policy"],
        mutates_workspace=True,
        params={"task": task, "change_artifact": f"changeset:{task['id']}"},
        approval=(
            ApprovalSpec(
                checkpoint=f"change-control:{task['id']}",
                phase="entry",
                role="change-advisory",
                reason="High-impact change: " + "; ".join(reasons) + ".",
            )
            if reasons
            else None
        ),
    )
