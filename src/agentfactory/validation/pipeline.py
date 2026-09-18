"""The Validation Pipeline — Layer 1 to Layer 3, then a verdict.

Layers run cheapest-first and the pipeline short-circuits on a hard failure, so
an agent rejected by static analysis never pays for a container. That ordering
is the whole reason the design is affordable; it is also why Layer 1 and Layer 2
latency is measured separately from Layer 3 (the sandbox overhead in RQ2 is the
difference between them).

Short-circuiting has a consequence worth stating: when Layer 1 rejects an agent,
there is no execution trace, so that run contributes to the safety metrics but
not to the latency distribution of any isolation level. The metrics module keeps
those populations separate rather than averaging over them.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agentfactory.contracts import (
    AgentSpec,
    IsolationLevel,
    LayerReport,
    ValidationLayer,
    ValidationResult,
)
from agentfactory.telemetry.events import EventLog, EventType

from .aggregator import StrictnessProfile, aggregate
from .layer1_static import analyse
from .layer2_policy import EnterprisePolicy, check
from .sandbox.base import SandboxRequest, SandboxRunner, SandboxUnavailableError


@dataclass
class PipelineConfig:
    """Everything that varies between experimental conditions."""

    profile: StrictnessProfile
    policy: EnterprisePolicy
    # Stop before the sandbox when an earlier layer already failed hard.
    short_circuit: bool = True
    use_bandit: bool = True
    # Test inputs the sandbox runs the agent against.
    test_inputs: Sequence[dict[str, Any]] = ()

    @classmethod
    def default(cls, **overrides: Any) -> PipelineConfig:
        base = {
            "profile": StrictnessProfile.balanced(),
            "policy": EnterprisePolicy.load(),
            "test_inputs": ({"case": 1}, {"case": 2}, {"case": 3}, {"case": 4}, {"case": 5}),
        }
        base.update(overrides)
        return cls(**base)  # type: ignore[arg-type]


class ValidationPipeline:
    """Runs the three layers for one isolation level."""

    def __init__(
        self,
        runner: SandboxRunner,
        config: PipelineConfig | None = None,
        *,
        event_log: EventLog | None = None,
    ) -> None:
        self.runner = runner
        self.config = config or PipelineConfig.default()
        self.event_log = event_log

    @property
    def level(self) -> IsolationLevel:
        return self.runner.level

    def _emit(self, event_type: EventType, spec: AgentSpec, **fields: Any) -> None:
        if self.event_log is not None:
            self.event_log.emit(
                event_type,
                task_id=spec.task_id,
                agent_id=spec.agent_id,
                code_sha256=spec.code_sha256,
                isolation_level=self.level,
                **fields,
            )

    def _run_sandbox(
        self, spec: AgentSpec, expected_output: dict[str, Any] | None
    ) -> LayerReport:
        """Execute against every test input; the worst result is the report.

        Worst-case rather than mean: an agent that behaves on four inputs and
        exfiltrates on the fifth is an unsafe agent, and averaging would hide
        exactly the case the pipeline exists to catch.
        """
        started = time.perf_counter()
        traces = []
        for test_input in self.config.test_inputs or ({"case": 1},):
            request = SandboxRequest(
                spec=spec, test_input=test_input, expected_output=expected_output
            )
            try:
                traces.append(self.runner.run(request))
            except SandboxUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 — a crashed run is a result
                from agentfactory.contracts import ExecutionTrace

                traces.append(
                    ExecutionTrace(
                        isolation_level=self.level,
                        exit_code=None,
                        stderr_tail=f"runner error: {type(exc).__name__}: {exc}",
                    )
                )

        # Worst trace: any safety violation dominates; then lowest output match.
        worst = min(
            traces,
            key=lambda t: (not t.has_safety_violation, t.output_match_score),
        )
        latency_ms = (time.perf_counter() - started) * 1000
        return LayerReport(
            layer=ValidationLayer.SANDBOX,
            passed=not worst.has_safety_violation,
            findings=(),
            latency_ms=latency_ms,
            score=worst.output_match_score,
            trace=worst,
        )

    def validate(
        self,
        spec: AgentSpec,
        *,
        expected_output: dict[str, Any] | None = None,
        correctness_score: float | None = None,
        correctness_source: str | None = None,
    ) -> ValidationResult:
        """Run the full pipeline for one agent and return the verdict."""
        reports: list[LayerReport] = []

        static = analyse(spec.source, spec.contract, use_bandit=self.config.use_bandit)
        reports.append(static)
        self._emit(
            EventType.LAYER_COMPLETED,
            spec,
            layer=ValidationLayer.STATIC,
            passed=static.passed,
            latency_ms=static.latency_ms,
            score=static.score,
            detail={"findings": [str(f) for f in static.findings]},
        )

        policy = check(spec.contract, self.config.policy)
        reports.append(policy)
        self._emit(
            EventType.LAYER_COMPLETED,
            spec,
            layer=ValidationLayer.POLICY,
            passed=policy.passed,
            latency_ms=policy.latency_ms,
            score=policy.score,
            detail={"findings": [str(f) for f in policy.findings]},
        )

        blocked = self.config.short_circuit and not (static.passed and policy.passed)
        if not blocked:
            sandbox = self._run_sandbox(spec, expected_output)
            reports.append(sandbox)
            self._emit(
                EventType.LAYER_COMPLETED,
                spec,
                layer=ValidationLayer.SANDBOX,
                passed=sandbox.passed,
                latency_ms=sandbox.latency_ms,
                score=sandbox.score,
                detail={
                    "trace": sandbox.trace.model_dump(mode="json") if sandbox.trace else None
                },
            )

        result = aggregate(
            spec,
            tuple(reports),
            profile=self.config.profile,
            correctness_score=correctness_score,
            correctness_source=correctness_source,
        )
        self._emit(
            EventType.VERDICT_EMITTED,
            spec,
            verdict=result.verdict,
            score=result.confidence,
            latency_ms=result.total_latency_ms,
            detail={
                "short_circuited": blocked,
                "strictness_profile": self.config.profile.name,
            },
        )
        return result
