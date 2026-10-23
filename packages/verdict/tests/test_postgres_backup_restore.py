"""Disposable PostgreSQL backup/restore drill for the customer-managed profile."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import httpx
import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.dashboard.app import create_app
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.issue_notices import IssueNotices
from verdict.dashboard.worker_status import WorkerCycleStatus
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    SourceSession,
)
from verdict.storage.postgres import PostgresStorage


def _pg_tool_env(dsn: str) -> dict[str, str]:
    from psycopg.conninfo import conninfo_to_dict

    details = conninfo_to_dict(dsn)
    env = os.environ.copy()
    for name in ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD",
                 "PGSSLMODE", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
        env.pop(name, None)
    for dsn_key, env_key in (
        ("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"),
        ("dbname", "PGDATABASE"), ("password", "PGPASSWORD"),
        ("sslmode", "PGSSLMODE"),
    ):
        if details.get(dsn_key):
            env[env_key] = str(details[dsn_key])
    return env


def test_disposable_postgres_dump_restores_agent_evidence_and_worker_state(tmp_path) -> None:
    from psycopg import connect, sql
    from psycopg.conninfo import conninfo_to_dict

    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        if os.environ.get("VERDICT_REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail(f"required disposable PostgreSQL DSN unavailable: {reason}")
        pytest.skip(reason)
    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        if os.environ.get("VERDICT_REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("required PostgreSQL backup tools unavailable")
        pytest.skip("PostgreSQL backup tools unavailable")
    with isolated_test_dsn(dsn) as isolated:
        schema = conninfo_to_dict(isolated)["options"].removeprefix("-csearch_path=")
        when = datetime.now(timezone.utc) + timedelta(minutes=1)
        source = SourceSession("source-backup", "customer-a", "verdict_sdk",
                               sha256(b"synthetic-backup").hexdigest(), when, when)
        run = AgentRun("run-backup", source.source_session_id, "customer-a", when,
                       ExecutionStatus.FAILED, ended_at=when)
        turn = AgentTurn("turn-backup", run.run_id, 0, when, ExecutionStatus.FAILED,
                         when, "synthetic request", "synthetic failure",
                         EvidenceState.PRESENT, EvidenceState.PRESENT)
        event = AgentEvent("event-backup", turn.turn_id, 0, when,
                           AgentEventType.TEST_RESULT, ExecutionStatus.FAILED,
                           "test:result", {"failed": 1})
        bundle = AgentRunBundle(source, run, (turn,), (event,))
        storage = PostgresStorage(isolated, min_pool=1, max_pool=1)
        try:
            storage.replace_agent_run_bundle(bundle)
        finally:
            storage.close()
        ControlStore(isolated).append(
            "customer-a", kind="schedule", document_id="daily", state="active",
            payload={"intervalHours": 24, "analyzeOnly": True}, expected_revision=None,
        )
        status = WorkerCycleStatus(isolated)
        status.begin("customer-a", trigger="worker", schedule_revision=1, now=when)
        status.finish("customer-a", state="completed", analysis_status="completed",
                      failure_code=None, now=when)
        notices = IssueNotices(isolated)
        assert notices.scan("customer-a") == 2
        notice = next(item for item in notices.list("customer-a")
                      if item["evidenceKind"] == "event")
        run_notice = next(item for item in notices.list("customer-a")
                          if item["evidenceKind"] == "run")
        assert notices.transition("customer-a", notice["noticeId"], "acknowledged")

        archive = tmp_path / "verdict-test.dump"
        tool_env = _pg_tool_env(dsn)
        subprocess.run(["pg_dump", "--format=custom", "--schema", schema,
                        "--file", str(archive)], check=True, env=tool_env,
                       capture_output=True, text=True, timeout=30)
        assert archive.stat().st_size > 0
        with connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        try:
            subprocess.run(["pg_restore", "--exit-on-error", "--single-transaction",
                            "--no-owner", "--no-privileges",
                            "--dbname", conninfo_to_dict(dsn)["dbname"], str(archive)],
                           check=True, env=tool_env, capture_output=True, text=True, timeout=30)
        finally:
            # Keep the disposable-schema context cleanup safe after a failed restore.
            with connect(dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}")
                              .format(sql.Identifier(schema)))

        restored = PostgresStorage(isolated, min_pool=1, max_pool=1)
        try:
            assert restored.get_agent_run_bundle("customer-a", run.run_id) == bundle
            assert restored.get_agent_run_bundle("other-tenant", run.run_id) is None
        finally:
            restored.close()
        [schedule] = [item for item in ControlStore(isolated).list_current("customer-a")
                      if item["kind"] == "schedule"]
        assert schedule["revision"] == 1 and schedule["state"] == "active"
        assert WorkerCycleStatus(isolated).read("customer-a")["state"] == "completed"
        [restored_notice] = IssueNotices(isolated).list("customer-a", state="reviewed")
        assert restored_notice["noticeId"] == notice["noticeId"]
        assert restored_notice["state"] == "acknowledged"
        [restored_run_notice] = IssueNotices(isolated).list("customer-a", state="open")
        assert restored_run_notice["noticeId"] == run_notice["noticeId"]
        assert restored_run_notice["eventId"] is None
        assert IssueNotices(isolated).list("other-tenant") == []

        async def read_restored_dashboard():
            own = create_app(storage=isolated, tenant_id="customer-a")
            foreign = create_app(storage=isolated, tenant_id="other-tenant")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=own), base_url="http://testserver",
            ) as client:
                detail = await client.get("/api/runs/run-backup",
                                          params={"event_id": "event-backup"})
                issues = await client.get("/api/control/issues", params={"state": "reviewed"})
                readiness = await client.get("/api/readiness")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=foreign), base_url="http://testserver",
            ) as client:
                foreign_detail = await client.get("/api/runs/run-backup")
                foreign_issues = await client.get("/api/control/issues",
                                                  params={"state": "reviewed"})
            return detail, issues, readiness, foreign_detail, foreign_issues

        detail, issues, readiness, foreign_detail, foreign_issues = asyncio.run(
            read_restored_dashboard()
        )
        assert detail.status_code == 200
        assert detail.json()["focusEventId"] == "event-backup"
        assert [item["turnId"] for item in detail.json()["turns"]] == ["turn-backup"]
        assert [item["eventId"] for item in detail.json()["events"]] == ["event-backup"]
        assert issues.status_code == 200
        assert [item["noticeId"] for item in issues.json()["issues"]] == [notice["noticeId"]]
        assert readiness.status_code == 200
        assert readiness.json()["openIssues"] == 1
        assert foreign_detail.status_code == 404
        assert foreign_issues.status_code == 200 and foreign_issues.json()["issues"] == []
