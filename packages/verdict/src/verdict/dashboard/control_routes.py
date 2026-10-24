"""Control-plane and scheduled-cycle routes for the local dashboard."""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from verdict.dashboard.analysis_service import run_analysis
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.issue_notices import IssueNotices
from verdict.dashboard.monitor_routes import MonitorRoutes
from verdict.dashboard.setup_routes import SetupRoutes
from verdict.dashboard.worker_status import (
    WorkerCycleStatus,
    WorkerDue,
    WorkerHeartbeat,
    cycle_failure_code,
)
from verdict.delivery_queue import create_intent, validate_delivery_cursor
from verdict.monitoring import MonitorRebootstrapRequired, monitor_requires_rebootstrap
from verdict.service import (
    CycleBusy,
    UnsupportedCyclePlatform,
    _intent_destination_matches,
    cycle_guard,
)
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
        self.tenant_id = setup.tenant_id
        self.scope = monitor.scope

    def _next_due(self, schedule: dict[str, Any] | None) -> dict[str, object] | None:
        if schedule is None or schedule["state"] != "active":
            return None
        due = WorkerDue(self.storage_url).read(self.tenant_id)
        return due if due is not None and due["scheduleRevision"] == schedule["revision"] else None

    def _delivery_counts(self) -> dict[str, int]:
        writable = self.setup.writable_storage()
        try:
            return writable.delivery_queue_counts(self.tenant_id)
        finally:
            writable.close()

    @staticmethod
    def _delivery_view(item) -> dict[str, object]:
        intent = item.intent
        return {
            "intentId": intent.intent_id,
            "notificationId": intent.notification_id,
            "sourceKind": intent.source_kind,
            "sourceId": intent.source_id,
            "kind": intent.notification.get("kind"),
            "code": intent.notification.get("code") if isinstance(
                intent.notification.get("code"), str
            ) and len(intent.notification["code"]) <= 80 else None,
            "status": item.status,
            "attemptCount": item.attempt_count,
            "alertRevision": intent.alert_revision,
            "destination": intent.destination,
            "destinationFingerprint": intent.destination_fingerprint,
            "createdAt": intent.created_at.isoformat(),
            "nextDueAt": item.next_due_at.isoformat() if item.next_due_at else None,
            "nextProbeAt": item.next_probe_at.isoformat() if item.next_probe_at else None,
            "lastErrorCode": item.last_error_code,
            "supersededById": item.superseded_by_id,
        }

    def register(self, app) -> None:
        @app.get("/api/readiness")
        def readiness(request: Request):
            if not self.setup.request_matches_tenant(request):
                return JSONResponse({"error": "readiness unavailable"}, status_code=403)
            documents = ControlStore(self.storage_url).list_current(self.tenant_id)
            schedule = next((item for item in documents
                             if item["kind"] == "schedule" and item["documentId"] == "daily"), None)
            return {
                "schema": "readiness-v1",
                "documents": ([{key: schedule[key] for key in ("kind", "documentId", "state", "revision")}] if schedule else []),
                "worker": WorkerHeartbeat(self.storage_url).read(self.tenant_id),
                "lastCycle": WorkerCycleStatus(self.storage_url).read(self.tenant_id),
                "nextDue": self._next_due(schedule),
                "openIssues": IssueNotices(self.storage_url).count_open(self.tenant_id),
                "deliveryQueue": self._delivery_counts(),
            }

        @app.get("/api/control")
        def control_state(request: Request):
            if not self.setup.request_matches_tenant(request):
                return JSONResponse({"error": "control unavailable"}, status_code=403)
            writable = self.setup.writable_storage()
            try:
                documents = [item for item in ControlStore(self.storage_url).list_current(self.tenant_id)
                             if item["kind"] in {"settings", "schedule", "alert", "proposal", "review"}]
                schedule = next((item for item in documents
                                 if item["kind"] == "schedule" and item["documentId"] == "daily"), None)
                attempts = writable.list_notification_delivery_attempts_for_tenant(
                    self.tenant_id, limit=100
                )
                queued = writable.list_delivery_intents_for_tenant(
                    self.tenant_id, limit=100, actionable_only=True,
                )
                traces = writable.list_traces(tenant_id=self.tenant_id, limit=501)
                local_sources = sorted(
                    source_kind
                    for source_kind in ("claude-code", "codex")
                    if writable.has_agent_run_source_kind(self.tenant_id, source_kind)
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
                    "deliveryQueue": [self._delivery_view(item) for item in queued],
                    "deliveryQueueCounts": writable.delivery_queue_counts(self.tenant_id),
                    "dailyOperations": {
                        "mode": (
                            "local_agent"
                            if local_sources or has_local_schedule
                            else "telemetry"
                        ),
                        "localAgentSources": local_sources,
                    },
                    "worker": WorkerHeartbeat(self.storage_url).read(self.tenant_id),
                    "lastCycle": WorkerCycleStatus(self.storage_url).read(self.tenant_id),
                    "nextDue": self._next_due(schedule),
                    "defaults": {
                        "captureContent": True,
                        "retentionDays": None,
                        "scheduleIntervalHours": 24,
                    },
                }
            finally:
                writable.close()

        @app.get("/api/control/delivery")
        def delivery_page(
            request: Request,
            afterCreatedAt: str | None = None,
            afterIntentId: str | None = None,
        ):
            if not self.setup.request_matches_tenant(request):
                return JSONResponse({"error": "control unavailable"}, status_code=403)
            after = None
            fields = (afterCreatedAt, afterIntentId)
            if any(value is not None for value in fields):
                if any(value is None for value in fields) or len(afterCreatedAt) > 40:
                    return JSONResponse({"error": "invalid delivery cursor"}, status_code=400)
                try:
                    after = validate_delivery_cursor((
                        datetime.fromisoformat(afterCreatedAt), afterIntentId,
                    ))
                except (TypeError, ValueError):
                    return JSONResponse({"error": "invalid delivery cursor"}, status_code=400)
            writable = self.setup.writable_storage()
            try:
                items = writable.list_delivery_intents_for_tenant(
                    self.tenant_id, limit=101, after=after, actionable_only=True,
                )
                return {"deliveryQueue": [self._delivery_view(item) for item in items[:100]],
                        "hasMore": len(items) > 100}
            finally:
                writable.close()

        def delivery_replay(request: Request, intent_id: str, payload: dict[str, Any]):
            if not self.setup.authorized(request):
                return JSONResponse({"error": "control authorization required"}, status_code=403)
            if len(intent_id) != 64 or any(ch not in "0123456789abcdef" for ch in intent_id):
                return JSONResponse({"error": "invalid delivery identity"}, status_code=400)
            try:
                with cycle_guard(self.storage_url):
                    writable = self.setup.writable_storage()
                    try:
                        item = writable.get_delivery_intent(self.tenant_id, intent_id)
                        if item is None or item.status != "exhausted" or (
                            type(payload.get("expectedAttemptCount")) is not int
                            or payload["expectedAttemptCount"] != item.attempt_count
                        ):
                            return JSONResponse({"error": "delivery state changed"}, status_code=409)
                        matched, _ = _intent_destination_matches(
                            self.storage_url, self.tenant_id, item,
                        )
                        if not matched or not writable.replay_delivery_intent(
                            self.tenant_id, intent_id, now=datetime.now(timezone.utc),
                            expected_count=item.attempt_count,
                        ):
                            return JSONResponse({"error": "destination changed"}, status_code=409)
                        return {"status": "queued", "intentId": intent_id}
                    finally:
                        writable.close()
            except CycleBusy:
                return JSONResponse({"error": "worker is busy"}, status_code=409)

        delivery_replay.__annotations__["request"] = Request
        app.post("/api/control/delivery/{intent_id}/replay")(delivery_replay)

        def delivery_rebind_preview(request: Request, intent_id: str):
            if not self.setup.authorized(request):
                return JSONResponse({"error": "control authorization required"}, status_code=403)
            if len(intent_id) != 64 or any(ch not in "0123456789abcdef" for ch in intent_id):
                return JSONResponse({"error": "invalid delivery identity"}, status_code=400)
            writable = self.setup.writable_storage()
            try:
                old = writable.get_delivery_intent(self.tenant_id, intent_id)
                if old is None or old.status not in {"pending", "failed", "blocked", "exhausted"}:
                    return JSONResponse({"error": "delivery cannot be rebound"}, status_code=409)
            finally:
                writable.close()
            current = next((item for item in ControlStore(self.storage_url).list_current(
                self.tenant_id,
            ) if item["kind"] == "alert" and item["documentId"] == "default"), None)
            config = current.get("payload") if current else None
            env_name = config.get("webhookUrlEnvVar") if isinstance(config, dict) else None
            replacement = create_intent(
                current, tenant_id=self.tenant_id, source_kind=old.intent.source_kind,
                source_id=old.intent.source_id, notification=old.intent.notification,
                webhook_url=os.environ.get(env_name) if isinstance(env_name, str) else None,
                rebind_from_id=intent_id,
            )
            if replacement is None:
                return JSONResponse({"error": "no active destination for this alert"}, status_code=409)
            return {
                "oldIntent": self._delivery_view(old),
                "newIntentId": replacement.intent_id,
                "currentAlertRevision": replacement.alert_revision,
                "newDestination": replacement.destination,
                "newDestinationFingerprint": replacement.destination_fingerprint,
                "notificationId": replacement.notification_id,
            }

        delivery_rebind_preview.__annotations__["request"] = Request
        app.get("/api/control/delivery/{intent_id}/rebind-preview")(delivery_rebind_preview)

        def delivery_rebind(request: Request, intent_id: str, payload: dict[str, Any]):
            if not self.setup.authorized(request):
                return JSONResponse({"error": "control authorization required"}, status_code=403)
            try:
                with cycle_guard(self.storage_url):
                    preview = delivery_rebind_preview(request, intent_id)
                    if isinstance(preview, JSONResponse):
                        return preview
                    old_view = preview["oldIntent"]
                    if (payload.get("expectedOldStatus") != old_view["status"]
                            or type(payload.get("expectedAttemptCount")) is not int
                            or payload["expectedAttemptCount"] != old_view["attemptCount"]
                            or type(payload.get("expectedAlertRevision")) is not int
                            or payload["expectedAlertRevision"] != preview["currentAlertRevision"]
                            or payload.get("expectedNewFingerprint") != preview["newDestinationFingerprint"]):
                        return JSONResponse({"error": "delivery preview changed"}, status_code=409)
                    writable = self.setup.writable_storage()
                    try:
                        old = writable.get_delivery_intent(self.tenant_id, intent_id)
                        if old is None:
                            return JSONResponse({"error": "delivery changed"}, status_code=409)
                        current = next((item for item in ControlStore(self.storage_url).list_current(
                            self.tenant_id,
                        ) if item["kind"] == "alert" and item["documentId"] == "default"), None)
                        config = current.get("payload") if current else None
                        env_name = config.get("webhookUrlEnvVar") if isinstance(config, dict) else None
                        replacement = create_intent(
                            current, tenant_id=self.tenant_id,
                            source_kind=old.intent.source_kind, source_id=old.intent.source_id,
                            notification=old.intent.notification,
                            webhook_url=os.environ.get(env_name) if isinstance(env_name, str) else None,
                            rebind_from_id=intent_id,
                        )
                        if replacement is None or replacement.intent_id != preview["newIntentId"] \
                                or replacement.destination_fingerprint != preview[
                                    "newDestinationFingerprint"
                                ] or not writable.rebind_delivery_intent(
                                    self.tenant_id, intent_id,
                                    expected_status=old_view["status"],
                                    expected_count=old_view["attemptCount"],
                                    new_intent=replacement,
                                ):
                            return JSONResponse({"error": "delivery changed"}, status_code=409)
                        return {"status": "queued", "intentId": replacement.intent_id}
                    finally:
                        writable.close()
            except (CycleBusy, ValueError):
                return JSONResponse({"error": "delivery changed"}, status_code=409)

        delivery_rebind.__annotations__["request"] = Request
        app.post("/api/control/delivery/{intent_id}/rebind")(delivery_rebind)

        def control_put(request, kind: str, document_id: str, payload: dict[str, Any]):
            if not self.setup.authorized(request):
                return JSONResponse(
                    {"error": "control authorization required"}, status_code=403
                )
            if kind in {"case_set", "experiment", "scoped_evaluation", "scheduled_evaluation"}:
                return JSONResponse({"error": "use the experiment workflow"}, status_code=400)
            try:
                body = payload.get("payload")
                if not isinstance(body, dict):
                    raise ValueError("invalid control payload")
                guard = cycle_guard(self.storage_url) if kind == "alert" and document_id == "default" \
                    else contextlib.nullcontext()
                with guard:
                    return ControlStore(self.storage_url).append(
                        self.tenant_id,
                        kind=kind,
                        document_id=document_id,
                        state=payload.get("state"),
                        payload=body,
                        expected_revision=payload.get("expectedRevision"),
                    )
            except (CycleBusy, OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse(
                    {"error": "invalid or conflicting control update"}, status_code=409
                )

        control_put.__annotations__["request"] = Request

        def control_rollback(
            request, kind: str, document_id: str, payload: dict[str, Any]
        ):
            if not self.setup.authorized(request):
                return JSONResponse(
                    {"error": "control authorization required"}, status_code=403
                )
            if kind in {"scheduled_evaluation", "experiment", "scoped_evaluation"}:
                return JSONResponse({"error": "result or egress record cannot be rolled back"},
                                    status_code=400)
            try:
                guard = cycle_guard(self.storage_url) if kind == "alert" and document_id == "default" \
                    else contextlib.nullcontext()
                with guard:
                    return ControlStore(self.storage_url).rollback(
                        self.tenant_id,
                        kind=kind,
                        document_id=document_id,
                        target_revision=int(payload["targetRevision"]),
                        expected_revision=int(payload["expectedRevision"]),
                    )
            except (CycleBusy, KeyError, OSError, TypeError, UnicodeError, ValueError):
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
                if "includeApprovedEvaluations" in payload and not isinstance(
                    payload["includeApprovedEvaluations"], bool
                ):
                    raise ValueError("includeApprovedEvaluations must be a boolean")
                claude_root, codex_root = self.setup.roots(payload)
                if claude_root is None and codex_root is None and payload.get("analyzeOnly") is not True:
                    raise ValueError("schedule source approval required")
                with cycle_guard(self.storage_url):
                    status = WorkerCycleStatus(self.storage_url)
                    status.begin(self.tenant_id, trigger="manual", schedule_revision=None)
                    finished = False
                    try:
                        writable = self.setup.writable_storage()
                        try:
                            monitor_policy = None
                            if payload.get("runMonitor") is True:
                                monitor_policy = writable.get_active_monitor_policy(self.scope)
                                if monitor_policy is not None:
                                    previous = writable.get_latest_monitor_snapshot(
                                        monitor_policy.policy_id
                                    )
                                    if monitor_requires_rebootstrap(
                                        monitor_policy,
                                        previous[0] if previous is not None else None,
                                        active=True,
                                    ):
                                        raise MonitorRebootstrapRequired(
                                            "monitor requires re-bootstrap"
                                        )
                            summary = capture_local_agents(
                                writable,
                                tenant_id=self.tenant_id,
                                claude_root=claude_root,
                                codex_root=codex_root,
                                capture_content=True,
                            )
                            monitor_result = self._run_monitor(writable, monitor_policy)
                        finally:
                            writable.close()
                        from verdict.dashboard.app import build_agent_insights_bundle

                        analysis = run_analysis(
                            self.storage_url,
                            tenant=self.tenant_id,
                            build=lambda: build_agent_insights_bundle(
                                self.storage_url,
                                tenant=self.tenant_id,
                                _include_input_fingerprint=True,
                            ),
                        )
                        new_issues = IssueNotices(self.storage_url).scan(self.tenant_id)
                        if payload.get("includeApprovedEvaluations") is False:
                            scheduled_evaluation = {"status": "disabled_by_plan"}
                        else:
                            from verdict.dashboard.scoped_evaluation import run_scheduled_once

                            scheduled_evaluation = run_scheduled_once(
                                self.storage_url, tenant=self.tenant_id,
                            )
                        result = {
                            "capture": summary.as_dict(),
                            "analysis": analysis["analysisState"],
                            "newIssues": new_issues,
                            "monitor": monitor_result,
                            "scheduledEvaluation": scheduled_evaluation,
                        }
                        failure = cycle_failure_code(result, payload, manual=True)
                        status.finish(self.tenant_id, state="failed" if failure else "completed",
                                      analysis_status=result["analysis"].get("status"),
                                      failure_code=failure)
                        finished = True
                        result["cycleFailureCode"] = failure
                        return result
                    except Exception:
                        if not finished:
                            status.finish(self.tenant_id, state="failed", analysis_status=None,
                                          failure_code="capture_or_cycle_error")
                        raise
            except MonitorRebootstrapRequired:
                return JSONResponse(
                    {"error": "monitor requires re-bootstrap"},
                    status_code=409,
                )
            except CycleBusy:
                return JSONResponse({"error": "scheduled cycle already running"}, status_code=409)
            except UnsupportedCyclePlatform as exc:
                return JSONResponse({"error": str(exc)}, status_code=501)
            except (OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse({"error": "scheduled cycle failed"}, status_code=400)

        schedule_run.__annotations__["request"] = Request
        app.post("/api/control/actions/run-schedule")(schedule_run)

        @app.get("/api/control/issues")
        def issues(request: Request, offset: int = 0, state: str = "open"):
            if not self.setup.request_matches_tenant(request):
                return JSONResponse({"error": "issues unavailable"}, status_code=403)
            if offset < 0 or offset > 100_000 or state not in {"open", "reviewed"}:
                return JSONResponse({"error": "invalid issue page"}, status_code=400)
            page = IssueNotices(self.storage_url).list(self.tenant_id, limit=51,
                                                        offset=offset, state=state)
            return {"schema": "agent-issue-notices-v1", "issues": page[:50],
                    "nextOffset": offset + 50 if len(page) > 50 else None,
                    "state": state}

        @app.get("/api/control/issues/{notice_id}")
        def issue_detail(request: Request, notice_id: str):
            if not self.setup.request_matches_tenant(request):
                return JSONResponse({"error": "issue unavailable"}, status_code=403)
            try:
                notice = IssueNotices(self.storage_url).get(self.tenant_id, notice_id)
            except (TypeError, ValueError):
                notice = None
            if notice is None:
                return JSONResponse({"error": "issue unavailable"}, status_code=404)
            return notice

        @app.get("/api/control/issues/{notice_id}/candidate-rechecks")
        def issue_recheck_candidates(request: Request, notice_id: str):
            if not self.setup.request_matches_tenant(request):
                return JSONResponse({"error": "candidate evidence unavailable"}, status_code=403)
            try:
                return IssueNotices(self.storage_url).find_recheck_candidates(
                    self.tenant_id, notice_id,
                )
            except (TypeError, ValueError):
                return JSONResponse({"error": "candidate evidence unavailable"},
                                    status_code=404)

        @app.post("/api/control/issues/{notice_id}/rechecks")
        async def attach_issue_recheck(request: Request, notice_id: str):
            if not self.setup.authorized(request):
                return JSONResponse({"error": "control authorization required"}, status_code=403)
            try:
                payload = await request.json()
                if not isinstance(payload, dict) or set(payload) != {"runId", "eventId"}:
                    raise ValueError("invalid recheck request")
                recheck = IssueNotices(self.storage_url).attach_recheck(
                    self.tenant_id, notice_id, payload["runId"], payload["eventId"],
                )
            except (TypeError, ValueError):
                return JSONResponse({"error": "recheck evidence unavailable"},
                                    status_code=400)
            return {"noticeId": notice_id, "recheck": recheck,
                    "recovery": "unverified"}

        def issue_transition(request, notice_id: str, action: str):
            if not self.setup.authorized(request):
                return JSONResponse({"error": "control authorization required"}, status_code=403)
            if action not in {"acknowledge", "dismiss"}:
                return JSONResponse({"error": "invalid issue action"}, status_code=400)
            state = "acknowledged" if action == "acknowledge" else "dismissed"
            try:
                changed = IssueNotices(self.storage_url).transition(
                    self.tenant_id, notice_id, state,
                )
            except ValueError:
                return JSONResponse({"error": "invalid issue identity"}, status_code=400)
            if not changed:
                return JSONResponse({"error": "issue already reviewed or unavailable"}, status_code=409)
            return {"noticeId": notice_id, "state": state}

        issue_transition.__annotations__["request"] = Request
        app.post("/api/control/issues/{notice_id}/{action}")(issue_transition)
        # Starlette matches routes in registration order. Keep literal actions
        # ahead of the parameterized document endpoint.
        app.post("/api/control/{kind}/{document_id}")(control_put)

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

    def _notifications(self, writable) -> list[dict[str, object]]:
        notifications: list[dict[str, object]] = []
        active = writable.get_active_monitor_policy(self.scope)
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
        return notifications

    def _run_monitor(self, writable, policy):
        if policy is None:
            return None
        manifest, comparison = self.monitor.prospective(writable, policy)
        return self.monitor.response(
            policy, "active", manifest, comparison,
            approved_historical=writable.get_initial_monitor_snapshot(policy.policy_id),
        )
