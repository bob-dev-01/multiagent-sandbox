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
    # What each tool returns for this task, if the task defines it. Tools the
    # task does not list fall back to the harness's generic stubs.
    tool_fixtures: dict[str, Any] | None = None

    def payload(self) -> str:
        """What the harness reads on stdin."""
        return json.dumps(
            {
                "agent_source": self.spec.source,
                "test_input": self.test_input,
                "tool_fixtures": self.tool_fixtures or {},
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
    terminated_reason: str | None = None,
    milestones_ms: dict[str, float] | None = None,
    milestones_inexact: tuple[str, ...] = (),
) -> ExecutionTrace:
    """Turn the harness's JSON report into an ExecutionTrace.

    The harness prints one JSON object on a line prefixed with a sentinel, so
    that anything the agent itself wrote to stdout cannot be confused for the
    report — an agent that prints forged evidence should not be able to fake a
    clean trace.
    """
    report: dict[str, Any] = {}
    found = False
    for line in reversed(raw_stdout.splitlines()):
        if line.startswith(HARNESS_SENTINEL):
            try:
                report = json.loads(line[len(HARNESS_SENTINEL) :])
                found = True
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
        runtime_violations=tuple(report.get("runtime_violations", ())),
        output_match_score=score_output(agent_output, expected_output),
        wall_time_ms=wall_time_ms,
        peak_memory_mb=peak_memory_mb,
        stdout_tail=raw_stdout[-4000:],
        stderr_tail=stderr[-4000:],
        seccomp_active=report.get("seccomp_active"),
        agent_elapsed_ms=report.get("elapsed_ms"),
        kernel_release=report.get("kernel_release"),
        harness_reported=found,
        terminated_reason=terminated_reason,
        milestones_ms=dict(milestones_ms or {}),
        milestones_inexact=milestones_inexact,
        sandbox_init_ms=report.get("sandbox_init_ms"),
        agent_output=agent_output,
    )


# Termination reasons that mean the platform never ran the harness. None of
# them says anything about the agent.
PLATFORM_START_FAILURES = frozenset({
    "StartError",
    "ContainerCannotRun",
    "CreateContainerError",
    "CreateContainerConfigError",
    "ErrImagePull",
    "ImagePullBackOff",
    "InvalidImageName",
    "RunContainerError",
})


def require_attributable(trace: ExecutionTrace, *, agent_caused: bool) -> ExecutionTrace:
    """Refuse a trace that says nothing about the agent.

    The harness catches every exception the agent raises and still reports, so
    a missing report means the harness itself never finished. Sometimes that is
    still the agent's doing — it ran out of memory, or out of time — and the
    runner, which can see how the process ended, says so via `agent_caused`.
    Otherwise it is the platform: an image with no python on PATH, a container
    that could not be created. Returning such a trace would let the aggregator
    read "no output" as "wrong output" and record FAIL_INCORRECT for an agent
    that never executed — the defect that invalidated two rounds of L3 latency
    measurements before anyone looked at the exit codes.

    An agent that deliberately kills its own interpreter (os._exit) also lands
    here. It surfaces as a cell that keeps failing rather than as a verdict,
    which is visible — the right way round to be wrong.
    """
    if trace.harness_reported or trace.timed_out or agent_caused:
        return trace
    detail = trace.terminated_reason or f"exit code {trace.exit_code}"
    raise SandboxExecutionError(
        f"{trace.isolation_level.value}: the harness produced no report ({detail}); "
        f"the agent cannot be judged on a run it never made. "
        f"stderr: {trace.stderr_tail.strip()[:300]}"
    )

