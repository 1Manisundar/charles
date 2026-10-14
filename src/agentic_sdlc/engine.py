"""The orchestration engine: a stateful, governed scheduler over the workflow graph.

Design rules that the rest of the code relies on:

* **Single writer.** Worker threads run agents and gates and return an
  ``Outcome``; only the scheduling thread mutates run state. The audit log and
  the workspace are the only shared structures workers write to, and both lock.
* **Quiescent restructuring.** Anything that re-shapes work that is already
  done - rework, change requests, re-planning - is queued and executed only
  when no agent is running, so a rollback can never race a running task.
* **Reset, then revalidate.** Re-opening a node rolls back its changes and
  those of everything downstream that wrote files, and marks the remaining
  downstream results STALE. A stale node whose input fingerprint turns out
  unchanged keeps its result; otherwise it is rolled back and re-run.
* **Approvals bind to content.** A human decision applies to the exact hash it
  was shown; if the subject changes, the checkpoint is raised again.
* **Stop is not failure.** Budgets, the kill switch and operator interrupts
  halt dispatch, drain in-flight work, persist state and leave the run resumable.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from .agents.base import Agent, AgentContext, AgentResult
from .approvals import ApprovalProvider, build_packet
from .faults import FaultInjector
from .gates import CONDITIONS, GATES, GateContext
from .graph import WorkflowGraph
from .models import (
    TERMINAL_RUN_STATUSES,
    AgentOutputError,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalSpec,
    GateResult,
    NodeSpec,
    NodeState,
    NodeStatus,
    PermanentError,
    PolicyViolation,
    RunState,
    RunStatus,
    TransientError,
    content_hash,
    now,
)
from .policy import Policy, ProjectProfile
from .providers.base import Provider
from .store import RunStore
from .workflow import task_node
from .workspace import _write_text

MAX_FEEDBACK_CHARS = 4000


@dataclass
class Job:
    spec: NodeSpec
    inputs: dict[str, Any]
    feedback: list[str]
    invocation: int
    fingerprint: str


@dataclass
class Outcome:
    fingerprint: str
    ok: bool = False
    result: AgentResult | None = None
    error: str | None = None
    rework: bool = False
    feedback: list[str] = field(default_factory=list)
    attempts: int = 0
    invocations: int = 0
    used_fallback: bool = False
    duration_s: float = 0.0


class Orchestrator:
    def __init__(
        self,
        store: RunStore,
        graph: WorkflowGraph,
        agents: dict[str, Agent],
        provider: Provider,
        approvals: ApprovalProvider,
        policy: Policy | None = None,
        profile: ProjectProfile | None = None,
        faults: FaultInjector | None = None,
        max_workers: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store, self.graph, self.agents = store, graph, agents
        self.provider, self.approvals = provider, approvals
        self.policy, self.profile = policy or Policy(), profile or ProjectProfile()
        self.faults = faults or FaultInjector()
        self.faults.on_fire = lambda node, fault: self.audit.append(
            "fault.injected", node=node, **{k: v for k, v in fault.items() if k != "node"}
        )
        self.max_workers, self.sleep = max_workers, sleep
        self._deferred: list[tuple[Any, ...]] = []
        self._asked: set[str] = set()
        self._stop: str | None = None
        self._rejected = False
        self._session_start = 0.0

    @property
    def state(self) -> RunState:
        return self.store.state

    @property
    def audit(self):
        return self.store.audit

    # ================================================================== main loop

    def run(self) -> RunState:
        """Drive the run until it completes, fails, pauses for a human or is stopped."""
        if self.state.status in TERMINAL_RUN_STATUSES:
            return self.state
        self._begin()
        running: dict[Future[Outcome], str] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="agent") as pool:
            while True:
                try:
                    progress = False
                    self._check_stop()
                    if not self._halted():
                        progress = self._process_approvals()
                        if self._deferred:
                            if not running:
                                self._run_deferred()
                                progress = True
                        else:
                            for node_id in self._ready():
                                if self._halted():
                                    break
                                if self._admit(node_id):
                                    running[pool.submit(self._execute, self._job(node_id))] = node_id
                                progress = True
                    if running:
                        done, _ = wait(list(running), timeout=0.25, return_when=FIRST_COMPLETED)
                        for future in done:
                            node_id = running.pop(future)
                            try:
                                outcome = future.result()
                            except Exception as exc:  # e.g. the audit log became unwritable
                                outcome = Outcome("", error=f"executor crashed: {type(exc).__name__}: {exc}")
                            self._complete(node_id, outcome)
                    elif not progress:
                        break
                except KeyboardInterrupt:
                    self._request_stop("interrupted by operator")
        return self._finish()

    def _begin(self) -> None:
        state = self.state
        self.graph.set_dynamic(state.dynamic_nodes)
        for node_id in self.graph.nodes:
            state.nodes.setdefault(node_id, NodeState(id=node_id))
        resumed = state.status is not RunStatus.CREATED
        for ns in state.nodes.values():
            if ns.status is NodeStatus.RUNNING:  # the previous process died mid-node
                self._undo(ns.id, "recovering an interrupted node")
                self._reopen(ns.id)
        state.status, state.stop_reason, state.error = RunStatus.RUNNING, None, None
        state.reserved_invocations = 0
        self._session_start = time.monotonic()
        self.faults.counts.update(state.fault_counts)
        if resumed:
            self.audit.append("run.resumed")
        else:
            self.audit.append(
                "run.started",
                scenario=state.scenario,
                provider=state.provider,
                target=state.target_dir,
                requirement_hash=content_hash(state.requirement)[:12],
                policy=self.policy.model_dump(),
            )
        self.store.save()

    def _finish(self) -> RunState:
        state = self.state
        state.active_seconds += time.monotonic() - self._session_start
        failed = [ns for ns in state.nodes.values() if ns.status is NodeStatus.FAILED]
        if self._deferred and not failed:
            # Stopped with restructuring still queued: apply it now (it runs no
            # agents) so the persisted state is consistent for the resume.
            self._run_deferred()
        if failed:
            state.status = RunStatus.REJECTED if self._rejected else RunStatus.FAILED
            state.error = f"{failed[0].id}: {failed[0].error}"
        elif self._stop:
            state.status, state.stop_reason = RunStatus.STOPPED, self._stop
        elif any(ns.status is NodeStatus.AWAITING_APPROVAL for ns in state.nodes.values()):
            state.status = RunStatus.PAUSED
        elif all(ns.status is NodeStatus.SUCCEEDED for ns in state.nodes.values()):
            state.status = RunStatus.SUCCEEDED
        else:
            state.status, state.error = RunStatus.FAILED, "no runnable node left but the run is incomplete"
        self.audit.append("run.finished", status=state.status.value, reason=state.error or state.stop_reason)
        self.store.save()
        return state

    # ================================================================== safe-stop

    def _request_stop(self, reason: str) -> None:
        if self._stop is None:
            self._stop = reason
            self.audit.append("safe_stop", reason=reason)

    def _check_stop(self) -> None:
        if (self.store.dir / "STOP").exists():
            self._request_stop("operator requested a safe-stop")
        elapsed = self.state.active_seconds + time.monotonic() - self._session_start
        if elapsed > self.policy.max_wall_clock_s:
            self._request_stop(f"wall-clock budget of {self.policy.max_wall_clock_s}s exceeded")

    def _halted(self) -> bool:
        return self._stop is not None or any(ns.status is NodeStatus.FAILED for ns in self.state.nodes.values())

    # ================================================================== scheduling

    def _ready(self) -> list[str]:
        nodes = self.state.nodes
        return [
            node_id
            for node_id in self.graph.topological_order()
            if nodes[node_id].status in (NodeStatus.PENDING, NodeStatus.STALE)
            and all(nodes[dep].status is NodeStatus.SUCCEEDED for dep in self.graph.deps(node_id))
        ]

    def _fingerprint(self, node_id: str) -> str:
        """Identity of everything that determines a node's result."""
        spec = self.graph[node_id]
        return content_hash(
            {
                "node": node_id,
                "agent": spec.agent,
                "params": spec.params,
                "inputs": {name: self.store.hash_of(name) for name in self.graph.inputs(node_id)},
                "feedback": self.state.nodes[node_id].feedback,
            }
        )

    def _gate_context(
        self, spec: NodeSpec, result: AgentResult | None = None, applied: dict | None = None
    ) -> GateContext:
        return GateContext(spec, self.store, self.graph, self.policy, self.profile, result, applied)

    def _evaluate(self, spec: NodeSpec, names: list[str], phase: str, ctx: GateContext) -> list[GateResult]:
        """Run gates, record each verdict, return the failures."""
        failed = []
        for name in names:
            try:
                verdict = GATES[name](ctx)
            except Exception as exc:  # a broken gate must fail closed, not open
                verdict = GateResult(gate=name, passed=False, details=f"gate crashed: {type(exc).__name__}: {exc}")
            self.audit.append(
                "gate.evaluated",
                node=spec.id,
                gate=name,
                phase=phase,
                passed=verdict.passed,
                details=verdict.details[:600],
            )
            if verdict.findings:
                self.audit.append(
                    "policy.violation" if not verdict.passed else "policy.finding",
                    node=spec.id,
                    gate=name,
                    findings=[f.model_dump() for f in verdict.findings[:20]],
                )
            if not verdict.passed:
                failed.append(verdict)
        return failed

    def _admit(self, node_id: str) -> bool:
        """Decide whether a ready node actually runs now. True means dispatch it."""
        spec, ns = self.graph[node_id], self.state.nodes[node_id]

        if ns.status is NodeStatus.STALE:
            if self._fingerprint(node_id) == ns.fingerprint:
                self.audit.append("node.revalidated", node=node_id, reason="inputs unchanged; previous result kept")
                self._settle(node_id, announce=False)
                return False
            self._undo_tree(node_id, "inputs changed upstream")
            self._reopen(node_id)

        for verdict in self._evaluate(spec, spec.entry_gates, "entry", self._gate_context(spec)):
            if verdict.safe_stop:
                self._request_stop(verdict.details)
            else:
                self._fail(node_id, f"entry gate {verdict.gate}: {verdict.details}")
            return False

        approval = spec.approval
        if approval and approval.phase == "entry":
            subject = self._fingerprint(node_id)
            if not self._is_approved(approval.checkpoint, subject):
                self._request_approval(node_id, approval, subject)
                return False
        return True

    def _job(self, node_id: str) -> Job:
        ns = self.state.nodes[node_id]
        ns.status, ns.started_at, ns.error = NodeStatus.RUNNING, now(), None
        self.state.reserved_invocations += 1
        self.store.save()
        return Job(
            spec=self.graph[node_id],
            inputs={name: self.store.get_artifact(name) for name in self.graph.inputs(node_id)},
            feedback=list(ns.feedback),
            invocation=ns.invocations,
            fingerprint=self._fingerprint(node_id),
        )

    # ================================================================== execution (worker threads)

    def _execute(self, job: Job) -> Outcome:
        """Run one node: agent -> apply -> exit gates, with retry and fallback.

        Runs on a worker thread and must not touch run state.
        """
        spec, started = job.spec, time.monotonic()
        out = Outcome(fingerprint=job.fingerprint)
        feedback = list(job.feedback)
        workspace = self.store.workspace
        chain = [spec.agent] + ([spec.fallback_agent] if spec.fallback_agent else [])

        for index, agent_name in enumerate(chain):
            if index:
                self.audit.append(
                    "node.fallback", node=spec.id, primary=chain[0], fallback=agent_name, reason=out.error
                )
            for attempt in range(1, spec.retry.max_attempts + 1):
                out.attempts += 1
                will_retry = attempt < spec.retry.max_attempts
                self.audit.append("node.started", actor=f"agent:{agent_name}", node=spec.id, attempt=out.attempts)

                def failed(kind: str, error: str, retry: bool, **extra: Any) -> None:
                    out.error = error
                    self._undo(spec.id, f"attempt failed ({kind})")
                    self.audit.append(
                        "node.attempt_failed", node=spec.id, attempt=out.attempts, kind=kind,
                        error=error[:600], will_retry=retry, **extra,
                    )  # fmt: skip

                try:
                    self.faults.before(spec.id)
                    invocation = job.invocation + out.invocations
                    ctx = AgentContext(
                        spec, self.store, job.inputs, feedback, invocation, self.provider, self.policy, self.profile
                    )
                    result = self.agents[agent_name].run(ctx)
                    out.invocations += 1
                    result = self.faults.after(spec.id, invocation, result)
                    applied = None
                    if result.changeset is not None:
                        applied = workspace.apply(spec.id, result.changeset)
                        self.audit.append(
                            "workspace.applied", actor=f"agent:{agent_name}", node=spec.id, files=sorted(applied)
                        )
                        result.artifacts[spec.params["change_artifact"]] = {
                            "rationale": result.changeset.rationale,
                            "files": applied,
                        }
                    failures = self._evaluate(spec, spec.exit_gates, "exit", self._gate_context(spec, result, applied))
                    if not failures:
                        out.ok, out.result, out.error = True, result, None
                        out.used_fallback = index > 0
                        out.duration_s = time.monotonic() - started
                        return out
                    details = [f"{g.gate}: {g.details}" for g in failures]
                    if any(g.rework for g in failures):
                        failed("gate", "; ".join(details), False, rework=True)
                        out.rework, out.feedback, out.result = True, details, result
                        out.duration_s = time.monotonic() - started
                        return out
                    retry = will_retry and all(g.retryable for g in failures)
                    failed("gate", "; ".join(details), retry)
                    if not retry:
                        break
                    feedback = feedback + details  # self-correction: the agent sees why it was rejected
                except TransientError as exc:
                    delay = spec.retry.delay(attempt)
                    failed("transient", str(exc), will_retry, retry_in_s=delay if will_retry else None)
                    if not will_retry:
                        break
                    self.sleep(delay)
                except AgentOutputError as exc:
                    failed("output", str(exc), will_retry)
                    if not will_retry:
                        break
                    feedback = feedback + [f"Your previous output was rejected: {exc}"]
                except PolicyViolation as exc:
                    self.audit.append(
                        "policy.violation", node=spec.id, findings=[{"rule": "sandbox", "message": str(exc)}]
                    )
                    failed("policy", str(exc), False)
                    break
                except PermanentError as exc:
                    failed("permanent", str(exc), False)
                    break
                except Exception as exc:  # an agent bug must not take the scheduler down
                    failed("crash", f"{type(exc).__name__}: {exc}", False)
                    break
        out.duration_s = time.monotonic() - started
        return out

    # ================================================================== completion (scheduling thread)

    def _complete(self, node_id: str, outcome: Outcome) -> None:
        spec, ns, state = self.graph[node_id], self.state.nodes[node_id], self.state
        ns.attempts += outcome.attempts
        ns.invocations += outcome.invocations
        state.agent_invocations += outcome.invocations
        state.reserved_invocations = max(0, state.reserved_invocations - 1)
        state.fault_counts = dict(self.faults.counts)

        if outcome.ok and outcome.result is not None:
            derived = {n: m.version for n in self.graph.inputs(node_id) if (m := self.store.latest(n))}
            for name, content in outcome.result.artifacts.items():
                meta, _ = self.store.put_artifact(name, content, node_id, derived)
                ns.outputs[name] = meta.version
            for decision in outcome.result.decisions:
                self.store.record_decision(
                    decision.get("actor", f"agent:{spec.agent}"),
                    decision["title"],
                    decision["rationale"],
                    node=node_id,
                    alternatives=decision.get("alternatives"),
                )
            ns.fingerprint, ns.used_fallback, ns.error = outcome.fingerprint, outcome.used_fallback, None
            ns.executions += 1
            ns.has_result = True
            ns.last_duration_s, ns.last_attempts = round(outcome.duration_s, 3), outcome.attempts
            self.audit.append(
                "node.produced", actor=f"agent:{spec.agent}", node=node_id,
                model=outcome.result.model, usage=outcome.result.usage, outputs=ns.outputs,
            )  # fmt: skip
            if spec.expands_plan:
                self._expand_plan()
            self._settle(node_id)
        elif outcome.rework and spec.rework:
            if outcome.result is not None and not spec.mutates_workspace:
                # Keep what was measured (e.g. the failing test report) as evidence,
                # under a name nothing downstream consumes.
                for name, content in outcome.result.artifacts.items():
                    self.store.put_artifact(f"rejected:{name}", content, node_id, {})
            self._reopen(node_id)
            ns.error = outcome.error
            self._deferred.append(("rework", node_id, outcome.feedback))
        else:
            self._fail(node_id, outcome.error or "unknown error")
        self.store.save()

    def _settle(self, node_id: str, announce: bool = True) -> None:
        """A node has a valid result: mark it done, or hold it for exit approval."""
        spec = self.graph[node_id]
        approval = spec.approval
        if approval and approval.phase == "exit" and self._condition_holds(approval, spec):
            subject = content_hash({name: self.store.hash_of(name) for name in spec.outputs})
            if not self._is_approved(approval.checkpoint, subject):
                self._request_approval(node_id, approval, subject)
                return
        self._succeed(node_id, announce)

    def _succeed(self, node_id: str, announce: bool = True) -> None:
        ns = self.state.nodes[node_id]
        ns.status, ns.finished_at = NodeStatus.SUCCEEDED, now()
        if announce:
            self.audit.append(
                "node.succeeded",
                node=node_id,
                stage=self.graph[node_id].stage,
                duration_s=ns.last_duration_s,
                attempts=ns.last_attempts,
                used_fallback=ns.used_fallback,
            )

    def _reopen(self, node_id: str) -> None:
        """Back to PENDING: whatever result the node had no longer stands."""
        ns = self.state.nodes[node_id]
        ns.status, ns.has_result = NodeStatus.PENDING, False

    def _fail(self, node_id: str, error: str) -> None:
        ns = self.state.nodes[node_id]
        ns.status, ns.error, ns.finished_at = NodeStatus.FAILED, error, now()
        self.audit.append("node.failed", node=node_id, error=error[:1000])

    # ================================================================== approvals

    def _condition_holds(self, approval: ApprovalSpec, spec: NodeSpec) -> bool:
        return approval.condition is None or CONDITIONS[approval.condition](self._gate_context(spec))

    def _is_approved(self, checkpoint: str, subject: str) -> bool:
        return any(
            a.checkpoint == checkpoint and a.subject_hash == subject and a.status == "approved"
            for a in self.state.approvals
        )

    def _supersede(self, node_id: str) -> None:
        for request in self.state.approvals:
            if request.node == node_id and request.status == "pending":
                request.status = "superseded"
                self.audit.append("approval.superseded", node=node_id, id=request.id, checkpoint=request.checkpoint)

    def _request_approval(self, node_id: str, approval: ApprovalSpec, subject: str) -> None:
        spec, ns = self.graph[node_id], self.state.nodes[node_id]
        self._supersede(node_id)
        summary, questions = build_packet(
            self.store, approval.kind, approval.checkpoint, spec.title, approval.reason, spec.params.get("task")
        )
        request = ApprovalRequest(
            id=f"A-{len(self.state.approvals) + 1:03d}",
            checkpoint=approval.checkpoint,
            node=node_id,
            phase=approval.phase,
            kind=approval.kind,
            role=approval.role,
            reason=approval.reason,
            subject_hash=subject,
            summary=summary,
            questions=questions,
            requested_at=now(),
        )
        self.state.approvals.append(request)
        ns.status = NodeStatus.AWAITING_APPROVAL
        _write_text(
            self.store.dir / "approvals" / f"{request.id}-{approval.checkpoint.replace(':', '-')}.md", summary + "\n"
        )
        self.audit.append(
            "approval.requested", node=node_id, id=request.id, checkpoint=approval.checkpoint,
            phase=approval.phase, kind=approval.kind, role=approval.role, subject=subject[:12],
        )  # fmt: skip
        self.store.save()

    def _process_approvals(self) -> bool:
        changed = False
        for request in list(self.state.approvals):
            if request.status != "pending":
                continue
            decision = self.state.inbox.pop(request.id, None)
            if decision is None and request.id not in self._asked:
                self._asked.add(request.id)
                decision = self.approvals.decide(request)
            if decision is None:
                continue
            if decision.role != request.role:
                self.audit.append(
                    "approval.refused", actor=f"human:{decision.by}", node=request.node, id=request.id,
                    reason=f"role '{decision.role}' may not decide a '{request.role}' checkpoint",
                )  # fmt: skip
                changed = True  # the inbox entry was consumed; state must be saved
                continue
            self._apply_decision(request, decision)
            changed = True
        if changed:
            self.store.save()
        return changed

    def _apply_decision(self, request: ApprovalRequest, decision: ApprovalDecision) -> None:
        ns = self.state.nodes[request.node]
        actor = f"human:{decision.by}"
        request.decision, request.decided_at = decision, now()
        request.status = {"approve": "approved", "reject": "rejected", "changes": "changes_requested"}[
            decision.decision
        ]
        self.audit.append(
            "approval.decided", actor=actor, node=request.node, id=request.id, checkpoint=request.checkpoint,
            decision=decision.decision, role=decision.role, comment=decision.comment, answers=decision.answers,
            target=decision.target, wait_s=round(request.decided_at - request.requested_at, 3),
        )  # fmt: skip
        self.store.record_decision(
            actor,
            f"{request.checkpoint}: {request.status.replace('_', ' ')}",
            decision.comment or "(no comment)",
            node=request.node,
        )
        if decision.decision == "approve":
            if request.phase == "exit":
                self._succeed(request.node)
            else:
                ns.status = NodeStatus.PENDING  # the approval record now lets it through _admit
        elif decision.decision == "reject":
            self._rejected = True
            self._fail(
                request.node, f"rejected at checkpoint '{request.checkpoint}' by {decision.by}: {decision.comment}"
            )
        else:
            target = decision.target or request.node
            if target != request.node and target not in self.graph.ancestors(request.node):
                self.audit.append(
                    "approval.target_ignored",
                    node=request.node,
                    target=target,
                    reason="not upstream of this checkpoint",
                )
                target = request.node
            parts = [decision.comment] if decision.comment else []
            parts += [f"Answer to {qid}: {answer}" for qid, answer in decision.answers.items()]
            self.state.nodes[target].feedback.append(f"[{decision.role} {decision.by}] " + " | ".join(parts))
            self._deferred.append(("reset", target, f"changes requested at '{request.checkpoint}'", actor))

    # ================================================================== reset, rework, re-plan

    def _undo(self, node_id: str, reason: str) -> bool:
        files = self.store.workspace.rollback(node_id)
        if files:
            self.audit.append("workspace.rolled_back", node=node_id, files=files, reason=reason)
        return bool(files)

    def _undo_tree(self, node_id: str, reason: str) -> set[str]:
        """Roll back a node and everything downstream of it, newest change first."""
        affected = {node_id, *self.graph.descendants(node_id)}
        undone: set[str] = set()
        for owner in reversed(self.store.workspace.applied_owners()):
            if owner in affected and owner not in undone and self._undo(owner, reason):
                undone.add(owner)
        for other in undone - {node_id}:
            self._supersede(other)
            self._reopen(other)
        return undone

    def _reset(self, target: str, reason: str, actor: str) -> set[str]:
        """Re-open a node. Returns every node whose result is now in question."""
        undone = self._undo_tree(target, reason) if self.graph[target].mutates_workspace else set()
        descendants = self.graph.descendants(target)
        for node_id in descendants:
            ns = self.state.nodes[node_id]
            if node_id in undone or ns.status is NodeStatus.FAILED:
                self._reopen(node_id)
            elif ns.status in (NodeStatus.AWAITING_APPROVAL, NodeStatus.SUCCEEDED):
                self._supersede(node_id)
                if ns.has_result:  # keep it for now; revalidated by fingerprint when its turn comes
                    ns.status = NodeStatus.STALE
                    self.audit.append("node.stale", node=node_id, reason=f"upstream '{target}' re-opened")
                else:  # was only waiting for an entry approval; nothing to keep
                    self._reopen(node_id)
        self._supersede(target)
        self._reopen(target)
        self.state.nodes[target].error = None
        self.audit.append("node.reset", actor=actor, node=target, reason=reason, downstream=descendants)
        return {target, *descendants}

    def _run_deferred(self) -> None:
        actions, self._deferred = self._deferred, []
        covered: set[str] = set()
        for action in actions:
            if action[0] == "reset":
                _, target, reason, actor = action
                if target not in covered:
                    covered |= self._reset(target, reason, actor)
            else:
                _, node_id, feedback = action
                self._rework(node_id, feedback, covered)
        self.store.save()

    def _rework(self, node_id: str, feedback: list[str], covered: set[str]) -> None:
        spec, ns = self.graph[node_id], self.state.nodes[node_id]
        assert spec.rework is not None
        if node_id in covered:  # an earlier request in this batch already re-opened the work upstream
            self.audit.append("rework.merged", node=node_id)
            return
        rounds = self.state.rework_rounds.get(node_id, 0)
        limit = min(spec.rework.max_rounds, self.policy.max_rework_rounds)
        if rounds >= limit:
            self._fail(node_id, f"still failing after {rounds} rework round(s): {ns.error}")
            return
        self.state.rework_rounds[node_id] = rounds + 1
        full = "\n".join(feedback)
        text = full[:MAX_FEEDBACK_CHARS]  # what the agents are shown
        targets, why = self._blame(spec.rework.target, full)  # blame sees every failure, not just the first few
        self.audit.append("rework.triggered", node=node_id, round=rounds + 1, limit=limit, targets=targets, blame=why)
        self.store.record_decision(
            "orchestrator",
            f"Rework round {rounds + 1}: re-open {', '.join(targets)}",
            f"Exit gate of '{node_id}' failed. {why}",
            node=node_id,
            alternatives=["fail the run", "re-open every implementation task"],
        )
        for target in targets:
            self.state.nodes[target].feedback.append(f"[rework requested by {node_id}] {text}")
            covered |= self._reset(target, f"rework round {rounds + 1} requested by '{node_id}'", "orchestrator")

    def _blame(self, group: str, failure_text: str) -> tuple[list[str], str]:
        """Pick the tasks to re-open: those whose files appear in the failure output."""
        children = self.graph.children(group)
        if not children:
            return [group], "No task breakdown available."
        text = failure_text.replace("\\", "/")
        blamed = [c for c in children if any(path in text for path in self.graph[c].params["task"]["writes"])]
        if blamed:
            return blamed, "Blamed the tasks whose files appear in the failure output."
        return children, "The failure output names no task-owned file, so every task is re-opened."

    def _expand_plan(self) -> None:
        """(Re-)materialise the plan as task nodes, keeping work the new plan did not change."""
        state, plan, baseline = self.state, self.store.get_artifact("plan"), self.store.baseline()
        new = {spec.id: spec for spec in (task_node(task, self.policy, baseline) for task in plan["tasks"])}
        old = state.dynamic_nodes
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(n for n in set(new) & set(old) if new[n].model_dump() != old[n].model_dump())
        kept = sorted(set(new) & set(old) - set(changed))

        for node_id in removed + changed:  # undo against the old graph shape, before it changes
            self._undo_tree(node_id, f"task changed in plan v{state.plan_version + 1}")
        for node_id in removed:
            self._supersede(node_id)
            state.nodes.pop(node_id, None)
        for node_id in changed:
            self._supersede(node_id)
            state.nodes[node_id] = NodeState(id=node_id, invocations=state.nodes[node_id].invocations)
        for node_id in added:
            state.nodes[node_id] = NodeState(id=node_id)
        state.dynamic_nodes = new
        self.graph.set_dynamic(new)
        state.plan_version += 1
        self.audit.append(
            "plan.expanded", node="planning", version=state.plan_version,
            added=added, removed=removed, changed=changed, kept=kept,
        )  # fmt: skip
        if state.plan_version > 1:
            self.store.record_decision(
                "orchestrator",
                f"Re-planned: plan v{state.plan_version}",
                f"Upstream output changed. Tasks added {added or 'none'}, removed {removed or 'none'}, "
                f"changed {changed or 'none'}, kept {kept or 'none'}.",
                node="planning",
            )
