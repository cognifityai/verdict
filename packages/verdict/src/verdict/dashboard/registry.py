"""Bounded read model for the Task 5 cluster-registry dashboard.

The reader owns no registry state and performs no migrations or writes.  It
projects one authorized tenant and one selected immutable version through the
same SQLite/PostgreSQL query-session seam as the packaged dashboard.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

MAX_REGISTRY_VERSIONS = 10
MAX_REGISTRY_CLUSTERS = 250
MAX_REGISTRY_ASSIGNMENTS = 50
MAX_REGISTRY_EVENTS = 20
MAX_DETAILED_CLUSTERS = 20
MAX_REPRESENTATIVES_PER_CLUSTER = 3
MAX_MODELS_PER_CLUSTER = 5
MAX_REGISTRY_MODELS = 20
READINESS_CONVERSATION_FLOOR = 30
_DAY_US = 86_400_000_000


class RegistryNotFoundError(LookupError):
    """The requested version does not belong to the authorized tenant."""


class RegistryStateError(RuntimeError):
    """Persisted registry state cannot be rendered safely."""


class QueryResult(Protocol):
    def fetchone(self) -> Mapping[str, Any] | None: ...

    def __iter__(self) -> Iterator[Mapping[str, Any]]: ...


class QuerySession(Protocol):
    def execute(self, query: str, params: tuple[Any, ...] = ()) -> QueryResult: ...

    def table_exists(self, table: str) -> bool: ...

    def valid_session_predicate(self, trace_alias: str) -> str: ...


def active_cluster_projection(
    session: QuerySession,
    tenant: str,
) -> tuple[dict[str, str], dict[str, str]] | None:
    """Return assigned trace IDs and labels for one authorized active registry."""
    required = (
        "cluster_registries",
        "cluster_registry_versions",
        "cluster_registry_clusters",
        "active_cluster_registry",
        "trace_cluster_assignments",
        "cluster_identities",
        "cluster_registry_events",
    )
    if any(not session.table_exists(table) for table in required):
        return None
    pointer = session.execute(
        "SELECT version_id FROM active_cluster_registry WHERE tenant_id=?",
        (tenant,),
    ).fetchone()
    if pointer is None or pointer.get("version_id") is None:
        return None
    version_id = pointer["version_id"]
    label_rows = session.execute(
        "SELECT c.cluster_id,i.display_name "
        "FROM cluster_registry_clusters c "
        "JOIN cluster_identities i ON i.tenant_id=c.tenant_id "
        "AND i.cluster_id=c.cluster_id AND i.kind=c.kind "
        "WHERE c.tenant_id=? AND c.version_id=?",
        (tenant, version_id),
    )
    labels = {
        str(row["cluster_id"]): str(row["display_name"])
        for row in label_rows
    }
    rows = session.execute(
        "SELECT a.trace_id,a.cluster_id "
        "FROM trace_cluster_assignments a "
        "JOIN traces t ON t.trace_id=a.trace_id "
        "JOIN cluster_registry_clusters c ON c.tenant_id=a.tenant_id "
        "AND c.version_id=a.version_id AND c.cluster_id=a.cluster_id "
        "AND c.kind=a.cluster_kind "
        "WHERE a.tenant_id=? AND a.version_id=? AND a.status='assigned' "
        f"AND {_trace_tenant_predicate()}",
        (tenant, version_id, tenant),
    )
    assignments: dict[str, str] = {}
    for row in rows:
        trace_id = str(row["trace_id"])
        cluster_id = str(row["cluster_id"])
        assignments[trace_id] = cluster_id
    return assignments, labels


def _trace_tenant_predicate() -> str:
    return (
        "(t.tenant_id=a.tenant_id OR "
        "(?='__verdict_local__' AND a.tenant_id='__verdict_local__' "
        "AND t.tenant_id IS NULL))"
    )


def _json_object(value: object, field: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise RegistryStateError(f"invalid {field}") from exc
    if not isinstance(decoded, dict):
        raise RegistryStateError(f"invalid {field}")
    return decoded


def _bounded_scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:256]
    return None


def _bounded_object(value: object, *, limit: int = 32) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    for key in sorted(value, key=str)[:limit]:
        if not isinstance(key, str):
            continue
        item = value[key]
        if isinstance(item, dict):
            result[key[:80]] = _bounded_object(item, limit=limit)
        elif isinstance(item, list):
            result[key[:80]] = [
                scalar for entry in item[:limit] if (scalar := _bounded_scalar(entry)) is not None
            ]
        else:
            result[key[:80]] = _bounded_scalar(item)
    return result


def _strategy_status(strategy: str) -> dict[str, object]:
    if strategy == "explicit":
        return {
            "strategy": strategy,
            "experimental": False,
            "semantic_component": "none",
        }
    if strategy == "semantic":
        return {
            "strategy": strategy,
            "experimental": True,
            "semantic_component": "automatic",
        }
    if strategy == "hybrid":
        return {
            "strategy": strategy,
            "experimental": True,
            "semantic_component": "fallback",
        }
    raise RegistryStateError("invalid registry strategy")


def _utc_us(value: object) -> int:
    if not isinstance(value, str):
        raise RegistryStateError("invalid registry cutoff")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistryStateError("invalid registry cutoff") from exc
    if parsed.tzinfo is None:
        raise RegistryStateError("invalid registry cutoff")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _preview(value: object) -> dict[str, object]:
    raw = _json_object(value, "preview report")
    return {
        "candidateCount": _bounded_scalar(raw.get("candidate_count")),
        "fitAssignmentCount": _bounded_scalar(raw.get("fit_assignment_count")),
        "clusterCount": _bounded_scalar(raw.get("cluster_count")),
        "explicitClusterCount": _bounded_scalar(raw.get("explicit_cluster_count")),
        "semanticClusterCount": _bounded_scalar(raw.get("semantic_cluster_count")),
        "chosenK": _bounded_scalar(raw.get("chosen_k")),
        "statuses": _bounded_object(raw.get("statuses")),
        "metrics": _bounded_object(raw.get("metrics")),
        "warnings": [item[:160] for item in raw.get("warnings", [])[:20] if isinstance(item, str)]
        if isinstance(raw.get("warnings"), list)
        else [],
        "candidateSummary": _bounded_object(raw.get("candidate_summary")),
    }


def _version(
    row: Mapping[str, Any],
    *,
    active_version_id: str | None,
) -> dict[str, object]:
    strategy = str(row["strategy"])
    definition = _json_object(row["fit_definition_json"], "fit definition")
    return {
        "versionId": str(row["version_id"]),
        "parentVersionId": row.get("parent_version_id"),
        "strategy": strategy,
        "strategyStatus": _strategy_status(strategy),
        "active": row["version_id"] == active_version_id,
        "cutoff": row["cutoff"],
        "lookbackDays": int(row["lookback_days"]),
        "createdAt": row["created_at"],
        "createdBy": str(row["created_by"]),
        "configuration": _bounded_object(definition.get("config")),
        "algorithm": _bounded_scalar(definition.get("algorithm")),
        "selector": _bounded_scalar(definition.get("selector")),
        "model": _bounded_object(definition.get("model")),
        "preview": _preview(row["preview_report_json"]),
    }


def _event(row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "eventId": str(row["event_id"]),
        "action": str(row["action"]),
        "fromVersionId": row.get("from_version_id"),
        "toVersionId": row.get("to_version_id"),
        "pointerGeneration": row.get("pointer_generation"),
        "createdAt": row["created_at"],
        "actor": str(row["actor"]),
        "details": _bounded_object(_json_object(row["details_json"], "event details")),
    }


def build_registry_bundle(
    session: QuerySession,
    *,
    tenant: str,
    version_id: str | None = None,
    assignment_limit: int = 50,
    assignment_offset: int = 0,
) -> dict[str, object]:
    """Build one bounded authorized registry view from a read transaction."""
    if not 1 <= assignment_limit <= MAX_REGISTRY_ASSIGNMENTS:
        raise ValueError("assignment limit is outside the dashboard bound")
    if assignment_offset < 0:
        raise ValueError("assignment offset must be nonnegative")
    if not session.table_exists("cluster_registry_versions"):
        return {
            "schema": "cluster-registry-dashboard-v1",
            "tenant": tenant,
            "status": "unavailable",
            "reason": "registry_not_installed",
        }

    pointer = session.execute(
        "SELECT version_id,generation,activated_at,activated_by "
        "FROM active_cluster_registry WHERE tenant_id=?",
        (tenant,),
    ).fetchone()
    active_version_id = str(pointer["version_id"]) if pointer and pointer["version_id"] else None
    version_rows = list(
        session.execute(
            "SELECT * FROM cluster_registry_versions WHERE tenant_id=? "
            "ORDER BY created_at DESC,version_id DESC LIMIT ?",
            (tenant, MAX_REGISTRY_VERSIONS + 1),
        )
    )
    if not version_rows:
        return {
            "schema": "cluster-registry-dashboard-v1",
            "tenant": tenant,
            "status": "empty",
            "active": {
                "versionId": active_version_id,
                "generation": int(pointer["generation"]) if pointer else 0,
                "activatedAt": pointer.get("activated_at") if pointer else None,
                "activatedBy": pointer.get("activated_by") if pointer else None,
            },
            "versions": [],
        }

    selected_row: Mapping[str, Any] | None = None
    selected_id = version_id or active_version_id or str(version_rows[0]["version_id"])
    for row in version_rows:
        if row["version_id"] == selected_id:
            selected_row = row
            break
    if selected_row is None:
        selected_row = session.execute(
            "SELECT * FROM cluster_registry_versions WHERE tenant_id=? AND version_id=?",
            (tenant, selected_id),
        ).fetchone()
    if selected_row is None:
        raise RegistryNotFoundError("registry version not found")

    selected_version = _version(selected_row, active_version_id=active_version_id)
    visible_version_rows = version_rows[:MAX_REGISTRY_VERSIONS]
    active_row = next(
        (row for row in visible_version_rows if row["version_id"] == active_version_id),
        None,
    )
    if active_version_id is not None and active_row is None:
        active_row = session.execute(
            "SELECT * FROM cluster_registry_versions WHERE tenant_id=? AND version_id=?",
            (tenant, active_version_id),
        ).fetchone()
    for required_row in (active_row, selected_row):
        if required_row is None or any(
            row["version_id"] == required_row["version_id"] for row in visible_version_rows
        ):
            continue
        if len(visible_version_rows) >= MAX_REGISTRY_VERSIONS:
            visible_version_rows.pop()
        visible_version_rows.append(required_row)
    versions = [_version(row, active_version_id=active_version_id) for row in visible_version_rows]

    event_rows = list(
        session.execute(
            "SELECT * FROM cluster_registry_events "
            "WHERE tenant_id=? AND to_version_id=? "
            "ORDER BY created_at DESC,event_id DESC LIMIT ?",
            (tenant, selected_id, MAX_REGISTRY_EVENTS),
        )
    )
    events = [_event(row) for row in event_rows]
    validation_row = session.execute(
        "SELECT * FROM cluster_registry_events "
        "WHERE tenant_id=? AND to_version_id=? "
        "AND action IN ('validated','validation_failed') "
        "ORDER BY created_at DESC,event_id DESC LIMIT 1",
        (tenant, selected_id),
    ).fetchone()
    validation = _event(validation_row) if validation_row else None
    activation_history = bool(
        session.execute(
            "SELECT 1 AS present FROM cluster_registry_events "
            "WHERE tenant_id=? AND to_version_id=? "
            "AND action IN ('activated','rolled_back') LIMIT 1",
            (tenant, selected_id),
        ).fetchone()
    )
    details = validation["details"] if validation else {}
    readiness = {
        "status": validation["action"] if validation else "unvalidated",
        "passed": validation["action"] == "validated" if validation else False,
        "coverage": details.get("coverage") if isinstance(details, dict) else None,
        "structural": details.get("structural") if isinstance(details, dict) else None,
        "definition": details.get("definition") if isinstance(details, dict) else None,
        "model": details.get("model") if isinstance(details, dict) else None,
    }

    cluster_rows = list(
        session.execute(
            "SELECT c.cluster_id,c.kind,c.radius,c.member_count,c.outlier_count,"
            "i.display_name,i.lifecycle,i.explicit_key,"
            "SUM(CASE WHEN a.status='assigned' THEN 1 ELSE 0 END) AS assigned_count "
            "FROM cluster_registry_clusters c "
            "JOIN cluster_identities i ON i.tenant_id=c.tenant_id "
            "AND i.cluster_id=c.cluster_id "
            "LEFT JOIN trace_cluster_assignments a ON a.tenant_id=c.tenant_id "
            "AND a.version_id=c.version_id AND a.cluster_id=c.cluster_id "
            "WHERE c.tenant_id=? AND c.version_id=? "
            "GROUP BY c.cluster_id,c.kind,c.radius,c.member_count,c.outlier_count,"
            "i.display_name,i.lifecycle,i.explicit_key "
            "ORDER BY assigned_count DESC,c.cluster_id LIMIT ?",
            (tenant, selected_id, MAX_REGISTRY_CLUSTERS + 1),
        )
    )
    if len(cluster_rows) > MAX_REGISTRY_CLUSTERS:
        raise RegistryStateError("registry cluster count exceeds dashboard bound")
    clusters = [
        {
            "clusterId": str(row["cluster_id"]),
            "displayName": str(row["display_name"]),
            "kind": str(row["kind"]),
            "lifecycle": str(row["lifecycle"]),
            "explicitKey": row.get("explicit_key"),
            "radius": float(row["radius"]) if row.get("radius") is not None else None,
            "memberCount": int(row["member_count"]),
            "outlierCount": int(row["outlier_count"]),
            "assignedCount": int(row["assigned_count"] or 0),
        }
        for row in cluster_rows
    ]

    cluster_by_id = {str(cluster["clusterId"]): cluster for cluster in clusters}
    for index, cluster in enumerate(clusters):
        cluster["detailsAvailable"] = index < MAX_DETAILED_CLUSTERS
    detail_cluster_by_id = {
        cluster_id: cluster
        for cluster_id, cluster in cluster_by_id.items()
        if cluster["detailsAvailable"]
    }
    for cluster in detail_cluster_by_id.values():
        cluster["representatives"] = []
        cluster["representativesTruncated"] = False
        cluster["modelDistribution"] = []
        cluster["modelDistributionTruncated"] = False
        cluster["warnings"] = []

    representative_rows = session.execute(
        "WITH ranked AS ("
        "SELECT a.cluster_id,t.trace_id,"
        "SUBSTR(t.prompt_redacted,1,240) AS prompt_preview,"
        "SUBSTR(COALESCE(NULLIF(t.provider,''),'unknown'),1,80) AS provider,"
        "SUBSTR(COALESCE(NULLIF(t.response_model,''),NULLIF(t.request_model,''),'unknown'),"
        "1,160) AS model,"
        "ROW_NUMBER() OVER (PARTITION BY a.cluster_id "
        "ORDER BY t.analysis_started_at_us DESC,t.trace_id) AS representative_rank "
        "FROM trace_cluster_assignments a JOIN traces t ON t.trace_id=a.trace_id "
        "WHERE a.tenant_id=? AND a.version_id=? AND a.status='assigned' "
        f"AND {_trace_tenant_predicate()} "
        "AND t.prompt_redacted IS NOT NULL AND t.prompt_redacted<>'') "
        "SELECT cluster_id,trace_id,prompt_preview,provider,model "
        "FROM ranked WHERE representative_rank<=? "
        "ORDER BY cluster_id,representative_rank LIMIT ?",
        (
            tenant,
            selected_id,
            tenant,
            MAX_REPRESENTATIVES_PER_CLUSTER + 1,
            MAX_REGISTRY_CLUSTERS * (MAX_REPRESENTATIVES_PER_CLUSTER + 1),
        ),
    )
    for row in representative_rows:
        cluster = detail_cluster_by_id.get(str(row["cluster_id"]))
        if cluster is None:
            continue
        representatives = cluster["representatives"]
        if not isinstance(representatives, list):
            raise RegistryStateError("invalid representative collection")
        if len(representatives) >= MAX_REPRESENTATIVES_PER_CLUSTER:
            cluster["representativesTruncated"] = True
            continue
        representatives.append(
            {
                "traceId": str(row["trace_id"]),
                "prompt": str(row["prompt_preview"]),
                "provider": str(row["provider"]),
                "model": str(row["model"]),
            }
        )

    distribution_rows = session.execute(
        "WITH distribution AS ("
        "SELECT a.cluster_id,"
        "SUBSTR(COALESCE(NULLIF(t.provider,''),'unknown'),1,80) AS provider,"
        "SUBSTR(COALESCE(NULLIF(t.response_model,''),NULLIF(t.request_model,''),'unknown'),"
        "1,160) AS model,COUNT(*) AS n "
        "FROM trace_cluster_assignments a JOIN traces t ON t.trace_id=a.trace_id "
        "WHERE a.tenant_id=? AND a.version_id=? AND a.status='assigned' "
        f"AND {_trace_tenant_predicate()} "
        "GROUP BY a.cluster_id,provider,model), ranked AS ("
        "SELECT cluster_id,provider,model,n,"
        "ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY n DESC,provider,model) "
        "AS model_rank FROM distribution) "
        "SELECT cluster_id,provider,model,n FROM ranked WHERE model_rank<=? "
        "ORDER BY cluster_id,n DESC,provider,model LIMIT ?",
        (
            tenant,
            selected_id,
            tenant,
            MAX_MODELS_PER_CLUSTER + 1,
            MAX_REGISTRY_CLUSTERS * (MAX_MODELS_PER_CLUSTER + 1),
        ),
    )
    for row in distribution_rows:
        cluster = detail_cluster_by_id.get(str(row["cluster_id"]))
        if cluster is None:
            continue
        entry = {
            "provider": str(row["provider"]),
            "model": str(row["model"]),
            "count": int(row["n"]),
        }
        model_distribution = cluster["modelDistribution"]
        if not isinstance(model_distribution, list):
            raise RegistryStateError("invalid model distribution")
        if len(model_distribution) >= MAX_MODELS_PER_CLUSTER:
            cluster["modelDistributionTruncated"] = True
            continue
        model_distribution.append(entry)

    model_distribution_rows = list(
        session.execute(
            "SELECT SUBSTR(COALESCE(NULLIF(t.provider,''),'unknown'),1,80) AS provider,"
            "SUBSTR(COALESCE(NULLIF(t.response_model,''),NULLIF(t.request_model,''),"
            "'unknown'),1,160) AS model,COUNT(*) AS n "
            "FROM trace_cluster_assignments a JOIN traces t ON t.trace_id=a.trace_id "
            "WHERE a.tenant_id=? AND a.version_id=? AND a.status='assigned' "
            f"AND {_trace_tenant_predicate()} "
            "GROUP BY provider,model ORDER BY n DESC,provider,model LIMIT ?",
            (tenant, selected_id, tenant, MAX_REGISTRY_MODELS + 1),
        )
    )
    model_distribution = [
        {
            "provider": str(row["provider"]),
            "model": str(row["model"]),
            "count": int(row["n"]),
        }
        for row in model_distribution_rows[:MAX_REGISTRY_MODELS]
    ]

    cutoff_us = _utc_us(selected_version["cutoff"])
    current_start_us = cutoff_us - _DAY_US
    baseline_end_us = current_start_us
    baseline_start_us = baseline_end_us - (7 * _DAY_US)
    valid_session = session.valid_session_predicate("t")
    traffic_rows = session.execute(
        "SELECT a.cluster_id,"
        f"COUNT(DISTINCT CASE WHEN {valid_session} "
        "AND t.analysis_started_at_us>=? AND t.analysis_started_at_us<? "
        "THEN t.session_id END) AS baseline_conversations,"
        f"COUNT(DISTINCT CASE WHEN {valid_session} "
        "AND t.analysis_started_at_us>=? AND t.analysis_started_at_us<? "
        "THEN t.session_id END) AS current_conversations "
        "FROM trace_cluster_assignments a JOIN traces t ON t.trace_id=a.trace_id "
        "WHERE a.tenant_id=? AND a.version_id=? AND a.status='assigned' "
        f"AND {_trace_tenant_predicate()} "
        "AND t.analysis_started_at_state='valid' "
        "GROUP BY a.cluster_id ORDER BY a.cluster_id LIMIT ?",
        (
            baseline_start_us,
            baseline_end_us,
            current_start_us,
            cutoff_us,
            tenant,
            selected_id,
            tenant,
            MAX_REGISTRY_CLUSTERS,
        ),
    )
    for row in traffic_rows:
        cluster = detail_cluster_by_id.get(str(row["cluster_id"]))
        if cluster is None:
            continue
        baseline = int(row["baseline_conversations"] or 0)
        current = int(row["current_conversations"] or 0)
        remaining_baseline = max(0, READINESS_CONVERSATION_FLOOR - baseline)
        remaining_current = max(0, READINESS_CONVERSATION_FLOOR - current)
        observed_rate = max(current, baseline / 7)
        estimated_days = (
            math.ceil(max(remaining_baseline, remaining_current) / observed_rate)
            if observed_rate > 0
            else None
        )
        cluster["conversationReadiness"] = {
            "status": (
                "ready"
                if remaining_baseline == 0 and remaining_current == 0
                else "collecting"
                if baseline + current > 0
                else "unavailable"
            ),
            "floor": READINESS_CONVERSATION_FLOOR,
            "baseline": baseline,
            "current": current,
            "remainingBaseline": remaining_baseline,
            "remainingCurrent": remaining_current,
            "estimatedDaysToReady": estimated_days,
        }
    for cluster in detail_cluster_by_id.values():
        if "conversationReadiness" not in cluster:
            cluster["conversationReadiness"] = {
                "status": "unavailable",
                "floor": READINESS_CONVERSATION_FLOOR,
                "baseline": 0,
                "current": 0,
                "remainingBaseline": READINESS_CONVERSATION_FLOOR,
                "remainingCurrent": READINESS_CONVERSATION_FLOOR,
                "estimatedDaysToReady": None,
            }

    semantic_clusters = [cluster for cluster in clusters if cluster["kind"] == "semantic"]
    health_warnings: list[str] = []
    semantic_assigned = sum(int(cluster["assignedCount"]) for cluster in semantic_clusters)
    if semantic_assigned > 0 and len(semantic_clusters) >= 4:
        if len(semantic_clusters) / semantic_assigned > 0.5:
            health_warnings.append("fragmented_semantic_space")
    if semantic_assigned > 0:
        for cluster in semantic_clusters:
            if int(cluster["assignedCount"]) / semantic_assigned > 0.30:
                if cluster["detailsAvailable"]:
                    warnings = cluster["warnings"]
                    if not isinstance(warnings, list):
                        raise RegistryStateError("invalid cluster warnings")
                    warnings.append("oversized_semantic_cluster")
                if "oversized_semantic_cluster" not in health_warnings:
                    health_warnings.append("oversized_semantic_cluster")

    status_rows = list(
        session.execute(
            "SELECT status,reason,COUNT(*) AS n FROM trace_cluster_assignments "
            "WHERE tenant_id=? AND version_id=? GROUP BY status,reason "
            "ORDER BY status,reason",
            (tenant, selected_id),
        )
    )
    counts = {"assigned": 0, "outlier": 0, "ineligible": 0}
    reasons = []
    for row in status_rows:
        status = str(row["status"])
        count = int(row["n"])
        if status not in counts:
            raise RegistryStateError("invalid assignment status")
        counts[status] += count
        if row.get("reason") is not None:
            reasons.append({"status": status, "reason": str(row["reason"]), "count": count})
    counts["total"] = sum(counts.values())

    assignment_rows = list(
        session.execute(
            "SELECT trace_id,origin,status,cluster_id,cluster_kind,reason,distance,assigned_at "
            "FROM trace_cluster_assignments WHERE tenant_id=? AND version_id=? "
            "ORDER BY status,cluster_id,trace_id LIMIT ? OFFSET ?",
            (tenant, selected_id, assignment_limit + 1, assignment_offset),
        )
    )
    assignments = [
        {
            "traceId": str(row["trace_id"]),
            "origin": str(row["origin"]),
            "status": str(row["status"]),
            "clusterId": row.get("cluster_id"),
            "clusterKind": row.get("cluster_kind"),
            "reason": row.get("reason"),
            "distance": float(row["distance"]) if row.get("distance") is not None else None,
            "assignedAt": row["assigned_at"],
        }
        for row in assignment_rows[:assignment_limit]
    ]
    return {
        "schema": "cluster-registry-dashboard-v1",
        "tenant": tenant,
        "status": "ready",
        "active": {
            "versionId": active_version_id,
            "generation": int(pointer["generation"]) if pointer else 0,
            "activatedAt": pointer.get("activated_at") if pointer else None,
            "activatedBy": pointer.get("activated_by") if pointer else None,
        },
        "versions": versions,
        "versionsTruncated": len(version_rows) > MAX_REGISTRY_VERSIONS,
        "selectedVersion": selected_version,
        "readiness": readiness,
        "activationHistory": activation_history,
        "counts": counts,
        "modelDistribution": model_distribution,
        "modelDistributionTruncated": len(model_distribution_rows) > MAX_REGISTRY_MODELS,
        "trafficWindow": {
            "cutoff": selected_version["cutoff"],
            "baselineDays": 7,
            "gapDays": 1,
            "currentDays": 1,
            "conversationFloor": READINESS_CONVERSATION_FLOOR,
            "diagnosticOnly": True,
        },
        "healthWarnings": health_warnings,
        "clusters": clusters,
        "clusterDetailsTruncated": len(clusters) > MAX_DETAILED_CLUSTERS,
        "assignments": assignments,
        "reasons": reasons,
        "events": events,
        "page": {
            "limit": assignment_limit,
            "offset": assignment_offset,
            "shown": len(assignments),
            "available": counts["total"],
            "truncated": len(assignment_rows) > assignment_limit,
        },
    }
