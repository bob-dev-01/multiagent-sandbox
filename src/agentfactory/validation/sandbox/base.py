"""Common interface for the three sandbox isolation levels.

Every runner takes the same request and returns the same `ExecutionTrace`, so
the isolation level is a configuration choice rather than a code path the rest
of the system has to know about. That is what makes RQ2 answerable: the only
thing that varies between L1, L2 and L3 is the runner.

Resource limits come from the agent's own contract, not from a fixed ceiling
(OQ-15 in architecture.md). Each runner translates them into whatever its
substrate understands — rlimits, container resources, pod limits.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agentfactory.contracts import AgentSpec, ExecutionTrace, IsolationLevel

# The harness that wraps the agent inside the sandbox.
HARNESS_PATH = Path(__file__).resolve().parent / "harness.py"

# The harness prefixes its JSON report with this so that anything the agent
# itself printed cannot be mistaken for the report.
HARNESS_SENTINEL = "__AGENTFACTORY_RESULT__"


def resolve_executable(name: str) -> str:
    """Absolute path to a CLI, or raise.

    On Windows `az` is a .cmd shim that subprocess cannot launch by bare name;
    it fails with WinError 2, which reads like a missing file rather than a
    resolution problem. `kubectl` is a real .exe and works either way, which is
    how L2 could be broken while L3 looked fine.
    """
    found = shutil.which(name) or shutil.which(f"{name}.cmd") or shutil.which(f"{name}.exe")
    if not found:
        raise SandboxUnavailableError(f"{name} not found on PATH")
    return found


class SandboxExecutionError(RuntimeError):
    """The sandbox could not run the agent.

    Deliberately distinct from a badly behaved agent. An agent that crashes,
    times out or returns nonsense produces a valid ExecutionTrace and a real
    verdict. A container that never started produces neither — and must never
    be recorded as though the agent had run and failed, because that turns an
    infrastructure outage into evidence about the agent.
    """


class SandboxUnavailableError(RuntimeError):
    """The substrate for this isolation level is not reachable.

    Raised rather than silently degrading to a weaker sandbox: quietly running
    an L3 agent under L1 isolation would be a security failure disguised as
    resilience, and would also corrupt the experiment.
    """


@dataclass(frozen=True)
class SandboxRequest:
    """One execution of one agent against one test input."""

    spec: AgentSpec
    test_input: dict[str, Any]
    expected_output: dict[str, Any] | None = None
    run_id: str = "adhoc"

    def payload(self) -> str:
        """What the harness reads on stdin."""
        return json.dumps(
            {
                "agent_source": self.spec.source,
                "test_input": self.test_input,
                "authorized_tools": list(self.spec.contract.authorized_tools),
                "evidence_requirements": list(self.spec.contract.evidence_requirements),
                "max_duration_sec": self.spec.contract.resource_limits.max_duration_sec,
            }
        )


@runtime_checkable
class SandboxRunner(Protocol):
    """Contract every isolation level implements."""

    level: IsolationLevel

    def available(self) -> bool:
        """Whether this runner can execute right now, without side effects."""
        ...

    def run(self, request: SandboxRequest) -> ExecutionTrace:
        """Execute the agent and return what was observed."""
        ...


def score_output(actual: Any, expected: dict[str, Any] | None) -> float:
    """Fraction of expected keys the agent got right.

    A deliberately simple, deterministic measure. It is the automatic signal
    only; the graded correctness score that H_3 depends on comes from the human
    rubric and the LLM judge, which see the whole output rather than key
    equality.
    """
    if not expected:
        return 1.0 if actual is not None else 0.0
    if not isinstance(actual, dict):
        return 0.0
    matched = sum(1 for key, value in expected.items() if key in actual and actual[key] == value)
    return matched / len(expected)


def parse_harness_result(
    raw_stdout: str,
    *,
    level: IsolationLevel,
    exit_code: int | None,
    wall_time_ms: float,
    peak_memory_mb: float = 0.0,
    timed_out: bool = False,
    stderr: str = "",
    expected_output: dict[str, Any] | None = None,
    extra_syscalls: tuple[str, ...] = (),
) -> ExecutionTrace:
    """Turn the harness's JSON report into an ExecutionTrace.

    The harness prints one JSON object on a line prefixed with a sentinel, so
    that anything the agent itself wrote to stdout cannot be confused for the
    report — an agent that prints forged evidence should not be able to fake a
    clean trace.
    """
    report: dict[str, Any] = {}
    for line in reversed(raw_stdout.splitlines()):
        if line.startswith(HARNESS_SENTINEL):
            try:
                report = json.loads(line[len(HARNESS_SENTINEL) :])
            except json.JSONDecodeError:
                report = {}
            break

    agent_output = report.get("output")
    return ExecutionTrace(
        isolation_level=level,
        exit_code=exit_code,
        timed_out=timed_out,
        syscalls_blocked=tuple(report.get("syscalls_blocked", ())) + extra_syscalls,
        network_attempts=tuple(report.get("network_attempts", ())),
        fs_violations=tuple(report.get("fs_violations", ())),
        unauthorized_tool_calls=tuple(report.get("unauthorized_tool_calls", ())),
        output_match_score=score_output(agent_output, expected_output),
        wall_time_ms=wall_time_ms,
        peak_memory_mb=peak_memory_mb,
        stdout_tail=raw_stdout[-4000:],
        stderr_tail=stderr[-4000:],
        seccomp_active=report.get("seccomp_active"),
    )

