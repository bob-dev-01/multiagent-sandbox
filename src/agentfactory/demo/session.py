"""One demo session: a registry that persists across runs, and one run at a time.

A run is a task submitted to the real system — the AutoGen team, the
validation pipeline with sandbox pods on AKS, the calibrated judge, the Task
Executor — with every component wrapped so the page can watch it. The only
staged part is which agent the generator returns: a scenario may serve a
labelled corpus agent (an unsafe one, or a wrong one the model then has to
fix) instead of, or before, a live model call.
"""

from __future__ import annotations

import csv
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentfactory.autogen_team import AutoGenAgentFactory
from agentfactory.contracts import IsolationLevel
from agentfactory.corpus import CorpusTask, load_corpus
from agentfactory.executor import TaskExecutor
from agentfactory.orchestrator.factory import AgentFactory, TaskSpecification
from agentfactory.registry.memory import InMemoryRegistry
from agentfactory.validation.aggregator import StrictnessProfile
from agentfactory.validation.layer2_policy import EnterprisePolicy
from agentfactory.validation.pipeline import PipelineConfig

from .events import DemoEventLog, DemoRun
from .instrument import (
    InstrumentedGenerator,
    InstrumentedJudge,
    InstrumentedPipeline,
    InstrumentedRegistry,
    InstrumentedRunner,
    ScriptedGenerator,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CALIBRATION_PAIRS = REPO_ROOT / "research" / "calibration" / "judge_score_pairs.csv"

# What a scenario makes the generator do on its first attempt.
MODES = {
    "live": None,       # the model writes the agent
    "safe_a1": "a1",    # a labelled correct corpus agent: no model call, no cost
    "unsafe_u1": "u1",  # a labelled unsafe corpus agent
    "unsafe_u2": "u2",
    "both_b1": "b1",    # unsafe and wrong
    "wrong_i1": "i1",   # wrong; the model then has to fix it
}


class RunRefused(Exception):
    """The request cannot be run now: busy, over budget, or invalid."""


@dataclass
class RunRequest:
    task_id: str
    mode: str = "live"
    wording: str = "original"        # original | p0 | p1 | custom
    custom_text: str | None = None
    level: str = "L3"
    profile: str = "strict"
    sandbox_always: bool = False     # run the sandbox even after a static failure


@dataclass
class Budget:
    daily_usd: float
    spent_usd: float = 0.0
    day: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))

    def roll(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.spent_usd = today, 0.0

    @property
    def left_usd(self) -> float:
        self.roll()
        return max(0.0, self.daily_usd - self.spent_usd)


class DemoSession:
    def __init__(self, *, runner_factory: Any, daily_budget_usd: float = 3.0,
                 per_run_budget_usd: float = 0.6, use_judge: bool = True,
                 embedder: Any = None) -> None:
        self.tasks: dict[str, CorpusTask] = {t.task_id: t for t in load_corpus()}
        self.runner_factory = runner_factory          # level -> real SandboxRunner
        self.registry = InMemoryRegistry()
        self.budget = Budget(daily_budget_usd)
        self.per_run_budget_usd = per_run_budget_usd
        self.use_judge = use_judge
        self.runs: dict[str, DemoRun] = {}
        self._busy = threading.Lock()
        self._embedder: Any = embedder
        self._judge: Any = None

    # ------------------------------------------------------------ components

    @property
    def embedder(self) -> Any:
        if self._embedder is None:
            from agentfactory.registry.embeddings import SentenceTransformerEmbedder

            self._embedder = SentenceTransformerEmbedder()
            self._embedder.embed("warm-up")
        return self._embedder

    def judge(self) -> Any:
        if not self.use_judge:
            return None
        if self._judge is None:
            from agentfactory.judge.correctness_judge import CorrectnessJudge

            judge = CorrectnessJudge()
            # The gate opens only by replaying the recorded calibration.
            rows = list(csv.DictReader(CALIBRATION_PAIRS.open(encoding="utf-8")))
            judge.calibrate([int(r["human"]) for r in rows], [int(r["judge"]) for r in rows])
            self._judge = judge
        return self._judge

    def reset(self) -> None:
        if not self._busy.acquire(blocking=False):
            raise RunRefused("a run is in progress")
        try:
            self.registry = InMemoryRegistry()
        finally:
            self._busy.release()

    # ------------------------------------------------------------------ runs

    def describe(self) -> dict[str, Any]:
        return {
            "tasks": [
                {"task_id": t.task_id, "title": t.title, "domain": t.domain,
                 "description": t.description, "paraphrases": list(t.paraphrases),
                 "tools": list(t.available_tools),
                 "agents": [{"variant": a.variant, "label": a.label.value,
                             "failure_mode": a.failure_mode} for a in t.agents]}
                for t in self.tasks.values()
            ],
            "modes": list(MODES), "levels": ["L2", "L3"],
            "profiles": ["permissive", "balanced", "strict", "paranoid"],
            "registered": len(self.registry._agents),
            "budget": {"daily_usd": self.budget.daily_usd,
                       "left_usd": round(self.budget.left_usd, 2)},
            "busy": self._busy.locked(),
        }

    def start(self, req: RunRequest) -> DemoRun:
        task = self.tasks.get(req.task_id)
        if task is None:
            raise RunRefused(f"unknown task {req.task_id}")
        if req.mode not in MODES:
            raise RunRefused(f"unknown mode {req.mode}")
        if req.level not in ("L2", "L3"):
            raise RunRefused("level must be L2 or L3")
        if self.budget.left_usd < 0.05:
            raise RunRefused(f"today's demo budget of ${self.budget.daily_usd:.2f} is spent")
        if not self._busy.acquire(blocking=False):
            raise RunRefused("another run is in progress")
        run = DemoRun()
        self.runs[run.run_id] = run
        threading.Thread(target=self._run, args=(run, req, task), daemon=True).start()
        return run

    def _description(self, req: RunRequest, task: CorpusTask) -> str:
        if req.wording == "p0":
            return task.paraphrases[0]
        if req.wording == "p1":
            return task.paraphrases[1]
        if req.wording == "custom" and req.custom_text and req.custom_text.strip():
            return req.custom_text.strip()[:2000]
        return task.description

    def _run(self, run: DemoRun, req: RunRequest, task: CorpusTask) -> None:
        team = None
        try:
            description = self._description(req, task)
            run.emit("run_started", replay=False, task_id=task.task_id, title=task.title,
                     description=description, wording=req.wording, mode=req.mode,
                     level=req.level, profile=req.profile, sandbox_always=req.sandbox_always,
                     tools=list(task.available_tools), expected_output=task.expected_output,
                     registered=len(self.registry._agents))

            live = None
            if req.mode in ("live", "wrong_i1"):
                from agentfactory.generator.agent_generator import AgentGenerator
                from agentfactory.integration import LiveGenerator

                live = LiveGenerator(AgentGenerator(),
                                     budget_usd=min(self.per_run_budget_usd,
                                                    self.budget.left_usd))
            variant = MODES[req.mode]
            scripted = []
            if variant is not None:
                agent = next(a for a in task.agents if a.variant == variant)
                scripted = [agent.to_spec(task.contract())]
                run.emit("scripted_agent", variant=variant, label=agent.label.value,
                         failure_mode=agent.failure_mode)
            generator = InstrumentedGenerator(ScriptedGenerator(scripted, live), run)

            level = IsolationLevel(req.level)
            log = DemoEventLog(run)
            profile = StrictnessProfile.named(req.profile)
            registry = InstrumentedRegistry(self.registry, run)
            judge = self.judge()
            ijudge = InstrumentedJudge(judge, run) if judge is not None else None
            pipeline = InstrumentedPipeline(
                InstrumentedRunner(self.runner_factory(level), run, "validation"),
                PipelineConfig.default(
                    profile=profile, policy=EnterprisePolicy.load(), use_bandit=True,
                    test_inputs=({"case": 1},), short_circuit=not req.sandbox_always),
                event_log=log, demo_run=run)
            executor = TaskExecutor(
                InstrumentedRunner(self.runner_factory(level), run, "execution"),
                high_risk_runner=InstrumentedRunner(
                    self.runner_factory(IsolationLevel.L3), run, "execution"),
                registry=registry, event_log=log, profile=profile)
            factory = AgentFactory(generator=generator, pipeline=pipeline, registry=registry,
                                   embedder=self.embedder, judge=ijudge, executor=executor,
                                   event_log=log)
            team = AutoGenAgentFactory(
                factory, on_message=lambda kind, to: run.emit("autogen_message",
                                                              message=kind, to=to))
            result = team.handle_task(TaskSpecification(
                task_id=task.task_id, description=description,
                available_tools=list(task.available_tools),
                expected_output=task.expected_output, risk_tier=task.risk_tier,
                tool_fixtures=dict(task.tool_fixtures),
                correctness_rubric=task.correctness_rubric))

            spent = generator.spent_usd + (ijudge.spent_usd if ijudge else 0.0)
            self.budget.spent_usd += spent
            execution = result.executions[-1] if result.executions else None
            run.emit("run_finished", outcome=result.outcome.value, succeeded=result.succeeded,
                     reused=result.reused, attempts=result.attempts,
                     time_to_deploy_sec=result.mean_time_to_deploy_sec,
                     escalation_reason=result.escalation_reason,
                     output=execution.output if execution else None,
                     cost_usd=round(spent, 4), budget_left_usd=round(self.budget.left_usd, 2),
                     registered=len(self.registry._agents))
        except Exception as exc:  # noqa: BLE001 — every failure is shown, never swallowed
            run.emit("error", stage="demo", message=f"{type(exc).__name__}: {exc}",
                     trace=traceback.format_exc()[-1500:])
            run.emit("run_finished", outcome="demo_error", succeeded=False)
        finally:
            if team is not None:
                team.close()
            run.finish()
            self._busy.release()
            # Keep memory bounded: the last 30 runs.
            for old in list(self.runs)[:-30]:
                self.runs.pop(old, None)


def runner_factory_from_env() -> Any:
    """Sandbox runners for the demo: pods on AKS through the demo's kubeconfig."""
    from agentfactory.validation.sandbox.pod_runner import gvisor_pod_runner, runc_pod_runner

    namespace = os.environ.get("AGENTFACTORY_K8S_NAMESPACE", "agentfactory-sandbox")
    context = os.environ.get("AGENTFACTORY_KUBE_CONTEXT", "agentfac-aks")

    def make(level: IsolationLevel) -> Any:
        if level is IsolationLevel.L3:
            return gvisor_pod_runner(namespace=namespace, kube_context=context)
        return runc_pod_runner(namespace=namespace, kube_context=context)

    return make
