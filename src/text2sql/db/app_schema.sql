-- Application tables: query history and user feedback.
-- Separate `app` schema: the read-only role that runs generated SQL has no USAGE on it,
-- so a generated query can never read (or leak) other users' questions. The guardrails
-- also reject any schema other than `public`.
-- Idempotent: never drops data (the demo seed only recreates the `public` tables).

CREATE SCHEMA IF NOT EXISTS app;
REVOKE ALL ON SCHEMA app FROM PUBLIC;

CREATE TABLE IF NOT EXISTS app.query_history (
    query_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at            timestamptz NOT NULL DEFAULT now(),
    question              text NOT NULL,
    status                text NOT NULL,
    sql_source            text NOT NULL CHECK (sql_source IN ('llm', 'user')),
    sql_generated         text,
    sql_executed          text,
    confidence            real,
    likely_hallucination  boolean,
    row_count             integer,
    duration_ms           real NOT NULL,
    cost_usd              numeric(12, 6) NOT NULL DEFAULT 0,
    response              jsonb NOT NULL  -- full API response (rows capped)
);
CREATE INDEX IF NOT EXISTS query_history_created_at_idx ON app.query_history (created_at DESC);

-- One verdict per answer (the latest one wins). Incorrect answers are exported as
-- candidate evaluation cases (python -m text2sql.feedback).
CREATE TABLE IF NOT EXISTS app.feedback (
    query_id       bigint PRIMARY KEY REFERENCES app.query_history (query_id) ON DELETE CASCADE,
    created_at     timestamptz NOT NULL DEFAULT now(),
    verdict        text NOT NULL CHECK (verdict IN ('correct', 'incorrect')),
    comment        text,
    corrected_sql  text
);
