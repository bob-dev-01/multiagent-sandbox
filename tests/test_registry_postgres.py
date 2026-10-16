"""The PostgreSQL registry against the in-process one, on the integration test.

Every measured run used `InMemoryRegistry`; the deployed design uses
PostgreSQL with pgvector (`registry/store.py`, `registry/schema.sql`). Here the
same seeds run through both and must agree on every outcome — reuse, the TSR
floor, violations — so the in-process results stand for the database too.

Needs a disposable database with the pgvector extension available, e.g.

    docker run -d --name af-pg-test -e POSTGRES_PASSWORD=... -p 55432:5432 pgvector/pgvector:pg16
    AGENTFACTORY_TEST_PG_DSN=postgresql://postgres:...@localhost:55432/registry pytest tests/test_registry_postgres.py

Skipped without it. Each run starts from empty tables.
"""

from __future__ import annotations

import os

import pytest

from agentfactory.executor import TaskExecutor
from agentfactory.integration import run_integration
from agentfactory.orchestrator.factory import AgentFactory
from agentfactory.validation.pipeline import PipelineConfig, ValidationPipeline

from .test_integration import TASKS, GuardedRunner, TaskOracleEmbedder, _builder

DSN = os.environ.get("AGENTFACTORY_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="AGENTFACTORY_TEST_PG_DSN not set")

SUBSET = TASKS[:8]
SEEDS = (1, 2)


class Oracle384(TaskOracleEmbedder):
    """The task-oracle embedder, padded to the schema's 384 dimensions."""

    def embed(self, text: str) -> list[float]:
        return super().embed(text) + [0.0] * (384 - self.dimension)


def _comparable(result):
    return [
        (r.seed, r.phase, r.task_id, r.paraphrase, r.outcome, r.succeeded, r.reused,
         r.attempts, r.refusals, r.executed_labels, r.executed_unsafe, r.verdicts)
        for r in result.records
    ]


def _postgres_builder(local_runner, embedder):
    from agentfactory.registry.store import AgentRegistry

    runner = GuardedRunner(local_runner)

    def build(generator):
        registry = AgentRegistry(DSN)
        registry.initialise()
        with registry._connect() as conn, conn.cursor() as cur:
            # A fresh registry per seed, as the in-process one is.
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            tables = [row[0] for row in cur.fetchall()]
            if tables:
                cur.execute("TRUNCATE " + ", ".join(tables) + " CASCADE")
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


def test_postgres_registry_matches_the_in_process_one() -> None:
    from .conftest import LocalHarnessRunner

    embedder = Oracle384()
    memory = run_integration(SUBSET, _builder(LocalHarnessRunner(), embedder), seeds=SEEDS)
    postgres = run_integration(SUBSET, _postgres_builder(LocalHarnessRunner(), embedder),
                               seeds=SEEDS)
    assert _comparable(postgres) == _comparable(memory)
    assert postgres.safety() == memory.safety()
    assert any(r.reused for r in postgres.records)
