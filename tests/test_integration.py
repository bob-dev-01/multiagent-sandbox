"""The end-to-end loop on the real corpus, with execution.

The runner here has no isolation, so it is wrapped in a guard that refuses to
run any corpus agent labelled unsafe. Today static analysis stops all of them
before the sandbox; the guard makes that a precondition of the test rather
than an assumption, so a corpus change can never make this suite execute
unsafe code on a developer's machine.
"""

from __future__ import annotations

import math

import pytest

from agentfactory.corpus import load_corpus
from agentfactory.executor import TaskExecutor
from agentfactory.integration import COLD, WARM, CorpusReplayGenerator, run_integration
from agentfactory.metrics.core import GroundTruth
from agentfactory.orchestrator.factory import AgentFactory, Outcome
from agentfactory.registry.memory import InMemoryRegistry
from agentfactory.validation.pipeline import PipelineConfig, ValidationPipeline

TASKS = load_corpus()
UNSAFE_SHAS = {
    a.source_sha256 for t in TASKS for a in t.agents if GroundTruth(a.label.value).is_unsafe
}


class GuardedRunner:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.level = inner.level

    def available(self) -> bool:
        return True

    def run(self, request):
        if request.spec.code_sha256 in UNSAFE_SHAS:
            raise AssertionError("refusing to execute unsafe-labelled code without isolation")
        return self.inner.run(request)


class TaskOracleEmbedder:
    """One axis per task: every wording of a task maps to the same vector.

    Tests the reuse plumbing in isolation from embedding quality, which is a
    separate question for the real model.
    """

    model_name = "task-oracle"

    def __init__(self) -> None:
        self.axis = {}
        for i, task in enumerate(TASKS):
            for text in (task.description, *task.paraphrases):
                self.axis[" ".join(text.split())] = i
        self.dimension = len(TASKS)

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        vector[self.axis[" ".join(text.split())]] = 1.0
        return vector

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


def _builder(local_runner, embedder):
    runner = GuardedRunner(local_runner)

    def build(generator: CorpusReplayGenerator) -> AgentFactory:
        registry = InMemoryRegistry()
        return AgentFactory(
            generator=generator,
            pipeline=ValidationPipeline(
                runner, PipelineConfig.default(use_bandit=False, test_inputs=({"case": 1},))
            ),
            registry=registry,
            embedder=embedder,
            executor=TaskExecutor(runner, registry=registry),
        )

    return build


@pytest.fixture(scope="module")
def result(request):
    from .conftest import LocalHarnessRunner

    return run_integration(
        TASKS, _builder(LocalHarnessRunner(), TaskOracleEmbedder()), seeds=(1, 2, 3)
    )


def test_every_submission_is_recorded(result) -> None:
    per_seed = len(TASKS) + sum(len(t.paraphrases) for t in TASKS)
    assert len(result.records) == 3 * per_seed
    assert {r.phase for r in result.records} == {COLD, WARM}


def test_nothing_unsafe_or_incorrect_was_deployed_successfully(result) -> None:
    for row in result.records:
        if row.succeeded:
            assert row.executed_labels[-1] == GroundTruth.SAFE_CORRECT.value, row
        assert GroundTruth.UNSAFE_ONLY.value not in row.executed_labels
        assert GroundTruth.BOTH.value not in row.executed_labels


def test_udr_and_cer_are_zero_and_measured(result) -> None:
    safety = result.safety()
    assert safety.n_unsafe > 0
    assert safety.unsafe_deployment_rate == 0.0
    assert safety.catastrophic_error_rate == 0.0
    assert not math.isnan(safety.udr_ci[1])


def test_validated_safe_agents_are_marked_as_executed(result) -> None:
    executed = [o for o in result.observations if o.reached_execution]
    assert executed
    assert all(o.ground_truth is GroundTruth.SAFE_CORRECT for o in executed)


def test_the_warm_pass_reuses_with_a_matching_embedder(result) -> None:
    warm = result.phase(WARM)
    assert warm.agent_reuse_rate > 0
    reused = [r for r in result.records if r.phase == WARM and r.reused]
    assert reused and all(r.outcome == Outcome.REUSED.value for r in reused)
    assert all(r.attempts == 0 for r in reused)


def test_cold_outcomes_follow_the_routing_rules(result) -> None:
    allowed = {
        Outcome.GENERATED_AND_DEPLOYED.value,
        Outcome.ESCALATED_UNSAFE.value,
        Outcome.ESCALATED_EXHAUSTED.value,
    }
    high_risk = {t.task_id for t in TASKS if t.risk_tier == "high"}
    cold = [r for r in result.records if r.phase == COLD]
    assert {r.outcome for r in cold if r.task_id not in high_risk} <= allowed
    # No L3 runner is configured here, so a high-risk task that reaches
    # deployment must be refused at the gate, never run somewhere weaker.
    for row in cold:
        if row.task_id in high_risk:
            assert row.outcome in allowed - {Outcome.GENERATED_AND_DEPLOYED.value} | {
                Outcome.DEPLOYMENT_REFUSED.value
            }, row
            assert not row.executed_labels, row


def test_summary_serialises(result) -> None:
    import json

    json.dumps(result.as_dict(), default=str)


def test_the_replay_generator_is_seeded() -> None:
    a = CorpusReplayGenerator(TASKS, seed=7)
    b = CorpusReplayGenerator(TASKS, seed=7)
    for task in TASKS:
        assert a.generate(task_id=task.task_id).spec.code_sha256 == (
            b.generate(task_id=task.task_id).spec.code_sha256
        )
