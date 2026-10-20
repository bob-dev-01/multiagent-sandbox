"""Wrappers that let the demo watch the system without changing what it does.

Each wraps one real component, emits demo events around the call, and returns
the component's own result untouched. Nothing here makes a decision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agentfactory.contracts import AgentSpec
from agentfactory.registry.memory import cosine_similarity
from agentfactory.validation.aggregator import StrictnessProfile, assess
from agentfactory.validation.pipeline import ValidationPipeline

from .events import DemoRun

PROFILES = ("permissive", "balanced", "strict", "paranoid")


def contract_view(spec: AgentSpec) -> dict[str, Any]:
    c = spec.contract
    return {
        "authorized_tools": list(c.authorized_tools),
        "cpu_cores": c.resource_limits.cpu_cores,
        "memory_mb": c.resource_limits.memory_mb,
        "max_duration_sec": c.resource_limits.max_duration_sec,
        "network_access": c.resource_limits.network_access,
        "evidence": list(c.evidence_requirements),
    }


@dataclass
class Scripted:
    """A generation result made from a stored agent rather than a model call."""

    spec: AgentSpec
    model: str = "corpus (scripted)"
    reasoning: str = "a stored agent, served instead of a model call"
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class ScriptedGenerator:
    """Serves stored agents first, then hands over to the live generator.

    A demo scenario such as "the model writes something wrong" is staged by
    serving a labelled corpus agent on the first attempt; the regeneration that
    follows is the real model's.
    """

    def __init__(self, scripted: list[AgentSpec], live: Any = None) -> None:
        self.scripted = list(scripted)
        self.live = live

    def generate(self, **kwargs: Any) -> Any:
        if self.scripted:
            return Scripted(spec=self.scripted.pop(0))
        if self.live is None:
            raise RuntimeError("no further scripted agents and live generation is off")
        return self.live.generate(**kwargs)


class InstrumentedGenerator:
    def __init__(self, inner: Any, run: DemoRun) -> None:
        self.inner = inner
        self.run = run
        self.attempt = 0
        self.spent_usd = 0.0

    def generate(self, **kwargs: Any) -> Any:
        self.attempt += 1
        feedback = kwargs.get("failure_feedback")
        self.run.emit("generation_started", attempt=self.attempt,
                      regeneration=bool(feedback),
                      task_spec=kwargs.get("task_spec"),
                      tools=list(kwargs.get("available_tools") or []))
        started = time.perf_counter()
        result = self.inner.generate(**kwargs)
        self.spent_usd += result.cost_usd
        self.run.emit("generation_finished", attempt=self.attempt,
                      seconds=round(time.perf_counter() - started, 1),
                      model=result.model, cost_usd=round(result.cost_usd, 4),
                      tokens={"in": result.input_tokens, "out": result.output_tokens},
                      reasoning=(result.reasoning or "")[:600],
                      source=result.spec.source, agent_id=str(result.spec.agent_id),
                      code_sha256=result.spec.code_sha256,
                      contract=contract_view(result.spec))
        return result


class InstrumentedRunner:
    """A sandbox runner that announces each run and reports its trace."""

    def __init__(self, inner: Any, demo: DemoRun, phase: str) -> None:
        self.inner = inner
        self.demo = demo
        self.phase = phase
        self.level = inner.level

    def available(self) -> bool:
        return self.inner.available()

    def preflight(self) -> None:
        if hasattr(self.inner, "preflight"):
            self.inner.preflight()

    def run(self, request: Any) -> Any:
        self.demo.emit("sandbox_started", phase=self.phase, level=self.level.value)
        trace = self.inner.run(request)
        self.demo.emit("sandbox_finished", phase=self.phase, level=self.level.value,
                       kernel=trace.kernel_release, agent_ms=trace.agent_elapsed_ms,
                       init_ms=trace.sandbox_init_ms, wall_ms=round(trace.wall_time_ms, 1),
                       network_attempts=list(trace.network_attempts),
                       fs_violations=list(trace.fs_violations),
                       unauthorized_tool_calls=list(trace.unauthorized_tool_calls),
                       runtime_violations=list(trace.runtime_violations),
                       output=trace.agent_output, match=trace.output_match_score)
        return trace


class InstrumentedRegistry:
    """The in-process registry, reporting the best match even below the threshold."""

    def __init__(self, inner: Any, run: DemoRun | None = None, *, tau: float = 0.85) -> None:
        self.inner = inner
        self.run = run
        self.tau = tau

    def find_reusable(self, query_embedding: list[float], **kwargs: Any) -> list[Any]:
        best, best_task = None, None
        for entry in self.inner._agents.values():
            sim = cosine_similarity(query_embedding, entry.embedding)
            if best is None or sim > best:
                best, best_task = sim, entry.spec.task_id
        matches = self.inner.find_reusable(query_embedding, **kwargs)
        if self.run is not None:
            self.run.emit("registry_lookup", registered=len(self.inner._agents),
                          best_similarity=None if best is None else round(best, 3),
                          best_task=best_task, tau=self.tau,
                          decision="reuse" if matches else "generate")
        return matches

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class InstrumentedJudge:
    def __init__(self, inner: Any, run: DemoRun) -> None:
        self.inner = inner
        self.run = run
        self.spent_usd = 0.0

    def score(self, *args: Any, **kwargs: Any) -> Any:
        self.run.emit("judge_started")
        result = self.inner.score(*args, **kwargs)
        self.spent_usd += result.cost_usd
        self.run.emit("judge_finished", score=result.score,
                      justification=result.justification[:800],
                      cost_usd=round(result.cost_usd, 4))
        return result


class InstrumentedPipeline(ValidationPipeline):
    """The real pipeline; afterwards, the verdict under every strictness profile."""

    def __init__(self, *args: Any, demo_run: DemoRun, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.demo_run = demo_run

    def validate(self, spec: AgentSpec, **kwargs: Any) -> Any:
        self.demo_run.emit("validation_started", agent_id=str(spec.agent_id),
                           profile=self.config.profile.name,
                           short_circuit=self.config.short_circuit)
        result = super().validate(spec, **kwargs)
        own = assess(result.layer_reports, spec, self.config.profile)
        by_profile = {
            name: assess(result.layer_reports, spec, StrictnessProfile.named(name)).verdict().value
            for name in PROFILES
        }
        self.demo_run.emit("verdict", verdict=result.verdict.value,
                           profile=self.config.profile.name, by_profile=by_profile,
                           safety_reasons=list(own.safety_reasons)[:8],
                           correctness_reasons=list(own.correctness_reasons)[:8],
                           correctness_score=result.correctness_score)
        return result
