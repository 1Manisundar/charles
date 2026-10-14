"""Core data model: workflow definition, run state, and the error taxonomy.

Everything that is persisted or hashed is a pydantic model so that state
snapshots, audit records and fingerprints share one canonical serialisation.
"""

from __future__ import annotations

import hashlib
import json
import time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def canonical_json(obj: Any) -> str:
    """Deterministic JSON used for hashing (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def now() -> float:
    return time.time()


# --------------------------------------------------------------------------- errors


class OrchestrationError(Exception):
    """Base class for errors the engine knows how to classify."""


class TransientError(OrchestrationError):
    """Worth retrying unchanged after a backoff (timeouts, rate limits, 5xx)."""


class AgentOutputError(OrchestrationError):
    """The agent produced unusable output. Retried with the message as feedback."""


class PermanentError(OrchestrationError):
    """Retrying the same agent cannot help; fall back or fail the node."""


class PolicyViolation(PermanentError):
    """An agent attempted something outside its autonomy boundary."""


# --------------------------------------------------------------------------- workflow definition


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    # Succeeded earlier, but something upstream is being re-executed. Revalidated
    # by fingerprint once its dependencies settle: unchanged inputs keep the
    # result, changed inputs roll it back and re-run it.
    STALE = "stale"
    FAILED = "failed"


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"  # waiting for a human decision
    STOPPED = "stopped"  # safe-stop: operator, budget or interrupt; resumable
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"  # a human rejected a checkpoint


TERMINAL_RUN_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.REJECTED}


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    backoff_base_s: float = 0.05
    backoff_factor: float = 2.0
    backoff_max_s: float = 2.0

    def delay(self, attempt: int) -> float:
        return min(self.backoff_max_s, self.backoff_base_s * self.backoff_factor ** (attempt - 1))


class ApprovalSpec(BaseModel):
    """A human checkpoint attached to a node.

    ``entry`` checkpoints guard a high-impact action before it happens;
    ``exit`` checkpoints sign off what a node produced before anything
    downstream may consume it.
    """

    checkpoint: str
    phase: Literal["entry", "exit"] = "exit"
    kind: Literal["approval", "clarification"] = "approval"
    role: str
    reason: str
    condition: str | None = None  # named predicate in gates.CONDITIONS; None = always


class ReworkSpec(BaseModel):
    """When this node's exit gate demands rework, re-open ``target``'s tasks."""

    target: str
    max_rounds: int = 2


class NodeSpec(BaseModel):
    id: str
    title: str
    stage: str
    agent: str
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    entry_gates: list[str] = Field(default_factory=list)
    exit_gates: list[str] = Field(default_factory=list)
    approval: ApprovalSpec | None = None
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    fallback_agent: str | None = None
    rework: ReworkSpec | None = None
    mutates_workspace: bool = False
    join: bool = False  # synchronisation point over dynamically created children
    expands_plan: bool = False  # output drives dynamic graph expansion
    parent: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- run state


class NodeState(BaseModel):
    id: str
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = 0  # every try, including failed ones
    invocations: int = 0  # agent calls that returned a result
    executions: int = 0  # times the node completed successfully
    has_result: bool = False  # a result from the latest execution is still in place
    last_duration_s: float = 0.0
    last_attempts: int = 0
    fingerprint: str | None = None
    feedback: list[str] = Field(default_factory=list)
    used_fallback: bool = False
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    outputs: dict[str, int] = Field(default_factory=dict)  # artifact name -> version


class ArtifactMeta(BaseModel):
    name: str
    version: int
    hash: str
    produced_by: str
    created_at: float
    derived_from: dict[str, int] = Field(default_factory=dict)


class Decision(BaseModel):
    """One entry in the decision lineage: who decided what, why, instead of what."""

    id: str
    node: str | None
    actor: str
    title: str
    rationale: str
    alternatives: list[str] = Field(default_factory=list)
    ts: float


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject", "changes"]
    by: str
    role: str
    comment: str = ""
    answers: dict[str, str] = Field(default_factory=dict)
    target: str | None = None  # for "changes": which node to re-open (default: this one)


class ApprovalRequest(BaseModel):
    id: str
    checkpoint: str
    node: str
    phase: Literal["entry", "exit"]
    kind: Literal["approval", "clarification"]
    role: str
    reason: str
    # Approvals are bound to the content they approved. If the subject changes
    # the approval no longer applies and a new request is raised.
    subject_hash: str
    summary: str
    questions: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["pending", "approved", "rejected", "changes_requested", "superseded"] = "pending"
    requested_at: float
    decided_at: float | None = None
    decision: ApprovalDecision | None = None


class RunState(BaseModel):
    run_id: str
    scenario: str
    requirement: str
    target_dir: str
    provider: str
    options: dict[str, Any] = Field(default_factory=dict)  # how to rebuild the run on resume
    status: RunStatus = RunStatus.CREATED
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    nodes: dict[str, NodeState] = Field(default_factory=dict)
    dynamic_nodes: dict[str, NodeSpec] = Field(default_factory=dict)
    artifacts: dict[str, list[ArtifactMeta]] = Field(default_factory=dict)
    decisions: list[Decision] = Field(default_factory=list)
    approvals: list[ApprovalRequest] = Field(default_factory=list)
    inbox: dict[str, ApprovalDecision] = Field(default_factory=dict)  # decided out-of-band via CLI
    rework_rounds: dict[str, int] = Field(default_factory=dict)
    fault_counts: dict[str, int] = Field(default_factory=dict)
    plan_version: int = 0
    agent_invocations: int = 0
    reserved_invocations: int = 0  # dispatched but not finished; counted against the budget
    active_seconds: float = 0.0
    stop_reason: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------- gate / policy results


class Finding(BaseModel):
    rule: str
    category: Literal["security", "compliance", "change_control"]
    severity: Literal["low", "medium", "high"]
    message: str
    path: str | None = None
    line: int | None = None


class GateResult(BaseModel):
    gate: str
    passed: bool
    details: str = ""
    findings: list[Finding] = Field(default_factory=list)
    # How the engine may recover from a failure of this gate:
    retryable: bool = False  # re-invoke the same agent with `details` as feedback
    rework: bool = False  # re-open upstream work (see NodeSpec.rework)
    safe_stop: bool = False  # halt the run without failing it (budgets)
