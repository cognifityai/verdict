"""Real PostgreSQL parity test for the packaged dashboard read path."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from psycopg.conninfo import conninfo_to_dict
from verdict.dashboard import build_bundle
from verdict.dashboard.app import build_registry_bundle, create_app
from verdict.dashboard.control_plane import ControlStore
from verdict.evidence import AgentRun, AgentRunBundle, ExecutionStatus, SourceSession
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

DSN = os.environ.get("VERDICT_TEST_POSTGRES_DSN")
try:
    TEST_DATABASE = conninfo_to_dict(DSN).get("dbname", "") if DSN else ""
except Exception:
    TEST_DATABASE = ""
if not (
    os.environ.get("VERDICT_TEST_POSTGRES_ALLOW_ANY_DB") == "1"
    or "test" in TEST_DATABASE.lower()
):
    DSN = None
POSTGRES_SKIP_REASON = "no explicitly disposable live PostgreSQL database"

pytestmark = pytest.mark.skipif(DSN is None, reason=POSTGRES_SKIP_REASON)


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

        assert postgres_bundle == sqlite_bundle
        assert postgres_page == sqlite_page
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
        assert postgres_bundle["driftAnalysis"] == {
            "runStatus": "completed_with_signals",
            "readinessStatus": "not_enough_current",
            "current": 2,
            "baseline": 0,
            "minimum": 30,
            "currentHours": 24,
            "baselineLagHours": 24,
            "baselineDays": 7,
        }
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
        assert postgres_data["clusters"] == [
            {
                "cluster_id": identity.cluster_id,
                "display_name": identity.display_name,
                "n": 64,
            }
        ]
        assert postgres_data["driftSignals"][0]["clusterLabel"] == identity.display_name
    finally:
        if sqlite is not None:
            sqlite.close()
        if postgres is not None:
            postgres.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
