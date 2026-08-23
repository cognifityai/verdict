"""SQLite storage adapter — durable, single-file, zero external deps.

For v0 this is the primary storage backend. Postgres lives behind the same
Storage Protocol and will be added when we need multi-process access.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from verdict.redaction import sanitize_judgment, sanitize_span, sanitize_trace
from verdict.schema import (
    ActiveClusterRegistry,
    ClusterIdentity,
    ClusterRegistryCluster,
    ClusterRegistryEvent,
    ClusterRegistryVersion,
    ClusterTraceCandidate,
    ConversationDriftRun,
    ConversationDriftSample,
    ConversationDriftSignal,
    ConversationTraceCandidate,
    ConversationTraceContent,
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
    datetime_to_utc_us,
    populate_trace_analysis_fields,
)
from verdict.storage.base import (
    _validate_conversation_drift_snapshot,
    _validate_drift_run_snapshot,
)
from verdict.storage.conversation import (
    RUN_COLUMNS,
    SAMPLE_COLUMNS,
    SIGNAL_COLUMNS,
    run_from_row,
    run_values,
    sample_from_row,
    sample_values,
    signal_from_row,
    signal_values,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    parent_span_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    provider TEXT,
    operation TEXT,
    request_model TEXT,
    response_model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    temperature REAL,
    max_tokens INTEGER,
    finish_reason TEXT,
    error TEXT,
    latency_ms REAL,
    prompt_redacted TEXT,
    response_redacted TEXT,
    raw_messages_json TEXT,
    tenant_id TEXT,
    session_id TEXT,
    user_id_hash TEXT,
    cluster_id TEXT,
    tags_json TEXT,
    cost_usd REAL,
    analysis_started_at_us INTEGER,
    analysis_started_at_state TEXT NOT NULL DEFAULT 'pending',
    analysis_raw_messages_utf8_bytes INTEGER,
    analysis_raw_messages_state TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_traces_cluster ON traces(cluster_id);
CREATE INDEX IF NOT EXISTS idx_traces_tenant ON traces(tenant_id);
CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at);
CREATE INDEX IF NOT EXISTS idx_traces_tenant_started_completed_v2
    ON traces(tenant_id,analysis_started_at_us,trace_id)
    WHERE ended_at IS NOT NULL AND analysis_started_at_state='valid';
CREATE INDEX IF NOT EXISTS idx_traces_tenant_workload_started_completed_v2
    ON traces(
      tenant_id,
      (CASE WHEN json_valid(tags_json)
        AND json_type(tags_json,'$."verdict.workload"')='text'
        AND length(CAST(json_extract(tags_json,'$."verdict.workload"') AS BLOB))
            BETWEEN 1 AND 64
       THEN json_extract(tags_json,'$."verdict.workload"') END),
      analysis_started_at_us,trace_id)
    WHERE ended_at IS NOT NULL AND analysis_started_at_state='valid';
CREATE INDEX IF NOT EXISTS idx_traces_tenant_analysis_pending_v2
    ON traces(tenant_id,trace_id)
    WHERE analysis_started_at_state='pending'
       OR analysis_raw_messages_state='pending';

CREATE TABLE IF NOT EXISTS judgments (
    judgment_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    rubric_name TEXT,
    rubric_version TEXT,
    created_at TEXT NOT NULL,
    judge_models_json TEXT,
    dimensions_json TEXT,
    position_swap_consistent INTEGER,
    evaluator_provider TEXT,
    evaluator_config_json TEXT,
    evaluator_fingerprint TEXT,
    expected_dimensions_json TEXT,
    status TEXT DEFAULT 'completed',
    error TEXT,
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
);
CREATE INDEX IF NOT EXISTS idx_judgments_trace ON judgments(trace_id);
CREATE INDEX IF NOT EXISTS idx_judgments_created ON judgments(created_at);

CREATE TABLE IF NOT EXISTS evaluator_health (
    health_id TEXT PRIMARY KEY,
    evaluated_at TEXT NOT NULL,
    evaluator_fingerprint TEXT NOT NULL,
    sentinel_set_name TEXT,
    sentinel_set_fingerprint TEXT NOT NULL,
    correct_examples INTEGER NOT NULL,
    total_examples INTEGER NOT NULL,
    example_agreement REAL,
    example_confidence_low REAL,
    example_confidence_high REAL,
    correct_labels INTEGER NOT NULL,
    total_labels INTEGER NOT NULL,
    label_agreement REAL,
    status TEXT NOT NULL,
    error_count INTEGER NOT NULL DEFAULT 0,
    method_version TEXT NOT NULL DEFAULT '2'
);
CREATE INDEX IF NOT EXISTS idx_evaluator_health_identity
    ON evaluator_health(evaluator_fingerprint, evaluated_at);

CREATE TABLE IF NOT EXISTS drift_runs (
    run_id TEXT PRIMARY KEY,
    analysis_time TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    evaluator_fingerprint TEXT NOT NULL,
    signal_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drift_runs_latest
    ON drift_runs(evaluator_fingerprint, analysis_time, completed_at, run_id);

CREATE TABLE IF NOT EXISTS drift_signals (
    signal_id TEXT PRIMARY KEY,
    detected_at TEXT NOT NULL,
    cluster_id TEXT,
    dimension TEXT,
    direction TEXT,
    evaluator_fingerprint TEXT,
    run_id TEXT,
    statistic_name TEXT,
    statistic_value REAL,
    p_value REAL,
    p_value_adjusted REAL,
    effect_size_cohens_d REAL,
    effect_size_cliffs_delta REAL DEFAULT 0.0,
    wasserstein_distance REAL DEFAULT 0.0,
    psi REAL DEFAULT 0.0,
    sample_size_current INTEGER,
    sample_size_baseline INTEGER,
    contributing_layers_json TEXT,
    example_trace_ids_json TEXT,
    recommended_action TEXT
);
-- Migrate older databases that lack the new effect-size columns
-- (SQLite ignores errors from duplicate-column ALTERs via separate execs)
CREATE INDEX IF NOT EXISTS idx_signals_detected ON drift_signals(detected_at);
CREATE INDEX IF NOT EXISTS idx_signals_cluster_dim ON drift_signals(cluster_id, dimension);
CREATE INDEX IF NOT EXISTS idx_signals_run ON drift_signals(run_id);

CREATE TABLE IF NOT EXISTS cluster_registries (
    version TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_identities (
    tenant_id TEXT NOT NULL CHECK(length(CAST(tenant_id AS BLOB)) BETWEEN 1 AND 128),
    cluster_id TEXT NOT NULL CHECK(length(CAST(cluster_id AS BLOB)) BETWEEN 1 AND 64),
    kind TEXT NOT NULL CHECK(kind IN ('explicit','semantic')),
    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('provisional','active')),
    explicit_key TEXT CHECK(explicit_key IS NULL OR
        length(CAST(explicit_key AS BLOB)) BETWEEN 1 AND 64),
    display_name TEXT NOT NULL CHECK(length(CAST(display_name AS BLOB)) BETWEEN 1 AND 256),
    last_model_fingerprint TEXT,
    last_centroid_json TEXT CHECK(last_centroid_json IS NULL OR
        length(CAST(last_centroid_json AS BLOB)) <= 65536),
    last_version_id TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK(length(CAST(created_by AS BLOB)) <= 256),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL CHECK(length(CAST(updated_by AS BLOB)) <= 256),
    PRIMARY KEY (tenant_id, cluster_id),
    UNIQUE (tenant_id, cluster_id, kind),
    UNIQUE (tenant_id, explicit_key),
    CHECK ((kind='explicit' AND explicit_key IS NOT NULL) OR
           (kind='semantic' AND explicit_key IS NULL))
);

CREATE TABLE IF NOT EXISTS cluster_registry_versions (
    tenant_id TEXT NOT NULL CHECK(length(CAST(tenant_id AS BLOB)) BETWEEN 1 AND 128),
    version_id TEXT NOT NULL CHECK(length(CAST(version_id AS BLOB)) BETWEEN 1 AND 64),
    parent_version_id TEXT,
    strategy TEXT NOT NULL CHECK(strategy IN ('explicit','semantic','hybrid')),
    cutoff TEXT NOT NULL,
    lookback_days INTEGER NOT NULL CHECK(lookback_days > 0),
    fit_definition_json TEXT NOT NULL CHECK(length(CAST(fit_definition_json AS BLOB)) <= 65536),
    fit_definition_fingerprint TEXT NOT NULL,
    preview_report_json TEXT NOT NULL CHECK(length(CAST(preview_report_json AS BLOB)) <= 1048576),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK(length(CAST(created_by AS BLOB)) <= 256),
    PRIMARY KEY (tenant_id, version_id),
    FOREIGN KEY (tenant_id, parent_version_id)
        REFERENCES cluster_registry_versions(tenant_id, version_id)
);

CREATE TABLE IF NOT EXISTS cluster_registry_clusters (
    tenant_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('explicit','semantic')),
    centroid_json TEXT CHECK(centroid_json IS NULL OR length(CAST(centroid_json AS BLOB)) <= 65536),
    radius REAL,
    member_count INTEGER NOT NULL CHECK(member_count >= 0),
    outlier_count INTEGER NOT NULL CHECK(outlier_count >= 0),
    PRIMARY KEY (tenant_id, version_id, cluster_id),
    UNIQUE (tenant_id, version_id, cluster_id, kind),
    FOREIGN KEY (tenant_id, version_id)
        REFERENCES cluster_registry_versions(tenant_id, version_id),
    FOREIGN KEY (tenant_id, cluster_id, kind)
        REFERENCES cluster_identities(tenant_id, cluster_id, kind),
    CHECK ((kind='explicit' AND centroid_json IS NULL AND radius IS NULL) OR
           (kind='semantic' AND centroid_json IS NOT NULL AND radius BETWEEN 0 AND 2))
);

CREATE TABLE IF NOT EXISTS trace_cluster_assignments (
    tenant_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    trace_id TEXT NOT NULL CHECK(length(CAST(trace_id AS BLOB)) BETWEEN 1 AND 256),
    origin TEXT NOT NULL CHECK(origin IN ('fit','incremental')),
    status TEXT NOT NULL CHECK(status IN ('assigned','outlier','ineligible')),
    cluster_id TEXT,
    cluster_kind TEXT,
    reason TEXT,
    distance REAL,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, version_id, trace_id),
    UNIQUE (tenant_id, version_id, trace_id, cluster_id),
    FOREIGN KEY (tenant_id, version_id)
        REFERENCES cluster_registry_versions(tenant_id, version_id),
    FOREIGN KEY (tenant_id, version_id, cluster_id, cluster_kind)
        REFERENCES cluster_registry_clusters(tenant_id, version_id, cluster_id, kind),
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
           'invalid_workload','unsafe_workload','missing_intent_key',
           'invalid_intent_key','unsafe_intent_key','content_not_captured',
           'raw_messages_oversize','malformed_messages','no_supported_user_text',
           'text_too_short','text_too_long','redaction_error'))
    )
);

CREATE TABLE IF NOT EXISTS active_cluster_registry (
    tenant_id TEXT PRIMARY KEY CHECK(length(CAST(tenant_id AS BLOB)) BETWEEN 1 AND 128),
    version_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    activated_at TEXT,
    activated_by TEXT,
    FOREIGN KEY (tenant_id, version_id)
        REFERENCES cluster_registry_versions(tenant_id, version_id)
);

CREATE TABLE IF NOT EXISTS cluster_registry_events (
    tenant_id TEXT NOT NULL CHECK(length(CAST(tenant_id AS BLOB)) BETWEEN 1 AND 128),
    event_id TEXT NOT NULL CHECK(length(CAST(event_id AS BLOB)) BETWEEN 1 AND 64),
    action TEXT NOT NULL CHECK(action IN (
        'validated','validation_failed','activated','rolled_back','renamed')),
    from_version_id TEXT,
    to_version_id TEXT,
    pointer_generation INTEGER CHECK(pointer_generation IS NULL OR pointer_generation >= 0),
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL CHECK(length(CAST(actor AS BLOB)) <= 256),
    details_json TEXT NOT NULL CHECK(length(CAST(details_json AS BLOB)) <= 1048576),
    PRIMARY KEY (tenant_id, event_id),
    FOREIGN KEY (tenant_id, from_version_id)
        REFERENCES cluster_registry_versions(tenant_id, version_id),
    FOREIGN KEY (tenant_id, to_version_id)
        REFERENCES cluster_registry_versions(tenant_id, version_id)
);
CREATE INDEX IF NOT EXISTS idx_cluster_events_version
    ON cluster_registry_events(tenant_id, to_version_id, created_at, event_id);

CREATE TABLE IF NOT EXISTS conversation_drift_runs (
    tenant_id TEXT NOT NULL CHECK(length(CAST(tenant_id AS BLOB)) BETWEEN 1 AND 128),
    run_id TEXT NOT NULL CHECK(length(CAST(run_id AS BLOB)) BETWEEN 1 AND 256),
    registry_version TEXT NOT NULL,
    method TEXT NOT NULL CHECK(method='conversation-v1'),
    analysis_policy_json TEXT NOT NULL CHECK(length(CAST(analysis_policy_json AS BLOB)) <= 32768),
    analysis_policy_fingerprint TEXT NOT NULL,
    evaluator_definition_json TEXT NOT NULL CHECK(length(CAST(evaluator_definition_json AS BLOB)) <= 65536),
    evaluator_fingerprint TEXT NOT NULL,
    target_workload TEXT NOT NULL CHECK(length(CAST(target_workload AS BLOB)) BETWEEN 1 AND 64),
    baseline_start INTEGER NOT NULL CHECK(typeof(baseline_start)='integer'),
    baseline_end INTEGER NOT NULL CHECK(typeof(baseline_end)='integer'),
    current_start INTEGER NOT NULL CHECK(typeof(current_start)='integer'),
    current_end INTEGER NOT NULL CHECK(typeof(current_end)='integer'),
    analysis_cutoff INTEGER NOT NULL CHECK(typeof(analysis_cutoff)='integer'),
    status TEXT NOT NULL CHECK(status IN ('ineligible','insufficient','partial','ready','unavailable')),
    unavailable_reason TEXT,
    coverage_json TEXT NOT NULL CHECK(length(CAST(coverage_json AS BLOB)) <= 16777216),
    signal_count INTEGER NOT NULL CHECK(signal_count BETWEEN 0 AND 16000),
    sample_count INTEGER NOT NULL CHECK(sample_count BETWEEN 0 AND 50000),
    started_at INTEGER NOT NULL CHECK(typeof(started_at)='integer'),
    completed_at INTEGER NOT NULL CHECK(typeof(completed_at)='integer'),
    actor TEXT NOT NULL CHECK(length(CAST(actor AS BLOB)) BETWEEN 1 AND 256),
    PRIMARY KEY (tenant_id, run_id),
    UNIQUE (tenant_id, run_id, registry_version),
    FOREIGN KEY (tenant_id, registry_version)
        REFERENCES cluster_registry_versions(tenant_id, version_id),
    CHECK (baseline_start < baseline_end AND baseline_end <= current_start AND
           current_start < current_end AND current_end <= analysis_cutoff),
    CHECK (baseline_end-baseline_start <= 7776000000000 AND
           current_end-current_start <= 7776000000000 AND
           current_end-baseline_start <= 15552000000000),
    CHECK (started_at <= completed_at),
    CHECK ((status='unavailable' AND unavailable_reason IS NOT NULL) OR
           (status<>'unavailable' AND unavailable_reason IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_conversation_runs_latest
    ON conversation_drift_runs(tenant_id, completed_at, run_id);

CREATE TABLE IF NOT EXISTS conversation_drift_signals (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    registry_version TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('regression','improvement','change')),
    statistic_name TEXT NOT NULL,
    statistic_value REAL NOT NULL,
    p_value REAL NOT NULL CHECK(p_value BETWEEN 0 AND 1),
    p_value_adjusted REAL NOT NULL CHECK(p_value_adjusted BETWEEN 0 AND 1),
    effect_size REAL NOT NULL CHECK(effect_size BETWEEN -1 AND 1),
    sample_size_current INTEGER NOT NULL CHECK(sample_size_current BETWEEN 0 AND 50000),
    sample_size_baseline INTEGER NOT NULL CHECK(sample_size_baseline BETWEEN 0 AND 50000),
    examples_json TEXT NOT NULL CHECK(length(CAST(examples_json AS BLOB)) <= 16384),
    recommended_action TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, signal_id),
    UNIQUE (tenant_id,run_id,cluster_id,dimension,statistic_name,direction),
    FOREIGN KEY (tenant_id,run_id,registry_version)
        REFERENCES conversation_drift_runs(tenant_id,run_id,registry_version),
    FOREIGN KEY (tenant_id,registry_version,cluster_id)
        REFERENCES cluster_registry_clusters(tenant_id,version_id,cluster_id)
);

CREATE TABLE IF NOT EXISTS conversation_drift_samples (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    registry_version TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    session_ordinal INTEGER NOT NULL CHECK(session_ordinal BETWEEN 1 AND 50000),
    window TEXT NOT NULL CHECK(window IN ('baseline','current')),
    trace_id TEXT NOT NULL,
    event_time INTEGER NOT NULL CHECK(typeof(event_time)='integer'),
    attempt_terminal_at INTEGER NOT NULL CHECK(typeof(attempt_terminal_at)='integer'),
    attempt_status TEXT NOT NULL CHECK(attempt_status IN ('completed','error')),
    error_category TEXT,
    judgment_id TEXT,
    legacy_write_status TEXT NOT NULL CHECK(legacy_write_status IN
        ('written','source_deleted','storage_error','not_attempted')),
    outcomes_json TEXT NOT NULL CHECK(length(CAST(outcomes_json AS BLOB)) <= 32768),
    PRIMARY KEY (tenant_id,run_id,cluster_id,session_ordinal),
    UNIQUE (tenant_id,run_id,trace_id),
    FOREIGN KEY (tenant_id,run_id,registry_version)
        REFERENCES conversation_drift_runs(tenant_id,run_id,registry_version),
    FOREIGN KEY (tenant_id,registry_version,trace_id,cluster_id)
        REFERENCES trace_cluster_assignments(tenant_id,version_id,trace_id,cluster_id),
    CHECK ((attempt_status='completed' AND error_category IS NULL AND
            json_valid(outcomes_json) AND json_type(outcomes_json)='object' AND
            json(outcomes_json)<>'{}') OR
           (attempt_status='error' AND error_category IN
            ('timeout','rate_limited','connection','provider','invalid_response','internal') AND
            json(outcomes_json)='{}'))
);

CREATE TRIGGER IF NOT EXISTS validate_conversation_sample_insert
BEFORE INSERT ON conversation_drift_samples BEGIN
    SELECT CASE WHEN NOT EXISTS (
      SELECT 1 FROM conversation_drift_runs r
      WHERE r.tenant_id=NEW.tenant_id AND r.run_id=NEW.run_id
        AND r.registry_version=NEW.registry_version
        AND NEW.attempt_terminal_at BETWEEN r.started_at AND r.completed_at
        AND ((NEW.window='baseline' AND NEW.event_time>=r.baseline_start
              AND NEW.event_time<r.baseline_end)
          OR (NEW.window='current' AND NEW.event_time>=r.current_start
              AND NEW.event_time<r.current_end))
    ) THEN RAISE(ABORT,'conversation sample time contract failed') END;
    SELECT CASE WHEN NEW.attempt_status='completed' AND (
      EXISTS (SELECT 1 FROM json_each(NEW.outcomes_json) WHERE value NOT IN ('PASS','FAIL','UNCLEAR')) OR
      EXISTS (
        SELECT 1 FROM json_each((SELECT evaluator_definition_json
          FROM conversation_drift_runs WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id),'$.dimensions') expected
        WHERE NOT EXISTS (SELECT 1 FROM json_each(NEW.outcomes_json) actual WHERE actual.key=expected.value)
      ) OR
      EXISTS (
        SELECT 1 FROM json_each(NEW.outcomes_json) actual
        WHERE NOT EXISTS (SELECT 1 FROM json_each((SELECT evaluator_definition_json
          FROM conversation_drift_runs WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id),'$.dimensions') expected
          WHERE expected.value=actual.key)
      )
    ) THEN RAISE(ABORT,'conversation sample dimensions failed') END;
END;

CREATE TRIGGER IF NOT EXISTS immutable_conversation_runs_update
BEFORE UPDATE ON conversation_drift_runs BEGIN
    SELECT RAISE(ABORT, 'conversation drift run is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_conversation_runs_delete
BEFORE DELETE ON conversation_drift_runs BEGIN
    SELECT RAISE(ABORT, 'conversation drift run is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_conversation_samples_update
BEFORE UPDATE ON conversation_drift_samples BEGIN
    SELECT RAISE(ABORT, 'conversation drift sample is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_conversation_samples_delete
BEFORE DELETE ON conversation_drift_samples BEGIN
    SELECT RAISE(ABORT, 'conversation drift sample is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_conversation_signals_update
BEFORE UPDATE ON conversation_drift_signals BEGIN
    SELECT RAISE(ABORT, 'conversation drift signal is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_conversation_signals_delete
BEFORE DELETE ON conversation_drift_signals BEGIN
    SELECT RAISE(ABORT, 'conversation drift signal is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_cluster_versions_update
BEFORE UPDATE ON cluster_registry_versions BEGIN
    SELECT RAISE(ABORT, 'cluster registry version is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_cluster_versions_delete
BEFORE DELETE ON cluster_registry_versions BEGIN
    SELECT RAISE(ABORT, 'cluster registry version is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_registry_clusters_update
BEFORE UPDATE ON cluster_registry_clusters BEGIN
    SELECT RAISE(ABORT, 'cluster registry cluster is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_registry_clusters_delete
BEFORE DELETE ON cluster_registry_clusters BEGIN
    SELECT RAISE(ABORT, 'cluster registry cluster is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_cluster_assignments_update
BEFORE UPDATE ON trace_cluster_assignments BEGIN
    SELECT RAISE(ABORT, 'trace cluster assignment is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_cluster_assignments_delete
BEFORE DELETE ON trace_cluster_assignments BEGIN
    SELECT RAISE(ABORT, 'trace cluster assignment is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_cluster_events_update
BEFORE UPDATE ON cluster_registry_events BEGIN
    SELECT RAISE(ABORT, 'cluster registry event is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_cluster_events_delete
BEFORE DELETE ON cluster_registry_events BEGIN
    SELECT RAISE(ABORT, 'cluster registry event is immutable');
END;
CREATE TRIGGER IF NOT EXISTS guarded_cluster_identity_update
BEFORE UPDATE ON cluster_identities
WHEN NEW.tenant_id<>OLD.tenant_id OR NEW.cluster_id<>OLD.cluster_id OR
     NEW.kind<>OLD.kind OR NEW.explicit_key IS NOT OLD.explicit_key OR
     NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by OR
     (OLD.lifecycle='active' AND NEW.lifecycle<>'active')
BEGIN
    SELECT RAISE(ABORT, 'cluster identity immutable field changed');
END;

CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    name TEXT,
    trace_id TEXT,
    parent_name TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms REAL,
    attributes_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_started ON spans(started_at);

CREATE TABLE IF NOT EXISTS user_signals (
    signal_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    kind TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_signals_trace ON user_signals(trace_id);
CREATE INDEX IF NOT EXISTS idx_user_signals_created ON user_signals(created_at);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    # Normalize timezone-naive datetimes to UTC before serializing so every
    # stored timestamp carries an explicit "+00:00" offset. Without this, a mix
    # of naive ("...T12:00:00") and aware ("...T12:00:00+00:00") values would
    # string-sort inconsistently in ORDER BY started_at clauses. Aware datetimes
    # pass through unchanged.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _parse_utc_us(value: object) -> datetime:
    if type(value) is not int:
        raise ValueError("conversation timestamp is invalid")
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value)


def _normalize_sqlite_path(path: str) -> str:
    """Accept either a filesystem path or the public ``sqlite:///`` URL form."""
    if path.startswith("sqlite:///"):
        path = path[len("sqlite:///") :]
    elif "://" in path:
        raise ValueError(f"Expected a SQLite path or sqlite:/// URL, got {path!r}")
    if not path:
        raise ValueError("SQLite path cannot be empty")
    return path


class SQLiteStorage:
    """Durable single-file storage. Thread-safe via a single connection + lock."""

    def __init__(self, path: str) -> None:
        path = _normalize_sqlite_path(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        # Set row_factory once on the shared connection rather than mutating it
        # per read call (shared-mutable-state smell). All reads expect sqlite3.Row.
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Enforce the judgments->traces foreign key. SQLite defaults FK
        # enforcement OFF per-connection, so without this the FK declared in
        # the schema is inert (orphan judgments could be inserted).
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            # Migrate columns used by CREATE INDEX statements *before* running
            # the full idempotent schema. Otherwise an older table makes
            # executescript abort at the index and none of the later tables or
            # migrations are created.
            existing_tables = {
                row[0]
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "traces" in existing_tables:
                trace_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(traces)")}
                for column, ddl in (
                    ("cluster_id", "TEXT"),
                    ("parent_span_id", "TEXT"),
                    ("analysis_started_at_us", "INTEGER"),
                    ("analysis_started_at_state", "TEXT NOT NULL DEFAULT 'pending'"),
                    ("analysis_raw_messages_utf8_bytes", "INTEGER"),
                    ("analysis_raw_messages_state", "TEXT NOT NULL DEFAULT 'pending'"),
                ):
                    if column not in trace_columns:
                        self._conn.execute(f"ALTER TABLE traces ADD COLUMN {column} {ddl}")
            if "spans" in existing_tables:
                span_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(spans)")}
                if "parent_name" not in span_columns:
                    self._conn.execute("ALTER TABLE spans ADD COLUMN parent_name TEXT")
            if "drift_signals" in existing_tables:
                drift_columns = {
                    row[1] for row in self._conn.execute("PRAGMA table_info(drift_signals)")
                }
                if "run_id" not in drift_columns:
                    self._conn.execute("ALTER TABLE drift_signals ADD COLUMN run_id TEXT")
            self._conn.executescript(_SCHEMA)
            try:
                self._conn.execute("ALTER TABLE traces ADD COLUMN parent_span_id TEXT")
            except sqlite3.OperationalError:
                pass
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_parent_span ON traces(parent_span_id)"
            )
            # Idempotent column-add for in-place migration of older DBs
            for col, ddl in [
                ("effect_size_cliffs_delta", "REAL DEFAULT 0.0"),
                ("wasserstein_distance", "REAL DEFAULT 0.0"),
                ("psi", "REAL DEFAULT 0.0"),
                ("evaluator_fingerprint", "TEXT"),
                ("run_id", "TEXT"),
            ]:
                try:
                    self._conn.execute(f"ALTER TABLE drift_signals ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    # Column already exists — fine
                    pass
            for col, ddl in [
                ("evaluator_provider", "TEXT"),
                ("evaluator_config_json", "TEXT"),
                ("evaluator_fingerprint", "TEXT"),
                ("expected_dimensions_json", "TEXT"),
                ("status", "TEXT DEFAULT 'completed'"),
                ("error", "TEXT"),
            ]:
                try:
                    self._conn.execute(f"ALTER TABLE judgments ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass
            for col, ddl in [
                ("correct_examples", "INTEGER NOT NULL DEFAULT 0"),
                ("total_examples", "INTEGER NOT NULL DEFAULT 0"),
                ("example_agreement", "REAL"),
                ("example_confidence_low", "REAL"),
                ("example_confidence_high", "REAL"),
                ("label_agreement", "REAL"),
                ("method_version", "TEXT NOT NULL DEFAULT '1'"),
            ]:
                try:
                    self._conn.execute(f"ALTER TABLE evaluator_health ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass

    # -- Traces ------------------------------------------------------------

    def insert_trace(self, trace: Trace) -> None:
        sanitize_trace(trace)
        populate_trace_analysis_fields(trace)
        with self._lock:
            # SQLite UPSERT with an explicit column list. Mirrors the Postgres
            # adapter's ON CONFLICT DO UPDATE SET exactly so re-writes behave
            # identically across backends. The previous INSERT OR REPLACE did a
            # delete-then-reinsert, which WIPED any column not present on the
            # re-written row (notably cluster_id, assigned later by the intent
            # clusterer). COALESCE(excluded.cluster_id, traces.cluster_id) keeps
            # an already-assigned cluster_id when a later write carries NULL.
            self._conn.execute(
                """INSERT INTO traces (
                    trace_id, parent_span_id, started_at, ended_at, provider, operation,
                    request_model, response_model, input_tokens, output_tokens,
                    temperature, max_tokens, finish_reason, error, latency_ms,
                    prompt_redacted, response_redacted, raw_messages_json,
                    tenant_id, session_id, user_id_hash, cluster_id,
                    tags_json, cost_usd, analysis_started_at_us,
                    analysis_started_at_state, analysis_raw_messages_utf8_bytes,
                    analysis_raw_messages_state
                ) VALUES (
                    :trace_id, :parent_span_id, :started_at, :ended_at, :provider, :operation,
                    :request_model, :response_model, :input_tokens, :output_tokens,
                    :temperature, :max_tokens, :finish_reason, :error, :latency_ms,
                    :prompt_redacted, :response_redacted, :raw_messages_json,
                    :tenant_id, :session_id, :user_id_hash, :cluster_id,
                    :tags_json, :cost_usd, :analysis_started_at_us,
                    :analysis_started_at_state, :analysis_raw_messages_utf8_bytes,
                    :analysis_raw_messages_state
                ) ON CONFLICT(trace_id) DO UPDATE SET
                    ended_at          = excluded.ended_at,
                    response_model    = excluded.response_model,
                    input_tokens      = excluded.input_tokens,
                    output_tokens     = excluded.output_tokens,
                    finish_reason     = excluded.finish_reason,
                    error             = excluded.error,
                    latency_ms        = excluded.latency_ms,
                    prompt_redacted   = excluded.prompt_redacted,
                    response_redacted = excluded.response_redacted,
                    parent_span_id    = COALESCE(
                        excluded.parent_span_id, traces.parent_span_id
                    ),
                    cluster_id        = COALESCE(excluded.cluster_id, traces.cluster_id),
                    cost_usd          = excluded.cost_usd""",
                {
                    "trace_id": trace.trace_id,
                    "parent_span_id": trace.parent_span_id,
                    "started_at": _iso(trace.started_at),
                    "ended_at": _iso(trace.ended_at),
                    "provider": trace.provider,
                    "operation": trace.operation.value,
                    "request_model": trace.request_model,
                    "response_model": trace.response_model,
                    "input_tokens": trace.input_tokens,
                    "output_tokens": trace.output_tokens,
                    "temperature": trace.temperature,
                    "max_tokens": trace.max_tokens,
                    "finish_reason": trace.finish_reason,
                    "error": trace.error,
                    "latency_ms": trace.latency_ms,
                    "prompt_redacted": trace.prompt_redacted,
                    "response_redacted": trace.response_redacted,
                    "raw_messages_json": json.dumps(trace.raw_messages)
                    if trace.raw_messages
                    else None,
                    "tenant_id": trace.tenant_id,
                    "session_id": trace.session_id,
                    "user_id_hash": trace.user_id_hash,
                    "cluster_id": trace.cluster_id,
                    "tags_json": json.dumps(trace.tags),
                    "cost_usd": trace.cost_usd,
                    "analysis_started_at_us": trace.analysis_started_at_us,
                    "analysis_started_at_state": trace.analysis_started_at_state,
                    "analysis_raw_messages_utf8_bytes": (trace.analysis_raw_messages_utf8_bytes),
                    "analysis_raw_messages_state": trace.analysis_raw_messages_state,
                },
            )

    def _row_to_trace(self, row: sqlite3.Row) -> Trace:
        return Trace(
            trace_id=row["trace_id"],
            parent_span_id=row["parent_span_id"],
            started_at=_parse_iso(row["started_at"]) or datetime.now(timezone.utc),
            ended_at=_parse_iso(row["ended_at"]),
            provider=row["provider"] or "",
            operation=Operation(row["operation"] or "chat"),
            request_model=row["request_model"] or "",
            response_model=row["response_model"] or "",
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            temperature=row["temperature"],
            max_tokens=row["max_tokens"],
            finish_reason=row["finish_reason"],
            error=row["error"],
            latency_ms=row["latency_ms"],
            prompt_redacted=row["prompt_redacted"],
            response_redacted=row["response_redacted"],
            raw_messages=json.loads(row["raw_messages_json"]) if row["raw_messages_json"] else None,
            tenant_id=row["tenant_id"],
            session_id=row["session_id"],
            user_id_hash=row["user_id_hash"],
            cluster_id=row["cluster_id"],
            tags=json.loads(row["tags_json"]) if row["tags_json"] else {},
            cost_usd=row["cost_usd"],
            analysis_started_at_us=row["analysis_started_at_us"],
            analysis_started_at_state=row["analysis_started_at_state"],
            analysis_raw_messages_utf8_bytes=row["analysis_raw_messages_utf8_bytes"],
            analysis_raw_messages_state=row["analysis_raw_messages_state"],
        )

    def get_trace(self, trace_id: str) -> Trace | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,))
            row = cur.fetchone()
        return self._row_to_trace(row) if row else None

    def trace_exists(self, trace_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM traces WHERE trace_id = ? LIMIT 1", (trace_id,)
            ).fetchone()
        return row is not None

    def list_traces(
        self,
        *,
        tenant_id: str | None = None,
        cluster_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        clauses = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if cluster_id is not None:
            clauses.append("cluster_id = ?")
            params.append(cluster_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT * FROM traces {where} ORDER BY started_at DESC LIMIT ?",
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_trace(r) for r in rows]

    def delete_trace(self, trace_id: str) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM judgments WHERE trace_id = ?", (trace_id,))
                self._conn.execute("DELETE FROM user_signals WHERE trace_id = ?", (trace_id,))
                self._conn.execute("DELETE FROM traces WHERE trace_id = ?", (trace_id,))
                self._conn.execute(
                    """DELETE FROM spans
                       WHERE trace_id = ?
                         AND NOT EXISTS (
                           SELECT 1 FROM traces
                           WHERE traces.parent_span_id = spans.span_id
                         )""",
                    (trace_id,),
                )
                self._conn.execute(
                    """UPDATE spans SET trace_id = NULL
                       WHERE trace_id = ?
                         AND EXISTS (
                           SELECT 1 FROM traces
                           WHERE traces.parent_span_id = spans.span_id
                         )""",
                    (trace_id,),
                )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def prune_before(self, cutoff_iso: str) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "SELECT trace_id FROM traces WHERE started_at < ?",
                    (cutoff_iso,),
                )
                ids = [r["trace_id"] for r in cur.fetchall()]
                for tid in ids:
                    self._conn.execute("DELETE FROM judgments WHERE trace_id = ?", (tid,))
                    self._conn.execute("DELETE FROM user_signals WHERE trace_id = ?", (tid,))
                self._conn.execute("DELETE FROM traces WHERE started_at < ?", (cutoff_iso,))
                for tid in ids:
                    self._conn.execute(
                        """DELETE FROM spans
                           WHERE trace_id = ?
                             AND NOT EXISTS (
                               SELECT 1 FROM traces
                               WHERE traces.parent_span_id = spans.span_id
                             )""",
                        (tid,),
                    )
                    self._conn.execute(
                        """UPDATE spans SET trace_id = NULL
                           WHERE trace_id = ?
                             AND EXISTS (
                               SELECT 1 FROM traces
                               WHERE traces.parent_span_id = spans.span_id
                             )""",
                        (tid,),
                    )
                self._conn.execute(
                    """DELETE FROM spans
                       WHERE started_at < ?
                         AND (
                           trace_id IS NULL
                           OR NOT EXISTS (
                             SELECT 1 FROM traces
                             WHERE traces.trace_id = spans.trace_id
                           )
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM traces
                           WHERE traces.parent_span_id = spans.span_id
                         )""",
                    (cutoff_iso,),
                )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        return len(ids)

    # -- Judgments ---------------------------------------------------------

    def insert_judgment(self, judgment: Judgment) -> None:
        sanitize_judgment(judgment)
        dims_payload = [asdict(d) for d in judgment.dimensions]
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO judgments (
                    judgment_id, trace_id, rubric_name, rubric_version, created_at,
                    judge_models_json, dimensions_json, position_swap_consistent,
                    evaluator_provider, evaluator_config_json,
                    evaluator_fingerprint, expected_dimensions_json, status, error
                ) VALUES (
                    :judgment_id, :trace_id, :rubric_name, :rubric_version, :created_at,
                    :judge_models_json, :dimensions_json, :position_swap_consistent,
                    :evaluator_provider, :evaluator_config_json,
                    :evaluator_fingerprint, :expected_dimensions_json, :status, :error
                )""",
                {
                    "judgment_id": judgment.judgment_id,
                    "trace_id": judgment.trace_id,
                    "rubric_name": judgment.rubric_name,
                    "rubric_version": judgment.rubric_version,
                    "created_at": _iso(judgment.created_at),
                    "judge_models_json": json.dumps(judgment.judge_models),
                    "dimensions_json": json.dumps(dims_payload),
                    "position_swap_consistent": (
                        None
                        if judgment.position_swap_consistent is None
                        else int(judgment.position_swap_consistent)
                    ),
                    "evaluator_provider": judgment.evaluator_provider,
                    "evaluator_config_json": json.dumps(judgment.evaluator_config, sort_keys=True),
                    "evaluator_fingerprint": judgment.evaluator_fingerprint,
                    "expected_dimensions_json": json.dumps(judgment.expected_dimensions),
                    "status": judgment.status.value,
                    "error": judgment.error,
                },
            )

    def _row_to_judgment(self, row: sqlite3.Row) -> Judgment:
        dims_raw = json.loads(row["dimensions_json"]) if row["dimensions_json"] else []
        dims = [
            DimensionScore(
                name=d["name"],
                verdict=Verdict(d["verdict"]),
                reasoning=d.get("reasoning", ""),
                judge_model=d.get("judge_model", ""),
            )
            for d in dims_raw
        ]
        swap = row["position_swap_consistent"]
        return Judgment(
            judgment_id=row["judgment_id"],
            trace_id=row["trace_id"],
            rubric_name=row["rubric_name"] or "default",
            rubric_version=row["rubric_version"] or "1",
            created_at=_parse_iso(row["created_at"]) or datetime.now(timezone.utc),
            judge_models=json.loads(row["judge_models_json"]) if row["judge_models_json"] else [],
            dimensions=dims,
            evaluator_provider=row["evaluator_provider"] or "",
            evaluator_config=(
                json.loads(row["evaluator_config_json"]) if row["evaluator_config_json"] else {}
            ),
            evaluator_fingerprint=row["evaluator_fingerprint"] or "",
            expected_dimensions=(
                json.loads(row["expected_dimensions_json"])
                if row["expected_dimensions_json"]
                else []
            ),
            status=JudgmentStatus(row["status"] or "completed"),
            error=row["error"],
            position_swap_consistent=None if swap is None else bool(swap),
        )

    def list_judgments_for_cluster(
        self,
        cluster_id: str,
        *,
        since_iso: str | None = None,
        limit: int = 1000,
    ) -> list[Judgment]:
        params: list[object] = [cluster_id]
        clause = ""
        if since_iso is not None:
            clause = " AND j.created_at >= ?"
            params.append(since_iso)
        params.append(limit)
        with self._lock:
            cur = self._conn.execute(
                f"""SELECT j.* FROM judgments j
                    JOIN traces t ON j.trace_id = t.trace_id
                    WHERE t.cluster_id = ?{clause}
                    ORDER BY j.created_at DESC LIMIT ?""",
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_judgment(r) for r in rows]

    # -- Evaluator health -------------------------------------------------

    def insert_evaluator_health(self, record: EvaluatorHealthRecord) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO evaluator_health (
                    health_id, evaluated_at, evaluator_fingerprint,
                    sentinel_set_name, sentinel_set_fingerprint,
                    correct_examples, total_examples, example_agreement,
                    example_confidence_low, example_confidence_high,
                    correct_labels, total_labels, label_agreement,
                    status, error_count, method_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.health_id,
                    _iso(record.evaluated_at),
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
            where = "WHERE evaluator_fingerprint = ?"
            params.append(evaluator_fingerprint)
        params.append(limit)
        with self._lock:
            # `where` is one of two fixed strings; fingerprint and limit are bound.
            sql = (
                f"SELECT * FROM evaluator_health {where} "  # nosec B608
                "ORDER BY evaluated_at DESC LIMIT ?"
            )
            rows = self._conn.execute(
                sql,
                params,
            ).fetchall()
        records = []
        for row in rows:
            method_version = row["method_version"] or "1"
            legacy = method_version == "1"
            row_keys = set(row.keys())
            records.append(
                EvaluatorHealthRecord(
                    health_id=row["health_id"],
                    evaluated_at=_parse_iso(row["evaluated_at"]) or datetime.now(timezone.utc),
                    evaluator_fingerprint=row["evaluator_fingerprint"],
                    sentinel_set_name=row["sentinel_set_name"] or "",
                    sentinel_set_fingerprint=row["sentinel_set_fingerprint"],
                    correct_examples=0 if legacy else row["correct_examples"],
                    total_examples=0 if legacy else row["total_examples"],
                    example_agreement=None if legacy else row["example_agreement"],
                    example_confidence_low=(None if legacy else row["example_confidence_low"]),
                    example_confidence_high=(None if legacy else row["example_confidence_high"]),
                    correct_labels=row["correct_labels"],
                    total_labels=row["total_labels"],
                    label_agreement=(
                        row["agreement"]
                        if legacy and "agreement" in row_keys
                        else row["label_agreement"]
                    ),
                    status=(
                        EvaluatorHealthStatus.INSUFFICIENT_DATA
                        if legacy
                        else EvaluatorHealthStatus(row["status"])
                    ),
                    error_count=row["error_count"],
                    method_version=method_version,
                )
            )
        return records

    # -- Drift signals -----------------------------------------------------

    @staticmethod
    def _drift_signal_params(signal: DriftSignal) -> dict[str, object]:
        return {
            "signal_id": signal.signal_id,
            "detected_at": _iso(signal.detected_at),
            "cluster_id": signal.cluster_id,
            "dimension": signal.dimension,
            "direction": signal.direction.value,
            "evaluator_fingerprint": signal.evaluator_fingerprint,
            "run_id": signal.run_id or None,
            "statistic_name": signal.statistic_name,
            "statistic_value": signal.statistic_value,
            "p_value": signal.p_value,
            "p_value_adjusted": signal.p_value_adjusted,
            "effect_size_cohens_d": signal.effect_size_cohens_d,
            "effect_size_cliffs_delta": signal.effect_size_cliffs_delta,
            "wasserstein_distance": signal.wasserstein_distance,
            "psi": signal.psi,
            "sample_size_current": signal.sample_size_current,
            "sample_size_baseline": signal.sample_size_baseline,
            "contributing_layers_json": json.dumps(signal.contributing_layers),
            "example_trace_ids_json": json.dumps(signal.example_trace_ids),
            "recommended_action": signal.recommended_action,
        }

    def _insert_drift_signal_locked(self, signal: DriftSignal) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO drift_signals (
                signal_id, detected_at, cluster_id, dimension, direction,
                evaluator_fingerprint, run_id,
                statistic_name, statistic_value, p_value, p_value_adjusted,
                effect_size_cohens_d, effect_size_cliffs_delta,
                wasserstein_distance, psi,
                sample_size_current, sample_size_baseline,
                contributing_layers_json, example_trace_ids_json, recommended_action
            ) VALUES (
                :signal_id, :detected_at, :cluster_id, :dimension, :direction,
                :evaluator_fingerprint, :run_id,
                :statistic_name, :statistic_value, :p_value, :p_value_adjusted,
                :effect_size_cohens_d, :effect_size_cliffs_delta,
                :wasserstein_distance, :psi,
                :sample_size_current, :sample_size_baseline,
                :contributing_layers_json, :example_trace_ids_json, :recommended_action
            )""",
            self._drift_signal_params(signal),
        )

    def insert_drift_signal(self, signal: DriftSignal) -> None:
        with self._lock:
            self._insert_drift_signal_locked(signal)

    def replace_drift_run(
        self,
        run: DriftRun,
        signals: list[DriftSignal],
    ) -> None:
        _validate_drift_run_snapshot(run, signals)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT evaluator_fingerprint FROM drift_runs WHERE run_id = ?",
                    (run.run_id,),
                ).fetchone()
                if existing and existing["evaluator_fingerprint"] != run.evaluator_fingerprint:
                    raise ValueError("run_id already belongs to another evaluator")
                for signal in signals:
                    owner = self._conn.execute(
                        "SELECT run_id FROM drift_signals WHERE signal_id = ?",
                        (signal.signal_id,),
                    ).fetchone()
                    if owner and owner["run_id"] and owner["run_id"] != run.run_id:
                        raise ValueError("signal_id already belongs to another drift run")
                self._conn.execute(
                    """INSERT INTO drift_runs (
                        run_id, analysis_time, completed_at,
                        evaluator_fingerprint, signal_count
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        analysis_time = excluded.analysis_time,
                        completed_at = excluded.completed_at,
                        evaluator_fingerprint = excluded.evaluator_fingerprint,
                        signal_count = excluded.signal_count""",
                    (
                        run.run_id,
                        _iso(run.analysis_time),
                        _iso(run.completed_at),
                        run.evaluator_fingerprint,
                        run.signal_count,
                    ),
                )
                self._conn.execute("DELETE FROM drift_signals WHERE run_id = ?", (run.run_id,))
                for signal in signals:
                    self._insert_drift_signal_locked(signal)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def get_latest_drift_run_snapshot(
        self,
        evaluator_fingerprint: str,
    ) -> tuple[DriftRun, list[DriftSignal]] | None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    """SELECT * FROM drift_runs
                       WHERE evaluator_fingerprint = ?
                       ORDER BY analysis_time DESC, completed_at DESC, run_id DESC
                       LIMIT 1""",
                    (evaluator_fingerprint,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                signal_rows = self._conn.execute(
                    "SELECT * FROM drift_signals WHERE run_id = ? ORDER BY signal_id",
                    (row["run_id"],),
                ).fetchall()
                run = DriftRun(
                    run_id=row["run_id"],
                    analysis_time=_parse_iso(row["analysis_time"]) or datetime.now(timezone.utc),
                    completed_at=_parse_iso(row["completed_at"]) or datetime.now(timezone.utc),
                    evaluator_fingerprint=row["evaluator_fingerprint"],
                    signal_count=row["signal_count"],
                )
                signals = [self._row_to_drift_signal(item) for item in signal_rows]
                if len(signals) != run.signal_count:
                    raise RuntimeError("stored drift run signal_count is inconsistent")
                self._conn.execute("COMMIT")
                return run, signals
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def delete_drift_signals_between(
        self,
        start: datetime,
        end: datetime,
        *,
        evaluator_fingerprint: str | None = None,
    ) -> None:
        with self._lock:
            match_sql = "detected_at >= ? AND detected_at < ?"
            params: tuple = (_iso(start), _iso(end))
            if evaluator_fingerprint is not None:
                match_sql += " AND evaluator_fingerprint = ?"
                params += (evaluator_fingerprint,)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                run_ids = [
                    row["run_id"]
                    for row in self._conn.execute(
                        f"SELECT DISTINCT run_id FROM drift_signals "  # nosec B608
                        f"WHERE {match_sql} AND run_id IS NOT NULL "
                        "AND run_id <> ''",
                        params,
                    ).fetchall()
                ]
                for run_id in run_ids:
                    self._conn.execute("DELETE FROM drift_signals WHERE run_id = ?", (run_id,))
                    self._conn.execute("DELETE FROM drift_runs WHERE run_id = ?", (run_id,))
                self._conn.execute(
                    f"DELETE FROM drift_signals WHERE {match_sql} "  # nosec B608
                    "AND (run_id IS NULL OR run_id = '')",
                    params,
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def list_drift_signals(self, *, limit: int = 100) -> list[DriftSignal]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM drift_signals ORDER BY detected_at DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        return [self._row_to_drift_signal(r) for r in rows]

    def _row_to_drift_signal(self, r: sqlite3.Row) -> DriftSignal:
        def _get(row, key, default=0.0):
            try:
                value = row[key]
                return value if value is not None else default
            except (IndexError, KeyError):
                return default

        return DriftSignal(
            signal_id=r["signal_id"],
            detected_at=_parse_iso(r["detected_at"]) or datetime.now(timezone.utc),
            cluster_id=r["cluster_id"] or "",
            dimension=r["dimension"] or "",
            direction=DriftDirection(r["direction"] or "change"),
            evaluator_fingerprint=_get(r, "evaluator_fingerprint", ""),
            run_id=_get(r, "run_id", ""),
            statistic_name=r["statistic_name"] or "",
            statistic_value=r["statistic_value"] or 0.0,
            p_value=r["p_value"] or 1.0,
            p_value_adjusted=r["p_value_adjusted"] or 1.0,
            effect_size_cohens_d=r["effect_size_cohens_d"] or 0.0,
            effect_size_cliffs_delta=_get(r, "effect_size_cliffs_delta", 0.0),
            wasserstein_distance=_get(r, "wasserstein_distance", 0.0),
            psi=_get(r, "psi", 0.0),
            sample_size_current=r["sample_size_current"] or 0,
            sample_size_baseline=r["sample_size_baseline"] or 0,
            contributing_layers=json.loads(r["contributing_layers_json"])
            if r["contributing_layers_json"]
            else [],
            example_trace_ids=json.loads(r["example_trace_ids_json"])
            if r["example_trace_ids_json"]
            else [],
            recommended_action=r["recommended_action"] or "",
        )

    # -- Spans -------------------------------------------------------------

    def insert_span(self, span: SpanRecord) -> None:
        sanitize_span(span)
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO spans (
                    span_id, name, trace_id, parent_name, started_at,
                    ended_at, duration_ms, attributes_json, error
                ) VALUES (
                    :span_id, :name, :trace_id, :parent_name, :started_at,
                    :ended_at, :duration_ms, :attributes_json, :error
                )""",
                {
                    "span_id": span.span_id,
                    "name": span.name,
                    "trace_id": span.trace_id,
                    "parent_name": span.parent_name,
                    "started_at": _iso(span.started_at),
                    "ended_at": _iso(span.ended_at),
                    "duration_ms": span.duration_ms,
                    "attributes_json": json.dumps(span.attributes),
                    "error": span.error,
                },
            )

    def _row_to_span(self, row: sqlite3.Row) -> SpanRecord:
        return SpanRecord(
            span_id=row["span_id"],
            name=row["name"] or "",
            trace_id=row["trace_id"],
            parent_name=row["parent_name"],
            started_at=_parse_iso(row["started_at"]) or datetime.now(timezone.utc),
            ended_at=_parse_iso(row["ended_at"]),
            duration_ms=row["duration_ms"],
            attributes=json.loads(row["attributes_json"]) if row["attributes_json"] else {},
            error=row["error"],
        )

    def list_spans(self, *, trace_id: str | None = None, limit: int = 100) -> list[SpanRecord]:
        clauses = []
        params: list[object] = []
        if trace_id is not None:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT * FROM spans {where} ORDER BY started_at DESC LIMIT ?",
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_span(r) for r in rows]

    # -- User signals ------------------------------------------------------

    def insert_user_signal(self, sig: UserSignalRecord) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO user_signals (
                    signal_id, trace_id, kind, created_at
                ) VALUES (:signal_id, :trace_id, :kind, :created_at)""",
                {
                    "signal_id": sig.signal_id,
                    "trace_id": sig.trace_id,
                    "kind": sig.kind,
                    "created_at": _iso(sig.created_at),
                },
            )

    def list_user_signals(self, *, limit: int = 1000) -> list[UserSignalRecord]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM user_signals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        return [
            UserSignalRecord(
                signal_id=r["signal_id"],
                trace_id=r["trace_id"] or "",
                kind=r["kind"] or "",
                created_at=_parse_iso(r["created_at"]) or datetime.now(timezone.utc),
            )
            for r in rows
        ]

    # -- Cluster registry --------------------------------------------------

    def save_cluster_registry(self, version: str, payload_json: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO cluster_registries (version, payload_json, updated_at)
                   VALUES (?, ?, ?)""",
                (version, payload_json, _iso(datetime.now(timezone.utc))),
            )

    def load_cluster_registry(self, version: str) -> str | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload_json FROM cluster_registries WHERE version = ?",
                (version,),
            )
            row = cur.fetchone()
        return row["payload_json"] if row else None

    # -- Versioned cluster registry ---------------------------------------

    @staticmethod
    def _row_to_cluster_version(row: sqlite3.Row) -> ClusterRegistryVersion:
        return ClusterRegistryVersion(
            tenant_id=row["tenant_id"],
            version_id=row["version_id"],
            parent_version_id=row["parent_version_id"],
            strategy=row["strategy"],
            cutoff=_parse_iso(row["cutoff"]) or datetime.now(timezone.utc),
            lookback_days=row["lookback_days"],
            fit_definition_json=row["fit_definition_json"],
            fit_definition_fingerprint=row["fit_definition_fingerprint"],
            preview_report_json=row["preview_report_json"],
            created_at=_parse_iso(row["created_at"]) or datetime.now(timezone.utc),
            created_by=row["created_by"],
        )

    @staticmethod
    def _row_to_registry_cluster(row: sqlite3.Row) -> ClusterRegistryCluster:
        return ClusterRegistryCluster(
            tenant_id=row["tenant_id"],
            version_id=row["version_id"],
            cluster_id=row["cluster_id"],
            kind=row["kind"],
            centroid=json.loads(row["centroid_json"]) if row["centroid_json"] else None,
            radius=row["radius"],
            member_count=row["member_count"],
            outlier_count=row["outlier_count"],
        )

    @staticmethod
    def _row_to_cluster_assignment(row: sqlite3.Row) -> TraceClusterAssignment:
        return TraceClusterAssignment(
            tenant_id=row["tenant_id"],
            version_id=row["version_id"],
            trace_id=row["trace_id"],
            origin=row["origin"],
            status=row["status"],
            cluster_id=row["cluster_id"],
            cluster_kind=row["cluster_kind"],
            reason=row["reason"],
            distance=row["distance"],
            assigned_at=_parse_iso(row["assigned_at"]) or datetime.now(timezone.utc),
        )

    @staticmethod
    def _row_to_cluster_identity(row: sqlite3.Row) -> ClusterIdentity:
        return ClusterIdentity(
            tenant_id=row["tenant_id"],
            cluster_id=row["cluster_id"],
            kind=row["kind"],
            lifecycle=row["lifecycle"],
            explicit_key=row["explicit_key"],
            display_name=row["display_name"],
            last_model_fingerprint=row["last_model_fingerprint"],
            last_centroid=(
                json.loads(row["last_centroid_json"]) if row["last_centroid_json"] else None
            ),
            last_version_id=row["last_version_id"],
            created_at=_parse_iso(row["created_at"]) or datetime.now(timezone.utc),
            created_by=row["created_by"],
            updated_at=_parse_iso(row["updated_at"]) or datetime.now(timezone.utc),
            updated_by=row["updated_by"],
        )

    @staticmethod
    def _row_to_cluster_event(row: sqlite3.Row) -> ClusterRegistryEvent:
        return ClusterRegistryEvent(
            tenant_id=row["tenant_id"],
            event_id=row["event_id"],
            action=row["action"],
            from_version_id=row["from_version_id"],
            to_version_id=row["to_version_id"],
            pointer_generation=row["pointer_generation"],
            created_at=_parse_iso(row["created_at"]) or datetime.now(timezone.utc),
            actor=row["actor"],
            details_json=row["details_json"],
        )

    def insert_cluster_preview(
        self,
        version: ClusterRegistryVersion,
        identities: list[ClusterIdentity],
        clusters: list[ClusterRegistryCluster],
        assignments: list[TraceClusterAssignment],
    ) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO active_cluster_registry "
                    "(tenant_id,version_id,generation) VALUES (?,NULL,0)",
                    (version.tenant_id,),
                )
                for identity in identities:
                    if (
                        identity.tenant_id != version.tenant_id
                        or identity.lifecycle != "provisional"
                    ):
                        raise ValueError("cluster preview tenant mismatch")
                    self._conn.execute(
                        """INSERT OR IGNORE INTO cluster_identities (
                            tenant_id,cluster_id,kind,lifecycle,explicit_key,display_name,
                            last_model_fingerprint,last_centroid_json,last_version_id,
                            created_at,created_by,updated_at,updated_by
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                            _iso(identity.created_at),
                            identity.created_by,
                            _iso(identity.updated_at),
                            identity.updated_by,
                        ),
                    )
                    row = self._conn.execute(
                        "SELECT kind,explicit_key FROM cluster_identities "
                        "WHERE tenant_id=? AND cluster_id=?",
                        (identity.tenant_id, identity.cluster_id),
                    ).fetchone()
                    if row is None or (row["kind"], row["explicit_key"]) != (
                        identity.kind,
                        identity.explicit_key,
                    ):
                        raise ValueError("cluster identity conflict")
                self._conn.execute(
                    """INSERT INTO cluster_registry_versions (
                        tenant_id,version_id,parent_version_id,strategy,cutoff,
                        lookback_days,fit_definition_json,fit_definition_fingerprint,
                        preview_report_json,created_at,created_by
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        version.tenant_id,
                        version.version_id,
                        version.parent_version_id,
                        version.strategy,
                        _iso(version.cutoff),
                        version.lookback_days,
                        version.fit_definition_json,
                        version.fit_definition_fingerprint,
                        version.preview_report_json,
                        _iso(version.created_at),
                        version.created_by,
                    ),
                )
                for cluster in clusters:
                    if (
                        cluster.tenant_id != version.tenant_id
                        or cluster.version_id != version.version_id
                    ):
                        raise ValueError("cluster preview version mismatch")
                    self._conn.execute(
                        """INSERT INTO cluster_registry_clusters (
                            tenant_id,version_id,cluster_id,kind,centroid_json,radius,
                            member_count,outlier_count
                        ) VALUES (?,?,?,?,?,?,?,?)""",
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
                self._insert_cluster_assignments_locked(
                    version.tenant_id,
                    assignments,
                    expected_version_id=version.version_id,
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def get_cluster_registry_version(
        self,
        authorized_tenant: str,
        version_id: str,
    ) -> ClusterRegistryVersion | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cluster_registry_versions WHERE tenant_id=? AND version_id=?",
                (authorized_tenant, version_id),
            ).fetchone()
        return self._row_to_cluster_version(row) if row else None

    def list_cluster_registry_clusters(
        self,
        authorized_tenant: str,
        version_id: str,
    ) -> list[ClusterRegistryCluster]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cluster_registry_clusters WHERE tenant_id=? AND version_id=? "
                "ORDER BY cluster_id",
                (authorized_tenant, version_id),
            ).fetchall()
        return [self._row_to_registry_cluster(row) for row in rows]

    def _insert_cluster_assignments_locked(
        self,
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
            self._conn.execute(
                """INSERT OR IGNORE INTO trace_cluster_assignments (
                    tenant_id,version_id,trace_id,origin,status,cluster_id,
                    cluster_kind,reason,distance,assigned_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
                    _iso(assignment.assigned_at),
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM trace_cluster_assignments "
                "WHERE tenant_id=? AND version_id=? AND trace_id=?",
                (assignment.tenant_id, assignment.version_id, assignment.trace_id),
            ).fetchone()
            if row is None or self._row_to_cluster_assignment(row) != assignment:
                raise ValueError("immutable assignment conflict")

    def insert_trace_cluster_assignments(
        self,
        authorized_tenant: str,
        assignments: list[TraceClusterAssignment],
    ) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._insert_cluster_assignments_locked(authorized_tenant, assignments)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

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
            "SELECT * FROM trace_cluster_assignments WHERE tenant_id=? AND version_id=? "
            "ORDER BY trace_id"
        )
        params: tuple[object, ...] = (authorized_tenant, version_id)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += (limit, offset)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_cluster_assignment(row) for row in rows]

    def list_judgments_for_registry_cluster(
        self,
        authorized_tenant: str,
        version_id: str,
        cluster_id: str,
        *,
        limit: int = 1_000,
    ) -> list[Judgment]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT j.* FROM judgments j JOIN trace_cluster_assignments a
                  ON a.trace_id=j.trace_id WHERE a.tenant_id=? AND a.version_id=?
                  AND a.status='assigned' AND a.cluster_id=?
                  ORDER BY j.created_at DESC LIMIT ?""",
                (authorized_tenant, version_id, cluster_id, limit),
            ).fetchall()
        return [self._row_to_judgment(row) for row in rows]

    def insert_conversation_drift_snapshot(
        self,
        run: ConversationDriftRun,
        samples: list[ConversationDriftSample],
        signals: list[ConversationDriftSignal],
    ) -> None:
        _validate_conversation_drift_snapshot(run, samples, signals)
        with self._lock:
            existing = self.get_conversation_drift_snapshot(run.tenant_id, run.run_id)
            if existing is not None:
                if existing != (run, samples, signals):
                    raise ValueError("immutable conversation snapshot conflict")
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO conversation_drift_runs VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    run_values(run, datetime_to_utc_us),
                )
                self._conn.executemany(
                    """INSERT INTO conversation_drift_samples VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [sample_values(item, datetime_to_utc_us) for item in samples],
                )
                self._conn.executemany(
                    """INSERT INTO conversation_drift_signals VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [signal_values(item) for item in signals],
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def get_conversation_drift_snapshot(
        self,
        authorized_tenant: str,
        run_id: str,
    ) -> (
        tuple[
            ConversationDriftRun,
            list[ConversationDriftSample],
            list[ConversationDriftSignal],
        ]
        | None
    ):
        with self._lock:
            run_row = self._conn.execute(
                f"SELECT {RUN_COLUMNS} FROM conversation_drift_runs "
                "WHERE tenant_id=? AND run_id=?",  # nosec B608
                (authorized_tenant, run_id),
            ).fetchone()
            if run_row is None:
                return None
            sample_rows = self._conn.execute(
                f"SELECT {SAMPLE_COLUMNS} FROM conversation_drift_samples "
                "WHERE tenant_id=? AND run_id=? "
                "ORDER BY cluster_id,session_ordinal",
                (authorized_tenant, run_id),
            ).fetchall()
            signal_rows = self._conn.execute(
                f"SELECT {SIGNAL_COLUMNS} FROM conversation_drift_signals "
                "WHERE tenant_id=? AND run_id=? "
                "ORDER BY signal_id",
                (authorized_tenant, run_id),
            ).fetchall()
        result = (
            run_from_row(run_row, _parse_utc_us),
            [sample_from_row(row, _parse_utc_us) for row in sample_rows],
            [signal_from_row(row) for row in signal_rows],
        )
        _validate_conversation_drift_snapshot(*result)
        return result

    def list_conversation_drift_runs(
        self,
        authorized_tenant: str,
        *,
        limit: int = 100,
    ) -> list[ConversationDriftRun]:
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("conversation run limit must be in [1,1000]")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {RUN_COLUMNS} FROM conversation_drift_runs WHERE tenant_id=? "
                "ORDER BY completed_at DESC,run_id DESC LIMIT ?",
                (authorized_tenant, limit),
            ).fetchall()
        return [run_from_row(row, _parse_utc_us) for row in rows]

    @staticmethod
    def _decode_bounded_text(value: object) -> str | None:
        if value is None:
            return None
        try:
            return bytes(value).decode("utf-8")
        except (TypeError, UnicodeError):
            return None

    def list_conversation_trace_candidates(
        self,
        authorized_tenant: str,
        registry_version: str,
        baseline_start_us: int,
        current_end_us: int,
        *,
        target_workload: str,
        limit: int,
    ) -> list[ConversationTraceCandidate]:
        if not 1 <= limit <= 50_001:
            raise ValueError("conversation candidate limit must be in [1,50001]")
        workload_path = '$."verdict.workload"'
        sampling_path = '$."verdict.success_sampling"'
        stream_path = '$."verdict.stream_completion"'

        def json_state(path: str) -> str:
            expression = f"json_type(t.tags_json,'{path}')"
            return (
                f"CASE {expression} WHEN 'text' THEN 'string' "
                "WHEN 'integer' THEN 'number' WHEN 'real' THEN 'number' "
                "WHEN 'true' THEN 'boolean' WHEN 'false' THEN 'boolean' "
                f"ELSE COALESCE({expression},'missing') END"
            )

        with self._lock:
            rows = self._conn.execute(
                f"""SELECT
                  length(CAST(t.trace_id AS BLOB)),
                  CASE WHEN length(CAST(t.trace_id AS BLOB))<=256 THEN CAST(t.trace_id AS BLOB) END,
                  t.analysis_started_at_us,
                  {json_state(workload_path)},
                  CASE WHEN json_type(t.tags_json,'{workload_path}')='text'
                    THEN length(CAST(json_extract(t.tags_json,'{workload_path}') AS BLOB)) END,
                  CASE WHEN json_type(t.tags_json,'{workload_path}')='text' AND
                    length(CAST(json_extract(t.tags_json,'{workload_path}') AS BLOB))<=64
                    THEN CAST(json_extract(t.tags_json,'{workload_path}') AS BLOB) END,
                  CASE WHEN t.session_id IS NULL THEN 'missing'
                    WHEN typeof(t.session_id)='text' THEN 'string'
                    ELSE typeof(t.session_id) END,
                  CASE WHEN typeof(t.session_id)='text' THEN length(CAST(t.session_id AS BLOB)) END,
                  CASE WHEN typeof(t.session_id)='text' AND length(CAST(t.session_id AS BLOB))<=256
                    THEN CAST(t.session_id AS BLOB) END,
                  {json_state(sampling_path)},
                  CASE WHEN json_type(t.tags_json,'{sampling_path}')='text'
                    THEN length(CAST(json_extract(t.tags_json,'{sampling_path}') AS BLOB)) END,
                  CASE WHEN json_type(t.tags_json,'{sampling_path}')='text' AND
                    length(CAST(json_extract(t.tags_json,'{sampling_path}') AS BLOB))<=16
                    THEN CAST(json_extract(t.tags_json,'{sampling_path}') AS BLOB) END,
                  {json_state(stream_path)},
                  CASE WHEN json_type(t.tags_json,'{stream_path}')='text'
                    THEN length(CAST(json_extract(t.tags_json,'{stream_path}') AS BLOB)) END,
                  CASE WHEN json_type(t.tags_json,'{stream_path}')='text' AND
                    length(CAST(json_extract(t.tags_json,'{stream_path}') AS BLOB))<=16
                    THEN CAST(json_extract(t.tags_json,'{stream_path}') AS BLOB) END,
                  a.status,a.reason,a.cluster_id,t.error IS NULL,
                  t.prompt_redacted IS NOT NULL,typeof(t.prompt_redacted)='text',
                  CASE WHEN t.prompt_redacted IS NOT NULL
                    THEN length(CAST(t.prompt_redacted AS BLOB)) END,
                  t.response_redacted IS NOT NULL,typeof(t.response_redacted)='text',
                  CASE WHEN t.response_redacted IS NOT NULL
                    THEN length(CAST(t.response_redacted AS BLOB)) END
                FROM traces t LEFT JOIN trace_cluster_assignments a
                  ON a.tenant_id=? AND a.version_id=? AND a.trace_id=t.trace_id
                WHERE (t.tenant_id=? OR (?='__verdict_local__' AND t.tenant_id IS NULL))
                  AND t.ended_at IS NOT NULL AND t.analysis_started_at_state='valid'
                  AND t.analysis_started_at_us>=? AND t.analysis_started_at_us<?
                  AND json_type(t.tags_json,'{workload_path}')='text'
                  AND json_extract(t.tags_json,'{workload_path}')=?
                ORDER BY t.analysis_started_at_us,t.trace_id LIMIT ?""",  # nosec B608
                (
                    authorized_tenant,
                    registry_version,
                    authorized_tenant,
                    authorized_tenant,
                    baseline_start_us,
                    current_end_us,
                    target_workload,
                    limit,
                ),
            ).fetchall()
        return [
            ConversationTraceCandidate(
                row[0],
                self._decode_bounded_text(row[1]),
                authorized_tenant,
                row[2],
                row[3],
                row[4],
                self._decode_bounded_text(row[5]),
                row[6],
                row[7],
                self._decode_bounded_text(row[8]),
                row[9],
                row[10],
                self._decode_bounded_text(row[11]),
                row[12],
                row[13],
                self._decode_bounded_text(row[14]),
                row[15],
                row[16],
                row[17],
                bool(row[18]),
                bool(row[19]),
                bool(row[20]),
                row[21],
                bool(row[22]),
                bool(row[23]),
                row[24],
            )
            for row in rows
        ]

    def get_conversation_trace_contents(
        self,
        authorized_tenant: str,
        trace_ids: list[str],
    ) -> dict[str, ConversationTraceContent]:
        result: dict[str, ConversationTraceContent] = {}
        with self._lock:
            for start in range(0, len(trace_ids), 500):
                batch = trace_ids[start : start + 500]
                if not batch:
                    continue
                placeholders = ",".join("?" for _ in batch)
                rows = self._conn.execute(
                    f"""SELECT trace_id,CAST(prompt_redacted AS BLOB),
                      CAST(response_redacted AS BLOB) FROM traces
                      WHERE (tenant_id=? OR (?='__verdict_local__' AND tenant_id IS NULL))
                        AND trace_id IN ({placeholders})
                        AND typeof(prompt_redacted)='text' AND typeof(response_redacted)='text'
                        AND length(CAST(prompt_redacted AS BLOB))+
                            length(CAST(response_redacted AS BLOB))<=16384""",  # nosec B608
                    (authorized_tenant, authorized_tenant, *batch),
                ).fetchall()
                for row in rows:
                    prompt = self._decode_bounded_text(row[1])
                    response = self._decode_bounded_text(row[2])
                    if prompt is not None and response is not None:
                        result[row[0]] = ConversationTraceContent(row[0], prompt, response)
        return result

    def insert_cluster_registry_event(self, event: ClusterRegistryEvent) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO cluster_registry_events (
                    tenant_id,event_id,action,from_version_id,to_version_id,
                    pointer_generation,created_at,actor,details_json
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    event.tenant_id,
                    event.event_id,
                    event.action,
                    event.from_version_id,
                    event.to_version_id,
                    event.pointer_generation,
                    _iso(event.created_at),
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
        params: list[object] = [authorized_tenant]
        where = "tenant_id=?"
        if version_id is not None:
            where += " AND (from_version_id=? OR to_version_id=?)"
            params.extend([version_id, version_id])
        with self._lock:
            sql = (
                f"SELECT * FROM cluster_registry_events WHERE {where} "  # nosec B608
                "ORDER BY created_at,event_id"
            )
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_cluster_event(row) for row in rows]

    def get_active_cluster_registry(
        self,
        authorized_tenant: str,
    ) -> ActiveClusterRegistry:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO active_cluster_registry "
                "(tenant_id,version_id,generation) VALUES (?,NULL,0)",
                (authorized_tenant,),
            )
            row = self._conn.execute(
                "SELECT * FROM active_cluster_registry WHERE tenant_id=?",
                (authorized_tenant,),
            ).fetchone()
        return ActiveClusterRegistry(
            row["tenant_id"],
            row["version_id"],
            row["generation"],
            _parse_iso(row["activated_at"]),
            row["activated_by"],
        )

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
        now = datetime.now(timezone.utc)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO active_cluster_registry "
                    "(tenant_id,version_id,generation) VALUES (?,NULL,0)",
                    (authorized_tenant,),
                )
                pointer = self._conn.execute(
                    "SELECT * FROM active_cluster_registry WHERE tenant_id=?",
                    (authorized_tenant,),
                ).fetchone()
                if pointer["generation"] != expected_generation:
                    raise ValueError("cluster registry generation conflict")
                version = self._conn.execute(
                    "SELECT * FROM cluster_registry_versions WHERE tenant_id=? AND version_id=?",
                    (authorized_tenant, version_id),
                ).fetchone()
                if version is None:
                    raise ValueError("unknown cluster registry version")
                if action == "activated" and version["parent_version_id"] != pointer["version_id"]:
                    raise ValueError("cluster registry parent conflict")
                validation = self._conn.execute(
                    """SELECT action FROM cluster_registry_events
                       WHERE tenant_id=? AND to_version_id=?
                         AND action IN ('validated','validation_failed')
                       ORDER BY created_at DESC,event_id DESC LIMIT 1""",
                    (authorized_tenant, version_id),
                ).fetchone()
                if validation is None or validation["action"] != "validated":
                    raise ValueError("cluster registry version is not validated")
                config = json.loads(version["fit_definition_json"]).get("config", {})
                cutoff = _parse_iso(version["cutoff"])
                if cutoff is None:
                    raise ValueError("cluster registry cutoff is invalid")
                rows = self.list_cluster_trace_candidates(
                    authorized_tenant,
                    datetime_to_utc_us(cutoff - timedelta(days=version["lookback_days"])),
                    datetime_to_utc_us(cutoff),
                    target_workload=config.get("target_workload"),
                    limit=config.get("max_fit_candidates", 50_000) + 1,
                )
                candidate_ids = [row.trace_id for row in rows if row.trace_id is not None]
                assigned_ids = {
                    row[0]
                    for row in self._conn.execute(
                        "SELECT trace_id FROM trace_cluster_assignments "
                        "WHERE tenant_id=? AND version_id=?",
                        (authorized_tenant, version_id),
                    ).fetchall()
                }
                if (
                    self.count_pending_analysis_rows(authorized_tenant)
                    or len(rows) > config.get("max_fit_candidates", 50_000)
                    or len(candidate_ids) != len(rows)
                    or cluster_candidate_digest(candidate_ids) != expected_candidate_digest
                    or not set(candidate_ids) <= assigned_ids
                ):
                    raise ValueError("cluster registry coverage changed")
                identity_counts = dict(
                    self._conn.execute(
                        """SELECT kind,COUNT(*) FROM cluster_identities i
                       WHERE tenant_id=? AND (lifecycle='active' OR EXISTS (
                         SELECT 1 FROM cluster_registry_clusters c
                         WHERE c.tenant_id=i.tenant_id AND c.cluster_id=i.cluster_id
                           AND c.version_id=?)) GROUP BY kind""",
                        (authorized_tenant, version_id),
                    ).fetchall()
                )
                if identity_counts.get("explicit", 0) > config.get(
                    "max_explicit_identities_per_tenant", 10_000
                ) or identity_counts.get("semantic", 0) > config.get(
                    "max_semantic_identities_per_tenant", 5_000
                ):
                    raise ValueError("identity_limit")
                generation = expected_generation + 1
                event = ClusterRegistryEvent(
                    tenant_id=authorized_tenant,
                    action=action,
                    from_version_id=pointer["version_id"],
                    to_version_id=version_id,
                    pointer_generation=generation,
                    created_at=now,
                    actor=actor,
                )
                self._conn.execute(
                    """INSERT INTO cluster_registry_events (
                        tenant_id,event_id,action,from_version_id,to_version_id,
                        pointer_generation,created_at,actor,details_json
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        event.tenant_id,
                        event.event_id,
                        event.action,
                        event.from_version_id,
                        event.to_version_id,
                        event.pointer_generation,
                        _iso(event.created_at),
                        event.actor,
                        event.details_json,
                    ),
                )
                self._conn.execute(
                    """UPDATE active_cluster_registry SET version_id=?,generation=?,
                       activated_at=?,activated_by=? WHERE tenant_id=?""",
                    (version_id, generation, _iso(now), actor, authorized_tenant),
                )
                clusters = self._conn.execute(
                    "SELECT cluster_id,kind,centroid_json FROM cluster_registry_clusters "
                    "WHERE tenant_id=? AND version_id=?",
                    (authorized_tenant, version_id),
                ).fetchall()
                for cluster in clusters:
                    self._conn.execute(
                        """UPDATE cluster_identities SET lifecycle='active',
                           last_model_fingerprint=?,last_centroid_json=?,last_version_id=?,
                           updated_at=?,updated_by=?
                           WHERE tenant_id=? AND cluster_id=? AND kind=?""",
                        (
                            json.loads(version["fit_definition_json"]).get("model_fingerprint")
                            or None,
                            cluster["centroid_json"],
                            version_id,
                            _iso(now),
                            actor,
                            authorized_tenant,
                            cluster["cluster_id"],
                            cluster["kind"],
                        ),
                    )
                valid = self._conn.execute(
                    """SELECT COUNT(*) FROM cluster_registry_clusters c
                       JOIN cluster_identities i ON i.tenant_id=c.tenant_id
                        AND i.cluster_id=c.cluster_id AND i.kind=c.kind
                       WHERE c.tenant_id=? AND c.version_id=? AND i.lifecycle='active'
                        AND i.last_version_id=?
                        AND i.last_model_fingerprint IS ?
                        AND i.last_centroid_json IS c.centroid_json""",
                    (
                        authorized_tenant,
                        version_id,
                        version_id,
                        json.loads(version["fit_definition_json"]).get("model_fingerprint") or None,
                    ),
                ).fetchone()[0]
                if valid != len(clusters):
                    raise ValueError("cluster activation identity invariant failed")
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return ActiveClusterRegistry(authorized_tenant, version_id, generation, now, actor)

    def rename_cluster_identity(
        self, authorized_tenant: str, cluster_id: str, display_name: str, *, actor: str
    ) -> None:
        event = ClusterRegistryEvent(
            authorized_tenant,
            action="renamed",
            actor=actor,
            details_json=json.dumps({"cluster_id": cluster_id}, sort_keys=True),
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO cluster_registry_events VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        event.tenant_id,
                        event.event_id,
                        event.action,
                        None,
                        None,
                        None,
                        _iso(event.created_at),
                        event.actor,
                        event.details_json,
                    ),
                )
                cursor = self._conn.execute(
                    "UPDATE cluster_identities SET display_name=?,updated_at=?,updated_by=? "
                    "WHERE tenant_id=? AND cluster_id=?",
                    (display_name, _iso(event.created_at), actor, authorized_tenant, cluster_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("unknown cluster identity")
                row = self._conn.execute(
                    "SELECT display_name FROM cluster_identities WHERE tenant_id=? AND cluster_id=?",
                    (authorized_tenant, cluster_id),
                ).fetchone()
                if row[0] != display_name:
                    raise ValueError("cluster rename invariant failed")
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

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
        params: list[object] = [authorized_tenant]
        where = "tenant_id=?"
        if cluster_ids is not None:
            if not cluster_ids:
                return []
            where += f" AND cluster_id IN ({','.join('?' for _ in cluster_ids)})"
            params.extend(cluster_ids)
        sql = f"SELECT * FROM cluster_identities WHERE {where} ORDER BY cluster_id"  # nosec B608
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_cluster_identity(row) for row in rows]

    _WORKLOAD_VALUE_SQL = """CASE WHEN json_valid(tags_json)
        AND json_type(tags_json,'$."verdict.workload"')='text'
        AND length(CAST(json_extract(tags_json,'$."verdict.workload"') AS BLOB))
            BETWEEN 1 AND 64
        THEN json_extract(tags_json,'$."verdict.workload"') END"""

    @staticmethod
    def _sqlite_json_type(value: str | None) -> str:
        return {
            None: "missing",
            "text": "string",
            "integer": "number",
            "real": "number",
            "true": "boolean",
            "false": "boolean",
        }.get(value, value if value in {"null", "array", "object"} else "object")

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
        type_sql = lambda path: (  # noqa: E731 - fixed SQL projection helper
            f"CASE WHEN json_valid(tags_json) THEN json_type(tags_json,'$.\"{path}\"') "
            "ELSE 'object' END"
        )
        length_sql = lambda path: (  # noqa: E731 - fixed SQL projection helper
            f"CASE WHEN json_valid(tags_json) AND json_type(tags_json,'$.\"{path}\"')="
            f"'text' THEN length(CAST(json_extract(tags_json,'$.\"{path}\"') AS BLOB)) END"
        )
        value_sql = lambda path: (  # noqa: E731 - fixed SQL projection helper
            f"CASE WHEN json_valid(tags_json) AND json_type(tags_json,'$.\"{path}\"')="
            f"'text' AND {length_sql(path)} BETWEEN 1 AND 64 THEN "
            f"json_extract(tags_json,'$.\"{path}\"') END"
        )
        tenant_clause = (
            "tenant_id IS NULL" if authorized_tenant == "__verdict_local__" else "tenant_id=?"
        )
        where = f"""{tenant_clause} AND ended_at IS NOT NULL
            AND analysis_started_at_state='valid'
            AND analysis_started_at_us>=? AND analysis_started_at_us<?"""
        params: list[object] = [start_us, cutoff_us]
        if authorized_tenant != "__verdict_local__":
            params.insert(0, authorized_tenant)
        if missing_version_id is not None:
            where += """ AND NOT EXISTS (
              SELECT 1 FROM trace_cluster_assignments a
              WHERE a.tenant_id=? AND a.version_id=? AND a.trace_id=traces.trace_id)"""
            params.extend([authorized_tenant, missing_version_id])
        if target_workload is not None:
            where += f" AND ({self._WORKLOAD_VALUE_SQL})=?"
            params.append(target_workload)
        else:
            where += f" AND COALESCE(({self._WORKLOAD_VALUE_SQL}),'') NOT IN (?,?)"
            params.extend(["judge", "paired_replay"])
        params.append(limit)
        sql = f"""SELECT length(CAST(trace_id AS BLOB)),
          CASE WHEN length(CAST(trace_id AS BLOB))<=256 THEN trace_id END,
          tenant_id,analysis_started_at_us,
          {type_sql("verdict.workload")},{length_sql("verdict.workload")},
          {value_sql("verdict.workload")},
          {type_sql("verdict.intent_key")},{length_sql("verdict.intent_key")},
          {value_sql("verdict.intent_key")},
          analysis_raw_messages_state,analysis_raw_messages_utf8_bytes
          FROM traces WHERE {where}
          ORDER BY analysis_started_at_us,trace_id LIMIT ?"""  # nosec B608
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            ClusterTraceCandidate(
                row[0],
                row[1],
                authorized_tenant,
                row[3],
                self._sqlite_json_type(row[4]),
                row[5],
                row[6],
                self._sqlite_json_type(row[7]),
                row[8],
                row[9],
                row[10],
                row[11],
            )
            for row in rows
        ]

    def get_cluster_trace_messages(
        self,
        authorized_tenant: str,
        trace_ids: list[str],
    ) -> dict[str, list[dict] | None]:
        if not trace_ids:
            return {}
        placeholders = ",".join("?" for _ in trace_ids)
        tenant_clause = (
            "tenant_id IS NULL" if authorized_tenant == "__verdict_local__" else "tenant_id=?"
        )
        params: list[object] = (
            [] if authorized_tenant == "__verdict_local__" else [authorized_tenant]
        )
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT trace_id,CASE WHEN analysis_raw_messages_state='valid'
                    AND analysis_raw_messages_utf8_bytes<=67108864
                    THEN raw_messages_json END FROM traces
                    WHERE {tenant_clause} AND trace_id IN ({placeholders})""",  # nosec B608
                [*params, *trace_ids],
            ).fetchall()
        return {row[0]: json.loads(row[1]) if row[1] else None for row in rows}

    def count_pending_analysis_rows(self, authorized_tenant: str) -> int:
        tenant_clause = (
            "tenant_id IS NULL" if authorized_tenant == "__verdict_local__" else "tenant_id=?"
        )
        params = () if authorized_tenant == "__verdict_local__" else (authorized_tenant,)
        with self._lock:
            return self._conn.execute(
                f"""SELECT COUNT(*) FROM traces WHERE {tenant_clause} AND
                  (analysis_started_at_state='pending' OR
                   analysis_raw_messages_state='pending')""",  # nosec B608
                params,
            ).fetchone()[0]

    @contextmanager
    def cluster_analysis_snapshot(self):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self
            finally:
                self._conn.execute("ROLLBACK")

    def normalize_cluster_trace_analysis(
        self, authorized_tenant: str, *, limit: int = 10_000
    ) -> int:
        if not 1 <= limit <= 10_000:
            raise ValueError("normalization limit must be in [1,10000]")
        tenant_clause = (
            "tenant_id IS NULL" if authorized_tenant == "__verdict_local__" else "tenant_id=?"
        )
        params: list[object] = (
            [] if authorized_tenant == "__verdict_local__" else [authorized_tenant]
        )
        params.append(limit)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    f"""SELECT trace_id,started_at,
                      length(CAST(raw_messages_json AS BLOB)),
                      CASE WHEN length(CAST(raw_messages_json AS BLOB))<=67108864
                           THEN raw_messages_json END
                      FROM traces WHERE {tenant_clause} AND
                       (analysis_started_at_state='pending' OR
                        analysis_raw_messages_state='pending')
                      ORDER BY trace_id LIMIT ?""",  # nosec B608
                    params,
                ).fetchall()
                for row in rows:
                    try:
                        started: object = _parse_iso(row[1])
                    except (TypeError, ValueError):
                        started = row[1]
                    trace = Trace(trace_id=row[0], started_at=started, raw_messages=None)
                    populate_trace_analysis_fields(trace)
                    if row[2] is not None and row[2] > 67_108_864:
                        trace.analysis_raw_messages_utf8_bytes = row[2]
                        trace.analysis_raw_messages_state = "oversize"
                    elif row[3] is not None:
                        try:
                            trace.raw_messages = json.loads(row[3])
                            populate_trace_analysis_fields(trace)
                        except (TypeError, ValueError, UnicodeError):
                            trace.analysis_raw_messages_utf8_bytes = None
                            trace.analysis_raw_messages_state = "malformed"
                    self._conn.execute(
                        """UPDATE traces SET analysis_started_at_us=?,
                           analysis_started_at_state=?,analysis_raw_messages_utf8_bytes=?,
                           analysis_raw_messages_state=? WHERE trace_id=?""",
                        (
                            trace.analysis_started_at_us,
                            trace.analysis_started_at_state,
                            trace.analysis_raw_messages_utf8_bytes,
                            trace.analysis_raw_messages_state,
                            trace.trace_id,
                        ),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return len(rows)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
