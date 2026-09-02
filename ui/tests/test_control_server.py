import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from verdict.dashboard.app import create_app
from verdict.dashboard.control_plane import ControlStore
from verdict.evidence import AgentRun, AgentRunBundle, ExecutionStatus, SourceSession
from verdict.schema import Trace
from verdict.storage import SQLiteStorage


def test_control_documents_are_versioned_conflict_checked_and_rollbackable(tmp_path):
    store = ControlStore(f"sqlite:///{tmp_path / 'verdict.db'}")
    first = store.append(
        "tenant", kind="settings", document_id="default", state="active",
        payload={"captureContent": True, "retentionDays": 30},
        expected_revision=None,
    )
    second = store.append(
        "tenant", kind="settings", document_id="default", state="active",
        payload={"captureContent": True, "retentionDays": 60},
        expected_revision=1,
    )
    with pytest.raises(ValueError, match="revision conflict"):
        store.append(
            "tenant", kind="settings", document_id="default", state="active",
            payload={"captureContent": True}, expected_revision=1,
        )
    rolled_back = store.rollback(
        "tenant", kind="settings", document_id="default",
        target_revision=first["revision"], expected_revision=second["revision"],
    )
    assert rolled_back["revision"] == 3
    assert rolled_back["payload"]["retentionDays"] == 30
    assert len(store.history("tenant", "settings", "default")) == 3


def test_control_documents_reject_secret_values_and_invalid_schedules(tmp_path):
    store = ControlStore(f"sqlite:///{tmp_path / 'verdict.db'}")
    with pytest.raises(ValueError, match="environment-variable reference"):
        store.append(
            "tenant", kind="settings", document_id="default", state="active",
            payload={"apiKey": "secret-value"}, expected_revision=None,
        )
    with pytest.raises(ValueError, match="1-168"):
        store.append(
            "tenant", kind="schedule", document_id="daily", state="active",
            payload={"intervalHours": 0}, expected_revision=None,
        )


def test_control_api_requires_capability_and_exposes_no_secret_values(tmp_path):
    database = tmp_path / "verdict.db"

    async def scenario():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{database}"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            denied = await client.post(
                "/api/control/settings/default",
                json={"state": "active", "payload": {"captureContent": True},
                      "expectedRevision": None},
            )
            saved = await client.post(
                "/api/control/settings/default",
                headers={"X-Verdict-Setup": token},
                json={"state": "active", "payload": {
                    "captureContent": True,
                    "providerKeyEnvVars": ["ANTHROPIC_API_KEY"],
                }, "expectedRevision": None},
            )
            snapshot = await client.get("/api/control")
            return denied, saved, snapshot

    denied, saved, snapshot = asyncio.run(scenario())
    assert denied.status_code == 403
    assert saved.status_code == 200
    assert snapshot.status_code == 200
    assert snapshot.json()["documents"][0]["payload"] == {
        "captureContent": True, "providerKeyEnvVars": ["ANTHROPIC_API_KEY"],
    }
    assert "secret-value" not in snapshot.text


def test_cluster_actions_are_capability_gated_and_reject_unapproved_semantic_model(tmp_path):
    async def scenario():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            denied = await client.post("/api/clusters/fit", json={"strategy": "explicit"})
            invalid = await client.post(
                "/api/clusters/fit", headers={"X-Verdict-Setup": token},
                json={"strategy": "semantic"},
            )
            return denied, invalid

    denied, invalid = asyncio.run(scenario())
    assert denied.status_code == 403
    assert invalid.status_code == 400
    assert "path" not in invalid.text.lower()


def test_control_api_reports_source_appropriate_daily_operations(tmp_path):
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    storage.insert_trace(Trace(
        trace_id="telemetry-trace", tenant_id="__verdict_local__", started_at=now,
        provider="openai", request_model="model", response_redacted="response",
    ))
    storage.close()

    async def get_state():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{database}")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return (await client.get("/api/control")).json()

    telemetry = asyncio.run(get_state())
    assert telemetry["dailyOperations"] == {
        "mode": "telemetry",
        "localAgentSources": [],
    }

    storage = SQLiteStorage(str(database))
    storage.replace_agent_run_bundle(AgentRunBundle(
        SourceSession("session", "__verdict_local__", "codex", "a" * 64, now, now),
        AgentRun("run", "session", "__verdict_local__", now, ExecutionStatus.UNKNOWN),
    ))
    storage.close()

    local = asyncio.run(get_state())
    assert local["dailyOperations"] == {
        "mode": "local_agent",
        "localAgentSources": ["codex"],
    }

    storage = SQLiteStorage(str(database))
    for index in range(101):
        observed = now + timedelta(seconds=index + 1)
        storage.replace_agent_run_bundle(AgentRunBundle(
            SourceSession(
                f"other-session-{index}", "__verdict_local__", "unknown-agent",
                f"{index + 1:064x}", observed, observed,
            ),
            AgentRun(
                f"other-run-{index}", f"other-session-{index}",
                "__verdict_local__", observed, ExecutionStatus.UNKNOWN,
            ),
        ))
    storage.close()

    mixed = asyncio.run(get_state())
    assert mixed["dailyOperations"] == {
        "mode": "local_agent",
        "localAgentSources": ["codex"],
    }
