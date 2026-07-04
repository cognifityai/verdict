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

from verdict.schema import (
    DimensionScore,
    DriftDirection,
    DriftSignal,
    Judgment,
    Operation,
    SpanRecord,
    Trace,
    UserSignalRecord,
    Verdict,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
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
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
);
CREATE INDEX IF NOT EXISTS idx_judgments_trace ON judgments(trace_id);
CREATE INDEX IF NOT EXISTS idx_judgments_created ON judgments(created_at);

CREATE TABLE IF NOT EXISTS drift_signals (
    signal_id TEXT PRIMARY KEY,
    detected_at TEXT NOT NULL,
    cluster_id TEXT,
    dimension TEXT,
    direction TEXT,
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


class SQLiteStorage:
    """Durable single-file storage. Thread-safe via a single connection + lock."""

    def __init__(self, path: str) -> None:
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
            self._conn.executescript(_SCHEMA)
            # Idempotent column-add for in-place migration of older DBs
            for col, ddl in [
                ("effect_size_cliffs_delta", "REAL DEFAULT 0.0"),
                ("wasserstein_distance", "REAL DEFAULT 0.0"),
                ("psi", "REAL DEFAULT 0.0"),
            ]:
                try:
                    self._conn.execute(f"ALTER TABLE drift_signals ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    # Column already exists — fine
                    pass

    # -- Traces ------------------------------------------------------------

    def insert_trace(self, trace: Trace) -> None:
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
                    trace_id, started_at, ended_at, provider, operation,
                    request_model, response_model, input_tokens, output_tokens,
                    temperature, max_tokens, finish_reason, error, latency_ms,
                    prompt_redacted, response_redacted, raw_messages_json,
                    tenant_id, session_id, user_id_hash, cluster_id,
                    tags_json, cost_usd
                ) VALUES (
                    :trace_id, :started_at, :ended_at, :provider, :operation,
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
                    cluster_id        = COALESCE(excluded.cluster_id, traces.cluster_id),
                    cost_usd          = excluded.cost_usd""",
                {
                    "trace_id": trace.trace_id,
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
            self._conn.execute("DELETE FROM judgments WHERE trace_id = ?", (trace_id,))
            self._conn.execute("DELETE FROM spans WHERE trace_id = ?", (trace_id,))
            self._conn.execute("DELETE FROM user_signals WHERE trace_id = ?", (trace_id,))
            self._conn.execute("DELETE FROM traces WHERE trace_id = ?", (trace_id,))

    def prune_before(self, cutoff_iso: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT trace_id FROM traces WHERE started_at < ?", (cutoff_iso,),
            )
            ids = [r["trace_id"] for r in cur.fetchall()]
            for tid in ids:
                self._conn.execute("DELETE FROM judgments WHERE trace_id = ?", (tid,))
                self._conn.execute("DELETE FROM spans WHERE trace_id = ?", (tid,))
                self._conn.execute("DELETE FROM user_signals WHERE trace_id = ?", (tid,))
            self._conn.execute("DELETE FROM traces WHERE started_at < ?", (cutoff_iso,))
        return len(ids)

    # -- Judgments ---------------------------------------------------------

    def insert_judgment(self, judgment: Judgment) -> None:
        dims_payload = [asdict(d) for d in judgment.dimensions]
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO judgments VALUES (
                    :judgment_id, :trace_id, :rubric_name, :rubric_version, :created_at,
                    :judge_models_json, :dimensions_json, :position_swap_consistent
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

    # -- Drift signals -----------------------------------------------------

    def insert_drift_signal(self, signal: DriftSignal) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO drift_signals (
                    signal_id, detected_at, cluster_id, dimension, direction,
                    statistic_name, statistic_value, p_value, p_value_adjusted,
                    effect_size_cohens_d, effect_size_cliffs_delta,
                    wasserstein_distance, psi,
                    sample_size_current, sample_size_baseline,
                    contributing_layers_json, example_trace_ids_json, recommended_action
                ) VALUES (
                    :signal_id, :detected_at, :cluster_id, :dimension, :direction,
                    :statistic_name, :statistic_value, :p_value, :p_value_adjusted,
                    :effect_size_cohens_d, :effect_size_cliffs_delta,
                    :wasserstein_distance, :psi,
                    :sample_size_current, :sample_size_baseline,
                    :contributing_layers_json, :example_trace_ids_json, :recommended_action
                )""",
                {
                    "signal_id": signal.signal_id,
                    "detected_at": _iso(signal.detected_at),
                    "cluster_id": signal.cluster_id,
                    "dimension": signal.dimension,
                    "direction": signal.direction.value,
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
                },
            )

    def list_drift_signals(self, *, limit: int = 100) -> list[DriftSignal]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM drift_signals ORDER BY detected_at DESC LIMIT ?", (limit,),
            )
            rows = cur.fetchall()
        def _get(row, key, default=0.0):
            try:
                v = row[key]
                return v if v is not None else default
            except (IndexError, KeyError):
                return default

        return [
            DriftSignal(
                signal_id=r["signal_id"],
                detected_at=_parse_iso(r["detected_at"]) or datetime.now(timezone.utc),
                cluster_id=r["cluster_id"] or "",
                dimension=r["dimension"] or "",
                direction=DriftDirection(r["direction"] or "change"),
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
            for r in rows
        ]

    # -- Spans -------------------------------------------------------------

    def insert_span(self, span: SpanRecord) -> None:
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
