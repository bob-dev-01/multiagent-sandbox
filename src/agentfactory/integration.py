"""The integration test: the whole loop, end to end, against the labelled corpus.

architecture.md section 14 specifies it as Orchestrator -> Generator ->
Pipeline -> Registry -> Executor on every task against an empty registry, then
again against the populated one to measure reuse. Two changes, both recorded:

**The warm pass uses paraphrases, not the original text.** Re-submitting the
same task text clears any similarity threshold, so ARR measured that way says
nothing about semantic matching (OQ-8). The warm pass submits each task's
reworded variants instead; same tools, same expected output.

**The generator is a replay of the corpus.** `CorpusReplayGenerator` stands in
for the LLM: each generation request returns the next variant of that task in
a seeded random order. Its error profile is therefore known exactly — the
corpus distribution, 5/2/2/1 — and every agent it produces has a ground-truth
label, which is what lets FNR, UDR and CER be computed on an end-to-end run.
What it cannot tell you is how good a real model is. The same harness runs
against `AgentGenerator` once the API key is in place; the numbers from the
replay are about the pipeline and the executor, and must be reported as such.

Each seed is an independent replicate: a fresh registry, a fresh order.
"""

from __future__ import annotations

import json
import random
import statistics
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from agentfactory.corpus import CorpusAgent, CorpusTask
from agentfactory.metrics.core import (
    GroundTruth,
    Observation,
    SafetyMetrics,
    safety_metrics,
    wilson_interval,
)
from agentfactory.orchestrator.factory import (
    AgentFactory,
    Outcome,
    TaskResult,
    TaskSpecification,
)

COLD = "cold"
WARM = "warm"


@dataclass
class ReplayGeneration:
    spec: Any
    model: str = "corpus-replay"
    cost_usd: float = 0.0


class CorpusReplayGenerator:
    """Serves corpus variants in a seeded order, as if a model had written them."""

    def __init__(self, tasks: Iterable[CorpusTask], seed: int) -> None:
        rng = random.Random(seed)
        self._tasks = {t.task_id: t for t in tasks}
        self._queues: dict[str, list[CorpusAgent]] = {}
        for task_id in sorted(self._tasks):
            order = list(self._tasks[task_id].agents)
            rng.shuffle(order)
            self._queues[task_id] = order
        self._cursor: dict[str, int] = dict.fromkeys(self._queues, 0)
        # agent_id -> the corpus agent it was made from
        self.served: dict[uuid.UUID, CorpusAgent] = {}

    def generate(self, *, task_id: str, **_: Any) -> ReplayGeneration:
        queue = self._queues[task_id]
        agent = queue[self._cursor[task_id] % len(queue)]
        self._cursor[task_id] += 1
        spec = agent.to_spec(self._tasks[task_id].contract())
        self.served[spec.agent_id] = agent
        return ReplayGeneration(spec=spec)

    def label_for(self, agent_id: uuid.UUID) -> str | None:
        agent = self.served.get(agent_id)
        return agent.label.value if agent else None

    exhausted = False


class BudgetExhausted(RuntimeError):
    """The live generator's spending cap was reached; the run stops cleanly."""


class LiveGenerator:
    """The real generator, with a spending cap and a record of everything it made.

    Generated agents carry no ground-truth label, so safety metrics computed
    against labels do not apply to them; what is measured is how the pipeline
    and executor treat them. Every agent is written to `sink` in full — source,
    contract, cost — because judging their correctness (H3) and what strictness
    costs on them (RQ4) needs the code itself, later.
    """

    def __init__(self, inner: Any, *, budget_usd: float, sink: Any = None) -> None:
        self.inner = inner
        self.budget_usd = budget_usd
        self.sink = sink
        self.spent_usd = 0.0
        self.calls = 0

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.budget_usd

    def label_for(self, agent_id: uuid.UUID) -> str | None:
        return None

    def generate(self, *, task_id: str, task_spec: str, **kwargs: Any) -> Any:
        if self.exhausted:
            raise BudgetExhausted(f"budget of ${self.budget_usd:.2f} reached")
        result = self.inner.generate(task_id=task_id, task_spec=task_spec, **kwargs)
        self.calls += 1
        self.spent_usd += result.cost_usd
        if self.sink is not None:
            spec = result.spec
            self.sink.write(json.dumps({
                "agent_id": str(spec.agent_id),
                "task_id": task_id,
                "task_spec": task_spec,
                "regeneration": bool(kwargs.get("failure_feedback")),
                "code_sha256": spec.code_sha256,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": round(result.cost_usd, 6),
                "authorized_tools": list(spec.contract.authorized_tools),
                "reasoning": result.reasoning,
                "source": spec.source,
            }) + "\n")
            self.sink.flush()
        return result


@dataclass(frozen=True)
class TaskRecord:
    """One task submission and how it ended."""

    seed: int
    phase: str
    task_id: str
    paraphrase: int | None
    outcome: str
    succeeded: bool
    reused: bool
    attempts: int
    time_to_deploy_sec: float | None
    time_to_first_action_sec: float | None
    refusals: tuple[str, ...]
    executed_labels: tuple[str, ...]
    executed_unsafe: bool
    cost_usd: float = 0.0
    verdicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhaseSummary:
    n_tasks: int
    task_success_rate: float
    tsr_ci: tuple[float, float]
    agent_reuse_rate: float
    arr_ci: tuple[float, float]
    escalation_rate: float
    mean_time_to_deploy_sec: float
    median_time_to_deploy_sec: float
    outcomes: dict[str, int]


@dataclass
class IntegrationResult:
    records: list[TaskRecord] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    # Set when a live run stopped early because its budget ran out.
    stopped: str | None = None

    def phase(self, name: str) -> PhaseSummary:
        rows = [r for r in self.records if r.phase == name]
        ok = sum(r.succeeded for r in rows)
        reused = sum(r.reused for r in rows)
        # Reuse rate over tasks where an agent was actually deployed, reused
        # or fresh. Escalations and infrastructure failures deployed nothing,
        # and counting them would understate reuse.
        deployed = sum(bool(r.executed_labels) for r in rows)
        ttd = [r.time_to_deploy_sec for r in rows if r.time_to_deploy_sec is not None]
        outcomes: dict[str, int] = {}
        for r in rows:
            outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
        escalated = sum(
            r.outcome in {
                Outcome.ESCALATED_UNSAFE.value,
                Outcome.ESCALATED_EXHAUSTED.value,
                Outcome.DEPLOYMENT_REFUSED.value,
                Outcome.EXECUTED_UNSAFE.value,
            }
            for r in rows
        )
        return PhaseSummary(
            n_tasks=len(rows),
            task_success_rate=ok / len(rows) if rows else float("nan"),
            tsr_ci=wilson_interval(ok, len(rows)),
            agent_reuse_rate=reused / deployed if deployed else float("nan"),
            arr_ci=wilson_interval(reused, deployed),
            escalation_rate=escalated / len(rows) if rows else float("nan"),
            mean_time_to_deploy_sec=statistics.fmean(ttd) if ttd else float("nan"),
            median_time_to_deploy_sec=statistics.median(ttd) if ttd else float("nan"),
            outcomes=outcomes,
        )

    def safety(self) -> SafetyMetrics:
        return safety_metrics(self.observations)

    def as_dict(self) -> dict[str, Any]:
        verdicts: dict[str, int] = {}
        for r in self.records:
            for v in r.verdicts:
                verdicts[v] = verdicts.get(v, 0) + 1
        out: dict[str, Any] = {
            "phases": {name: asdict(self.phase(name)) for name in (COLD, WARM)},
            "verdicts": verdicts,
            "executions_unsafe": sum(r.executed_unsafe for r in self.records),
            "cost_usd": round(sum(r.cost_usd for r in self.records), 4),
            "stopped": self.stopped,
        }
        # Safety against ground truth only exists for labelled agents.
        if self.observations:
            safety = self.safety()
            out["safety"] = {**asdict(safety), "udr_fnr_gap": safety.udr_fnr_gap}
            out["n_validations"] = len(self.observations)
        return out


FactoryBuilder = Callable[[Any], AgentFactory]


def run_integration(
    tasks: Sequence[CorpusTask],
    build_factory: FactoryBuilder,
    *,
    seeds: Sequence[int],
    on_record: Callable[[TaskRecord], None] | None = None,
    make_generator: Callable[[int], Any] | None = None,
) -> IntegrationResult:
    """Cold pass on the original tasks, then a warm pass on their paraphrases.

    `build_factory` is called once per seed and must return a factory with a
    fresh registry and an executor; a factory without an executor would count
    approvals as successes, which is not what this test measures.
    """
    out = IntegrationResult()
    for seed in seeds:
        generator = (make_generator(seed) if make_generator is not None
                     else CorpusReplayGenerator(tasks, seed))
        factory = build_factory(generator)
        if factory.executor is None:
            raise ValueError("the integration test needs a factory with a Task Executor")

        order = list(tasks)
        random.Random(seed).shuffle(order)
        executed: dict[uuid.UUID, list[bool]] = {}

        submissions: list[tuple[str, CorpusTask, int | None, str]] = [
            (COLD, t, None, t.description) for t in order
        ]
        warm = [(WARM, t, i, p) for t in order for i, p in enumerate(t.paraphrases)]
        random.Random(seed + 1).shuffle(warm)
        submissions += warm

        for phase, task, paraphrase, description in submissions:
            if generator.exhausted:
                out.stopped = (f"budget reached at seed {seed} before {phase} "
                               f"{task.task_id}; later submissions not run")
                break
            result = factory.handle_task(
                TaskSpecification(
                    task_id=task.task_id,
                    description=description,
                    available_tools=list(task.available_tools),
                    expected_output=task.expected_output,
                    risk_tier=task.risk_tier,
                    tool_fixtures=dict(task.tool_fixtures),
                )
            )
            for record in result.executions:
                executed.setdefault(record.agent_id, []).append(record.unsafe)
            row = _record(seed, phase, task, paraphrase, result, generator)
            out.records.append(row)
            if on_record is not None:
                on_record(row)

            for validation in result.history:
                label = generator.label_for(validation.agent_id)
                if label is None:
                    continue  # a generated agent: no ground truth to score against
                out.observations.append(
                    Observation(
                        agent_id=str(validation.agent_id),
                        task_id=task.task_id,
                        ground_truth=GroundTruth(label),
                        verdict=validation.verdict,
                        isolation_level=validation.isolation_level.value,
                        latency_ms=validation.total_latency_ms,
                        replicate=seed,
                    )
                )

        # Execution status is known only once every submission for this seed
        # has run: an agent validated in the cold pass may first execute when
        # a paraphrase reuses it.
        for index, obs in enumerate(out.observations):
            if obs.replicate != seed:
                continue
            runs = executed.get(uuid.UUID(obs.agent_id), [])
            out.observations[index] = Observation(
                **{**asdict(obs), "reached_execution": bool(runs), "executed_unsafely": any(runs)}
            )
        if out.stopped:
            break
    return out


def _record(
    seed: int,
    phase: str,
    task: CorpusTask,
    paraphrase: int | None,
    result: TaskResult,
    generator: Any,
) -> TaskRecord:
    labels = [generator.label_for(e.agent_id) or "generated" for e in result.executions]
    return TaskRecord(
        seed=seed,
        phase=phase,
        task_id=task.task_id,
        paraphrase=paraphrase,
        outcome=result.outcome.value,
        succeeded=result.succeeded,
        reused=result.reused,
        attempts=result.attempts,
        time_to_deploy_sec=result.mean_time_to_deploy_sec,
        time_to_first_action_sec=result.time_to_first_action_sec,
        refusals=tuple(result.refusals),
        executed_labels=tuple(labels),
        executed_unsafe=any(e.unsafe for e in result.executions),
        cost_usd=result.cost_usd,
        verdicts=tuple(v.verdict.value for v in result.history),
    )
