"""The Task Executor: the deployment gate, execution, and what gets recorded.

Every refusal path is tested on its own, because each one is a post-verdict
control and the gap between FNR and UDR is made of exactly these. The agents
that are actually executed here are benign: SAFE_AGENT, and an agent whose only
"violation" is calling a tool it was not authorised for, which the harness's
stub records without anything real happening.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from agentfactory.contracts import (
    AgentContract,
    ExecutionTrace,
    IsolationLevel,
    LayerReport,
    ValidationLayer,
    ValidationResult,
    Verdict,
)
from agentfactory.executor import DeploymentRefused, TaskExecutor
from agentfactory.orchestrator.factory import AgentFactory, Outcome, TaskSpecification
from agentfactory.registry.embeddings import HashingEmbedder
from agentfactory.registry.memory import InMemoryRegistry
from agentfactory.validation.sandbox.base import SandboxExecutionError

from .conftest import SAFE_AGENT, UNDECLARED_TOOL_AGENT, make_spec

EXPECTED = {"error_counts": {"connection": 2}, "total": 2}  # SAFE_AGENT keys by field 3


@pytest.fixture
def task() -> TaskSpecification:
    return TaskSpecification(
        task_id="T001",
        description="Count ERROR lines per service in a log file.",
        available_tools=["read_log_file"],
        expected_output=EXPECTED,
    )


def _pass_for(spec) -> ValidationResult:
    return ValidationResult(
        agent_id=spec.agent_id, task_id=spec.task_id, code_sha256=spec.code_sha256,
        verdict=Verdict.PASS, confidence=0.9, isolation_level=IsolationLevel.L1,
        layer_reports=(LayerReport(layer=ValidationLayer.STATIC, passed=True),),
    )


class FakeRunner:
    """Returns a scripted trace, or raises; never executes anything."""

    def __init__(self, level: IsolationLevel = IsolationLevel.L1, **trace: Any) -> None:
        self.level = level
        self.trace = trace
        self.raises: Exception | None = None
        self.calls = 0

    def available(self) -> bool:
        return True

    def run(self, request) -> ExecutionTrace:
        self.calls += 1
        if self.raises:
            raise self.raises
        fields = {"exit_code": 0, "output_match_score": 1.0, "agent_output": {"total": 2}}
        fields.update(self.trace)
        return ExecutionTrace(isolation_level=self.level, **fields)


# ------------------------------------------------------------------- gate


def test_a_verdict_other_than_pass_is_refused(task, safe_contract) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    runner = FakeRunner()
    with pytest.raises(DeploymentRefused, match="not PASS"):
        TaskExecutor(runner).execute(
            spec, task, approved_sha256=spec.code_sha256, verdict=Verdict.FAIL_INCORRECT
        )
    assert runner.calls == 0


def test_code_other_than_the_approved_code_is_refused(task, safe_contract) -> None:
    approved = make_spec(SAFE_AGENT, safe_contract)
    swapped = make_spec(SAFE_AGENT + "\n# changed after validation\n", safe_contract)
    runner = FakeRunner()
    with pytest.raises(DeploymentRefused, match="differs from what was approved"):
        TaskExecutor(runner).execute(
            swapped, task, approved_sha256=approved.code_sha256, verdict=Verdict.PASS
        )
    assert runner.calls == 0


def test_a_contract_that_does_not_fit_the_task_is_refused(task) -> None:
    """Warm path: a registered agent may hold tools the new task never offered."""
    contract = AgentContract(
        task_spec="x", authorized_tools=("read_log_file", "create_incident_ticket")
    )
    spec = make_spec(SAFE_AGENT, contract)
    with pytest.raises(DeploymentRefused, match="create_incident_ticket"):
        TaskExecutor(FakeRunner()).execute(
            spec, task, approved_sha256=spec.code_sha256, verdict=Verdict.PASS
        )


def test_high_risk_without_l3_is_refused(task, safe_contract) -> None:
    task.risk_tier = "high"
    spec = make_spec(SAFE_AGENT, safe_contract)
    for high in (None, FakeRunner(IsolationLevel.L2)):
        with pytest.raises(DeploymentRefused, match="needs an L3 runner"):
            TaskExecutor(FakeRunner(), high_risk_runner=high).execute(
                spec, task, approved_sha256=spec.code_sha256, verdict=Verdict.PASS
            )


def test_high_risk_runs_on_l3(task, safe_contract) -> None:
    task.risk_tier = "high"
    spec = make_spec(SAFE_AGENT, safe_contract)
    standard, l3 = FakeRunner(), FakeRunner(IsolationLevel.L3)
    record = TaskExecutor(standard, high_risk_runner=l3).execute(
        spec, task, approved_sha256=spec.code_sha256, verdict=Verdict.PASS
    )
    assert (standard.calls, l3.calls) == (0, 1)
    assert record.isolation_level is IsolationLevel.L3


# -------------------------------------------------------------- execution


def test_a_clean_correct_run_succeeds_and_returns_the_output(
    task, safe_contract, local_runner
) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    record = TaskExecutor(local_runner).execute(
        spec, task, approved_sha256=spec.code_sha256, verdict=Verdict.PASS
    )
    assert record.succeeded and not record.unsafe
    assert record.tsr == 1.0
    assert record.output == EXPECTED


def test_misbehaviour_after_pass_is_unsafe_and_output_is_withheld(
    task, safe_contract, local_runner
) -> None:
    """A catastrophic error: it passed validation and misbehaved anyway."""
    spec = make_spec(UNDECLARED_TOOL_AGENT, safe_contract)
    record = TaskExecutor(local_runner).execute(
        spec, task, approved_sha256=spec.code_sha256, verdict=Verdict.PASS
    )
    assert record.unsafe and not record.succeeded
    assert any("unauthorized tool" in r for r in record.safety_reasons)
    assert record.output is None


def test_a_partial_match_is_a_failed_execution(task, safe_contract) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    record = TaskExecutor(FakeRunner(output_match_score=0.5)).execute(
        spec, task, approved_sha256=spec.code_sha256, verdict=Verdict.PASS
    )
    assert not record.succeeded and not record.unsafe
    assert record.tsr == 0.0


def test_no_expected_output_means_success_is_not_claimed(task, safe_contract) -> None:
    task.expected_output = None
    spec = make_spec(SAFE_AGENT, safe_contract)
    record = TaskExecutor(FakeRunner()).execute(
        spec, task, approved_sha256=spec.code_sha256, verdict=Verdict.PASS
    )
    assert not record.succeeded


def test_an_infrastructure_failure_is_not_recorded_as_a_result(task, safe_contract) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    registry = InMemoryRegistry()
    registry.register(spec, _pass_for(spec), [1.0, 0.0])
    runner = FakeRunner()
    runner.raises = SandboxExecutionError("node went away")
    with pytest.raises(SandboxExecutionError):
        TaskExecutor(runner, registry=registry).execute(
            spec, task, approved_sha256=spec.code_sha256, verdict=Verdict.PASS
        )
    assert registry._agents[spec.agent_id].tsr_history == []


def test_executions_and_violations_reach_the_registry(task, safe_contract) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    registry = InMemoryRegistry()
    registry.register(spec, _pass_for(spec), [1.0, 0.0])
    executor = TaskExecutor(FakeRunner(network_attempts=("evil.example:443",)), registry=registry)
    executor.execute(spec, task, approved_sha256=spec.code_sha256, verdict=Verdict.PASS)
    assert registry._agents[spec.agent_id].tsr_history == [0.0]
    assert registry.violations(spec.agent_id)
    # Never served again.
    assert registry.find_reusable([1.0, 0.0], similarity_threshold=0.5) == []


# ---------------------------------------------------------------- registry


def test_registry_refuses_anything_but_pass(safe_contract) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    failed = _pass_for(spec).model_copy(update={"verdict": Verdict.FAIL_UNSAFE})
    with pytest.raises(ValueError, match="refusing"):
        InMemoryRegistry().register(spec, failed, [1.0])


def test_rolling_tsr_below_the_floor_flags_for_revalidation(safe_contract) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    registry = InMemoryRegistry()
    registry.register(spec, _pass_for(spec), [1.0, 0.0])
    for tsr in (1.0, 1.0, 0.0):  # mean 0.67 < 0.70
        registry.record_execution(spec.agent_id, "T001", tsr)
    assert registry.counts()["flagged"] == 1
    assert registry.find_reusable([1.0, 0.0], similarity_threshold=0.5) == []


def test_reuse_needs_similarity_and_healthy_tsr(safe_contract) -> None:
    spec = make_spec(SAFE_AGENT, safe_contract)
    registry = InMemoryRegistry()
    registry.register(spec, _pass_for(spec), [1.0, 0.0])
    assert registry.find_reusable([0.0, 1.0]) == []
    assert registry.find_reusable([1.0, 0.0])[0].agent_id == spec.agent_id
    for tsr in (1.0, 1.0, 1.0, 1.0, 0.0):  # mean 0.80, not above the floor
        registry.record_execution(spec.agent_id, "T001", tsr)
    assert registry.find_reusable([1.0, 0.0]) == []


# -------------------------------------------------------- orchestrator path


@dataclass
class _Generation:
    spec: Any
    model: str = "replay"
    cost_usd: float = 0.0


@dataclass
class _Generator:
    sources: list[str]
    contract: AgentContract
    calls: int = 0
    specs: list = field(default_factory=list)

    def generate(self, *, task_id: str, **_: Any) -> _Generation:
        source = self.sources[min(self.calls, len(self.sources) - 1)]
        self.calls += 1
        spec = make_spec(source, self.contract, task_id=task_id)
        self.specs.append(spec)
        return _Generation(spec=spec)


class _Pipeline:
    def __init__(self, verdict: Verdict = Verdict.PASS) -> None:
        self.verdict = verdict

    def validate(self, spec, **_: Any) -> ValidationResult:
        return _pass_for(spec).model_copy(update={"verdict": self.verdict})


def _factory(generator, runner, registry=None, pipeline=None) -> AgentFactory:
    registry = registry if registry is not None else InMemoryRegistry()
    return AgentFactory(
        generator=generator,
        pipeline=pipeline or _Pipeline(),
        registry=registry,
        embedder=HashingEmbedder(),
        executor=TaskExecutor(runner, registry=registry),
    )


def test_cold_path_executes_before_counting_success(task, safe_contract, local_runner) -> None:
    factory = _factory(_Generator([SAFE_AGENT], safe_contract), local_runner)
    result = factory.handle_task(task)
    assert result.outcome is Outcome.GENERATED_AND_DEPLOYED
    assert result.execution is not None and result.execution.output == EXPECTED
    assert result.mean_time_to_deploy_sec <= result.time_to_first_action_sec


def test_warm_path_runs_the_registered_agent(task, safe_contract, local_runner) -> None:
    registry = InMemoryRegistry()
    generator = _Generator([SAFE_AGENT], safe_contract)
    factory = _factory(generator, local_runner, registry)
    first = factory.handle_task(task)
    second = factory.handle_task(task)
    assert second.outcome is Outcome.REUSED and second.reused
    assert second.agent_id == first.agent_id
    assert generator.calls == 1
    assert registry.counts()["total_reuses"] == 1


def test_a_refused_reuse_falls_back_to_generation(task, safe_contract, local_runner) -> None:
    registry = InMemoryRegistry()
    wide = AgentContract(task_spec="x", authorized_tools=("read_log_file", "query_cmdb"))
    _factory(_Generator([SAFE_AGENT], wide), local_runner, registry).handle_task(
        TaskSpecification(task_id="T000", description=task.description,
                          available_tools=["read_log_file", "query_cmdb"],
                          expected_output=EXPECTED)
    )
    generator = _Generator([SAFE_AGENT], safe_contract)
    result = _factory(generator, local_runner, registry).handle_task(task)
    assert result.refusals and "query_cmdb" in result.refusals[0]
    assert result.outcome is Outcome.GENERATED_AND_DEPLOYED and not result.reused
    assert generator.calls == 1
    assert registry.counts()["total_reuses"] == 0


def test_unsafe_at_execution_is_its_own_outcome(task, safe_contract, local_runner) -> None:
    contract = AgentContract(task_spec="x", authorized_tools=("read_log_file",))
    factory = _factory(_Generator([UNDECLARED_TOOL_AGENT], contract), local_runner)
    result = factory.handle_task(task)
    assert result.outcome is Outcome.EXECUTED_UNSAFE
    assert not result.succeeded


def test_execution_infrastructure_failure_is_reported(task, safe_contract) -> None:
    runner = FakeRunner()
    runner.raises = SandboxExecutionError("pod never started")
    result = _factory(_Generator([SAFE_AGENT], safe_contract), runner).handle_task(task)
    assert result.outcome is Outcome.FAILED_INFRASTRUCTURE
    assert result.executions == []


def test_cold_path_refusal_escalates(task, safe_contract) -> None:
    task.risk_tier = "high"
    result = _factory(_Generator([SAFE_AGENT], safe_contract), FakeRunner()).handle_task(task)
    assert result.outcome is Outcome.DEPLOYMENT_REFUSED
    assert "L3" in (result.escalation_reason or "")
