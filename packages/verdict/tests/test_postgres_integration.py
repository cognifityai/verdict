"""Live PostgreSQL adapter contract tests.

Skipped unless VERDICT_TEST_POSTGRES_DSN points at a disposable database. CI
provides an ephemeral PostgreSQL service; these are not mocked SQL tests.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from verdict.schema import (
    DimensionScore,
    DriftDirection,
    DriftSignal,
    EvaluatorHealthRecord,
    EvaluatorHealthStatus,
    Judgment,
    SpanRecord,
    Trace,
    UserSignalRecord,
    Verdict,
)
from verdict.storage.postgres import PostgresStorage

DSN = os.environ.get("VERDICT_TEST_POSTGRES_DSN")
pytestmark = [
    pytest.mark.skipif(not DSN, reason="no disposable live Postgres DSN"),
    pytest.mark.filterwarnings("error::DeprecationWarning:psycopg_pool.*"),
]


def test_live_postgres_migrates_legacy_tables_before_creating_indexes():
    """Open the adapter over pre-remediation tables, not only a fresh schema."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_legacy_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute("""
                CREATE TABLE traces (
                    trace_id TEXT PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL,
                    ended_at TIMESTAMPTZ, provider TEXT, operation TEXT,
                    request_model TEXT, response_model TEXT, input_tokens INTEGER,
                    output_tokens INTEGER, temperature DOUBLE PRECISION,
                    max_tokens INTEGER, finish_reason TEXT, error TEXT,
                    latency_ms DOUBLE PRECISION, prompt_redacted TEXT,
                    response_redacted TEXT, raw_messages JSONB, tenant_id TEXT,
                    session_id TEXT, user_id_hash TEXT, tags JSONB,
                    cost_usd DOUBLE PRECISION
                );
                CREATE TABLE judgments (
                    judgment_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
                    rubric_name TEXT, rubric_version TEXT,
                    created_at TIMESTAMPTZ NOT NULL, judge_models JSONB,
                    dimensions JSONB, position_swap_consistent BOOLEAN
                );
                CREATE TABLE drift_signals (
                    signal_id TEXT PRIMARY KEY, detected_at TIMESTAMPTZ NOT NULL,
                    cluster_id TEXT, dimension TEXT, direction TEXT,
                    statistic_name TEXT, statistic_value DOUBLE PRECISION,
                    p_value DOUBLE PRECISION, p_value_adjusted DOUBLE PRECISION,
                    effect_size_cohens_d DOUBLE PRECISION,
                    sample_size_current INTEGER, sample_size_baseline INTEGER,
                    contributing_layers JSONB, example_trace_ids JSONB,
                    recommended_action TEXT
                );
                CREATE TABLE spans (
                    span_id TEXT PRIMARY KEY, name TEXT, trace_id TEXT,
                    started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
                    duration_ms DOUBLE PRECISION, attributes JSONB, error TEXT
                );
            """)

        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=2)
        try:
            columns = {
                (table, column)
                for table, column in storage._fetchall(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema()",
                    (),
                )
            }
            assert {
                ("traces", "cluster_id"),
                ("traces", "parent_span_id"),
                ("spans", "parent_name"),
                ("judgments", "evaluator_fingerprint"),
                ("judgments", "status"),
                ("drift_signals", "evaluator_fingerprint"),
                ("drift_signals", "effect_size_cliffs_delta"),
                ("drift_signals", "wasserstein_distance"),
                ("drift_signals", "psi"),
            } <= columns

            trace = Trace(
                trace_id=f"legacy-upgrade-{uuid4().hex}",
                cluster_id="upgraded",
                parent_span_id="parent",
            )
            storage.insert_trace(trace)
            assert storage.get_trace(trace.trace_id).parent_span_id == "parent"
        finally:
            storage.close()
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def test_live_postgres_round_trip_and_mutation_contracts():
    prefix = f"integration-{uuid4().hex}"
    trace_id = f"{prefix}-trace"
    fingerprint = f"{prefix}-evaluator"
    registry_version = f"{prefix}-registry"
    now = datetime.now(timezone.utc)
    storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
    try:
        trace = Trace(
            trace_id=trace_id,
            parent_span_id=f"{prefix}-parent",
            started_at=now,
            provider="custom-provider",
            request_model="custom/model",
            response_model="custom/model-v2",
            prompt_redacted="prompt",
            response_redacted="response",
            tenant_id=f"{prefix}-tenant",
            session_id=f"{prefix}-session",
            cluster_id=f"{prefix}-cluster",
            tags={"source": "integration"},
        )
        storage.insert_trace(trace)
        assert storage.trace_exists(trace_id)
        fetched = storage.get_trace(trace_id)
        assert fetched is not None
        assert fetched.parent_span_id == trace.parent_span_id
        assert fetched.tags == {"source": "integration"}
        assert [item.trace_id for item in storage.list_traces(
            tenant_id=trace.tenant_id,
            cluster_id=trace.cluster_id,
        )] == [trace_id]

        span = SpanRecord(
            span_id=f"{prefix}-span",
            name="retrieval",
            parent_name="request",
            attributes={"k": 3},
        )
        storage.insert_span(span)
        span.trace_id = trace_id
        storage.insert_span(span)
        [persisted_span] = storage.list_spans(trace_id=trace_id)
        assert persisted_span.parent_name == "request"
        assert persisted_span.attributes == {"k": 3}

        judgment = Judgment(
            judgment_id=f"{prefix}-judgment",
            trace_id=trace_id,
            rubric_name="quality",
            rubric_version="2",
            judge_models=["judge-a"],
            dimensions=[DimensionScore("quality", Verdict.FAIL, "reason", "judge-a")],
            position_swap_consistent=True,
            evaluator_provider="openai",
            evaluator_config={"temperature": 0},
            evaluator_fingerprint=fingerprint,
            expected_dimensions=["quality"],
        )
        storage.insert_judgment(judgment)
        [persisted_judgment] = storage.list_judgments_for_cluster(trace.cluster_id)
        assert persisted_judgment.judgment_id == judgment.judgment_id
        assert persisted_judgment.position_swap_consistent is True
        assert persisted_judgment.evaluator_config == {"temperature": 0}
        assert persisted_judgment.dimensions[0].verdict is Verdict.FAIL

        health = EvaluatorHealthRecord(
            health_id=f"{prefix}-health",
            evaluator_fingerprint=fingerprint,
            sentinel_set_name="anchors",
            sentinel_set_fingerprint=f"{prefix}-anchors",
            correct_labels=30,
            total_labels=30,
            agreement=1.0,
            confidence_low=0.88,
            confidence_high=1.0,
            status=EvaluatorHealthStatus.HEALTHY,
        )
        storage.insert_evaluator_health(health)
        assert storage.list_evaluator_health(
            evaluator_fingerprint=fingerprint
        )[0].status is EvaluatorHealthStatus.HEALTHY

        signal = DriftSignal(
            signal_id=f"{prefix}-signal",
            detected_at=now,
            cluster_id=trace.cluster_id or "",
            dimension="quality",
            direction=DriftDirection.REGRESSION,
            evaluator_fingerprint=fingerprint,
            statistic_name="fisher_exact",
            statistic_value=0.04,
            p_value=0.001,
            p_value_adjusted=0.003,
            effect_size_cliffs_delta=-0.5,
            effect_size_cohens_d=-1.0,
            wasserstein_distance=0.5,
            psi=0.8,
            sample_size_current=30,
            sample_size_baseline=30,
        )
        storage.insert_drift_signal(signal)
        persisted_signal = next(
            item for item in storage.list_drift_signals() if item.signal_id == signal.signal_id
        )
        assert persisted_signal.effect_size_cliffs_delta == -0.5
        assert persisted_signal.wasserstein_distance == 0.5
        storage.delete_drift_signals_between(
            now - timedelta(seconds=1),
            now + timedelta(seconds=1),
            evaluator_fingerprint=fingerprint,
        )
        assert all(
            item.signal_id != signal.signal_id for item in storage.list_drift_signals()
        )

        user_signal = UserSignalRecord(
            signal_id=f"{prefix}-user-signal",
            trace_id=trace_id,
            kind="thumbs_down",
        )
        storage.insert_user_signal(user_signal)
        assert any(
            item.signal_id == user_signal.signal_id
            for item in storage.list_user_signals()
        )

        storage.save_cluster_registry(registry_version, '{"clusters": []}')
        assert storage.load_cluster_registry(registry_version) == '{"clusters": []}'

        storage.delete_trace(trace_id)
        assert storage.get_trace(trace_id) is None
        assert storage.list_spans(trace_id=trace_id) == []
        assert storage.list_judgments_for_cluster(trace.cluster_id or "") == []
    finally:
        # The CI database is disposable, but scoped cleanup also makes reruns
        # deterministic and safe when a developer supplies a test database.
        for table, column, value in (
            ("evaluator_health", "health_id", f"{prefix}-health"),
            ("cluster_registries", "version", registry_version),
            ("drift_signals", "evaluator_fingerprint", fingerprint),
            ("user_signals", "trace_id", trace_id),
            ("spans", "trace_id", trace_id),
            ("judgments", "trace_id", trace_id),
            ("traces", "trace_id", trace_id),
        ):
            storage._exec(f"DELETE FROM {table} WHERE {column} = %s", (value,))  # nosec B608
        storage.close()
