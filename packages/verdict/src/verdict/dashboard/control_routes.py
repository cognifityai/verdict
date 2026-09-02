"""Control-plane and scheduled-cycle routes for the local dashboard."""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from verdict.dashboard.analysis_service import run_analysis
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.monitor_routes import SCOPE, TENANT, MonitorRoutes
from verdict.dashboard.setup_routes import SetupRoutes
from verdict.telemetry.local_agents import capture_local_agents


class ControlRoutes:
    """Own durable control documents, review summaries, and manual cycles."""

    def __init__(
        self,
        storage_url: str,
        setup: SetupRoutes,
        monitor: MonitorRoutes,
    ) -> None:
        self.storage_url = storage_url
        self.setup = setup
        self.monitor = monitor

    def register(self, app) -> None:
        @app.get("/api/control")
        def control_state():
            writable = self.setup.writable_storage()
            try:
                documents = ControlStore(self.storage_url).list_current(TENANT)
                signals = writable.list_user_signals(limit=10_001)
                attempts = writable.list_notification_delivery_attempts_for_tenant(
                    TENANT, limit=100
                )
                traces = writable.list_traces(tenant_id=TENANT, limit=501)
                local_sources = sorted(
                    source_kind
                    for source_kind in ("claude-code", "codex")
                    if writable.has_agent_run_source_kind(TENANT, source_kind)
                )
                has_local_schedule = any(
                    item["kind"] == "schedule"
                    and item["state"] == "active"
                    and any(item["payload"].get(name) for name in ("claudeRoot", "codexRoot"))
                    for item in documents
                )
                return {
                    "schema": "product-control-v1",
                    "documents": documents,
                    "userSignals": self._user_signals(signals),
                    "reviewQueue": self._review_queue(writable, traces[:500]),
                    "reviewQueueScope": {
                        "tracesAnalyzed": min(len(traces), 500),
                        "complete": len(traces) <= 500,
                    },
                    "notifications": self._notifications(writable),
                    "deliveryAttempts": [
                        {
                            "attemptId": item.attempt_id,
                            "notificationId": item.notification_id,
                            "sourceKind": item.source_kind,
                            "sourceId": item.source_id,
                            "attemptedAt": item.attempted_at.isoformat(),
                            "outcome": item.outcome.value,
                            "httpStatus": item.http_status,
                            "errorCode": item.error_code,
                        }
                        for item in attempts
                    ],
                    "dailyOperations": {
                        "mode": (
                            "local_agent"
                            if local_sources or has_local_schedule
                            else "telemetry"
                        ),
                        "localAgentSources": local_sources,
                    },
                    "defaults": {
                        "captureContent": True,
                        "retentionDays": None,
                        "scheduleIntervalHours": 24,
                    },
                }
            finally:
                writable.close()

        def control_put(request, kind: str, document_id: str, payload: dict[str, Any]):
            if not self.setup.authorized(request):
                return JSONResponse(
                    {"error": "control authorization required"}, status_code=403
                )
            try:
                body = payload.get("payload")
                if not isinstance(body, dict):
                    raise ValueError("invalid control payload")
                return ControlStore(self.storage_url).append(
                    TENANT,
                    kind=kind,
                    document_id=document_id,
                    state=payload.get("state"),
                    payload=body,
                    expected_revision=payload.get("expectedRevision"),
                )
            except (OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse(
                    {"error": "invalid or conflicting control update"}, status_code=409
                )

        control_put.__annotations__["request"] = Request
        app.post("/api/control/{kind}/{document_id}")(control_put)

        def control_rollback(
            request, kind: str, document_id: str, payload: dict[str, Any]
        ):
            if not self.setup.authorized(request):
                return JSONResponse(
                    {"error": "control authorization required"}, status_code=403
                )
            try:
                return ControlStore(self.storage_url).rollback(
                    TENANT,
                    kind=kind,
                    document_id=document_id,
                    target_revision=int(payload["targetRevision"]),
                    expected_revision=int(payload["expectedRevision"]),
                )
            except (KeyError, OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse(
                    {"error": "invalid or conflicting rollback"}, status_code=409
                )

        control_rollback.__annotations__["request"] = Request
        app.post("/api/control/{kind}/{document_id}/rollback")(control_rollback)

        def schedule_run(request, payload: dict[str, Any]):
            if not self.setup.authorized(request):
                return JSONResponse(
                    {"error": "control authorization required"}, status_code=403
                )
            try:
                claude_root, codex_root = self.setup.roots(payload)
                if claude_root is None and codex_root is None:
                    raise ValueError("schedule source approval required")
                writable = self.setup.writable_storage()
                try:
                    summary = capture_local_agents(
                        writable,
                        tenant_id=TENANT,
                        claude_root=claude_root,
                        codex_root=codex_root,
                        capture_content=True,
                    )
                    monitor_result = self._run_monitor(
                        writable, payload.get("runMonitor") is True
                    )
                finally:
                    writable.close()
                from verdict.dashboard.app import build_agent_insights_bundle

                analysis = run_analysis(
                    self.storage_url,
                    tenant=TENANT,
                    build=lambda: build_agent_insights_bundle(
                        self.storage_url,
                        tenant=TENANT,
                        _include_input_fingerprint=True,
                    ),
                )
                return {
                    "capture": summary.as_dict(),
                    "analysis": analysis["analysisState"],
                    "monitor": monitor_result,
                }
            except (OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse({"error": "scheduled cycle failed"}, status_code=400)

        schedule_run.__annotations__["request"] = Request
        app.post("/api/control/actions/run-schedule")(schedule_run)

    @staticmethod
    def _user_signals(signals) -> dict[str, object]:
        counts = Counter(signal.kind for signal in signals[:10_000])
        return {
            "counts": dict(sorted(counts.items())),
            "analyzed": min(len(signals), 10_000),
            "complete": len(signals) <= 10_000,
        }

    @staticmethod
    def _review_queue(writable, traces) -> list[dict[str, object]]:
        queue: list[dict[str, object]] = []
        for trace in traces:
            for judgment in writable.list_judgments_for_trace(trace.trace_id, limit=20):
                for result in judgment.dimensions:
                    verdict = getattr(result.verdict, "value", result.verdict)
                    if verdict not in {"fail", "unclear"}:
                        continue
                    queue.append(
                        {
                            "traceId": trace.trace_id,
                            "judgmentId": judgment.judgment_id,
                            "dimension": result.name,
                            "verdict": verdict,
                            "evaluatorFingerprint": judgment.evaluator_fingerprint,
                        }
                    )
                    if len(queue) == 100:
                        return queue
        return queue

    @staticmethod
    def _notifications(writable) -> list[dict[str, object]]:
        notifications: list[dict[str, object]] = []
        active = writable.get_active_monitor_policy(SCOPE)
        if active is not None:
            snapshot = writable.get_latest_monitor_snapshot(active.policy_id)
            if snapshot is not None and snapshot[1].status.value in {
                "alert",
                "reference_stale",
            }:
                notifications.append(
                    {
                        "kind": "monitor",
                        "severity": "warning",
                        "message": snapshot[1].status.value.replace("_", " "),
                        "policyId": active.policy_id,
                    }
                )
            latest_alert = writable.get_latest_monitor_alert(active.policy_id)
            if latest_alert is not None and (
                snapshot is None or latest_alert[0].snapshot_id != snapshot[0].snapshot_id
            ):
                notifications.append(
                    {
                        "kind": "monitor_history",
                        "severity": "warning",
                        "message": "A prior monitor alert remains open for review",
                        "policyId": active.policy_id,
                        "snapshotId": latest_alert[0].snapshot_id,
                    }
                )
        drift = writable.list_drift_signals(limit=100)
        if drift:
            notifications.append(
                {
                    "kind": "drift",
                    "severity": "warning",
                    "message": f"{len(drift)} stored drift signal(s) require evaluator-aware review",
                }
            )
        return notifications

    def _run_monitor(self, writable, enabled: bool):
        if not enabled:
            return None
        policy = writable.get_active_monitor_policy(SCOPE)
        if policy is None:
            return None
        previous = writable.get_latest_monitor_snapshot(policy.policy_id)
        if previous is None:
            return None
        manifest, comparison = self.monitor.prospective(writable, policy, previous[0])
        writable.save_monitor_snapshot(policy.policy_id, manifest, comparison)
        return self.monitor.response(policy, "active", manifest, comparison)
