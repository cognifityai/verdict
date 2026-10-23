"""Scheduled local rescan and monitor worker."""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import http.client
import json
import logging
import os
import secrets
import signal
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from verdict.analysis_records import (
    DeliveryOutcome,
    NotificationDeliveryAttempt,
)
from verdict.client import _resolve_storage
from verdict.dashboard.analysis_service import run_analysis
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.issue_notices import IssueNotices
from verdict.dashboard.storage_url import is_postgres_storage
from verdict.dashboard.worker_status import (
    WorkerCycleStatus,
    WorkerDue,
    WorkerHeartbeat,
    cycle_failure_code,
)
from verdict.delivery_queue import (
    DeliveryQueueItem,
    create_intent,
    webhook_fingerprint,
)
from verdict.monitor_inputs import (
    LOCAL_TENANT,
    LOCAL_TRACE_SCOPE,
    MonitorProjectionPending,
    advance_monitor,
)
from verdict.monitoring import MonitorRebootstrapRequired, MonitorStateConflict
from verdict.storage.sqlite import sqlite_path
from verdict.telemetry.local_agents import capture_local_agents
from verdict.telemetry.model import safe_tenant_id

_log = logging.getLogger("verdict.service")
TENANT = LOCAL_TENANT
SCOPE = LOCAL_TRACE_SCOPE
DELIVERY_SEND_BUDGET = 2  # Keep slow destinations from consuming an entire backlog per poll.


class CycleBusy(RuntimeError):
    """Another cycle owns this store's analysis lease."""


class UnsupportedCyclePlatform(OSError):
    """Local SQLite cycle locking is unavailable on this platform."""


@contextlib.contextmanager
def worker_presence(storage_url: str, *, tenant_id: str = TENANT):
    """Publish a short-lived heartbeat while the recurring worker is alive."""
    heartbeat = WorkerHeartbeat(storage_url)
    worker_id = secrets.token_hex(16)
    stopped = threading.Event()

    def pulse() -> None:
        while not stopped.is_set():
            try:
                heartbeat.touch(tenant_id, worker_id)
            except Exception as exc:
                _log.warning("worker heartbeat failed: %s", type(exc).__name__)
            stopped.wait(30)

    try:
        heartbeat.touch(tenant_id, worker_id)
    except Exception as exc:
        _log.warning("initial worker heartbeat failed; retrying: %s", type(exc).__name__)
    thread = threading.Thread(target=pulse, name="verdict-worker-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=5)
        try:
            heartbeat.remove(tenant_id, worker_id)
        except Exception as exc:
            _log.warning("worker heartbeat cleanup failed: %s", type(exc).__name__)


@contextlib.contextmanager
def cycle_guard(storage_url: str):
    """Serialize local cycles across processes; an interrupted process releases its lease."""
    if is_postgres_storage(storage_url):
        import psycopg

        with psycopg.connect(storage_url, autocommit=True) as connection:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (0x5645524449435401,),
            ).fetchone()[0]
            if not acquired:
                raise CycleBusy("a scheduled cycle is already running")
            try:
                yield
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s)", (0x5645524449435401,))
        return
    if os.name != "posix":
        raise UnsupportedCyclePlatform("SQLite cycle locking requires POSIX; use PostgreSQL on this platform")
    import fcntl

    path = sqlite_path(storage_url)
    lock_path = path.with_name(path.name + ".service.lock")
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise CycleBusy("a scheduled cycle is already running") from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _configured_schedule(storage_url: str, *, tenant_id: str = TENANT) -> dict[str, object] | None:
    documents = ControlStore(storage_url).list_current(tenant_id)
    entry = next((item for item in documents if item["kind"] == "schedule" and item["documentId"] == "daily"), None)
    if entry is None or entry["state"] != "active":
        return None
    return {**entry["payload"], "_scheduleRevision": entry["revision"]}


def _schedule(storage_url: str, *, tenant_id: str = TENANT) -> dict[str, object]:
    schedule = _configured_schedule(storage_url, tenant_id=tenant_id)
    if schedule is None:
        raise ValueError("no active daily schedule; configure it in Verdict first")
    return schedule


def _finding_source_id(finding: dict[str, object]) -> str:
    """Identify the finding evidence, independent of a scan's wall-clock time."""
    return hashlib.sha256(json.dumps(
        finding, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _active_alert(storage_url: str, tenant_id: str) -> dict[str, object] | None:
    return next((item for item in ControlStore(storage_url).list_current(tenant_id)
                 if item["kind"] == "alert" and item["documentId"] == "default"), None)


def _worker_intent(
    alert: dict[str, object] | None, *, tenant_id: str,
    source_kind: str, source_id: str, notification: dict[str, object],
):
    config = alert.get("payload") if alert else None
    env_name = config.get("webhookUrlEnvVar") if isinstance(config, dict) else None
    return create_intent(
        alert, tenant_id=tenant_id, source_kind=source_kind,
        source_id=source_id, notification=notification,
        webhook_url=os.environ.get(env_name) if isinstance(env_name, str) else None,
    )


def _intent_destination_matches(
    storage_url: str, tenant_id: str, item: DeliveryQueueItem,
) -> tuple[bool, str | None]:
    intent = item.intent
    alert = _active_alert(storage_url, tenant_id)
    if alert is None or alert.get("state") != "active" \
            or alert.get("revision") != intent.alert_revision:
        return False, None
    config = alert.get("payload")
    if not isinstance(config, dict) or config.get("destination") != intent.destination:
        return False, None
    if config.get("drift" if intent.notification.get("kind") == "drift" else "findings") is not True:
        return False, None
    if intent.destination == "local_log":
        return True, None
    if config.get("webhookUrlEnvVar") != intent.webhook_url_env_var:
        return False, None
    url = os.environ.get(intent.webhook_url_env_var or "", "")
    fingerprint = webhook_fingerprint(url)
    if item.status == "unbound":
        return True, url if fingerprint else None
    return fingerprint == intent.destination_fingerprint, url if fingerprint else None


def _drain_delivery_queue(
    storage_url: str, *, tenant_id: str, now: datetime | None = None,
) -> list[dict[str, str]]:
    """Send a bounded page of committed obligations under the existing cycle guard."""
    instant = now or datetime.now(timezone.utc)
    storage = _resolve_storage(storage_url)
    outcomes: list[dict[str, str]] = []
    try:
        for item in storage.list_unbound_delivery_intents(tenant_id, now=instant, limit=100):
            matched, url = _intent_destination_matches(storage_url, tenant_id, item)
            if not matched:
                storage.block_delivery_intent(tenant_id, item.intent.intent_id)
                continue
            fingerprint = webhook_fingerprint(url or "")
            storage.probe_delivery_intent(
                tenant_id, item.intent.intent_id, now=instant,
                fingerprint=fingerprint,
                next_probe_at=None if fingerprint else instant + timedelta(minutes=1),
            )
        for item in storage.list_due_delivery_intents(
            tenant_id, now=instant, limit=DELIVERY_SEND_BUDGET,
        ):
            intent = item.intent
            matched, url = _intent_destination_matches(storage_url, tenant_id, item)
            if not matched:
                storage.block_delivery_intent(tenant_id, intent.intent_id)
                outcomes.append({"status": "blocked", "notificationId": intent.notification_id})
                continue
            if storage.mark_delivery_intent_previously_delivered(
                tenant_id, intent.intent_id, expected_count=item.attempt_count,
            ):
                outcomes.append({"status": "already_delivered",
                                 "notificationId": intent.notification_id})
                continue
            attempted_at = datetime.now(timezone.utc)
            http_status = None
            error_code = None
            try:
                if intent.destination == "local_log":
                    _log.warning("Verdict notification: %s", json.dumps(intent.notification, sort_keys=True))
                else:
                    body = json.dumps(
                        {"source": "verdict", "notification": intent.notification},
                        sort_keys=True,
                    ).encode()
                    request = urllib.request.Request(
                        url, data=body, method="POST",
                        headers={"Content-Type": "application/json",
                                 "Idempotency-Key": intent.notification_id},
                    )
                    with _open_webhook(request, timeout=10) as response:
                        http_status = response.status
                        if not 200 <= response.status < 300:
                            error_code = "http_rejected"
            except urllib.error.HTTPError as exc:
                http_status = exc.code
                error_code = "http_rejected"
            except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError,
                    UnicodeError):
                error_code = "transport_error"
            outcome = DeliveryOutcome.FAILED if error_code else DeliveryOutcome.DELIVERED
            attempt = NotificationDeliveryAttempt(
                attempt_id=hashlib.sha256(
                    f"{intent.notification_id}:{intent.destination_fingerprint}:"
                    f"{attempted_at.isoformat()}:{secrets.token_hex(8)}".encode()
                ).hexdigest(),
                notification_id=intent.notification_id,
                tenant_id=tenant_id,
                source_kind=intent.source_kind,
                source_id=intent.source_id,
                destination_fingerprint=intent.destination_fingerprint,
                attempted_at=attempted_at,
                outcome=outcome,
                payload=intent.notification,
                http_status=http_status,
                error_code=error_code,
            )
            next_due = instant + timedelta(seconds=min(30 * (2 ** item.attempt_count), 3600))
            if storage.finish_delivery_intent_attempt(
                tenant_id, intent.intent_id, attempt,
                expected_count=item.attempt_count, next_due_at=next_due,
            ):
                outcomes.append({
                    "status": outcome.value,
                    "notificationId": intent.notification_id,
                    "destinationFingerprint": intent.destination_fingerprint,
                })
    finally:
        storage.close()
    return outcomes


class _RejectWebhookRedirect(urllib.request.HTTPRedirectHandler):
    """A redirected POST cannot be treated as a delivered notification."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _open_webhook(request: urllib.request.Request, *, timeout: int):
    return urllib.request.build_opener(_RejectWebhookRedirect).open(request, timeout=timeout)



def run_cycle(storage_url: str, schedule: dict[str, object], *,
              tenant_id: str = TENANT) -> dict[str, object]:
    with cycle_guard(storage_url):
        earlier_deliveries = _drain_delivery_queue(storage_url, tenant_id=tenant_id)
        revision = schedule.get("_scheduleRevision")
        configured = isinstance(revision, int) and not isinstance(revision, bool) and revision > 0
        if configured and _configured_schedule(storage_url, tenant_id=tenant_id) != schedule:
            raise ValueError("saved schedule changed or paused before the cycle started")
        due = WorkerDue(storage_url) if configured else None
        if due is not None:
            due.begin(tenant_id, revision)
        return _run_cycle_locked(
            storage_url, schedule, tenant_id=tenant_id, due=due,
            earlier_deliveries=earlier_deliveries,
        )


def poll_cycle(storage_url: str, *, now: datetime | None = None,
               tenant_id: str = TENANT) -> dict[str, object]:
    """Run one due worker cycle, rereading the plan under the process guard."""
    with cycle_guard(storage_url):
        earlier_deliveries = _drain_delivery_queue(storage_url, tenant_id=tenant_id, now=now)
        schedule = _configured_schedule(storage_url, tenant_id=tenant_id)
        if schedule is None:
            return {"status": "no_active_schedule", "cycleRan": False,
                    "notifications": earlier_deliveries}
        revision = schedule["_scheduleRevision"]
        assert isinstance(revision, int)
        due = WorkerDue(storage_url)
        if not due.ready(tenant_id, revision, now=now):
            return {"status": "waiting", "cycleRan": False,
                    "notifications": earlier_deliveries}
        due.begin(tenant_id, revision, now=now)
        return _run_cycle_locked(storage_url, schedule, tenant_id=tenant_id,
                                 due=due, now=now,
                                 earlier_deliveries=earlier_deliveries)


def _run_cycle_locked(storage_url: str, schedule: dict[str, object], *,
                      tenant_id: str, due: WorkerDue | None = None,
                      now: datetime | None = None,
                      earlier_deliveries: list[dict[str, str]] | None = None,
                      ) -> dict[str, object]:
    status = WorkerCycleStatus(storage_url)
    revision = schedule.get("_scheduleRevision")
    status.begin(tenant_id, trigger="worker",
                 schedule_revision=revision if isinstance(revision, int) else None)
    try:
        result = _run_cycle_unlocked(storage_url, schedule, tenant_id=tenant_id)
        result["notifications"] = [
            *(earlier_deliveries or []),
            *_drain_delivery_queue(storage_url, tenant_id=tenant_id, now=now),
        ]
    except Exception:
        status.finish(tenant_id, state="failed", analysis_status=None,
                      failure_code="capture_or_cycle_error")
        if due is not None:
            due.finish(tenant_id, revision, interval_hours=int(schedule.get("intervalHours", 24)), now=now)
        raise
    failure = cycle_failure_code(result, schedule, manual=False)
    status.finish(tenant_id, state="failed" if failure else "completed",
                  analysis_status=result.get("analysis", {}).get("status"),
                  failure_code=failure)
    if due is not None:
        due.finish(tenant_id, revision, interval_hours=int(schedule.get("intervalHours", 24)), now=now)
    result["cycleFailureCode"] = failure
    return result


def _run_cycle_unlocked(storage_url: str, schedule: dict[str, object], *,
                        tenant_id: str) -> dict[str, object]:
    alert = _active_alert(storage_url, tenant_id)
    storage = _resolve_storage(storage_url)
    try:
        capture = capture_local_agents(
            storage,
            tenant_id=tenant_id,
            claude_root=(Path(str(schedule["claudeRoot"])).expanduser() if schedule.get("claudeRoot") else None),
            codex_root=(Path(str(schedule["codexRoot"])).expanduser() if schedule.get("codexRoot") else None),
            capture_content=True,
        )
        monitor = None
        policy = storage.get_active_monitor_policy(
            SCOPE if tenant_id == TENANT else f"{tenant_id}:application:trace"
        )
        if schedule.get("runMonitor") is True and policy is not None:
            try:
                _manifest, comparison = advance_monitor(
                    storage,
                    policy,
                    tenant_id=tenant_id,
                    delivery_intent_factory=lambda candidate, result: _worker_intent(
                        alert, tenant_id=tenant_id, source_kind="monitor",
                        source_id=candidate.snapshot_id,
                        notification={
                            "kind": "drift", "policyId": policy.policy_id,
                            "status": result.status.value,
                            "alerts": sum(metric.alert for metric in result.metrics),
                        },
                    ) if result.status.value in {"alert", "reference_stale"} else None,
                )
            except MonitorRebootstrapRequired:
                monitor = {
                    "policyId": policy.policy_id,
                    "status": "requires_rebootstrap",
                    "alerts": 0,
                }
            except MonitorProjectionPending:
                monitor = {
                    "policyId": policy.policy_id,
                    "status": "projection_pending",
                    "alerts": 0,
                }
            except MonitorStateConflict:
                monitor = {
                    "policyId": policy.policy_id,
                    "status": "superseded",
                    "alerts": 0,
                }
            else:
                monitor = {
                    "policyId": policy.policy_id,
                    "status": comparison.status.value,
                    "alerts": sum(metric.alert for metric in comparison.metrics),
                }
    finally:
        storage.close()
    from verdict.dashboard.app import build_agent_insights_bundle

    analysis = run_analysis(
        storage_url,
        tenant=tenant_id,
        build=lambda: build_agent_insights_bundle(
            storage_url, tenant=tenant_id, _include_input_fingerprint=True,
        ),
        delivery_intent_factory=lambda result: [
            intent for finding in result.get("findings", [])
            if isinstance(finding, dict) and finding.get("severity") in {"warning", "error"}
            if (intent := _worker_intent(
                alert, tenant_id=tenant_id, source_kind="analysis",
                source_id=_finding_source_id(finding),
                notification={"kind": "finding", **finding},
            )) is not None
        ],
    )
    new_issues = IssueNotices(storage_url).scan(tenant_id)
    if schedule.get("includeApprovedEvaluations") is False:
        scheduled_evaluation = {"status": "disabled_by_plan"}
    else:
        from verdict.dashboard.scoped_evaluation import run_scheduled_once

        scheduled_evaluation = run_scheduled_once(storage_url, tenant=tenant_id)
    return {
        "capture": capture.as_dict(),
        "analysis": analysis["analysisState"],
        "newIssues": new_issues,
        "monitor": monitor,
        "notifications": [],
        "scheduledEvaluation": scheduled_evaluation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verdict-service")
    parser.add_argument("--storage", default=os.environ.get("VERDICT_STORAGE", "sqlite:///./verdict.db"))
    parser.add_argument("--tenant-id", default=TENANT,
                        help="Single-tenant routing ID; match verdict-dashboard --tenant-id")
    parser.add_argument("--once", action="store_true", help="Run the configured cycle once")
    args = parser.parse_args(argv)
    if safe_tenant_id(args.tenant_id) is None:
        parser.error("--tenant-id must be a non-sensitive bounded routing identifier")
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    previous_term = None
    if not args.once and threading.current_thread() is threading.main_thread():
        previous_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda _number, _frame: stop.set())
    try:
        with worker_presence(args.storage, tenant_id=args.tenant_id) if not args.once else contextlib.nullcontext():
            while not stop.is_set():
                try:
                    result = (run_cycle(args.storage,
                                        _schedule(args.storage, tenant_id=args.tenant_id),
                                        tenant_id=args.tenant_id) if args.once
                              else poll_cycle(args.storage, tenant_id=args.tenant_id))
                except CycleBusy:
                    if args.once:
                        print(json.dumps({"status": "busy", "cycleRan": False}), flush=True)
                        return 2
                    _log.info("scheduled cycle is busy; retrying")
                    stop.wait(1)
                    continue
                except Exception as exc:
                    if args.once:
                        raise
                    _log.error("scheduled cycle failed; retrying after polling interval: %s",
                               type(exc).__name__)
                    stop.wait(30)
                    continue
                if result.get("cycleRan") is not False:
                    print(json.dumps(result, sort_keys=True), flush=True)
                if args.once:
                    return 1 if result.get("cycleFailureCode") else 0
                stop.wait(30)
    except KeyboardInterrupt:
        if args.once:
            raise
        return 0
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
    return 0
