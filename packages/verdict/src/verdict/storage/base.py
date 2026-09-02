"""Storage port — every adapter implements this Protocol.

Critically: this is a Protocol, not an ABC. Adapters need not inherit; they
just need to provide the methods. This keeps the port lightweight and avoids
import cycles.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from verdict.analysis_records import (
    DeterministicAnalysisRun,
    NotificationDeliveryAttempt,
)
from verdict.evidence import AgentRunBundle
from verdict.monitoring import CohortManifest, MonitorComparison, MonitorPolicy
from verdict.schema import (
    DriftRun,
    DriftSignal,
    EvaluatorHealthRecord,
    Judgment,
    SpanRecord,
    Trace,
    UserSignalRecord,
)


def _validate_drift_run_snapshot(
    run: DriftRun,
    signals: list[DriftSignal],
) -> None:
    """Validate a completed run before any adapter mutates durable state."""
    if run.signal_count != len(signals):
        raise ValueError(
            "drift run signal_count does not match the provided signal list"
        )
    signal_ids: set[str] = set()
    for signal in signals:
        if signal.run_id != run.run_id:
            raise ValueError("every drift signal must reference the owning run_id")
        if signal.evaluator_fingerprint != run.evaluator_fingerprint:
            raise ValueError(
                "every drift signal must match the run evaluator_fingerprint"
            )
        if signal.signal_id in signal_ids:
            raise ValueError("drift run contains duplicate signal_id values")
        signal_ids.add(signal.signal_id)


def _validate_agent_bundle_query(tenant_id: str, limit: int) -> None:
    if not isinstance(tenant_id, str) or not tenant_id or "\x00" in tenant_id:
        raise ValueError("tenant_id must be bounded text")
    try:
        tenant_size = len(tenant_id.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("tenant_id must be bounded text") from exc
    if tenant_size > 256:
        raise ValueError("tenant_id must be bounded text")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")


def _validate_agent_bundle_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not run_id or "\x00" in run_id:
        raise ValueError("run_id must be bounded text")
    try:
        run_size = len(run_id.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("run_id must be bounded text") from exc
    if run_size > 256:
        raise ValueError("run_id must be bounded text")


@runtime_checkable
class Storage(Protocol):
    """Persistent backing store for Traces, Judgments, and DriftSignals.

    All methods are sync for v0. An AsyncStorage Protocol may follow.
    """

    def insert_trace(self, trace: Trace) -> None: ...

    def replace_agent_run_bundle(self, bundle: AgentRunBundle) -> None: ...

    def get_agent_run_bundle(
        self,
        tenant_id: str,
        run_id: str,
    ) -> AgentRunBundle | None: ...

    def list_agent_run_bundles(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> list[AgentRunBundle]: ...

    def save_deterministic_analysis_run(
        self, run: DeterministicAnalysisRun,
    ) -> None: ...

    def get_latest_deterministic_analysis_run(
        self, tenant_id: str, scope_key: str,
    ) -> DeterministicAnalysisRun | None: ...

    def save_notification_delivery_attempt(
        self, attempt: NotificationDeliveryAttempt,
    ) -> None: ...

    def list_notification_delivery_attempts(
        self,
        notification_id: str,
        destination_fingerprint: str,
        *,
        limit: int = 100,
    ) -> list[NotificationDeliveryAttempt]: ...

    def notification_was_delivered(
        self, notification_id: str, destination_fingerprint: str,
    ) -> bool: ...

    def list_notification_delivery_attempts_for_tenant(
        self, tenant_id: str, *, limit: int = 100,
    ) -> list[NotificationDeliveryAttempt]: ...

    def save_monitor_policy(self, policy: MonitorPolicy) -> None: ...

    def get_monitor_policy(self, policy_id: str) -> tuple[MonitorPolicy, str] | None: ...

    def get_active_monitor_policy(self, scope_key: str) -> MonitorPolicy | None: ...

    def activate_monitor_policy(
        self, scope_key: str, policy_id: str, *, expected_active_policy_id: str | None
    ) -> MonitorPolicy: ...

    def save_monitor_snapshot(
        self, policy_id: str, manifest: CohortManifest, comparison: MonitorComparison
    ) -> None: ...

    def get_latest_monitor_snapshot(
        self, policy_id: str
    ) -> tuple[CohortManifest, MonitorComparison] | None: ...

    def get_latest_monitor_alert(
        self, policy_id: str
    ) -> tuple[CohortManifest, MonitorComparison] | None: ...

    def get_trace(self, trace_id: str) -> Trace | None: ...

    def trace_exists(self, trace_id: str) -> bool: ...

    def list_traces(
        self,
        *,
        tenant_id: str | None = None,
        cluster_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]: ...

    def delete_trace(self, trace_id: str) -> None: ...

    def prune_before(self, cutoff_iso: str) -> int: ...

    def insert_judgment(self, judgment: Judgment) -> None: ...

    def list_judgments_for_trace(
        self, trace_id: str, *, limit: int = 100,
    ) -> list[Judgment]: ...

    def has_completed_judgment(
        self, trace_id: str, evaluator_fingerprint: str,
    ) -> bool: ...

    def list_judgments_for_cluster(
        self,
        cluster_id: str,
        *,
        since_iso: str | None = None,
        limit: int = 1000,
    ) -> list[Judgment]: ...

    def insert_evaluator_health(self, record: EvaluatorHealthRecord) -> None: ...

    def list_evaluator_health(
        self, *, evaluator_fingerprint: str | None = None, limit: int = 100,
    ) -> list[EvaluatorHealthRecord]: ...

    def insert_drift_signal(self, signal: DriftSignal) -> None: ...

    def replace_drift_run(
        self, run: DriftRun, signals: list[DriftSignal],
    ) -> None: ...

    def get_latest_drift_run_snapshot(
        self, evaluator_fingerprint: str,
    ) -> tuple[DriftRun, list[DriftSignal]] | None: ...

    def delete_drift_signals_between(
        self,
        start: datetime,
        end: datetime,
        *,
        evaluator_fingerprint: str | None = None,
    ) -> None: ...

    def list_drift_signals(self, *, limit: int = 100) -> list[DriftSignal]: ...

    # -- Cluster registry --------------------------------------------------
    # Persists the StableIntentClusterer registry (a JSON blob) keyed by a
    # clustering_version, so cluster IDs stay stable across runs instead of
    # being recomputed from scratch each time.

    def save_cluster_registry(self, version: str, payload_json: str) -> None: ...

    def load_cluster_registry(self, version: str) -> str | None: ...

    # -- Spans (manual @trace / span persistence) --------------------------

    def insert_span(self, span: SpanRecord) -> None: ...

    def list_spans(
        self, *, trace_id: str | None = None, limit: int = 100,
    ) -> list[SpanRecord]: ...

    # -- User signals (thumbs/regenerate/abandon, for the correlator) -------

    def insert_user_signal(self, sig: UserSignalRecord) -> None: ...

    def list_user_signals(self, *, limit: int = 1000) -> list[UserSignalRecord]: ...

    def close(self) -> None: ...
