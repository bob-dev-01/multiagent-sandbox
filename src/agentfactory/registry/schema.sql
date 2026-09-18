-- Agent Registry schema.
--
-- Task 7 specifies the registry only as "PostgreSQL with JSON columns", so the
-- table design below is new. Two decisions worth flagging:
--
-- 1. pgvector rather than a FAISS index rebuilt on every registration. Task 7's
--    FAISS approach is O(n) per write and needs a separate index artifact kept
--    consistent with the database; an ivfflat index gives approximate nearest
--    neighbour in the same transaction as the insert, with no second store to
--    keep in sync.
-- 2. TSR history is a separate table, not an array column. The reuse rule reads
--    "the five most recent scores", and an append-only table makes that a
--    query rather than a read-modify-write race between concurrent executors.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Embedding dimensionality of all-MiniLM-L6-v2, the default local model.
-- Changing the embedding model means reindexing: the similarity threshold
-- tau = 0.85 is only meaningful relative to one model.
CREATE TABLE IF NOT EXISTS registered_agents (
    agent_id            UUID PRIMARY KEY,
    task_id             TEXT        NOT NULL,
    schema_version      TEXT        NOT NULL,
    code_sha256         CHAR(64)    NOT NULL,
    generated_code_b64  TEXT        NOT NULL,
    contract            JSONB       NOT NULL,
    generator_model     TEXT,
    embedding_model     TEXT        NOT NULL,
    task_embedding      vector(384),
    registered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at        TIMESTAMPTZ,
    reuse_count         INTEGER     NOT NULL DEFAULT 0,
    total_invocations   INTEGER     NOT NULL DEFAULT 0,
    -- Set when the rolling TSR drops below the re-validation floor. A flagged
    -- agent is never served for reuse until it is validated again.
    revalidation_required BOOLEAN   NOT NULL DEFAULT FALSE,
    retired_at          TIMESTAMPTZ,
    CONSTRAINT code_sha256_is_hex CHECK (code_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_agents_task       ON registered_agents (task_id);
CREATE INDEX IF NOT EXISTS idx_agents_sha        ON registered_agents (code_sha256);
CREATE INDEX IF NOT EXISTS idx_agents_available
    ON registered_agents (revalidation_required, retired_at)
    WHERE retired_at IS NULL AND revalidation_required = FALSE;

-- Cosine distance, matching the similarity measure the reuse rule is stated in.
CREATE INDEX IF NOT EXISTS idx_agents_embedding
    ON registered_agents USING ivfflat (task_embedding vector_cosine_ops)
    WITH (lists = 100);

-- Append-only performance history.
CREATE TABLE IF NOT EXISTS agent_performance (
    id            BIGSERIAL PRIMARY KEY,
    agent_id      UUID        NOT NULL REFERENCES registered_agents(agent_id) ON DELETE CASCADE,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    task_id       TEXT        NOT NULL,
    tsr           REAL        NOT NULL CHECK (tsr >= 0 AND tsr <= 1),
    latency_ms    REAL,
    run_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_perf_agent_recent
    ON agent_performance (agent_id, recorded_at DESC);

-- Policy violations observed during execution, after validation passed. This is
-- the second line of defence described in architecture.md section 10.3, and the
-- source of the Catastrophic Error Rate.
CREATE TABLE IF NOT EXISTS policy_violations (
    id            BIGSERIAL PRIMARY KEY,
    agent_id      UUID        NOT NULL REFERENCES registered_agents(agent_id) ON DELETE CASCADE,
    observed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id        TEXT,
    violation     TEXT        NOT NULL,
    detail        JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_violations_agent ON policy_violations (agent_id);

-- Validation verdicts, kept so a registered agent's provenance survives the
-- JSONL logs and so re-validation can be compared against the original verdict.
CREATE TABLE IF NOT EXISTS validation_results (
    id                 BIGSERIAL PRIMARY KEY,
    agent_id           UUID        NOT NULL,
    task_id            TEXT        NOT NULL,
    code_sha256        CHAR(64)    NOT NULL,
    verdict            TEXT        NOT NULL
                       CHECK (verdict IN ('PASS','FAIL_UNSAFE','FAIL_INCORRECT','FAIL_BOTH')),
    confidence         REAL        NOT NULL,
    isolation_level    TEXT        NOT NULL CHECK (isolation_level IN ('L1','L2','L3')),
    correctness_score  REAL,
    correctness_source TEXT,
    total_latency_ms   REAL        NOT NULL,
    strictness_profile TEXT,
    run_id             TEXT,
    git_commit         TEXT,
    payload            JSONB       NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_validation_agent ON validation_results (agent_id);
CREATE INDEX IF NOT EXISTS idx_validation_run   ON validation_results (run_id, isolation_level);

-- Rolling TSR over the five most recent runs, which is what the reuse rule
-- reads. A view rather than a stored column so it cannot drift from the history.
CREATE OR REPLACE VIEW agent_recent_tsr AS
SELECT
    a.agent_id,
    a.task_id,
    a.reuse_count,
    a.revalidation_required,
    a.retired_at,
    COALESCE(recent.mean_tsr, 1.0) AS mean_tsr,
    COALESCE(recent.n, 0)          AS n_recent
FROM registered_agents a
LEFT JOIN LATERAL (
    SELECT AVG(tsr)::REAL AS mean_tsr, COUNT(*) AS n
    FROM (
        SELECT tsr
        FROM agent_performance p
        WHERE p.agent_id = a.agent_id
        ORDER BY p.recorded_at DESC
        LIMIT 5
    ) last_five
) recent ON TRUE;
