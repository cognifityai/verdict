"""Durable worker notification obligations across the two supported stores."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.analysis_records import (
    AnalysisRunStatus,
    DeliveryOutcome,
    DeterministicAnalysisRun,
    NotificationDeliveryAttempt,
)
from verdict.dashboard.control_plane import ControlStore
from verdict.delivery_queue import create_intent, webhook_fingerprint
from verdict.monitor_inputs import advance_monitor
from verdict.monitoring import (
    AnalysisUnitRecord,
    MonitorPolicy,
    compare_manifest,
    plan_historical_manifest,
    plan_prospective_manifest,
)
from verdict.service import poll_cycle
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        storage = SQLiteStorage(str(tmp_path / "delivery.db"))
        try:
            yield storage
        finally:
            storage.close()
        return
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        if os.environ.get("VERDICT_REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail(f"required disposable PostgreSQL DSN unavailable: {reason}")
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            yield storage
        finally:
            storage.close()


def _run(when: datetime) -> DeterministicAnalysisRun:
    return DeterministicAnalysisRun(
        analysis_id=hashlib.sha256(b"synthetic-analysis").hexdigest(),
        tenant_id="tenant-a", scope_key="agent-and-trace",
        cutoff=when, completed_at=when, status=AnalysisRunStatus.COMPLETED,
        analyzer_version="test-v1", input_fingerprint=hashlib.sha256(b"input").hexdigest(),
        result={"findings": [{"code": "synthetic_failure", "severity": "error"}]},
    )


def _intent(when: datetime, source_id: str = "failure"):
    intent = create_intent(
        {"state": "active", "revision": 1, "payload": {
            "destination": "webhook", "webhookUrlEnvVar": "VERDICT_TEST_ENDPOINT",
            "findings": True, "drift": False,
        }},
        tenant_id="tenant-a", source_kind="analysis", source_id=source_id,
        notification={"kind": "finding", "code": source_id, "severity": "error"},
        webhook_url="https://example.test/receiver", created_at=when,
    )
    assert intent is not None
    return intent


def test_notification_identity_matches_published_beta_serialization() -> None:
    import json

    notification = {"kind": "finding", "code": "synthetic", "message": "café"}
    intent = create_intent(
        {"state": "active", "revision": 1, "payload": {
            "destination": "local_log", "findings": True, "drift": False,
        }},
        tenant_id="__verdict_local__", source_kind="analysis", source_id="prior",
        notification=notification, webhook_url=None,
    )
    assert intent is not None
    legacy_id = hashlib.sha256(json.dumps({
        "sourceKind": "analysis", "sourceId": "prior", "notification": notification,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert intent.notification_id == legacy_id


@pytest.mark.parametrize("url", [
    "https://exa mple.com/path", "https://example.com/has space",
    "https://example.com\\other", "https://example.com/\nnext",
])
def test_invalid_https_urls_remain_unbound(url: str) -> None:
    assert webhook_fingerprint(url) is None


def test_worker_source_and_intent_commit_together_and_are_tenant_scoped(store) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run, intent = _run(when), _intent(when)
    store.save_worker_analysis_run(run, [intent])
    assert store.get_deterministic_analysis_run(run.analysis_id) is not None
    item = store.get_delivery_intent("tenant-a", intent.intent_id)
    assert item is not None and item.status == "pending"
    assert store.get_delivery_intent("tenant-b", intent.intent_id) is None
    assert store.list_due_delivery_intents("tenant-b", now=when + timedelta(days=1)) == []
    assert [row.intent.intent_id for row in store.list_due_delivery_intents(
        "tenant-a", now=when,
    )] == [intent.intent_id]
    # Re-verifying the same analysis must not replace its immutable obligation.
    store.verify_worker_analysis_run(run.analysis_id, revision=1, intents=[_intent(
        when + timedelta(days=1),
    )])
    assert store.get_delivery_intent("tenant-a", intent.intent_id).intent.created_at == when


def test_worker_source_rolls_back_when_intent_insert_fails(store, monkeypatch) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run, intent = _run(when), _intent(when)

    def fail(*_args):
        raise RuntimeError("injected intent insert failure")

    monkeypatch.setattr(store, "_insert_delivery_intent", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.save_worker_analysis_run(run, [intent])
    assert store.get_deterministic_analysis_run(run.analysis_id) is None
    assert store.get_delivery_intent("tenant-a", intent.intent_id) is None


def test_unbound_probe_and_due_page_do_not_starve_each_other(store) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run = _run(when)
    unbound = []
    for index in range(105):
        item = create_intent(
            {"state": "active", "revision": 1, "payload": {
                "destination": "webhook", "webhookUrlEnvVar": "VERDICT_TEST_ENDPOINT",
                "findings": True, "drift": False,
            }},
            tenant_id="tenant-a", source_kind="analysis", source_id=f"unbound-{index}",
            notification={"kind": "finding", "code": f"unbound-{index}"},
            webhook_url=None, created_at=when,
        )
        assert item is not None
        unbound.append(item)
    due = _intent(when, "deliverable")
    store.save_worker_analysis_run(run, [*unbound, due])
    page = store.list_delivery_intents_for_tenant(
        "tenant-a", limit=100, actionable_only=True,
    )
    assert len(page) == 100
    anchor = page[-1]
    assert len(store.list_delivery_intents_for_tenant(
        "tenant-a", limit=100,
        after=(anchor.intent.created_at, anchor.intent.intent_id),
        actionable_only=True,
    )) == 6
    assert [item.intent.intent_id for item in store.list_due_delivery_intents(
        "tenant-a", now=when, limit=1,
    )] == [due.intent_id]
    first = store.list_unbound_delivery_intents("tenant-a", now=when, limit=100)
    assert len(first) == 100
    for item in first:
        store.probe_delivery_intent(
            "tenant-a", item.intent.intent_id, now=when,
            fingerprint=None, next_probe_at=when + timedelta(minutes=1),
        )
    assert len(store.list_unbound_delivery_intents("tenant-a", now=when, limit=100)) == 5


def test_delivery_cursor_does_not_skip_after_earlier_row_is_delivered(store) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intents = [_intent(when + timedelta(minutes=index), f"page-{index}")
               for index in range(102)]
    store.save_worker_analysis_run(_run(when), intents)
    first = store.list_delivery_intents_for_tenant(
        "tenant-a", limit=100, actionable_only=True,
    )
    anchor = first[-1]
    delivered = first[0].intent
    assert store.finish_delivery_intent_attempt(
        "tenant-a", delivered.intent_id, NotificationDeliveryAttempt(
            attempt_id=hashlib.sha256(b"page-delivery").hexdigest(),
            notification_id=delivered.notification_id, tenant_id="tenant-a",
            source_kind="analysis", source_id=delivered.source_id,
            destination_fingerprint=delivered.destination_fingerprint,
            attempted_at=when + timedelta(days=1), outcome=DeliveryOutcome.DELIVERED,
            payload=delivered.notification, http_status=200,
        ), expected_count=0, next_due_at=None,
    )
    second = store.list_delivery_intents_for_tenant(
        "tenant-a", limit=100,
        after=(anchor.intent.created_at, anchor.intent.intent_id),
        actionable_only=True,
    )
    assert len(second) == 2
    assert {row.intent.intent_id for row in second} == (
        {intent.intent_id for intent in intents}
        - {row.intent.intent_id for row in first}
    )


def test_delivery_cursor_does_not_skip_unseen_row_when_retry_fails(store) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intents = [_intent(when + timedelta(minutes=index), f"retry-page-{index}")
               for index in range(102)]
    store.save_worker_analysis_run(_run(when), intents)
    first = store.list_delivery_intents_for_tenant(
        "tenant-a", limit=100, actionable_only=True,
    )
    first_ids = {row.intent.intent_id for row in first}
    unseen = next(intent for intent in intents if intent.intent_id not in first_ids)
    assert store.finish_delivery_intent_attempt(
        "tenant-a", unseen.intent_id, NotificationDeliveryAttempt(
            attempt_id=hashlib.sha256(b"retry-page-failure").hexdigest(),
            notification_id=unseen.notification_id, tenant_id="tenant-a",
            source_kind="analysis", source_id=unseen.source_id,
            destination_fingerprint=unseen.destination_fingerprint,
            attempted_at=when + timedelta(days=1), outcome=DeliveryOutcome.FAILED,
            payload=unseen.notification, error_code="http_rejected", http_status=503,
        ), expected_count=0, next_due_at=when + timedelta(days=1, minutes=1),
    )
    anchor = first[-1].intent
    second = store.list_delivery_intents_for_tenant(
        "tenant-a", limit=100,
        after=(anchor.created_at, anchor.intent_id), actionable_only=True,
    )
    assert {row.intent.intent_id for row in second} == (
        {intent.intent_id for intent in intents} - first_ids
    )


def test_published_delivery_attempt_closes_matching_new_queue_without_resending(store) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _intent(when)
    store.save_notification_delivery_attempt(NotificationDeliveryAttempt(
        attempt_id=hashlib.sha256(b"published-attempt").hexdigest(),
        notification_id=intent.notification_id, tenant_id="tenant-a",
        source_kind="analysis", source_id=intent.source_id,
        destination_fingerprint=intent.destination_fingerprint,
        attempted_at=when, outcome=DeliveryOutcome.DELIVERED,
        payload=intent.notification, http_status=200,
    ))
    store.save_worker_analysis_run(_run(when), [intent])
    assert store.mark_delivery_intent_previously_delivered(
        "tenant-a", intent.intent_id, expected_count=0,
    )
    item = store.get_delivery_intent("tenant-a", intent.intent_id)
    assert item.status == "delivered" and item.attempt_count == 0
    assert len(store.list_notification_delivery_attempts_for_tenant("tenant-a")) == 1


def test_attempt_and_intent_state_commit_together(store) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _intent(when)
    store.save_worker_analysis_run(_run(when), [intent])
    attempt = NotificationDeliveryAttempt(
        attempt_id=hashlib.sha256(b"attempt").hexdigest(),
        notification_id=intent.notification_id, tenant_id="tenant-a",
        source_kind="analysis", source_id=intent.source_id,
        destination_fingerprint=intent.destination_fingerprint,
        attempted_at=when, outcome=DeliveryOutcome.FAILED,
        payload=intent.notification, error_code="http_rejected", http_status=503,
    )
    assert store.finish_delivery_intent_attempt(
        "tenant-a", intent.intent_id, attempt, expected_count=0,
        next_due_at=when + timedelta(seconds=30),
    )
    item = store.get_delivery_intent("tenant-a", intent.intent_id)
    assert item.status == "failed" and item.attempt_count == 1
    assert item.next_due_at == when + timedelta(seconds=30)
    assert not store.finish_delivery_intent_attempt(
        "tenant-a", intent.intent_id, attempt, expected_count=0,
        next_due_at=when + timedelta(seconds=30),
    )
    assert len(store.list_notification_delivery_attempts_for_tenant("tenant-a")) == 1


def test_rebind_is_atomic_and_requires_current_alert_revision(store, tmp_path) -> None:
    url = store._dsn if isinstance(store, PostgresStorage) else str(tmp_path / "delivery.db")
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = ControlStore(url).append(
        "tenant-a", kind="alert", document_id="default", state="active",
        payload={"destination": "webhook", "webhookUrlEnvVar": "VERDICT_TEST_ENDPOINT",
                 "findings": True, "drift": False}, expected_revision=None,
    )
    old = create_intent(
        first, tenant_id="tenant-a", source_kind="analysis", source_id="failure",
        notification={"kind": "finding", "code": "failure", "severity": "error"},
        webhook_url="https://example.test/first", created_at=when,
    )
    store.save_worker_analysis_run(_run(when), [old])
    current = ControlStore(url).append(
        "tenant-a", kind="alert", document_id="default", state="active",
        payload={"destination": "local_log", "findings": True, "drift": False},
        expected_revision=1,
    )
    replacement = create_intent(
        current, tenant_id="tenant-a", source_kind="analysis", source_id="failure",
        notification=old.notification, webhook_url=None, created_at=when,
        rebind_from_id=old.intent_id,
    )
    assert replacement is not None
    assert not store.rebind_delivery_intent(
        "tenant-a", old.intent_id, expected_status="failed", expected_count=0,
        new_intent=replacement,
    )
    assert store.rebind_delivery_intent(
        "tenant-a", old.intent_id, expected_status="pending", expected_count=0,
        new_intent=replacement,
    )
    assert store.get_delivery_intent("tenant-a", old.intent_id).status == "superseded"
    assert store.get_delivery_intent("tenant-a", replacement.intent_id).status == "pending"
    assert store.delivery_queue_counts("tenant-a") == {"pending": 1, "superseded": 1}


def test_monitor_successor_and_intent_commit_together(store, monkeypatch) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    policy = MonitorPolicy(
        "synthetic-policy", "tenant-a:application:trace", reference_ratio=0.5,
        minimum_reference=5, minimum_current=5, prospective_target=10,
        minimum_effect=0.5,
    )
    reference = [AnalysisUnitRecord(
        f"reference-{index}", when + timedelta(minutes=index), {"failed": False},
    ) for index in range(20)]
    historical = plan_historical_manifest(reference, policy, cutoff=when + timedelta(hours=1))
    store.save_monitor_policy(policy)
    store.save_monitor_snapshot(
        policy.policy_id, historical, compare_manifest(reference, historical, policy),
    )
    store.activate_monitor_policy(
        policy.scope_key, policy.policy_id, expected_active_policy_id=None,
    )
    collecting = plan_prospective_manifest(historical, reference, policy)
    store.save_monitor_snapshot(
        policy.policy_id, collecting, compare_manifest(reference, collecting, policy),
    )
    current = [AnalysisUnitRecord(
        f"current-{index}", when + timedelta(hours=2, minutes=index), {"failed": True},
    ) for index in range(10)]
    successor = plan_prospective_manifest(collecting, reference + current, policy)
    comparison = compare_manifest(reference + current, successor, policy)
    assert comparison.status.value == "alert"
    intent = create_intent(
        {"state": "active", "revision": 1, "payload": {
            "destination": "local_log", "drift": True, "findings": False,
        }}, tenant_id="tenant-a", source_kind="monitor",
        source_id=successor.snapshot_id,
        notification={"kind": "drift", "status": "alert", "policyId": policy.policy_id},
        webhook_url=None, created_at=when,
    )
    assert intent is not None
    original = store._insert_delivery_intent

    def fail(*_args):
        raise RuntimeError("injected intent insert failure")

    monkeypatch.setattr(store, "_insert_delivery_intent", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.save_worker_monitor_successor(
            policy.policy_id, collecting.snapshot_id, successor, comparison,
            expected_state="active", delivery_intent=intent,
        )
    assert store.get_latest_monitor_snapshot(policy.policy_id)[0].snapshot_id == collecting.snapshot_id
    assert store.get_delivery_intent("tenant-a", intent.intent_id) is None
    monkeypatch.setattr(store, "_insert_delivery_intent", original)
    store.save_worker_monitor_successor(
        policy.policy_id, collecting.snapshot_id, successor, comparison,
        expected_state="active", delivery_intent=intent,
    )
    assert store.get_latest_monitor_snapshot(policy.policy_id)[0].snapshot_id == successor.snapshot_id
    assert store.get_delivery_intent("tenant-a", intent.intent_id).status == "pending"


def test_monitor_cas_loser_attaches_winning_alert_and_worker_delivers(
    store, tmp_path, monkeypatch,
) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    policy = MonitorPolicy(
        "cas-loser-policy", "tenant-a:application:trace", reference_ratio=0.5,
        minimum_reference=5, minimum_current=5, prospective_target=10,
        minimum_effect=0.5,
    )
    reference = [AnalysisUnitRecord(
        f"reference-{index}", when + timedelta(minutes=index), {"failed": False},
    ) for index in range(20)]
    historical = plan_historical_manifest(reference, policy, cutoff=when + timedelta(hours=1))
    store.save_monitor_policy(policy)
    store.save_monitor_snapshot(
        policy.policy_id, historical, compare_manifest(reference, historical, policy),
    )
    store.activate_monitor_policy(
        policy.scope_key, policy.policy_id, expected_active_policy_id=None,
    )
    collecting = plan_prospective_manifest(historical, reference, policy)
    store.save_monitor_snapshot(
        policy.policy_id, collecting, compare_manifest(reference, collecting, policy),
    )
    current = [AnalysisUnitRecord(
        f"current-{index}", when + timedelta(hours=2, minutes=index), {"failed": True},
    ) for index in range(10)]
    monkeypatch.setattr(
        "verdict.monitor_inputs.load_monitor_units", lambda *_args, **_kwargs: reference + current,
    )
    alert = {"state": "active", "revision": 1, "payload": {
        "destination": "local_log", "findings": False, "drift": True,
    }}
    if isinstance(store, PostgresStorage):
        from urllib.parse import urlencode

        from psycopg.conninfo import conninfo_to_dict

        connection = conninfo_to_dict(store._dsn)
        storage_url = f"postgresql:///?{urlencode(connection)}"
    else:
        storage_url = f"sqlite:///{tmp_path / 'delivery.db'}"
    ControlStore(storage_url).append(
        "tenant-a", kind="alert", document_id="default", state="active",
        payload=alert["payload"], expected_revision=None,
    )

    def make_intent(manifest, comparison):
        assert comparison.status.value == "alert"
        return create_intent(
            alert, tenant_id="tenant-a", source_kind="monitor",
            source_id=manifest.snapshot_id,
            notification={"kind": "drift", "status": "alert", "policyId": policy.policy_id},
            webhook_url=None, created_at=when,
        )

    original = store.save_worker_monitor_successor
    raced = []

    def dashboard_wins(policy_id, previous_id, manifest, comparison, *, expected_state,
                       delivery_intent):
        store.save_monitor_successor(
            policy_id, previous_id, manifest, comparison,
            expected_state=expected_state,
        )
        raced.append(manifest.snapshot_id)
        return original(
            policy_id, previous_id, manifest, comparison,
            expected_state=expected_state, delivery_intent=delivery_intent,
        )

    monkeypatch.setattr(store, "save_worker_monitor_successor", dashboard_wins)
    winner, comparison = advance_monitor(
        store, policy, tenant_id="tenant-a", delivery_intent_factory=make_intent,
    )
    assert raced == [winner.snapshot_id]
    assert comparison.status.value == "alert"
    intent = make_intent(winner, comparison)
    assert store.get_latest_monitor_snapshot(policy.policy_id)[0].snapshot_id == winner.snapshot_id
    assert store.get_delivery_intent("tenant-a", intent.intent_id).status == "pending"
    assert poll_cycle(storage_url, tenant_id="tenant-a", now=datetime.now(timezone.utc))[
        "notifications"
    ] == [{"status": "delivered", "notificationId": intent.notification_id,
           "destinationFingerprint": intent.destination_fingerprint}]
    assert store.get_delivery_intent("tenant-a", intent.intent_id).status == "delivered"
    assert len(store.list_notification_delivery_attempts_for_tenant("tenant-a")) == 1


def test_attempt_insert_rolls_back_if_intent_state_update_fails(store) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _intent(when)
    store.save_worker_analysis_run(_run(when), [intent])
    if isinstance(store, SQLiteStorage):
        store._conn.execute(
            """CREATE TRIGGER reject_delivered BEFORE UPDATE ON notification_delivery_queue
               WHEN NEW.status='delivered' BEGIN SELECT RAISE(ABORT,'injected state failure'); END"""
        )
    else:
        with store._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE notification_delivery_queue ADD CONSTRAINT reject_delivered "
                "CHECK (status <> 'delivered')"
            )
    attempt = NotificationDeliveryAttempt(
        attempt_id=hashlib.sha256(b"delivered-attempt").hexdigest(),
        notification_id=intent.notification_id, tenant_id="tenant-a",
        source_kind="analysis", source_id=intent.source_id,
        destination_fingerprint=intent.destination_fingerprint,
        attempted_at=when, outcome=DeliveryOutcome.DELIVERED,
        payload=intent.notification, http_status=200,
    )
    with pytest.raises(Exception, match=r"injected state failure|reject_delivered"):
        store.finish_delivery_intent_attempt(
            "tenant-a", intent.intent_id, attempt, expected_count=0,
            next_due_at=None,
        )
    item = store.get_delivery_intent("tenant-a", intent.intent_id)
    assert item.status == "pending" and item.attempt_count == 0
    assert store.list_notification_delivery_attempts_for_tenant("tenant-a") == []
