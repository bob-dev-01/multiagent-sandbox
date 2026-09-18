"""Layer 2 — policy check. Declarative, no execution, milliseconds.

Validates what the agent *claims* against enterprise policy: are the declared
tools real and permitted, is the declared network posture allowed for this task
domain, are the declared resource limits inside approved bounds, are the
required evidence artifacts declared.

This layer deliberately checks the declaration and not the behaviour. An agent
that declares `network_access: false` and then opens a socket passes here and is
caught by Layer 3. That division is the point: Layer 2 is about whether the
contract is *admissible*, Layer 3 is about whether the agent *honours* it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentfactory.contracts import (
    AgentContract,
    Finding,
    LayerReport,
    ResourceLimits,
    Severity,
    ValidationLayer,
)

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "policy" / "enterprise_policy.yaml"


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    requires_network: bool = False
    requires_approval: bool = False
    domains: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class EnterprisePolicy:
    """The enterprise tool registry and the bounds a contract must fit inside."""

    tools: dict[str, ToolDefinition]
    denied_tools: frozenset[str]
    max_cpu_cores: float
    max_memory_mb: int
    max_duration_sec: int
    network_allowed_domains: frozenset[str]
    required_evidence: frozenset[str]
    require_approval_actions: frozenset[str]

    @classmethod
    def load(cls, path: Path | str | None = None) -> EnterprisePolicy:
        path = Path(path) if path else DEFAULT_POLICY_PATH
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        tools: dict[str, ToolDefinition] = {}
        for entry in raw.get("tools", []) or []:
            if isinstance(entry, str):
                tools[entry] = ToolDefinition(name=entry)
                continue
            name = entry["name"]
            tools[name] = ToolDefinition(
                name=name,
                requires_network=bool(entry.get("requires_network", False)),
                requires_approval=bool(entry.get("requires_approval", False)),
                domains=frozenset(entry.get("domains", []) or []),
            )

        limits = raw.get("resource_bounds", {}) or {}
        return cls(
            tools=tools,
            denied_tools=frozenset(raw.get("denied_tools", []) or []),
            max_cpu_cores=float(limits.get("max_cpu_cores", 2)),
            max_memory_mb=int(limits.get("max_memory_mb", 512)),
            max_duration_sec=int(limits.get("max_duration_sec", 30)),
            network_allowed_domains=frozenset(raw.get("network_allowed_domains", []) or []),
            required_evidence=frozenset(raw.get("required_evidence", []) or []),
            require_approval_actions=frozenset(raw.get("require_approval_actions", []) or []),
        )


def _finding(rule: str, severity: Severity, message: str) -> Finding:
    return Finding(
        layer=ValidationLayer.POLICY,
        rule_id=rule,
        severity=severity,
        message=message,
    )


def _check_tools(contract: AgentContract, policy: EnterprisePolicy) -> list[Finding]:
    findings: list[Finding] = []
    for tool in contract.authorized_tools:
        if tool in policy.denied_tools:
            findings.append(_finding("POL101", Severity.HIGH, f"tool is explicitly denied: {tool}"))
        elif tool not in policy.tools:
            # Deny by default: an undeclared tool is not an unknown tool, it is a
            # forbidden one.
            findings.append(
                _finding("POL102", Severity.HIGH, f"tool is not in the enterprise registry: {tool}")
            )
    return findings


def _check_network(contract: AgentContract, policy: EnterprisePolicy) -> list[Finding]:
    findings: list[Finding] = []
    wants_network = contract.resource_limits.network_access
    needing = [
        t
        for t in contract.authorized_tools
        if t in policy.tools and policy.tools[t].requires_network
    ]

    if wants_network and not needing:
        findings.append(
            _finding(
                "POL201",
                Severity.HIGH,
                "network access requested but no authorized tool requires it",
            )
        )
    if needing and not wants_network:
        # Not a safety problem — the agent under-declared and will fail at
        # runtime. Worth surfacing, not worth rejecting.
        findings.append(
            _finding(
                "POL202",
                Severity.LOW,
                f"tools {sorted(needing)} require network but network_access is false",
            )
        )
    return findings


def _check_limits(limits: ResourceLimits, policy: EnterprisePolicy) -> list[Finding]:
    findings: list[Finding] = []
    if limits.cpu_cores > policy.max_cpu_cores:
        findings.append(
            _finding(
                "POL301",
                Severity.HIGH,
                f"cpu_cores {limits.cpu_cores} exceeds enterprise bound {policy.max_cpu_cores}",
            )
        )
    if limits.memory_mb > policy.max_memory_mb:
        findings.append(
            _finding(
                "POL302",
                Severity.HIGH,
                f"memory_mb {limits.memory_mb} exceeds enterprise bound {policy.max_memory_mb}",
            )
        )
    if limits.max_duration_sec > policy.max_duration_sec:
        findings.append(
            _finding(
                "POL303",
                Severity.HIGH,
                f"max_duration_sec {limits.max_duration_sec} exceeds bound {policy.max_duration_sec}",
            )
        )
    return findings


def _check_evidence(contract: AgentContract, policy: EnterprisePolicy) -> list[Finding]:
    missing = policy.required_evidence - set(contract.evidence_requirements)
    if missing:
        return [
            _finding(
                "POL401",
                Severity.MEDIUM,
                f"contract omits required evidence artifacts: {sorted(missing)}",
            )
        ]
    return []


def _check_approval(contract: AgentContract, policy: EnterprisePolicy) -> list[Finding]:
    declared = set(contract.security_policy.require_human_approval_for)
    missing = policy.require_approval_actions - declared
    if missing:
        return [
            _finding(
                "POL501",
                Severity.HIGH,
                f"contract does not require approval for high-impact actions: {sorted(missing)}",
            )
        ]
    return []


def check(contract: AgentContract, policy: EnterprisePolicy | None = None) -> LayerReport:
    """Run Layer 2 over one contract.

    The aggregate compliance score is the fraction of checks that produced no
    finding, which gives the threshold work in RQ4 something continuous to tune
    rather than a bare boolean.
    """
    started = time.perf_counter()
    policy = policy or EnterprisePolicy.load()

    groups = (
        _check_tools(contract, policy),
        _check_network(contract, policy),
        _check_limits(contract.resource_limits, policy),
        _check_evidence(contract, policy),
        _check_approval(contract, policy),
    )
    findings = [f for group in groups for f in group]
    clean_groups = sum(1 for group in groups if not group)
    score = clean_groups / len(groups)

    high = [f for f in findings if f.severity is Severity.HIGH]
    return LayerReport(
        layer=ValidationLayer.POLICY,
        passed=not high,
        findings=tuple(findings),
        latency_ms=(time.perf_counter() - started) * 1000,
        score=score,
    )
