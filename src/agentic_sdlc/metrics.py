"""Reliability metrics, derived purely from the audit log.

Deriving metrics from the same hash-chained events that provide traceability
means the numbers cannot disagree with the record of what happened.
"""

from __future__ import annotations

import statistics
from typing import Any

Event = dict[str, Any]


def run_metrics(events: list[Event]) -> dict[str, Any]:
    if not events:
        return {}
    by_type: dict[str, list[Event]] = {}
    for event in events:
        by_type.setdefault(event["type"], []).append(event)

    finished = by_type.get("run.finished", [])
    status = finished[-1]["data"]["status"] if finished else "running"
    started_ts, ended_ts = events[0]["ts"], (finished[-1]["ts"] if finished else events[-1]["ts"])

    # Time the engine was not running at all (paused for a human, safe-stopped).
    idle = 0.0
    for event in by_type.get("run.resumed", []):
        earlier = [f for f in finished if f["seq"] < event["seq"]]
        if earlier:
            idle += event["ts"] - earlier[-1]["ts"]

    attempts = by_type.get("node.started", [])
    attempt_failures = by_type.get("node.attempt_failed", [])
    retries = [e for e in attempt_failures if e["data"].get("will_retry")]

    # An incident opens at a node's first failure and closes at its next success.
    open_since: dict[str, float] = {}
    recoveries: list[float] = []
    first_pass, seen_success = 0, set()
    starts_before_success: dict[str, int] = {}
    for event in events:
        node = event.get("node")
        if event["type"] == "node.started":
            starts_before_success[node] = starts_before_success.get(node, 0) + 1
        elif event["type"] in ("node.attempt_failed", "node.failed"):
            open_since.setdefault(node, event["ts"])
        elif event["type"] == "node.succeeded":
            if node in open_since:
                recoveries.append(event["ts"] - open_since.pop(node))
            if node not in seen_success:
                seen_success.add(node)
                first_pass += starts_before_success.get(node, 0) == 1

    stage_latency: dict[str, float] = {}
    for event in by_type.get("node.succeeded", []):
        stage = event["data"].get("stage", "?")
        stage_latency[stage] = round(stage_latency.get(stage, 0.0) + event["data"].get("duration_s", 0.0), 3)

    gates = by_type.get("gate.evaluated", [])
    approvals = by_type.get("approval.decided", [])
    wall = ended_ts - started_ts
    return {
        "status": status,
        "end_to_end_latency_s": round(wall, 3),
        "active_latency_s": round(wall - idle, 3),
        "human_wait_s": round(sum(e["data"].get("wait_s", 0.0) for e in approvals), 3),
        "nodes_succeeded": len(seen_success),
        "node_attempts": len(attempts),
        "first_pass_success_rate": round(first_pass / len(seen_success), 3) if seen_success else None,
        "retries": len(retries),
        "retry_frequency": round(len(retries) / len(attempts), 3) if attempts else 0.0,
        "fallbacks": len(by_type.get("node.fallback", [])),
        "rollbacks": len(by_type.get("workspace.rolled_back", [])) + len(by_type.get("release.rolled_back", [])),
        "rework_rounds": len(by_type.get("rework.triggered", [])),
        "replans": max(0, len(by_type.get("plan.expanded", [])) - 1),
        "invalidations": len(by_type.get("node.reset", [])) + len(by_type.get("node.stale", [])),
        "incidents": len(recoveries) + len(open_since),
        "incidents_recovered": len(recoveries),
        "mttr_s": round(statistics.fmean(recoveries), 3) if recoveries else None,
        "gates_evaluated": len(gates),
        "gates_failed": sum(1 for e in gates if not e["data"]["passed"]),
        "approvals": len(approvals),
        "policy_violations": len(by_type.get("policy.violation", [])),
        "faults_injected": len(by_type.get("fault.injected", [])),
        "stage_latency_s": stage_latency,
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))], 3)


def aggregate(per_run: list[dict[str, Any]]) -> dict[str, Any]:
    """Fleet view across runs: success rate, retry/rollback frequency, MTTR, latency."""
    terminal = [m for m in per_run if m.get("status") in ("succeeded", "failed", "rejected")]
    succeeded = [m for m in terminal if m["status"] == "succeeded"]
    attempts = sum(m["node_attempts"] for m in per_run)
    recovered = sum(m["incidents_recovered"] for m in per_run)
    mttr_total = sum((m["mttr_s"] or 0.0) * m["incidents_recovered"] for m in per_run)
    latencies = [m["active_latency_s"] for m in succeeded]
    return {
        "runs": len(per_run),
        "runs_terminal": len(terminal),
        "runs_in_flight": len(per_run) - len(terminal),
        "success_rate": round(len(succeeded) / len(terminal), 3) if terminal else None,
        "retry_frequency": round(sum(m["retries"] for m in per_run) / attempts, 3) if attempts else 0.0,
        "rollbacks_per_run": round(sum(m["rollbacks"] for m in per_run) / len(per_run), 3) if per_run else 0.0,
        "rework_rounds_per_run": round(sum(m["rework_rounds"] for m in per_run) / len(per_run), 3) if per_run else 0.0,
        "incidents": sum(m["incidents"] for m in per_run),
        "incidents_recovered": recovered,
        "mttr_s": round(mttr_total / recovered, 3) if recovered else None,
        "latency_p50_s": _percentile(latencies, 0.5),
        "latency_p95_s": _percentile(latencies, 0.95),
    }
