"""Task Executor — runs a validated agent for real, and keeps watching it.

The orchestrator used to stop at "deployable": a PASS was registered and the
task counted as done, though nothing had run. That left three of the study's
metrics with no measurement point at all — Task Success Rate, the Catastrophic
Error Rate, and UDR at the deployment boundary rather than the verdict boundary
(OQ-10). This module is that point.

Two rules shape it.

**Execution stays inside a sandbox.** There is no production environment in
this study (OQ-11), and the architecture's second line of defence (section
10.3) is that monitoring continues after PASS. So the Executor runs agents
through the same `SandboxRunner` interface as Layer 3, and reads the same trace.
An agent that misbehaves here despite its PASS is a catastrophic error: it is
recorded as one, reported to the registry so it is never served again, and its
output is withheld from the caller.

**Deployment is a gate, and it fails closed.** Between verdict and execution
the Executor re-checks what a PASS does not guarantee on its own:

- the code about to run is byte-for-byte the code that was approved;
- the agent's declared tools fit the task it is being deployed for — on the
  warm path a registered agent is matched by similarity, and its contract was
  written for a different task;
- a high-risk task runs under L3, and is refused if no L3 runner is available
  rather than run somewhere weaker.

A refusal is not an error. It is the post-verdict control doing its job, and
it is what makes UDR differ from FNR: an unsafe agent that passed validation
but was refused here counts toward FNR and not toward UDR.

Infrastructure failures are neither refusals nor results. They propagate, as
in the validation pipeline, so a run the agent never made cannot be scored as
the agent failing.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentfactory.contracts import (
    AgentSpec,
    ExecutionTrace,
    IsolationLevel,
    LayerReport,
    ValidationLayer,
    Verdict,
)
from agentfactory.telemetry.events import EventLog, EventType
from agentfactory.validation.aggregator import StrictnessProfile, assess
from agentfactory.validation.sandbox.base import SandboxRequest, SandboxRunner

HIGH_RISK = "high"


class DeploymentRefused(Exception):
    """The gate declined to run this agent. Not an infrastructure failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TaskLike(Protocol):
    task_id: str
    available_tools: list[str]
    expected_output: dict[str, Any] | None
    risk_tier: str
    task_input: Mapping[str, Any]
    tool_fixtures: Mapping[str, Any]


class ExecutionRegistry(Protocol):
    def record_execution(
        self, agent_id: uuid.UUID, task_id: str, tsr: float, **kw: Any
    ) -> None: ...
    def record_violation(self, agent_id: uuid.UUID, violation: str, **kw: Any) -> None: ...


@dataclass(frozen=True)
class ExecutionRecord:
    """One deployment of one agent to one task."""

    agent_id: uuid.UUID
    task_id: str
    code_sha256: str
    isolation_level: IsolationLevel
    reused: bool
    succeeded: bool
    unsafe: bool
    safety_reasons: tuple[str, ...]
    failure_reasons: tuple[str, ...]
    trace: ExecutionTrace
    latency_ms: float
    # The agent's result, or None. Withheld whenever the run was unsafe: output
    # produced by an agent caught misbehaving is not evidence of anything the
    # caller should act on.
    output: Any = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def tsr(self) -> float:
        return 1.0 if self.succeeded else 0.0


class TaskExecutor:
    """Deploys approved agents into a sandbox and records what happened."""

    def __init__(
        self,
        runner: SandboxRunner,
        *,
        high_risk_runner: SandboxRunner | None = None,
        registry: ExecutionRegistry | None = None,
        event_log: EventLog | None = None,
        profile: StrictnessProfile | None = None,
    ) -> None:
        self.runner = runner
        self.high_risk_runner = high_risk_runner
        self.registry = registry
        self.event_log = event_log
        # Success at execution is judged by the same rule as validation. Using
        # a looser one here would let the Executor report TSR for outputs the
        # pipeline would have rejected.
        self.profile = profile or StrictnessProfile.strict()

    # ---------------------------------------------------------------- gate

    def _runner_for(self, task: TaskLike) -> SandboxRunner:
        if task.risk_tier != HIGH_RISK:
            return self.runner
        runner = self.high_risk_runner
        if runner is None or runner.level is not IsolationLevel.L3:
            raise DeploymentRefused(
                f"task {task.task_id} is high-risk and needs an L3 runner; "
                f"none is configured (have {getattr(runner, 'level', None)})"
            )
        return runner

    @staticmethod
    def check(spec: AgentSpec, task: TaskLike, *, approved_sha256: str, verdict: Verdict) -> None:
        """Everything that must hold before an approved agent may run.

        Raises DeploymentRefused with the first reason found. Static so the
        orchestrator can ask before committing to a warm-path agent.
        """
        if not verdict.is_pass:
            raise DeploymentRefused(f"verdict is {verdict.value}, not PASS")
        actual = hashlib.sha256(spec.source.encode("utf-8")).hexdigest()
        if not (actual == spec.code_sha256 == approved_sha256):
            raise DeploymentRefused(
                "code differs from what was approved "
                f"(approved {approved_sha256[:12]}, about to run {actual[:12]})"
            )
        undeclared = sorted(set(spec.contract.authorized_tools) - set(task.available_tools))
        if undeclared:
            raise DeploymentRefused(
                f"agent is authorised for tools task {task.task_id} does not provide: "
                f"{undeclared}"
            )

    # ------------------------------------------------------------- execute

    def execute(
        self,
        spec: AgentSpec,
        task: TaskLike,
        *,
        approved_sha256: str,
        verdict: Verdict,
        reused: bool = False,
        run_id: str | None = None,
    ) -> ExecutionRecord:
        """Gate, run, classify, record. Raises DeploymentRefused or an infra error."""
        self.check(spec, task, approved_sha256=approved_sha256, verdict=verdict)
        runner = self._runner_for(task)

        started = time.perf_counter()
        trace = runner.run(
            SandboxRequest(
                spec=spec,
                test_input=dict(task.task_input),
                expected_output=task.expected_output,
                tool_fixtures=dict(task.tool_fixtures),
            )
        )
        latency_ms = (time.perf_counter() - started) * 1000

        # The same two-dimensional assessment validation uses, applied to the
        # execution trace on its own.
        report = LayerReport(
            layer=ValidationLayer.SANDBOX,
            passed=not trace.has_safety_violation,
            score=trace.output_match_score,
            trace=trace,
        )
        assessment = assess((report,), spec, self.profile)
        failures = list(assessment.correctness_reasons)
        if task.expected_output is None:
            # Nothing to check the output against: success cannot be shown, so
            # it is not claimed.
            failures.append("task has no expected output; success cannot be verified")

        record = ExecutionRecord(
            agent_id=spec.agent_id,
            task_id=task.task_id,
            code_sha256=spec.code_sha256,
            isolation_level=trace.isolation_level,
            reused=reused,
            succeeded=not assessment.unsafe and not failures,
            unsafe=assessment.unsafe,
            safety_reasons=assessment.safety_reasons,
            failure_reasons=tuple(failures),
            trace=trace,
            latency_ms=latency_ms,
            output=None if assessment.unsafe else trace.agent_output,
        )
        self._record(record, run_id=run_id)
        return record

    def _record(self, record: ExecutionRecord, *, run_id: str | None) -> None:
        if self.registry is not None:
            if record.unsafe:
                # Reported first and on its own: whatever else fails, the agent
                # must stop being served.
                self.registry.record_violation(
                    record.agent_id,
                    "; ".join(record.safety_reasons)[:2000],
                    detail={"task_id": record.task_id, "code_sha256": record.code_sha256},
                    run_id=run_id,
                )
            self.registry.record_execution(
                record.agent_id, record.task_id, record.tsr,
                latency_ms=record.latency_ms, run_id=run_id,
            )
        if self.event_log is not None:
            self.event_log.emit(
                EventType.EXECUTION_COMPLETED,
                task_id=record.task_id,
                agent_id=record.agent_id,
                code_sha256=record.code_sha256,
                isolation_level=record.isolation_level,
                passed=record.succeeded,
                score=record.tsr,
                latency_ms=record.latency_ms,
                detail={
                    "reused": record.reused,
                    "unsafe": record.unsafe,
                    "safety_reasons": list(record.safety_reasons),
                    "failure_reasons": list(record.failure_reasons),
                    "trace": record.trace.model_dump(mode="json", exclude={"agent_output"}),
                },
            )
