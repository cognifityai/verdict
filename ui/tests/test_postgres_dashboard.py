"""Real PostgreSQL parity test for the packaged dashboard read path."""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier, Event, current_thread
from uuid import uuid4

import httpx
import pytest
import verdict.dashboard.analysis_service as analysis_service
import verdict.dashboard.scoped_evaluation as scoped_evaluation
from verdict.analysis_records import (
    AnalysisRunStatus,
    DeterministicAnalysisRun,
    analysis_run_to_json,
)
from verdict.dashboard import build_bundle
from verdict.dashboard.analysis_service import run_analysis
from verdict.dashboard.app import (
    build_agent_insights_bundle,
    build_agent_run_detail,
    build_agent_runs_bundle,
    build_agent_turn_analysis,
    build_agent_usage_bundle,
    build_registry_bundle,
    create_app,
)
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.evaluator_lab import _IdentityOnlyProvider
from verdict.dashboard.query import PostgresSession
from verdict.dashboard.run_outcome_preview import build_run_outcome_preview
from verdict.dashboard.scoped_evaluation import (
    cancel_scheduled_once,
    execute_scoped,
    preview_scoped,
    run_scheduled_once,
    schedule_scoped_once,
)
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
)
from verdict.schema import (
    ClusterIdentity,
    ClusterRegistryCluster,
    ClusterRegistryEvent,
    ClusterRegistryVersion,
    DimensionScore,
    DriftRun,
    DriftSignal,
    Judgment,
    Trace,
    TraceClusterAssignment,
    Verdict,
    cluster_candidate_digest,
)
from verdict.storage import SQLiteStorage
from verdict.storage.postgres import PostgresStorage
from verdict_eval.providers import CompletionResponse

DSN = os.environ.get("VERDICT_TEST_POSTGRES_DSN")
try:
    if DSN:
        from psycopg.conninfo import conninfo_to_dict

        TEST_DATABASE = conninfo_to_dict(DSN).get("dbname", "")
    else:
        TEST_DATABASE = ""
except Exception:
    TEST_DATABASE = ""
if not (
    os.environ.get("VERDICT_TEST_POSTGRES_ALLOW_ANY_DB") == "1"
    or "test" in TEST_DATABASE.lower()
):
    DSN = None
POSTGRES_SKIP_REASON = "no explicitly disposable live PostgreSQL database"

pytestmark = pytest.mark.skipif(DSN is None, reason=POSTGRES_SKIP_REASON)


def test_live_postgres_run_outcome_preview_matches_sqlite(tmp_path) -> None:
    import sqlite3

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_run_outcomes_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        sqlite_path = tmp_path / "outcomes.db"
        stores = (SQLiteStorage(str(sqlite_path)),
                  PostgresStorage(scoped_dsn, min_pool=1, max_pool=1))
        try:
            for store in stores:
                for tenant, run_id, status, error in (
                    ("customer-a", "success", "completed", False),
                    ("customer-a", "run-alice@example.com", "failed", True),
                    ("customer-a", "timeout", "timed_out", False),
                    ("customer-a", "unknown", "unknown", None),
                    ("customer-a", "unfinished", "completed", False),
                    ("customer-b", "FOREIGN-PRIVATE-RUN", "failed", True),
                ):
                    started = datetime(2026, 10, 19, tzinfo=timezone.utc)
                    source_id = f"source-{tenant}-{run_id}"
                    terminal = status != "unknown"
                    event = () if error is None else (AgentEvent(
                        f"event-{tenant}-{run_id}", f"turn-{tenant}-{run_id}", 0,
                        started, AgentEventType.TOOL_RESULT, ExecutionStatus(status),
                        "test-tool", {"is_error": error},
                    ),)
                    if run_id == "success":
                        event += (AgentEvent(
                            f"marker-{tenant}-{run_id}", f"turn-{tenant}-{run_id}", 1,
                            started, AgentEventType.CONTEXT, ExecutionStatus.UNKNOWN,
                            "verdict:capture_limit",
                            {"name": "source_events_omitted", "source": "1", "available": False},
                        ),)
                    store.replace_agent_run_bundle(AgentRunBundle(
                        SourceSession(source_id, tenant, "verdict_sdk",
                                      sha256(source_id.encode()).hexdigest(),
                                      started, started),
                        AgentRun(run_id, source_id, tenant, started,
                                 ExecutionStatus(status),
                                 started + timedelta(seconds=1) if terminal else None,
                                 agent_name="worker", service_name="app",
                                 environment="prod"),
                        (AgentTurn(f"turn-{tenant}-{run_id}", run_id, 0, started,
                                   ExecutionStatus(status),
                                   started + timedelta(seconds=1) if terminal else None),),
                        event,
                    ))
        finally:
            for store in stores:
                store.close()

        with sqlite3.connect(sqlite_path) as connection:
            connection.execute(
                "UPDATE agent_runs SET ended_at=NULL WHERE tenant_id=? AND run_id=?",
                ("customer-a", "unfinished"),
            )
            connection.execute(
                "UPDATE agent_runs SET ended_at=? WHERE tenant_id=? AND run_id=?",
                ((datetime(2026, 10, 19, tzinfo=timezone.utc)
                  + timedelta(minutes=1)).isoformat(), "customer-a", "unknown"),
            )
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute(
                "UPDATE agent_runs SET ended_at=NULL WHERE tenant_id=%s AND run_id=%s",
                ("customer-a", "unfinished"),
            )
            connection.execute(
                "UPDATE agent_runs SET ended_at=%s WHERE tenant_id=%s AND run_id=%s",
                (datetime(2026, 10, 19, tzinfo=timezone.utc)
                 + timedelta(minutes=1), "customer-a", "unknown"),
            )

        sqlite_result = build_run_outcome_preview(
            f"sqlite:///{sqlite_path}", tenant="customer-a")
        postgres_result = build_run_outcome_preview(scoped_dsn, tenant="customer-a")
        assert postgres_result == sqlite_result
        assert postgres_result["scope"]["terminalRuns"] == 3
        assert postgres_result["execution"]["totals"] == {
            "completed": 1, "failed": 1, "timed_out": 1,
            "cancelled": 0, "unfinalized": 2,
        }
        assert postgres_result["totals"]["observedWithoutFailure"] == 0
        assert postgres_result["totals"]["unclear"] == 1
        assert postgres_result["groups"][0]["comparison"] == sqlite_result["groups"][0]["comparison"]
        assert postgres_result["groups"][0]["comparison"]["earlier"]["terminalRuns"] == 1
        assert postgres_result["groups"][0]["comparison"]["newer"]["terminalRuns"] == 2
        assert "FOREIGN-PRIVATE-RUN" not in json.dumps(postgres_result)
        selected_day = "2026-10-19"
        selected_now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        sqlite_usage = build_agent_usage_bundle(
            f"sqlite:///{sqlite_path}", tenant="customer-a",
            now=selected_now, day=selected_day)
        postgres_usage = build_agent_usage_bundle(
            scoped_dsn, tenant="customer-a", now=selected_now, day=selected_day)
        assert postgres_usage["selectedDayIssues"] == sqlite_usage["selectedDayIssues"]
        assert postgres_usage["selectedDayIssues"]["count"] == 2
        assert "FOREIGN-PRIVATE-RUN" not in json.dumps(postgres_usage)
        exact_run_id = "run-alice@example.com"
        exact_event_id = f"event-customer-a-{exact_run_id}"
        listed = build_agent_runs_bundle(
            scoped_dsn, tenant="customer-a", view="list", selected_run_id=exact_run_id,
        )
        detail = build_agent_run_detail(
            scoped_dsn, tenant="customer-a", run_id=exact_run_id,
            event_id=exact_event_id,
        )
        assert listed["selectedRun"]["runId"] == exact_run_id
        assert detail["focusEventId"] == exact_event_id
        assert detail["events"][0]["eventId"] == exact_event_id
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute("DROP INDEX idx_agent_runs_tenant_ended")
        unmigrated = build_run_outcome_preview(scoped_dsn, tenant="customer-a")
        assert unmigrated["status"] == "migration_required"
        assert unmigrated["totals"] is None
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_live_postgres_run_preview_rejects_nonboolean_flags(tmp_path) -> None:
    import sqlite3

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_run_flag_types_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        sqlite_path = tmp_path / "flag-types.db"
        stores = (SQLiteStorage(str(sqlite_path)),
                  PostgresStorage(scoped_dsn, min_pool=1, max_pool=1))
        try:
            for store in stores:
                for run_id in ("integer-zero", "integer-one", "text-false",
                               "text-true", "non-object"):
                    started = datetime(2026, 10, 19, tzinfo=timezone.utc)
                    source_id = f"source-{run_id}"
                    store.replace_agent_run_bundle(AgentRunBundle(
                        SourceSession(source_id, "customer-a", "verdict_sdk",
                                      sha256(source_id.encode()).hexdigest(),
                                      started, started),
                        AgentRun(run_id, source_id, "customer-a", started,
                                 ExecutionStatus.COMPLETED,
                                 started + timedelta(seconds=1), agent_name="worker"),
                        (AgentTurn(f"turn-{run_id}", run_id, 0, started,
                                   ExecutionStatus.COMPLETED,
                                   started + timedelta(seconds=1)),),
                        (AgentEvent(f"event-{run_id}", f"turn-{run_id}", 0,
                                    started, AgentEventType.TOOL_RESULT,
                                    ExecutionStatus.COMPLETED if run_id == "non-object"
                                    else ExecutionStatus.UNKNOWN, "tool", {}),),
                    ))
        finally:
            for store in stores:
                store.close()
        for run_id, flag in (("integer-zero", 0), ("integer-one", 1),
                             ("text-false", "false"), ("text-true", "true"),
                             ("non-object", [])):
            attributes = flag if run_id == "non-object" else {"is_error": flag}
            with sqlite3.connect(sqlite_path) as connection:
                connection.execute(
                    "UPDATE agent_events SET attributes_json=? WHERE run_id=?",
                    (json.dumps(attributes), run_id),
                )
            with psycopg.connect(scoped_dsn) as connection:
                connection.execute(
                    "UPDATE agent_events SET attributes_json=%s::jsonb WHERE run_id=%s",
                    (json.dumps(attributes), run_id),
                )
        sqlite_result = build_run_outcome_preview(
            f"sqlite:///{sqlite_path}", tenant="customer-a")
        postgres_result = build_run_outcome_preview(scoped_dsn, tenant="customer-a")
        assert postgres_result == sqlite_result
        assert postgres_result["totals"] == {
            "recordedFailure": 0, "observedWithoutFailure": 0,
            "unclear": 5, "conflicts": 0,
        }
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_live_postgres_run_preview_uses_one_snapshot_during_writer(monkeypatch) -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_run_snapshot_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        try:
            started = datetime(2026, 10, 19, tzinfo=timezone.utc)
            storage.replace_agent_run_bundle(AgentRunBundle(
                SourceSession("source", "customer-a", "verdict_sdk",
                              sha256(b"source").hexdigest(), started, started),
                AgentRun("run", "source", "customer-a", started,
                         ExecutionStatus.COMPLETED, started + timedelta(seconds=1),
                         agent_name="old-worker"),
                (AgentTurn("turn", "run", 0, started, ExecutionStatus.COMPLETED,
                           started + timedelta(seconds=1)),),
                (AgentEvent("event", "turn", 0, started, AgentEventType.TOOL_RESULT,
                            ExecutionStatus.COMPLETED, "tool", {"is_error": False}),),
            ))
        finally:
            storage.close()

        run_selected = Event()
        writer_done = Event()
        original_execute = PostgresSession.execute

        def pause_after_run_select(self, query, params=()):
            result = original_execute(self, query, params)
            if query.startswith("SELECT r.run_id,r.agent_name"):
                run_selected.set()
                assert writer_done.wait(10), "writer did not commit after run selection"
            return result

        monkeypatch.setattr(PostgresSession, "execute", pause_after_run_select)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(build_run_outcome_preview, scoped_dsn,
                                     tenant="customer-a")
            try:
                assert run_selected.wait(10), "preview did not select runs"
                with psycopg.connect(scoped_dsn) as connection:
                    connection.execute(
                        "UPDATE agent_runs SET agent_name='new-worker' WHERE run_id='run'")
                    connection.execute(
                        "UPDATE agent_events SET status='failed' WHERE event_id='event'")
            finally:
                writer_done.set()
            result = future.result(timeout=10)
        assert result["groups"][0]["agentName"] == "old-worker"
        assert result["totals"]["observedWithoutFailure"] == 1
        assert result["totals"]["recordedFailure"] == 0
        after = build_run_outcome_preview(scoped_dsn, tenant="customer-a")
        assert after["groups"][0]["agentName"] == "new-worker"
        assert after["totals"]["recordedFailure"] == 1
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_live_postgres_monitor_activation_rejects_foreign_policy_id() -> None:
    import asyncio

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_monitor_tenant_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        try:
            for index in range(20):
                started = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
                storage.insert_trace(Trace(
                    trace_id=f"foreign-trace-{index}", tenant_id="customer-b",
                    started_at=started, ended_at=started + timedelta(seconds=1),
                    provider="openai", request_model="model",
                    prompt_redacted="request", response_redacted="ok",
                ))
        finally:
            storage.close()

        async def exercise():
            foreign_app = create_app(storage=scoped_dsn, tenant_id="customer-b")
            own_app = create_app(storage=scoped_dsn, tenant_id="customer-a")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=foreign_app), base_url="http://testserver"
            ) as foreign:
                token = (await foreign.get("/api/setup/token")).json()["setupToken"]
                preview = await foreign.post(
                    "/api/monitor/preview", headers={"X-Verdict-Setup": token},
                    json={"analysisUnit": "trace", "windowMode": "count",
                          "referenceRatio": 0.5, "minimumReference": 5,
                          "minimumCurrent": 5},
                )
                assert preview.status_code == 200
                policy_id = preview.json()["policy"]["policy_id"]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=own_app), base_url="http://testserver"
            ) as own:
                token = (await own.get("/api/setup/token")).json()["setupToken"]
                activation = await own.post(
                    "/api/monitor/activate", headers={"X-Verdict-Setup": token},
                    json={"policyId": policy_id, "expectedActivePolicyId": None},
                )
                missing = await own.post(
                    "/api/monitor/activate", headers={"X-Verdict-Setup": token},
                    json={"policyId": "policy-does-not-exist",
                          "expectedActivePolicyId": None},
                )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=foreign_app), base_url="http://testserver"
            ) as foreign:
                token = (await foreign.get("/api/setup/token")).json()["setupToken"]
                accepted = await foreign.post(
                    "/api/monitor/activate", headers={"X-Verdict-Setup": token},
                    json={"policyId": policy_id, "expectedActivePolicyId": None},
                )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=own_app), base_url="http://testserver"
            ) as own:
                token = (await own.get("/api/setup/token")).json()["setupToken"]
                state = await own.get("/api/monitor")
                run = await own.post("/api/monitor/run", headers={"X-Verdict-Setup": token})
            return activation, missing, accepted, state, run, policy_id

        activation, missing, accepted, state, run, policy_id = asyncio.run(exercise())
        assert activation.status_code == 400
        assert activation.json() == {"error": "invalid monitor activation"}
        assert (missing.status_code, missing.json()) == (activation.status_code, activation.json())
        assert accepted.status_code == 200
        assert state.status_code == 200
        assert state.json()["state"] == "not_configured"
        assert run.status_code == 409
        assert run.json() == {"error": "no active monitor"}
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        try:
            assert storage.get_monitor_policy(policy_id)[1] == "active"
            assert storage.get_active_monitor_policy("customer-b:application:trace") is not None
            assert storage.get_active_monitor_policy("customer-a:application:trace") is None
        finally:
            storage.close()
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_read_only_dashboard_reports_legacy_analysis_without_revision_column() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_legacy_report_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        run = DeterministicAnalysisRun(
            analysis_id="a" * 64, tenant_id="local", scope_key="agent-and-trace",
            cutoff=now, completed_at=now, status=AnalysisRunStatus.COMPLETED,
            analyzer_version="agent-insights-v5", input_fingerprint="b" * 64,
            result={"schema": "agent-insights-v2", "scope": {"availableRuns": 0,
                "analyzedRuns": 0, "complete": True, "traces": {"available": 0,
                    "analyzed": 0, "complete": True}}},
        )
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute("CREATE TABLE deterministic_analysis_runs ("
                               "analysis_id TEXT PRIMARY KEY, tenant_id TEXT, scope_key TEXT, "
                               "analyzer_version TEXT, completed_at TIMESTAMPTZ, payload_json TEXT)")
            connection.execute(
                "INSERT INTO deterministic_analysis_runs VALUES (%s,%s,%s,%s,%s,%s)",
                (run.analysis_id, run.tenant_id, run.scope_key, run.analyzer_version,
                 run.completed_at, analysis_run_to_json(run)),
            )
        coverage = build_bundle(scoped_dsn, registry_tenant="local")["coverage"][
            "deterministicAnalysis"
        ]
        assert coverage["status"] == "completed"
        assert coverage["complete"] is True
        assert coverage["needsRefresh"] is True

        async def request_data():
            transport = httpx.ASGITransport(app=create_app(storage=scoped_dsn, tenant_id="local"))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get("/api/data")

        import asyncio

        response = asyncio.run(request_data())
        assert response.status_code == 200
        assert response.json()["coverage"]["deterministicAnalysis"]["needsRefresh"] is True
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_live_postgres_run_outcome_preview_requires_matched_tool_results(tmp_path) -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_run_pairing_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        stores = (SQLiteStorage(str(tmp_path / "run-pairing.db")),
                  PostgresStorage(scoped_dsn, min_pool=1, max_pool=1))
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        try:
            for store in stores:
                for tenant, run_id, outcomes in (
                    ("customer-a", "complete", ("a", "b")),
                    ("customer-a", "sparse", ("a",)),
                    ("customer-a", "unknown-result", ("a", "b")),
                    ("customer-b", "foreign", ("a",)),
                ):
                    source = f"source-{tenant}-{run_id}"
                    turn = f"turn-{tenant}-{run_id}"
                    events = [AgentEvent(
                        f"{run_id}-call-{index}", turn, index, now,
                        AgentEventType.TOOL_CALL, ExecutionStatus.UNKNOWN,
                        "synthetic", {"call_id": call_id},
                    ) for index, call_id in enumerate(("a", "b"))]
                    events.extend(AgentEvent(
                        f"{run_id}-result-{index}", turn, index + 2, now,
                        AgentEventType.TOOL_RESULT,
                        (ExecutionStatus.UNKNOWN if run_id == "unknown-result" and index == 0
                         else ExecutionStatus.COMPLETED),
                        "synthetic", {"call_id": call_id, "is_error": False},
                    ) for index, call_id in enumerate(outcomes))
                    store.replace_agent_run_bundle(AgentRunBundle(
                        SourceSession(source, tenant, "verdict_sdk",
                                      sha256(source.encode()).hexdigest(), now, now),
                        AgentRun(run_id, source, tenant, now, ExecutionStatus.COMPLETED,
                                 now + timedelta(seconds=1), agent_name="worker"),
                        (AgentTurn(turn, run_id, 0, now, ExecutionStatus.COMPLETED,
                                   now + timedelta(seconds=1)),),
                        tuple(events),
                    ))
        finally:
            for store in stores:
                store.close()
        sqlite_result = build_run_outcome_preview(
            f"sqlite:///{tmp_path / 'run-pairing.db'}", tenant="customer-a")
        postgres_result = build_run_outcome_preview(scoped_dsn, tenant="customer-a")
        assert postgres_result == sqlite_result
        assert postgres_result["totals"]["observedWithoutFailure"] == 1
        assert postgres_result["totals"]["unclear"] == 2
        assert "foreign" not in json.dumps(postgres_result)
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_same_trace_rewrite_and_no_change_recheck_have_revision() -> None:
    with contextlib.nullcontext(DSN) as scoped_dsn:
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=2)
        try:
            tenant = f"revision-{uuid4().hex}"
            now = datetime(2026, 10, 17, tzinfo=timezone.utc)
            trace = Trace(trace_id=f"trace-{uuid4().hex}", tenant_id=tenant,
                          started_at=now, prompt_redacted="synthetic request",
                          response_redacted="synthetic response")
            storage.insert_trace(trace)
            def build():
                return build_agent_insights_bundle(
                    scoped_dsn, tenant=tenant, _include_input_fingerprint=True,
                )
            first = run_analysis(scoped_dsn, tenant=tenant, build=build)
            storage.insert_trace(trace)
            repeated = run_analysis(scoped_dsn, tenant=tenant, build=build)
            assert repeated["analysisState"]["analysisId"] == first["analysisState"]["analysisId"]
            assert repeated["analysisState"]["verifiedRevision"] > first["analysisState"][
                "verifiedRevision"
            ]
            storage.insert_trace(replace(trace, error="synthetic error"))
            metadata = build_bundle(scoped_dsn, registry_tenant=tenant)["meta"]
            assert metadata["totalTraces"] == 1
            assert metadata["evidenceRevision"] > repeated["analysisState"]["verifiedRevision"]
            stale_coverage = build_bundle(scoped_dsn, registry_tenant=tenant)["coverage"][
                "deterministicAnalysis"
            ]
            assert stale_coverage["complete"] is True
            assert stale_coverage["needsRefresh"] is True
            changed = run_analysis(scoped_dsn, tenant=tenant, build=build)
            assert changed["analysisState"]["analysisId"] != first["analysisState"]["analysisId"]
            storage.insert_trace(trace)
            reverted = run_analysis(scoped_dsn, tenant=tenant, build=build)
            assert reverted["analysisState"]["analysisId"] == first["analysisState"]["analysisId"]
            assert build_bundle(scoped_dsn, registry_tenant=tenant)["coverage"][
                "deterministicAnalysis"
            ]["analysisId"] == reverted["analysisState"]["analysisId"]
            assert build_bundle(scoped_dsn, registry_tenant=tenant)["coverage"][
                "deterministicAnalysis"
            ]["needsRefresh"] is False
        finally:
            storage._exec("DELETE FROM traces WHERE tenant_id=%s", (tenant,))
            storage._exec("DELETE FROM deterministic_analysis_runs WHERE tenant_id=%s", (tenant,))
            storage._exec("DELETE FROM evidence_revisions WHERE tenant_id=%s", (tenant,))
            storage.close()


def test_postgres_bounded_analysis_reidentifies_out_of_sample_trace() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_bounded_fingerprint_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        tenant = "synthetic"
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.insert_trace(Trace(trace_id="earlier", tenant_id=tenant, started_at=now))
        storage.close()

        def build():
            return build_agent_insights_bundle(
                scoped_dsn, tenant=tenant, scan_limit=1, _include_input_fingerprint=True,
            )

        first = run_analysis(scoped_dsn, tenant=tenant, build=build)
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.insert_trace(Trace(trace_id="later", tenant_id=tenant,
                                   started_at=now + timedelta(hours=1)))
        storage.close()
        changed = run_analysis(scoped_dsn, tenant=tenant, build=build)
        assert changed["scope"]["traces"] == {
            "available": 2, "analyzed": 1, "complete": False,
        }
        assert changed["analysisState"]["analysisId"] != first["analysisState"]["analysisId"]
        assert run_analysis(scoped_dsn, tenant=tenant, build=build)[
            "analysisState"
        ]["analysisId"] == changed["analysisState"]["analysisId"]
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_concurrent_same_identity_recheck_repoints_current(monkeypatch) -> None:
    tenant = f"analysis-race-{uuid4().hex}"
    instant = datetime(2026, 10, 17, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(analysis_service, "datetime", FrozenDatetime)
    original_save = PostgresStorage.save_deterministic_analysis_run
    entered, resume = Event(), Event()

    def delayed_save(self, run):
        if current_thread().name.startswith("delayed-analysis"):
            entered.set()
            assert resume.wait(10)
        return original_save(self, run)

    monkeypatch.setattr(PostgresStorage, "save_deterministic_analysis_run", delayed_save)

    def build(fingerprint):
        return lambda: {"schema": "agent-insights-v2", "sourceActivity": [],
                        "_analysisInputFingerprint": fingerprint}

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="delayed-analysis") as pool:
        delayed = pool.submit(run_analysis, DSN, tenant=tenant, build=build("a" * 64))
        assert entered.wait(10)
        try:
            competing = run_analysis(DSN, tenant=tenant, build=build("a" * 64))
            newer = run_analysis(DSN, tenant=tenant, build=build("b" * 64))
            storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
            try:
                assert storage.get_latest_deterministic_analysis_run(
                    tenant, "agent-and-trace", analyzer_version="agent-insights-v5",
                ).analysis_id == newer["analysisState"]["analysisId"]
            finally:
                storage.close()
        finally:
            resume.set()
        returned = delayed.result(timeout=10)
    assert returned["analysisState"]["analysisId"] == competing[
        "analysisState"
    ]["analysisId"]
    storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
    try:
        assert storage.get_latest_deterministic_analysis_run(
            tenant, "agent-and-trace", analyzer_version="agent-insights-v5",
        ).analysis_id == returned["analysisState"]["analysisId"]
    finally:
        storage._exec("DELETE FROM deterministic_analysis_runs WHERE tenant_id=%s", (tenant,))
        storage.close()


def _postgres_scoped_worker(scoped_dsn, approved, start, calls, outcomes):
    child_storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)

    class SlowProvider:
        name = "anthropic"
        supports_temperature = _IdentityOnlyProvider("anthropic").supports_temperature

        def complete(self, request):
            calls.put("called")
            time.sleep(0.4)
            return CompletionResponse(text=(
                '{"completion":{"reasoning":"[turn:turn] records completion",'
                '"verdict":"PASS"}}'
            ), input_tokens=10, output_tokens=10)

    start.wait()
    try:
        result = execute_scoped(
            child_storage, storage_url=scoped_dsn, tenant="local", config=approved,
            confirm_external_egress=True, provider=SlowProvider(),
        )
        outcomes.put(("ok", result["resultId"]))
    except Exception as exc:
        outcomes.put(("error", type(exc).__name__))
    finally:
        child_storage.close()


def _postgres_waiting_scheduled_worker(scoped_dsn, signal, calls, outcomes):
    original_guard = scoped_evaluation._cross_process_scoped_guard

    @contextlib.contextmanager
    def signalled_guard(storage_url):
        signal.put("waiting")
        with original_guard(storage_url):
            yield

    class CountingProvider:
        name = "anthropic"
        supports_temperature = _IdentityOnlyProvider("anthropic").supports_temperature

        def complete(self, request):
            calls.put("called")
            return CompletionResponse(text=(
                '{"completion":{"reasoning":"[turn:turn] records completion",'
                '"verdict":"PASS"}}'
            ), input_tokens=10, output_tokens=10)

    scoped_evaluation._cross_process_scoped_guard = signalled_guard
    try:
        outcomes.put(run_scheduled_once(scoped_dsn, tenant="local", provider=CountingProvider()))
    except Exception as exc:
        outcomes.put({"status": "error", "error": type(exc).__name__})


@pytest.mark.skipif(os.name != "posix", reason="cross-process fixture requires POSIX")
def test_live_postgres_scheduled_approval_stays_pending_during_scoped_lock() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_scheduled_lock_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        now = datetime.now(timezone.utc)
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.replace_agent_run_bundle(AgentRunBundle(
            SourceSession("source", "local", "synthetic", "a" * 64, now, now),
            AgentRun("run", "source", "local", now, ExecutionStatus.COMPLETED, now),
            (AgentTurn("turn", "run", 0, now, ExecutionStatus.COMPLETED, now,
                       "Do the task", "Task complete",
                       EvidenceState.PRESENT, EvidenceState.PRESENT),), (),
        ))
        config = {"scope": "run", "runId": "run", "provider": "anthropic",
                  "model": "claude-haiku-4-5", "maxOutputTokens": 256,
                  "rubric": {"name": "task_review", "version": "1", "dimensions": [
                      {"name": "completion", "description": "Did the task complete?"},
                  ]}}
        preview = preview_scoped(storage, tenant="local", config=config)
        queued = schedule_scoped_once(
            storage, storage_url=scoped_dsn, tenant="local",
            config={**config, "planFingerprint": preview["planFingerprint"]},
            max_estimated_usd=1, confirm_persistent_egress=True,
        )
        storage.close()
        reopened = scoped_evaluation._scheduled_storage(scoped_dsn)
        try:
            assert preview_scoped(reopened, tenant="local", config=config,
                                  storage_url=scoped_dsn)["planFingerprint"] == preview["planFingerprint"]
        finally:
            reopened.close()
        context = multiprocessing.get_context("spawn")
        signal = context.Queue()
        calls = context.Queue()
        outcomes = context.Queue()
        process = context.Process(target=_postgres_waiting_scheduled_worker,
                                  args=(scoped_dsn, signal, calls, outcomes))
        with scoped_evaluation._cross_process_scoped_guard(scoped_dsn):
            process.start()
            try:
                assert signal.get(timeout=10) == "waiting"
            except queue.Empty:
                pytest.fail(f"worker did not reach lock: {outcomes.get_nowait()}")
            current = ControlStore(scoped_dsn).history(
                "local", "scheduled_evaluation", queued["jobId"],
            )[0]
            assert current["state"] == "pending"
            cancel_scheduled_once(scoped_dsn, tenant="local", job_id=queued["jobId"],
                                  expected_revision=queued["revision"])
        process.join(timeout=10)
        assert process.exitcode == 0
        assert outcomes.get(timeout=1)["status"] == "approval_changed"
        assert ControlStore(scoped_dsn).history(
            "local", "scheduled_evaluation", queued["jobId"],
        )[0]["state"] == "cancelled"
        with pytest.raises(queue.Empty):
            calls.get_nowait()
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.skipif(os.name != "posix", reason="cross-process fixture requires POSIX")
def test_live_postgres_identical_scoped_evaluations_call_provider_once() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_scoped_lock_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        now = datetime.now(timezone.utc)
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.replace_agent_run_bundle(AgentRunBundle(
            SourceSession("source", "local", "synthetic", "a" * 64, now, now),
            AgentRun("run", "source", "local", now, ExecutionStatus.COMPLETED, now),
            (AgentTurn("turn", "run", 0, now, ExecutionStatus.COMPLETED, now,
                       "Do the task", "Task complete",
                       EvidenceState.PRESENT, EvidenceState.PRESENT),), (),
        ))
        config = {"scope": "run", "runId": "run", "provider": "anthropic",
                  "model": "claude-haiku-4-5", "maxOutputTokens": 256,
                  "rubric": {"name": "task_review", "version": "1", "dimensions": [
                      {"name": "completion", "description": "Did the task complete?"},
                  ]}}
        preview = preview_scoped(storage, tenant="local", config=config)
        approved = {**config, "planFingerprint": preview["planFingerprint"]}
        storage.close()
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        calls = context.Queue()
        outcomes = context.Queue()

        processes = [context.Process(target=_postgres_scoped_worker,
                                     args=(scoped_dsn, approved, start, calls, outcomes))
                     for _ in range(2)]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        results = [outcomes.get(timeout=1) for _ in processes]
        assert results[0][0] == results[1][0] == "ok"
        assert results[0][1] == results[1][1]
        assert calls.get(timeout=1) == "called"
        with pytest.raises(queue.Empty):
            calls.get_nowait()
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_live_postgres_only_one_pending_approval_per_plan() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_approval_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        store = ControlStore(scoped_dsn)
        store.list_current("local")
        barrier = Barrier(2)
        payload = {"planFingerprint": "a" * 64, "config": {"scope": "run"}}

        def queue(index: int) -> str:
            barrier.wait()
            try:
                return store.append(
                    "local", kind="scheduled_evaluation", document_id=f"approval-{index}",
                    state="pending", payload=payload, expected_revision=None,
                )["state"]
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(queue, range(2))) == ["pending", "rejected"]
        pending = [row for row in store.list_current("local")
                   if row["kind"] == "scheduled_evaluation" and row["state"] == "pending"]
        assert len(pending) == 1
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_live_postgres_turn_analysis_reads_one_tenant_and_links_evidence() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_turn_analysis_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.replace_agent_run_bundle(AgentRunBundle(
            SourceSession("source", "local", "codex", "a" * 64, now, now),
            AgentRun("run", "source", "local", now, ExecutionStatus.UNKNOWN),
            (AgentTurn("turn", "run", 0, now, ExecutionStatus.UNKNOWN),),
            (AgentEvent("test", "turn", 0, now, AgentEventType.TEST_RESULT,
                        ExecutionStatus.FAILED, "synthetic", {"failed": 1},
                        PrivacyClassification.METADATA),),
        ))
        storage.close()
        analysis = build_agent_turn_analysis(scoped_dsn, tenant="local", run_id="run", turn_id="turn")
        assert analysis["reviewSignals"] == [
            {"eventId": "test", "code": "test_failed", "label": "Test execution reported failure"},
        ]
        with pytest.raises(KeyError):
            build_agent_turn_analysis(scoped_dsn, tenant="other", run_id="run", turn_id="turn")
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_live_postgres_later_turn_inherits_source_capture_gap() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_turn_limit_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.replace_agent_run_bundle(AgentRunBundle(
            SourceSession("source", "local", "synthetic-codex", "a" * 64, now, now),
            AgentRun("run", "source", "local", now, ExecutionStatus.UNKNOWN),
            (AgentTurn("before", "run", 0, now, ExecutionStatus.COMPLETED, now),
             AgentTurn("marker", "run", 1, now, ExecutionStatus.COMPLETED, now),
             AgentTurn("later", "run", 2, now, ExecutionStatus.COMPLETED, now)),
            (AgentEvent("limit", "marker", 0, now, AgentEventType.CONTEXT,
                        ExecutionStatus.UNKNOWN, "verdict:capture_limit",
                        {"name": "source_events_omitted", "source": "4", "available": False},
                        PrivacyClassification.METADATA),),
        ))
        storage.close()
        before = build_agent_turn_analysis(scoped_dsn, tenant="local", run_id="run", turn_id="before")
        later = build_agent_turn_analysis(scoped_dsn, tenant="local", run_id="run", turn_id="later")
        assert before["eventCoverage"]["complete"] is True
        assert before["eventCoverage"]["sourcePartial"] is False
        assert later["eventCoverage"]["available"] == 0
        assert later["eventCoverage"]["complete"] is False
        assert later["eventCoverage"]["sourcePartial"] is True
        with pytest.raises(KeyError):
            build_agent_turn_analysis(scoped_dsn, tenant="other", run_id="run", turn_id="later")
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_live_postgres_run_parent_availability_is_tenant_scoped() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_run_parent_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        for index, (tenant, run_id, parent_id) in enumerate((
            ("local", "parent", None),
            ("local", "child", "parent"),
            ("local", "orphan", "missing"),
            ("other", "foreign-child", "parent"),
        )):
            source_id = f"source-{tenant}-{run_id}"
            storage.replace_agent_run_bundle(AgentRunBundle(
                SourceSession(source_id, tenant, "synthetic-agent", f"{index + 1:064x}", now, now),
                AgentRun(run_id, source_id, tenant, now, ExecutionStatus.UNKNOWN,
                         parent_run_id=parent_id),
            ))
        storage.close()
        assert build_agent_run_detail(scoped_dsn, tenant="local", run_id="parent")["parentAvailable"] is None
        assert build_agent_run_detail(scoped_dsn, tenant="local", run_id="child")["parentAvailable"] is True
        assert build_agent_run_detail(scoped_dsn, tenant="local", run_id="orphan")["parentAvailable"] is False
        assert build_agent_run_detail(scoped_dsn, tenant="other", run_id="foreign-child")["parentAvailable"] is False
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_live_postgres_session_usage_matches_jsonb_model_evidence() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_usage_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        turn = AgentTurn(
            "turn", "run", 0, now, ExecutionStatus.COMPLETED, now,
            input_tokens=80, output_tokens=20, total_tokens=100,
            token_usage_basis="codex_turn_delta",
        )
        event = AgentEvent(
            "model", "turn", 0, now, AgentEventType.CONTEXT,
            ExecutionStatus.COMPLETED, "test",
            {"name": "model_configuration", "source": "gpt-6-luna"},
            PrivacyClassification.METADATA,
        )
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.replace_agent_run_bundle(AgentRunBundle(
            SourceSession("source", "local", "codex", "a" * 64, now, now),
            AgentRun("run", "source", "local", now, ExecutionStatus.UNKNOWN,
                     session_id="session"),
            (turn,), (event,),
        ))
        storage.close()
        report = build_agent_usage_bundle(scoped_dsn, tenant="local", now=now,
                                          day=now.date().isoformat(), session_id="session")
        day = next(row for row in report["days"] if row["day"] == now.date().isoformat())
        assert day["totalTokens"] == 100
        assert day["pricedTurns"] == 1
        assert report["dayModels"][0]["model"] == "gpt-6-luna"
        assert report["selectedSession"]["turnCount"] == 1
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_live_postgres_scoped_preview_and_saved_case_experiment(monkeypatch) -> None:
    import asyncio

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_profiler_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        now = datetime.now(timezone.utc)
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.replace_agent_run_bundle(AgentRunBundle(
            SourceSession("source", "local", "synthetic", "a" * 64, now, now),
            AgentRun("run", "source", "local", now, ExecutionStatus.COMPLETED, now),
            (AgentTurn("turn", "run", 0, now, ExecutionStatus.COMPLETED, now,
                       "Do the task", "Task complete",
                       EvidenceState.PRESENT, EvidenceState.PRESENT),), (),
        ))
        storage.close()
        from verdict.service import CycleBusy, cycle_guard

        with cycle_guard(scoped_dsn), pytest.raises(CycleBusy):
            with cycle_guard(scoped_dsn):
                pass
        monkeypatch.setenv("VERDICT_EXPERIMENT_RUNNER", "verdict.dashboard.demo_task:run")
        monkeypatch.setenv("VERDICT_EXPERIMENT_CONFIGS", json.dumps({
            "baseline": {"version": "baseline-v1"}, "candidate": {"version": "candidate-v1"},
        }))

        async def request():
            transport = httpx.ASGITransport(app=create_app(storage=scoped_dsn, tenant_id="local"))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                token = (await client.get("/api/setup/token")).json()["setupToken"]
                headers = {"X-Verdict-Setup": token}
                preview = await client.post("/api/scoped/preview", json={
                    "scope": "run", "runId": "run", "provider": "anthropic",
                    "model": "claude-haiku-4-5", "rubric": {"name": "task_review", "version": "1",
                        "dimensions": [{"name": "completion", "description": "Did it do the task?"}]},
                }, headers=headers)
                saved = await client.post("/api/experiments/case-sets/cases", json={
                    "title": "Synthetic", "reviewed": True, "cases": [
                        {"id": "critical_outage",
                         "input": "Synthetic incident: checkout unavailable for all users; severity P1",
                         "expectedBehavior": "Page on-call",
                         "checks": [{"kind": "exact", "value": "page_on_call"}]},
                    ],
                }, headers=headers)
                ran = await client.post("/api/experiments/run", json={
                    "caseSetId": "cases", "baseline": "baseline", "candidate": "candidate",
                }, headers=headers)
                return preview, saved, ran

        preview, saved, ran = asyncio.run(request())
        assert preview.status_code == 200, preview.text
        assert preview.json()["plannedCalls"] == 1
        assert saved.status_code == 200, saved.text
        assert ran.status_code == 200, ran.text
        assert ran.json()["comparison"]["fixed"] == ["critical_outage"]
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize("foreign_trace", [False, True])
def test_live_postgres_empty_tenant_view_reports_only_backend(
    foreign_trace: bool,
) -> None:
    import asyncio

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_empty_dashboard_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        if foreign_trace:
            storage.insert_trace(Trace(
                trace_id="foreign-tenant-canary",
                tenant_id="tenant-b",
                provider="openai",
                request_model="gpt-4.1-mini",
            ))
        storage.close()

        async def request_data():
            transport = httpx.ASGITransport(
                app=create_app(storage=scoped_dsn, tenant_id="tenant-a"),
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.get("/api/data")

        response = asyncio.run(request_data())

        assert response.status_code == 200, response.text
        assert response.json()["meta"]["storageBackend"] == "postgresql"
        assert response.json()["meta"]["totalAgentRuns"] == 0
        assert response.json()["meta"]["totalTraces"] == 0
        assert "storageConfigured" not in response.json()["meta"]
        assert "foreign-tenant-canary" not in response.text
        assert scoped_dsn not in response.text
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def test_live_postgres_control_center_and_analysis_use_conninfo() -> None:
    import asyncio

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_control_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        assert ControlStore(scoped_dsn).postgres is True

        async def request_control():
            transport = httpx.ASGITransport(app=create_app(storage=scoped_dsn))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                token = (await client.get("/api/setup/token")).json()["setupToken"]
                saved = await client.post(
                    "/api/control/settings/default",
                    headers={"X-Verdict-Setup": token},
                    json={
                        "state": "active",
                        "payload": {"captureContent": True},
                        "expectedRevision": None,
                    },
                )
                analysis = await client.post(
                    "/api/insights/run", headers={"X-Verdict-Setup": token}
                )
                return saved, analysis, await client.get("/api/control")

        saved, analysis, response = asyncio.run(request_control())
        assert saved.status_code == 200, saved.text
        assert analysis.status_code == 200, analysis.text
        assert response.status_code == 200
        assert response.json()["dailyOperations"]["mode"] == "telemetry"
        with psycopg.connect(scoped_dsn) as verification:
            assert verification.execute(
                "SELECT count(*) FROM product_control_documents"
            ).fetchone()[0] == 1
            assert verification.execute(
                "SELECT count(*) FROM deterministic_analysis_runs"
            ).fetchone()[0] == 1
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def test_live_postgres_and_sqlite_produce_the_same_dashboard_bundle(
    monkeypatch,
    tmp_path,
) -> None:
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
        monkeypatch.setattr(
            "verdict.dashboard.app._now_utc",
            lambda: started_at + timedelta(hours=1),
        )
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
            tags={"verdict.workload": "agent"},
        )
        failed_trace = Trace(
            trace_id="dashboard-parity-failed-trace",
            started_at=started_at,
            provider="custom-provider",
            request_model="custom-model",
            prompt_redacted="captured prompt",
            response_redacted="partial response",
            error="provider failed",
            tags={"verdict.workload": "agent"},
        )
        empty_error_trace = Trace(
            trace_id="dashboard-parity-empty-error-trace",
            started_at=started_at,
            provider="custom-provider",
            request_model="custom-model",
            prompt_redacted="captured prompt",
            response_redacted="captured response",
            error="",
            tags={"verdict.workload": "agent"},
        )
        whitespace_trace = Trace(
            trace_id="dashboard-parity-whitespace-trace",
            started_at=started_at,
            provider="custom-provider",
            request_model="custom-model",
            prompt_redacted="\t\n\N{NO-BREAK SPACE}",
            response_redacted="captured response",
            tags={"verdict.workload": "agent"},
        )
        judge_trace = Trace(
            trace_id="dashboard-parity-judge-trace",
            started_at=started_at + timedelta(minutes=5),
            provider="custom-judge-provider",
            request_model="custom-judge-model",
            tags={"verdict.workload": "judge"},
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
            dimensions=[DimensionScore(name="custom-dimension", verdict=Verdict.FAIL)],
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
        agent_bundle = AgentRunBundle(
            SourceSession(
                "dashboard-parity-session", "__verdict_local__", "codex",
                "a" * 64, started_at, started_at,
            ),
            AgentRun(
                "dashboard-parity-agent-run", "dashboard-parity-session",
                "__verdict_local__", started_at, ExecutionStatus.UNKNOWN,
            ),
        )

        for storage in (sqlite, postgres):
            storage.insert_trace(deepcopy(trace))
            storage.insert_trace(deepcopy(failed_trace))
            storage.insert_trace(deepcopy(empty_error_trace))
            storage.insert_trace(deepcopy(whitespace_trace))
            storage.insert_trace(deepcopy(judge_trace))
            storage.insert_judgment(deepcopy(judgment))
            storage.replace_drift_run(deepcopy(run), [deepcopy(signal)])
            storage.replace_agent_run_bundle(deepcopy(agent_bundle))

        sqlite.close()
        sqlite = None
        postgres.close()
        postgres = None

        sqlite_bundle = build_bundle(f"sqlite:///{sqlite_path}")
        postgres_bundle = build_bundle(scoped_dsn)
        sqlite_page = build_bundle(f"sqlite:///{sqlite_path}", trace_offset=1)
        postgres_page = build_bundle(scoped_dsn, trace_offset=1)

        for bundle in (sqlite_bundle, postgres_bundle, sqlite_page, postgres_page):
            assert bundle["meta"]["lastAgentStoredAt"]
            bundle["meta"]["lastAgentStoredAt"] = "<backend write time>"
        assert postgres_bundle == sqlite_bundle
        assert postgres_page == sqlite_page
        sqlite_day = build_bundle(f"sqlite:///{sqlite_path}", trace_date="2026-08-20")
        postgres_day = build_bundle(scoped_dsn, trace_date="2026-08-20")
        assert [sample["trace_id"] for sample in postgres_day["samples"]] == [
            sample["trace_id"] for sample in sqlite_day["samples"]
        ]
        assert postgres_day["truncation"]["resources"]["traceSamples"] == (
            sqlite_day["truncation"]["resources"]["traceSamples"]
        )
        selected_id = sqlite_day["samples"][0]["trace_id"]
        sqlite_selected = build_bundle(
            f"sqlite:///{sqlite_path}", trace_date="2026-08-20",
            trace_offset=1, trace_id=selected_id,
        )
        postgres_selected = build_bundle(
            scoped_dsn, trace_date="2026-08-20",
            trace_offset=1, trace_id=selected_id,
        )
        assert postgres_selected["samples"] == sqlite_selected["samples"]
        assert postgres_selected["selectedTrace"] == sqlite_selected["selectedTrace"]
        assert postgres_selected["selectedTrace"]["trace_id"] == selected_id
        assert selected_id not in {
            sample["trace_id"] for sample in postgres_selected["samples"]
        }
        json.dumps(postgres_bundle)
        assert type(postgres_bundle["providers"][0]["cost"]) is type(
            sqlite_bundle["providers"][0]["cost"]
        )
        assert type(postgres_bundle["providers"][0]["inTok"]) is type(
            sqlite_bundle["providers"][0]["inTok"]
        )
        assert postgres_bundle["meta"]["totalTraces"] == 5
        assert postgres_bundle["meta"]["totalAgentRuns"] == 1
        assert postgres_bundle["meta"]["agentRunSources"] == [
            {"sourceKind": "codex", "runs": 1}
        ]
        assert postgres_bundle["meta"]["agentRunSourcesTruncated"] is False
        assert postgres_bundle["meta"]["lastAgentCaptureAt"]
        assert postgres_bundle["meta"]["workload"] == "agent"
        assert postgres_bundle["meta"]["costBreakdown"]["judge"]["traces"] == 1
        assert postgres_bundle["truncation"]["resources"]["traceSamples"] == {
            "available": 4,
            "shown": 4,
            "limit": 30,
        }
        assert judge_trace.trace_id not in {
            sample["trace_id"] for sample in postgres_bundle["samples"]
        }
        assert "driftAnalysis" not in postgres_bundle
        assert postgres_bundle["providers"][0]["rawProvider"] == "custom-provider"
        assert "driftSignals" not in postgres_bundle
    finally:
        if sqlite is not None:
            sqlite.close()
        if postgres is not None:
            postgres.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def test_live_postgres_and_sqlite_registry_dashboard_shapes_match(tmp_path) -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_registry_dashboard_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    postgres = None
    sqlite = None
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        postgres = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        sqlite_path = tmp_path / "registry-dashboard-parity.db"
        sqlite = SQLiteStorage(str(sqlite_path))
        now = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
        tenant = "registry-dashboard-tenant"
        trace = Trace(
            trace_id="registry-dashboard-trace",
            tenant_id=tenant,
            session_id="registry-dashboard-session",
            started_at=now,
            ended_at=now,
            provider="anthropic",
            request_model="claude-haiku-4-5",
            response_model="claude-haiku-4-5",
            prompt_redacted="Safe representative billing prompt",
            tags={"verdict.workload": "agent", "verdict.intent_key": "billing"},
        )
        identity = ClusterIdentity(
            tenant_id=tenant,
            cluster_id="clu-registry-dashboard",
            kind="explicit",
            explicit_key="billing",
            display_name="Billing requests",
            created_by="admin@example.test",
            updated_by="admin@example.test",
        )
        version = ClusterRegistryVersion(
            tenant_id=tenant,
            version_id="crv-registry-dashboard",
            strategy="explicit",
            cutoff=now + timedelta(minutes=1),
            fit_definition_json=json.dumps(
                {
                    "algorithm": "ward-best-k-v2",
                    "selector": "latest-user-v1",
                    "model": {},
                    "config": {"target_workload": "agent"},
                }
            ),
            fit_definition_fingerprint="registry-dashboard-definition",
            preview_report_json=json.dumps(
                {
                    "candidate_count": 1,
                    "fit_assignment_count": 1,
                    "cluster_count": 1,
                    "explicit_cluster_count": 1,
                    "semantic_cluster_count": 0,
                    "chosen_k": None,
                    "statuses": {"assigned": 1, "outlier": 0, "ineligible": 0},
                    "metrics": {},
                    "warnings": [],
                    "candidate_summary": {},
                }
            ),
            created_by="admin@example.test",
        )
        cluster = ClusterRegistryCluster(
            tenant,
            version.version_id,
            identity.cluster_id,
            "explicit",
            member_count=1,
        )
        assignment = TraceClusterAssignment(
            tenant,
            version.version_id,
            trace.trace_id,
            "fit",
            "assigned",
            identity.cluster_id,
            "explicit",
        )
        judgment = Judgment(
            judgment_id="registry-dashboard-judgment",
            trace_id=trace.trace_id,
            created_at=now,
            evaluator_provider="anthropic",
            evaluator_config={"temperature": 0},
            evaluator_fingerprint="registry-dashboard-evaluator",
            expected_dimensions=["quality"],
            judge_models=["claude-haiku-4-5"],
            dimensions=[DimensionScore(name="quality", verdict=Verdict.FAIL)],
        )
        drift_run = DriftRun(
            run_id="registry-dashboard-drift-run",
            analysis_time=now + timedelta(hours=1),
            completed_at=now + timedelta(hours=1, seconds=1),
            evaluator_fingerprint=judgment.evaluator_fingerprint,
            signal_count=1,
        )
        drift_signal = DriftSignal(
            signal_id="registry-dashboard-drift-signal",
            run_id=drift_run.run_id,
            detected_at=drift_run.analysis_time,
            cluster_id=identity.cluster_id,
            dimension="quality",
            evaluator_fingerprint=judgment.evaluator_fingerprint,
        )
        validation = ClusterRegistryEvent(
            tenant_id=tenant,
            event_id="cre-registry-dashboard-validation",
            action="validated",
            to_version_id=version.version_id,
            actor="admin@example.test",
            details_json=json.dumps(
                {
                    "coverage": True,
                    "structural": True,
                    "definition": True,
                    "model": True,
                }
            ),
        )
        tenantless_trace = deepcopy(trace)
        tenantless_trace.trace_id = "registry-dashboard-tenantless-private"
        tenantless_trace.tenant_id = None
        tenantless_trace.session_id = "tenantless-private-session"
        tenantless_trace.prompt_redacted = "TENANTLESS PRIVATE PROMPT"
        foreign_trace = deepcopy(trace)
        foreign_trace.trace_id = "registry-dashboard-foreign-private"
        foreign_trace.tenant_id = "another-tenant"
        foreign_trace.session_id = "foreign-private-session"
        foreign_trace.prompt_redacted = "FOREIGN PRIVATE PROMPT"
        boundary_traces: list[Trace] = []
        for window, started_at, valid_count in (
            ("baseline", now - timedelta(days=2), 29),
            ("current", now, 28),
        ):
            for index in range(valid_count):
                item = deepcopy(trace)
                item.trace_id = f"registry-dashboard-{window}-valid-{index}"
                item.session_id = f"{window}-session-{index}"
                item.started_at = started_at
                item.ended_at = started_at
                boundary_traces.append(item)
            for index, session_id in enumerate(("", "a" * 257, "é" * 129)):
                item = deepcopy(trace)
                item.trace_id = f"registry-dashboard-{window}-invalid-{index}"
                item.session_id = session_id
                item.started_at = started_at
                item.ended_at = started_at
                boundary_traces.append(item)
        boundary_assignments = [
            TraceClusterAssignment(
                tenant,
                version.version_id,
                item.trace_id,
                "incremental",
                "assigned",
                identity.cluster_id,
                "explicit",
            )
            for item in (tenantless_trace, *boundary_traces)
        ]
        for storage in (sqlite, postgres):
            storage.insert_trace(deepcopy(trace))
            storage.insert_cluster_preview(
                deepcopy(version),
                [deepcopy(identity)],
                [deepcopy(cluster)],
                [deepcopy(assignment)],
            )
            storage.insert_cluster_registry_event(deepcopy(validation))
            storage.activate_cluster_registry(
                tenant,
                version.version_id,
                expected_generation=0,
                actor="admin@example.test",
                action="activated",
                expected_candidate_digest=cluster_candidate_digest([trace.trace_id]),
            )
            storage.insert_trace(deepcopy(tenantless_trace))
            storage.insert_trace(deepcopy(foreign_trace))
            for boundary_trace in boundary_traces:
                storage.insert_trace(deepcopy(boundary_trace))
            storage.insert_trace_cluster_assignments(
                tenant,
                deepcopy(boundary_assignments),
            )
            storage.insert_judgment(deepcopy(judgment))
            storage.replace_drift_run(
                deepcopy(drift_run),
                [deepcopy(drift_signal)],
            )

        sqlite.close()
        sqlite = None
        postgres.close()
        postgres = None
        sqlite_bundle = build_registry_bundle(f"sqlite:///{sqlite_path}", tenant=tenant)
        postgres_bundle = build_registry_bundle(scoped_dsn, tenant=tenant)
        sqlite_data = build_bundle(
            f"sqlite:///{sqlite_path}",
            registry_tenant=tenant,
        )
        postgres_data = build_bundle(scoped_dsn, registry_tenant=tenant)

        json.dumps(postgres_bundle)
        for bundle in (sqlite_bundle, postgres_bundle):
            assert bundle["status"] == "ready"
            assert bundle["tenant"] == tenant
            assert bundle["active"]["versionId"] == version.version_id
            assert bundle["selectedVersion"]["algorithm"] == "ward-best-k-v2"
            assert bundle["selectedVersion"]["selector"] == "latest-user-v1"
            assert bundle["counts"] == {
                "assigned": 65,
                "outlier": 0,
                "ineligible": 0,
                "total": 65,
            }
            assert bundle["readiness"]["passed"] is True
            assert bundle["clusters"][0]["displayName"] == "Billing requests"
            assert bundle["clusters"][0]["representatives"][0]["prompt"] == (
                "Safe representative billing prompt"
            )
            assert bundle["clusters"][0]["conversationReadiness"] == {
                "status": "collecting",
                "floor": 30,
                "baseline": 29,
                "current": 29,
                "remainingBaseline": 1,
                "remainingCurrent": 1,
                "estimatedDaysToReady": 1,
            }
            assert bundle["modelDistribution"] == [
                {"provider": "anthropic", "model": "claude-haiku-4-5", "count": 64}
            ]
            assert "TENANTLESS PRIVATE PROMPT" not in str(bundle)
            assert "tenantless-private-session" not in str(bundle)
        assert postgres_bundle["selectedVersion"] == sqlite_bundle["selectedVersion"]
        assert postgres_bundle["clusters"] == sqlite_bundle["clusters"]
        assert postgres_bundle["assignments"] == sqlite_bundle["assignments"]
        assert postgres_data == sqlite_data
        assert postgres_data["meta"]["totalTraces"] == 64
        assert "FOREIGN PRIVATE PROMPT" not in json.dumps(postgres_data)
        assert "TENANTLESS PRIVATE PROMPT" not in json.dumps(postgres_data)
        assert postgres_data["clusters"] == [
            {
                "cluster_id": identity.cluster_id,
                "display_name": identity.display_name,
                "n": 64,
            }
        ]
        assert "driftSignals" not in postgres_data
        assert "driftRun" not in postgres_data
    finally:
        if sqlite is not None:
            sqlite.close()
        if postgres is not None:
            postgres.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
