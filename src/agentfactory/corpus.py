"""Ground-truth corpus loader.

The corpus is an instrument, not test data: every classification metric in the
study is computed against these labels, so the loader is strict about them. It
verifies the label distribution per task, hashes every agent, and refuses to
load a task whose variants do not match the pre-registered design.

That strictness is the point. A corpus that silently drifts — an extra safe
agent here, a mislabelled variant there — produces metrics that look fine and
mean nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentfactory.contracts import AgentContract, AgentSpec, ResourceLimits
from agentfactory.metrics.core import GroundTruth
from agentfactory.orchestrator.factory import TaskSpecification

CORPUS_ROOT = Path(__file__).resolve().parents[2] / "corpus" / "tasks"

# The pre-registered distribution, per task (Task 7 section 4.3.1).
EXPECTED_DISTRIBUTION: dict[GroundTruth, int] = {
    GroundTruth.SAFE_CORRECT: 5,
    GroundTruth.UNSAFE_ONLY: 2,
    GroundTruth.INCORRECT_ONLY: 2,
    GroundTruth.BOTH: 1,
}

VALID_DOMAINS = {"incident_response", "data_transformation", "knowledge_retrieval"}


class CorpusError(ValueError):
    """The corpus on disk does not match the pre-registered design."""


@dataclass(frozen=True)
class CorpusAgent:
    """One labelled agent variant."""

    task_id: str
    variant: str
    label: GroundTruth
    failure_mode: str | None
    source: str

    @property
    def agent_key(self) -> str:
        return f"{self.task_id}:{self.variant}"

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()

    def to_spec(self, contract: AgentContract) -> AgentSpec:
        return AgentSpec.from_source(
            task_id=self.task_id,
            source=self.source,
            contract=contract,
            generator_model="corpus",
        )


@dataclass(frozen=True)
class CorpusTask:
    """A task specification plus its ten labelled agents."""

    task_id: str
    domain: str
    title: str
    description: str
    available_tools: tuple[str, ...]
    expected_output: dict[str, Any] | None
    correctness_rubric: str
    risk_tier: str
    agents: tuple[CorpusAgent, ...]
    # Reworded descriptions of the same task, for measuring reuse on semantic
    # matching rather than identical text (OQ-8). Not part of the manifest hash:
    # they change what the integration test asks, not what the agents are.
    paraphrases: tuple[str, ...] = ()
    # What this task's tools return, by tool name. A tool not listed returns the
    # harness's generic stub. The expected output is derived from this data.
    tool_fixtures: dict[str, Any] = field(default_factory=dict)

    @property
    def content_sha256(self) -> str:
        """Identity of what the labels are judged against.

        The labels mean "correct for this description, these tools, this data
        and this expected output" — change any of them and the same agent code
        can change label, so all of them are hashed, not just the agents.
        """
        payload = json.dumps(
            {
                "description": self.description,
                "available_tools": list(self.available_tools),
                "expected_output": self.expected_output,
                "tool_fixtures": self.tool_fixtures,
                "risk_tier": self.risk_tier,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def contract(self) -> AgentContract:
        """The reference contract every variant of this task is validated against.

        Using one contract per task rather than one per variant keeps the
        experiment's independent variable clean: what differs between variants
        is the code, not what the code was permitted to do.
        """
        return AgentContract(
            task_spec=self.description,
            authorized_tools=self.available_tools,
            resource_limits=ResourceLimits(cpu_cores=1, memory_mb=256, max_duration_sec=30),
        )

    def to_task_specification(self) -> TaskSpecification:
        return TaskSpecification(
            task_id=self.task_id,
            description=self.description,
            available_tools=list(self.available_tools),
            expected_output=self.expected_output,
            risk_tier=self.risk_tier,
            tool_fixtures=dict(self.tool_fixtures),
        )

    def specs(self) -> Iterator[tuple[CorpusAgent, AgentSpec]]:
        contract = self.contract()
        for agent in self.agents:
            yield agent, agent.to_spec(contract)


def _parse_label(raw: str, where: str) -> GroundTruth:
    try:
        return GroundTruth(raw)
    except ValueError as exc:
        raise CorpusError(
            f"{where}: unknown label {raw!r}; expected one of "
            f"{[g.value for g in GroundTruth]}"
        ) from exc


def load_task(path: Path) -> CorpusTask:
    """Load and validate one task file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CorpusError(f"{path.name}: file is not a mapping")

    for required in ("task_id", "domain", "description", "available_tools", "agents"):
        if required not in raw:
            raise CorpusError(f"{path.name}: missing required field {required!r}")

    if raw["domain"] not in VALID_DOMAINS:
        raise CorpusError(
            f"{path.name}: domain {raw['domain']!r} is not one of {sorted(VALID_DOMAINS)}"
        )

    agents: list[CorpusAgent] = []
    seen_variants: set[str] = set()
    for entry in raw["agents"]:
        variant = entry["variant"]
        if variant in seen_variants:
            raise CorpusError(f"{path.name}: duplicate variant {variant!r}")
        seen_variants.add(variant)
        source = entry["source"]
        if not source.strip():
            raise CorpusError(f"{path.name}:{variant}: empty source")
        if "def run(" not in source:
            raise CorpusError(
                f"{path.name}:{variant}: does not define run(task_input, tools)"
            )
        agents.append(
            CorpusAgent(
                task_id=raw["task_id"],
                variant=variant,
                label=_parse_label(entry["label"], f"{path.name}:{variant}"),
                failure_mode=entry.get("failure_mode"),
                source=source,
            )
        )

    distribution = Counter(a.label for a in agents)
    if distribution != Counter(EXPECTED_DISTRIBUTION):
        raise CorpusError(
            f"{path.name}: label distribution {dict(distribution)} does not match the "
            f"pre-registered {dict(EXPECTED_DISTRIBUTION)}"
        )

    # An injected failure must be named. An unnamed one cannot be broken out by
    # failure type later, and is usually a copy-paste slip.
    for agent in agents:
        if agent.label is not GroundTruth.SAFE_CORRECT and not agent.failure_mode:
            raise CorpusError(
                f"{path.name}:{agent.variant}: labelled {agent.label.value} "
                "but names no failure_mode"
            )
        if agent.label is GroundTruth.SAFE_CORRECT and agent.failure_mode:
            raise CorpusError(
                f"{path.name}:{agent.variant}: labelled safe_correct but names a failure_mode"
            )

    # Fail closed on correctness: the sandbox's automatic check is an exact
    # match against this, so a task without it cannot distinguish a correct
    # agent from one that returns anything at all.
    expected = raw.get("expected_output")
    if not isinstance(expected, dict) or not expected:
        raise CorpusError(f"{path.name}: expected_output is missing or empty")

    fixtures = raw.get("tool_fixtures") or {}
    if not isinstance(fixtures, dict):
        raise CorpusError(f"{path.name}: tool_fixtures must be a mapping of tool -> response")
    stray = sorted(set(fixtures) - set(raw["available_tools"]))
    if stray:
        # Data for a tool the task does not offer would never be seen, and is
        # almost always a typo in a tool name that leaves the real tool on its
        # generic stub.
        raise CorpusError(f"{path.name}: tool_fixtures for tools the task does not offer: {stray}")

    return CorpusTask(
        task_id=raw["task_id"],
        domain=raw["domain"],
        title=raw.get("title", raw["task_id"]),
        description=raw["description"].strip(),
        available_tools=tuple(raw["available_tools"]),
        expected_output=raw.get("expected_output"),
        correctness_rubric=raw.get("correctness_rubric", "").strip(),
        risk_tier=raw.get("risk_tier", "standard"),
        agents=tuple(agents),
        paraphrases=tuple(" ".join(p.split()) for p in raw.get("paraphrases") or ()),
        tool_fixtures=fixtures,
    )


def load_corpus(root: Path | str | None = None) -> list[CorpusTask]:
    """Load every task file, sorted by task id."""
    root = Path(root) if root else CORPUS_ROOT
    if not root.exists():
        raise CorpusError(f"corpus directory not found: {root}")

    tasks = [load_task(path) for path in sorted(root.glob("*.yaml"))]
    if not tasks:
        raise CorpusError(f"no task files in {root}")

    ids = [t.task_id for t in tasks]
    duplicates = [i for i, count in Counter(ids).items() if count > 1]
    if duplicates:
        raise CorpusError(f"duplicate task ids: {duplicates}")

    return tasks


def manifest(tasks: list[CorpusTask]) -> dict[str, Any]:
    """Content-addressed manifest of the whole corpus.

    Written alongside every experiment batch so a result can be tied to the
    exact corpus that produced it, and so an accidental edit between batches is
    detectable rather than invisible.
    """
    entries = {
        agent.agent_key: {
            "sha256": agent.source_sha256,
            "label": agent.label.value,
            "failure_mode": agent.failure_mode,
        }
        for task in tasks
        for agent in task.agents
    }
    task_entries = {task.task_id: task.content_sha256 for task in tasks}
    # Code, label and the task each label is judged against: relabelling an
    # agent or changing a task's expected output changes the hash, which the
    # agent hashes alone did not.
    combined = hashlib.sha256(
        (
            "".join(f"{k}:{v['sha256']}:{v['label']}" for k, v in sorted(entries.items()))
            + "".join(f"{k}:{v}" for k, v in sorted(task_entries.items()))
        ).encode()
    ).hexdigest()
    return {
        "n_tasks": len(tasks),
        "n_agents": len(entries),
        "domains": sorted({t.domain for t in tasks}),
        "corpus_sha256": combined,
        "tasks": task_entries,
        "agents": entries,
    }
