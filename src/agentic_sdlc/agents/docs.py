"""Documentation agents.

The API reference is never written by a model: it is exported from the running
application's OpenAPI schema and rendered deterministically, so it cannot drift
from the code. The model contributes only the narrative (README, changelog).
``TemplateDocWriter`` is the fallback when the narrative writer is unavailable.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from ..schemas import ChangeSet, FileChange
from .base import AgentContext, AgentResult
from .model_agents import ModelAgent
from .verifier import sandbox_env

_EXPORT_SCRIPT = (
    "import importlib, json, sys\n"
    "module, factory = sys.argv[1].split(':')\n"
    "app = getattr(importlib.import_module(module), factory)()\n"
    "print(json.dumps(app.openapi(), indent=2, sort_keys=True))\n"
)


def export_openapi(ctx: AgentContext) -> tuple[dict[str, Any] | None, str]:
    """Import the app in a subprocess inside the sandbox and dump its schema."""
    scratch = ctx.store.dir / "scratch"
    scratch.mkdir(exist_ok=True)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _EXPORT_SCRIPT, ctx.profile.app_factory],
            cwd=ctx.store.workspace.root,
            env=sandbox_env(scratch, ctx.profile.export_env),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None, "OpenAPI export timed out"
    if proc.returncode != 0:
        return None, proc.stderr[-1500:]
    return json.loads(proc.stdout), ""


def _schema_name(schema: dict[str, Any] | None) -> str:
    if not schema:
        return ""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        return " | ".join(filter(None, (_schema_name(s) or s.get("type", "") for s in schema["anyOf"])))
    if schema.get("type") == "array":
        return f"{_schema_name(schema.get('items')) or 'object'}[]"
    return schema.get("type", "")


def render_api_markdown(openapi: dict[str, Any]) -> str:
    info = openapi.get("info", {})
    lines = [
        f"# {info.get('title', 'API')} - API reference",
        "",
        f"Version {info.get('version', '')}. Generated from the application's OpenAPI schema "
        "(`openapi.json`); do not edit by hand.",
        "",
        "## Endpoints",
        "",
    ]
    for path, operations in sorted(openapi.get("paths", {}).items()):
        for method, op in sorted(operations.items()):
            lines += [f"### `{method.upper()} {path}`", "", op.get("summary", ""), ""]
            if op.get("description"):
                lines += [op["description"], ""]
            params = op.get("parameters", [])
            if params:
                lines += ["| Parameter | In | Required | Type |", "|---|---|---|---|"]
                lines += [
                    f"| `{p['name']}` | {p['in']} | {'yes' if p.get('required') else 'no'} | "
                    f"{_schema_name(p.get('schema'))} |"
                    for p in params
                ]
                lines.append("")
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            if body:
                lines += [f"Request body: `{_schema_name(body)}`", ""]
            lines += ["| Status | Meaning | Body |", "|---|---|---|"]
            for status, response in sorted(op.get("responses", {}).items()):
                schema = response.get("content", {}).get("application/json", {}).get("schema")
                name = _schema_name(schema)
                lines.append(f"| {status} | {response.get('description', '')} | {f'`{name}`' if name else ''} |")
            lines.append("")
    schemas = openapi.get("components", {}).get("schemas", {})
    if schemas:
        lines += ["## Schemas", ""]
    for name, schema in sorted(schemas.items()):
        lines += [f"### `{name}`", ""]
        if schema.get("description"):
            lines += [schema["description"], ""]
        required = set(schema.get("required", []))
        props = schema.get("properties", {})
        if props:
            lines += ["| Field | Type | Required | Description |", "|---|---|---|---|"]
            lines += [
                f"| `{field}` | {_schema_name(prop)} | {'yes' if field in required else 'no'} | "
                f"{prop.get('description', '')} |"
                for field, prop in props.items()
            ]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _with_api_reference(ctx: AgentContext, narrative: ChangeSet) -> AgentResult:
    openapi, error = export_openapi(ctx)
    changes = list(narrative.changes)
    endpoints: list[str] = []
    if openapi is not None:
        endpoints = sorted(f"{m.upper()} {p}" for p, ops in openapi.get("paths", {}).items() for m in ops)
        changes += [
            FileChange(
                path="openapi.json", action="write", content=json.dumps(openapi, indent=2, sort_keys=True) + "\n"
            ),
            FileChange(path="docs/API.md", action="write", content=render_api_markdown(openapi)),
        ]
    manifest = {"openapi_error": error, "endpoints": endpoints}
    return AgentResult(
        artifacts={"docs_report": manifest},
        changeset=ChangeSet(rationale=narrative.rationale, changes=changes),
    )


class DocWriter(ModelAgent):
    role = "technical-writer"
    schema = ChangeSet
    instructions = (
        "Role: technical writer. Update README.md and CHANGELOG.md for the change that was just "
        "implemented. Describe behaviour, configuration and how to run and test the service; keep "
        "existing content that is still true. Do not write docs/API.md or openapi.json - they are "
        "generated from the code."
    )

    def payload(self, ctx: AgentContext) -> dict[str, Any]:
        ws = ctx.store.workspace
        return {
            "requirement_spec": ctx.inputs["requirement_spec"],
            "design": ctx.inputs["design"],
            "plan": ctx.inputs["plan"],
            "current_docs": {p: ws.read(p) for p in ("README.md", "CHANGELOG.md") if ws.exists(p)},
        }

    def finish(self, ctx: AgentContext, output: ChangeSet) -> AgentResult:
        return _with_api_reference(ctx, output)


class TemplateDocWriter:
    """Fallback: a plain but correct changelog entry built from the plan."""

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.store.workspace
        spec, plan = ctx.inputs["requirement_spec"], ctx.inputs["plan"]
        entry = [f"## {spec['title']}", "", spec["summary"], ""]
        entry += [f"- {task['title']} ({', '.join(task['requirement_ids'])})" for task in plan["tasks"]]
        previous = ws.read("CHANGELOG.md") if ws.exists("CHANGELOG.md") else "# Changelog\n"
        head, _, tail = previous.partition("\n")
        content = "\n".join([head, "", *entry, tail.lstrip("\n")]).rstrip() + "\n"
        narrative = ChangeSet(
            rationale="Narrative writer unavailable; generated a changelog entry from the plan.",
            changes=[FileChange(path="CHANGELOG.md", action="write", content=content)],
        )
        return _with_api_reference(ctx, narrative)
