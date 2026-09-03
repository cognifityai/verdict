"""In-memory storage — the second adapter, used in tests.

The existence of this adapter is what makes Storage a real abstraction.
If we only had SQLiteStorage, the port wouldn't be exercised by alternative
implementations and would slowly couple to SQLite semantics.
"""

from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager
from datetime import datetime

from verdict.analysis_records import (
    DeliveryOutcome,
    DeterministicAnalysisRun,
    NotificationDeliveryAttempt,
    analysis_run_from_json,
    analysis_run_to_json,
    notification_attempt_from_json,
    notification_attempt_to_json,
    validate_delivery_query,
)
from verdict.evidence import (
    AgentRunBundle,
    agent_run_bundle_from_json,
    agent_run_bundle_to_json,
)
from verdict.monitoring import (
    CohortManifest,
    MonitorComparison,
    MonitorPolicy,
    monitor_policy_from_json,
    monitor_policy_to_json,
    monitor_snapshot_from_json,
    monitor_snapshot_to_json,
)
from verdict.redaction import (
    sanitize_agent_run_bundle,
    sanitize_judgment,
    sanitize_span,
    sanitize_trace,
)
from verdict.schema import (
    ActiveClusterRegistry,
    ClusterIdentity,
    ClusterRegistryCluster,
    ClusterRegistryEvent,
    ClusterRegistryVersion,
    ClusterTraceCandidate,
    DriftRun,
    DriftSignal,
    EvaluatorHealthRecord,
    Judgment,
    JudgmentStatus,
    SpanRecord,
    Trace,
    TraceClusterAssignment,
    UserSignalRecord,
    cluster_candidate_digest,
    populate_trace_analysis_fields,
)
from verdict.storage.base import (
    _validate_agent_bundle_query,
    _validate_agent_bundle_run_id,
    _validate_drift_run_snapshot,
    _validate_evaluator_judgment_query,
)


class InMemoryStorage:
    """Process-local, non-persistent storage. Loses everything on close()."""

    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}
        self._agent_run_bundles: dict[tuple[str, str], str] = {}
        self._agent_evidence_lock = threading.RLock()
        self._analysis_runs: dict[str, str] = {}
        self._analysis_inputs: dict[tuple[str, str, str, str, str], str] = {}
        self._notification_attempts: dict[str, str] = {}
        self._analysis_lock = threading.RLock()
        self._monitor_policies: dict[str, tuple[str, str]] = {}
        self._active_monitor_policies: dict[str, str] = {}
        self._monitor_snapshots: dict[tuple[str, str], str] = {}
        self._monitor_lock = threading.RLock()
        self._judgments: dict[str, Judgment] = {}
        self._evaluator_health: dict[str, EvaluatorHealthRecord] = {}
        self._drift_runs: dict[str, DriftRun] = {}
        self._signals: dict[str, DriftSignal] = {}
        self._drift_lock = threading.RLock()
        self._cluster_registries: dict[str, str] = {}
        self._cluster_identities: dict[tuple[str, str], ClusterIdentity] = {}
        self._cluster_versions: dict[tuple[str, str], ClusterRegistryVersion] = {}
        self._cluster_version_clusters: dict[tuple[str, str, str], ClusterRegistryCluster] = {}
        self._trace_cluster_assignments: dict[tuple[str, str, str], TraceClusterAssignment] = {}
        self._active_cluster_registries: dict[str, ActiveClusterRegistry] = {}
        self._cluster_registry_events: dict[tuple[str, str], ClusterRegistryEvent] = {}
        self._cluster_v2_lock = threading.RLock()
        self._cluster_snapshot = threading.local()
        self._spans: dict[str, SpanRecord] = {}
        self._user_signals: dict[str, UserSignalRecord] = {}

    def insert_trace(self, trace: Trace) -> None:
        sanitize_trace(trace)
        populate_trace_analysis_fields(trace)
        # Match the SQL adapters' UPSERT semantics: a re-write that carries no
        # cluster_id (None) must not clobber an already-assigned one. The
        # clusterer assigns cluster_id after the trace is first written, and a
        # later content/usage update would otherwise erase it. (COALESCE parity
        # with sqlite/postgres ON CONFLICT.)
        existing = self._traces.get(trace.trace_id)
        if existing is not None:
            for name in (
                "started_at",
                "provider",
                "operation",
                "request_model",
                "temperature",
                "max_tokens",
                "raw_messages",
                "tenant_id",
                "session_id",
                "user_id_hash",
                "tags",
                "analysis_started_at_us",
                "analysis_started_at_state",
                "analysis_raw_messages_utf8_bytes",
                "analysis_raw_messages_state",
            ):
                setattr(trace, name, copy.deepcopy(getattr(existing, name)))
            if trace.cluster_id is None and existing.cluster_id is not None:
                trace.cluster_id = existing.cluster_id
            if trace.parent_span_id is None and existing.parent_span_id is not None:
                trace.parent_span_id = existing.parent_span_id
        with self._cluster_v2_lock:
            self._traces[trace.trace_id] = trace

    def replace_agent_run_bundle(self, bundle: AgentRunBundle) -> None:
        sanitized = sanitize_agent_run_bundle(bundle)
        payload = agent_run_bundle_to_json(sanitized)
        with self._agent_evidence_lock:
            self._agent_run_bundles[(sanitized.run.tenant_id, sanitized.run.run_id)] = payload

    def get_agent_run_bundle(
        self,
        tenant_id: str,
        run_id: str,
    ) -> AgentRunBundle | None:
        _validate_agent_bundle_query(tenant_id, 1)
        _validate_agent_bundle_run_id(run_id)
        with self._agent_evidence_lock:
            payload = self._agent_run_bundles.get((tenant_id, run_id))
        return agent_run_bundle_from_json(payload) if payload is not None else None

    def list_agent_run_bundles(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> list[AgentRunBundle]:
        _validate_agent_bundle_query(tenant_id, limit)
        with self._agent_evidence_lock:
            bundles = [
                agent_run_bundle_from_json(payload)
                for (scope, _run_id), payload in self._agent_run_bundles.items()
                if scope == tenant_id
            ]
        bundles.sort(key=lambda bundle: (bundle.run.started_at, bundle.run.run_id), reverse=True)
        return bundles[:limit]

    def has_agent_run_source_kind(self, tenant_id: str, source_kind: str) -> bool:
        _validate_agent_bundle_query(tenant_id, 1)
        if not isinstance(source_kind, str) or not source_kind or len(source_kind) > 64:
            raise ValueError("invalid source kind")
        with self._agent_evidence_lock:
            return any(
                scope == tenant_id
                and agent_run_bundle_from_json(payload).session.source_kind == source_kind
                for (scope, _run_id), payload in self._agent_run_bundles.items()
            )

    def save_deterministic_analysis_run(self, run: DeterministicAnalysisRun) -> None:
        payload = analysis_run_to_json(run)
        input_key = (
            run.tenant_id,
            run.scope_key,
            run.analyzer_version,
            run.input_fingerprint,
            run.status.value,
        )
        with self._analysis_lock:
            existing = self._analysis_runs.get(run.analysis_id)
            if existing is not None and existing != payload:
                raise ValueError("analysis identity has different content")
            prior_id = self._analysis_inputs.get(input_key)
            if prior_id is not None:
                prior = analysis_run_from_json(self._analysis_runs[prior_id])
                if prior.result != run.result or prior.cutoff != run.cutoff:
                    raise ValueError("analysis input produced different content")
                return
            self._analysis_runs[run.analysis_id] = payload
            self._analysis_inputs[input_key] = run.analysis_id

    def get_latest_deterministic_analysis_run(
        self, tenant_id: str, scope_key: str,
    ) -> DeterministicAnalysisRun | None:
        with self._analysis_lock:
            matches = []
            for payload in self._analysis_runs.values():
                parsed = analysis_run_from_json(payload)
                if parsed.tenant_id == tenant_id and parsed.scope_key == scope_key:
                    matches.append(parsed)
        return max(matches, key=lambda value: (value.completed_at, value.analysis_id)) if matches else None

    def save_notification_delivery_attempt(
        self, attempt: NotificationDeliveryAttempt,
    ) -> None:
        payload = notification_attempt_to_json(attempt)
        with self._analysis_lock:
            existing = self._notification_attempts.get(attempt.attempt_id)
            if existing is not None and existing != payload:
                raise ValueError("notification attempt identity has different content")
            self._notification_attempts.setdefault(attempt.attempt_id, payload)

    def list_notification_delivery_attempts(
        self,
        notification_id: str,
        destination_fingerprint: str,
        *,
        limit: int = 100,
    ) -> list[NotificationDeliveryAttempt]:
        validate_delivery_query(notification_id, destination_fingerprint, limit)
        with self._analysis_lock:
            attempts = [
                notification_attempt_from_json(payload)
                for payload in self._notification_attempts.values()
            ]
        attempts = [
            value for value in attempts
            if value.notification_id == notification_id
            and value.destination_fingerprint == destination_fingerprint
        ]
        attempts.sort(key=lambda value: (value.attempted_at, value.attempt_id), reverse=True)
        return attempts[:limit]

    def notification_was_delivered(
        self, notification_id: str, destination_fingerprint: str,
    ) -> bool:
        return any(
            attempt.outcome is DeliveryOutcome.DELIVERED
            for attempt in self.list_notification_delivery_attempts(
                notification_id, destination_fingerprint, limit=1000,
            )
        )

    def list_notification_delivery_attempts_for_tenant(
        self, tenant_id: str, *, limit: int = 100,
    ) -> list[NotificationDeliveryAttempt]:
        _validate_agent_bundle_query(tenant_id, limit)
        with self._analysis_lock:
            attempts = [
                notification_attempt_from_json(payload)
                for payload in self._notification_attempts.values()
            ]
        attempts = [attempt for attempt in attempts if attempt.tenant_id == tenant_id]
        attempts.sort(key=lambda value: (value.attempted_at, value.attempt_id), reverse=True)
        return attempts[:limit]

    def save_monitor_policy(self, policy: MonitorPolicy) -> None:
        payload = monitor_policy_to_json(policy)
        with self._monitor_lock:
            existing = self._monitor_policies.get(policy.policy_id)
            if existing is not None and existing[0] != payload:
                raise ValueError("monitor policy identity has a different definition")
            self._monitor_policies.setdefault(policy.policy_id, (payload, "candidate"))

    def get_monitor_policy(self, policy_id: str) -> tuple[MonitorPolicy, str] | None:
        with self._monitor_lock:
            stored = self._monitor_policies.get(policy_id)
        return (monitor_policy_from_json(stored[0]), stored[1]) if stored else None

    def get_active_monitor_policy(self, scope_key: str) -> MonitorPolicy | None:
        with self._monitor_lock:
            policy_id = self._active_monitor_policies.get(scope_key)
            stored = self._monitor_policies.get(policy_id) if policy_id else None
        return monitor_policy_from_json(stored[0]) if stored else None

    def activate_monitor_policy(
        self, scope_key: str, policy_id: str, *, expected_active_policy_id: str | None
    ) -> MonitorPolicy:
        with self._monitor_lock:
            current = self._active_monitor_policies.get(scope_key)
            if current != expected_active_policy_id:
                raise ValueError("active policy changed")
            stored = self._monitor_policies.get(policy_id)
            if stored is None or monitor_policy_from_json(stored[0]).scope_key != scope_key:
                raise ValueError("unknown monitor policy")
            if current:
                payload, _state = self._monitor_policies[current]
                self._monitor_policies[current] = (payload, "retired")
            self._monitor_policies[policy_id] = (stored[0], "active")
            self._active_monitor_policies[scope_key] = policy_id
            return monitor_policy_from_json(stored[0])

    def save_monitor_snapshot(
        self, policy_id: str, manifest: CohortManifest, comparison: MonitorComparison
    ) -> None:
        payload = monitor_snapshot_to_json(manifest, comparison)
        with self._monitor_lock:
            stored_policy = self._monitor_policies.get(policy_id)
            if stored_policy is None:
                raise ValueError("unknown policy")
            if monitor_policy_from_json(stored_policy[0]).fingerprint != manifest.policy_fingerprint:
                raise ValueError("monitor snapshot does not match policy")
            key = (policy_id, manifest.snapshot_id)
            existing = self._monitor_snapshots.get(key)
            if existing is not None and existing != payload:
                raise ValueError("monitor snapshot identity has different content")
            self._monitor_snapshots.setdefault(key, payload)

    def get_latest_monitor_snapshot(
        self, policy_id: str
    ) -> tuple[CohortManifest, MonitorComparison] | None:
        with self._monitor_lock:
            rows = [
                payload for (stored_policy, _snapshot), payload in self._monitor_snapshots.items()
                if stored_policy == policy_id
            ]
        return monitor_snapshot_from_json(rows[-1]) if rows else None

    def get_initial_monitor_snapshot(
        self, policy_id: str
    ) -> tuple[CohortManifest, MonitorComparison] | None:
        with self._monitor_lock:
            rows = [
                payload for (stored_policy, _snapshot), payload in self._monitor_snapshots.items()
                if stored_policy == policy_id
            ]
        return monitor_snapshot_from_json(rows[0]) if rows else None

    def get_latest_monitor_alert(
        self, policy_id: str
    ) -> tuple[CohortManifest, MonitorComparison] | None:
        with self._monitor_lock:
            rows = [
                payload
                for (stored_policy, _snapshot), payload in self._monitor_snapshots.items()
                if stored_policy == policy_id
            ]
        for payload in reversed(rows):
            snapshot = monitor_snapshot_from_json(payload)
            if snapshot[1].status.value == "alert":
                return snapshot
        return None

    def get_trace(self, trace_id: str) -> Trace | None:
        return self._traces.get(trace_id)

    def trace_exists(self, trace_id: str) -> bool:
        return trace_id in self._traces

    def list_traces(
        self,
        *,
        tenant_id: str | None = None,
        cluster_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        out = []
        for t in self._traces.values():
            if tenant_id is not None and t.tenant_id != tenant_id:
                continue
            if cluster_id is not None and t.cluster_id != cluster_id:
                continue
            out.append(t)
        out.sort(key=lambda t: t.started_at, reverse=True)
        return out[:limit]

    def delete_trace(self, trace_id: str) -> None:
        # Registry activation holds this lock while proving candidate coverage.
        # Make source-row removal participate in the same atomic boundary.
        with self._cluster_v2_lock:
            self._traces.pop(trace_id, None)
        retained_parent_span_ids = {
            parent_span_id
            for trace in self._traces.values()
            if (parent_span_id := trace.parent_span_id) is not None
        }
        self._judgments = {jid: j for jid, j in self._judgments.items() if j.trace_id != trace_id}
        for sid, record in self._spans.items():
            if record.trace_id == trace_id and sid in retained_parent_span_ids:
                record.trace_id = None
        self._spans = {
            sid: s
            for sid, s in self._spans.items()
            if s.trace_id != trace_id or sid in retained_parent_span_ids
        }
        self._user_signals = {
            sid: s for sid, s in self._user_signals.items() if s.trace_id != trace_id
        }

    def prune_before(self, cutoff_iso: str) -> int:
        with self._cluster_v2_lock:
            doomed = [
                tid
                for tid, trace in self._traces.items()
                if trace.started_at.isoformat() < cutoff_iso
            ]
            doomed_set = set(doomed)
            for tid in doomed:
                self._traces.pop(tid, None)
        retained_parent_span_ids = {
            parent_span_id
            for trace in self._traces.values()
            if (parent_span_id := trace.parent_span_id) is not None
        }
        self._judgments = {
            jid: judgment
            for jid, judgment in self._judgments.items()
            if judgment.trace_id not in doomed_set
        }
        self._user_signals = {
            sid: signal
            for sid, signal in self._user_signals.items()
            if signal.trace_id not in doomed_set
        }
        for span_id, record in self._spans.items():
            if record.trace_id in doomed_set and span_id in retained_parent_span_ids:
                record.trace_id = None
        self._spans = {
            span_id: record
            for span_id, record in self._spans.items()
            if not (
                span_id not in retained_parent_span_ids
                and (
                    record.trace_id in doomed_set
                    or (
                        record.started_at.isoformat() < cutoff_iso
                        and (record.trace_id is None or record.trace_id not in self._traces)
                    )
                )
            )
        }
        return len(doomed)

    def insert_judgment(self, judgment: Judgment) -> None:
        sanitize_judgment(judgment)
        self._judgments[judgment.judgment_id] = judgment

    def list_judgments_for_cluster(
        self,
        cluster_id: str,
        *,
        since_iso: str | None = None,
        limit: int = 1000,
    ) -> list[Judgment]:
        out = []
        for j in self._judgments.values():
            trace = self._traces.get(j.trace_id)
            if trace is None or trace.cluster_id != cluster_id:
                continue
            if since_iso and j.created_at.isoformat() < since_iso:
                continue
            out.append(j)
            if len(out) >= limit:
                break
        return out

    def list_judgments_for_trace(
        self, trace_id: str, *, limit: int = 100,
    ) -> list[Judgment]:
        if not isinstance(trace_id, str) or not trace_id or not 1 <= limit <= 10_000:
            raise ValueError("invalid trace judgment query")
        return sorted(
            (item for item in self._judgments.values() if item.trace_id == trace_id),
            key=lambda item: (item.created_at, item.judgment_id),
            reverse=True,
        )[:limit]

    def list_latest_judgments_for_evaluator(
        self,
        tenant_id: str,
        evaluator_fingerprint: str,
        *,
        limit: int = 100_000,
    ) -> list[Judgment]:
        _validate_evaluator_judgment_query(tenant_id, evaluator_fingerprint, limit)
        latest: dict[str, Judgment] = {}
        for judgment in self._judgments.values():
            trace = self._traces.get(judgment.trace_id)
            if (
                trace is None
                or trace.tenant_id != tenant_id
                or judgment.evaluator_fingerprint != evaluator_fingerprint
            ):
                continue
            previous = latest.get(judgment.trace_id)
            if previous is None or (judgment.created_at, judgment.judgment_id) > (
                previous.created_at,
                previous.judgment_id,
            ):
                latest[judgment.trace_id] = judgment
        return sorted(
            latest.values(),
            key=lambda item: (item.created_at, item.judgment_id),
            reverse=True,
        )[:limit]

    def has_completed_judgment(
        self, trace_id: str, evaluator_fingerprint: str,
    ) -> bool:
        return any(
            item.trace_id == trace_id
            and item.evaluator_fingerprint == evaluator_fingerprint
            and item.status is JudgmentStatus.COMPLETED
            for item in self._judgments.values()
        )

    def insert_evaluator_health(self, record: EvaluatorHealthRecord) -> None:
        self._evaluator_health[record.health_id] = record

    def list_evaluator_health(
        self,
        *,
        evaluator_fingerprint: str | None = None,
        limit: int = 100,
    ) -> list[EvaluatorHealthRecord]:
        records = sorted(
            self._evaluator_health.values(),
            key=lambda record: record.evaluated_at,
            reverse=True,
        )
        if evaluator_fingerprint is not None:
            records = [
                record
                for record in records
                if record.evaluator_fingerprint == evaluator_fingerprint
            ]
        return records[:limit]

    def insert_drift_signal(self, signal: DriftSignal) -> None:
        with self._drift_lock:
            self._signals[signal.signal_id] = signal

    def replace_drift_run(
        self,
        run: DriftRun,
        signals: list[DriftSignal],
    ) -> None:
        _validate_drift_run_snapshot(run, signals)
        with self._drift_lock:
            existing = self._drift_runs.get(run.run_id)
            if existing is not None and existing.evaluator_fingerprint != run.evaluator_fingerprint:
                raise ValueError("run_id already belongs to another evaluator")
            signal_ids = {signal.signal_id for signal in signals}
            for signal_id in signal_ids:
                existing_signal = self._signals.get(signal_id)
                if (
                    existing_signal is not None
                    and existing_signal.run_id
                    and existing_signal.run_id != run.run_id
                ):
                    raise ValueError("signal_id already belongs to another drift run")
            replacement_signals = {
                signal_id: signal
                for signal_id, signal in self._signals.items()
                if signal.run_id != run.run_id
            }
            replacement_signals.update(
                {signal.signal_id: copy.deepcopy(signal) for signal in signals}
            )
            self._signals = replacement_signals
            self._drift_runs[run.run_id] = copy.deepcopy(run)

    def get_latest_drift_run_snapshot(
        self,
        evaluator_fingerprint: str,
    ) -> tuple[DriftRun, list[DriftSignal]] | None:
        with self._drift_lock:
            candidates = [
                run
                for run in self._drift_runs.values()
                if run.evaluator_fingerprint == evaluator_fingerprint
            ]
            if not candidates:
                return None
            latest = max(
                candidates,
                key=lambda run: (run.analysis_time, run.completed_at, run.run_id),
            )
            signals = sorted(
                (signal for signal in self._signals.values() if signal.run_id == latest.run_id),
                key=lambda signal: signal.signal_id,
            )
            if len(signals) != latest.signal_count:
                raise RuntimeError("stored drift run signal_count is inconsistent")
            return copy.deepcopy(latest), copy.deepcopy(signals)

    def delete_drift_signals_between(
        self,
        start: datetime,
        end: datetime,
        *,
        evaluator_fingerprint: str | None = None,
    ) -> None:
        with self._drift_lock:

            def matches(signal: DriftSignal) -> bool:
                return start <= signal.detected_at < end and (
                    evaluator_fingerprint is None
                    or signal.evaluator_fingerprint == evaluator_fingerprint
                )

            # A completed run is one immutable snapshot. If any attributed
            # signal matches the deletion window, remove that whole snapshot;
            # deleting only part of it would make signal_count untruthful.
            matched_run_ids = {
                signal.run_id
                for signal in self._signals.values()
                if signal.run_id and matches(signal)
            }
            self._signals = {
                signal_id: signal
                for signal_id, signal in self._signals.items()
                if not (signal.run_id in matched_run_ids or (not signal.run_id and matches(signal)))
            }
            for run_id in matched_run_ids:
                self._drift_runs.pop(run_id, None)

    def list_drift_signals(self, *, limit: int = 100) -> list[DriftSignal]:
        with self._drift_lock:
            items = sorted(self._signals.values(), key=lambda s: s.detected_at, reverse=True)
            return items[:limit]

    def insert_span(self, span: SpanRecord) -> None:
        sanitize_span(span)
        self._spans[span.span_id] = span

    def list_spans(self, *, trace_id: str | None = None, limit: int = 100) -> list[SpanRecord]:
        items = sorted(self._spans.values(), key=lambda s: s.started_at, reverse=True)
        out = []
        for s in items:
            if trace_id is not None and s.trace_id != trace_id:
                continue
            out.append(s)
            if len(out) >= limit:
                break
        return out

    def insert_user_signal(self, sig: UserSignalRecord) -> None:
        self._user_signals[sig.signal_id] = sig

    def list_user_signals(self, *, limit: int = 1000) -> list[UserSignalRecord]:
        items = sorted(self._user_signals.values(), key=lambda s: s.created_at, reverse=True)
        return items[:limit]

    def save_cluster_registry(self, version: str, payload_json: str) -> None:
        self._cluster_registries[version] = payload_json

    def load_cluster_registry(self, version: str) -> str | None:
        return self._cluster_registries.get(version)

    def insert_cluster_preview(
        self,
        version: ClusterRegistryVersion,
        identities: list[ClusterIdentity],
        clusters: list[ClusterRegistryCluster],
        assignments: list[TraceClusterAssignment],
    ) -> None:
        tenant = version.tenant_id
        version_key = (tenant, version.version_id)
        with self._cluster_v2_lock:
            if version_key in self._cluster_versions:
                raise ValueError("cluster registry version already exists")
            candidate_identities = copy.deepcopy(self._cluster_identities)
            candidate_clusters = copy.deepcopy(self._cluster_version_clusters)
            candidate_assignments = copy.deepcopy(self._trace_cluster_assignments)
            for identity in identities:
                if identity.tenant_id != tenant or identity.lifecycle != "provisional":
                    raise ValueError("cluster preview tenant mismatch")
                key = (tenant, identity.cluster_id)
                existing = candidate_identities.get(key)
                explicit_owner = next(
                    (
                        item
                        for (scope, _), item in candidate_identities.items()
                        if scope == tenant
                        and identity.explicit_key is not None
                        and item.explicit_key == identity.explicit_key
                    ),
                    None,
                )
                if explicit_owner is not None and explicit_owner.cluster_id != identity.cluster_id:
                    raise ValueError("cluster identity conflict")
                if existing is not None and (
                    existing.kind != identity.kind or existing.explicit_key != identity.explicit_key
                ):
                    raise ValueError("cluster identity conflict")
                candidate_identities.setdefault(key, copy.deepcopy(identity))
            for cluster in clusters:
                if cluster.tenant_id != tenant or cluster.version_id != version.version_id:
                    raise ValueError("cluster preview version mismatch")
                identity = candidate_identities.get((tenant, cluster.cluster_id))
                if identity is None or identity.kind != cluster.kind:
                    raise ValueError("cluster preview identity mismatch")
                candidate_clusters[(tenant, version.version_id, cluster.cluster_id)] = (
                    copy.deepcopy(cluster)
                )
            for assignment in assignments:
                self._validate_preview_assignment(
                    tenant, version.version_id, assignment, candidate_clusters
                )
                candidate_assignments[(tenant, version.version_id, assignment.trace_id)] = (
                    copy.deepcopy(assignment)
                )
            self._cluster_identities = candidate_identities
            self._cluster_version_clusters = candidate_clusters
            self._trace_cluster_assignments = candidate_assignments
            self._cluster_versions[version_key] = copy.deepcopy(version)
            self._active_cluster_registries.setdefault(tenant, ActiveClusterRegistry(tenant))

    @staticmethod
    def _validate_preview_assignment(
        tenant: str,
        version_id: str,
        assignment: TraceClusterAssignment,
        clusters: dict[tuple[str, str, str], ClusterRegistryCluster],
    ) -> None:
        if assignment.tenant_id != tenant or assignment.version_id != version_id:
            raise ValueError("cluster assignment scope mismatch")
        if assignment.status == "assigned":
            cluster = clusters.get((tenant, version_id, assignment.cluster_id or ""))
            if cluster is None or cluster.kind != assignment.cluster_kind:
                raise ValueError("cluster assignment target mismatch")

    def get_cluster_registry_version(
        self,
        authorized_tenant: str,
        version_id: str,
    ) -> ClusterRegistryVersion | None:
        with self._cluster_v2_lock:
            value = self._cluster_versions.get((authorized_tenant, version_id))
            return copy.deepcopy(value)

    def list_cluster_registry_clusters(
        self,
        authorized_tenant: str,
        version_id: str,
    ) -> list[ClusterRegistryCluster]:
        with self._cluster_v2_lock:
            return copy.deepcopy(
                sorted(
                    (
                        cluster
                        for (tenant, version, _), cluster in self._cluster_version_clusters.items()
                        if tenant == authorized_tenant and version == version_id
                    ),
                    key=lambda cluster: cluster.cluster_id,
                )
            )

    def insert_trace_cluster_assignments(
        self,
        authorized_tenant: str,
        assignments: list[TraceClusterAssignment],
    ) -> None:
        with self._cluster_v2_lock:
            candidate = copy.deepcopy(self._trace_cluster_assignments)
            for assignment in assignments:
                if assignment.tenant_id != authorized_tenant:
                    raise ValueError("cluster assignment tenant mismatch")
                if (authorized_tenant, assignment.version_id) not in self._cluster_versions:
                    raise ValueError("unknown cluster registry version")
                self._validate_preview_assignment(
                    authorized_tenant,
                    assignment.version_id,
                    assignment,
                    self._cluster_version_clusters,
                )
                key = (authorized_tenant, assignment.version_id, assignment.trace_id)
                existing = candidate.get(key)
                if existing is not None and existing != assignment:
                    raise ValueError("immutable assignment conflict")
                candidate[key] = copy.deepcopy(assignment)
            self._trace_cluster_assignments = candidate

    def list_trace_cluster_assignments(
        self,
        authorized_tenant: str,
        version_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[TraceClusterAssignment]:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("assignment limit must be a positive integer")
        if type(offset) is not int or offset < 0 or (offset and limit is None):
            raise ValueError("assignment offset requires a limit")
        with self._cluster_v2_lock:
            rows = sorted(
                (
                    assignment
                    for (
                        tenant,
                        version,
                        _,
                    ), assignment in self._trace_cluster_assignments.items()
                    if tenant == authorized_tenant and version == version_id
                ),
                key=lambda assignment: assignment.trace_id,
            )
            return copy.deepcopy(rows if limit is None else rows[offset : offset + limit])

    def list_judgments_for_registry_cluster(
        self,
        authorized_tenant: str,
        version_id: str,
        cluster_id: str,
        *,
        limit: int = 1_000,
    ) -> list[Judgment]:
        trace_ids = {
            item.trace_id
            for (tenant, version, _), item in self._trace_cluster_assignments.items()
            if tenant == authorized_tenant
            and version == version_id
            and item.status == "assigned"
            and item.cluster_id == cluster_id
        }
        rows = sorted(
            (item for item in self._judgments.values() if item.trace_id in trace_ids),
            key=lambda item: item.created_at,
            reverse=True,
        )
        return copy.deepcopy(rows[:limit])

    def insert_cluster_registry_event(self, event: ClusterRegistryEvent) -> None:
        with self._cluster_v2_lock:
            key = (event.tenant_id, event.event_id)
            existing = self._cluster_registry_events.get(key)
            if existing is not None and existing != event:
                raise ValueError("immutable cluster event conflict")
            self._cluster_registry_events[key] = copy.deepcopy(event)

    def list_cluster_registry_events(
        self,
        authorized_tenant: str,
        version_id: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ClusterRegistryEvent]:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("event limit must be a positive integer")
        if type(offset) is not int or offset < 0 or (offset and limit is None):
            raise ValueError("event offset requires a limit")
        with self._cluster_v2_lock:
            events = [
                event
                for (tenant, _), event in self._cluster_registry_events.items()
                if tenant == authorized_tenant
                and (
                    version_id is None
                    or event.from_version_id == version_id
                    or event.to_version_id == version_id
                )
            ]
            rows = sorted(events, key=lambda event: (event.created_at, event.event_id))
            return copy.deepcopy(rows if limit is None else rows[offset : offset + limit])

    def get_active_cluster_registry(
        self,
        authorized_tenant: str,
    ) -> ActiveClusterRegistry:
        with self._cluster_v2_lock:
            value = self._active_cluster_registries.setdefault(
                authorized_tenant, ActiveClusterRegistry(authorized_tenant)
            )
            return copy.deepcopy(value)

    def activate_cluster_registry(
        self,
        authorized_tenant: str,
        version_id: str,
        *,
        expected_generation: int,
        actor: str,
        action: str,
        expected_candidate_digest: str,
    ) -> ActiveClusterRegistry:
        if action not in {"activated", "rolled_back"}:
            raise ValueError("activation action is invalid")
        with self._cluster_v2_lock:
            pointer = self._active_cluster_registries.setdefault(
                authorized_tenant, ActiveClusterRegistry(authorized_tenant)
            )
            if pointer.generation != expected_generation:
                raise ValueError("cluster registry generation conflict")
            version = self._cluster_versions.get((authorized_tenant, version_id))
            if version is None:
                raise ValueError("unknown cluster registry version")
            if action == "activated" and version.parent_version_id != pointer.version_id:
                raise ValueError("cluster registry parent conflict")
            validations = [
                event
                for event in self.list_cluster_registry_events(authorized_tenant, version_id)
                if event.to_version_id == version_id
                and event.action in {"validated", "validation_failed"}
            ]
            if not validations or validations[-1].action != "validated":
                raise ValueError("cluster registry version is not validated")
            clusters = self.list_cluster_registry_clusters(authorized_tenant, version_id)
            config = json.loads(version.fit_definition_json).get("config", {})
            candidate_ids = [
                assignment.trace_id
                for (tenant, candidate_version, _), assignment
                in self._trace_cluster_assignments.items()
                if tenant == authorized_tenant
                and candidate_version == version_id
                and assignment.origin == "fit"
            ]
            if (
                len(candidate_ids) > config.get("max_fit_candidates", 50_000)
                or cluster_candidate_digest(candidate_ids) != expected_candidate_digest
            ):
                raise ValueError("cluster registry coverage changed")
            model_fingerprint = (
                json.loads(version.fit_definition_json).get("model_fingerprint") or None
            )
            target_ids = {cluster.cluster_id for cluster in clusters}
            counts = {"explicit": 0, "semantic": 0}
            for (tenant, cluster_id), identity in self._cluster_identities.items():
                if tenant == authorized_tenant and (
                    identity.lifecycle == "active" or cluster_id in target_ids
                ):
                    counts[identity.kind] += 1
            if counts["explicit"] > config.get(
                "max_explicit_identities_per_tenant", 10_000
            ) or counts["semantic"] > config.get("max_semantic_identities_per_tenant", 5_000):
                raise ValueError("identity_limit")
            updated_identities = copy.deepcopy(self._cluster_identities)
            for cluster in clusters:
                identity = updated_identities[(authorized_tenant, cluster.cluster_id)]
                identity.lifecycle = "active"
                identity.last_version_id = version_id
                identity.last_model_fingerprint = model_fingerprint
                identity.last_centroid = copy.deepcopy(cluster.centroid)
                identity.updated_at = datetime.now(version.cutoff.tzinfo)
                identity.updated_by = actor
            for cluster in clusters:
                identity = updated_identities[(authorized_tenant, cluster.cluster_id)]
                if not (
                    identity.lifecycle == "active"
                    and identity.kind == cluster.kind
                    and identity.last_version_id == version_id
                    and identity.last_model_fingerprint == model_fingerprint
                    and identity.last_centroid == cluster.centroid
                ):
                    raise ValueError("cluster activation identity invariant failed")
            now = datetime.now(version.cutoff.tzinfo)
            new_pointer = ActiveClusterRegistry(
                authorized_tenant,
                version_id,
                expected_generation + 1,
                now,
                actor,
            )
            event = ClusterRegistryEvent(
                tenant_id=authorized_tenant,
                action=action,
                from_version_id=pointer.version_id,
                to_version_id=version_id,
                pointer_generation=new_pointer.generation,
                created_at=now,
                actor=actor,
            )
            self._cluster_registry_events[(authorized_tenant, event.event_id)] = event
            self._active_cluster_registries[authorized_tenant] = new_pointer
            self._cluster_identities = updated_identities
            return copy.deepcopy(new_pointer)

    def rename_cluster_identity(
        self, authorized_tenant: str, cluster_id: str, display_name: str, *, actor: str
    ) -> None:
        with self._cluster_v2_lock:
            key = (authorized_tenant, cluster_id)
            identity = self._cluster_identities.get(key)
            if identity is None:
                raise ValueError("unknown cluster identity")
            event = ClusterRegistryEvent(
                authorized_tenant,
                action="renamed",
                actor=actor,
                details_json=json.dumps({"cluster_id": cluster_id}, sort_keys=True),
            )
            updated = copy.deepcopy(identity)
            updated.display_name = display_name
            updated.updated_at = event.created_at
            updated.updated_by = actor
            self._cluster_registry_events[(authorized_tenant, event.event_id)] = event
            self._cluster_identities[key] = updated

    def list_cluster_identities(
        self,
        authorized_tenant: str,
        *,
        cluster_ids: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ClusterIdentity]:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("identity limit must be a positive integer")
        if type(offset) is not int or offset < 0 or (offset and limit is None):
            raise ValueError("identity offset requires a limit")
        selected_ids = None if cluster_ids is None else set(cluster_ids)
        with self._cluster_v2_lock:
            rows = sorted(
                (
                    identity
                    for (tenant, cluster_id), identity in self._cluster_identities.items()
                    if tenant == authorized_tenant
                    and (selected_ids is None or cluster_id in selected_ids)
                ),
                key=lambda identity: identity.cluster_id,
            )
            return copy.deepcopy(rows if limit is None else rows[offset : offset + limit])

    @staticmethod
    def _routing_projection(
        value: object,
        *,
        present: bool,
    ) -> tuple[str, int | None, str | None]:
        if not present:
            return "missing", None, None
        if value is None:
            return "null", None, None
        if isinstance(value, str):
            encoded = value.encode("utf-8", "surrogatepass")
            return "string", len(encoded), value if len(encoded) <= 64 else None
        if isinstance(value, bool):
            return "boolean", None, None
        if isinstance(value, (int, float)):
            return "number", None, None
        if isinstance(value, list):
            return "array", None, None
        return "object", None, None

    def list_cluster_trace_candidates(
        self,
        authorized_tenant: str,
        start_us: int,
        cutoff_us: int,
        *,
        target_workload: str | None,
        limit: int,
        missing_version_id: str | None = None,
    ) -> list[ClusterTraceCandidate]:
        rows: list[ClusterTraceCandidate] = []
        traces = getattr(self._cluster_snapshot, "traces", self._traces)
        for trace in traces.values():
            if (
                not (
                    trace.tenant_id == authorized_tenant
                    or (authorized_tenant == "__verdict_local__" and trace.tenant_id is None)
                )
                or trace.ended_at is None
                or trace.analysis_started_at_state != "valid"
                or trace.analysis_started_at_us is None
                or not start_us <= trace.analysis_started_at_us < cutoff_us
            ):
                continue
            if (
                missing_version_id is not None
                and (
                    authorized_tenant,
                    missing_version_id,
                    trace.trace_id,
                )
                in self._trace_cluster_assignments
            ):
                continue
            workload = self._routing_projection(
                trace.tags.get("verdict.workload"),
                present="verdict.workload" in trace.tags,
            )
            if target_workload is not None and workload[2] != target_workload:
                continue
            if target_workload is None and workload[2] in {"judge", "paired_replay"}:
                continue
            intent = self._routing_projection(
                trace.tags.get("verdict.intent_key"),
                present="verdict.intent_key" in trace.tags,
            )
            trace_bytes = trace.trace_id.encode("utf-8", "surrogatepass")
            rows.append(
                ClusterTraceCandidate(
                    len(trace_bytes),
                    trace.trace_id if len(trace_bytes) <= 256 else None,
                    authorized_tenant,
                    trace.analysis_started_at_us,
                    workload[0],
                    workload[1],
                    workload[2],
                    intent[0],
                    intent[1],
                    intent[2],
                    trace.analysis_raw_messages_state,
                    trace.analysis_raw_messages_utf8_bytes,
                )
            )
        rows.sort(key=lambda row: (row.started_at_us, row.trace_id or ""))
        return rows[:limit]

    def cluster_trace_time_bounds(
        self,
        authorized_tenant: str,
        *,
        target_workload: str | None,
    ) -> tuple[int, int | None, int | None]:
        traces = getattr(self._cluster_snapshot, "traces", self._traces)
        times: list[int] = []
        for trace in traces.values():
            if (
                not (
                    trace.tenant_id == authorized_tenant
                    or (authorized_tenant == "__verdict_local__" and trace.tenant_id is None)
                )
                or trace.ended_at is None
                or trace.analysis_started_at_state != "valid"
                or trace.analysis_started_at_us is None
            ):
                continue
            workload = self._routing_projection(
                trace.tags.get("verdict.workload"),
                present="verdict.workload" in trace.tags,
            )[2]
            if target_workload is not None and workload != target_workload:
                continue
            if target_workload is None and workload in {"judge", "paired_replay"}:
                continue
            times.append(trace.analysis_started_at_us)
        return (len(times), min(times) if times else None, max(times) if times else None)

    def get_cluster_trace_messages(
        self,
        authorized_tenant: str,
        trace_ids: list[str],
    ) -> dict[str, list[dict] | None]:
        traces = getattr(self._cluster_snapshot, "traces", self._traces)
        return {
            trace_id: copy.deepcopy(trace.raw_messages)
            for trace_id in trace_ids
            if (trace := traces.get(trace_id)) is not None
            and (
                trace.tenant_id == authorized_tenant
                or (authorized_tenant == "__verdict_local__" and trace.tenant_id is None)
            )
            and trace.analysis_raw_messages_state == "valid"
        }

    def count_pending_analysis_rows(self, authorized_tenant: str) -> int:
        traces = getattr(self._cluster_snapshot, "traces", self._traces)
        return sum(
            1
            for trace in traces.values()
            if (
                trace.tenant_id == authorized_tenant
                or (authorized_tenant == "__verdict_local__" and trace.tenant_id is None)
            )
            and (
                trace.analysis_started_at_state == "pending"
                or trace.analysis_raw_messages_state == "pending"
            )
        )

    @contextmanager
    def cluster_analysis_snapshot(self):
        previous = getattr(self._cluster_snapshot, "traces", None)
        with self._cluster_v2_lock:
            self._cluster_snapshot.traces = copy.deepcopy(self._traces)
        try:
            yield self
        finally:
            if previous is None:
                del self._cluster_snapshot.traces
            else:
                self._cluster_snapshot.traces = previous

    def normalize_cluster_trace_analysis(
        self, authorized_tenant: str, *, limit: int = 10_000
    ) -> int:
        if not 1 <= limit <= 10_000:
            raise ValueError("normalization limit must be in [1,10000]")
        pending = [
            trace
            for trace in self._traces.values()
            if (
                trace.tenant_id == authorized_tenant
                or (authorized_tenant == "__verdict_local__" and trace.tenant_id is None)
            )
            and (
                trace.analysis_started_at_state == "pending"
                or trace.analysis_raw_messages_state == "pending"
            )
        ]
        pending.sort(key=lambda trace: trace.trace_id)
        for trace in pending[:limit]:
            populate_trace_analysis_fields(trace)
        return min(len(pending), limit)

    def close(self) -> None:
        self._traces.clear()
        self._judgments.clear()
        self._evaluator_health.clear()
        self._signals.clear()
        self._drift_runs.clear()
        self._analysis_runs.clear()
        self._analysis_inputs.clear()
        self._notification_attempts.clear()
        self._cluster_registries.clear()
        self._cluster_identities.clear()
        self._cluster_versions.clear()
        self._cluster_version_clusters.clear()
        self._trace_cluster_assignments.clear()
        self._active_cluster_registries.clear()
        self._cluster_registry_events.clear()
        self._spans.clear()
        self._user_signals.clear()
