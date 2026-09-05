"""Postgres storage adapter.

Proves the Storage Protocol scales beyond SQLite. Multi-process safe (real
DB locks, not in-process locks). For v0 this is opt-in — you have to install
`psycopg[binary,pool]` and pass a postgres URL to `verdict.init()`.

URL form (any libpq-compatible URL works):
    postgres://user:pass@host:5432/dbname
    postgresql://user:pass@host:5432/dbname

The schema mirrors SQLiteStorage so migrations are linear: the same logical
data lives in both adapters; only the SQL dialect differs.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime

from verdict.analysis_records import (
    DeliveryOutcome,
    DeterministicAnalysisRun,
    NotificationDeliveryAttempt,
    analysis_run_from_json,
    analysis_run_to_json,
    notification_attempt_from_json,
    notification_attempt_to_json,
    validate_delivery_query,
)
from verdict.evidence import (
    AgentRunBundle,
    agent_run_bundle_from_json,
    agent_run_bundle_to_json,
)
from verdict.monitoring import (
    CohortManifest,
    MonitorComparison,
    MonitorPolicy,
    monitor_policy_from_json,
    monitor_policy_to_json,
    monitor_snapshot_from_json,
    monitor_snapshot_to_json,
)
from verdict.redaction import (
    sanitize_agent_run_bundle,
    sanitize_judgment,
    sanitize_span,
    sanitize_trace,
)
from verdict.schema import (
    ActiveClusterRegistry,
    ClusterIdentity,
    ClusterRegistryCluster,
    ClusterRegistryEvent,
    ClusterRegistryVersion,
    ClusterTraceCandidate,
    DimensionScore,
    DriftDirection,
    DriftRun,
    DriftSignal,
    EvaluatorHealthRecord,
    EvaluatorHealthStatus,
    Judgment,
    JudgmentStatus,
    Operation,
    SpanRecord,
    Trace,
    TraceClusterAssignment,
    UserSignalRecord,
    Verdict,
    cluster_candidate_digest,
    populate_trace_analysis_fields,
)
from verdict.storage.base import (
    _validate_agent_bundle_query,
    _validate_agent_bundle_run_id,
    _validate_drift_run_snapshot,
    _validate_evaluator_judgment_query,
)


def _trace_tenant_clause(requested: str) -> str:
    return (
        "(tenant_id IS NULL OR tenant_id=%s)"
        if requested == "__verdict_local__"
        else "tenant_id=%s"
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id          TEXT PRIMARY KEY,
    parent_span_id    TEXT,
    started_at        TIMESTAMPTZ NOT NULL,
    ended_at          TIMESTAMPTZ,
    provider          TEXT,
    operation         TEXT,
    request_model     TEXT,
    response_model    TEXT,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    temperature       DOUBLE PRECISION,
    max_tokens        INTEGER,
    finish_reason     TEXT,
    error             TEXT,
    latency_ms        DOUBLE PRECISION,
    prompt_redacted   TEXT,
    response_redacted TEXT,
    raw_messages      JSONB,
    tenant_id         TEXT,
    session_id        TEXT,
    user_id_hash      TEXT,
    cluster_id        TEXT,
    tags              JSONB DEFAULT '{}'::jsonb,
    cost_usd          DOUBLE PRECISION,
    analysis_started_at_us BIGINT,
    analysis_started_at_state TEXT NOT NULL DEFAULT 'pending',
    analysis_raw_messages_utf8_bytes BIGINT,
    analysis_raw_messages_state TEXT NOT NULL DEFAULT 'pending'
);
ALTER TABLE traces ADD COLUMN IF NOT EXISTS cluster_id TEXT;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS parent_span_id TEXT;
CREATE INDEX IF NOT EXISTS idx_traces_cluster  ON traces(cluster_id);
CREATE INDEX IF NOT EXISTS idx_traces_tenant   ON traces(tenant_id);
CREATE INDEX IF NOT EXISTS idx_traces_started  ON traces(started_at);

CREATE TABLE IF NOT EXISTS agent_run_bundles (
    tenant_id TEXT NOT NULL CHECK(octet_length(tenant_id) BETWEEN 1 AND 256),
    run_id TEXT NOT NULL CHECK(octet_length(run_id) BETWEEN 1 AND 256),
    source_session_id TEXT NOT NULL
        CHECK(octet_length(source_session_id) BETWEEN 1 AND 256),
    source_kind TEXT NOT NULL CHECK(octet_length(source_kind) BETWEEN 1 AND 256),
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK(status IN (
        'completed','failed','timed_out','cancelled','unknown')),
    content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
    payload_json TEXT NOT NULL CHECK(octet_length(payload_json)<=4194304),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_run_bundles_tenant_started
    ON agent_run_bundles(tenant_id, started_at DESC, run_id DESC);

CREATE TABLE IF NOT EXISTS deterministic_analysis_runs (
    analysis_id TEXT PRIMARY KEY CHECK(length(analysis_id)=64),
    tenant_id TEXT NOT NULL CHECK(octet_length(tenant_id) BETWEEN 1 AND 256),
    scope_key TEXT NOT NULL CHECK(octet_length(scope_key) BETWEEN 1 AND 512),
    cutoff TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed','error')),
    analyzer_version TEXT NOT NULL CHECK(octet_length(analyzer_version) BETWEEN 1 AND 128),
    input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64),
    payload_json TEXT NOT NULL CHECK(octet_length(payload_json)<=4194304),
    UNIQUE(tenant_id, scope_key, analyzer_version, input_fingerprint, status)
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_tenant_scope_latest
    ON deterministic_analysis_runs(tenant_id, scope_key, completed_at DESC, analysis_id DESC);

CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
    attempt_id TEXT PRIMARY KEY CHECK(length(attempt_id)=64),
    notification_id TEXT NOT NULL CHECK(length(notification_id)=64),
    tenant_id TEXT NOT NULL CHECK(octet_length(tenant_id) BETWEEN 1 AND 256),
    destination_fingerprint TEXT NOT NULL CHECK(length(destination_fingerprint)=64),
    attempted_at TIMESTAMPTZ NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('delivered','failed')),
    payload_json TEXT NOT NULL CHECK(octet_length(payload_json)<=4194304)
);
CREATE INDEX IF NOT EXISTS idx_notification_attempts_lookup
    ON notification_delivery_attempts(
        notification_id, destination_fingerprint, attempted_at DESC, attempt_id DESC
    );

CREATE TABLE IF NOT EXISTS monitor_policies (
    policy_id TEXT PRIMARY KEY CHECK(octet_length(policy_id) BETWEEN 1 AND 256),
    scope_key TEXT NOT NULL CHECK(octet_length(scope_key) BETWEEN 1 AND 512),
    state TEXT NOT NULL CHECK(state IN ('candidate','active','retired')),
    content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
    payload_json TEXT NOT NULL CHECK(octet_length(payload_json)<=262144),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_policies_one_active
    ON monitor_policies(scope_key) WHERE state='active';

CREATE TABLE IF NOT EXISTS monitor_snapshots (
    snapshot_id TEXT PRIMARY KEY CHECK(length(snapshot_id)=64),
    policy_id TEXT NOT NULL REFERENCES monitor_policies(policy_id),
    cutoff TIMESTAMPTZ NOT NULL,
    content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
    payload_json TEXT NOT NULL CHECK(octet_length(payload_json)<=4194304),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_monitor_snapshots_latest
    ON monitor_snapshots(policy_id, cutoff DESC, snapshot_id DESC);

CREATE TABLE IF NOT EXISTS judgments (
    judgment_id              TEXT PRIMARY KEY,
    trace_id                 TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
    rubric_name              TEXT,
    rubric_version           TEXT,
    created_at               TIMESTAMPTZ NOT NULL,
    judge_models             JSONB,
    dimensions               JSONB,
    position_swap_consistent BOOLEAN,
    evaluator_provider       TEXT,
    evaluator_config         JSONB,
    evaluator_fingerprint    TEXT,
    expected_dimensions      JSONB,
    status                   TEXT DEFAULT 'completed',
    error                    TEXT
);
CREATE INDEX IF NOT EXISTS idx_judgments_trace   ON judgments(trace_id);
CREATE INDEX IF NOT EXISTS idx_judgments_created ON judgments(created_at);

CREATE TABLE IF NOT EXISTS evaluator_health (
    health_id                TEXT PRIMARY KEY,
    evaluated_at             TIMESTAMPTZ NOT NULL,
    evaluator_fingerprint    TEXT NOT NULL,
    sentinel_set_name        TEXT,
    sentinel_set_fingerprint TEXT NOT NULL,
    correct_examples         INTEGER NOT NULL,
    total_examples           INTEGER NOT NULL,
    example_agreement        DOUBLE PRECISION,
    example_confidence_low   DOUBLE PRECISION,
    example_confidence_high  DOUBLE PRECISION,
    correct_labels           INTEGER NOT NULL,
    total_labels             INTEGER NOT NULL,
    label_agreement          DOUBLE PRECISION,
    status                   TEXT NOT NULL,
    error_count              INTEGER NOT NULL DEFAULT 0,
    method_version           TEXT NOT NULL DEFAULT '2'
);
CREATE INDEX IF NOT EXISTS idx_evaluator_health_identity
    ON evaluator_health(evaluator_fingerprint, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS drift_runs (
    run_id                TEXT PRIMARY KEY,
    analysis_time         TIMESTAMPTZ NOT NULL,
    completed_at          TIMESTAMPTZ NOT NULL,
    evaluator_fingerprint TEXT NOT NULL,
    signal_count          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drift_runs_latest
    ON drift_runs(evaluator_fingerprint, analysis_time DESC, completed_at DESC, run_id DESC);

CREATE TABLE IF NOT EXISTS drift_signals (
    signal_id              TEXT PRIMARY KEY,
    detected_at            TIMESTAMPTZ NOT NULL,
    cluster_id             TEXT,
    dimension              TEXT,
    direction              TEXT,
    evaluator_fingerprint  TEXT,
    run_id                 TEXT,
    statistic_name         TEXT,
    statistic_value        DOUBLE PRECISION,
    p_value                DOUBLE PRECISION,
    p_value_adjusted       DOUBLE PRECISION,
    effect_size_cohens_d   DOUBLE PRECISION,
    effect_size_cliffs_delta DOUBLE PRECISION DEFAULT 0.0,
    wasserstein_distance   DOUBLE PRECISION DEFAULT 0.0,
    psi                    DOUBLE PRECISION DEFAULT 0.0,
    sample_size_current    INTEGER,
    sample_size_baseline   INTEGER,
    contributing_layers    JSONB,
    example_trace_ids      JSONB,
    recommended_action     TEXT
);
ALTER TABLE drift_signals ADD COLUMN IF NOT EXISTS run_id TEXT;
CREATE INDEX IF NOT EXISTS idx_signals_detected     ON drift_signals(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_cluster_dim  ON drift_signals(cluster_id, dimension);
CREATE INDEX IF NOT EXISTS idx_signals_run          ON drift_signals(run_id);

CREATE TABLE IF NOT EXISTS cluster_registries (
    version      TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cluster_identities (
    tenant_id TEXT NOT NULL CHECK(octet_length(tenant_id) BETWEEN 1 AND 128),
    cluster_id TEXT NOT NULL CHECK(octet_length(cluster_id) BETWEEN 1 AND 64),
    kind TEXT NOT NULL CHECK(kind IN ('explicit','semantic')),
    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('provisional','active')),
    explicit_key TEXT CHECK(explicit_key IS NULL OR octet_length(explicit_key) BETWEEN 1 AND 64),
    display_name TEXT NOT NULL CHECK(octet_length(display_name) BETWEEN 1 AND 256),
    last_model_fingerprint TEXT,
    last_centroid JSONB CHECK(last_centroid IS NULL OR octet_length(last_centroid::text)<=65536),
    last_version_id TEXT,
    created_at TIMESTAMPTZ NOT NULL, created_by TEXT NOT NULL
        CHECK(octet_length(created_by)<=256),
    updated_at TIMESTAMPTZ NOT NULL, updated_by TEXT NOT NULL
        CHECK(octet_length(updated_by)<=256),
    PRIMARY KEY (tenant_id,cluster_id), UNIQUE (tenant_id,cluster_id,kind),
    UNIQUE (tenant_id,explicit_key),
    CHECK ((kind='explicit') = (explicit_key IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS cluster_registry_versions (
    tenant_id TEXT NOT NULL CHECK(octet_length(tenant_id) BETWEEN 1 AND 128),
    version_id TEXT NOT NULL CHECK(octet_length(version_id) BETWEEN 1 AND 64),
    parent_version_id TEXT, strategy TEXT NOT NULL
        CHECK(strategy IN ('explicit','semantic','hybrid')),
    cutoff TIMESTAMPTZ NOT NULL, lookback_days INTEGER NOT NULL CHECK(lookback_days>0),
    fit_definition_json JSONB NOT NULL
        CHECK(octet_length(fit_definition_json::text)<=65536),
    fit_definition_fingerprint TEXT NOT NULL,
    preview_report_json JSONB NOT NULL
        CHECK(octet_length(preview_report_json::text)<=1048576),
    created_at TIMESTAMPTZ NOT NULL, created_by TEXT NOT NULL
        CHECK(octet_length(created_by)<=256),
    PRIMARY KEY (tenant_id,version_id),
    FOREIGN KEY (tenant_id,parent_version_id)
        REFERENCES cluster_registry_versions(tenant_id,version_id)
);
CREATE TABLE IF NOT EXISTS cluster_registry_clusters (
    tenant_id TEXT NOT NULL, version_id TEXT NOT NULL, cluster_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('explicit','semantic')),
    centroid JSONB CHECK(centroid IS NULL OR octet_length(centroid::text)<=65536),
    radius DOUBLE PRECISION,
    member_count INTEGER NOT NULL CHECK(member_count>=0),
    outlier_count INTEGER NOT NULL CHECK(outlier_count>=0),
    PRIMARY KEY (tenant_id,version_id,cluster_id),
    UNIQUE (tenant_id,version_id,cluster_id,kind),
    FOREIGN KEY (tenant_id,version_id)
        REFERENCES cluster_registry_versions(tenant_id,version_id),
    FOREIGN KEY (tenant_id,cluster_id,kind)
        REFERENCES cluster_identities(tenant_id,cluster_id,kind),
    CHECK ((kind='explicit' AND centroid IS NULL AND radius IS NULL) OR
           (kind='semantic' AND centroid IS NOT NULL AND radius BETWEEN 0 AND 2))
);
CREATE TABLE IF NOT EXISTS trace_cluster_assignments (
    tenant_id TEXT NOT NULL, version_id TEXT NOT NULL,
    trace_id TEXT NOT NULL CHECK(octet_length(trace_id) BETWEEN 1 AND 256),
    origin TEXT NOT NULL CHECK(origin IN ('fit','incremental')),
    status TEXT NOT NULL CHECK(status IN ('assigned','outlier','ineligible')),
    cluster_id TEXT, cluster_kind TEXT, reason TEXT,
    distance DOUBLE PRECISION, assigned_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id,version_id,trace_id),
    UNIQUE (tenant_id,version_id,trace_id,cluster_id),
    FOREIGN KEY (tenant_id,version_id)
        REFERENCES cluster_registry_versions(tenant_id,version_id),
    FOREIGN KEY (tenant_id,version_id,cluster_id,cluster_kind)
        REFERENCES cluster_registry_clusters(tenant_id,version_id,cluster_id,kind),
    CHECK (
      (status='assigned' AND cluster_id IS NOT NULL AND
       cluster_kind IN ('explicit','semantic') AND reason IS NULL AND
       ((cluster_kind='explicit' AND distance IS NULL) OR
        (cluster_kind='semantic' AND distance IS NOT NULL AND distance BETWEEN 0 AND 2))) OR
      (status='outlier' AND cluster_id IS NULL AND cluster_kind IS NULL AND
       reason IN ('distance','explicit_key_not_in_version','semantic_fit_too_small') AND
       ((reason='distance' AND distance IS NOT NULL AND distance BETWEEN 0 AND 2) OR
        (reason<>'distance' AND distance IS NULL))) OR
      (status='ineligible' AND cluster_id IS NULL AND cluster_kind IS NULL AND
       distance IS NULL AND reason IN (
        'invalid_workload','unsafe_workload','missing_intent_key','invalid_intent_key',
        'unsafe_intent_key','content_not_captured','raw_messages_oversize',
        'malformed_messages','no_supported_user_text','text_too_short',
        'text_too_long','redaction_error')))
);
CREATE TABLE IF NOT EXISTS active_cluster_registry (
    tenant_id TEXT PRIMARY KEY CHECK(octet_length(tenant_id) BETWEEN 1 AND 128),
    version_id TEXT, generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
    activated_at TIMESTAMPTZ, activated_by TEXT,
    FOREIGN KEY (tenant_id,version_id)
        REFERENCES cluster_registry_versions(tenant_id,version_id)
);
CREATE TABLE IF NOT EXISTS cluster_registry_events (
    tenant_id TEXT NOT NULL CHECK(octet_length(tenant_id) BETWEEN 1 AND 128),
    event_id TEXT NOT NULL CHECK(octet_length(event_id) BETWEEN 1 AND 64),
    action TEXT NOT NULL CHECK(action IN (
      'validated','validation_failed','activated','rolled_back','renamed')),
    from_version_id TEXT, to_version_id TEXT,
    pointer_generation INTEGER CHECK(pointer_generation IS NULL OR pointer_generation>=0),
    created_at TIMESTAMPTZ NOT NULL, actor TEXT NOT NULL CHECK(octet_length(actor)<=256),
    details_json JSONB NOT NULL CHECK(octet_length(details_json::text)<=1048576),
    PRIMARY KEY (tenant_id,event_id),
    FOREIGN KEY (tenant_id,from_version_id)
        REFERENCES cluster_registry_versions(tenant_id,version_id),
    FOREIGN KEY (tenant_id,to_version_id)
        REFERENCES cluster_registry_versions(tenant_id,version_id)
);
CREATE INDEX IF NOT EXISTS idx_cluster_events_version
    ON cluster_registry_events(tenant_id,to_version_id,created_at,event_id);

CREATE OR REPLACE FUNCTION reject_cluster_registry_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION '% is immutable', TG_TABLE_NAME; END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS immutable_cluster_versions ON cluster_registry_versions;
CREATE TRIGGER immutable_cluster_versions BEFORE UPDATE OR DELETE
    ON cluster_registry_versions FOR EACH ROW EXECUTE FUNCTION reject_cluster_registry_mutation();
DROP TRIGGER IF EXISTS immutable_registry_clusters ON cluster_registry_clusters;
CREATE TRIGGER immutable_registry_clusters BEFORE UPDATE OR DELETE
    ON cluster_registry_clusters FOR EACH ROW EXECUTE FUNCTION reject_cluster_registry_mutation();
DROP TRIGGER IF EXISTS immutable_cluster_assignments ON trace_cluster_assignments;
CREATE TRIGGER immutable_cluster_assignments BEFORE UPDATE OR DELETE
    ON trace_cluster_assignments FOR EACH ROW EXECUTE FUNCTION reject_cluster_registry_mutation();
DROP TRIGGER IF EXISTS immutable_cluster_events ON cluster_registry_events;
CREATE TRIGGER immutable_cluster_events BEFORE UPDATE OR DELETE
    ON cluster_registry_events FOR EACH ROW EXECUTE FUNCTION reject_cluster_registry_mutation();
CREATE OR REPLACE FUNCTION guard_cluster_identity_update() RETURNS trigger AS $$
BEGIN
  IF NEW.tenant_id<>OLD.tenant_id OR NEW.cluster_id<>OLD.cluster_id OR
     NEW.kind<>OLD.kind OR NEW.explicit_key IS DISTINCT FROM OLD.explicit_key OR
     NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by OR
     (OLD.lifecycle='active' AND NEW.lifecycle<>'active') THEN
    RAISE EXCEPTION 'cluster identity immutable field changed';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS guarded_cluster_identity_update ON cluster_identities;
CREATE TRIGGER guarded_cluster_identity_update BEFORE UPDATE ON cluster_identities
    FOR EACH ROW EXECUTE FUNCTION guard_cluster_identity_update();

CREATE TABLE IF NOT EXISTS spans (
    span_id      TEXT PRIMARY KEY,
    name         TEXT,
    trace_id     TEXT,
    parent_name  TEXT,
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ,
    duration_ms  DOUBLE PRECISION,
    attributes   JSONB DEFAULT '{}'::jsonb,
    error        TEXT
);
ALTER TABLE spans ADD COLUMN IF NOT EXISTS parent_name TEXT;
CREATE INDEX IF NOT EXISTS idx_spans_trace   ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_started ON spans(started_at DESC);

CREATE TABLE IF NOT EXISTS user_signals (
    signal_id    TEXT PRIMARY KEY,
    trace_id     TEXT NOT NULL,
    kind         TEXT,
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_signals_trace   ON user_signals(trace_id);
CREATE INDEX IF NOT EXISTS idx_user_signals_created ON user_signals(created_at DESC);
"""


class PostgresStorage:
    """Postgres-backed durable storage.

    Single connection pool per instance. Thread-safe.

    Install: `pip install "psycopg[binary,pool]"`.
    """

    def __init__(self, dsn: str, *, min_pool: int = 1, max_pool: int = 8) -> None:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as e:
            raise ImportError(
                'PostgresStorage requires `pip install "psycopg[binary,pool]"`'
            ) from e
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_pool,
            max_size=max_pool,
            kwargs={"autocommit": True},
            open=True,
        )
        self._lock = threading.Lock()
        self._cluster_snapshot_connection: ContextVar[object | None] = ContextVar(
            "verdict_cluster_snapshot_connection", default=None
        )
        # Initialize schema
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA)
                # Idempotent migration for databases created before the May-2026
                # effect-size refresh. Postgres supports ADD COLUMN IF NOT EXISTS,
                # so this is safe to run on every startup.
                for col, ddl in [
                    ("effect_size_cliffs_delta", "DOUBLE PRECISION DEFAULT 0.0"),
                    ("wasserstein_distance", "DOUBLE PRECISION DEFAULT 0.0"),
                    ("psi", "DOUBLE PRECISION DEFAULT 0.0"),
                    ("evaluator_fingerprint", "TEXT"),
                    ("run_id", "TEXT"),
                ]:
                    cur.execute(f"ALTER TABLE drift_signals ADD COLUMN IF NOT EXISTS {col} {ddl}")
                for col, ddl in [
                    ("evaluator_provider", "TEXT"),
                    ("evaluator_config", "JSONB"),
                    ("evaluator_fingerprint", "TEXT"),
                    ("expected_dimensions", "JSONB"),
                    ("status", "TEXT DEFAULT 'completed'"),
                    ("error", "TEXT"),
                ]:
                    cur.execute(f"ALTER TABLE judgments ADD COLUMN IF NOT EXISTS {col} {ddl}")
                for col, ddl in [
                    ("correct_examples", "INTEGER NOT NULL DEFAULT 0"),
                    ("total_examples", "INTEGER NOT NULL DEFAULT 0"),
                    ("example_agreement", "DOUBLE PRECISION"),
                    ("example_confidence_low", "DOUBLE PRECISION"),
                    ("example_confidence_high", "DOUBLE PRECISION"),
                    ("label_agreement", "DOUBLE PRECISION"),
                    ("method_version", "TEXT NOT NULL DEFAULT '1'"),
                ]:
                    cur.execute(
                        f"ALTER TABLE evaluator_health ADD COLUMN IF NOT EXISTS {col} {ddl}"
                    )
                cur.execute("ALTER TABLE traces ADD COLUMN IF NOT EXISTS parent_span_id TEXT")
                for col, ddl in [
                    ("analysis_started_at_us", "BIGINT"),
                    ("analysis_started_at_state", "TEXT NOT NULL DEFAULT 'pending'"),
                    ("analysis_raw_messages_utf8_bytes", "BIGINT"),
                    ("analysis_raw_messages_state", "TEXT NOT NULL DEFAULT 'pending'"),
                ]:
                    cur.execute(f"ALTER TABLE traces ADD COLUMN IF NOT EXISTS {col} {ddl}")
                cur.execute("""CREATE INDEX IF NOT EXISTS idx_traces_tenant_started_completed_v2
                    ON traces(tenant_id,analysis_started_at_us,trace_id)
                    WHERE ended_at IS NOT NULL AND analysis_started_at_state='valid'""")
                cur.execute("""CREATE INDEX IF NOT EXISTS
                    idx_traces_tenant_workload_started_completed_v2 ON traces(
                    tenant_id,(CASE WHEN jsonb_typeof(tags->'verdict.workload')='string'
                    AND octet_length(tags->>'verdict.workload') BETWEEN 1 AND 64
                    THEN tags->>'verdict.workload' END),analysis_started_at_us,trace_id)
                    WHERE ended_at IS NOT NULL AND analysis_started_at_state='valid'""")
                cur.execute("""CREATE INDEX IF NOT EXISTS idx_traces_tenant_analysis_pending_v2
                    ON traces(tenant_id,trace_id) WHERE analysis_started_at_state='pending'
                    OR analysis_raw_messages_state='pending'""")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_traces_parent_span ON traces(parent_span_id)"
                )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _iso(dt: datetime | None) -> datetime | None:
        return dt  # psycopg handles datetime natively

    def _exec(self, sql: str, params: tuple) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    def _fetchone(self, sql: str, params: tuple):
        if (conn := self._cluster_snapshot_connection.get()) is not None:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def _fetchall(self, sql: str, params: tuple):
        if (conn := self._cluster_snapshot_connection.get()) is not None:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    # -- Traces -----------------------------------------------------------

    _TRACE_COLUMNS = """trace_id, parent_span_id, started_at, ended_at,
        provider, operation, request_model, response_model, input_tokens,
        output_tokens, temperature, max_tokens, finish_reason, error,
        latency_ms, prompt_redacted, response_redacted, raw_messages,
        tenant_id, session_id, user_id_hash, cluster_id, tags, cost_usd,
        analysis_started_at_us, analysis_started_at_state,
        analysis_raw_messages_utf8_bytes, analysis_raw_messages_state"""

    def insert_trace(self, trace: Trace) -> None:
        sanitize_trace(trace)
        populate_trace_analysis_fields(trace)
        # Only the fixed, class-owned column list is interpolated; every trace
        # value remains a driver-bound parameter.
        sql = (
            f"INSERT INTO traces ({self._TRACE_COLUMNS}) VALUES ("  # nosec B608
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,"
            "%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s) "
            "ON CONFLICT (trace_id) DO UPDATE SET "
            "ended_at = EXCLUDED.ended_at, "
            "response_model = EXCLUDED.response_model, "
            "input_tokens = EXCLUDED.input_tokens, "
            "output_tokens = EXCLUDED.output_tokens, "
            "finish_reason = EXCLUDED.finish_reason, "
            "error = EXCLUDED.error, "
            "latency_ms = EXCLUDED.latency_ms, "
            "prompt_redacted = EXCLUDED.prompt_redacted, "
            "response_redacted = EXCLUDED.response_redacted, "
            "parent_span_id = COALESCE(EXCLUDED.parent_span_id, traces.parent_span_id), "
            "cluster_id = COALESCE(EXCLUDED.cluster_id, traces.cluster_id), "
            "cost_usd = EXCLUDED.cost_usd"
        )
        self._exec(
            sql,
            (
                trace.trace_id,
                trace.parent_span_id,
                trace.started_at,
                trace.ended_at,
                trace.provider,
                trace.operation.value,
                trace.request_model,
                trace.response_model,
                trace.input_tokens,
                trace.output_tokens,
                trace.temperature,
                trace.max_tokens,
                trace.finish_reason,
                trace.error,
                trace.latency_ms,
                trace.prompt_redacted,
                trace.response_redacted,
                json.dumps(trace.raw_messages) if trace.raw_messages else None,
                trace.tenant_id,
                trace.session_id,
                trace.user_id_hash,
                trace.cluster_id,
                json.dumps(trace.tags),
                trace.cost_usd,
                trace.analysis_started_at_us,
                trace.analysis_started_at_state,
                trace.analysis_raw_messages_utf8_bytes,
                trace.analysis_raw_messages_state,
            ),
        )

    def _row_to_trace(self, row) -> Trace:
        return Trace(
            trace_id=row[0],
            parent_span_id=row[1],
            started_at=row[2],
            ended_at=row[3],
            provider=row[4] or "",
            operation=Operation(row[5] or "chat"),
            request_model=row[6] or "",
            response_model=row[7] or "",
            input_tokens=row[8],
            output_tokens=row[9],
            temperature=row[10],
            max_tokens=row[11],
            finish_reason=row[12],
            error=row[13],
            latency_ms=row[14],
            prompt_redacted=row[15],
            response_redacted=row[16],
            raw_messages=(
                row[17] if isinstance(row[17], list) else json.loads(row[17]) if row[17] else None
            ),
            tenant_id=row[18],
            session_id=row[19],
            user_id_hash=row[20],
            cluster_id=row[21],
            tags=(row[22] if isinstance(row[22], dict) else json.loads(row[22]) if row[22] else {}),
            cost_usd=row[23],
            analysis_started_at_us=row[24],
            analysis_started_at_state=row[25],
            analysis_raw_messages_utf8_bytes=row[26],
            analysis_raw_messages_state=row[27],
        )

    def replace_agent_run_bundle(self, bundle: AgentRunBundle) -> None:
        sanitized = sanitize_agent_run_bundle(bundle)
        payload = agent_run_bundle_to_json(sanitized)
        self._exec(
            """INSERT INTO agent_run_bundles (
                tenant_id, run_id, source_session_id, source_kind,
                started_at, ended_at, status, content_hash, payload_json, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (tenant_id, run_id) DO UPDATE SET
                source_session_id=EXCLUDED.source_session_id,
                source_kind=EXCLUDED.source_kind,
                started_at=EXCLUDED.started_at,
                ended_at=EXCLUDED.ended_at,
                status=EXCLUDED.status,
                content_hash=EXCLUDED.content_hash,
                payload_json=EXCLUDED.payload_json,
                updated_at=now()""",
            (
                sanitized.run.tenant_id,
                sanitized.run.run_id,
                sanitized.session.source_session_id,
                sanitized.session.source_kind,
                sanitized.run.started_at,
                sanitized.run.ended_at,
                sanitized.run.status.value,
                sanitized.content_hash,
                payload,
            ),
        )

    @staticmethod
    def _row_to_agent_run_bundle(row) -> AgentRunBundle:
        payload = (
            json.dumps(row[0], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if isinstance(row[0], dict)
            else row[0]
        )
        bundle = agent_run_bundle_from_json(payload)
        if bundle.content_hash != row[1]:
            raise RuntimeError("stored agent run bundle content hash is inconsistent")
        return bundle

    def get_agent_run_bundle(
        self,
        tenant_id: str,
        run_id: str,
    ) -> AgentRunBundle | None:
        _validate_agent_bundle_query(tenant_id, 1)
        _validate_agent_bundle_run_id(run_id)
        row = self._fetchone(
            """SELECT payload_json, content_hash FROM agent_run_bundles
               WHERE tenant_id=%s AND run_id=%s""",
            (tenant_id, run_id),
        )
        return self._row_to_agent_run_bundle(row) if row is not None else None

    def list_agent_run_bundles(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> list[AgentRunBundle]:
        _validate_agent_bundle_query(tenant_id, limit)
        rows = self._fetchall(
            """SELECT payload_json, content_hash FROM agent_run_bundles
               WHERE tenant_id=%s ORDER BY started_at DESC, run_id DESC LIMIT %s""",
            (tenant_id, limit),
        )
        return [self._row_to_agent_run_bundle(row) for row in rows]

    def has_agent_run_source_kind(self, tenant_id: str, source_kind: str) -> bool:
        _validate_agent_bundle_query(tenant_id, 1)
        if not isinstance(source_kind, str) or not source_kind or len(source_kind) > 64:
            raise ValueError("invalid source kind")
        return self._fetchone(
            """SELECT 1 FROM agent_run_bundles WHERE tenant_id=%s
               AND (payload_json::jsonb)->'session'->>'source_kind'=%s LIMIT 1""",
            (tenant_id, source_kind),
        ) is not None

    def save_deterministic_analysis_run(self, run: DeterministicAnalysisRun) -> None:
        payload = analysis_run_to_json(run)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """INSERT INTO deterministic_analysis_runs (
                       analysis_id,tenant_id,scope_key,cutoff,completed_at,status,
                       analyzer_version,input_fingerprint,payload_json
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (
                    run.analysis_id, run.tenant_id, run.scope_key, run.cutoff,
                    run.completed_at, run.status.value, run.analyzer_version,
                    run.input_fingerprint, payload,
                ),
            )
            cur.execute(
                "SELECT payload_json FROM deterministic_analysis_runs WHERE analysis_id=%s",
                (run.analysis_id,),
            )
            by_id = cur.fetchone()
            if by_id is not None:
                if by_id[0] != payload:
                    raise ValueError("analysis identity has different content")
                return
            cur.execute(
                """SELECT payload_json FROM deterministic_analysis_runs
                   WHERE tenant_id=%s AND scope_key=%s AND analyzer_version=%s
                     AND input_fingerprint=%s AND status=%s""",
                (
                    run.tenant_id, run.scope_key, run.analyzer_version,
                    run.input_fingerprint, run.status.value,
                ),
            )
            prior = cur.fetchone()
            if prior is None:
                raise RuntimeError("analysis insert conflict could not be resolved")
            previous = analysis_run_from_json(prior[0])
            if previous.result != run.result or previous.cutoff != run.cutoff:
                raise ValueError("analysis input produced different content")

    def get_latest_deterministic_analysis_run(
        self, tenant_id: str, scope_key: str,
    ) -> DeterministicAnalysisRun | None:
        row = self._fetchone(
            """SELECT payload_json FROM deterministic_analysis_runs
               WHERE tenant_id=%s AND scope_key=%s
               ORDER BY completed_at DESC, analysis_id DESC LIMIT 1""",
            (tenant_id, scope_key),
        )
        return analysis_run_from_json(row[0]) if row is not None else None

    def save_notification_delivery_attempt(
        self, attempt: NotificationDeliveryAttempt,
    ) -> None:
        payload = notification_attempt_to_json(attempt)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """INSERT INTO notification_delivery_attempts (
                       attempt_id,notification_id,tenant_id,destination_fingerprint,
                       attempted_at,outcome,payload_json
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    attempt.attempt_id, attempt.notification_id, attempt.tenant_id,
                    attempt.destination_fingerprint, attempt.attempted_at,
                    attempt.outcome.value, payload,
                ),
            )
            cur.execute(
                "SELECT payload_json FROM notification_delivery_attempts WHERE attempt_id=%s",
                (attempt.attempt_id,),
            )
            stored = cur.fetchone()
            if stored is None:
                raise RuntimeError("notification attempt insert could not be resolved")
            if stored[0] != payload:
                raise ValueError("notification attempt identity has different content")

    def list_notification_delivery_attempts(
        self,
        notification_id: str,
        destination_fingerprint: str,
        *,
        limit: int = 100,
    ) -> list[NotificationDeliveryAttempt]:
        validate_delivery_query(notification_id, destination_fingerprint, limit)
        rows = self._fetchall(
            """SELECT payload_json FROM notification_delivery_attempts
               WHERE notification_id=%s AND destination_fingerprint=%s
               ORDER BY attempted_at DESC, attempt_id DESC LIMIT %s""",
            (notification_id, destination_fingerprint, limit),
        )
        return [notification_attempt_from_json(row[0]) for row in rows]

    def notification_was_delivered(
        self, notification_id: str, destination_fingerprint: str,
    ) -> bool:
        validate_delivery_query(notification_id, destination_fingerprint, 1)
        row = self._fetchone(
            """SELECT 1 FROM notification_delivery_attempts
               WHERE notification_id=%s AND destination_fingerprint=%s
                 AND outcome=%s LIMIT 1""",
            (notification_id, destination_fingerprint, DeliveryOutcome.DELIVERED.value),
        )
        return row is not None

    def list_notification_delivery_attempts_for_tenant(
        self, tenant_id: str, *, limit: int = 100,
    ) -> list[NotificationDeliveryAttempt]:
        _validate_agent_bundle_query(tenant_id, limit)
        rows = self._fetchall(
            """SELECT payload_json FROM notification_delivery_attempts
               WHERE tenant_id=%s ORDER BY attempted_at DESC, attempt_id DESC LIMIT %s""",
            (tenant_id, limit),
        )
        return [notification_attempt_from_json(row[0]) for row in rows]

    def save_monitor_policy(self, policy: MonitorPolicy) -> None:
        payload = monitor_policy_to_json(policy)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO monitor_policies "
                "(policy_id,scope_key,state,content_hash,payload_json) "
                "VALUES (%s,%s,'candidate',%s,%s) ON CONFLICT DO NOTHING",
                (policy.policy_id, policy.scope_key, digest, payload),
            )
            cur.execute(
                "SELECT content_hash FROM monitor_policies WHERE policy_id=%s",
                (policy.policy_id,),
            )
            if cur.fetchone()[0] != digest:
                raise ValueError("monitor policy identity has a different definition")

    @staticmethod
    def _monitor_policy_payload(value) -> str:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"))
                if isinstance(value, dict) else value)

    def get_monitor_policy(self, policy_id: str) -> tuple[MonitorPolicy, str] | None:
        row = self._fetchone(
            "SELECT payload_json,state FROM monitor_policies WHERE policy_id=%s", (policy_id,),
        )
        return (monitor_policy_from_json(self._monitor_policy_payload(row[0])), row[1]) if row else None

    def get_active_monitor_policy(self, scope_key: str) -> MonitorPolicy | None:
        row = self._fetchone(
            "SELECT payload_json FROM monitor_policies WHERE scope_key=%s AND state='active'",
            (scope_key,),
        )
        return monitor_policy_from_json(self._monitor_policy_payload(row[0])) if row else None

    def activate_monitor_policy(
        self, scope_key: str, policy_id: str, *, expected_active_policy_id: str | None
    ) -> MonitorPolicy:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute("LOCK TABLE monitor_policies IN SHARE ROW EXCLUSIVE MODE")
            cur.execute(
                "SELECT policy_id FROM monitor_policies "
                "WHERE scope_key=%s AND state='active'", (scope_key,),
            )
            row = cur.fetchone()
            current_id = row[0] if row else None
            if current_id != expected_active_policy_id:
                raise ValueError("active policy changed")
            cur.execute(
                "SELECT payload_json FROM monitor_policies WHERE scope_key=%s AND policy_id=%s",
                (scope_key, policy_id),
            )
            target = cur.fetchone()
            if target is None:
                raise ValueError("unknown monitor policy")
            cur.execute(
                "UPDATE monitor_policies SET state='retired',updated_at=now() "
                "WHERE scope_key=%s AND state='active'", (scope_key,),
            )
            cur.execute(
                "UPDATE monitor_policies SET state='active',updated_at=now() "
                "WHERE policy_id=%s", (policy_id,),
            )
        return monitor_policy_from_json(self._monitor_policy_payload(target[0]))

    def save_monitor_snapshot(
        self, policy_id: str, manifest: CohortManifest, comparison: MonitorComparison
    ) -> None:
        payload = monitor_snapshot_to_json(manifest, comparison)
        if not 2 <= len(payload.encode("utf-8")) <= 4_194_304:
            raise ValueError("monitor snapshot exceeds the 4 MiB storage contract")
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT payload_json FROM monitor_policies WHERE policy_id=%s", (policy_id,),
            )
            policy_row = cur.fetchone()
            if policy_row is None:
                raise ValueError("unknown policy")
            stored_policy = monitor_policy_from_json(
                self._monitor_policy_payload(policy_row[0])
            )
            if stored_policy.fingerprint != manifest.policy_fingerprint:
                raise ValueError("monitor snapshot does not match policy")
            cur.execute(
                "INSERT INTO monitor_snapshots "
                "(snapshot_id,policy_id,cutoff,content_hash,payload_json) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (manifest.snapshot_id, policy_id, manifest.cutoff, digest, payload),
            )
            cur.execute(
                "SELECT content_hash FROM monitor_snapshots WHERE snapshot_id=%s",
                (manifest.snapshot_id,),
            )
            if cur.fetchone()[0] != digest:
                raise ValueError("monitor snapshot identity has different content")

    def get_latest_monitor_snapshot(
        self, policy_id: str
    ) -> tuple[CohortManifest, MonitorComparison] | None:
        row = self._fetchone(
            "SELECT payload_json FROM monitor_snapshots WHERE policy_id=%s "
            "ORDER BY created_at DESC,snapshot_id DESC LIMIT 1", (policy_id,),
        )
        return monitor_snapshot_from_json(self._monitor_policy_payload(row[0])) if row else None

    def get_initial_monitor_snapshot(
        self, policy_id: str
    ) -> tuple[CohortManifest, MonitorComparison] | None:
        row = self._fetchone(
            "SELECT payload_json FROM monitor_snapshots WHERE policy_id=%s "
            "ORDER BY created_at ASC,snapshot_id ASC LIMIT 1", (policy_id,),
        )
        return monitor_snapshot_from_json(self._monitor_policy_payload(row[0])) if row else None

    def get_latest_monitor_alert(
        self, policy_id: str
    ) -> tuple[CohortManifest, MonitorComparison] | None:
        row = self._fetchone(
            "SELECT payload_json FROM monitor_snapshots WHERE policy_id=%s "
            "AND payload_json LIKE '%%\"status\":\"alert\"%%' "
            "ORDER BY created_at DESC,snapshot_id DESC LIMIT 1",
            (policy_id,),
        )
        return monitor_snapshot_from_json(self._monitor_policy_payload(row[0])) if row else None

    def get_trace(self, trace_id: str) -> Trace | None:
        row = self._fetchone(
            # Fixed column list; trace_id remains parameterized.
            f"SELECT {self._TRACE_COLUMNS} FROM traces WHERE trace_id = %s",  # nosec B608
            (trace_id,),
        )
        return self._row_to_trace(row) if row else None

    def trace_exists(self, trace_id: str) -> bool:
        return (
            self._fetchone(
                "SELECT 1 FROM traces WHERE trace_id = %s LIMIT 1",
                (trace_id,),
            )
            is not None
        )

    def list_traces(
        self,
        *,
        tenant_id: str | None = None,
        cluster_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        clauses, params = [], []
        if tenant_id is not None:
            clauses.append(_trace_tenant_clause(tenant_id))
            params.append(tenant_id)
        if cluster_id is not None:
            clauses.append("cluster_id = %s")
            params.append(cluster_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self._fetchall(
            f"SELECT {self._TRACE_COLUMNS} FROM traces {where} ORDER BY started_at DESC LIMIT %s",
            tuple(params),
        )
        return [self._row_to_trace(r) for r in rows]

    def delete_trace(self, trace_id: str) -> None:
        # judgments cascade via ON DELETE CASCADE, but spans/user_signals do
        # not declare an FK, so delete them explicitly for parity.
        with self._lock, self._pool.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                # Cleanup decides whether spans are still referenced by retained
                # traces. Serialize trace writers until that decision and all
                # related deletes commit so a concurrent insert cannot interleave.
                cur.execute("LOCK TABLE traces IN SHARE ROW EXCLUSIVE MODE")
                cur.execute("DELETE FROM user_signals WHERE trace_id = %s", (trace_id,))
                cur.execute("DELETE FROM judgments WHERE trace_id = %s", (trace_id,))
                cur.execute("DELETE FROM traces WHERE trace_id = %s", (trace_id,))
                cur.execute(
                    """DELETE FROM spans AS candidate
                       WHERE candidate.trace_id = %s
                         AND NOT EXISTS (
                           SELECT 1 FROM traces
                           WHERE traces.parent_span_id = candidate.span_id
                         )""",
                    (trace_id,),
                )
                cur.execute(
                    """UPDATE spans AS candidate SET trace_id = NULL
                       WHERE candidate.trace_id = %s
                         AND EXISTS (
                           SELECT 1 FROM traces
                           WHERE traces.parent_span_id = candidate.span_id
                         )""",
                    (trace_id,),
                )

    def prune_before(self, cutoff_iso: str) -> int:
        with self._lock, self._pool.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                # See delete_trace(): this makes the multi-table cleanup one
                # linearizable maintenance operation across storage instances.
                cur.execute("LOCK TABLE traces IN SHARE ROW EXCLUSIVE MODE")
                cur.execute(
                    "SELECT trace_id FROM traces WHERE started_at < %s",
                    (cutoff_iso,),
                )
                ids = [row[0] for row in cur.fetchall()]
                if ids:
                    cur.execute("DELETE FROM user_signals WHERE trace_id = ANY(%s)", (ids,))
                    cur.execute("DELETE FROM judgments WHERE trace_id = ANY(%s)", (ids,))
                    cur.execute("DELETE FROM traces WHERE trace_id = ANY(%s)", (ids,))
                    cur.execute(
                        """DELETE FROM spans AS candidate
                           WHERE candidate.trace_id = ANY(%s)
                             AND NOT EXISTS (
                               SELECT 1 FROM traces
                               WHERE traces.parent_span_id = candidate.span_id
                             )""",
                        (ids,),
                    )
                    cur.execute(
                        """UPDATE spans AS candidate SET trace_id = NULL
                           WHERE candidate.trace_id = ANY(%s)
                             AND EXISTS (
                               SELECT 1 FROM traces
                               WHERE traces.parent_span_id = candidate.span_id
                             )""",
                        (ids,),
                    )
                cur.execute(
                    """DELETE FROM spans AS candidate
                       WHERE candidate.started_at < %s
                         AND (
                           candidate.trace_id IS NULL
                           OR NOT EXISTS (
                             SELECT 1 FROM traces
                             WHERE traces.trace_id = candidate.trace_id
                           )
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM traces
                           WHERE traces.parent_span_id = candidate.span_id
                         )""",
                    (cutoff_iso,),
                )
        return len(ids)

    # -- Judgments --------------------------------------------------------

    _JUDGMENT_COLUMNS = (
        "j.judgment_id, j.trace_id, j.rubric_name, j.rubric_version, "
        "j.created_at, j.judge_models, j.dimensions, "
        "j.position_swap_consistent, j.evaluator_provider, "
        "j.evaluator_config, j.evaluator_fingerprint, "
        "j.expected_dimensions, j.status, j.error"
    )

    def insert_judgment(self, judgment: Judgment) -> None:
        sanitize_judgment(judgment)
        self._exec(
            """INSERT INTO judgments (
                judgment_id, trace_id, rubric_name, rubric_version, created_at,
                judge_models, dimensions, position_swap_consistent,
                evaluator_provider, evaluator_config, evaluator_fingerprint,
                expected_dimensions, status, error
            ) VALUES (
                %s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s
            ) ON CONFLICT (judgment_id) DO UPDATE SET
                trace_id = EXCLUDED.trace_id,
                rubric_name = EXCLUDED.rubric_name,
                rubric_version = EXCLUDED.rubric_version,
                created_at = EXCLUDED.created_at,
                judge_models = EXCLUDED.judge_models,
                dimensions = EXCLUDED.dimensions,
                position_swap_consistent = EXCLUDED.position_swap_consistent,
                evaluator_provider = EXCLUDED.evaluator_provider,
                evaluator_config = EXCLUDED.evaluator_config,
                evaluator_fingerprint = EXCLUDED.evaluator_fingerprint,
                expected_dimensions = EXCLUDED.expected_dimensions,
                status = EXCLUDED.status,
                error = EXCLUDED.error""",
            (
                judgment.judgment_id,
                judgment.trace_id,
                judgment.rubric_name,
                judgment.rubric_version,
                judgment.created_at,
                json.dumps(judgment.judge_models),
                json.dumps([asdict(d) for d in judgment.dimensions]),
                judgment.position_swap_consistent,
                judgment.evaluator_provider,
                json.dumps(judgment.evaluator_config, sort_keys=True),
                judgment.evaluator_fingerprint,
                json.dumps(judgment.expected_dimensions),
                judgment.status.value,
                judgment.error,
            ),
        )

    def _row_to_judgment(self, row) -> Judgment:
        dims_raw = row[6] if isinstance(row[6], list) else (json.loads(row[6]) if row[6] else [])
        dims = [
            DimensionScore(
                name=d["name"],
                verdict=Verdict(d["verdict"]),
                reasoning=d.get("reasoning", ""),
                judge_model=d.get("judge_model", ""),
            )
            for d in dims_raw
        ]
        return Judgment(
            judgment_id=row[0],
            trace_id=row[1],
            rubric_name=row[2] or "default",
            rubric_version=row[3] or "1",
            created_at=row[4],
            judge_models=row[5]
            if isinstance(row[5], list)
            else (json.loads(row[5]) if row[5] else []),
            dimensions=dims,
            evaluator_provider=row[8] or "",
            evaluator_config=(
                row[9] if isinstance(row[9], dict) else (json.loads(row[9]) if row[9] else {})
            ),
            evaluator_fingerprint=row[10] or "",
            expected_dimensions=(
                row[11] if isinstance(row[11], list) else (json.loads(row[11]) if row[11] else [])
            ),
            status=JudgmentStatus(row[12] or "completed"),
            error=row[13],
            position_swap_consistent=row[7],
        )

    def list_judgments_for_cluster(
        self,
        cluster_id: str,
        *,
        since_iso: str | None = None,
        limit: int = 1000,
    ) -> list[Judgment]:
        params: list = [cluster_id]
        clause = ""
        if since_iso is not None:
            clause = " AND j.created_at >= %s"
            params.append(since_iso)
        params.append(limit)
        rows = self._fetchall(
            f"""SELECT {self._JUDGMENT_COLUMNS} FROM judgments j
                JOIN traces t ON j.trace_id = t.trace_id
                WHERE t.cluster_id = %s{clause}
                ORDER BY j.created_at DESC LIMIT %s""",
            tuple(params),
        )
        return [self._row_to_judgment(r) for r in rows]

    def list_judgments_for_trace(
        self, trace_id: str, *, limit: int = 100,
    ) -> list[Judgment]:
        if not isinstance(trace_id, str) or not trace_id or not 1 <= limit <= 10_000:
            raise ValueError("invalid trace judgment query")
        rows = self._fetchall(
            f"SELECT {self._JUDGMENT_COLUMNS} FROM judgments j "  # nosec B608
            "WHERE j.trace_id=%s ORDER BY j.created_at DESC, j.judgment_id DESC LIMIT %s",
            (trace_id, limit),
        )
        return [self._row_to_judgment(row) for row in rows]

    def list_latest_judgments_for_evaluator(
        self,
        tenant_id: str,
        evaluator_fingerprint: str,
        *,
        limit: int = 100_000,
    ) -> list[Judgment]:
        _validate_evaluator_judgment_query(tenant_id, evaluator_fingerprint, limit)
        rows = self._fetchall(
            f"""SELECT * FROM (
                    SELECT DISTINCT ON (j.trace_id) {self._JUDGMENT_COLUMNS}
                    FROM judgments j JOIN traces t ON t.trace_id=j.trace_id
                    WHERE t.tenant_id=%s AND j.evaluator_fingerprint=%s
                    ORDER BY j.trace_id,j.created_at DESC,j.judgment_id DESC
                ) latest
                ORDER BY created_at DESC,judgment_id DESC LIMIT %s""",  # nosec B608
            (tenant_id, evaluator_fingerprint, limit),
        )
        return [self._row_to_judgment(row) for row in rows]

    def has_completed_judgment(
        self, trace_id: str, evaluator_fingerprint: str,
    ) -> bool:
        return self._fetchone(
            "SELECT 1 FROM judgments WHERE trace_id=%s "
            "AND evaluator_fingerprint=%s AND status=%s LIMIT 1",
            (trace_id, evaluator_fingerprint, JudgmentStatus.COMPLETED.value),
        ) is not None

    # -- Evaluator health ------------------------------------------------

    def insert_evaluator_health(self, record: EvaluatorHealthRecord) -> None:
        self._exec(
            """INSERT INTO evaluator_health (
                health_id, evaluated_at, evaluator_fingerprint,
                sentinel_set_name, sentinel_set_fingerprint,
                correct_examples, total_examples, example_agreement,
                example_confidence_low, example_confidence_high,
                correct_labels, total_labels, label_agreement,
                status, error_count, method_version
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (health_id) DO UPDATE SET
                evaluated_at = EXCLUDED.evaluated_at,
                evaluator_fingerprint = EXCLUDED.evaluator_fingerprint,
                sentinel_set_name = EXCLUDED.sentinel_set_name,
                sentinel_set_fingerprint = EXCLUDED.sentinel_set_fingerprint,
                correct_examples = EXCLUDED.correct_examples,
                total_examples = EXCLUDED.total_examples,
                example_agreement = EXCLUDED.example_agreement,
                example_confidence_low = EXCLUDED.example_confidence_low,
                example_confidence_high = EXCLUDED.example_confidence_high,
                correct_labels = EXCLUDED.correct_labels,
                total_labels = EXCLUDED.total_labels,
                label_agreement = EXCLUDED.label_agreement,
                status = EXCLUDED.status,
                error_count = EXCLUDED.error_count,
                method_version = EXCLUDED.method_version""",
            (
                record.health_id,
                record.evaluated_at,
                record.evaluator_fingerprint,
                record.sentinel_set_name,
                record.sentinel_set_fingerprint,
                record.correct_examples,
                record.total_examples,
                record.example_agreement,
                record.example_confidence_low,
                record.example_confidence_high,
                record.correct_labels,
                record.total_labels,
                record.label_agreement,
                record.status.value,
                record.error_count,
                record.method_version,
            ),
        )

    def list_evaluator_health(
        self,
        *,
        evaluator_fingerprint: str | None = None,
        limit: int = 100,
    ) -> list[EvaluatorHealthRecord]:
        where = ""
        params: list[object] = []
        if evaluator_fingerprint is not None:
            where = "WHERE evaluator_fingerprint = %s"
            params.append(evaluator_fingerprint)
        params.append(limit)
        # `where` is one of two fixed strings; fingerprint and limit are bound.
        sql = (
            "SELECT health_id, evaluated_at, evaluator_fingerprint, "
            "sentinel_set_name, sentinel_set_fingerprint, correct_examples, "
            "total_examples, example_agreement, example_confidence_low, "
            "example_confidence_high, correct_labels, total_labels, "
            "label_agreement, status, error_count, method_version "
            f"FROM evaluator_health {where} "  # nosec B608
            "ORDER BY evaluated_at DESC LIMIT %s"
        )
        rows = self._fetchall(
            sql,
            tuple(params),
        )
        records = []
        for row in rows:
            legacy = (row[15] or "1") == "1"
            records.append(
                EvaluatorHealthRecord(
                    health_id=row[0],
                    evaluated_at=row[1],
                    evaluator_fingerprint=row[2],
                    sentinel_set_name=row[3] or "",
                    sentinel_set_fingerprint=row[4],
                    correct_examples=0 if legacy else row[5],
                    total_examples=0 if legacy else row[6],
                    example_agreement=None if legacy else row[7],
                    example_confidence_low=None if legacy else row[8],
                    example_confidence_high=None if legacy else row[9],
                    correct_labels=row[10],
                    total_labels=row[11],
                    label_agreement=None if legacy else row[12],
                    status=(
                        EvaluatorHealthStatus.INSUFFICIENT_DATA
                        if legacy
                        else EvaluatorHealthStatus(row[13])
                    ),
                    error_count=row[14],
                    method_version=row[15] or "1",
                )
            )
        return records

    # -- Drift signals ----------------------------------------------------

    # Explicit column list keeps the INSERT and SELECT in lockstep so a future
    # schema change can't silently drop a field (the bug that lost Cliff's δ,
    # Wasserstein, and PSI on Postgres before the May-2026 fix).
    _SIGNAL_COLUMNS = (
        "signal_id, detected_at, cluster_id, dimension, direction, "
        "evaluator_fingerprint, run_id, "
        "statistic_name, statistic_value, p_value, p_value_adjusted, "
        "effect_size_cohens_d, effect_size_cliffs_delta, wasserstein_distance, psi, "
        "sample_size_current, sample_size_baseline, "
        "contributing_layers, example_trace_ids, recommended_action"
    )

    @staticmethod
    def _signal_params(signal: DriftSignal) -> tuple:
        return (
            signal.signal_id,
            signal.detected_at,
            signal.cluster_id,
            signal.dimension,
            signal.direction.value,
            signal.evaluator_fingerprint,
            signal.run_id or None,
            signal.statistic_name,
            signal.statistic_value,
            signal.p_value,
            signal.p_value_adjusted,
            signal.effect_size_cohens_d,
            signal.effect_size_cliffs_delta,
            signal.wasserstein_distance,
            signal.psi,
            signal.sample_size_current,
            signal.sample_size_baseline,
            json.dumps(signal.contributing_layers),
            json.dumps(signal.example_trace_ids),
            signal.recommended_action,
        )

    def _insert_drift_signal_cursor(self, cur, signal: DriftSignal) -> None:
        cur.execute(
            f"""INSERT INTO drift_signals ({self._SIGNAL_COLUMNS}) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s
            ) ON CONFLICT (signal_id) DO UPDATE SET
                detected_at = EXCLUDED.detected_at,
                cluster_id = EXCLUDED.cluster_id,
                dimension = EXCLUDED.dimension,
                direction = EXCLUDED.direction,
                evaluator_fingerprint = EXCLUDED.evaluator_fingerprint,
                run_id = EXCLUDED.run_id,
                statistic_name = EXCLUDED.statistic_name,
                statistic_value = EXCLUDED.statistic_value,
                p_value = EXCLUDED.p_value,
                p_value_adjusted = EXCLUDED.p_value_adjusted,
                effect_size_cohens_d = EXCLUDED.effect_size_cohens_d,
                effect_size_cliffs_delta = EXCLUDED.effect_size_cliffs_delta,
                wasserstein_distance = EXCLUDED.wasserstein_distance,
                psi = EXCLUDED.psi,
                sample_size_current = EXCLUDED.sample_size_current,
                sample_size_baseline = EXCLUDED.sample_size_baseline,
                contributing_layers = EXCLUDED.contributing_layers,
                example_trace_ids = EXCLUDED.example_trace_ids,
                recommended_action = EXCLUDED.recommended_action""",
            self._signal_params(signal),
        )

    def insert_drift_signal(self, signal: DriftSignal) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                self._insert_drift_signal_cursor(cur, signal)

    def replace_drift_run(
        self,
        run: DriftRun,
        signals: list[DriftSignal],
    ) -> None:
        _validate_drift_run_snapshot(run, signals)
        with self._pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    # Advisory locks close the absent-row race: two transactions
                    # must not both claim the same signal_id for different runs.
                    lock_names = {
                        f"verdict:drift-run:{run.run_id}",
                        *(f"verdict:drift-signal:{signal.signal_id}" for signal in signals),
                    }
                    for lock_name in sorted(lock_names):
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (lock_name,),
                        )
                    cur.execute(
                        "SELECT evaluator_fingerprint FROM drift_runs WHERE run_id = %s",
                        (run.run_id,),
                    )
                    existing = cur.fetchone()
                    if existing and existing[0] != run.evaluator_fingerprint:
                        raise ValueError("run_id already belongs to another evaluator")
                    for signal in signals:
                        cur.execute(
                            "SELECT run_id FROM drift_signals WHERE signal_id = %s",
                            (signal.signal_id,),
                        )
                        owner = cur.fetchone()
                        if owner and owner[0] and owner[0] != run.run_id:
                            raise ValueError("signal_id already belongs to another drift run")
                    cur.execute(
                        """INSERT INTO drift_runs (
                            run_id, analysis_time, completed_at,
                            evaluator_fingerprint, signal_count
                        ) VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT (run_id) DO UPDATE SET
                            analysis_time = EXCLUDED.analysis_time,
                            completed_at = EXCLUDED.completed_at,
                            evaluator_fingerprint = EXCLUDED.evaluator_fingerprint,
                            signal_count = EXCLUDED.signal_count""",
                        (
                            run.run_id,
                            run.analysis_time,
                            run.completed_at,
                            run.evaluator_fingerprint,
                            run.signal_count,
                        ),
                    )
                    cur.execute(
                        "DELETE FROM drift_signals WHERE run_id = %s",
                        (run.run_id,),
                    )
                    for signal in signals:
                        self._insert_drift_signal_cursor(cur, signal)

    def get_latest_drift_run_snapshot(
        self,
        evaluator_fingerprint: str,
    ) -> tuple[DriftRun, list[DriftSignal]] | None:
        with self._pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT run_id, analysis_time, completed_at,
                                  evaluator_fingerprint, signal_count
                             FROM drift_runs
                            WHERE evaluator_fingerprint = %s
                            ORDER BY analysis_time DESC, completed_at DESC,
                                     run_id DESC
                            LIMIT 1""",
                        (evaluator_fingerprint,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    cur.execute(
                        f"SELECT {self._SIGNAL_COLUMNS} FROM drift_signals "
                        "WHERE run_id = %s ORDER BY signal_id",  # nosec B608
                        (row[0],),
                    )
                    signal_rows = cur.fetchall()
        run = DriftRun(
            run_id=row[0],
            analysis_time=row[1],
            completed_at=row[2],
            evaluator_fingerprint=row[3],
            signal_count=row[4],
        )
        signals = [self._row_to_drift_signal(item) for item in signal_rows]
        if len(signals) != run.signal_count:
            raise RuntimeError("stored drift run signal_count is inconsistent")
        return run, signals

    def delete_drift_signals_between(
        self,
        start: datetime,
        end: datetime,
        *,
        evaluator_fingerprint: str | None = None,
    ) -> None:
        match_sql = "detected_at >= %s AND detected_at < %s"
        params: tuple = (start, end)
        if evaluator_fingerprint is not None:
            match_sql += " AND evaluator_fingerprint = %s"
            params += (evaluator_fingerprint,)
        with self._pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT DISTINCT run_id FROM drift_signals "  # nosec B608
                        f"WHERE {match_sql} AND run_id IS NOT NULL "
                        "AND run_id <> ''",
                        params,
                    )
                    run_ids = sorted(row[0] for row in cur.fetchall())
                    for run_id in run_ids:
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (f"verdict:drift-run:{run_id}",),
                        )
                    if run_ids:
                        cur.execute(
                            "DELETE FROM drift_signals WHERE run_id = ANY(%s)",
                            (run_ids,),
                        )
                        cur.execute(
                            "DELETE FROM drift_runs WHERE run_id = ANY(%s)",
                            (run_ids,),
                        )
                    cur.execute(
                        f"DELETE FROM drift_signals WHERE {match_sql} "  # nosec B608
                        "AND (run_id IS NULL OR run_id = '')",
                        params,
                    )

    def list_drift_signals(self, *, limit: int = 100) -> list[DriftSignal]:
        rows = self._fetchall(
            f"SELECT {self._SIGNAL_COLUMNS} FROM drift_signals ORDER BY detected_at DESC LIMIT %s",
            (limit,),
        )
        return [self._row_to_drift_signal(r) for r in rows]

    @staticmethod
    def _row_to_drift_signal(r) -> DriftSignal:
        return DriftSignal(
            signal_id=r[0],
            detected_at=r[1],
            cluster_id=r[2] or "",
            dimension=r[3] or "",
            direction=DriftDirection(r[4] or "change"),
            evaluator_fingerprint=r[5] or "",
            run_id=r[6] or "",
            statistic_name=r[7] or "",
            statistic_value=r[8] or 0.0,
            p_value=r[9] or 1.0,
            p_value_adjusted=r[10] or 1.0,
            effect_size_cohens_d=r[11] or 0.0,
            effect_size_cliffs_delta=r[12] or 0.0,
            wasserstein_distance=r[13] or 0.0,
            psi=r[14] or 0.0,
            sample_size_current=r[15] or 0,
            sample_size_baseline=r[16] or 0,
            contributing_layers=(
                r[17] if isinstance(r[17], list) else (json.loads(r[17]) if r[17] else [])
            ),
            example_trace_ids=(
                r[18] if isinstance(r[18], list) else (json.loads(r[18]) if r[18] else [])
            ),
            recommended_action=r[19] or "",
        )

    # -- Spans ------------------------------------------------------------

    def insert_span(self, span: SpanRecord) -> None:
        sanitize_span(span)
        self._exec(
            """INSERT INTO spans (
                span_id, name, trace_id, parent_name, started_at,
                ended_at, duration_ms, attributes, error
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s
            ) ON CONFLICT (span_id) DO UPDATE SET
                -- Assign, not COALESCE: a link retraction rewrites trace_id to
                -- NULL, and COALESCE would silently keep the stale link, making
                -- the retraction guarantee backend-dependent.
                trace_id    = EXCLUDED.trace_id,
                ended_at    = EXCLUDED.ended_at,
                duration_ms = EXCLUDED.duration_ms,
                attributes  = EXCLUDED.attributes,
                error       = EXCLUDED.error""",
            (
                span.span_id,
                span.name,
                span.trace_id,
                span.parent_name,
                span.started_at,
                span.ended_at,
                span.duration_ms,
                json.dumps(span.attributes),
                span.error,
            ),
        )

    def _row_to_span(self, row) -> SpanRecord:
        return SpanRecord(
            span_id=row[0],
            name=row[1] or "",
            trace_id=row[2],
            parent_name=row[3],
            started_at=row[4],
            ended_at=row[5],
            duration_ms=row[6],
            attributes=row[7]
            if isinstance(row[7], dict)
            else (json.loads(row[7]) if row[7] else {}),
            error=row[8],
        )

    def list_spans(self, *, trace_id: str | None = None, limit: int = 100) -> list[SpanRecord]:
        clauses, params = [], []
        if trace_id is not None:
            clauses.append("trace_id = %s")
            params.append(trace_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self._fetchall(
            f"""SELECT span_id, name, trace_id, parent_name, started_at,
                       ended_at, duration_ms, attributes, error
                FROM spans {where} ORDER BY started_at DESC LIMIT %s""",
            tuple(params),
        )
        return [self._row_to_span(r) for r in rows]

    # -- User signals -----------------------------------------------------

    def insert_user_signal(self, sig: UserSignalRecord) -> None:
        self._exec(
            """INSERT INTO user_signals (signal_id, trace_id, kind, created_at)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (signal_id) DO UPDATE SET
                   kind = EXCLUDED.kind""",
            (sig.signal_id, sig.trace_id, sig.kind, sig.created_at),
        )

    def list_user_signals(self, *, limit: int = 1000) -> list[UserSignalRecord]:
        rows = self._fetchall(
            """SELECT signal_id, trace_id, kind, created_at FROM user_signals
               ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        return [
            UserSignalRecord(
                signal_id=r[0],
                trace_id=r[1] or "",
                kind=r[2] or "",
                created_at=r[3],
            )
            for r in rows
        ]

    # -- Cluster registry --------------------------------------------------

    def save_cluster_registry(self, version: str, payload_json: str) -> None:
        self._exec(
            """INSERT INTO cluster_registries (version, payload_json, updated_at)
               VALUES (%s, %s, now())
               ON CONFLICT (version) DO UPDATE
                   SET payload_json = EXCLUDED.payload_json, updated_at = now()""",
            (version, payload_json),
        )

    def load_cluster_registry(self, version: str) -> str | None:
        row = self._fetchone(
            "SELECT payload_json FROM cluster_registries WHERE version = %s",
            (version,),
        )
        return row[0] if row else None

    # -- Versioned cluster registry --------------------------------------

    @staticmethod
    def _json_text(value) -> str:
        if isinstance(value, str):
            value = json.loads(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _cluster_version_from_row(cls, row) -> ClusterRegistryVersion:
        return ClusterRegistryVersion(
            tenant_id=row[0],
            version_id=row[1],
            parent_version_id=row[2],
            strategy=row[3],
            cutoff=row[4],
            lookback_days=row[5],
            fit_definition_json=cls._json_text(row[6]),
            fit_definition_fingerprint=row[7],
            preview_report_json=cls._json_text(row[8]),
            created_at=row[9],
            created_by=row[10],
        )

    @staticmethod
    def _registry_cluster_from_row(row) -> ClusterRegistryCluster:
        return ClusterRegistryCluster(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]
        )

    @staticmethod
    def _cluster_assignment_from_row(row) -> TraceClusterAssignment:
        return TraceClusterAssignment(
            tenant_id=row[0],
            version_id=row[1],
            trace_id=row[2],
            origin=row[3],
            status=row[4],
            cluster_id=row[5],
            cluster_kind=row[6],
            reason=row[7],
            distance=row[8],
            assigned_at=row[9],
        )

    @classmethod
    def _cluster_event_from_row(cls, row) -> ClusterRegistryEvent:
        return ClusterRegistryEvent(
            tenant_id=row[0],
            event_id=row[1],
            action=row[2],
            from_version_id=row[3],
            to_version_id=row[4],
            pointer_generation=row[5],
            created_at=row[6],
            actor=row[7],
            details_json=cls._json_text(row[8]),
        )

    def insert_cluster_preview(
        self,
        version: ClusterRegistryVersion,
        identities: list[ClusterIdentity],
        clusters: list[ClusterRegistryCluster],
        assignments: list[TraceClusterAssignment],
    ) -> None:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO active_cluster_registry (tenant_id,version_id,generation) "
                "VALUES (%s,NULL,0) ON CONFLICT (tenant_id) DO NOTHING",
                (version.tenant_id,),
            )
            for identity in identities:
                if identity.tenant_id != version.tenant_id or identity.lifecycle != "provisional":
                    raise ValueError("cluster preview tenant mismatch")
                cur.execute(
                    """INSERT INTO cluster_identities (
                      tenant_id,cluster_id,kind,lifecycle,explicit_key,display_name,
                      last_model_fingerprint,last_centroid,last_version_id,
                      created_at,created_by,updated_at,updated_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id,cluster_id) DO NOTHING""",
                    (
                        identity.tenant_id,
                        identity.cluster_id,
                        identity.kind,
                        identity.lifecycle,
                        identity.explicit_key,
                        identity.display_name,
                        identity.last_model_fingerprint,
                        json.dumps(identity.last_centroid) if identity.last_centroid else None,
                        identity.last_version_id,
                        identity.created_at,
                        identity.created_by,
                        identity.updated_at,
                        identity.updated_by,
                    ),
                )
                cur.execute(
                    "SELECT kind,explicit_key FROM cluster_identities "
                    "WHERE tenant_id=%s AND cluster_id=%s",
                    (identity.tenant_id, identity.cluster_id),
                )
                if cur.fetchone() != (identity.kind, identity.explicit_key):
                    raise ValueError("cluster identity conflict")
            cur.execute(
                """INSERT INTO cluster_registry_versions (
                  tenant_id,version_id,parent_version_id,strategy,cutoff,lookback_days,
                  fit_definition_json,fit_definition_fingerprint,preview_report_json,
                  created_at,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s)""",
                (
                    version.tenant_id,
                    version.version_id,
                    version.parent_version_id,
                    version.strategy,
                    version.cutoff,
                    version.lookback_days,
                    version.fit_definition_json,
                    version.fit_definition_fingerprint,
                    version.preview_report_json,
                    version.created_at,
                    version.created_by,
                ),
            )
            for cluster in clusters:
                if (
                    cluster.tenant_id != version.tenant_id
                    or cluster.version_id != version.version_id
                ):
                    raise ValueError("cluster preview version mismatch")
                cur.execute(
                    """INSERT INTO cluster_registry_clusters (
                      tenant_id,version_id,cluster_id,kind,centroid,radius,
                      member_count,outlier_count
                    ) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
                    (
                        cluster.tenant_id,
                        cluster.version_id,
                        cluster.cluster_id,
                        cluster.kind,
                        json.dumps(cluster.centroid) if cluster.centroid is not None else None,
                        cluster.radius,
                        cluster.member_count,
                        cluster.outlier_count,
                    ),
                )
            self._insert_cluster_assignments_cursor(
                cur,
                version.tenant_id,
                assignments,
                expected_version_id=version.version_id,
            )

    def get_cluster_registry_version(
        self,
        authorized_tenant: str,
        version_id: str,
    ) -> ClusterRegistryVersion | None:
        row = self._fetchone(
            "SELECT tenant_id,version_id,parent_version_id,strategy,cutoff,lookback_days,"
            "fit_definition_json,fit_definition_fingerprint,preview_report_json,"
            "created_at,created_by FROM cluster_registry_versions "
            "WHERE tenant_id=%s AND version_id=%s",
            (authorized_tenant, version_id),
        )
        return self._cluster_version_from_row(row) if row else None

    def list_cluster_registry_clusters(
        self,
        authorized_tenant: str,
        version_id: str,
    ) -> list[ClusterRegistryCluster]:
        rows = self._fetchall(
            "SELECT tenant_id,version_id,cluster_id,kind,centroid,radius,"
            "member_count,outlier_count FROM cluster_registry_clusters "
            "WHERE tenant_id=%s AND version_id=%s ORDER BY cluster_id",
            (authorized_tenant, version_id),
        )
        return [self._registry_cluster_from_row(row) for row in rows]

    def _insert_cluster_assignments_cursor(
        self,
        cur,
        authorized_tenant: str,
        assignments: list[TraceClusterAssignment],
        *,
        expected_version_id: str | None = None,
    ) -> None:
        for assignment in assignments:
            if assignment.tenant_id != authorized_tenant or (
                expected_version_id is not None and assignment.version_id != expected_version_id
            ):
                raise ValueError("cluster assignment tenant mismatch")
            cur.execute(
                """INSERT INTO trace_cluster_assignments (
                  tenant_id,version_id,trace_id,origin,status,cluster_id,cluster_kind,
                  reason,distance,assigned_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,version_id,trace_id) DO NOTHING""",
                (
                    assignment.tenant_id,
                    assignment.version_id,
                    assignment.trace_id,
                    assignment.origin,
                    assignment.status,
                    assignment.cluster_id,
                    assignment.cluster_kind,
                    assignment.reason,
                    assignment.distance,
                    assignment.assigned_at,
                ),
            )
            cur.execute(
                "SELECT tenant_id,version_id,trace_id,origin,status,cluster_id,"
                "cluster_kind,reason,distance,assigned_at FROM trace_cluster_assignments "
                "WHERE tenant_id=%s AND version_id=%s AND trace_id=%s",
                (assignment.tenant_id, assignment.version_id, assignment.trace_id),
            )
            if self._cluster_assignment_from_row(cur.fetchone()) != assignment:
                raise ValueError("immutable assignment conflict")

    def insert_trace_cluster_assignments(
        self,
        authorized_tenant: str,
        assignments: list[TraceClusterAssignment],
    ) -> None:
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._insert_cluster_assignments_cursor(cur, authorized_tenant, assignments)

    def list_trace_cluster_assignments(
        self,
        authorized_tenant: str,
        version_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[TraceClusterAssignment]:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("assignment limit must be a positive integer")
        if type(offset) is not int or offset < 0 or (offset and limit is None):
            raise ValueError("assignment offset requires a limit")
        sql = (
            "SELECT tenant_id,version_id,trace_id,origin,status,cluster_id,cluster_kind,"
            "reason,distance,assigned_at FROM trace_cluster_assignments "
            "WHERE tenant_id=%s AND version_id=%s ORDER BY trace_id"
        )
        params: tuple = (authorized_tenant, version_id)
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            params += (limit, offset)
        rows = self._fetchall(
            sql,
            params,
        )
        return [self._cluster_assignment_from_row(row) for row in rows]

    def list_judgments_for_registry_cluster(
        self,
        authorized_tenant: str,
        version_id: str,
        cluster_id: str,
        *,
        limit: int = 1_000,
    ) -> list[Judgment]:
        rows = self._fetchall(
            f"""SELECT {self._JUDGMENT_COLUMNS} FROM judgments j
              JOIN trace_cluster_assignments a ON a.trace_id=j.trace_id
              WHERE a.tenant_id=%s AND a.version_id=%s AND a.status='assigned'
                AND a.cluster_id=%s ORDER BY j.created_at DESC LIMIT %s""",  # nosec B608
            (authorized_tenant, version_id, cluster_id, limit),
        )
        return [self._row_to_judgment(row) for row in rows]

    def insert_cluster_registry_event(self, event: ClusterRegistryEvent) -> None:
        self._exec(
            """INSERT INTO cluster_registry_events (
              tenant_id,event_id,action,from_version_id,to_version_id,
              pointer_generation,created_at,actor,details_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (
                event.tenant_id,
                event.event_id,
                event.action,
                event.from_version_id,
                event.to_version_id,
                event.pointer_generation,
                event.created_at,
                event.actor,
                event.details_json,
            ),
        )

    def list_cluster_registry_events(
        self,
        authorized_tenant: str,
        version_id: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ClusterRegistryEvent]:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("event limit must be a positive integer")
        if type(offset) is not int or offset < 0 or (offset and limit is None):
            raise ValueError("event offset requires a limit")
        sql = (
            "SELECT tenant_id,event_id,action,from_version_id,to_version_id,"
            "pointer_generation,created_at,actor,details_json "
            "FROM cluster_registry_events WHERE tenant_id=%s"
        )
        params: tuple = (authorized_tenant,)
        if version_id is not None:
            sql += " AND (from_version_id=%s OR to_version_id=%s)"
            params += (version_id, version_id)
        sql += " ORDER BY created_at,event_id"
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            params += (limit, offset)
        rows = self._fetchall(sql, params)
        return [self._cluster_event_from_row(row) for row in rows]

    def get_active_cluster_registry(
        self,
        authorized_tenant: str,
    ) -> ActiveClusterRegistry:
        self._exec(
            "INSERT INTO active_cluster_registry (tenant_id,version_id,generation) "
            "VALUES (%s,NULL,0) ON CONFLICT (tenant_id) DO NOTHING",
            (authorized_tenant,),
        )
        row = self._fetchone(
            "SELECT tenant_id,version_id,generation,activated_at,activated_by "
            "FROM active_cluster_registry WHERE tenant_id=%s",
            (authorized_tenant,),
        )
        return ActiveClusterRegistry(*row)

    def activate_cluster_registry(
        self,
        authorized_tenant: str,
        version_id: str,
        *,
        expected_generation: int,
        actor: str,
        action: str,
        expected_candidate_digest: str,
    ) -> ActiveClusterRegistry:
        if action not in {"activated", "rolled_back"}:
            raise ValueError("activation action is invalid")
        now = datetime.now().astimezone()
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO active_cluster_registry (tenant_id,version_id,generation) "
                "VALUES (%s,NULL,0) ON CONFLICT (tenant_id) DO NOTHING",
                (authorized_tenant,),
            )
            cur.execute(
                "SELECT version_id,generation FROM active_cluster_registry "
                "WHERE tenant_id=%s FOR UPDATE",
                (authorized_tenant,),
            )
            previous, generation = cur.fetchone()
            if generation != expected_generation:
                raise ValueError("cluster registry generation conflict")
            cur.execute(
                "SELECT fit_definition_json->>'model_fingerprint',parent_version_id,"
                "fit_definition_json->'config',cutoff,lookback_days "
                "FROM cluster_registry_versions "
                "WHERE tenant_id=%s AND version_id=%s",
                (authorized_tenant, version_id),
            )
            version = cur.fetchone()
            if version is None:
                raise ValueError("unknown cluster registry version")
            if action == "activated" and version[1] != previous:
                raise ValueError("cluster registry parent conflict")
            cur.execute(
                "SELECT action FROM cluster_registry_events WHERE tenant_id=%s "
                "AND to_version_id=%s AND action IN ('validated','validation_failed') "
                "ORDER BY created_at DESC,event_id DESC LIMIT 1",
                (authorized_tenant, version_id),
            )
            validation = cur.fetchone()
            if validation is None or validation[0] != "validated":
                raise ValueError("cluster registry version is not validated")
            config = version[2] or {}
            cur.execute(
                "SELECT trace_id FROM trace_cluster_assignments "
                "WHERE tenant_id=%s AND version_id=%s AND origin='fit'",
                (authorized_tenant, version_id),
            )
            candidate_ids = [row[0] for row in cur.fetchall()]
            if (
                len(candidate_ids) > config.get("max_fit_candidates", 50_000)
                or cluster_candidate_digest(candidate_ids) != expected_candidate_digest
            ):
                raise ValueError("cluster registry coverage changed")
            cur.execute(
                """SELECT kind,COUNT(*) FROM cluster_identities i
                  WHERE tenant_id=%s AND (lifecycle='active' OR EXISTS (
                    SELECT 1 FROM cluster_registry_clusters c
                    WHERE c.tenant_id=i.tenant_id AND c.cluster_id=i.cluster_id
                      AND c.version_id=%s)) GROUP BY kind""",
                (authorized_tenant, version_id),
            )
            identity_counts = dict(cur.fetchall())
            if identity_counts.get("explicit", 0) > config.get(
                "max_explicit_identities_per_tenant", 10_000
            ) or identity_counts.get("semantic", 0) > config.get(
                "max_semantic_identities_per_tenant", 5_000
            ):
                raise ValueError("identity_limit")
            next_generation = generation + 1
            event = ClusterRegistryEvent(
                authorized_tenant,
                action=action,
                from_version_id=previous,
                to_version_id=version_id,
                pointer_generation=next_generation,
                created_at=now,
                actor=actor,
            )
            cur.execute(
                "INSERT INTO cluster_registry_events VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (
                    event.tenant_id,
                    event.event_id,
                    event.action,
                    event.from_version_id,
                    event.to_version_id,
                    event.pointer_generation,
                    event.created_at,
                    event.actor,
                    event.details_json,
                ),
            )
            cur.execute(
                "UPDATE active_cluster_registry SET version_id=%s,generation=%s,"
                "activated_at=%s,activated_by=%s WHERE tenant_id=%s",
                (version_id, next_generation, now, actor, authorized_tenant),
            )
            cur.execute(
                """UPDATE cluster_identities i SET lifecycle='active',
                  last_model_fingerprint=%s,last_centroid=c.centroid,
                  last_version_id=%s,updated_at=%s,updated_by=%s
                  FROM cluster_registry_clusters c
                  WHERE c.tenant_id=%s AND c.version_id=%s
                    AND i.tenant_id=c.tenant_id AND i.cluster_id=c.cluster_id
                    AND i.kind=c.kind""",
                (version[0], version_id, now, actor, authorized_tenant, version_id),
            )
            cur.execute(
                """SELECT COUNT(*) FROM cluster_registry_clusters c
                  JOIN cluster_identities i USING (tenant_id,cluster_id,kind)
                  WHERE c.tenant_id=%s AND c.version_id=%s
                    AND i.lifecycle='active' AND i.last_version_id=%s
                    AND i.last_model_fingerprint IS NOT DISTINCT FROM %s
                    AND i.last_centroid IS NOT DISTINCT FROM c.centroid""",
                (authorized_tenant, version_id, version_id, version[0]),
            )
            valid = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM cluster_registry_clusters "
                "WHERE tenant_id=%s AND version_id=%s",
                (authorized_tenant, version_id),
            )
            if valid != cur.fetchone()[0]:
                raise ValueError("cluster activation identity invariant failed")
        return ActiveClusterRegistry(authorized_tenant, version_id, next_generation, now, actor)

    def rename_cluster_identity(
        self, authorized_tenant: str, cluster_id: str, display_name: str, *, actor: str
    ) -> None:
        event = ClusterRegistryEvent(
            authorized_tenant,
            action="renamed",
            actor=actor,
            details_json=json.dumps({"cluster_id": cluster_id}, sort_keys=True),
        )
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cluster_registry_events VALUES (%s,%s,%s,NULL,NULL,NULL,%s,%s,%s::jsonb)",
                (
                    event.tenant_id,
                    event.event_id,
                    event.action,
                    event.created_at,
                    event.actor,
                    event.details_json,
                ),
            )
            cur.execute(
                "UPDATE cluster_identities SET display_name=%s,updated_at=%s,updated_by=%s "
                "WHERE tenant_id=%s AND cluster_id=%s RETURNING display_name",
                (display_name, event.created_at, actor, authorized_tenant, cluster_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("unknown cluster identity")
            if row[0] != display_name:
                raise ValueError("cluster rename invariant failed")

    def list_cluster_identities(
        self,
        authorized_tenant: str,
        *,
        cluster_ids: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ClusterIdentity]:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("identity limit must be a positive integer")
        if type(offset) is not int or offset < 0 or (offset and limit is None):
            raise ValueError("identity offset requires a limit")
        if cluster_ids == []:
            return []
        sql = (
            "SELECT tenant_id,cluster_id,kind,lifecycle,explicit_key,display_name,"
            "last_model_fingerprint,last_centroid,last_version_id,created_at,created_by,"
            "updated_at,updated_by FROM cluster_identities WHERE tenant_id=%s "
        )
        params: tuple = (authorized_tenant,)
        if cluster_ids is not None:
            sql += "AND cluster_id=ANY(%s) "
            params += (cluster_ids,)
        sql += "ORDER BY cluster_id"
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            params += (limit, offset)
        rows = self._fetchall(sql, params)
        return [ClusterIdentity(*row) for row in rows]

    _WORKLOAD_VALUE_SQL = """CASE
      WHEN jsonb_typeof(tags->'verdict.workload')='string'
       AND octet_length(tags->>'verdict.workload') BETWEEN 1 AND 64
      THEN tags->>'verdict.workload' END"""

    def list_cluster_trace_candidates(
        self,
        authorized_tenant: str,
        start_us: int,
        cutoff_us: int,
        *,
        target_workload: str | None,
        limit: int,
        missing_version_id: str | None = None,
    ) -> list[ClusterTraceCandidate]:
        where = f"""{_trace_tenant_clause(authorized_tenant)} AND ended_at IS NOT NULL
          AND analysis_started_at_state='valid'
          AND analysis_started_at_us>=%s AND analysis_started_at_us<%s"""
        params: tuple = (authorized_tenant, start_us, cutoff_us)
        if missing_version_id is not None:
            where += """ AND NOT EXISTS (
              SELECT 1 FROM trace_cluster_assignments a
              WHERE a.tenant_id=%s AND a.version_id=%s AND a.trace_id=traces.trace_id)"""
            params += (authorized_tenant, missing_version_id)
        if target_workload is None:
            where += f" AND COALESCE(({self._WORKLOAD_VALUE_SQL}),'') NOT IN (%s,%s)"
            params += ("judge", "paired_replay")
        else:
            where += f" AND ({self._WORKLOAD_VALUE_SQL})=%s"
            params += (target_workload,)
        rows = self._fetchall(
            f"""SELECT octet_length(trace_id),
              CASE WHEN octet_length(trace_id)<=256 THEN trace_id END,
              tenant_id,analysis_started_at_us,
              COALESCE(jsonb_typeof(tags->'verdict.workload'),'missing'),
              CASE WHEN jsonb_typeof(tags->'verdict.workload')='string'
                THEN octet_length(tags->>'verdict.workload') END,
              {self._WORKLOAD_VALUE_SQL},
              COALESCE(jsonb_typeof(tags->'verdict.intent_key'),'missing'),
              CASE WHEN jsonb_typeof(tags->'verdict.intent_key')='string'
                THEN octet_length(tags->>'verdict.intent_key') END,
              CASE WHEN jsonb_typeof(tags->'verdict.intent_key')='string'
                AND octet_length(tags->>'verdict.intent_key') BETWEEN 1 AND 64
                THEN tags->>'verdict.intent_key' END,
              analysis_raw_messages_state,analysis_raw_messages_utf8_bytes
              FROM traces WHERE {where}
              ORDER BY analysis_started_at_us,trace_id LIMIT %s""",  # nosec B608
            (*params, limit),
        )
        return [ClusterTraceCandidate(*row) for row in rows]

    def cluster_trace_time_bounds(
        self,
        authorized_tenant: str,
        *,
        target_workload: str | None,
    ) -> tuple[int, int | None, int | None]:
        where = f"""{_trace_tenant_clause(authorized_tenant)} AND ended_at IS NOT NULL
          AND analysis_started_at_state='valid'"""
        params: tuple[object, ...] = (authorized_tenant,)
        if target_workload is None:
            where += f" AND COALESCE(({self._WORKLOAD_VALUE_SQL}),'') NOT IN (%s,%s)"
            params += ("judge", "paired_replay")
        else:
            where += f" AND ({self._WORKLOAD_VALUE_SQL})=%s"
            params += (target_workload,)
        row = self._fetchone(
            f"SELECT COUNT(*),MIN(analysis_started_at_us),MAX(analysis_started_at_us) "
            f"FROM traces WHERE {where}",  # nosec B608
            params,
        )
        return int(row[0]), row[1], row[2]

    def get_cluster_trace_messages(
        self,
        authorized_tenant: str,
        trace_ids: list[str],
    ) -> dict[str, list[dict] | None]:
        if not trace_ids:
            return {}
        rows = self._fetchall(
            f"""SELECT trace_id,CASE WHEN analysis_raw_messages_state='valid'
              AND analysis_raw_messages_utf8_bytes<=67108864 THEN raw_messages END
              FROM traces WHERE {_trace_tenant_clause(authorized_tenant)}
              AND trace_id=ANY(%s)""",  # nosec B608
            (authorized_tenant, trace_ids),
        )
        return {row[0]: row[1] for row in rows}

    def count_pending_analysis_rows(self, authorized_tenant: str) -> int:
        return self._fetchone(
            f"""SELECT COUNT(*) FROM traces WHERE {_trace_tenant_clause(authorized_tenant)} AND
              (analysis_started_at_state='pending' OR
               analysis_raw_messages_state='pending')""",  # nosec B608
            (authorized_tenant,),
        )[0]

    @contextmanager
    def cluster_analysis_snapshot(self):
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            token = self._cluster_snapshot_connection.set(conn)
            try:
                yield self
            finally:
                self._cluster_snapshot_connection.reset(token)

    def normalize_cluster_trace_analysis(
        self, authorized_tenant: str, *, limit: int = 10_000
    ) -> int:
        if not 1 <= limit <= 10_000:
            raise ValueError("normalization limit must be in [1,10000]")
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                f"""SELECT trace_id,started_at,octet_length(raw_messages::text),
                  CASE WHEN octet_length(raw_messages::text)<=67108864
                       THEN raw_messages END
                  FROM traces WHERE {_trace_tenant_clause(authorized_tenant)} AND
                   (analysis_started_at_state='pending' OR
                    analysis_raw_messages_state='pending')
                  ORDER BY trace_id LIMIT %s FOR UPDATE""",  # nosec B608
                (authorized_tenant, limit),
            )
            rows = cur.fetchall()
            for trace_id, started_at, stored_bytes, raw_messages in rows:
                trace = Trace(trace_id=trace_id, started_at=started_at, raw_messages=None)
                populate_trace_analysis_fields(trace)
                if stored_bytes is not None and stored_bytes > 67_108_864:
                    trace.analysis_raw_messages_utf8_bytes = stored_bytes
                    trace.analysis_raw_messages_state = "oversize"
                elif raw_messages is not None:
                    trace.raw_messages = raw_messages
                    populate_trace_analysis_fields(trace)
                cur.execute(
                    """UPDATE traces SET analysis_started_at_us=%s,
                       analysis_started_at_state=%s,analysis_raw_messages_utf8_bytes=%s,
                       analysis_raw_messages_state=%s WHERE trace_id=%s""",
                    (
                        trace.analysis_started_at_us,
                        trace.analysis_started_at_state,
                        trace.analysis_raw_messages_utf8_bytes,
                        trace.analysis_raw_messages_state,
                        trace.trace_id,
                    ),
                )
        return len(rows)

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception:
            pass
