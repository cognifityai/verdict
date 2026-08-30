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
from dataclasses import replace
from datetime import datetime, timedelta

from verdict.redaction import sanitize_judgment, sanitize_span, sanitize_trace
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
    MonitorMember,
    MonitorResult,
    MonitorSeries,
    SpanRecord,
    Trace,
    TraceClusterAssignment,
    UserSignalRecord,
    cluster_candidate_digest,
    datetime_to_utc_us,
    populate_trace_analysis_fields,
)
from verdict.storage.base import _validate_drift_run_snapshot


class InMemoryStorage:
    """Process-local, non-persistent storage. Loses everything on close()."""

    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}
        self._judgments: dict[str, Judgment] = {}
        self._evaluator_health: dict[str, EvaluatorHealthRecord] = {}
        self._drift_runs: dict[str, DriftRun] = {}
        self._signals: dict[str, DriftSignal] = {}
        self._monitor_series: dict[str, MonitorSeries] = {}
        self._monitor_members: dict[tuple[str, str], MonitorMember] = {}
        self._monitor_results: dict[tuple[str, str, int], MonitorResult] = {}
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

    def list_judgments(
        self,
        *,
        evaluator_fingerprint: str | None = None,
        limit: int = 1000,
    ) -> list[Judgment]:
        rows = sorted(
            self._judgments.values(),
            key=lambda judgment: (judgment.created_at, judgment.judgment_id),
            reverse=True,
        )
        if evaluator_fingerprint is not None:
            rows = [
                row for row in rows if row.evaluator_fingerprint == evaluator_fingerprint
            ]
        return rows[:limit]

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
            self._validate_snapshot_ownership_locked(run, signals)
            self._store_snapshot_locked(run, signals)

    def _validate_snapshot_ownership_locked(
        self, run: DriftRun, signals: list[DriftSignal]
    ) -> None:
        existing = self._drift_runs.get(run.run_id)
        if existing is not None and existing.evaluator_fingerprint != run.evaluator_fingerprint:
            raise ValueError("run_id already belongs to another evaluator")
        for signal in signals:
            existing_signal = self._signals.get(signal.signal_id)
            if (
                existing_signal is not None
                and existing_signal.run_id
                and existing_signal.run_id != run.run_id
            ):
                raise ValueError("signal_id already belongs to another drift run")

    def _store_snapshot_locked(self, run: DriftRun, signals: list[DriftSignal]) -> None:
        replacement_signals = {
            signal_id: signal
            for signal_id, signal in self._signals.items()
            if signal.run_id != run.run_id
        }
        replacement_signals.update({signal.signal_id: copy.deepcopy(signal) for signal in signals})
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

    # -- Count-cohort monitoring -----------------------------------------

    def create_monitor_series(
        self,
        series: MonitorSeries,
        members: list[MonitorMember],
        *,
        snapshot: tuple[DriftRun, list[DriftSignal]] | None = None,
    ) -> None:
        if snapshot is not None:
            _validate_drift_run_snapshot(*snapshot)
        with self._drift_lock:
            existing = self._monitor_series.get(series.series_id)
            if existing is not None:
                if existing != series:
                    raise ValueError("monitor series identity conflict")
                if snapshot is not None:
                    self._validate_snapshot_ownership_locked(*snapshot)
                    self._store_snapshot_locked(*snapshot)
                return
            if series.state == "active" and any(
                item.scope_key == series.scope_key and item.state == "active"
                for item in self._monitor_series.values()
            ):
                raise ValueError("monitor scope already has an active series")
            keys = [(member.series_id, member.trace_id) for member in members]
            if any(member.series_id != series.series_id for member in members) or len(keys) != len(
                set(keys)
            ):
                raise ValueError("invalid monitor membership")
            if snapshot is not None:
                self._validate_snapshot_ownership_locked(*snapshot)
            self._monitor_series[series.series_id] = copy.deepcopy(series)
            self._monitor_members.update(
                {key: copy.deepcopy(member) for key, member in zip(keys, members, strict=True)}
            )
            if snapshot is not None:
                self._store_snapshot_locked(*snapshot)

    def get_monitor_series(self, series_id: str) -> MonitorSeries | None:
        with self._drift_lock:
            return copy.deepcopy(self._monitor_series.get(series_id))

    def get_active_monitor_series(self, scope_key: str) -> MonitorSeries | None:
        with self._drift_lock:
            matches = [
                series
                for series in self._monitor_series.values()
                if series.scope_key == scope_key and series.state == "active"
            ]
            return copy.deepcopy(matches[0]) if matches else None

    def list_monitor_series(self, *, scope_key: str | None = None) -> list[MonitorSeries]:
        with self._drift_lock:
            rows = [
                series
                for series in self._monitor_series.values()
                if scope_key is None or series.scope_key == scope_key
            ]
            rows.sort(key=lambda series: (series.created_at, series.series_id))
            return copy.deepcopy(rows)

    def list_monitor_members(self, series_id: str) -> list[MonitorMember]:
        with self._drift_lock:
            rows = [
                member for (owner, _), member in self._monitor_members.items() if owner == series_id
            ]
            rows.sort(
                key=lambda member: (
                    member.role,
                    member.cluster_id,
                    member.bucket_index,
                    member.event_time,
                    member.trace_id,
                )
            )
            return copy.deepcopy(rows)

    def list_monitor_results(self, series_id: str) -> list[MonitorResult]:
        with self._drift_lock:
            rows = [
                result
                for (owner, _, _), result in self._monitor_results.items()
                if owner == series_id
            ]
            rows.sort(key=lambda result: (result.cluster_id, result.bucket_index))
            return copy.deepcopy(rows)

    def commit_monitor_cycle(
        self,
        *,
        series_id: str,
        expected_generation: int,
        members: list[MonitorMember],
        results: list[MonitorResult],
        snapshots: list[tuple[DriftRun, list[DriftSignal]]],
        late_arrival_delta: int,
    ) -> MonitorSeries:
        for run, signals in snapshots:
            _validate_drift_run_snapshot(run, signals)
        with self._drift_lock:
            series = self._monitor_series.get(series_id)
            if series is None or series.state != "active":
                raise ValueError("active monitor series not found")
            if series.generation != expected_generation:
                raise ValueError("monitor generation conflict")
            next_members = copy.deepcopy(self._monitor_members)
            next_results = copy.deepcopy(self._monitor_results)
            next_runs = copy.deepcopy(self._drift_runs)
            next_signals = copy.deepcopy(self._signals)
            for member in members:
                if member.series_id != series_id:
                    raise ValueError("monitor member belongs to another series")
                key = (series_id, member.trace_id)
                if (existing := next_members.get(key)) is not None and existing != member:
                    raise ValueError("monitor trace membership conflict")
                next_members[key] = copy.deepcopy(member)
            for result in results:
                if result.series_id != series_id:
                    raise ValueError("monitor result belongs to another series")
                key = (series_id, result.cluster_id, result.bucket_index)
                if (existing := next_results.get(key)) is not None and existing != result:
                    raise ValueError("monitor result identity conflict")
                next_results[key] = copy.deepcopy(result)
            for run, signals in snapshots:
                next_runs[run.run_id] = copy.deepcopy(run)
                next_signals = {
                    signal_id: signal
                    for signal_id, signal in next_signals.items()
                    if signal.run_id != run.run_id
                }
                next_signals.update({signal.signal_id: copy.deepcopy(signal) for signal in signals})
            updated = replace(
                series,
                generation=series.generation + 1,
                late_arrival_count=series.late_arrival_count + late_arrival_delta,
                updated_at=max(
                    [series.updated_at]
                    + [result.completed_at for result in results]
                    + [run.completed_at for run, _ in snapshots]
                ),
            )
            self._monitor_members = next_members
            self._monitor_results = next_results
            self._drift_runs = next_runs
            self._signals = next_signals
            self._monitor_series[series_id] = updated
            return copy.deepcopy(updated)

    def activate_monitor_series(
        self,
        series_id: str,
        *,
        expected_active_series_id: str,
        snapshot: tuple[DriftRun, list[DriftSignal]] | None = None,
    ) -> MonitorSeries:
        if snapshot is not None:
            _validate_drift_run_snapshot(*snapshot)
        with self._drift_lock:
            candidate = self._monitor_series.get(series_id)
            active = self._monitor_series.get(expected_active_series_id)
            if candidate is None or candidate.state not in {"candidate", "retired"}:
                raise ValueError("monitor candidate not found")
            if (
                active is None
                or active.state != "active"
                or active.scope_key != candidate.scope_key
            ):
                raise ValueError("active monitor compare-and-swap conflict")
            if snapshot is not None:
                self._validate_snapshot_ownership_locked(*snapshot)
            now = max(active.updated_at, candidate.updated_at)
            self._monitor_series[active.series_id] = replace(
                active, state="retired", generation=active.generation + 1, updated_at=now
            )
            activated = replace(
                candidate,
                state="active",
                generation=candidate.generation + 1,
                updated_at=now,
            )
            self._monitor_series[candidate.series_id] = activated
            if snapshot is not None:
                self._store_snapshot_locked(*snapshot)
            return copy.deepcopy(activated)

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
            fit_definition = json.loads(version.fit_definition_json)
            config = fit_definition.get("config", {})
            if fit_definition.get("selector") == "trace-manifest-v1":
                candidate_ids = [
                    trace_id
                    for (tenant, candidate_version, trace_id), assignment in (
                        self._trace_cluster_assignments.items()
                    )
                    if tenant == authorized_tenant
                    and candidate_version == version_id
                    and assignment.origin == "fit"
                ]
                rows = candidate_ids
                manifest = fit_definition.get("manifest", {})
                manifest_changed = (
                    len(candidate_ids) != manifest.get("candidate_count")
                    or cluster_candidate_digest(candidate_ids)
                    != manifest.get("candidate_digest")
                    or any(trace_id not in self._traces for trace_id in candidate_ids)
                )
            else:
                rows = self.list_cluster_trace_candidates(
                    authorized_tenant,
                    datetime_to_utc_us(version.cutoff - timedelta(days=version.lookback_days)),
                    datetime_to_utc_us(version.cutoff),
                    target_workload=config.get("target_workload"),
                    limit=config.get("max_fit_candidates", 50_000) + 1,
                )
                candidate_ids = [row.trace_id for row in rows if row.trace_id is not None]
                manifest_changed = False
            assigned_ids = {
                trace_id
                for tenant, candidate_version, trace_id in self._trace_cluster_assignments
                if tenant == authorized_tenant and candidate_version == version_id
            }
            if (
                manifest_changed
                or (
                    fit_definition.get("selector") != "trace-manifest-v1"
                    and self.count_pending_analysis_rows(authorized_tenant)
                )
                or len(rows) > config.get("max_fit_candidates", 50_000)
                or len(candidate_ids) != len(rows)
                or cluster_candidate_digest(candidate_ids) != expected_candidate_digest
                or not set(candidate_ids) <= assigned_ids
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
        self._cluster_registries.clear()
        self._cluster_identities.clear()
        self._cluster_versions.clear()
        self._cluster_version_clusters.clear()
        self._trace_cluster_assignments.clear()
        self._active_cluster_registries.clear()
        self._cluster_registry_events.clear()
        self._spans.clear()
        self._user_signals.clear()
