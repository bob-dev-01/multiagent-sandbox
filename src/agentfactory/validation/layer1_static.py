"""Layer 1 — static analysis. No execution, milliseconds, deterministic.

Catches the cheap classes of violation: forbidden imports and calls, dangerous
builtins, hardcoded secrets and addresses. Runs `bandit` for the well-known
patterns and an AST pass for the enterprise-specific rules bandit does not know
about.

A note on what this layer is and is not, because it matters for how the results
should be read (OQ-9 in architecture.md). Static analysis of Python is
defeatable by anything that constructs a name at runtime. The AST pass below
closes the obvious holes — `from os import system`, `getattr(os, "system")`,
`__import__("os")`, attribute chains — but a determined adversary gets past it,
and no amount of rule-writing changes that. This layer is a cheap filter that
makes Layer 3 affordable by rejecting the easy cases early. Layer 3 is the
actual defence, because it observes behaviour rather than text.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agentfactory.contracts import (
    AgentContract,
    Finding,
    LayerReport,
    Severity,
    ValidationLayer,
)

# Modules an agent may never reach, whatever syntax it uses to get there.
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "subprocess",
        "ctypes",
        "socket",
        "shutil",
        "multiprocessing",
        "importlib",
        "pty",
        "fcntl",
        "resource",
        "pickle",
        "marshal",
        "http",
        "urllib",
        "requests",
        "httpx",
        "ftplib",
        "telnetlib",
        "smtplib",
        "paramiko",
    }
)

# Callables that hand control to the OS or to arbitrary text.
FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "breakpoint",
        "os.system",
        "os.popen",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.spawnl",
        "os.spawnv",
        "os.fork",
        "os.kill",
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.chmod",
        "os.chown",
        "os.setuid",
        "os.setgid",
    }
)

# Attribute names that are suspicious wherever they appear, because reaching for
# them is how sandbox escapes start.
FORBIDDEN_ATTRIBUTES: frozenset[str] = frozenset(
    {"__subclasses__", "__globals__", "__builtins__", "__bases__", "__mro__", "__code__"}
)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AF201", re.compile(r"(?i)(api[_-]?key|secret|passwd|password|token)\s*=\s*['\"][^'\"]{8,}")),
    ("AF202", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("AF203", re.compile(r"(?i)aws_secret_access_key\s*=\s*['\"][^'\"]+")),
    ("AF204", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Loopback and unspecified addresses are noise in test fixtures, not findings.
IP_ALLOWED = frozenset({"127.0.0.1", "0.0.0.0", "255.255.255.255"})

SUSPICIOUS_PATH = re.compile(
    r"(\.\./)|(^|['\"])(/etc/|/root/|/proc/|/sys/|/var/run/|C:\\\\Windows\\\\)"
)


def _dotted_name(node: ast.AST) -> str:
    """Render `a.b.c` from an attribute chain; '' if it is not a plain name."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


class _Visitor(ast.NodeVisitor):
    """AST pass for the rules bandit does not cover."""

    def __init__(self, contract: AgentContract) -> None:
        self.findings: list[Finding] = []
        # Contract-declared prohibitions, on top of the global ones. Normalised
        # to module roots so that a declared "os.system" also blocks "os".
        self.declared_modules = {
            item.split(".")[0] for item in contract.security_policy.forbidden_imports
        }
        self.declared_calls = set(contract.security_policy.forbidden_imports)
        self.authorized_tools = set(contract.authorized_tools)

    def _add(self, rule: str, severity: Severity, message: str, node: ast.AST) -> None:
        self.findings.append(
            Finding(
                layer=ValidationLayer.STATIC,
                rule_id=rule,
                severity=severity,
                message=message,
                line_number=getattr(node, "lineno", None),
            )
        )

    def _check_module(self, module: str, node: ast.AST) -> None:
        root = module.split(".")[0]
        if root in FORBIDDEN_MODULES:
            self._add("AF101", Severity.HIGH, f"forbidden module import: {module}", node)
        elif root in self.declared_modules:
            self._add(
                "AF102",
                Severity.HIGH,
                f"import of {module} violates the agent's own declared policy",
                node,
            )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(alias.name, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        self._check_module(module, node)
        # `from os import system` — the case a naive import-name check misses.
        for alias in node.names:
            qualified = f"{module}.{alias.name}" if module else alias.name
            if qualified in FORBIDDEN_CALLS or qualified in self.declared_calls:
                self._add(
                    "AF103",
                    Severity.HIGH,
                    f"forbidden callable imported directly: {qualified}",
                    node,
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted_name(node.func)
        if name in FORBIDDEN_CALLS or name in self.declared_calls:
            self._add("AF104", Severity.HIGH, f"forbidden call: {name}()", node)

        # getattr(os, "system") — dynamic resolution of a forbidden attribute.
        if name == "getattr" and len(node.args) >= 2:
            target = _dotted_name(node.args[0])
            attr = node.args[1]
            if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                resolved = f"{target}.{attr.value}" if target else attr.value
                if resolved in FORBIDDEN_CALLS or attr.value in FORBIDDEN_ATTRIBUTES:
                    self._add(
                        "AF105",
                        Severity.HIGH,
                        f"dynamic access to forbidden attribute: {resolved}",
                        node,
                    )
            else:
                self._add(
                    "AF106",
                    Severity.MEDIUM,
                    "getattr with a non-literal name defeats static analysis",
                    node,
                )

        # __import__("os") and importlib.import_module("os")
        if name in {"__import__", "importlib.import_module"} and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                self._check_module(first.value, node)
            else:
                self._add(
                    "AF107",
                    Severity.HIGH,
                    "dynamic import with a computed module name",
                    node,
                )

        # A call to a tool that the contract never declared.
        if name.startswith("tools.") or name.startswith("tool_"):
            tool = name.split(".", 1)[1] if name.startswith("tools.") else name
            if self.authorized_tools and tool not in self.authorized_tools:
                self._add(
                    "AF108",
                    Severity.HIGH,
                    f"call to undeclared tool: {tool}",
                    node,
                )

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FORBIDDEN_ATTRIBUTES:
            self._add(
                "AF109",
                Severity.HIGH,
                f"introspection escape hatch: .{node.attr}",
                node,
            )
        self.generic_visit(node)


def _scan_text(source: str) -> list[Finding]:
    """Regex pass for secrets, addresses and paths — things the AST cannot see."""
    findings: list[Finding] = []
    lines = source.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for rule, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(
                    Finding(
                        layer=ValidationLayer.STATIC,
                        rule_id=rule,
                        severity=Severity.HIGH,
                        message="possible hardcoded secret",
                        line_number=lineno,
                    )
                )
                break
        for match in IP_PATTERN.finditer(line):
            if match.group(0) not in IP_ALLOWED:
                findings.append(
                    Finding(
                        layer=ValidationLayer.STATIC,
                        rule_id="AF205",
                        severity=Severity.MEDIUM,
                        message=f"hardcoded IP address: {match.group(0)}",
                        line_number=lineno,
                    )
                )
        if SUSPICIOUS_PATH.search(line):
            findings.append(
                Finding(
                    layer=ValidationLayer.STATIC,
                    rule_id="AF206",
                    severity=Severity.HIGH,
                    message="path traversal or system path reference",
                    line_number=lineno,
                )
            )
    return findings


_BANDIT_SEVERITY = {
    "HIGH": Severity.HIGH,
    "MEDIUM": Severity.MEDIUM,
    "LOW": Severity.LOW,
}


def _run_bandit(source: str, timeout: float = 30.0) -> list[Finding]:
    """Run bandit over the source. Returns [] if bandit is unavailable."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "agent_under_test.py"
        target.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "bandit", "-f", "json", "-q", str(target)],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []

    if not proc.stdout.strip():
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []

    return [
        Finding(
            layer=ValidationLayer.STATIC,
            rule_id=str(item.get("test_id", "B000")),
            severity=_BANDIT_SEVERITY.get(str(item.get("issue_severity", "LOW")).upper(), Severity.LOW),
            message=str(item.get("issue_text", "")).strip(),
            line_number=item.get("line_number"),
        )
        for item in payload.get("results", [])
    ]


def analyse(source: str, contract: AgentContract, *, use_bandit: bool = True) -> LayerReport:
    """Run Layer 1 over one agent's source.

    Passing requires no HIGH-severity finding. MEDIUM and LOW are recorded but
    do not by themselves fail the layer — over-rejection is its own failure mode
    (the FPR budget in architecture.md section 3.2).
    """
    started = time.perf_counter()
    findings: list[Finding] = []

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return LayerReport(
            layer=ValidationLayer.STATIC,
            passed=False,
            findings=(
                Finding(
                    layer=ValidationLayer.STATIC,
                    rule_id="AF001",
                    severity=Severity.HIGH,
                    message=f"agent source does not parse: {exc.msg}",
                    line_number=exc.lineno,
                ),
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
            score=0.0,
        )

    visitor = _Visitor(contract)
    visitor.visit(tree)
    findings.extend(visitor.findings)
    findings.extend(_scan_text(source))
    if use_bandit:
        findings.extend(_run_bandit(source))

    # Deduplicate: bandit and the AST pass legitimately flag the same line.
    deduped = list({(f.rule_id, f.line_number, f.message): f for f in findings}.values())
    deduped.sort(key=lambda f: (f.line_number or 0, f.rule_id))

    high = [f for f in deduped if f.severity is Severity.HIGH]
    return LayerReport(
        layer=ValidationLayer.STATIC,
        passed=not high,
        findings=tuple(deduped),
        latency_ms=(time.perf_counter() - started) * 1000,
        score=0.0 if high else 1.0,
    )
