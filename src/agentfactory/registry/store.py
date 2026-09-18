"""Agent Registry — persistence and the reuse decision.

The reuse rule from architecture.md section 5.3:

    reuse  <=>  cosine_similarity >= tau (0.85)  AND  rolling TSR > 0.80
    re-validate  <=>  rolling TSR < 0.70

Both halves matter. Similarity alone would serve an agent that matches the task
but has been failing; the TSR floor alone would serve a healthy agent at the
wrong task.

A caveat the study should not paper over: measuring Agent Reuse Rate by
re-submitting identical task text makes the similarity test trivial, because
identical strings clear any threshold. `find_reusable` is therefore written to
take an arbitrary query embedding rather than a task id, so the harness can feed
it paraphrased variants and measure something that is actually about semantic
matching (OQ-8).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agentfactory.contracts import AgentContract, AgentSpec, ValidationResult

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

REUSE_SIMILARITY_THRESHOLD = 0.85
REUSE_TSR_FLOOR = 0.80
REVALIDATION_TSR_FLOOR = 0.70


@dataclass(frozen=True)
class RegisteredAgent:
    agent_id: uuid.UUID
    task_id: str
    code_sha256: str
    contract: AgentContract
    similarity: float
    mean_tsr: float
    reuse_count: int
    registered_at: datetime

    def to_spec(self, generated_code_b64: str) -> AgentSpec:
        return AgentSpec(
            agent_id=self.agent_id,
            task_id=self.task_id,
            contract=self.contract,
            generated_code_b64=generated_code_b64,
        )


class AgentRegistry:
    """PostgreSQL-backed registry. Requires the pgvector extension."""

    def __init__(self, dsn: str, *, embedding_model: str = "all-MiniLM-L6-v2") -> None:
        self.dsn = dsn
        self.embedding_model = embedding_model

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        import psycopg

        with psycopg.connect(self.dsn, autocommit=False) as conn:
            yield conn

    def initialise(self) -> None:
        """Apply the schema. Idempotent — every statement is IF NOT EXISTS."""
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()

    # ----------------------------------------------------------- writes

    def register(
        self,
        spec: AgentSpec,
        result: ValidationResult,
        embedding: list[float],
        *,
        run_id: str | None = None,
        git_commit: str | None = None,
        strictness_profile: str | None = None,
    ) -> None:
        """Register a validated agent.

        Refuses anything that did not pass. The registry is the set of agents
        the system is willing to reuse without re-validating, so admitting a
        failed verdict here would quietly turn a caught unsafe agent into a
        deployable one.
        """
        if not result.verdict.is_pass:
            raise ValueError(
                f"refusing to register agent {spec.agent_id} with verdict {result.verdict}"
            )
        if result.code_sha256 != spec.code_sha256:
            raise ValueError("validation result does not match the spec it is being stored with")

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO registered_agents (
                        agent_id, task_id, schema_version, code_sha256,
                        generated_code_b64, contract, generator_model,
                        embedding_model, task_embedding
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (agent_id) DO NOTHING
                    """,
                    (
                        str(spec.agent_id),
                        spec.task_id,
                        spec.schema_version,
                        spec.code_sha256,
                        spec.generated_code_b64,
                        json.dumps(spec.contract.model_dump(mode="json")),
                        spec.generator_model,
                        self.embedding_model,
                        _vector_literal(embedding),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO validation_results (
                        agent_id, task_id, code_sha256, verdict, confidence,
                        isolation_level, correctness_score, correctness_source,
                        total_latency_ms, strictness_profile, run_id, git_commit, payload
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(result.agent_id),
                        result.task_id,
                        result.code_sha256,
                        result.verdict.value,
                        result.confidence,
                        result.isolation_level.value,
                        result.correctness_score,
                        result.correctness_source,
                        result.total_latency_ms,
                        strictness_profile,
                        run_id,
                        git_commit,
                        json.dumps(result.model_dump(mode="json")),
                    ),
                )
            conn.commit()

    def record_execution(
        self, agent_id: uuid.UUID, task_id: str, tsr: float, *,
        latency_ms: float | None = None, run_id: str | None = None,
    ) -> None:
        """Append a TSR observation and re-evaluate the re-validation flag."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_performance (agent_id, task_id, tsr, latency_ms, run_id)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (str(agent_id), task_id, tsr, latency_ms, run_id),
                )
                cur.execute(
                    "UPDATE registered_agents SET total_invocations = total_invocations + 1,"
                    " last_used_at = now() WHERE agent_id = %s",
                    (str(agent_id),),
                )
                # Flag for re-validation if the rolling mean has fallen through
                # the floor. Done in the same transaction as the observation so
                # a concurrent reuse cannot slip between the two.
                cur.execute(
                    "UPDATE registered_agents SET revalidation_required = TRUE"
                    " WHERE agent_id = %s AND %s > ("
                    "   SELECT mean_tsr FROM agent_recent_tsr WHERE agent_id = %s)",
                    (str(agent_id), REVALIDATION_TSR_FLOOR, str(agent_id)),
                )
            conn.commit()

    def record_violation(
        self, agent_id: uuid.UUID, violation: str, *,
        detail: dict[str, Any] | None = None, run_id: str | None = None,
    ) -> None:
        """Record a policy violation seen after the agent passed validation.

        This is what a Catastrophic Error Rate event looks like in the data.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO policy_violations (agent_id, violation, detail, run_id)"
                    " VALUES (%s,%s,%s,%s)",
                    (str(agent_id), violation, json.dumps(detail or {}), run_id),
                )
                cur.execute(
                    "UPDATE registered_agents SET revalidation_required = TRUE WHERE agent_id = %s",
                    (str(agent_id),),
                )
            conn.commit()

    # ------------------------------------------------------------ reads

    def find_reusable(
        self,
        query_embedding: list[float],
        *,
        similarity_threshold: float = REUSE_SIMILARITY_THRESHOLD,
        tsr_floor: float = REUSE_TSR_FLOOR,
        limit: int = 1,
    ) -> list[RegisteredAgent]:
        """Best reusable agents for a task embedding, strongest match first."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.agent_id, a.task_id, a.code_sha256, a.contract,
                       1 - (a.task_embedding <=> %s::vector) AS similarity,
                       t.mean_tsr, a.reuse_count, a.registered_at
                FROM registered_agents a
                JOIN agent_recent_tsr t USING (agent_id)
                WHERE a.retired_at IS NULL
                  AND a.revalidation_required = FALSE
                  AND a.task_embedding IS NOT NULL
                  AND 1 - (a.task_embedding <=> %s::vector) >= %s
                  AND t.mean_tsr > %s
                ORDER BY similarity DESC
                LIMIT %s
                """,
                (
                    _vector_literal(query_embedding),
                    _vector_literal(query_embedding),
                    similarity_threshold,
                    tsr_floor,
                    limit,
                ),
            )
            rows = cur.fetchall()

        return [
            RegisteredAgent(
                agent_id=uuid.UUID(str(row[0])),
                task_id=row[1],
                code_sha256=row[2],
                contract=AgentContract.model_validate(
                    row[3] if isinstance(row[3], dict) else json.loads(row[3])
                ),
                similarity=float(row[4]),
                mean_tsr=float(row[5]),
                reuse_count=int(row[6]),
                registered_at=row[7],
            )
            for row in rows
        ]

    def mark_reused(self, agent_id: uuid.UUID) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE registered_agents SET reuse_count = reuse_count + 1 WHERE agent_id = %s",
                    (str(agent_id),),
                )
            conn.commit()

    def load_code(self, agent_id: uuid.UUID) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT generated_code_b64 FROM registered_agents WHERE agent_id = %s",
                (str(agent_id),),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def counts(self) -> dict[str, int]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FILTER (WHERE retired_at IS NULL),"
                "       count(*) FILTER (WHERE revalidation_required),"
                "       COALESCE(sum(reuse_count), 0)"
                " FROM registered_agents"
            )
            active, flagged, reuses = cur.fetchone()
        return {"active": int(active), "flagged": int(flagged), "total_reuses": int(reuses)}


def _vector_literal(values: list[float]) -> str:
    """pgvector's text input format."""
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"
