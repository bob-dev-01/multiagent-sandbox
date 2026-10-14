"""The Agent Factory orchestrator — the loop that ties everything together.

Owns the decisions the rest of the system does not make for itself:
reuse-vs-generate, what to do with each verdict, when to give up and escalate.
It also owns every outbound model and embedding call, which is what keeps the
sandbox free of credentials and network (architecture.md section 10.1).

Failure handling is new here. Task 7 specifies the happy path and the
regeneration cap; it says nothing about what happens when the model API rate
limits, the registry is unreachable, or the sandbox substrate disappears
mid-batch. Those are not exotic cases over a 2,700-run experiment, and a run
that dies at hour six having written no usable record is worse than one that
degrades predictably — so each failure mode has a defined outcome below.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from agentfactory.contracts import AgentSpec, ExecutionTrace, ValidationResult, Verdict
from agentfactory.executor import DeploymentRefused, ExecutionRecord, TaskExecutor
from agentfactory.generator.agent_generator import describe_shape
from agentfactory.judge.evidence import tool_data
from agentfactory.telemetry.events import EventLog, EventType
from agentfactory.validation.sandbox.base import SandboxExecutionError, SandboxUnavailableError

logger = logging.getLogger(__name__)

MAX_REGENERATION_ATTEMPTS = 3
API_RETRY_ATTEMPTS = 3
API_RETRY_BASE_DELAY_SEC = 2.0


class Outcome(StrEnum):
    """How a task ended.

    With a Task Executor configured, REUSED and GENERATED_AND_DEPLOYED mean the
    agent ran and produced the expected result. Without one the factory is in
    validation-only mode and they mean "approved for deployment" — nothing ran.
    """

    REUSED = "reused"
    GENERATED_AND_DEPLOYED = "generated_and_deployed"
    ESCALATED_UNSAFE = "escalated_unsafe"
    ESCALATED_EXHAUSTED = "escalated_exhausted"
    # Passed validation, then stopped at the deployment gate (wrong code, a
    # contract that does not fit, no L3 for a high-risk task). Goes to a human.
    DEPLOYMENT_REFUSED = "deployment_refused"
    # Passed validation, ran, and misbehaved: a catastrophic error.
    EXECUTED_UNSAFE = "executed_unsafe"
    # Ran cleanly but did not produce the expected result.
    EXECUTED_FAILED = "executed_failed"
    FAILED_INFRASTRUCTURE = "failed_infrastructure"


@dataclass
class TaskResult:
    task_id: str
    outcome: Outcome
    agent_id: uuid.UUID | None = None
    verdict: Verdict | None = None
    attempts: int = 0
    reused: bool = False
    time_to_first_action_sec: float | None = None
    mean_time_to_deploy_sec: float | None = None
    cost_usd: float = 0.0
    escalation_reason: str | None = None
    history: list[ValidationResult] = field(default_factory=list)
    # Every deployment attempted for this task, in order: a refused or failed
    # warm-path reuse, then the cold-path agent, if it came to that.
    executions: list[ExecutionRecord] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.outcome in (Outcome.REUSED, Outcome.GENERATED_AND_DEPLOYED)

    @property
    def execution(self) -> ExecutionRecord | None:
        return self.executions[-1] if self.executions else None


@dataclass
class TaskSpecification:
    task_id: str
    description: str
    available_tools: list[str]
    expected_output: dict[str, Any] | None = None
    risk_tier: str = "standard"  # standard | high — decides L3 escalation
    # What the deployed agent is run on. The corpus's stub tools return fixed
    # data, so this mainly identifies the case.
    task_input: dict[str, Any] = field(default_factory=lambda: {"case": 1})
    # What the task's tools return; empty means the harness's generic stubs.
    tool_fixtures: dict[str, Any] = field(default_factory=dict)
    # Task-specific scoring guide for the judge, when the task has one (corpus
    # tasks do). The judge is calibrated with it, so it is given it here too.
    correctness_rubric: str | None = None


class Registry(Protocol):
    def find_reusable(self, query_embedding: list[float], **kwargs: Any) -> list[Any]: ...
    def mark_reused(self, agent_id: uuid.UUID) -> None: ...
    def register(self, spec: AgentSpec, result: ValidationResult, embedding: list[float], **kw: Any) -> None: ...
    def load_code(self, agent_id: uuid.UUID) -> str | None: ...
    def record_execution(self, agent_id: uuid.UUID, task_id: str, tsr: float, **kw: Any) -> None: ...
    def record_violation(self, agent_id: uuid.UUID, violation: str, **kw: Any) -> None: ...


def _retry_api(call: Any, *, attempts: int = API_RETRY_ATTEMPTS, what: str = "API call") -> Any:
    """Retry a model call with exponential backoff.

    Retries transient failures only. A refusal or a schema violation is a result,
    not a transport problem, and retrying it just spends money to get the same
    answer.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            transient = any(
                token in message
                for token in ("rate limit", "429", "overloaded", "timeout", "503", "502", "connection")
            )
            last = exc
            if not transient or attempt == attempts:
                raise
            delay = API_RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
            logger.warning("%s failed (%s); retrying in %.1fs", what, exc, delay)
            time.sleep(delay)
    raise last  # pragma: no cover


class AgentFactory:
    """One orchestrator per experiment run."""

    def __init__(
        self,
        *,
        generator: Any,
        pipeline: Any,
        registry: Registry | None = None,
        embedder: Any = None,
        judge: Any = None,
        executor: TaskExecutor | None = None,
        event_log: EventLog | None = None,
        max_attempts: int = MAX_REGENERATION_ATTEMPTS,
    ) -> None:
        self.generator = generator
        self.pipeline = pipeline
        self.registry = registry
        self.embedder = embedder
        self.judge = judge
        self.executor = executor
        self.event_log = event_log
        self.max_attempts = max_attempts

    @property
    def _run_id(self) -> str | None:
        return self.event_log.run_id if self.event_log is not None else None

    def _emit(self, event_type: EventType, **fields: Any) -> None:
        if self.event_log is not None:
            self.event_log.emit(event_type, **fields)

    # ------------------------------------------------------------ reuse

    def _try_reuse(self, task: TaskSpecification) -> Any | None:
        """Look for a registered agent that already covers this task."""
        if self.registry is None or self.embedder is None:
            return None
        try:
            embedding = self.embedder.embed(task.description)
            matches = self.registry.find_reusable(embedding, limit=1)
        except Exception as exc:  # noqa: BLE001
            # A registry outage must not stop the run: generating a fresh agent
            # is always correct, just more expensive. Degrading the other way —
            # serving a stale agent — would be the unsafe direction.
            logger.warning("registry lookup failed (%s); falling back to generation", exc)
            return None
        return matches[0] if matches else None

    # --------------------------------------------------------- the loop

    def handle_task(self, task: TaskSpecification) -> TaskResult:
        """Run one task end to end."""
        started = time.perf_counter()
        result = TaskResult(task_id=task.task_id, outcome=Outcome.FAILED_INFRASTRUCTURE)

        reusable = self._try_reuse(task)
        if reusable is not None and self.executor is None:
            # Validation-only mode: the match is the result.
            self.registry.mark_reused(reusable.agent_id)  # type: ignore[union-attr]
            self._emit(
                EventType.AGENT_REUSED,
                task_id=task.task_id,
                agent_id=reusable.agent_id,
                score=reusable.similarity,
                detail={"mean_tsr": reusable.mean_tsr},
            )
            return TaskResult(
                task_id=task.task_id,
                outcome=Outcome.REUSED,
                agent_id=reusable.agent_id,
                verdict=Verdict.PASS,
                reused=True,
                time_to_first_action_sec=time.perf_counter() - started,
                mean_time_to_deploy_sec=time.perf_counter() - started,
            )
        if reusable is not None:
            warm = self._deploy_reused(task, reusable, result, started)
            if warm is not None:
                return warm
            # Refused at the gate: the match does not fit this task. Generating
            # a fresh agent is always a safe fallback.

        feedback: str | None = None
        # The interface of each tool — what a tool registry documents — so the
        # generator does not have to guess field names. Shapes only, no data.
        tool_shapes = {
            name: describe_shape(response)
            for name, response in tool_data(task.available_tools, task.tool_fixtures).items()
        }
        for attempt in range(1, self.max_attempts + 1):
            result.attempts = attempt
            try:
                # `feedback` is bound as a default rather than captured: the
                # closure would otherwise read whatever the loop variable holds
                # at call time, which is only safe by accident.
                generation = _retry_api(
                    lambda _feedback=feedback: self.generator.generate(
                        task_id=task.task_id,
                        task_spec=task.description,
                        available_tools=task.available_tools,
                        failure_feedback=_feedback,
                        tool_shapes=tool_shapes,
                    ),
                    what=f"generation attempt {attempt}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("generation failed for %s: %s", task.task_id, exc)
                self._emit(
                    EventType.RUN_FAILED,
                    task_id=task.task_id,
                    detail={"stage": "generation", "error": str(exc), "attempt": attempt},
                )
                result.outcome = Outcome.FAILED_INFRASTRUCTURE
                result.escalation_reason = f"generation failed: {exc}"
                return result

            spec = generation.spec
            result.agent_id = spec.agent_id
            result.cost_usd += generation.cost_usd
            self._emit(
                EventType.AGENT_GENERATED,
                task_id=task.task_id,
                agent_id=spec.agent_id,
                code_sha256=spec.code_sha256,
                model=generation.model,
                detail={"attempt": attempt, "cost_usd": round(generation.cost_usd, 6)},
            )

            try:
                validation = self.pipeline.validate(
                    spec,
                    expected_output=task.expected_output,
                    tool_fixtures=task.tool_fixtures,
                    # Graded after the sandbox, on the trace: the judge sees
                    # what the agent did, as it does in calibration.
                    grade=lambda trace, spec=spec: self._grade(spec, task, trace),
                )
            except (SandboxExecutionError, SandboxUnavailableError) as exc:
                return self._infrastructure_failure(result, task, "validation", exc)
            result.history.append(validation)
            result.verdict = validation.verdict

            if validation.verdict.is_pass:
                self._register(spec, validation, task)
                if self.executor is None:
                    result.outcome = Outcome.GENERATED_AND_DEPLOYED
                    result.mean_time_to_deploy_sec = time.perf_counter() - started
                    result.time_to_first_action_sec = result.mean_time_to_deploy_sec
                    self._emit(
                        EventType.AGENT_DEPLOYED,
                        task_id=task.task_id,
                        agent_id=spec.agent_id,
                        verdict=validation.verdict,
                        latency_ms=validation.total_latency_ms,
                    )
                    return result
                return self._deploy(
                    task, spec, result, started,
                    approved_sha256=validation.code_sha256,
                    verdict=validation.verdict,
                    reused=False,
                )

            if validation.verdict.is_safety_failure:
                # Never regenerate against a safety control.
                result.outcome = Outcome.ESCALATED_UNSAFE
                result.escalation_reason = "; ".join(
                    str(f) for f in validation.all_findings[:5]
                ) or "sandbox observed a policy violation"
                self._emit(
                    EventType.ESCALATED,
                    task_id=task.task_id,
                    agent_id=spec.agent_id,
                    verdict=validation.verdict,
                    detail={"reason": result.escalation_reason},
                )
                return result

            # FAIL_INCORRECT — retry with the validator's own words as feedback.
            feedback = self._feedback(validation)
            self._emit(
                EventType.REGENERATION_REQUESTED,
                task_id=task.task_id,
                agent_id=spec.agent_id,
                verdict=validation.verdict,
                detail={"attempt": attempt, "feedback": feedback[:1000]},
            )

        result.outcome = Outcome.ESCALATED_EXHAUSTED
        result.escalation_reason = f"still incorrect after {self.max_attempts} attempts"
        self._emit(
            EventType.ESCALATED,
            task_id=task.task_id,
            agent_id=result.agent_id,
            detail={"reason": result.escalation_reason},
        )
        return result

    # ------------------------------------------------------- deployment

    def _deploy(
        self,
        task: TaskSpecification,
        spec: AgentSpec,
        result: TaskResult,
        started: float,
        *,
        approved_sha256: str,
        verdict: Verdict,
        reused: bool,
    ) -> TaskResult:
        """Cold path: run the freshly validated agent; a refusal ends the task."""
        try:
            return self._execute(
                task, spec, result, started,
                approved_sha256=approved_sha256, verdict=verdict, reused=reused,
            )
        except DeploymentRefused as exc:
            self._refused(task, spec, result, exc, reused=reused)
            result.outcome = Outcome.DEPLOYMENT_REFUSED
            result.escalation_reason = exc.reason
            return result

    def _deploy_reused(
        self, task: TaskSpecification, match: Any, result: TaskResult, started: float
    ) -> TaskResult | None:
        """Warm path. Returns None when the gate refuses the match."""
        assert self.registry is not None
        code_b64 = self.registry.load_code(match.agent_id)
        if code_b64 is None:
            result.refusals.append(f"registered agent {match.agent_id} has no stored code")
            return None
        spec = match.to_spec(code_b64)
        try:
            outcome = self._execute(
                task, spec, result, started,
                approved_sha256=match.code_sha256, verdict=Verdict.PASS, reused=True,
            )
        except DeploymentRefused as exc:
            self._refused(task, spec, result, exc, reused=True)
            return None
        # Counted only once the agent has actually been deployed: a match the
        # gate turned away was not a reuse.
        self.registry.mark_reused(match.agent_id)
        self._emit(
            EventType.AGENT_REUSED,
            task_id=task.task_id,
            agent_id=match.agent_id,
            score=match.similarity,
            detail={"mean_tsr": match.mean_tsr, "source_task_id": match.task_id},
        )
        outcome.reused = True
        outcome.verdict = Verdict.PASS
        return outcome

    def _execute(
        self,
        task: TaskSpecification,
        spec: AgentSpec,
        result: TaskResult,
        started: float,
        *,
        approved_sha256: str,
        verdict: Verdict,
        reused: bool,
    ) -> TaskResult:
        """Hand the agent to the Executor and turn its record into an outcome.

        Raises DeploymentRefused for the caller to route; the two paths treat a
        refusal differently.
        """
        assert self.executor is not None
        result.mean_time_to_deploy_sec = time.perf_counter() - started
        try:
            record = self.executor.execute(
                spec, task,
                approved_sha256=approved_sha256, verdict=verdict,
                reused=reused, run_id=self._run_id,
            )
        except (SandboxExecutionError, SandboxUnavailableError) as exc:
            return self._infrastructure_failure(result, task, "execution", exc)

        result.executions.append(record)
        result.agent_id = spec.agent_id
        result.time_to_first_action_sec = time.perf_counter() - started
        if record.unsafe:
            result.outcome = Outcome.EXECUTED_UNSAFE
            result.escalation_reason = "; ".join(record.safety_reasons)
        elif not record.succeeded:
            result.outcome = Outcome.EXECUTED_FAILED
            result.escalation_reason = "; ".join(record.failure_reasons)
        else:
            result.outcome = Outcome.REUSED if reused else Outcome.GENERATED_AND_DEPLOYED
        self._emit(
            EventType.AGENT_DEPLOYED,
            task_id=task.task_id,
            agent_id=spec.agent_id,
            code_sha256=spec.code_sha256,
            verdict=verdict,
            passed=record.succeeded,
            latency_ms=record.latency_ms,
            detail={"outcome": result.outcome.value, "reused": reused},
        )
        return result

    def _refused(
        self,
        task: TaskSpecification,
        spec: AgentSpec,
        result: TaskResult,
        exc: DeploymentRefused,
        *,
        reused: bool,
    ) -> None:
        result.refusals.append(exc.reason)
        self._emit(
            EventType.DEPLOYMENT_REFUSED,
            task_id=task.task_id,
            agent_id=spec.agent_id,
            code_sha256=spec.code_sha256,
            detail={"reason": exc.reason, "reused": reused},
        )

    def _infrastructure_failure(
        self, result: TaskResult, task: TaskSpecification, stage: str, exc: Exception
    ) -> TaskResult:
        """A run the agent never made. Recorded, never scored."""
        logger.error("%s failed for %s: %s", stage, task.task_id, exc)
        self._emit(
            EventType.RUN_FAILED,
            task_id=task.task_id,
            agent_id=result.agent_id,
            detail={"stage": stage, "error": str(exc)[:1000]},
        )
        result.outcome = Outcome.FAILED_INFRASTRUCTURE
        result.escalation_reason = f"{stage} failed: {exc}"
        return result

    # ------------------------------------------------------------ parts

    def _grade(
        self, spec: AgentSpec, task: TaskSpecification, trace: ExecutionTrace | None
    ) -> tuple[float | None, str | None]:
        """Correctness score from the judge, when the judge is allowed to speak.

        Given the same evidence a human rater gets on the calibration sheet:
        the code, what the tools return, the expected output, the task's own
        scoring guide when it has one, and the sandbox run.
        """
        if self.judge is None:
            return None, None
        try:
            judgement = _retry_api(
                lambda: self.judge.score(
                    spec, trace, task.expected_output,
                    task_rubric=task.correctness_rubric,  # as calibrate_judge.py passes it
                    tools_return=tool_data(task.available_tools, task.tool_fixtures),
                ),
                what="judge",
            )
        except Exception as exc:  # noqa: BLE001
            # An unavailable judge means no correctness dimension for this run,
            # not a failed run. The metrics pipeline treats a missing score as
            # missing rather than as zero.
            logger.warning("judge unavailable for %s: %s", spec.agent_id, exc)
            return None, None
        return float(judgement.score), "llm_judge"

    def _register(
        self, spec: AgentSpec, validation: ValidationResult, task: TaskSpecification
    ) -> None:
        if self.registry is None or self.embedder is None:
            return
        try:
            embedding = self.embedder.embed(task.description)
            self.registry.register(spec, validation, embedding)
            self._emit(
                EventType.AGENT_REGISTERED,
                task_id=task.task_id,
                agent_id=spec.agent_id,
                code_sha256=spec.code_sha256,
            )
        except Exception as exc:  # noqa: BLE001
            # The agent is still deployable for this task; it just will not be
            # available for reuse. Worth a warning, not worth failing the task.
            logger.warning("could not register %s: %s", spec.agent_id, exc)

    @staticmethod
    def _feedback(validation: ValidationResult) -> str:
        lines = [f"Verdict: {validation.verdict.value}"]
        for report in validation.layer_reports:
            if report.findings:
                lines.append(f"[{report.layer.value}] " + "; ".join(str(f) for f in report.findings))
            if report.trace is not None:
                trace = report.trace
                if trace.timed_out:
                    lines.append("[sandbox] the agent exceeded its declared execution time")
                if trace.exit_code not in (0, None):
                    lines.append(f"[sandbox] exited with code {trace.exit_code}")
                if trace.stderr_tail.strip():
                    lines.append(f"[sandbox stderr] {trace.stderr_tail[-800:]}")
                lines.append(f"[sandbox] output match score {trace.output_match_score:.2f}")
        if validation.correctness_score is not None:
            lines.append(f"Graded correctness: {validation.correctness_score:.1f}/3")
        return "\n".join(lines)
