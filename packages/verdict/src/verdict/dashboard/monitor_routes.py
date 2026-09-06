"""Monitor policy lifecycle routes for the local dashboard."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from verdict.dashboard.setup_routes import SetupRoutes
from verdict.monitor_inputs import (
    LOCAL_TENANT,
    LOCAL_TRACE_SCOPE,
    advance_monitor,
    load_monitor_units,
    select_monitor_evaluator,
)
from verdict.monitoring import (
    MonitorPolicy,
    WindowMode,
    compare_manifest,
    monitor_policy_to_json,
    monitor_requires_rebootstrap,
    monitor_snapshot_to_json,
    plan_historical_manifest,
)

TENANT = LOCAL_TENANT
SCOPE = LOCAL_TRACE_SCOPE

_BOUNDED_MONITOR_ERRORS = {
    "monitor grouping exceeds 250 groups": (
        "Monitor supports at most 250 groups. Choose no grouping or reduce the "
        "number of provider/model or cluster groups."
    ),
    "monitor grouping produces too many metric cells": (
        "This monitor has too many group and metric combinations. Reduce its "
        "groups or evaluator dimensions."
    ),
}


def _error_response(exc: Exception, fallback: str) -> JSONResponse:
    message = _BOUNDED_MONITOR_ERRORS.get(str(exc), fallback)
    return JSONResponse({"error": message}, status_code=400)


class MonitorRoutes:
    """Own monitor policy parsing, bounded units, and lifecycle endpoints."""

    def __init__(self, setup: SetupRoutes) -> None:
        self.setup = setup

    @staticmethod
    def policy(
        payload: dict[str, Any],
        policy_id: str,
        *,
        evaluator_fingerprint: str | None = None,
        evaluator_dimensions: tuple[str, ...] = (),
        cluster_registry_version_id: str | None = None,
    ) -> MonitorPolicy:
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
            "evaluator_fingerprint": evaluator_fingerprint,
            "evaluator_dimensions": evaluator_dimensions,
            "cluster_registry_version_id": cluster_registry_version_id,
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
    def cluster_registry_selection(writable, payload: dict[str, Any]):
        if payload.get("groupingMode", "none") != "cluster":
            return None
        active = writable.get_active_cluster_registry(TENANT)
        if active is None or active.version_id is None:
            raise ValueError("cluster grouping requires an active registry")
        return active.version_id

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
        if state == "requires_rebootstrap":
            result["rebootstrapRequired"] = True
            result["rebootstrapReason"] = (
                "This monitor predates immutable cohort evidence. Preview and "
                "activate a replacement before running it again."
            )
        return result

    @staticmethod
    def bounded_units(writable, policy):
        return load_monitor_units(writable, policy, tenant_id=TENANT)

    @staticmethod
    def prospective(writable, policy):
        return advance_monitor(writable, policy, tenant_id=TENANT)

    def register(self, app) -> None:
        def monitor_preview(request, payload: dict[str, Any]):
            if not self.setup.authorized(request):
                return JSONResponse(
                    {"error": "monitor authorization required"}, status_code=403
                )
            writable = None
            try:
                writable = self.setup.writable_storage()
                fingerprint, dimensions = select_monitor_evaluator(
                    writable,
                    tenant_id=TENANT,
                    evaluator_fingerprint=payload.get("evaluatorFingerprint"),
                )
                cluster_version = self.cluster_registry_selection(writable, payload)
                policy = self.policy(
                    payload,
                    f"policy-{secrets.token_hex(12)}",
                    evaluator_fingerprint=fingerprint,
                    evaluator_dimensions=dimensions,
                    cluster_registry_version_id=cluster_version,
                )
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
            except (KeyError, OSError, TypeError, UnicodeError, ValueError) as exc:
                return _error_response(exc, "invalid monitor request")
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
                if monitor_requires_rebootstrap(stored[0], historical[0]):
                    return JSONResponse(
                        {"error": "monitor requires re-bootstrap"},
                        status_code=409,
                    )
                policy = writable.activate_monitor_policy(
                    stored[0].scope_key,
                    policy_id,
                    expected_active_policy_id=expected,
                )
                manifest, comparison = self.prospective(writable, policy)
                return self.response(
                    policy, "active", manifest, comparison,
                    approved_historical=historical,
                )
            except (OSError, TypeError, UnicodeError, ValueError) as exc:
                return _error_response(exc, "invalid monitor activation")
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
                if monitor_requires_rebootstrap(policy, previous[0]):
                    return JSONResponse(
                        {"error": "monitor requires re-bootstrap"},
                        status_code=409,
                    )
                manifest, comparison = self.prospective(writable, policy)
                return self.response(
                    policy, "active", manifest, comparison,
                    approved_historical=writable.get_initial_monitor_snapshot(
                        policy.policy_id
                    ),
                )
            except (OSError, TypeError, UnicodeError, ValueError) as exc:
                return _error_response(exc, "monitor run unavailable")
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
                if snapshot and monitor_requires_rebootstrap(policy, snapshot[0]):
                    return self.response(
                        policy,
                        "requires_rebootstrap",
                        *snapshot,
                        approved_historical=writable.get_initial_monitor_snapshot(policy.policy_id),
                    )
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
