"""Real PostgreSQL parity test for the packaged dashboard read path."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from verdict.dashboard import build_bundle
from verdict.schema import DimensionScore, DriftRun, DriftSignal, Judgment, Trace, Verdict
from verdict.storage import SQLiteStorage
from verdict.storage.postgres import PostgresStorage

DSN = os.environ.get("VERDICT_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="no disposable live Postgres DSN")


def test_live_postgres_and_sqlite_produce_the_same_dashboard_bundle(tmp_path) -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_dashboard_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    postgres = None
    sqlite = None
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        postgres = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        sqlite_path = tmp_path / "dashboard-parity.db"
        sqlite = SQLiteStorage(str(sqlite_path))

        started_at = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        trace = Trace(
            trace_id="dashboard-parity-trace",
            started_at=started_at,
            provider="custom-provider",
            request_model="custom-model",
            cluster_id="custom-cluster",
            prompt_redacted="safe prompt",
            response_redacted="safe response",
            input_tokens=12,
            output_tokens=34,
            latency_ms=123.0,
            cost_usd=0.001,
        )
        judgment = Judgment(
            judgment_id="dashboard-parity-judgment",
            trace_id=trace.trace_id,
            created_at=started_at + timedelta(minutes=1),
            evaluator_provider="custom-judge-provider",
            evaluator_config={"temperature": 0},
            evaluator_fingerprint="dashboard-parity-evaluator",
            expected_dimensions=["custom-dimension"],
            judge_models=["custom-judge-model"],
            dimensions=[
                DimensionScore(name="custom-dimension", verdict=Verdict.FAIL)
            ],
        )
        run = DriftRun(
            run_id="dashboard-parity-run",
            analysis_time=started_at + timedelta(hours=1),
            completed_at=started_at + timedelta(hours=1, seconds=1),
            evaluator_fingerprint=judgment.evaluator_fingerprint,
            signal_count=1,
        )
        signal = DriftSignal(
            signal_id="dashboard-parity-signal",
            run_id=run.run_id,
            detected_at=run.analysis_time,
            cluster_id=trace.cluster_id,
            dimension="custom-dimension",
            evaluator_fingerprint=judgment.evaluator_fingerprint,
            example_trace_ids=[trace.trace_id],
            contributing_layers=["judge_rubric"],
        )

        for storage in (sqlite, postgres):
            storage.insert_trace(deepcopy(trace))
            storage.insert_judgment(deepcopy(judgment))
            storage.replace_drift_run(deepcopy(run), [deepcopy(signal)])

        sqlite.close()
        sqlite = None
        postgres.close()
        postgres = None

        sqlite_bundle = build_bundle(f"sqlite:///{sqlite_path}")
        postgres_bundle = build_bundle(scoped_dsn)

        assert postgres_bundle == sqlite_bundle
        assert postgres_bundle["meta"]["totalTraces"] == 1
        assert postgres_bundle["providers"][0]["rawProvider"] == "custom-provider"
        assert postgres_bundle["driftSignals"][0]["id"] == signal.signal_id
    finally:
        if sqlite is not None:
            sqlite.close()
        if postgres is not None:
            postgres.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
