"""SQLite storage adapter — durable, single-file, zero external deps.

For v0 this is the primary storage backend. Postgres lives behind the same
Storage Protocol and will be added when we need multi-process access.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from verdict.redaction import sanitize_judgment, sanitize_span, sanitize_trace
from verdict.schema import (
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
    UserSignalRecord,
    Verdict,
)
from verdict.storage.base import _validate_drift_run_snapshot

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
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_traces_cluster ON traces(cluster_id);
CREATE INDEX IF NOT EXISTS idx_traces_tenant ON traces(tenant_id);
CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at);

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


def _normalize_sqlite_path(path: str) -> str:
    """Accept either a filesystem path or the public ``sqlite:///`` URL form."""
    if path.startswith("sqlite:///"):
        path = path[len("sqlite:///"):]
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
        self._lock = threading.Lock()
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
                trace_columns = {
                    row[1] for row in self._conn.execute("PRAGMA table_info(traces)")
                }
                for column, ddl in (
                    ("cluster_id", "TEXT"),
                    ("parent_span_id", "TEXT"),
                ):
                    if column not in trace_columns:
                        self._conn.execute(
                            f"ALTER TABLE traces ADD COLUMN {column} {ddl}"
                        )
            if "spans" in existing_tables:
                span_columns = {
                    row[1] for row in self._conn.execute("PRAGMA table_info(spans)")
                }
                if "parent_name" not in span_columns:
                    self._conn.execute(
                        "ALTER TABLE spans ADD COLUMN parent_name TEXT"
                    )
            if "drift_signals" in existing_tables:
                drift_columns = {
                    row[1]
                    for row in self._conn.execute("PRAGMA table_info(drift_signals)")
                }
                if "run_id" not in drift_columns:
                    self._conn.execute(
                        "ALTER TABLE drift_signals ADD COLUMN run_id TEXT"
                    )
            self._conn.executescript(_SCHEMA)
            try:
                self._conn.execute(
                    "ALTER TABLE traces ADD COLUMN parent_span_id TEXT"
                )
            except sqlite3.OperationalError:
                pass
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_parent_span "
                "ON traces(parent_span_id)"
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
                    self._conn.execute(
                        f"ALTER TABLE evaluator_health ADD COLUMN {col} {ddl}"
                    )
                except sqlite3.OperationalError:
                    pass

    # -- Traces ------------------------------------------------------------

    def insert_trace(self, trace: Trace) -> None:
        sanitize_trace(trace)
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
                    tags_json, cost_usd
                ) VALUES (
                    :trace_id, :parent_span_id, :started_at, :ended_at, :provider, :operation,
                    :request_model, :response_model, :input_tokens, :output_tokens,
                    :temperature, :max_tokens, :finish_reason, :error, :latency_ms,
                    :prompt_redacted, :response_redacted, :raw_messages_json,
                    :tenant_id, :session_id, :user_id_hash, :cluster_id,
                    :tags_json, :cost_usd
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
                    "raw_messages_json": json.dumps(trace.raw_messages) if trace.raw_messages else None,
                    "tenant_id": trace.tenant_id,
                    "session_id": trace.session_id,
                    "user_id_hash": trace.user_id_hash,
                    "cluster_id": trace.cluster_id,
                    "tags_json": json.dumps(trace.tags),
                    "cost_usd": trace.cost_usd,
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
                f"SELECT * FROM traces {where} ORDER BY started_at DESC LIMIT ?", params,
            )
            rows = cur.fetchall()
        return [self._row_to_trace(r) for r in rows]

    def delete_trace(self, trace_id: str) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM judgments WHERE trace_id = ?", (trace_id,)
                )
                self._conn.execute(
                    "DELETE FROM user_signals WHERE trace_id = ?", (trace_id,)
                )
                self._conn.execute(
                    "DELETE FROM traces WHERE trace_id = ?", (trace_id,)
                )
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
                    "SELECT trace_id FROM traces WHERE started_at < ?", (cutoff_iso,),
                )
                ids = [r["trace_id"] for r in cur.fetchall()]
                for tid in ids:
                    self._conn.execute(
                        "DELETE FROM judgments WHERE trace_id = ?", (tid,)
                    )
                    self._conn.execute(
                        "DELETE FROM user_signals WHERE trace_id = ?", (tid,)
                    )
                self._conn.execute(
                    "DELETE FROM traces WHERE started_at < ?", (cutoff_iso,)
                )
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
                        None if judgment.position_swap_consistent is None
                        else int(judgment.position_swap_consistent)
                    ),
                    "evaluator_provider": judgment.evaluator_provider,
                    "evaluator_config_json": json.dumps(
                        judgment.evaluator_config, sort_keys=True
                    ),
                    "evaluator_fingerprint": judgment.evaluator_fingerprint,
                    "expected_dimensions_json": json.dumps(
                        judgment.expected_dimensions
                    ),
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
                json.loads(row["evaluator_config_json"])
                if row["evaluator_config_json"] else {}
            ),
            evaluator_fingerprint=row["evaluator_fingerprint"] or "",
            expected_dimensions=(
                json.loads(row["expected_dimensions_json"])
                if row["expected_dimensions_json"] else []
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
            records.append(EvaluatorHealthRecord(
                health_id=row["health_id"],
                evaluated_at=_parse_iso(row["evaluated_at"])
                or datetime.now(timezone.utc),
                evaluator_fingerprint=row["evaluator_fingerprint"],
                sentinel_set_name=row["sentinel_set_name"] or "",
                sentinel_set_fingerprint=row["sentinel_set_fingerprint"],
                correct_examples=0 if legacy else row["correct_examples"],
                total_examples=0 if legacy else row["total_examples"],
                example_agreement=None if legacy else row["example_agreement"],
                example_confidence_low=(
                    None if legacy else row["example_confidence_low"]
                ),
                example_confidence_high=(
                    None if legacy else row["example_confidence_high"]
                ),
                correct_labels=row["correct_labels"],
                total_labels=row["total_labels"],
                label_agreement=(
                    row["agreement"]
                    if legacy and "agreement" in row_keys
                    else row["label_agreement"]
                ),
                status=(
                    EvaluatorHealthStatus.INSUFFICIENT_DATA
                    if legacy else EvaluatorHealthStatus(row["status"])
                ),
                error_count=row["error_count"],
                method_version=method_version,
            ))
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
                if (
                    existing
                    and existing["evaluator_fingerprint"]
                    != run.evaluator_fingerprint
                ):
                    raise ValueError("run_id already belongs to another evaluator")
                for signal in signals:
                    owner = self._conn.execute(
                        "SELECT run_id FROM drift_signals WHERE signal_id = ?",
                        (signal.signal_id,),
                    ).fetchone()
                    if owner and owner["run_id"] and owner["run_id"] != run.run_id:
                        raise ValueError(
                            "signal_id already belongs to another drift run"
                        )
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
                self._conn.execute(
                    "DELETE FROM drift_signals WHERE run_id = ?", (run.run_id,)
                )
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
                    analysis_time=_parse_iso(row["analysis_time"])
                    or datetime.now(timezone.utc),
                    completed_at=_parse_iso(row["completed_at"])
                    or datetime.now(timezone.utc),
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
                    self._conn.execute(
                        "DELETE FROM drift_signals WHERE run_id = ?", (run_id,)
                    )
                    self._conn.execute(
                        "DELETE FROM drift_runs WHERE run_id = ?", (run_id,)
                    )
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
                "SELECT * FROM drift_signals ORDER BY detected_at DESC LIMIT ?", (limit,),
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
                contributing_layers=json.loads(r["contributing_layers_json"]) if r["contributing_layers_json"] else [],
                example_trace_ids=json.loads(r["example_trace_ids_json"]) if r["example_trace_ids_json"] else [],
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
                f"SELECT * FROM spans {where} ORDER BY started_at DESC LIMIT ?", params,
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
                "SELECT * FROM user_signals ORDER BY created_at DESC LIMIT ?", (limit,),
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
                "SELECT payload_json FROM cluster_registries WHERE version = ?", (version,),
            )
            row = cur.fetchone()
        return row["payload_json"] if row else None

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
