"""Orchestrator routing tests, using fakes for the model-backed parts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from agentfactory.contracts import (
    AgentContract,
    AgentSpec,
    IsolationLevel,
    LayerReport,
    ValidationLayer,
    ValidationResult,
    Verdict,
)
from agentfactory.orchestrator.factory import AgentFactory, Outcome, TaskSpecification


@dataclass
class FakeGeneration:
    spec: AgentSpec
    reasoning: str = "fake"
    model: str = "fake-model"
    cost_usd: float = 0.001


class FakeGenerator:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def generate(self, *, task_id: str, task_spec: str, available_tools: list[str],
                 failure_feedback: str | None = None) -> FakeGeneration:
        self.calls.append(failure_feedback)
        spec = AgentSpec.from_source(
            task_id=task_id,
            source=f"def run(task_input, tools):\n    return {{'attempt': {len(self.calls)}}}\n",
            contract=AgentContract(task_spec=task_spec, authorized_tools=("read_log_file",)),
            generator_model="fake-model",
        )
        return FakeGeneration(spec=spec)


class FakePipeline:
    """Returns a scripted sequence of verdicts."""

    def __init__(self, verdicts: list[Verdict]) -> None:
        self.verdicts = list(verdicts)
        self.calls = 0

    def validate(self, spec: AgentSpec, **kwargs: Any) -> ValidationResult:
        verdict = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return ValidationResult(
            agent_id=spec.agent_id,
            task_id=spec.task_id,
            code_sha256=spec.code_sha256,
            verdict=verdict,
            confidence=0.9,
            isolation_level=IsolationLevel.L2,
            layer_reports=(
                LayerReport(layer=ValidationLayer.STATIC, passed=verdict.is_pass, latency_ms=5.0),
            ),
            total_latency_ms=5.0,
        )


class FakeEmbedder:
    model_name = "fake"
    dimension = 4

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


@dataclass
class FakeMatch:
    agent_id: uuid.UUID
    similarity: float = 0.95
    mean_tsr: float = 0.9


class FakeRegistry:
    def __init__(self, match: FakeMatch | None = None, *, broken: bool = False) -> None:
        self.match = match
        self.broken = broken
        self.registered: list[AgentSpec] = []
        self.reused: list[uuid.UUID] = []

    def find_reusable(self, query_embedding: list[float], **kwargs: Any) -> list[FakeMatch]:
        if self.broken:
            raise RuntimeError("registry is down")
        return [self.match] if self.match else []

    def mark_reused(self, agent_id: uuid.UUID) -> None:
        self.reused.append(agent_id)

    def register(self, spec: AgentSpec, result: ValidationResult, embedding: list[float], **kw: Any) -> None:
        self.registered.append(spec)

    def load_code(self, agent_id: uuid.UUID) -> str | None:
        return None


@pytest.fixture
def task() -> TaskSpecification:
    return TaskSpecification(
        task_id="T001",
        description="Count ERROR lines per service.",
        available_tools=["read_log_file"],
        expected_output={"total": 2},
    )


def test_pass_deploys_and_registers(task: TaskSpecification) -> None:
    registry = FakeRegistry()
    factory = AgentFactory(
        generator=FakeGenerator(),
        pipeline=FakePipeline([Verdict.PASS]),
        registry=registry,
        embedder=FakeEmbedder(),
    )
    result = factory.handle_task(task)
    assert result.outcome is Outcome.GENERATED_AND_DEPLOYED
    assert result.succeeded
    assert result.attempts == 1
    assert len(registry.registered) == 1


def test_unsafe_escalates_without_regenerating(task: TaskSpecification) -> None:
    """A safety failure must never enter the retry loop."""
    generator = FakeGenerator()
    factory = AgentFactory(
        generator=generator,
        pipeline=FakePipeline([Verdict.FAIL_UNSAFE]),
        registry=FakeRegistry(),
        embedder=FakeEmbedder(),
    )
    result = factory.handle_task(task)
    assert result.outcome is Outcome.ESCALATED_UNSAFE
    assert result.attempts == 1
    assert len(generator.calls) == 1


def test_fail_both_also_escalates(task: TaskSpecification) -> None:
    factory = AgentFactory(
        generator=FakeGenerator(),
        pipeline=FakePipeline([Verdict.FAIL_BOTH]),
    )
    assert factory.handle_task(task).outcome is Outcome.ESCALATED_UNSAFE


def test_incorrect_regenerates_then_succeeds(task: TaskSpecification) -> None:
    generator = FakeGenerator()
    factory = AgentFactory(
        generator=generator,
        pipeline=FakePipeline([Verdict.FAIL_INCORRECT, Verdict.FAIL_INCORRECT, Verdict.PASS]),
    )
    result = factory.handle_task(task)
    assert result.outcome is Outcome.GENERATED_AND_DEPLOYED
    assert result.attempts == 3
    # The second and third attempts must have carried validator feedback.
    assert generator.calls[0] is None
    assert generator.calls[1] and "FAIL_INCORRECT" in generator.calls[1]


def test_regeneration_cap_is_enforced(task: TaskSpecification) -> None:
    generator = FakeGenerator()
    factory = AgentFactory(
        generator=generator,
        pipeline=FakePipeline([Verdict.FAIL_INCORRECT]),
        max_attempts=3,
    )
    result = factory.handle_task(task)
    assert result.outcome is Outcome.ESCALATED_EXHAUSTED
    assert result.attempts == 3
    assert len(generator.calls) == 3


def test_reuse_skips_generation_entirely(task: TaskSpecification) -> None:
    match = FakeMatch(agent_id=uuid.uuid4())
    generator = FakeGenerator()
    factory = AgentFactory(
        generator=generator,
        pipeline=FakePipeline([Verdict.PASS]),
        registry=FakeRegistry(match=match),
        embedder=FakeEmbedder(),
    )
    result = factory.handle_task(task)
    assert result.outcome is Outcome.REUSED
    assert result.reused
    assert generator.calls == []
    assert result.agent_id == match.agent_id


def test_registry_outage_degrades_to_generation(task: TaskSpecification) -> None:
    """A broken registry must cost money, not correctness."""
    factory = AgentFactory(
        generator=FakeGenerator(),
        pipeline=FakePipeline([Verdict.PASS]),
        registry=FakeRegistry(broken=True),
        embedder=FakeEmbedder(),
    )
    result = factory.handle_task(task)
    assert result.outcome is Outcome.GENERATED_AND_DEPLOYED
    assert not result.reused


def test_generation_failure_is_reported_not_raised(task: TaskSpecification) -> None:
    class BrokenGenerator:
        def generate(self, **kwargs: Any) -> Any:
            raise ValueError("model produced nothing usable")

    factory = AgentFactory(generator=BrokenGenerator(), pipeline=FakePipeline([Verdict.PASS]))
    result = factory.handle_task(task)
    assert result.outcome is Outcome.FAILED_INFRASTRUCTURE
    assert result.escalation_reason is not None


def test_judge_failure_does_not_fail_the_task(task: TaskSpecification) -> None:
    class BrokenJudge:
        def score(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("judge is not calibrated")

    factory = AgentFactory(
        generator=FakeGenerator(),
        pipeline=FakePipeline([Verdict.PASS]),
        judge=BrokenJudge(),
    )
    assert factory.handle_task(task).outcome is Outcome.GENERATED_AND_DEPLOYED
