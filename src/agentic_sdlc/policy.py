"""Policy guardrails: security, compliance and change control.

Policy is data (``Policy``) plus pure check functions that return ``Finding``s.
Gates decide what a finding means for the workflow; nothing here has side
effects. Every rule is deterministic so that a guardrail never depends on a
model "agreeing" to be safe.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import sys
from collections.abc import Iterable

from pydantic import BaseModel, Field

from .models import Finding


class ProjectProfile(BaseModel):
    """What the orchestrator needs to know about the codebase it works on."""

    source_package: str = "urlshortener"
    test_dir: str = "tests"
    app_factory: str = "urlshortener.main:create_app"
    # Environment for exporting the OpenAPI schema without touching a real database.
    export_env: dict[str, str] = Field(default_factory=lambda: {"URLSHORTENER_DB_PATH": ":memory:"})
    # Owned by the documentation stage; implementation tasks may not write them.
    docs_paths: list[str] = Field(default_factory=lambda: ["README.md", "CHANGELOG.md", "docs/*", "openapi.json"])


class Policy(BaseModel):
    # --- change control
    protected_paths: list[str] = Field(
        default_factory=lambda: ["*/migrations.py", "migrations/*", "requirements.txt", "Dockerfile", "pytest.ini"]
    )
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [".env", ".env.*", "*.pem", "*.key", ".github/*", "*id_rsa*"]
    )
    max_files_per_change: int = 20
    max_bytes_per_file: int = 100_000
    change_freeze: bool = False
    # --- security
    allowed_third_party: list[str] = Field(
        default_factory=lambda: ["fastapi", "starlette", "pydantic", "uvicorn", "httpx", "pytest"]
    )
    # --- compliance
    pii_columns: list[str] = Field(
        default_factory=lambda: ["ip", "ip_address", "client_ip", "remote_addr", "email", "phone", "ssn"]
    )
    min_coverage: float = 85.0
    require_requirement_tests: bool = True
    # --- autonomy budgets (exceeding any of them is a safe-stop, not a failure)
    max_agent_invocations: int = 80
    max_rework_rounds: int = 2
    max_wall_clock_s: float = 900.0

    def is_protected(self, path: str) -> bool:
        return _matches(path, self.protected_paths)

    def is_forbidden(self, path: str) -> bool:
        return _matches(path, self.forbidden_paths)

    def high_impact_reasons(self, kind: str, writes: Iterable[str], baseline: Iterable[str]) -> list[str]:
        """Why a task needs change-control approval before it may run.

        Only changes to something that already exists are high impact: creating
        a schema in an empty repository cannot damage existing data.
        """
        existing = set(baseline)
        reasons = []
        if kind == "migration" and existing:
            reasons.append("schema migration against a codebase with existing data")
        for path in writes:
            if path in existing and self.is_protected(path):
                reasons.append(f"modifies protected path {path}")
        return reasons


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path.rsplit("/", 1)[-1], pat) for pat in patterns)


# --------------------------------------------------------------------------- security checks

_SECRET_PATTERNS = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("api-token", re.compile(r"\b(?:sk-ant-|sk_live_|ghp_|xox[bp]-)[A-Za-z0-9_\-]{16,}")),
    (
        "hardcoded-credential",
        re.compile(r"""(?i)\b(password|passwd|secret|api_key|apikey|token)\b\s*[:=]\s*["'][^"'\s]{8,}["']"""),
    ),
]

_DANGEROUS_CALLS = {
    "eval": "eval() executes arbitrary code",
    "exec": "exec() executes arbitrary code",
    "os.system": "os.system() runs a shell command",
    "pickle.loads": "pickle.loads() deserialises untrusted data",
    "pickle.load": "pickle.load() deserialises untrusted data",
    "yaml.load": "yaml.load() without SafeLoader is unsafe",
}


def scan_secrets(path: str, text: str) -> list[Finding]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rule, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(
                    Finding(
                        rule=f"secret/{rule}",
                        category="security",
                        severity="high",
                        message="possible hard-coded secret",
                        path=path,
                        line=lineno,
                    )
                )
    return findings


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _is_dynamic_sql(arg: ast.AST) -> bool:
    """True if a query is built by string interpolation rather than parameters."""
    if isinstance(arg, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in arg.values)
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Mod, ast.Add)):
        return True
    return isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "format"


def scan_python(path: str, text: str, policy: Policy, profile: ProjectProfile) -> list[Finding]:
    """AST checks: dangerous calls, injectable SQL, shell use, unapproved imports."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [
            Finding(
                rule="python/syntax",
                category="security",
                severity="high",
                message=f"does not parse: {exc.msg}",
                path=path,
                line=exc.lineno,
            )
        ]

    def finding(rule: str, severity: str, message: str, node: ast.AST) -> Finding:
        return Finding(
            rule=rule,
            category="security",
            severity=severity,  # type: ignore[arg-type]
            message=message,
            path=path,
            line=getattr(node, "lineno", None),
        )

    allowed = set(sys.stdlib_module_names) | set(policy.allowed_third_party) | {profile.source_package}
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.level:
                continue  # relative import inside the package
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for module in modules:
                top = module.split(".")[0]
                if top and top not in allowed:
                    findings.append(
                        finding(
                            "supply-chain/unapproved-import", "high", f"'{top}' is not an approved dependency", node
                        )
                    )
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in _DANGEROUS_CALLS:
                findings.append(finding(f"dangerous-call/{name}", "high", _DANGEROUS_CALLS[name], node))
            if name.startswith("subprocess.") and any(
                kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in node.keywords
            ):
                findings.append(finding("dangerous-call/shell", "high", "subprocess with shell=True", node))
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"execute", "executemany", "executescript"}:
                if node.args and _is_dynamic_sql(node.args[0]):
                    findings.append(
                        finding(
                            "injection/sql", "high", "SQL built by string interpolation; use bound parameters", node
                        )
                    )
        elif isinstance(node, ast.ExceptHandler) and node.type is None:
            findings.append(finding("quality/bare-except", "low", "bare 'except:' hides failures", node))
    return findings


# --------------------------------------------------------------------------- compliance checks


def scan_pii(path: str, text: str, policy: Policy) -> list[Finding]:
    """Flag schema columns that would persist raw personal data."""
    if not re.search(r"(?i)\b(create\s+table|add\s+column)\b", text):
        return []
    names = "|".join(re.escape(c) for c in policy.pii_columns)
    column = re.compile(rf"(?i)(?<![A-Za-z0-9_])({names})\s+(text|varchar|char|blob|integer)\b")
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = column.search(line)
        if match:
            findings.append(
                Finding(
                    rule="compliance/raw-pii-column",
                    category="compliance",
                    severity="high",
                    message=f"column '{match.group(1)}' would persist raw personal data; store a salted hash or drop it",
                    path=path,
                    line=lineno,
                )
            )
    return findings


def scan_file(path: str, text: str, policy: Policy, profile: ProjectProfile) -> list[Finding]:
    """Every content check that applies to one file."""
    is_test = path.startswith(f"{profile.test_dir}/")
    findings = [] if is_test else scan_secrets(path, text)
    if path.endswith(".py"):
        findings += scan_python(path, text, policy, profile)
        if not is_test:
            findings += scan_pii(path, text, policy)
    return findings


# --------------------------------------------------------------------------- change-control checks


def check_change_scope(paths: Iterable[str], sizes: dict[str, int], policy: Policy) -> list[Finding]:
    paths = list(paths)
    findings = []

    def finding(rule: str, message: str, path: str | None = None) -> Finding:
        return Finding(rule=rule, category="change_control", severity="high", message=message, path=path)

    if len(paths) > policy.max_files_per_change:
        findings.append(
            finding("change/too-large", f"{len(paths)} files in one change (limit {policy.max_files_per_change})")
        )
    for path in paths:
        if policy.is_forbidden(path):
            findings.append(finding("change/forbidden-path", "agents may never write this path", path))
        if sizes.get(path, 0) > policy.max_bytes_per_file:
            findings.append(finding("change/file-too-large", f"exceeds {policy.max_bytes_per_file} bytes", path))
    return findings
