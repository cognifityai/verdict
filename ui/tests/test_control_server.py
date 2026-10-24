import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier, Event

import httpx
import pytest
import verdict.service as verdict_service
from verdict.analysis_records import AnalysisRunStatus, DeterministicAnalysisRun
from verdict.dashboard.app import create_app
from verdict.dashboard.control_plane import ControlStore
from verdict.delivery_queue import create_intent
from verdict.evidence import AgentRun, AgentRunBundle, ExecutionStatus, SourceSession
from verdict.monitoring import MonitorPolicy, monitor_policy_to_json
from verdict.schema import DriftSignal, Trace
from verdict.service import poll_cycle, worker_presence
from verdict.storage import SQLiteStorage


def test_delivery_rebind_requires_review_and_preserves_source_identity(tmp_path):
    url = f"sqlite:///{tmp_path / 'delivery-review.db'}"
    tenant = "__verdict_local__"
    first = ControlStore(url).append(
        tenant, kind="alert", document_id="default", state="active",
        payload={"destination": "webhook", "webhookUrlEnvVar": "VERDICT_REBIND_TEST",
                 "findings": True, "drift": False}, expected_revision=None,
    )
    when = datetime.now(timezone.utc)
    notification = {"kind": "finding", "code": "synthetic_failure", "severity": "error"}
    old_intent = create_intent(
        first, tenant_id=tenant, source_kind="analysis", source_id="synthetic",
        notification=notification, webhook_url="https://example.test/first",
        created_at=when,
    )
    run = DeterministicAnalysisRun(
        analysis_id=sha256(b"delivery-review").hexdigest(),
        tenant_id=tenant, scope_key="agent-and-trace", cutoff=when,
        completed_at=when, status=AnalysisRunStatus.COMPLETED,
        analyzer_version="test-v1", input_fingerprint=sha256(b"synthetic-input").hexdigest(),
        result={"findings": [notification]},
    )
    storage = SQLiteStorage(str(tmp_path / "delivery-review.db"))
    try:
        storage.save_worker_analysis_run(run, [old_intent])
    finally:
        storage.close()
    ControlStore(url).append(
        tenant, kind="alert", document_id="default", state="active",
        payload={"destination": "local_log", "findings": True, "drift": False},
        expected_revision=1,
    )
    app = create_app(storage=url)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            unauth = await client.get(
                f"/api/control/delivery/{old_intent.intent_id}/rebind-preview",
            )
            headers = {"X-Verdict-Setup": token}
            before = await client.get("/api/control", headers=headers)
            readiness = await client.get("/api/readiness")
            preview = await client.get(
                f"/api/control/delivery/{old_intent.intent_id}/rebind-preview",
                headers=headers,
            )
            info = preview.json()
            stale = await client.post(
                f"/api/control/delivery/{old_intent.intent_id}/rebind",
                headers=headers,
                json={"expectedOldStatus": "pending", "expectedAttemptCount": 0,
                      "expectedAlertRevision": 1,
                      "expectedNewFingerprint": info["newDestinationFingerprint"]},
            )
            confirmed = await client.post(
                f"/api/control/delivery/{old_intent.intent_id}/rebind",
                headers=headers,
                json={"expectedOldStatus": "pending", "expectedAttemptCount": 0,
                      "expectedAlertRevision": 2,
                      "expectedNewFingerprint": info["newDestinationFingerprint"]},
            )
            repeated = await client.post(
                f"/api/control/delivery/{old_intent.intent_id}/rebind",
                headers=headers,
                json={"expectedOldStatus": "pending", "expectedAttemptCount": 0,
                      "expectedAlertRevision": 2,
                      "expectedNewFingerprint": info["newDestinationFingerprint"]},
            )
            return unauth, before, readiness, preview, stale, confirmed, repeated

    unauth, before, readiness, preview, stale, confirmed, repeated = asyncio.run(scenario())
    assert unauth.status_code == 403
    assert before.json()["deliveryQueueCounts"] == {"pending": 1}
    assert readiness.json()["deliveryQueue"] == {"pending": 1}
    assert preview.status_code == 200
    assert preview.json()["notificationId"] == old_intent.notification_id
    assert preview.json()["currentAlertRevision"] == 2
    assert stale.status_code == 409
    assert confirmed.status_code == 200
    assert repeated.status_code == 409
    storage = SQLiteStorage(str(tmp_path / "delivery-review.db"))
    try:
        old = storage.get_delivery_intent(tenant, old_intent.intent_id)
        new = storage.get_delivery_intent(tenant, confirmed.json()["intentId"])
        assert storage.get_delivery_intent("foreign", old_intent.intent_id) is None
    finally:
        storage.close()
    assert old.status == "superseded" and old.superseded_by_id == new.intent.intent_id
    assert new.status == "pending" and new.intent.notification_id == old_intent.notification_id


def test_alert_disable_cannot_commit_between_queue_validation_and_send(
    tmp_path, monkeypatch,
):
    url = f"sqlite:///{tmp_path / 'alert-disable-race.db'}"
    tenant = "__verdict_local__"
    alert = ControlStore(url).append(
        tenant, kind="alert", document_id="default", state="active",
        payload={"destination": "webhook", "webhookUrlEnvVar": "VERDICT_RACE_ENDPOINT",
                 "findings": True, "drift": False}, expected_revision=None,
    )
    monkeypatch.setenv("VERDICT_RACE_ENDPOINT", "https://example.test/receiver")
    now = datetime.now(timezone.utc)
    finding = {"kind": "finding", "code": "synthetic"}
    intent = create_intent(
        alert, tenant_id=tenant, source_kind="analysis", source_id="synthetic",
        notification=finding, webhook_url="https://example.test/receiver", created_at=now,
    )
    run = DeterministicAnalysisRun(
        analysis_id=sha256(b"alert-disable-race").hexdigest(), tenant_id=tenant,
        scope_key="agent-and-trace", cutoff=now, completed_at=now,
        status=AnalysisRunStatus.COMPLETED, analyzer_version="test-v1",
        input_fingerprint=sha256(b"input").hexdigest(), result={"findings": [finding]},
    )
    storage = SQLiteStorage(str(tmp_path / "alert-disable-race.db"))
    try:
        storage.save_worker_analysis_run(run, [intent])
    finally:
        storage.close()
    validated, release = Event(), Event()
    original = verdict_service._intent_destination_matches

    def pause_after_validation(*args):
        answer = original(*args)
        validated.set()
        assert release.wait(5)
        return answer

    class Accepted:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    sent = []
    monkeypatch.setattr(verdict_service, "_intent_destination_matches", pause_after_validation)
    monkeypatch.setattr(verdict_service, "_open_webhook", lambda request, **_kw: (
        sent.append(request), Accepted(),
    )[1])

    async def disable():
        transport = httpx.ASGITransport(app=create_app(storage=url))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/control/alert/default", headers={"X-Verdict-Setup": token},
                json={"state": "disabled", "payload": alert["payload"],
                      "expectedRevision": 1},
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(poll_cycle, url, now=now + timedelta(seconds=1))
        assert validated.wait(5)
        try:
            response = asyncio.run(disable())
            assert response.status_code == 409
        finally:
            release.set()
        future.result(timeout=5)
    assert len(sent) == 1
    assert ControlStore(url).list_current(tenant)[0]["state"] == "active"


def test_actionable_delivery_queue_is_reachable_beyond_recent_successes(tmp_path):
    url = f"sqlite:///{tmp_path / 'delivery-pages.db'}"
    tenant = "__verdict_local__"
    when = datetime.now(timezone.utc) - timedelta(days=2)
    alert = ControlStore(url).append(
        tenant, kind="alert", document_id="default", state="active",
        payload={"destination": "local_log", "findings": True, "drift": False},
        expected_revision=None,
    )
    intents = [create_intent(
        alert, tenant_id=tenant, source_kind="analysis", source_id=f"item-{index}",
        notification={"kind": "finding", "code": f"synthetic-{index}"},
        webhook_url=None, created_at=when + timedelta(minutes=index),
    ) for index in range(203)]
    storage = SQLiteStorage(str(tmp_path / "delivery-pages.db"))
    try:
        storage.save_worker_analysis_run(DeterministicAnalysisRun(
            analysis_id=sha256(b"delivery-pages").hexdigest(), tenant_id=tenant,
            scope_key="agent-and-trace", cutoff=when, completed_at=when,
            status=AnalysisRunStatus.COMPLETED, analyzer_version="test-v1",
            input_fingerprint=sha256(b"input").hexdigest(), result={},
        ), intents)
        storage._conn.execute(
            "UPDATE notification_delivery_queue SET status='delivered',next_due_at=NULL "
            "WHERE source_id LIKE 'item-%' AND CAST(substr(source_id,6) AS INTEGER)>=102"
        )
        storage._conn.execute(
            "UPDATE notification_delivery_queue SET status='blocked',next_due_at=NULL "
            "WHERE source_id='item-0'"
        )
    finally:
        storage.close()

    async def scenario():
        transport = httpx.ASGITransport(app=create_app(storage=url))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.get("/api/control")
            anchor = first.json()["deliveryQueue"][-1]
            second = await client.get("/api/control/delivery", params={
                "afterCreatedAt": anchor["createdAt"],
                "afterIntentId": anchor["intentId"],
            })
            return first, second

    first, second = asyncio.run(scenario())
    assert first.status_code == second.status_code == 200
    assert first.json()["deliveryQueueCounts"]["blocked"] == 1
    assert intents[0].intent_id in {item["intentId"] for item in second.json()["deliveryQueue"]}
    first_ids = {item["intentId"] for item in first.json()["deliveryQueue"]}
    second_ids = {item["intentId"] for item in second.json()["deliveryQueue"]}
    assert len(first_ids) == 100 and len(second_ids) == 2
    assert not first_ids.intersection(second_ids)


def test_delivery_page_keeps_alerts_reachable_across_delivery_and_retry_transition(tmp_path):
    url = f"sqlite:///{tmp_path / 'delivery-changing.db'}"
    tenant = "__verdict_local__"
    when = datetime.now(timezone.utc) - timedelta(days=2)
    alert = ControlStore(url).append(
        tenant, kind="alert", document_id="default", state="active",
        payload={"destination": "local_log", "findings": True, "drift": False},
        expected_revision=None,
    )
    intents = [create_intent(
        alert, tenant_id=tenant, source_kind="analysis", source_id=f"item-{index}",
        notification={"kind": "finding", "code": f"synthetic-{index}"},
        webhook_url=None, created_at=when + timedelta(minutes=index),
    ) for index in range(102)]
    storage = SQLiteStorage(str(tmp_path / "delivery-changing.db"))
    try:
        storage.save_worker_analysis_run(DeterministicAnalysisRun(
            analysis_id=sha256(b"delivery-changing").hexdigest(), tenant_id=tenant,
            scope_key="agent-and-trace", cutoff=when, completed_at=when,
            status=AnalysisRunStatus.COMPLETED, analyzer_version="test-v1",
            input_fingerprint=sha256(b"input").hexdigest(), result={},
        ), intents)
    finally:
        storage.close()

    async def scenario():
        transport = httpx.ASGITransport(app=create_app(storage=url))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.get("/api/control")
            rows = first.json()["deliveryQueue"]
            storage = SQLiteStorage(str(tmp_path / "delivery-changing.db"))
            try:
                storage._conn.execute(
                    "UPDATE notification_delivery_queue SET status='delivered',next_due_at=NULL "
                    "WHERE intent_id=?", (rows[0]["intentId"],),
                )
                storage._conn.execute(
                    "UPDATE notification_delivery_queue SET status='failed' WHERE intent_id=?",
                    (intents[1].intent_id,),
                )
            finally:
                storage.close()
            anchor = rows[-1]
            second = await client.get("/api/control/delivery", params={
                "afterCreatedAt": anchor["createdAt"],
                "afterIntentId": anchor["intentId"],
            })
            return first, second

    first, second = asyncio.run(scenario())
    assert first.status_code == second.status_code == 200
    first_ids = {item["intentId"] for item in first.json()["deliveryQueue"]}
    second_ids = {item["intentId"] for item in second.json()["deliveryQueue"]}
    assert len(second_ids) == 2
    assert second_ids.isdisjoint(first_ids)
    assert second_ids == {item.intent_id for item in intents} - first_ids
    assert second.json()["hasMore"] is False


@pytest.mark.parametrize("params", [
    {"afterCreatedAt": "2026-10-18T00:00:00+00:00"},
    {"afterCreatedAt": "2026-10-18T00:00:00", "afterIntentId": "a" * 64},
    {"afterCreatedAt": "2026-10-18T00:00:00+00:00", "afterIntentId": "bad"},
])
def test_delivery_page_rejects_incomplete_or_invalid_cursor(tmp_path, params):
    async def scenario():
        transport = httpx.ASGITransport(app=create_app(
            storage=f"sqlite:///{tmp_path / 'invalid-cursor.db'}",
        ))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/control/delivery", params=params)

    response = asyncio.run(scenario())
    assert response.status_code == 400
    assert response.json() == {"error": "invalid delivery cursor"}


def test_readiness_reports_saved_schedule_without_claiming_a_running_worker(tmp_path):
    url = f"sqlite:///{tmp_path / 'readiness.db'}"
    ControlStore(url).append(
        "__verdict_local__", kind="schedule", document_id="daily", state="active",
        payload={"intervalHours": 24, "analyzeOnly": True}, expected_revision=None,
    )

    async def scenario():
        transport = httpx.ASGITransport(app=create_app(storage=url))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/readiness")

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.json()["documents"] == [{
        "kind": "schedule", "documentId": "daily", "state": "active", "revision": 1,
    }]
    assert response.json()["worker"]["status"] == "unverified"
    assert response.json()["lastCycle"]["state"] == "never_run"
    assert response.json()["nextDue"] is None

    with worker_presence(url):
        assert poll_cycle(url)["analysis"]["status"] == "completed"
        live_response = asyncio.run(scenario())
        assert live_response.json()["worker"]["status"] == "recent"
        assert live_response.json()["nextDue"]["scheduleRevision"] == 1
        assert datetime.fromisoformat(live_response.json()["nextDue"]["nextDueAt"]) > datetime.now(timezone.utc)


def test_readiness_requires_basic_auth_when_dashboard_is_protected(tmp_path, monkeypatch):
    import base64

    url = f"sqlite:///{tmp_path / 'protected-readiness.db'}"
    ControlStore(url).append(
        "__verdict_local__", kind="schedule", document_id="daily", state="active",
        payload={"intervalHours": 24, "analyzeOnly": True}, expected_revision=None,
    )
    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")
    app = create_app(storage=url)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            credentials = base64.b64encode(b"reviewer:secret").decode()
            return (
                await client.get("/api/readiness"),
                await client.get("/api/readiness", headers={"Authorization": "Basic invalid"}),
                await client.get("/api/readiness", headers={"Authorization": f"Basic {credentials}"}),
                await client.get("/api/health"),
            )

    missing, invalid, authenticated, health = asyncio.run(scenario())
    assert missing.status_code == invalid.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json()["documents"][0]["kind"] == "schedule"
    assert health.status_code == 200


def test_run_schedule_reports_non_posix_sqlite_lock_limit(tmp_path, monkeypatch):
    database = tmp_path / "non-posix.db"

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{database}"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            with monkeypatch.context() as patch:
                patch.setattr(verdict_service.os, "name", "nt")
                return await client.post(
                    "/api/control/actions/run-schedule", json={"analyzeOnly": True},
                    headers={"X-Verdict-Setup": token},
                )

    response = asyncio.run(request())
    assert response.status_code == 501
    assert "SQLite" in response.json()["error"]
    assert "POSIX" in response.json()["error"]


def test_manual_cycle_exposes_analysis_failure_as_unverified_result(tmp_path, monkeypatch):
    import verdict.dashboard.app as dashboard_app

    url = f"sqlite:///{tmp_path / 'analysis-failure.db'}"
    monkeypatch.setattr(dashboard_app, "build_agent_insights_bundle",
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("synthetic builder failure")))

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=url))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post("/api/control/actions/run-schedule",
                                     json={"analyzeOnly": True, "includeApprovedEvaluations": False},
                                     headers={"X-Verdict-Setup": token})

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["analysis"]["status"] == "error"
    assert asyncio.run(_read_cycle_status(url))["state"] == "failed"


def test_manual_cycle_persists_incomplete_local_capture_status(tmp_path):
    source = tmp_path / "codex"
    source.mkdir()
    (source / "broken.jsonl").write_bytes(b"{malformed}\n")
    url = f"sqlite:///{tmp_path / 'manual-capture.db'}"

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=url))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/control/actions/run-schedule",
                json={"codexRoot": str(source), "runMonitor": False,
                      "includeApprovedEvaluations": False},
                headers={"X-Verdict-Setup": token},
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["capture"]["skip_reasons"] == {"malformed_jsonl": 1}
    assert response.json()["cycleFailureCode"] == "capture_incomplete"
    assert asyncio.run(_read_cycle_status(url))["failureCode"] == "capture_incomplete"


async def _read_cycle_status(url):
    transport = httpx.ASGITransport(app=create_app(storage=url))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return (await client.get("/api/readiness")).json()["lastCycle"]


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


@pytest.mark.parametrize("kind", ["experiment", "scoped_evaluation"])
def test_result_documents_cannot_be_rolled_back_through_control_api(tmp_path, kind):
    url = f"sqlite:///{tmp_path / 'results.db'}"
    store = ControlStore(url)
    store.append("__verdict_local__", kind=kind, document_id="synthetic-result",
                 state="completed", payload={"status": "completed"},
                 expected_revision=None)
    before = store.history("__verdict_local__", kind, "synthetic-result")

    async def scenario():
        transport = httpx.ASGITransport(app=create_app(storage=url))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                f"/api/control/{kind}/synthetic-result/rollback",
                headers={"X-Verdict-Setup": token},
                json={"targetRevision": 1, "expectedRevision": 1},
            )

    response = asyncio.run(scenario())
    assert response.status_code == 400
    assert store.history("__verdict_local__", kind, "synthetic-result") == before


def test_two_concurrent_approvals_for_one_plan_leave_one_pending(tmp_path):
    url = f"sqlite:///{tmp_path / 'concurrent-approval.db'}"
    store = ControlStore(url)
    store.list_current("local")
    barrier = Barrier(2)
    payload = {"planFingerprint": "a" * 64, "config": {"scope": "run"}}

    def queue(index):
        barrier.wait()
        try:
            return store.append(
                "local", kind="scheduled_evaluation", document_id=f"approval-{index}",
                state="pending", payload=payload, expected_revision=None,
            )["state"]
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(queue, range(2)))
    assert sorted(outcomes) == ["pending", "rejected"]
    pending = [item for item in store.list_current("local")
               if item["kind"] == "scheduled_evaluation" and item["state"] == "pending"]
    assert len(pending) == 1


def test_cancel_and_spend_race_has_one_terminal_winner(tmp_path):
    url = f"sqlite:///{tmp_path / 'approval-race.db'}"
    store = ControlStore(url)
    payload = {"planFingerprint": "a" * 64, "config": {"scope": "run"}}
    pending = store.append(
        "local", kind="scheduled_evaluation", document_id="approval-race",
        state="pending", payload=payload, expected_revision=None,
    )
    barrier = Barrier(2)

    def transition(state):
        barrier.wait()
        try:
            store.append(
                "local", kind="scheduled_evaluation", document_id="approval-race",
                state=state, payload=payload, expected_revision=pending["revision"],
            )
            return state
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(transition, ("cancelled", "spent")))
    assert "rejected" in outcomes
    assert len(store.history("local", "scheduled_evaluation", "approval-race")) == 2


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


def test_scheduled_analysis_can_run_on_existing_instrumented_agent_activity(tmp_path):
    database = tmp_path / "analysis-only.db"
    now = datetime.now(timezone.utc)
    storage = SQLiteStorage(str(database))
    storage.replace_agent_run_bundle(AgentRunBundle(
        SourceSession("sdk-session", "__verdict_local__", "verdict_sdk", "a" * 64, now, now),
        AgentRun("sdk-run", "sdk-session", "__verdict_local__", now,
                 ExecutionStatus.COMPLETED, now),
    ))
    storage.close()

    async def scenario():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{database}"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            response = await client.post(
                "/api/control/actions/run-schedule", headers={"X-Verdict-Setup": token},
                json={"analyzeOnly": True, "runMonitor": False},
            )
            return response, await client.get("/api/insights")

    response, insights = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.json()["analysis"]["status"] == "completed"
    assert insights.json()["analysisState"]["status"] == "completed"


def test_default_schedule_with_blank_roots_saves_and_runs(tmp_path):
    database = tmp_path / "blank-roots.db"
    payload = {
        "intervalHours": 24, "claudeRoot": "  ", "codexRoot": "",
        "analyzeOnly": True, "runMonitor": False,
    }

    async def scenario():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            saved = await client.post(
                "/api/control/schedule/daily", headers=headers,
                json={"state": "active", "payload": payload, "expectedRevision": None},
            )
            run = await client.post(
                "/api/control/actions/run-schedule", headers=headers, json=payload,
            )
            return saved, run

    saved, run = asyncio.run(scenario())
    assert saved.status_code == 200
    assert run.status_code == 200
    assert run.json()["analysis"]["status"] == "completed"


def test_control_documents_use_the_configured_dashboard_tenant(tmp_path):
    database = tmp_path / "configured-control.db"

    async def scenario():
        app = create_app(storage=f"sqlite:///{database}", tenant_id="customer-a")
        transport = httpx.ASGITransport(app=app)
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
            return saved, await client.get("/api/control")

    saved, state = asyncio.run(scenario())

    assert saved.status_code == 200
    assert len(state.json()["documents"]) == 1
    store = ControlStore(f"sqlite:///{database}")
    assert len(store.list_current("customer-a")) == 1
    assert store.list_current("__verdict_local__") == []


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
    storage.insert_drift_signal(DriftSignal(signal_id="legacy-drift-history"))
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
    assert telemetry["notifications"] == []

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


def test_run_schedule_action_advances_active_monitor_once(tmp_path):
    database = tmp_path / "verdict.db"
    source = tmp_path / "claude"
    source.mkdir()
    storage = SQLiteStorage(str(database))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(4):
        observed = now + timedelta(minutes=index)
        storage.insert_trace(Trace(
            trace_id=f"historical-{index}", tenant_id="__verdict_local__",
            started_at=observed, ended_at=observed, response_redacted="ok",
        ))
    storage.close()

    async def scenario():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{database}")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = await client.post(
                "/api/monitor/preview", headers=headers,
                json={
                    "windowMode": "count", "referenceRatio": 0.5,
                    "minimumReference": 1, "minimumCurrent": 1,
                    "prospectiveTarget": 1,
                },
            )
            policy_id = preview.json()["policy"]["policy_id"]
            activation = await client.post(
                "/api/monitor/activate", headers=headers,
                json={"policyId": policy_id, "expectedActivePolicyId": None},
            )
            prospective_start = datetime.fromisoformat(
                activation.json()["snapshot"]["manifest"]["prospective_start_at"]
            )
            storage = SQLiteStorage(str(database))
            observed = prospective_start + timedelta(microseconds=1)
            storage.insert_trace(Trace(
                trace_id="new-trace", tenant_id="__verdict_local__",
                started_at=observed, ended_at=observed, response_redacted="done",
            ))
            storage.close()
            run = await client.post(
                "/api/control/actions/run-schedule", headers=headers,
                json={"claudeRoot": str(source), "runMonitor": True},
            )
            return preview, activation, run, policy_id

    preview, activation, run, policy_id = asyncio.run(scenario())
    assert preview.status_code == activation.status_code == run.status_code == 200
    assert run.json()["capture"]["stored"] == 0
    assert run.json()["monitor"]["snapshot"]["manifest"]["comparison_index"] == 1

    storage = SQLiteStorage(str(database))
    try:
        snapshot_count = storage._conn.execute(
            "SELECT COUNT(*) FROM monitor_snapshots WHERE policy_id=?", (policy_id,)
        ).fetchone()[0]
    finally:
        storage.close()
    assert snapshot_count == 3


def test_run_schedule_preflights_monitor_rebootstrap_before_capture(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "published-session-without-snapshot.db"
    source = tmp_path / "claude"
    source.mkdir()
    SQLiteStorage(str(database)).close()
    policy = MonitorPolicy(
        "published-session-without-snapshot",
        "__verdict_local__:application:trace",
        analysis_unit="session",
    )
    payload = monitor_policy_to_json(policy)
    connection = sqlite3.connect(database)
    try:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        connection.execute(
            "INSERT INTO monitor_policies "
            "(policy_id,scope_key,state,content_hash,payload_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                policy.policy_id,
                policy.scope_key,
                "active",
                sha256(payload.encode()).hexdigest(),
                payload,
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    def unexpected_capture(*args, **kwargs):
        raise AssertionError("capture must not run before monitor preflight")

    monkeypatch.setattr(
        "verdict.dashboard.control_routes.capture_local_agents",
        unexpected_capture,
    )

    async def scenario():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{database}")
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/control/actions/run-schedule",
                headers={"X-Verdict-Setup": token},
                json={"claudeRoot": str(source), "runMonitor": True},
            )

    response = asyncio.run(scenario())

    assert response.status_code == 409
    assert response.json() == {"error": "monitor requires re-bootstrap"}
