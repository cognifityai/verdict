"""Semantic registry orchestration for count-cohort monitoring."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

from verdict.schema import Trace, TraceClusterAssignment

from verdict_eval.cluster_registry import ClusterRegistryService
from verdict_eval.clustering_strategies import FitConfig
from verdict_eval.count_monitor import plan_history, scope_for_trace

LOCAL_REGISTRY_TENANT = "__verdict_local__"


@dataclass(frozen=True, slots=True)
class SemanticMonitoringState:
    assignments: dict[str, str]
    registry_references: dict[str | None, str]
    version_ids: dict[str, str]
    validation_reports: dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class _TenantCohorts:
    baseline_representatives: list[Trace]
    remaining_representatives: list[Trace]
    projection: dict[str, tuple[str, bool]]


def has_active_semantic_registry(storage: object, traces: list[Trace]) -> bool:
    """Return whether every eligible tenant has an active semantic registry."""
    tenants = {
        trace.tenant_id or LOCAL_REGISTRY_TENANT for trace in _eligible_traces(traces)
    }
    if not tenants:
        return False
    active = {
        tenant
        for tenant in tenants
        if storage.get_active_cluster_registry(tenant).version_id is not None
    }
    if active and active != tenants:
        raise ValueError("semantic registry activation is incomplete across tenant scopes")
    return active == tenants


def fit_semantic_bootstrap(
    storage: object,
    traces: list[Trace],
    *,
    embedder: object,
    actor: str,
    config: FitConfig | None = None,
) -> SemanticMonitoringState:
    """Fit and activate one exact older-cohort registry per tenant scope."""
    config = config or FitConfig(strategy="semantic")
    if config.strategy != "semantic":
        raise ValueError("count monitoring requires semantic clustering")
    by_tenant = _cohorts_by_tenant(traces)
    service = ClusterRegistryService(storage, embedder=embedder)
    assignments: dict[str, str] = {}
    references: dict[str | None, str] = {}
    versions: dict[str, str] = {}
    reports: dict[str, dict[str, object]] = {}
    for trace_tenant, cohorts in sorted(
        by_tenant.items(), key=lambda item: item[0] or ""
    ):
        registry_tenant = trace_tenant or LOCAL_REGISTRY_TENANT
        pointer = storage.get_active_cluster_registry(registry_tenant)
        if pointer.version_id is not None:
            raise ValueError("semantic registry is already active; run scheduled assignment")
        version = service.fit_manifest(
            registry_tenant,
            actor=actor,
            strategy="semantic",
            traces=cohorts.baseline_representatives,
            config=config,
        )
        service.assign_manifest(
            registry_tenant,
            version.version_id,
            traces=cohorts.remaining_representatives,
        )
        representative_assignments = {
            item.trace_id: item
            for item in storage.list_trace_cluster_assignments(
                registry_tenant, version.version_id
            )
        }
        _persist_projection(
            storage,
            registry_tenant,
            version.version_id,
            cohorts.projection,
            representative_assignments,
        )
        report = service.validate(registry_tenant, version.version_id, actor=actor)
        if not report["passed"]:
            raise ValueError("semantic cluster registry failed validation")
        service.activate(
            registry_tenant,
            version.version_id,
            expected_generation=pointer.generation,
            actor=actor,
        )
        assignments.update(
            _projected_monitor_assignments(
                cohorts.projection,
                representative_assignments,
            )
        )
        references[trace_tenant] = _registry_reference(registry_tenant, version.version_id)
        versions[registry_tenant] = version.version_id
        reports[registry_tenant] = report
    if not versions:
        raise ValueError("no eligible historical cohorts for semantic clustering")
    return SemanticMonitoringState(assignments, references, versions, reports)


def assign_active_semantic(
    storage: object,
    traces: list[Trace],
    *,
    embedder: object,
) -> SemanticMonitoringState:
    """Assign unseen traffic through each tenant's active frozen registry."""
    service = ClusterRegistryService(storage, embedder=embedder)
    by_tenant: dict[str | None, list[Trace]] = defaultdict(list)
    for trace in _eligible_traces(traces):
        by_tenant[trace.tenant_id].append(trace)
    assignments: dict[str, str] = {}
    references: dict[str | None, str] = {}
    versions: dict[str, str] = {}
    for trace_tenant, rows in sorted(by_tenant.items(), key=lambda item: item[0] or ""):
        registry_tenant = trace_tenant or LOCAL_REGISTRY_TENANT
        pointer = storage.get_active_cluster_registry(registry_tenant)
        if pointer.version_id is None:
            raise ValueError("no active semantic registry for monitor scope")
        stored = {
            item.trace_id: item
            for item in storage.list_trace_cluster_assignments(
                registry_tenant, pointer.version_id
            )
        }
        projection: dict[str, tuple[str, bool]] = {}
        new_representatives: list[Trace] = []
        by_unit: dict[str, list[Trace]] = defaultdict(list)
        for trace in rows:
            by_unit[trace.session_id or trace.trace_id].append(trace)
        for unit_rows in by_unit.values():
            ordered = sorted(unit_rows, key=lambda trace: (trace.started_at, trace.trace_id))
            represented = [trace for trace in ordered if trace.trace_id in stored]
            representative = represented[0] if represented else ordered[0]
            if not represented:
                new_representatives.append(representative)
            for trace in ordered:
                projection[trace.trace_id] = (representative.trace_id, False)
        service.assign_manifest(
            registry_tenant,
            pointer.version_id,
            traces=new_representatives,
        )
        stored = {
            item.trace_id: item
            for item in storage.list_trace_cluster_assignments(
                registry_tenant, pointer.version_id
            )
        }
        _persist_projection(
            storage,
            registry_tenant,
            pointer.version_id,
            projection,
            stored,
        )
        resolved_projection = {
            trace_id: (
                representative_id,
                stored[representative_id].origin == "fit",
            )
            for trace_id, (representative_id, _baseline) in projection.items()
        }
        assignments.update(_projected_monitor_assignments(resolved_projection, stored))
        references[trace_tenant] = _registry_reference(registry_tenant, pointer.version_id)
        versions[registry_tenant] = pointer.version_id
    return SemanticMonitoringState(assignments, references, versions, {})


def _cohorts_by_tenant(
    traces: list[Trace],
) -> dict[str | None, _TenantCohorts]:
    by_scope: dict[object, list[Trace]] = defaultdict(list)
    for trace in _eligible_traces(traces):
        by_scope[scope_for_trace(trace)].append(trace)
    baseline_representatives: dict[str | None, dict[str, Trace]] = defaultdict(dict)
    remaining_representatives: dict[str | None, dict[str, Trace]] = defaultdict(dict)
    projection: dict[str | None, dict[str, tuple[str, bool]]] = defaultdict(dict)
    for scope, rows in by_scope.items():
        plan = plan_history(rows)
        if not plan.baseline or not plan.current:
            continue
        for baseline, units in (
            (True, plan.baseline),
            (False, plan.current),
            (False, (plan.excluded_middle,) if plan.excluded_middle else ()),
        ):
            for unit in units:
                representative = unit.traces[0]
                targets = (
                    baseline_representatives if baseline else remaining_representatives
                )
                targets[scope.tenant_id][representative.trace_id] = representative
                for trace in unit.traces:
                    projection[scope.tenant_id][trace.trace_id] = (
                        representative.trace_id,
                        baseline,
                    )
    return {
        tenant: _TenantCohorts(
            list(baseline_representatives[tenant].values()),
            list(remaining_representatives[tenant].values()),
            projection[tenant],
        )
        for tenant in baseline_representatives
    }


def _eligible_traces(traces: list[Trace]) -> list[Trace]:
    return [
        trace
        for trace in traces
        if trace.tags.get("verdict.workload") != "judge"
        and trace.started_at.tzinfo is not None
        and (trace.prompt_redacted or "").strip()
    ]


def _persist_projection(
    storage: object,
    tenant: str,
    version_id: str,
    projection: dict[str, tuple[str, bool]],
    representative_assignments: dict[str, TraceClusterAssignment],
) -> None:
    rows = []
    for trace_id, (representative_id, _baseline) in projection.items():
        if trace_id == representative_id or trace_id in representative_assignments:
            continue
        source = representative_assignments[representative_id]
        rows.append(
            TraceClusterAssignment(
                tenant,
                version_id,
                trace_id,
                "incremental",
                source.status,
                source.cluster_id,
                source.cluster_kind,
                source.reason,
                source.distance,
            )
        )
    for start in range(0, len(rows), 1_000):
        storage.insert_trace_cluster_assignments(tenant, rows[start : start + 1_000])


def _projected_monitor_assignments(
    projection: dict[str, tuple[str, bool]],
    representative_assignments: dict[str, TraceClusterAssignment],
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for trace_id, (representative_id, baseline) in projection.items():
        assignment = representative_assignments[representative_id]
        if assignment.status == "assigned" and assignment.cluster_id:
            resolved[trace_id] = assignment.cluster_id
        elif assignment.status == "outlier" and not baseline:
            resolved[trace_id] = "new_intent"
        else:
            resolved[trace_id] = "not_evaluable"
    return resolved


def _registry_reference(tenant: str, version_id: str) -> str:
    return json.dumps(
        {
            "schema": "cluster-registry-reference-v1",
            "tenant_id": tenant,
            "version_id": version_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
