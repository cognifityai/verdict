from datetime import datetime, timedelta, timezone

from verdict.dashboard.worker_status import (
    WorkerCycleStatus,
    WorkerDue,
    WorkerHeartbeat,
    cycle_failure_code,
)
from verdict.service import worker_presence


def test_heartbeat_is_tenant_scoped_and_expires_after_worker_disappears(tmp_path):
    storage = f"sqlite:///{tmp_path / 'worker.db'}"
    heartbeat = WorkerHeartbeat(storage)
    now = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
    assert heartbeat.read("tenant-a", now=now)["status"] == "unverified"
    heartbeat.touch("tenant-a", "worker-1", now=now)
    assert heartbeat.read("tenant-a", now=now)["status"] == "recent"
    assert heartbeat.read("tenant-b", now=now)["status"] == "unverified"
    assert heartbeat.read("tenant-a", now=now + timedelta(seconds=91))["status"] == "stale"
    heartbeat.remove("tenant-a", "worker-1")
    assert heartbeat.read("tenant-a", now=now)["status"] == "unverified"


def test_one_live_worker_keeps_status_recent_when_another_is_stale(tmp_path):
    heartbeat = WorkerHeartbeat(f"sqlite:///{tmp_path / 'workers.db'}")
    now = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
    heartbeat.touch("tenant", "old", now=now - timedelta(minutes=5))
    heartbeat.touch("tenant", "new", now=now)
    assert heartbeat.read("tenant", now=now)["status"] == "recent"
    assert heartbeat.read("tenant", now=now)["lastSeenAt"] == now.isoformat()


def test_service_presence_is_visible_during_lifetime_and_removed_on_shutdown(tmp_path):
    storage = f"sqlite:///{tmp_path / 'service.db'}"
    with worker_presence(storage):
        assert WorkerHeartbeat(storage).read("__verdict_local__")["status"] == "recent"
    assert WorkerHeartbeat(storage).read("__verdict_local__")["status"] == "unverified"


def test_cycle_status_preserves_start_unknown_failure_and_prior_success(tmp_path):
    status = WorkerCycleStatus(f"sqlite:///{tmp_path / 'cycles.db'}")
    start = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
    assert status.read("tenant")["state"] == "never_run"
    status.begin("tenant", trigger="worker", schedule_revision=1, now=start)
    started = status.read("tenant")
    assert started["state"] == "started" and started["finishedAt"] is None
    assert started["lastSuccessAt"] is None
    assert status.read("other")["state"] == "never_run"
    status.finish("tenant", state="completed", analysis_status="completed",
                  failure_code=None, now=start + timedelta(seconds=2))
    assert status.read("tenant")["lastSuccessAt"] == (start + timedelta(seconds=2)).isoformat()
    status.begin("tenant", trigger="manual", schedule_revision=None,
                 now=start + timedelta(minutes=1))
    status.finish("tenant", state="failed", analysis_status="error",
                  failure_code="analysis_error", now=start + timedelta(minutes=1, seconds=1))
    latest = status.read("tenant")
    assert latest["state"] == "failed" and latest["trigger"] == "manual"
    assert latest["failureCode"] == "analysis_error"
    assert latest["lastSuccessAt"] == (start + timedelta(seconds=2)).isoformat()
    status.begin("tenant", trigger="worker", schedule_revision=2,
                 now=start + timedelta(minutes=2))
    assert status.read("tenant")["state"] == "started"
    assert status.read("tenant")["scheduleRevision"] == 2


def test_cycle_outcome_requires_selected_checks_and_delivery():
    base = {"analysis": {"status": "completed"},
            "scheduledEvaluation": {"status": "idle"}, "notifications": []}
    assert cycle_failure_code(base, {}, manual=False) is None
    assert cycle_failure_code({**base, "analysis": {"status": "error"}}, {}, manual=False) == "analysis_error"
    assert cycle_failure_code({**base, "monitor": None}, {"runMonitor": True}, manual=False) == "monitor_incomplete"
    assert cycle_failure_code({**base, "monitor": {"status": "insufficient"}},
                              {"runMonitor": True}, manual=False) is None
    assert cycle_failure_code({**base, "monitor": {"state": "active", "snapshot": {}}},
                              {"runMonitor": True}, manual=True) is None
    assert cycle_failure_code({**base, "scheduledEvaluation": {"status": "waiting_provider"}},
                              {"includeApprovedEvaluations": True}, manual=False) == "approved_evaluation_incomplete"
    assert cycle_failure_code({**base, "notifications": [{"status": "failed"}]},
                              {}, manual=False) == "delivery_failed"
    incomplete = {**base, "capture": {"files": 1, "stored": 0, "skipped": 1,
                                      "skip_reasons": {"malformed_jsonl": 1}}}
    assert cycle_failure_code(incomplete, {}, manual=False) == "capture_incomplete"
    assert cycle_failure_code(incomplete, {}, manual=True) == "capture_incomplete"
    assert cycle_failure_code({**incomplete, "analysis": {"status": "error"}}, {},
                              manual=False) == "capture_incomplete"
    assert cycle_failure_code({**base, "capture": {"files": 0, "stored": 0,
                                                "skipped": 0}}, {}, manual=False) is None
    assert cycle_failure_code({**base, "capture": {"files": 1, "stored": 1,
                                                "skipped": 0, "recent_sample_errors": 1}},
                              {}, manual=False) == "capture_incomplete"
    assert cycle_failure_code({**base, "capture": {"files": 1, "stored": 1,
                                                "skipped": 0, "codex_model_calls": {
                                                    "status": "unavailable", "error": "source_error"}}},
                              {}, manual=False) == "capture_incomplete"
    assert cycle_failure_code({**base, "capture": {"files": 1, "stored": 1,
                                                "skipped": 0, "deferred": 1,
                                                "scan_incomplete": True}},
                              {}, manual=False) == "capture_incomplete"


def test_worker_due_is_tenant_and_revision_scoped_and_survives_manual_cycle(tmp_path):
    storage = f"sqlite:///{tmp_path / 'due.db'}"
    due = WorkerDue(storage)
    now = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
    assert due.ready("tenant", 1, now=now)
    due.begin("tenant", 1, now=now)
    assert not due.ready("tenant", 1, now=now + timedelta(seconds=29))
    assert due.ready("tenant", 1, now=now + timedelta(seconds=30))
    assert due.ready("other", 1, now=now)
    due.finish("tenant", 1, interval_hours=24, now=now + timedelta(minutes=1))
    expected = now + timedelta(days=1, minutes=1)
    assert due.read("tenant") == {"scheduleRevision": 1, "nextDueAt": expected.isoformat()}
    assert not due.ready("tenant", 1, now=expected - timedelta(seconds=1))
    status = WorkerCycleStatus(storage)
    status.begin("tenant", trigger="manual", schedule_revision=None, now=now + timedelta(minutes=2))
    status.finish("tenant", state="completed", analysis_status="completed",
                  failure_code=None, now=now + timedelta(minutes=3))
    assert not due.ready("tenant", 1, now=expected - timedelta(seconds=1))
    assert due.ready("tenant", 2, now=now + timedelta(minutes=3))
    assert due.ready("tenant", 1, now=expected)
