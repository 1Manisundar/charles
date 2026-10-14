"""Verification agents: they measure, they do not judge.

``Verifier`` really runs the generated test-suite in the sandbox and reports
results, coverage and requirement-to-test traceability. ``SecurityReviewer``
runs the policy scanners over the whole workspace. Exit gates turn those
measurements into pass / rework decisions.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..models import TransientError
from ..policy import scan_file
from .base import AgentContext, AgentResult

TEST_TIMEOUT_S = 300


def sandbox_env(scratch: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "COVERAGE_FILE": str(scratch / ".coverage")})
    env.pop("PYTHONPATH", None)
    env.update(extra or {})
    return env


def _parse_junit(path: Path) -> tuple[dict[str, int], list[dict[str, str]]]:
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failures: list[dict[str, str]] = []
    if not path.exists():
        return totals, failures
    for case in ET.parse(path).getroot().iter("testcase"):
        totals["tests"] += 1
        if case.find("skipped") is not None:
            totals["skipped"] += 1
        for kind in ("failure", "error"):
            problem = case.find(kind)
            if problem is not None:
                totals[f"{kind}s"] += 1
                classname = case.get("classname", "")
                failures.append(
                    {
                        "test": f"{classname.replace('.', '/')}.py::{case.get('name', '')}",
                        "kind": kind,
                        "message": (problem.get("message") or "")[:500],
                        "detail": (problem.text or "")[-1500:],
                    }
                )
    return totals, failures


def trace_requirements(ctx: AgentContext, requirement_ids: list[str]) -> dict[str, list[str]]:
    """Map each requirement id to the test functions that mention it."""
    ws = ctx.store.workspace
    matrix: dict[str, list[str]] = {rid: [] for rid in requirement_ids}
    for path in ws.files():
        if not (path.startswith(f"{ctx.profile.test_dir}/") and path.endswith(".py")):
            continue
        text = ws.read(path)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                segment = ast.get_source_segment(text, node) or ""
                for rid in requirement_ids:
                    if re.search(rf"\b{re.escape(rid)}\b", segment):
                        matrix[rid].append(f"{path}::{node.name}")
    return matrix


class Verifier:
    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.store.workspace
        scratch = ctx.store.dir / "scratch"
        scratch.mkdir(exist_ok=True)
        junit = scratch / f"junit-{uuid.uuid4().hex[:8]}.xml"
        cov_json = scratch / f"coverage-{uuid.uuid4().hex[:8]}.json"
        cmd = [
            sys.executable, "-m", "pytest", ctx.profile.test_dir,
            # Pin the root: without it pytest would walk up and adopt the
            # configuration of whatever repository the sandbox happens to live in.
            "--rootdir", str(ws.root),
            "-p", "no:cacheprovider", "-q", "--no-header",
            f"--junitxml={junit}",
            f"--cov={ctx.profile.source_package}", "--cov-report=", f"--cov-report=json:{cov_json}",
        ]  # fmt: skip
        try:
            proc = subprocess.run(
                cmd, cwd=ws.root, env=sandbox_env(scratch), capture_output=True, text=True, timeout=TEST_TIMEOUT_S
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientError(f"test run exceeded {TEST_TIMEOUT_S}s") from exc

        totals, failures = _parse_junit(junit)
        coverage: dict[str, Any] = {"percent": 0.0, "files": {}}
        if cov_json.exists():
            data = json.loads(cov_json.read_text(encoding="utf-8"))
            coverage = {
                "percent": round(data["totals"]["percent_covered"], 2),
                "files": {
                    name.replace("\\", "/"): round(info["summary"]["percent_covered"], 1)
                    for name, info in sorted(data["files"].items())
                },
            }
        spec = ctx.inputs.get("requirement_spec") or {}
        functional = [r["id"] for r in spec.get("functional", [])]
        non_functional = [r["id"] for r in spec.get("non_functional", [])]
        matrix = trace_requirements(ctx, functional + non_functional)
        report = {
            "passed": proc.returncode == 0 and totals["tests"] > 0,
            "exit_code": proc.returncode,
            **totals,
            "failed_tests": failures,
            "coverage": coverage,
            "output_tail": (proc.stdout + proc.stderr)[-3000:],
        }
        traceability = {
            "matrix": matrix,
            "untested_functional": [rid for rid in functional if not matrix[rid]],
            "untested_non_functional": [rid for rid in non_functional if not matrix[rid]],
        }
        return AgentResult(artifacts={"test_report": report, "traceability": traceability})


class SecurityReviewer:
    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.store.workspace
        findings, scanned = [], 0
        for path in ws.files():
            try:
                text = ws.read(path)
            except (UnicodeDecodeError, FileNotFoundError):
                # Binary, or removed by a parallel stage since the listing. Every
                # change is also scanned by the exit gate of the node that made it.
                continue
            scanned += 1
            findings += [f.model_dump() for f in scan_file(path, text, ctx.policy, ctx.profile)]
        counts = {level: sum(1 for f in findings if f["severity"] == level) for level in ("high", "medium", "low")}
        report = {
            "files_scanned": scanned,
            "counts": counts,
            "findings": findings,
            "rules": [
                "hard-coded secrets",
                "dangerous calls (eval/exec/shell/pickle)",
                "SQL built by interpolation",
                "imports outside the approved dependency list",
                "schema columns persisting raw personal data",
            ],  # fmt: skip
        }
        return AgentResult(artifacts={"security_report": report})
