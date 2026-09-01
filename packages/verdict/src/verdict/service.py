"""Scheduled local rescan and monitor worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from verdict.analysis_records import (
    DeliveryOutcome,
    NotificationDeliveryAttempt,
)
from verdict.client import _resolve_storage
from verdict.dashboard.analysis_service import run_analysis
from verdict.dashboard.control_plane import ControlStore
from verdict.monitoring import (
    compare_manifest,
    plan_prospective_manifest,
    trace_monitor_units,
)
from verdict.telemetry.local_agents import capture_local_agents

_log = logging.getLogger("verdict.service")
TENANT = "__verdict_local__"
SCOPE = "__verdict_local__:application:trace"


def _schedule(storage_url: str) -> dict[str, object]:
    documents = ControlStore(storage_url).list_current(TENANT)
    entry = next((item for item in documents if item["kind"] == "schedule" and item["documentId"] == "daily"), None)
    if entry is None or entry["state"] != "active":
        raise ValueError("no active daily schedule; configure it in Verdict first")
    return dict(entry["payload"])


def _finding_source_id(finding: dict[str, object]) -> str:
    """Identify the finding evidence, independent of a scan's wall-clock time."""
    return hashlib.sha256(json.dumps(
        finding, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _notify(
    storage_url: str,
    *,
    source_kind: str,
    source_id: str,
    notification: dict[str, object],
) -> dict[str, str]:
    documents = ControlStore(storage_url).list_current(TENANT)
    entry = next((item for item in documents if item["kind"] == "alert" and item["documentId"] == "default"), None)
    if entry is None or entry["state"] != "active":
        return {"status": "disabled"}
    config = dict(entry["payload"])
    kind = notification.get("kind")
    enabled = (
        config.get("findings") is True if kind == "finding"
        else config.get("drift") is True if kind == "drift"
        else False
    )
    if not enabled:
        return {"status": "disabled"}
    destination = config.get("destination")
    env_name = config.get("webhookUrlEnvVar")
    url = os.environ.get(env_name, "") if isinstance(env_name, str) else ""
    if destination == "webhook" and not url.startswith("https://"):
        raise ValueError("webhook URL environment variable must contain an HTTPS URL")
    destination_material = "local_log" if destination == "local_log" else url
    destination_fingerprint = hashlib.sha256(destination_material.encode()).hexdigest()
    notification_id = hashlib.sha256(json.dumps(
        {
            "sourceKind": source_kind, "sourceId": source_id,
            "notification": notification,
        },
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    storage = _resolve_storage(storage_url)
    try:
        if storage.notification_was_delivered(notification_id, destination_fingerprint):
            return {
                "status": "already_delivered",
                "notificationId": notification_id,
                "destinationFingerprint": destination_fingerprint,
            }
        attempted_at = datetime.now(timezone.utc)
        http_status = None
        error_code = None
        try:
            if destination == "local_log":
                _log.warning("Verdict notification: %s", json.dumps(notification, sort_keys=True))
            else:
                body = json.dumps(
                    {"source": "verdict", "notification": notification}, sort_keys=True,
                ).encode()
                request = urllib.request.Request(
                    url,
                    data=body,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Idempotency-Key": notification_id,
                    },
                )
                with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
                    http_status = response.status
                    if not 200 <= response.status < 300:
                        error_code = "http_rejected"
        except urllib.error.HTTPError as exc:
            http_status = exc.code
            error_code = "http_rejected"
        except (OSError, urllib.error.URLError):
            error_code = "transport_error"
        outcome = (
            DeliveryOutcome.FAILED if error_code else DeliveryOutcome.DELIVERED
        )
        attempt = NotificationDeliveryAttempt(
            attempt_id=hashlib.sha256(
                f"{notification_id}:{destination_fingerprint}:{attempted_at.isoformat()}:{secrets.token_hex(8)}".encode()
            ).hexdigest(),
            notification_id=notification_id,
            tenant_id=TENANT,
            source_kind=source_kind,
            source_id=source_id,
            destination_fingerprint=destination_fingerprint,
            attempted_at=attempted_at,
            outcome=outcome,
            payload=notification,
            http_status=http_status,
            error_code=error_code,
        )
        storage.save_notification_delivery_attempt(attempt)
    finally:
        storage.close()
    return {
        "status": outcome.value,
        "notificationId": notification_id,
        "destinationFingerprint": destination_fingerprint,
    }


def run_cycle(storage_url: str, schedule: dict[str, object]) -> dict[str, object]:
    storage = _resolve_storage(storage_url)
    try:
        capture = capture_local_agents(
            storage,
            tenant_id=TENANT,
            claude_root=(Path(str(schedule["claudeRoot"])).expanduser() if schedule.get("claudeRoot") else None),
            codex_root=(Path(str(schedule["codexRoot"])).expanduser() if schedule.get("codexRoot") else None),
            capture_content=True,
        )
        monitor = None
        policy = storage.get_active_monitor_policy(SCOPE)
        if schedule.get("runMonitor") is True and policy is not None:
            previous = storage.get_latest_monitor_snapshot(policy.policy_id)
            if previous is not None:
                traces = storage.list_traces(tenant_id=TENANT, limit=100_001)
                if len(traces) > 100_000:
                    raise ValueError("monitor exceeds bounded trace limit")
                assignments = None
                if policy.grouping_mode == "cluster":
                    pointer = storage.get_active_cluster_registry(TENANT)
                    if pointer.version_id is None:
                        raise ValueError("cluster monitor has no active registry")
                    rows = storage.list_trace_cluster_assignments(
                        TENANT, pointer.version_id, limit=100_001
                    )
                    assignments = {
                        row.trace_id: row.cluster_id for row in rows
                        if row.status == "assigned" and row.cluster_id is not None
                    }
                units = trace_monitor_units(
                    traces, grouping_mode=policy.grouping_mode,
                    cluster_assignments=assignments,
                )
                manifest = plan_prospective_manifest(previous[0], units, policy)
                comparison = compare_manifest(units, manifest, policy)
                storage.save_monitor_snapshot(policy.policy_id, manifest, comparison)
                monitor = {
                    "policyId": policy.policy_id,
                    "status": comparison.status.value,
                    "alerts": sum(metric.alert for metric in comparison.metrics),
                }
                if comparison.status.value in {"alert", "reference_stale"}:
                    _notify(
                        storage_url,
                        source_kind="monitor",
                        source_id=manifest.snapshot_id,
                        notification={"kind": "drift", **monitor},
                    )
    finally:
        storage.close()
    from verdict.dashboard.app import build_agent_insights_bundle

    analysis = run_analysis(
        storage_url,
        tenant=TENANT,
        build=lambda: build_agent_insights_bundle(
            storage_url, tenant=TENANT, _include_input_fingerprint=True,
        ),
    )
    notifications = [
        _notify(
            storage_url,
            source_kind="analysis",
            source_id=_finding_source_id(finding),
            notification={"kind": "finding", **finding},
        )
        for finding in analysis.get("findings", [])
        if finding.get("severity") in {"warning", "error"}
    ]
    return {
        "capture": capture.as_dict(),
        "analysis": analysis["analysisState"],
        "monitor": monitor,
        "notifications": notifications,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verdict-service")
    parser.add_argument("--storage", default=os.environ.get("VERDICT_STORAGE", "sqlite:///./verdict.db"))
    parser.add_argument("--once", action="store_true", help="Run the configured cycle once")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    while True:
        schedule = _schedule(args.storage)
        print(json.dumps(run_cycle(args.storage, schedule), sort_keys=True))
        if args.once:
            return 0
        interval = int(schedule.get("intervalHours", 24))
        threading.Event().wait(interval * 3600)
