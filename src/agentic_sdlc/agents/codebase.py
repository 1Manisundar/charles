"""Codebase analyst: a deterministic structural map of the workspace.

Brownfield reasoning starts from facts, not from a model's recollection: the
import graph, HTTP routes and tables are extracted with ``ast`` and handed to
the architect, and the architect's claims about impacted modules are later
checked against this same map by an exit gate.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from .base import AgentContext, AgentResult

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*;", re.I | re.S)
_ADD_COLUMN = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.I)
_CONSTRAINT_WORDS = {"primary", "foreign", "unique", "check", "constraint"}


def _module_name(path: str) -> str:
    return path[: -len(".py")].replace("/", ".").removesuffix(".__init__")


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _is_within(directory: str, ancestor: str) -> bool:
    return ancestor == "" or directory == ancestor or directory.startswith(ancestor + "/")


def _routes(tree: ast.AST, path: str) -> list[dict[str, str]]:
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                for kw in node.value.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        prefixes[target.id] = str(kw.value.value)
    routes = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr in _HTTP_METHODS
                and isinstance(deco.func.value, ast.Name)
                and deco.args
                and isinstance(deco.args[0], ast.Constant)
            ):
                routes.append(
                    {
                        "method": deco.func.attr.upper(),
                        "path": prefixes.get(deco.func.value.id, "") + str(deco.args[0].value),
                        "handler": node.name,
                        "file": path,
                    }
                )
    return routes


def _tables(text: str, path: str) -> list[dict[str, Any]]:
    tables: dict[str, dict[str, Any]] = {}
    for name, body in _CREATE_TABLE.findall(text):
        columns = [seg.split()[0] for seg in body.split(",") if seg.split()]
        tables[name] = {
            "name": name,
            "columns": [c for c in columns if c.lower() not in _CONSTRAINT_WORDS],
            "file": path,
        }
    for name, column in _ADD_COLUMN.findall(text):
        tables.setdefault(name, {"name": name, "columns": [], "file": path})["columns"].append(column)
    return list(tables.values())


class CodebaseAnalyst:
    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.store.workspace
        files = ws.files()
        py_files = [f for f in files if f.endswith(".py")]
        by_module = {_module_name(f): f for f in py_files}

        modules, routes, tables = [], [], []
        dependents: dict[str, set[str]] = {}
        for path in py_files:
            text = ws.read(path)
            try:
                tree = ast.parse(text)
            except SyntaxError:
                modules.append({"path": path, "module": _module_name(path), "loc": 0, "imports": [], "symbols": []})
                continue
            package = _module_name(path).rsplit(".", 1)[0] if "." in _module_name(path) else ""
            imports: set[str] = set()
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:  # relative import: resolve against this module's package
                        base = ".".join(p for p in (package, base) if p)
                    names = [base] + [f"{base}.{a.name}" for a in node.names]
                imports.update(by_module[n] for n in names if n in by_module and by_module[n] != path)
            # pytest injects fixtures from every conftest.py at or above a test module,
            # a dependency that no import statement shows.
            if path.startswith(f"{ctx.profile.test_dir}/") and not path.endswith("conftest.py"):
                imports.update(
                    f
                    for f in py_files
                    if f.rsplit("/", 1)[-1] == "conftest.py" and _is_within(_parent(path), _parent(f))
                )
            for target in imports:
                dependents.setdefault(target, set()).add(path)
            modules.append(
                {
                    "path": path,
                    "module": _module_name(path),
                    "loc": len(text.splitlines()),
                    "imports": sorted(imports),
                    "symbols": [
                        n.name
                        for n in tree.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    ],
                }
            )
            routes += _routes(tree, path)
            tables += _tables(text, path)

        test_prefix = f"{ctx.profile.test_dir}/"
        codebase_map = {
            "empty": not files,
            "files": files,
            "modules": modules,
            "dependents": {k: sorted(v) for k, v in sorted(dependents.items())},
            "routes": routes,
            "tables": tables,
            "tests": [f for f in py_files if f.startswith(test_prefix)],
        }
        return AgentResult(artifacts={"codebase_map": codebase_map})
