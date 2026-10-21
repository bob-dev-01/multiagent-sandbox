"""In-process Agent Registry with the PostgreSQL registry's semantics.

For the integration harness and the tests, so the reuse loop can be exercised
without a database. Every rule is the one `store.AgentRegistry` enforces, and
the tests hold the two to the same behaviour:

- only PASS verdicts are admitted, and only for the code they were issued for;
- reuse needs similarity >= tau and a rolling TSR over the last five
  executions above the reuse floor; an agent not yet executed counts as 1.0,
  as the `agent_recent_tsr` view does;
- a rolling TSR under the re-validation floor, or any violation observed after
  PASS, flags the agent, and a flagged agent is never served.

Nothing persists. A run that needs the registry to outlive the process uses
the PostgreSQL one.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agentfactory.contracts import AgentContract, AgentSpec, ValidationResult

from .embeddings import cosine_similarity
from .store import (
    REUSE_SIMILARITY_THRESHOLD,
    REUSE_TSR_FLOOR,
    REVALIDATION_TSR_FLOOR,
    RegisteredAgent,
)

RECENT_WINDOW = 5


@dataclass
class _Entry:
    spec: AgentSpec
    embedding: list[float]
    registered_at: datetime
    tsr_history: list[float] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    reuse_count: int = 0
    revalidation_required: bool = False

    @property
    def mean_tsr(self) -> float:
        recent = self.tsr_history[-RECENT_WINDOW:]
        return sum(recent) / len(recent) if recent else 1.0


class InMemoryRegistry:
    def __init__(self) -> None:
        self._agents: dict[uuid.UUID, _Entry] = {}
        # Demo runs share one registry from several threads.
        self._lock = threading.RLock()

    # ----------------------------------------------------------- writes

    def register(
        self, spec: AgentSpec, result: ValidationResult, embedding: list[float], **_: Any
    ) -> None:
        if not result.verdict.is_pass:
            raise ValueError(
                f"refusing to register agent {spec.agent_id} with verdict {result.verdict}"
            )
        if result.code_sha256 != spec.code_sha256:
            raise ValueError("validation result does not match the spec it is being stored with")
        with self._lock:
            self._agents.setdefault(
                spec.agent_id, _Entry(spec=spec, embedding=list(embedding), registered_at=_now())
            )

    def record_execution(
        self, agent_id: uuid.UUID, task_id: str, tsr: float, **_: Any
    ) -> None:
        with self._lock:
            entry = self._agents[agent_id]
            entry.tsr_history.append(float(tsr))
            if entry.mean_tsr < REVALIDATION_TSR_FLOOR:
                entry.revalidation_required = True

    def record_violation(self, agent_id: uuid.UUID, violation: str, **_: Any) -> None:
        with self._lock:
            entry = self._agents[agent_id]
            entry.violations.append(violation)
            entry.revalidation_required = True

    def mark_reused(self, agent_id: uuid.UUID) -> None:
        with self._lock:
            self._agents[agent_id].reuse_count += 1

    # ------------------------------------------------------------ reads

    def find_reusable(
        self,
        query_embedding: list[float],
        *,
        similarity_threshold: float = REUSE_SIMILARITY_THRESHOLD,
        tsr_floor: float = REUSE_TSR_FLOOR,
        limit: int = 1,
    ) -> list[RegisteredAgent]:
        scored = []
        with self._lock:
            agents = list(self._agents.items())
        for agent_id, entry in agents:
            if entry.revalidation_required:
                continue
            similarity = cosine_similarity(query_embedding, entry.embedding)
            if similarity >= similarity_threshold and entry.mean_tsr > tsr_floor:
                scored.append((similarity, agent_id, entry))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            RegisteredAgent(
                agent_id=agent_id,
                task_id=entry.spec.task_id,
                code_sha256=entry.spec.code_sha256,
                contract=AgentContract.model_validate(entry.spec.contract.model_dump()),
                similarity=similarity,
                mean_tsr=entry.mean_tsr,
                reuse_count=entry.reuse_count,
                registered_at=entry.registered_at,
            )
            for similarity, agent_id, entry in scored[:limit]
        ]

    def load_code(self, agent_id: uuid.UUID) -> str | None:
        entry = self._agents.get(agent_id)
        return entry.spec.generated_code_b64 if entry else None

    def counts(self) -> dict[str, int]:
        return {
            "active": len(self._agents),
            "flagged": sum(e.revalidation_required for e in self._agents.values()),
            "total_reuses": sum(e.reuse_count for e in self._agents.values()),
        }

    def violations(self, agent_id: uuid.UUID) -> list[str]:
        return list(self._agents[agent_id].violations)


def _now() -> datetime:
    return datetime.now(UTC)
