"""One demo session: a registry that persists across runs, and one run or batch at a time.

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
import uuid
from concurrent.futures import ThreadPoolExecutor
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


# A batch runs at most this many tasks at once.
MAX_PARALLEL = 15
# What one live task may cost, held back from the day's budget while it runs,
# so tasks running side by side cannot together overspend it.
LIVE_RESERVE_USD = 0.15


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


@dataclass
class Batch:
    """Several tasks, each an ordinary demo run, run in order or side by side."""

    batch_id: str
    items: list[dict[str, Any]]
    parallel: int = 1
    done: bool = False
    stopped: str | None = None
    cancel: bool = False

    def view(self) -> dict[str, Any]:
        finished = [i for i in self.items if i["status"] == "done"]
        tally: dict[str, int] = {}
        for i in finished:
            tally[i["outcome"]] = tally.get(i["outcome"], 0) + 1
        return {"batch_id": self.batch_id, "done": self.done, "stopped": self.stopped,
                "items": self.items, "finished": len(finished), "total": len(self.items),
                "parallel": self.parallel,
                "tally": tally, "cost_usd": round(sum(i.get("cost_usd") or 0 for i in finished), 4)}


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
        self.batches: dict[str, Batch] = {}
        self._busy = threading.Lock()
        self._money = threading.Lock()   # budget spending and reservations
        self._reserved_usd = 0.0
        self._init = threading.Lock()    # lazy loading of the shared embedder and judge
        self._embedder: Any = _LockedEmbedder(embedder) if embedder is not None else None
        self._judge: Any = None

    # ------------------------------------------------------------ components

    @property
    def embedder(self) -> Any:
        with self._init:
            if self._embedder is None:
                from agentfactory.registry.embeddings import SentenceTransformerEmbedder

                embedder = _LockedEmbedder(SentenceTransformerEmbedder())
                embedder.embed("warm-up")
                self._embedder = embedder
        return self._embedder

    def judge(self) -> Any:
        if not self.use_judge:
            return None
        with self._init:
            if self._judge is None:
                from agentfactory.judge.correctness_judge import CorrectnessJudge

                judge = CorrectnessJudge()
                # The gate opens only by replaying the recorded calibration.
                rows = list(csv.DictReader(CALIBRATION_PAIRS.open(encoding="utf-8")))
                judge.calibrate([int(r["human"]) for r in rows], [int(r["judge"]) for r in rows])
                judge._get_client()
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

    def start_batch(self, task_ids: list[str], template: RunRequest, parallel: int = 1) -> Batch:
        """Run the tasks with the template's mode, wording and level, `parallel` at a time."""
        if not task_ids or len(task_ids) > 30:
            raise RunRefused("a batch takes between 1 and 30 tasks")
        if not 1 <= parallel <= MAX_PARALLEL:
            raise RunRefused(f"a batch runs between 1 and {MAX_PARALLEL} tasks at once")
        unknown = [t for t in task_ids if t not in self.tasks]
        if unknown:
            raise RunRefused(f"unknown tasks {unknown}")
        if template.mode not in MODES or template.level not in ("L2", "L3"):
            raise RunRefused("unknown mode or level")
        if self.budget.left_usd < 0.05:
            raise RunRefused(f"today's demo budget of ${self.budget.daily_usd:.2f} is spent")
        if not self._busy.acquire(blocking=False):
            raise RunRefused("another run is in progress")
        batch = Batch(batch_id=uuid.uuid4().hex[:10], parallel=parallel,
                      items=[{"task_id": t, "status": "queued", "run_id": None} for t in task_ids])
        self.batches[batch.batch_id] = batch
        threading.Thread(target=self._run_batch, args=(batch, template), daemon=True).start()
        return batch

    def _run_batch(self, batch: Batch, template: RunRequest) -> None:
        # The session lock, taken by start_batch, is held for the whole batch;
        # its runs do not release it themselves.
        reserve = LIVE_RESERVE_USD if template.mode in ("live", "wrong_i1") else 0.0

        def work(index: int, item: dict[str, Any]) -> None:
            with self._money:
                short = self.budget.left_usd - self._reserved_usd < max(reserve, 0.05)
                if batch.cancel or short:
                    batch.stopped = batch.stopped or (
                        "stopped by request" if batch.cancel else "daily budget spent")
                    item["status"] = "skipped"
                    return
                self._reserved_usd += reserve
            try:
                req = RunRequest(**{**template.__dict__, "task_id": item["task_id"]})
                run = DemoRun()
                self.runs[run.run_id] = run
                item.update(status="running", run_id=run.run_id)
                started = time.perf_counter()
                self._run(run, req, self.tasks[item["task_id"]], own_lock=False,
                          batch={"batch_id": batch.batch_id, "index": index + 1,
                                 "total": len(batch.items), "parallel": batch.parallel})
                finished = next((e["data"] for e in run.events if e["type"] == "run_finished"), {})
                item.update(status="done", outcome=finished.get("outcome", "demo_error"),
                            succeeded=bool(finished.get("succeeded")),
                            reused=bool(finished.get("reused")),
                            reason=finished.get("escalation_reason"),
                            verdicts=[e["data"]["verdict"] for e in run.events if e["type"] == "verdict"],
                            cost_usd=finished.get("cost_usd") or 0.0,
                            seconds=round(time.perf_counter() - started, 1))
            finally:
                with self._money:
                    self._reserved_usd -= reserve

        try:
            with ThreadPoolExecutor(max_workers=batch.parallel,
                                    thread_name_prefix=f"batch-{batch.batch_id}") as pool:
                for index, item in enumerate(batch.items):
                    pool.submit(work, index, item)
        finally:
            batch.done = True
            self._busy.release()

    def _description(self, req: RunRequest, task: CorpusTask) -> str:
        if req.wording == "p0":
            return task.paraphrases[0]
        if req.wording == "p1":
            return task.paraphrases[1]
        if req.wording == "custom" and req.custom_text and req.custom_text.strip():
            return req.custom_text.strip()[:2000]
        return task.description

    def _run(self, run: DemoRun, req: RunRequest, task: CorpusTask,
             batch: dict[str, Any] | None = None, own_lock: bool = True) -> None:
        team = None
        try:
            description = self._description(req, task)
            run.emit("run_started", replay=False, task_id=task.task_id, title=task.title,
                     description=description, wording=req.wording, mode=req.mode,
                     level=req.level, profile=req.profile, sandbox_always=req.sandbox_always,
                     tools=list(task.available_tools), expected_output=task.expected_output,
                     registered=len(self.registry._agents), batch=batch)

            live = None
            if req.mode in ("live", "wrong_i1"):
                from agentfactory.generator.agent_generator import AgentGenerator
                from agentfactory.integration import LiveGenerator

                # Inside a batch, each task is capped at its reserved share.
                cap = LIVE_RESERVE_USD if batch else self.per_run_budget_usd
                live = LiveGenerator(AgentGenerator(),
                                     budget_usd=min(cap, self.budget.left_usd))
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
            # Stored unsafe or wrong agents get an empty registry of their own:
            # with the shared one, a task already registered would be served
            # from it and the stored agent would never be shown.
            shared = req.mode in ("live", "safe_a1")
            registry = InstrumentedRegistry(self.registry if shared else InMemoryRegistry(), run)
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
            with self._money:
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
            if own_lock:
                self._busy.release()
            # Keep memory bounded: the last 80 runs.
            for old in list(self.runs)[:-80]:
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


class _LockedEmbedder:
    """The shared embedding model, used by one run at a time."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._lock = threading.Lock()

    def embed(self, text: str) -> list[float]:
        with self._lock:
            return self.inner.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return self.inner.embed_batch(texts)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)
