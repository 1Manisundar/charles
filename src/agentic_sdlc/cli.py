"""Command-line interface: ``sdlc <command>`` (or ``python -m agentic_sdlc``).

Exit codes: 0 run succeeded, 2 failed or rejected, 3 paused for a human
decision, 4 safe-stopped, 1 usage or environment error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .agents import default_agents
from .approvals import DeferredApprovals, InteractiveApprovals, ScriptedApprovals
from .engine import Orchestrator
from .faults import FaultInjector, parse_fault
from .metrics import aggregate, run_metrics
from .models import ApprovalDecision, PermanentError, RunStatus
from .policy import Policy, ProjectProfile
from .providers import Playbook, ScriptedProvider
from .report import build_summary
from .store import RunStore
from .workflow import build_sdlc_graph
from .workspace import list_tree, restore_promotion

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO_ROOT / "playbooks"
SCENARIOS = ("greenfield", "brownfield", "ambiguous")
EXIT = {RunStatus.SUCCEEDED: 0, RunStatus.FAILED: 2, RunStatus.REJECTED: 2, RunStatus.PAUSED: 3, RunStatus.STOPPED: 4}


class UsageError(Exception):
    pass


# --------------------------------------------------------------------------- console observer

_QUIET = {
    "gate.evaluated",
    "artifact.stored",
    "decision.recorded",
    "node.produced",
    "workspace.applied",
    "policy.finding",
}


def _format(record: dict[str, Any], verbose: bool) -> str | None:
    kind, node, data = record["type"], record["node"] or "", record["data"]
    if kind == "gate.evaluated":
        if data["passed"] and not verbose:
            return None
        text = f"gate {data['gate']} ({data['phase']}) {'passed' if data['passed'] else 'FAILED'}"
        text += "" if data["passed"] else f": {data['details'].splitlines()[0][:150]}"
    elif kind in _QUIET and not verbose:
        return None
    elif kind == "node.started":
        text = f"started by {record['actor']}" + (f" (attempt {data['attempt']})" if data["attempt"] > 1 else "")
    elif kind == "node.succeeded":
        text = f"SUCCEEDED in {data['duration_s']}s" + (" via fallback agent" if data.get("used_fallback") else "")
    elif kind == "node.attempt_failed":
        text = f"attempt failed [{data['kind']}]" + (" -> will retry" if data["will_retry"] else "")
        text += f" in {data['retry_in_s']}s" if data.get("retry_in_s") else ""
    elif kind == "approval.requested":
        text = f"HUMAN CHECKPOINT {data['id']} '{data['checkpoint']}' needs role '{data['role']}'"
    elif kind == "approval.decided":
        text = f"checkpoint '{data['checkpoint']}' -> {data['decision'].upper()} by {record['actor']}"
        text += f": {data['comment']}" if data.get("comment") else ""
    elif kind == "plan.expanded":
        text = f"plan v{data['version']} -> task graph: added {data['added'] or '-'}, changed {data['changed'] or '-'}, removed {data['removed'] or '-'}, kept {data['kept'] or '-'}"
    elif kind == "rework.triggered":
        text = f"REWORK round {data['round']}/{data['limit']}: re-opening {data['targets']}"
    elif kind == "workspace.rolled_back":
        text = f"rolled back {len(data['files'])} file(s): {data['reason']}"
    elif kind == "node.reset":
        text = f"re-opened by {record['actor']}: {data['reason']}"
    elif kind == "fault.injected":
        text = f"[drill] injected {data['kind']} fault"
    elif kind == "release.promoted":
        text = f"PROMOTED to target: {data['added']} added, {data['modified']} modified, {data['deleted']} deleted"
    elif kind == "release.rolled_back":
        text = f"promotion ROLLED BACK: {data.get('reason', '')}"
    elif kind == "run.started":
        text = f"run started: scenario={data['scenario']} provider={data['provider']}"
    elif kind == "safe_stop":
        text = f"SAFE-STOP: {data['reason']}"
    elif kind == "node.fallback":
        text = f"primary agent '{data['primary']}' failed -> FALLBACK to '{data['fallback']}'"
    elif kind == "node.failed":
        text = f"FAILED: {data['error'].splitlines()[0][:200]}"
    elif kind == "node.revalidated":
        text = "revalidated: inputs unchanged, previous result kept"
    elif kind == "node.stale":
        text = f"marked stale: {data['reason']}"
    elif kind == "run.finished":
        text = f"run {data['status'].upper()}" + (f": {data['reason']}" if data.get("reason") else "")
    else:
        text = kind + (f" {json.dumps(data)[:160]}" if data else "")
    return f"{time.strftime('%H:%M:%S', time.localtime(record['ts']))}  {node:<18} {text}"


def _console(verbose: bool):
    def listener(record: dict[str, Any]) -> None:
        line = _format(record, verbose)
        if line:
            print(line, flush=True)

    return listener


# --------------------------------------------------------------------------- wiring


def _resolve_run(runs: Path, run_id: str) -> RunStore:
    if run_id == "latest":
        candidates = sorted((p for p in runs.glob("*") if (p / "state.json").exists()), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise UsageError(f"no runs found under {runs}")
        return RunStore.load(candidates[-1])
    if not (runs / run_id / "state.json").exists():
        raise UsageError(f"unknown run '{run_id}' under {runs}")
    return RunStore.load(runs / run_id)


def _check_baseline(playbook: Playbook, target: Path) -> None:
    """Fail early, with a useful message, if a scenario is run against the wrong codebase."""
    present = set(list_tree(target))
    check = playbook.config.get("baseline", {})
    if check.get("empty") and present:
        raise UsageError(f"scenario '{playbook.name}' builds from scratch, but {target} is not empty")
    missing = [p for p in check.get("must_exist", []) if p not in present]
    if missing:
        raise UsageError(
            f"scenario '{playbook.name}' extends an existing service; {target} is missing {missing}. {check.get('hint', '')}"
        )
    already = [p for p in check.get("must_not_exist", []) if p in present]
    if already:
        raise UsageError(f"scenario '{playbook.name}' appears to be applied to {target} already (found {already})")


def build_orchestrator(
    store: RunStore, approvals: str, approver: str, verbose: bool = False, quiet: bool = False
) -> Orchestrator:
    state, options = store.state, store.state.options
    playbook = Playbook(Path(options["playbook"])) if options.get("playbook") else None
    if state.provider == "scripted":
        if playbook is None:
            raise UsageError(
                "the scripted provider needs a scenario playbook; use --provider anthropic for free-form requirements"
            )
        provider = ScriptedProvider(playbook)
    elif state.provider == "openrouter":
        from .providers.openrouter_provider import DEFAULT_MODEL, OpenRouterProvider

        try:
            provider = OpenRouterProvider(model=options.get("model") or DEFAULT_MODEL)
        except PermanentError as exc:
            raise UsageError(f"cannot start the OpenRouter provider: {exc}") from exc
    else:
        try:
            from .providers.anthropic_provider import AnthropicProvider

            provider = AnthropicProvider(model=options.get("model") or "claude-opus-5-5")
        except Exception as exc:
            raise UsageError(
                f"cannot start the Anthropic provider ({exc}). Install with `pip install -e .[llm]` and configure credentials."
            ) from exc

    if approvals == "scripted":
        if playbook is None:
            raise UsageError("--approvals scripted needs a scenario playbook")
        approval_provider: Any = ScriptedApprovals(playbook.approvals, state)
    elif approvals == "interactive":
        approval_provider = InteractiveApprovals(approver)
    else:
        approval_provider = DeferredApprovals()

    faults = list(options.get("faults", []))
    if playbook is not None and state.provider == "scripted" and not options.get("no_playbook_faults"):
        faults += playbook.faults
    if not quiet:
        store.audit.subscribe(_console(verbose))
    return Orchestrator(
        store,
        build_sdlc_graph(),
        default_agents(),
        provider,
        approval_provider,
        policy=Policy(**options.get("policy", {})),
        profile=ProjectProfile(),
        faults=FaultInjector(faults, state.fault_counts),
    )


def _drive(store: RunStore, approvals: str, approver: str, verbose: bool, quiet: bool = False) -> int:
    state = build_orchestrator(store, approvals, approver, verbose, quiet).run()
    summary = store.dir / "ENGINEERING_SUMMARY.md"
    summary.write_bytes(build_summary(store).encode("utf-8"))
    if not quiet:
        print(f"\nRun {state.run_id}: {state.status.value.upper()}")
        print(f"  summary : {summary}")
        print(f"  audit   : {store.dir / 'audit.jsonl'}")
        pending = [a for a in state.approvals if a.status == "pending"]
        for request in pending:
            print(
                f"  waiting : {request.id} '{request.checkpoint}' (role {request.role}) - review {store.dir / 'approvals'}"
            )
        if pending and pending[0].checkpoint == "clarification":
            print(
                f"  answer  : sdlc approve {state.run_id} --decision changes --by <you> --role {pending[0].role} "
                '--answer "Q1=..." [--answer "Q2=..."]'
            )
        elif pending:
            print(f"  decide  : sdlc approve {state.run_id} --decision approve --by <you> --role {pending[0].role}")
        if pending:
            print(f"  then    : sdlc resume {state.run_id}")
    return EXIT.get(state.status, 1)


def start_run(
    runs: Path,
    target: Path,
    scenario: str | None = None,
    requirement: str | None = None,
    provider: str = "scripted",
    run_id: str | None = None,
    faults: list[dict[str, Any]] | None = None,
    policy: dict[str, Any] | None = None,
    model: str | None = None,
    no_playbook_faults: bool = False,
) -> RunStore:
    playbook = Playbook(PLAYBOOKS / scenario) if scenario else None
    if playbook is None and not requirement:
        raise UsageError("give --scenario, --requirement or --requirement-file")
    if playbook is not None and provider == "scripted":
        _check_baseline(playbook, target)
    runs.mkdir(parents=True, exist_ok=True)
    store = RunStore.create(
        runs,
        scenario or "custom",
        requirement or playbook.requirement,
        target,
        provider,
        run_id,  # type: ignore[union-attr]
    )
    store.state.options = {
        "playbook": str(playbook.root) if playbook else None,
        "faults": faults or [],
        "policy": policy or {},
        "model": model,
        "no_playbook_faults": no_playbook_faults,
    }
    store.save()
    return store


# --------------------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    requirement = args.requirement
    if args.requirement_file:
        requirement = Path(args.requirement_file).read_text(encoding="utf-8").strip()
    policy: dict[str, Any] = {}
    if args.min_coverage is not None:
        policy["min_coverage"] = args.min_coverage
    if args.max_invocations is not None:
        policy["max_agent_invocations"] = args.max_invocations
    if args.max_wall_clock is not None:
        policy["max_wall_clock_s"] = args.max_wall_clock
    if args.freeze:
        policy["change_freeze"] = True
    store = start_run(
        Path(args.runs), Path(args.target), args.scenario, requirement, args.provider, args.run_id,
        [parse_fault(f) for f in args.fault], policy, args.model, args.no_playbook_faults,
    )  # fmt: skip
    print(f"Run {store.state.run_id} -> sandbox {store.workspace.root}\n")
    return _drive(store, args.approvals, args.approver, args.verbose)


def cmd_resume(args: argparse.Namespace) -> int:
    store = _resolve_run(Path(args.runs), args.run_id)
    (store.dir / "STOP").unlink(missing_ok=True)
    # An operator deliberately extends a budget; the extension is audited like any other decision.
    extended = {
        name: value
        for name, value in (("max_agent_invocations", args.max_invocations), ("max_wall_clock_s", args.max_wall_clock))
        if value is not None
    }
    if extended:
        store.state.options.setdefault("policy", {}).update(extended)
        store.audit.append("budget.extended", actor=f"human:{args.approver}", **extended)
        store.save()
    return _drive(store, args.approvals, args.approver, args.verbose)


def cmd_approve(args: argparse.Namespace) -> int:
    store = _resolve_run(Path(args.runs), args.run_id)
    pending = [a for a in store.state.approvals if a.status == "pending"]
    if args.request:
        pending = [a for a in pending if a.id == args.request]
    if not pending:
        raise UsageError("no matching pending checkpoint")
    if len(pending) > 1:
        raise UsageError(f"several checkpoints are pending ({[a.id for a in pending]}); name one")
    request = pending[0]
    if args.answer and args.decision != "changes":
        # Answers reach the agent only as feedback on a re-run; any other decision would drop them silently.
        raise UsageError("--answer needs --decision changes: answers are applied by re-running the node")
    answers = dict(a.split("=", 1) for a in args.answer)
    decision = ApprovalDecision(
        decision=args.decision, by=args.by, role=args.role or request.role,
        comment=args.comment, answers=answers, target=args.target,
    )  # fmt: skip
    store.state.inbox[request.id] = decision
    store.save()
    print(
        f"Recorded '{args.decision}' for {request.id} ({request.checkpoint}). It takes effect on `sdlc resume {store.state.run_id}`."
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _resolve_run(Path(args.runs), args.run_id)
    state, graph = store.state, build_sdlc_graph()
    graph.set_dynamic(state.dynamic_nodes)
    print(f"Run {state.run_id}  scenario={state.scenario}  provider={state.provider}  status={state.status.value}")
    if state.error or state.stop_reason:
        print(f"  reason: {state.error or state.stop_reason}")
    print(
        f"  plan v{state.plan_version}, {state.agent_invocations} agent invocations, {len(state.decisions)} decisions\n"
    )
    for node_id in graph.topological_order():
        ns = state.nodes.get(node_id)
        if ns:
            extra = (
                f"  attempts={ns.attempts}"
                + (" fallback" if ns.used_fallback else "")
                + (f"  ! {ns.error[:90]}" if ns.error else "")
            )
            print(f"  {ns.status.value:<18} {node_id:<20}{extra}")
    for request in state.approvals:
        if request.status == "pending":
            print(f"\nPending checkpoint {request.id} '{request.checkpoint}' (role {request.role}):\n{request.summary}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for path in sorted(Path(args.runs).glob("*/state.json")):
        state = RunStore.load(path.parent).state
        print(f"{state.run_id:<44} {state.status.value:<10} {state.scenario}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    store = _resolve_run(Path(args.runs), args.run_id)
    (store.dir / "STOP").write_text("stop requested\n", encoding="utf-8")
    print("Safe-stop requested: in-flight agents finish, nothing new starts, state is kept for `sdlc resume`.")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    store = _resolve_run(Path(args.runs), args.run_id)
    ok, detail = store.audit.verify()
    if not args.verify:
        for record in store.audit.read():
            if not args.type or record["type"].startswith(args.type):
                line = _format(record, verbose=True)
                print(f"{record['seq']:>4} {record['hash'][:8]}  {line}")
    print(f"audit chain: {'OK' if ok else 'BROKEN'} - {detail}")
    return 0 if ok else 2


def cmd_lineage(args: argparse.Namespace) -> int:
    store = _resolve_run(Path(args.runs), args.run_id)
    state = store.state

    def show(name: str, version: int, depth: int) -> None:
        metas = state.artifacts.get(name, [])
        meta = next((m for m in metas if m.version == version), None)
        if meta is None:
            return
        print(f"{'  ' * depth}{name} v{meta.version} [{meta.hash[:12]}] produced by {meta.produced_by}")
        if depth == 0:
            for decision in state.decisions:
                if decision.node == meta.produced_by:
                    print(f"    decision {decision.id} by {decision.actor}: {decision.title}")
            for request in state.approvals:
                if request.node == meta.produced_by and request.decision:
                    print(f"    checkpoint {request.checkpoint}: {request.status} by {request.decision.by}")
        for parent, parent_version in sorted(meta.derived_from.items()):
            show(parent, parent_version, depth + 1)

    latest = store.latest(args.artifact)
    if latest is None:
        raise UsageError(f"no artifact '{args.artifact}'; known: {sorted(state.artifacts)}")
    show(args.artifact, latest.version, 0)
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    runs = Path(args.runs)
    if args.run_id:
        print(json.dumps(run_metrics(_resolve_run(runs, args.run_id).audit.read()), indent=2))
        return 0
    per_run = [run_metrics(RunStore.load(p.parent).audit.read()) for p in sorted(runs.glob("*/state.json"))]
    print(json.dumps(aggregate([m for m in per_run if m]), indent=2))
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    graph, statuses = build_sdlc_graph(), None
    if args.run_id:
        state = _resolve_run(Path(args.runs), args.run_id).state
        graph.set_dynamic(state.dynamic_nodes)
        statuses = {n: s.status for n, s in state.nodes.items()}
    print(graph.to_mermaid(statuses))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    print(build_summary(_resolve_run(Path(args.runs), args.run_id)))
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    store = _resolve_run(Path(args.runs), args.run_id)
    backup = store.dir / "promotion"
    if not (backup / "manifest.json").exists():
        raise UsageError("this run did not promote anything")
    delta = restore_promotion(Path(store.state.target_dir), backup)
    store.audit.append(
        "release.rolled_back",
        actor=f"human:{args.by}",
        node="promotion",
        reason=args.reason,
        **{k: len(v) for k, v in delta.items()},
    )
    print(f"Restored {store.state.target_dir} to its state before run {store.state.run_id}.")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Run the three scenarios back to back against one fresh target directory."""
    root = Path(args.dir) if args.dir else Path("demo") / time.strftime("%Y%m%d-%H%M%S")
    target, runs = root / "service", root / "runs"
    if list_tree(target):
        raise UsageError(f"{target} is not empty; choose another --dir")
    for scenario in SCENARIOS:
        print("\n" + "#" * 78 + f"\n# Scenario: {scenario}\n" + "#" * 78)
        store = start_run(runs, target, scenario, run_id=scenario)
        code = _drive(store, "scripted", "demo", args.verbose)
        if code != 0:
            return code
    per_run = [run_metrics(RunStore.load(runs / s).audit.read()) for s in SCENARIOS]
    print("\nFleet reliability metrics across the three runs:")
    print(json.dumps(aggregate(per_run), indent=2))
    print(f"\nService built at : {target}\nRun records      : {runs}")
    print(f"Try it           : cd {target} && python -m uvicorn urlshortener.main:create_app --factory")
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdlc", description="Governed agentic SDLC orchestrator.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--runs", default="runs", help="directory holding run records (default: ./runs)")
    sub = parser.add_subparsers(dest="command", required=True)

    def drive_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--approvals", choices=("deferred", "interactive", "scripted"), default="deferred",
                       help="deferred: pause until `sdlc approve` (default); interactive: prompt here; scripted: replay the playbook's simulated reviewer")  # fmt: skip
        p.add_argument("--approver", default="operator", help="your name, for interactive approvals")
        p.add_argument("-v", "--verbose", action="store_true", help="also print passing gates, artifacts and decisions")

    p = sub.add_parser("run", help="start a run from a scenario or a free-form requirement")
    p.add_argument("--scenario", choices=SCENARIOS)
    p.add_argument("--requirement")
    p.add_argument("--requirement-file")
    p.add_argument("--target", default="service", help="codebase to work on (default: ./service)")
    p.add_argument(
        "--provider",
        choices=("scripted", "openrouter", "anthropic"),
        default="scripted",
        help="scripted: offline playbook replay; openrouter: any OpenRouter model (OPENROUTER_API_KEY); "
        "anthropic: Claude (ANTHROPIC_API_KEY)",
    )
    p.add_argument("--model", help="model id for a live provider")
    p.add_argument("--run-id")
    p.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="NODE:KIND[:N]",
        help="inject a transient or permanent fault (resilience drill)",
    )
    p.add_argument(
        "--no-playbook-faults", action="store_true", help="skip the faults a scenario playbook seeds by default"
    )
    p.add_argument("--min-coverage", type=float)
    p.add_argument("--max-invocations", type=int, help="agent invocation budget (safe-stop when exhausted)")
    p.add_argument("--max-wall-clock", type=float, help="active-time budget in seconds (safe-stop when exceeded)")
    p.add_argument("--freeze", action="store_true", help="simulate a change freeze (blocks promotion)")
    drive_options(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("resume", help="continue a paused or stopped run")
    p.add_argument("run_id")
    p.add_argument("--max-invocations", type=int, help="raise the agent invocation budget before continuing")
    p.add_argument("--max-wall-clock", type=float, help="raise the active-time budget (seconds) before continuing")
    drive_options(p)
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("approve", help="record a human decision for a pending checkpoint")
    p.add_argument("run_id")
    p.add_argument("request", nargs="?", help="request id (e.g. A-002) if several are pending")
    p.add_argument("--decision", choices=("approve", "reject", "changes"), required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--role", help="role you are acting in (must match the checkpoint's required role)")
    p.add_argument("--comment", default="")
    p.add_argument("--target", help="with --decision changes: the upstream node to re-open")
    p.add_argument("--answer", action="append", default=[], metavar="Q1=TEXT", help="answer a clarification question")
    p.set_defaults(fn=cmd_approve)

    for name, fn, text in (
        ("status", cmd_status, "show node states and pending checkpoints"),
        ("stop", cmd_stop, "request a safe-stop of a running run"),
        ("report", cmd_report, "print the engineering summary"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("run_id")
        p.set_defaults(fn=fn)

    p = sub.add_parser("list", help="list runs")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("audit", help="print and verify the hash-chained audit log")
    p.add_argument("run_id")
    p.add_argument("--verify", action="store_true", help="only verify the chain")
    p.add_argument("--type", help="only events whose type starts with this prefix")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("lineage", help="show how an artifact was derived and who decided what")
    p.add_argument("run_id")
    p.add_argument("artifact")
    p.set_defaults(fn=cmd_lineage)

    p = sub.add_parser("metrics", help="reliability metrics for one run, or aggregated over all runs")
    p.add_argument("run_id", nargs="?")
    p.set_defaults(fn=cmd_metrics)

    p = sub.add_parser("graph", help="print the workflow graph as Mermaid")
    p.add_argument("run_id", nargs="?")
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("rollback", help="undo a run's promotion to the target")
    p.add_argument("run_id")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", default="operator rollback")
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("demo", help="run greenfield, brownfield and ambiguous scenarios end to end")
    p.add_argument("--dir", help="output directory (default: ./demo/<timestamp>)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
