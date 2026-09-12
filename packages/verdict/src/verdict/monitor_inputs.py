"""Canonical storage-to-monitor projection used by every monitor runner."""

from __future__ import annotations

from datetime import timedelta

from verdict.cluster_runtime import (
    cluster_model_path_for_version,
    cluster_registry_service,
)
from verdict.monitoring import (
    MAX_MONITOR_GROUPS,
    MonitorPolicy,
    MonitorRebootstrapRequired,
    MonitorStateConflict,
    compare_manifest,
    monitor_requires_rebootstrap,
    plan_prospective_manifest,
    trace_monitor_units,
)

LOCAL_TENANT = "__verdict_local__"
LOCAL_TRACE_SCOPE = "__verdict_local__:application:trace"
MAX_MONITOR_INPUTS = 100_000
MAX_CLUSTER_PROJECTION_PASSES = 128


class MonitorProjectionPending(ValueError):
    """Bounded cluster projection made progress but has not completed yet."""


def select_monitor_evaluator(
    storage, *, tenant_id: str, evaluator_fingerprint: str | None
) -> tuple[str | None, tuple[str, ...]]:
    """Resolve one stored evaluator identity for a new monitor policy."""
    if evaluator_fingerprint in (None, ""):
        return None, ()
    judgments = _evaluator_judgments(
        storage,
        tenant_id=tenant_id,
        evaluator_fingerprint=evaluator_fingerprint,
    )
    dimensions = {tuple(row.expected_dimensions) for row in judgments}
    if len(dimensions) != 1:
        raise ValueError("selected evaluator dimensions are inconsistent")
    return evaluator_fingerprint, dimensions.pop()


def _evaluator_judgments(
    storage,
    *,
    tenant_id: str,
    evaluator_fingerprint: str,
    expected_dimensions: tuple[str, ...] | None = None,
):
    if len(evaluator_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in evaluator_fingerprint
    ):
        raise ValueError("selected evaluator fingerprint is invalid")
    judgments = storage.list_latest_judgments_for_evaluator(
        tenant_id,
        evaluator_fingerprint,
        limit=MAX_MONITOR_INPUTS + 1,
    )
    if not judgments and expected_dimensions is None:
        raise ValueError("selected evaluator is unavailable")
    if len(judgments) > MAX_MONITOR_INPUTS:
        raise ValueError("monitor exceeds bounded judgment limit")
    if any(
        not row.evaluator_identity_complete
        or row.evaluator_fingerprint != evaluator_fingerprint
        or (
            expected_dimensions is not None
            and tuple(row.expected_dimensions) != expected_dimensions
        )
        for row in judgments
    ):
        raise ValueError("selected evaluator identity changed")
    return judgments


def load_monitor_units(storage, policy: MonitorPolicy, *, tenant_id: str):
    """Load one bounded, evaluator- and grouping-aware monitor input set."""
    sequenced_traces = storage.list_traces_with_ingest_sequence(
        tenant_id=tenant_id, limit=MAX_MONITOR_INPUTS + 1,
    )
    if len(sequenced_traces) > MAX_MONITOR_INPUTS:
        raise ValueError("monitor exceeds bounded trace limit")
    traces = [trace for trace, _sequence in sequenced_traces]
    ingest_sequences = {
        trace.trace_id: sequence for trace, sequence in sequenced_traces
    }

    judgments_by_trace = None
    if policy.evaluator_fingerprint is not None:
        judgments = _evaluator_judgments(
            storage,
            tenant_id=tenant_id,
            evaluator_fingerprint=policy.evaluator_fingerprint,
            expected_dimensions=policy.evaluator_dimensions,
        )
        judgments_by_trace = {row.trace_id: row for row in judgments}

    assignments = None
    cluster_labels = None
    if policy.grouping_mode == "cluster":
        version_id = policy.cluster_registry_version_id
        if version_id is None:
            raise ValueError("cluster monitor requires a frozen registry version")
        version = storage.get_cluster_registry_version(tenant_id, version_id)
        if version is None:
            raise ValueError("frozen cluster registry is unavailable")
        rows = storage.list_trace_cluster_assignments(
            tenant_id, version_id, limit=MAX_MONITOR_INPUTS + 1,
        )
        if len(rows) > MAX_MONITOR_INPUTS:
            raise ValueError("cluster assignment limit exceeded")
        projected_trace_ids = {row.trace_id for row in rows}
        possibly_assignable = {
            trace.trace_id
            for trace in traces
            if trace.ended_at is not None
            and trace.tags.get("verdict.workload") not in {"judge", "paired_replay"}
        }
        if possibly_assignable - projected_trace_ids:
            service = cluster_registry_service(
                storage,
                strategy=version.strategy,
                model_path=cluster_model_path_for_version(version),
            )
            projection_cutoff = max(trace.started_at for trace in traces) + timedelta(
                microseconds=1
            )
            projected = 0
            for _ in range(MAX_CLUSTER_PROJECTION_PASSES):
                assigned = service.assign(
                    tenant_id, version_id, through_cutoff=projection_cutoff,
                )
                if assigned == 0:
                    break
                projected += assigned
                if projected > MAX_MONITOR_INPUTS:
                    raise ValueError("cluster assignment limit exceeded")
            else:
                raise MonitorProjectionPending(
                    "cluster projection is still processing; run the monitor again"
                )
            rows = storage.list_trace_cluster_assignments(
                tenant_id, version_id, limit=MAX_MONITOR_INPUTS + 1,
            )
            if len(rows) > MAX_MONITOR_INPUTS:
                raise ValueError("cluster assignment limit exceeded")
        assignments = {
            row.trace_id: row.cluster_id
            for row in rows
            if row.status == "assigned" and row.cluster_id is not None
        }
        cluster_ids = sorted(set(assignments.values()))
        identities = storage.list_cluster_identities(
            tenant_id,
            cluster_ids=cluster_ids,
            limit=MAX_MONITOR_GROUPS + 1,
        )
        if len(identities) > MAX_MONITOR_GROUPS:
            raise ValueError(f"monitor grouping exceeds {MAX_MONITOR_GROUPS} groups")
        cluster_labels = {item.cluster_id: item.display_name for item in identities}

    return trace_monitor_units(
        traces,
        analysis_unit=policy.analysis_unit,
        ingest_sequences=ingest_sequences,
        grouping_mode=policy.grouping_mode,
        cluster_assignments=assignments,
        judgments_by_trace=judgments_by_trace,
        evaluator_dimensions=policy.evaluator_dimensions,
        cluster_labels=cluster_labels,
    )


def advance_monitor(
    storage,
    policy: MonitorPolicy,
    *,
    tenant_id: str,
    expected_state: str = "active",
):
    """Advance one active policy through the canonical persisted lifecycle."""
    if expected_state not in {"active", "candidate"}:
        raise ValueError("monitor expected state is invalid")
    previous = storage.get_latest_monitor_snapshot(policy.policy_id)
    if previous is None:
        raise ValueError("monitor policy has no snapshot")
    if monitor_requires_rebootstrap(
        policy, previous[0], active=expected_state == "active",
    ):
        raise MonitorRebootstrapRequired("monitor requires re-bootstrap")
    units = load_monitor_units(storage, policy, tenant_id=tenant_id)
    manifest = plan_prospective_manifest(previous[0], units, policy)
    comparison = compare_manifest(units, manifest, policy)
    try:
        storage.save_monitor_successor(
            policy.policy_id,
            previous[0].snapshot_id,
            manifest,
            comparison,
            expected_state=expected_state,
        )
    except MonitorStateConflict:
        stored = storage.get_monitor_policy(policy.policy_id)
        if stored is None or stored[1] != expected_state:
            raise
        if (
            expected_state == "active"
            and getattr(storage.get_active_monitor_policy(policy.scope_key), "policy_id", None)
            != policy.policy_id
        ):
            raise
        winner = storage.get_latest_monitor_snapshot(policy.policy_id)
        if winner is None or winner[0].snapshot_id == previous[0].snapshot_id:
            raise
        return winner
    return manifest, comparison
