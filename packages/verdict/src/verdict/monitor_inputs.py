"""Canonical storage-to-monitor projection used by every monitor runner."""

from __future__ import annotations

from verdict.monitoring import MonitorPolicy, trace_monitor_units

LOCAL_TENANT = "__verdict_local__"
LOCAL_TRACE_SCOPE = "__verdict_local__:application:trace"
MAX_MONITOR_INPUTS = 100_000


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
    if not judgments:
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
    traces = storage.list_traces(
        tenant_id=tenant_id, limit=MAX_MONITOR_INPUTS + 1,
    )
    if len(traces) > MAX_MONITOR_INPUTS:
        raise ValueError("monitor exceeds bounded trace limit")

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
    if policy.grouping_mode == "cluster":
        version_id = policy.cluster_registry_version_id
        if version_id is None:
            raise ValueError("cluster monitor requires a frozen registry version")
        if storage.get_cluster_registry_version(tenant_id, version_id) is None:
            raise ValueError("frozen cluster registry is unavailable")
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

    return trace_monitor_units(
        traces,
        grouping_mode=policy.grouping_mode,
        cluster_assignments=assignments,
        judgments_by_trace=judgments_by_trace,
        evaluator_dimensions=policy.evaluator_dimensions,
    )
