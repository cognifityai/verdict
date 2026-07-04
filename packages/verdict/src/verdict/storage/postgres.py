"""Postgres storage adapter.

Proves the Storage Protocol scales beyond SQLite. Multi-process safe (real
DB locks, not in-process locks). For v0 this is opt-in — you have to install
`psycopg[binary]` and pass a postgres URL to `verdict.init()`.

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
    trace_id          TEXT PRIMARY KEY,
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
    position_swap_consistent BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_judgments_trace   ON judgments(trace_id);
CREATE INDEX IF NOT EXISTS idx_judgments_created ON judgments(created_at);

CREATE TABLE IF NOT EXISTS drift_signals (
    signal_id              TEXT PRIMARY KEY,
    detected_at            TIMESTAMPTZ NOT NULL,
    cluster_id             TEXT,
    dimension              TEXT,
    direction              TEXT,
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

    Install: `pip install "psycopg[binary]"`.
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
                ]:
                    cur.execute(
                        f"ALTER TABLE drift_signals ADD COLUMN IF NOT EXISTS {col} {ddl}"
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

    def insert_trace(self, trace: Trace) -> None:
        self._exec(
            """INSERT INTO traces VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s
            ) ON CONFLICT (trace_id) DO UPDATE SET
                ended_at         = EXCLUDED.ended_at,
                response_model   = EXCLUDED.response_model,
                input_tokens     = EXCLUDED.input_tokens,
                output_tokens    = EXCLUDED.output_tokens,
                finish_reason    = EXCLUDED.finish_reason,
                error            = EXCLUDED.error,
                latency_ms       = EXCLUDED.latency_ms,
                prompt_redacted  = EXCLUDED.prompt_redacted,
                response_redacted= EXCLUDED.response_redacted,
                cluster_id       = COALESCE(EXCLUDED.cluster_id, traces.cluster_id),
                cost_usd         = EXCLUDED.cost_usd
            """,
            (
                trace.trace_id,
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
            started_at=row[1],
            ended_at=row[2],
            provider=row[3] or "",
            operation=Operation(row[4] or "chat"),
            request_model=row[5] or "",
            response_model=row[6] or "",
            input_tokens=row[7],
            output_tokens=row[8],
            temperature=row[9],
            max_tokens=row[10],
            finish_reason=row[11],
            error=row[12],
            latency_ms=row[13],
            prompt_redacted=row[14],
            response_redacted=row[15],
            raw_messages=row[16] if isinstance(row[16], list) else (json.loads(row[16]) if row[16] else None),
            tenant_id=row[17],
            session_id=row[18],
            user_id_hash=row[19],
            cluster_id=row[20],
            tags=row[21] if isinstance(row[21], dict) else (json.loads(row[21]) if row[21] else {}),
            cost_usd=row[22],
        )

    def get_trace(self, trace_id: str) -> Trace | None:
        row = self._fetchone("SELECT * FROM traces WHERE trace_id = %s", (trace_id,))
        return self._row_to_trace(row) if row else None

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
            f"SELECT * FROM traces {where} ORDER BY started_at DESC LIMIT %s",
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

    def insert_judgment(self, judgment: Judgment) -> None:
        self._exec(
            """INSERT INTO judgments VALUES (
                %s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s
            ) ON CONFLICT (judgment_id) DO UPDATE SET
                dimensions = EXCLUDED.dimensions,
                judge_models = EXCLUDED.judge_models""",
            (
                judgment.judgment_id,
                judgment.trace_id,
                judgment.rubric_name,
                judgment.rubric_version,
                judgment.created_at,
                json.dumps(judgment.judge_models),
                json.dumps([asdict(d) for d in judgment.dimensions]),
                judgment.position_swap_consistent,
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
            f"""SELECT j.* FROM judgments j
                JOIN traces t ON j.trace_id = t.trace_id
                WHERE t.cluster_id = %s{clause}
                ORDER BY j.created_at DESC LIMIT %s""",
            tuple(params),
        )
        return [self._row_to_judgment(r) for r in rows]

    # -- Drift signals ----------------------------------------------------

    # Explicit column list keeps the INSERT and SELECT in lockstep so a future
    # schema change can't silently drop a field (the bug that lost Cliff's δ,
    # Wasserstein, and PSI on Postgres before the May-2026 fix).
    _SIGNAL_COLUMNS = (
        "signal_id, detected_at, cluster_id, dimension, direction, "
        "statistic_name, statistic_value, p_value, p_value_adjusted, "
        "effect_size_cohens_d, effect_size_cliffs_delta, wasserstein_distance, psi, "
        "sample_size_current, sample_size_baseline, "
        "contributing_layers, example_trace_ids, recommended_action"
    )

    def insert_drift_signal(self, signal: DriftSignal) -> None:
        self._exec(
            f"""INSERT INTO drift_signals ({self._SIGNAL_COLUMNS}) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s
            ) ON CONFLICT (signal_id) DO NOTHING""",
            (
                signal.signal_id,
                signal.detected_at,
                signal.cluster_id,
                signal.dimension,
                signal.direction.value,
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
                statistic_name=r[5] or "",
                statistic_value=r[6] or 0.0,
                p_value=r[7] or 1.0,
                p_value_adjusted=r[8] or 1.0,
                effect_size_cohens_d=r[9] or 0.0,
                effect_size_cliffs_delta=r[10] or 0.0,
                wasserstein_distance=r[11] or 0.0,
                psi=r[12] or 0.0,
                sample_size_current=r[13] or 0,
                sample_size_baseline=r[14] or 0,
                contributing_layers=r[15] if isinstance(r[15], list) else (json.loads(r[15]) if r[15] else []),
                example_trace_ids=r[16] if isinstance(r[16], list) else (json.loads(r[16]) if r[16] else []),
                recommended_action=r[17] or "",
            )
            for r in rows
        ]

    # -- Spans ------------------------------------------------------------

    def insert_span(self, span: SpanRecord) -> None:
        self._exec(
            """INSERT INTO spans (
                span_id, name, trace_id, parent_name, started_at,
                ended_at, duration_ms, attributes, error
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s
            ) ON CONFLICT (span_id) DO UPDATE SET
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
