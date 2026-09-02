"""Monitor policy lifecycle routes for the local dashboard."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from verdict.dashboard.setup_routes import SetupRoutes
from verdict.monitoring import (
    MonitorPolicy,
    WindowMode,
    compare_manifest,
    monitor_policy_to_json,
    monitor_snapshot_to_json,
    plan_historical_manifest,
    plan_prospective_manifest,
    trace_monitor_units,
)

TENANT = "__verdict_local__"
SCOPE = "__verdict_local__:application:trace"


class MonitorRoutes:
    """Own monitor policy parsing, bounded units, and lifecycle endpoints."""

    def __init__(self, setup: SetupRoutes) -> None:
        self.setup = setup

    @staticmethod
    def policy(payload: dict[str, Any], policy_id: str) -> MonitorPolicy:
        mode = WindowMode(payload.get("windowMode", "count"))
        values: dict[str, Any] = {
            "policy_id": policy_id,
            "scope_key": SCOPE,
            "window_mode": mode,
            "reference_ratio": float(payload.get("referenceRatio", 0.8)),
            "minimum_reference": int(payload.get("minimumReference", 30)),
            "minimum_current": int(payload.get("minimumCurrent", 30)),
            "prospective_target": int(payload.get("prospectiveTarget", 30)),
            "p_threshold": float(payload.get("pThreshold", 0.05)),
            "minimum_effect": float(payload.get("minimumEffect", 0.1)),
            "maximum_unseen_group_share": float(payload.get("maximumUnseenShare", 0.2)),
            "analysis_unit": payload.get("analysisUnit", "trace"),
            "grouping_mode": payload.get("groupingMode", "none"),
        }
        if mode is WindowMode.EXPLICIT:
            for source, target in (
                ("referenceStart", "reference_start"),
                ("referenceEnd", "reference_end"),
                ("currentStart", "current_start"),
                ("currentEnd", "current_end"),
            ):
                value = payload[source]
                if not isinstance(value, str):
                    raise ValueError("explicit window boundary must be text")
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                values[target] = (
                    parsed.replace(tzinfo=timezone.utc)
                    if parsed.tzinfo is None
                    else parsed.astimezone(timezone.utc)
                )
        return MonitorPolicy(**values)

    @staticmethod
    def response(
        policy, state, manifest, comparison, *, approved_historical=None,
    ) -> dict[str, object]:
        result = {
            "policy": json.loads(monitor_policy_to_json(policy)),
            "state": state,
            "snapshot": json.loads(monitor_snapshot_to_json(manifest, comparison)),
        }
        if approved_historical is not None:
            result["approvedHistoricalSnapshot"] = json.loads(
                monitor_snapshot_to_json(*approved_historical)
            )
        return result

    @staticmethod
    def bounded_units(writable, policy):
        traces = writable.list_traces(limit=100_001)
        if len(traces) > 100_000:
            raise ValueError("monitor exceeds bounded trace limit")
        assignments = None
        if policy.grouping_mode == "cluster":
            pointer = writable.get_active_cluster_registry(TENANT)
            if pointer.version_id is None:
                raise ValueError("cluster grouping requires an active registry")
            rows = writable.list_trace_cluster_assignments(
                TENANT, pointer.version_id, limit=100_001
            )
            if len(rows) > 100_000:
                raise ValueError("cluster assignment limit exceeded")
            assignments = {
                row.trace_id: row.cluster_id
                for row in rows
                if row.status == "assigned" and row.cluster_id is not None
            }
        return trace_monitor_units(
            traces,
            grouping_mode=policy.grouping_mode,
            cluster_assignments=assignments,
        )

    def prospective(self, writable, policy, previous_manifest):
        units = self.bounded_units(writable, policy)
        manifest = plan_prospective_manifest(previous_manifest, units, policy)
        return manifest, compare_manifest(units, manifest, policy)

    def register(self, app) -> None:
        def monitor_preview(request, payload: dict[str, Any]):
            if not self.setup.authorized(request):
                return JSONResponse(
                    {"error": "monitor authorization required"}, status_code=403
                )
            writable = None
            try:
                policy = self.policy(payload, f"policy-{secrets.token_hex(12)}")
                writable = self.setup.writable_storage()
                units = self.bounded_units(writable, policy)
                cutoff = max(
                    (unit.event_time for unit in units),
                    default=datetime.now(timezone.utc),
                )
                manifest = plan_historical_manifest(units, policy, cutoff=cutoff)
                comparison = compare_manifest(units, manifest, policy)
                writable.save_monitor_policy(policy)
                writable.save_monitor_snapshot(policy.policy_id, manifest, comparison)
                return self.response(policy, "candidate", manifest, comparison)
            except (KeyError, OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse({"error": "invalid monitor request"}, status_code=400)
            finally:
                if writable is not None:
                    writable.close()

        monitor_preview.__annotations__["request"] = Request
        app.post("/api/monitor/preview")(monitor_preview)

        def monitor_activate(request, payload: dict[str, Any]):
            if not self.setup.authorized(request):
                return JSONResponse(
                    {"error": "monitor authorization required"}, status_code=403
                )
            writable = None
            try:
                policy_id = payload.get("policyId")
                expected = payload.get("expectedActivePolicyId")
                if not isinstance(policy_id, str) or (
                    expected is not None and not isinstance(expected, str)
                ):
                    raise ValueError("invalid activation")
                writable = self.setup.writable_storage()
                stored = writable.get_monitor_policy(policy_id)
                if stored is None or stored[1] != "candidate":
                    raise ValueError("unknown policy")
                historical = writable.get_latest_monitor_snapshot(policy_id)
                if historical is None:
                    raise ValueError("candidate has no snapshot")
                policy = writable.activate_monitor_policy(
                    stored[0].scope_key,
                    policy_id,
                    expected_active_policy_id=expected,
                )
                manifest, comparison = self.prospective(
                    writable, policy, historical[0]
                )
                writable.save_monitor_snapshot(policy_id, manifest, comparison)
                return self.response(
                    policy, "active", manifest, comparison,
                    approved_historical=historical,
                )
            except (OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse(
                    {"error": "invalid monitor activation"}, status_code=400
                )
            finally:
                if writable is not None:
                    writable.close()

        monitor_activate.__annotations__["request"] = Request
        app.post("/api/monitor/activate")(monitor_activate)

        def monitor_run(request):
            if not self.setup.authorized(request):
                return JSONResponse(
                    {"error": "monitor authorization required"}, status_code=403
                )
            writable = None
            try:
                writable = self.setup.writable_storage()
                policy = writable.get_active_monitor_policy(SCOPE)
                if policy is None:
                    return JSONResponse({"error": "no active monitor"}, status_code=409)
                previous = writable.get_latest_monitor_snapshot(policy.policy_id)
                if previous is None:
                    raise ValueError("active monitor has no snapshot")
                manifest, comparison = self.prospective(writable, policy, previous[0])
                writable.save_monitor_snapshot(policy.policy_id, manifest, comparison)
                return self.response(
                    policy, "active", manifest, comparison,
                    approved_historical=writable.get_initial_monitor_snapshot(
                        policy.policy_id
                    ),
                )
            except (OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse({"error": "monitor run unavailable"}, status_code=400)
            finally:
                if writable is not None:
                    writable.close()

        monitor_run.__annotations__["request"] = Request
        app.post("/api/monitor/run")(monitor_run)

        @app.get("/api/monitor")
        def monitor_state():
            writable = self.setup.writable_storage()
            try:
                policy = writable.get_active_monitor_policy(SCOPE)
                if policy is None:
                    return {"state": "not_configured"}
                snapshot = writable.get_latest_monitor_snapshot(policy.policy_id)
                return (
                    self.response(
                        policy, "active", *snapshot,
                        approved_historical=writable.get_initial_monitor_snapshot(
                            policy.policy_id
                        ),
                    )
                    if snapshot
                    else {"state": "active_without_snapshot"}
                )
            finally:
                writable.close()
