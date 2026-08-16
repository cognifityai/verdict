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

import json
import threading
from dataclasses import asdict
from datetime import datetime

from verdict.redaction import sanitize_judgment, sanitize_span, sanitize_trace
from verdict.schema import (
    DimensionScore,
    DriftDirection,
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
    cost_usd          DOUBLE PRECISION
);
ALTER TABLE traces ADD COLUMN IF NOT EXISTS cluster_id TEXT;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS parent_span_id TEXT;
CREATE INDEX IF NOT EXISTS idx_traces_cluster  ON traces(cluster_id);
CREATE INDEX IF NOT EXISTS idx_traces_tenant   ON traces(tenant_id);
CREATE INDEX IF NOT EXISTS idx_traces_started  ON traces(started_at);

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
    correct_labels           INTEGER NOT NULL,
    total_labels             INTEGER NOT NULL,
    agreement                DOUBLE PRECISION,
    confidence_low           DOUBLE PRECISION,
    confidence_high          DOUBLE PRECISION,
    status                   TEXT NOT NULL,
    error_count              INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_evaluator_health_identity
    ON evaluator_health(evaluator_fingerprint, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS drift_signals (
    signal_id              TEXT PRIMARY KEY,
    detected_at            TIMESTAMPTZ NOT NULL,
    cluster_id             TEXT,
    dimension              TEXT,
    direction              TEXT,
    evaluator_fingerprint  TEXT,
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
CREATE INDEX IF NOT EXISTS idx_signals_detected     ON drift_signals(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_cluster_dim  ON drift_signals(cluster_id, dimension);

CREATE TABLE IF NOT EXISTS cluster_registries (
    version      TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
                "PostgresStorage requires `pip install \"psycopg[binary,pool]\"`"
            ) from e
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_pool,
            max_size=max_pool,
            kwargs={"autocommit": True},
            open=True,
        )
        self._lock = threading.Lock()
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
                ]:
                    cur.execute(
                        f"ALTER TABLE drift_signals ADD COLUMN IF NOT EXISTS {col} {ddl}"
                    )
                for col, ddl in [
                    ("evaluator_provider", "TEXT"),
                    ("evaluator_config", "JSONB"),
                    ("evaluator_fingerprint", "TEXT"),
                    ("expected_dimensions", "JSONB"),
                    ("status", "TEXT DEFAULT 'completed'"),
                    ("error", "TEXT"),
                ]:
                    cur.execute(
                        f"ALTER TABLE judgments ADD COLUMN IF NOT EXISTS {col} {ddl}"
                    )
                cur.execute(
                    "ALTER TABLE traces ADD COLUMN IF NOT EXISTS parent_span_id TEXT"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_traces_parent_span "
                    "ON traces(parent_span_id)"
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
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def _fetchall(self, sql: str, params: tuple):
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    # -- Traces -----------------------------------------------------------

    _TRACE_COLUMNS = """trace_id, parent_span_id, started_at, ended_at,
        provider, operation, request_model, response_model, input_tokens,
        output_tokens, temperature, max_tokens, finish_reason, error,
        latency_ms, prompt_redacted, response_redacted, raw_messages,
        tenant_id, session_id, user_id_hash, cluster_id, tags, cost_usd"""

    def insert_trace(self, trace: Trace) -> None:
        sanitize_trace(trace)
        # Only the fixed, class-owned column list is interpolated; every trace
        # value remains a driver-bound parameter.
        sql = (
            f"INSERT INTO traces ({self._TRACE_COLUMNS}) VALUES ("  # nosec B608
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,"
            "%s,%s,%s,%s,%s::jsonb,%s) "
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
                row[17]
                if isinstance(row[17], list)
                else json.loads(row[17]) if row[17] else None
            ),
            tenant_id=row[18],
            session_id=row[19],
            user_id_hash=row[20],
            cluster_id=row[21],
            tags=(
                row[22]
                if isinstance(row[22], dict)
                else json.loads(row[22]) if row[22] else {}
            ),
            cost_usd=row[23],
        )

    def get_trace(self, trace_id: str) -> Trace | None:
        row = self._fetchone(
            # Fixed column list; trace_id remains parameterized.
            f"SELECT {self._TRACE_COLUMNS} FROM traces WHERE trace_id = %s",  # nosec B608
            (trace_id,),
        )
        return self._row_to_trace(row) if row else None

    def trace_exists(self, trace_id: str) -> bool:
        return self._fetchone(
            "SELECT 1 FROM traces WHERE trace_id = %s LIMIT 1",
            (trace_id,),
        ) is not None

    def list_traces(
        self,
        *,
        tenant_id: str | None = None,
        cluster_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        clauses, params = [], []
        if tenant_id is not None:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if cluster_id is not None:
            clauses.append("cluster_id = %s")
            params.append(cluster_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self._fetchall(
            f"SELECT {self._TRACE_COLUMNS} FROM traces "
            f"{where} ORDER BY started_at DESC LIMIT %s",
            tuple(params),
        )
        return [self._row_to_trace(r) for r in rows]

    def delete_trace(self, trace_id: str) -> None:
        # judgments cascade via ON DELETE CASCADE, but spans/user_signals do
        # not declare an FK, so delete them explicitly for parity.
        self._exec("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
        self._exec("DELETE FROM user_signals WHERE trace_id = %s", (trace_id,))
        self._exec("DELETE FROM judgments WHERE trace_id = %s", (trace_id,))
        self._exec("DELETE FROM traces WHERE trace_id = %s", (trace_id,))

    def prune_before(self, cutoff_iso: str) -> int:
        rows = self._fetchall(
            "SELECT trace_id FROM traces WHERE started_at < %s", (cutoff_iso,),
        )
        ids = [r[0] for r in rows]
        if ids:
            self._exec("DELETE FROM spans WHERE trace_id = ANY(%s)", (ids,))
            self._exec("DELETE FROM user_signals WHERE trace_id = ANY(%s)", (ids,))
            self._exec("DELETE FROM judgments WHERE trace_id = ANY(%s)", (ids,))
            self._exec("DELETE FROM traces WHERE trace_id = ANY(%s)", (ids,))
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
            judge_models=row[5] if isinstance(row[5], list) else (json.loads(row[5]) if row[5] else []),
            dimensions=dims,
            evaluator_provider=row[8] or "",
            evaluator_config=(
                row[9] if isinstance(row[9], dict)
                else (json.loads(row[9]) if row[9] else {})
            ),
            evaluator_fingerprint=row[10] or "",
            expected_dimensions=(
                row[11] if isinstance(row[11], list)
                else (json.loads(row[11]) if row[11] else [])
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

    # -- Evaluator health ------------------------------------------------

    def insert_evaluator_health(self, record: EvaluatorHealthRecord) -> None:
        self._exec(
            """INSERT INTO evaluator_health (
                health_id, evaluated_at, evaluator_fingerprint,
                sentinel_set_name, sentinel_set_fingerprint,
                correct_labels, total_labels, agreement, confidence_low,
                confidence_high, status, error_count
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (health_id) DO UPDATE SET
                evaluated_at = EXCLUDED.evaluated_at,
                evaluator_fingerprint = EXCLUDED.evaluator_fingerprint,
                sentinel_set_name = EXCLUDED.sentinel_set_name,
                sentinel_set_fingerprint = EXCLUDED.sentinel_set_fingerprint,
                correct_labels = EXCLUDED.correct_labels,
                total_labels = EXCLUDED.total_labels,
                agreement = EXCLUDED.agreement,
                confidence_low = EXCLUDED.confidence_low,
                confidence_high = EXCLUDED.confidence_high,
                status = EXCLUDED.status,
                error_count = EXCLUDED.error_count""",
            (
                record.health_id,
                record.evaluated_at,
                record.evaluator_fingerprint,
                record.sentinel_set_name,
                record.sentinel_set_fingerprint,
                record.correct_labels,
                record.total_labels,
                record.agreement,
                record.confidence_low,
                record.confidence_high,
                record.status.value,
                record.error_count,
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
            "sentinel_set_name, sentinel_set_fingerprint, correct_labels, "
            "total_labels, agreement, confidence_low, confidence_high, status, "
            f"error_count FROM evaluator_health {where} "  # nosec B608
            "ORDER BY evaluated_at DESC LIMIT %s"
        )
        rows = self._fetchall(
            sql,
            tuple(params),
        )
        return [
            EvaluatorHealthRecord(
                health_id=row[0],
                evaluated_at=row[1],
                evaluator_fingerprint=row[2],
                sentinel_set_name=row[3] or "",
                sentinel_set_fingerprint=row[4],
                correct_labels=row[5],
                total_labels=row[6],
                agreement=row[7],
                confidence_low=row[8],
                confidence_high=row[9],
                status=EvaluatorHealthStatus(row[10]),
                error_count=row[11],
            )
            for row in rows
        ]

    # -- Drift signals ----------------------------------------------------

    # Explicit column list keeps the INSERT and SELECT in lockstep so a future
    # schema change can't silently drop a field (the bug that lost Cliff's δ,
    # Wasserstein, and PSI on Postgres before the May-2026 fix).
    _SIGNAL_COLUMNS = (
        "signal_id, detected_at, cluster_id, dimension, direction, "
        "evaluator_fingerprint, "
        "statistic_name, statistic_value, p_value, p_value_adjusted, "
        "effect_size_cohens_d, effect_size_cliffs_delta, wasserstein_distance, psi, "
        "sample_size_current, sample_size_baseline, "
        "contributing_layers, example_trace_ids, recommended_action"
    )

    def insert_drift_signal(self, signal: DriftSignal) -> None:
        self._exec(
            f"""INSERT INTO drift_signals ({self._SIGNAL_COLUMNS}) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s
            ) ON CONFLICT (signal_id) DO UPDATE SET
                detected_at = EXCLUDED.detected_at,
                cluster_id = EXCLUDED.cluster_id,
                dimension = EXCLUDED.dimension,
                direction = EXCLUDED.direction,
                evaluator_fingerprint = EXCLUDED.evaluator_fingerprint,
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
            (
                signal.signal_id,
                signal.detected_at,
                signal.cluster_id,
                signal.dimension,
                signal.direction.value,
                signal.evaluator_fingerprint,
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
            ),
        )

    def delete_drift_signals_between(
        self,
        start: datetime,
        end: datetime,
        *,
        evaluator_fingerprint: str | None = None,
    ) -> None:
        sql = "DELETE FROM drift_signals WHERE detected_at >= %s AND detected_at < %s"
        params: tuple = (start, end)
        if evaluator_fingerprint is not None:
            sql += " AND evaluator_fingerprint = %s"
            params += (evaluator_fingerprint,)
        self._exec(sql, params)

    def list_drift_signals(self, *, limit: int = 100) -> list[DriftSignal]:
        rows = self._fetchall(
            f"SELECT {self._SIGNAL_COLUMNS} FROM drift_signals "
            "ORDER BY detected_at DESC LIMIT %s",
            (limit,),
        )
        return [
            DriftSignal(
                signal_id=r[0],
                detected_at=r[1],
                cluster_id=r[2] or "",
                dimension=r[3] or "",
                direction=DriftDirection(r[4] or "change"),
                evaluator_fingerprint=r[5] or "",
                statistic_name=r[6] or "",
                statistic_value=r[7] or 0.0,
                p_value=r[8] or 1.0,
                p_value_adjusted=r[9] or 1.0,
                effect_size_cohens_d=r[10] or 0.0,
                effect_size_cliffs_delta=r[11] or 0.0,
                wasserstein_distance=r[12] or 0.0,
                psi=r[13] or 0.0,
                sample_size_current=r[14] or 0,
                sample_size_baseline=r[15] or 0,
                contributing_layers=r[16] if isinstance(r[16], list) else (json.loads(r[16]) if r[16] else []),
                example_trace_ids=r[17] if isinstance(r[17], list) else (json.loads(r[17]) if r[17] else []),
                recommended_action=r[18] or "",
            )
            for r in rows
        ]

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
                trace_id    = COALESCE(EXCLUDED.trace_id, spans.trace_id),
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
            attributes=row[7] if isinstance(row[7], dict) else (json.loads(row[7]) if row[7] else {}),
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
            "SELECT payload_json FROM cluster_registries WHERE version = %s", (version,),
        )
        return row[0] if row else None

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception:
            pass
