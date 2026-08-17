"""In-memory storage — the second adapter, used in tests.

The existence of this adapter is what makes Storage a real abstraction.
If we only had SQLiteStorage, the port wouldn't be exercised by alternative
implementations and would slowly couple to SQLite semantics.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime

from verdict.redaction import sanitize_judgment, sanitize_span, sanitize_trace
from verdict.schema import (
    DriftRun,
    DriftSignal,
    EvaluatorHealthRecord,
    Judgment,
    SpanRecord,
    Trace,
    UserSignalRecord,
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
        self._drift_lock = threading.RLock()
        self._cluster_registries: dict[str, str] = {}
        self._spans: dict[str, SpanRecord] = {}
        self._user_signals: dict[str, UserSignalRecord] = {}

    def insert_trace(self, trace: Trace) -> None:
        sanitize_trace(trace)
        # Match the SQL adapters' UPSERT semantics: a re-write that carries no
        # cluster_id (None) must not clobber an already-assigned one. The
        # clusterer assigns cluster_id after the trace is first written, and a
        # later content/usage update would otherwise erase it. (COALESCE parity
        # with sqlite/postgres ON CONFLICT.)
        existing = self._traces.get(trace.trace_id)
        if existing is not None:
            if trace.cluster_id is None and existing.cluster_id is not None:
                trace.cluster_id = existing.cluster_id
            if trace.parent_span_id is None and existing.parent_span_id is not None:
                trace.parent_span_id = existing.parent_span_id
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
        self._traces.pop(trace_id, None)
        retained_parent_span_ids = {
            parent_span_id
            for trace in self._traces.values()
            if (parent_span_id := trace.parent_span_id) is not None
        }
        self._judgments = {
            jid: j for jid, j in self._judgments.items() if j.trace_id != trace_id
        }
        for sid, record in self._spans.items():
            if record.trace_id == trace_id and sid in retained_parent_span_ids:
                record.trace_id = None
        self._spans = {
            sid: s
            for sid, s in self._spans.items()
            if s.trace_id != trace_id
            or sid in retained_parent_span_ids
        }
        self._user_signals = {
            sid: s for sid, s in self._user_signals.items() if s.trace_id != trace_id
        }

    def prune_before(self, cutoff_iso: str) -> int:
        doomed = [
            tid for tid, t in self._traces.items()
            if t.started_at.isoformat() < cutoff_iso
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
            if (
                record.trace_id in doomed_set
                and span_id in retained_parent_span_ids
            ):
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
                        and (
                            record.trace_id is None
                            or record.trace_id not in self._traces
                        )
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
                record for record in records
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
            if (
                existing is not None
                and existing.evaluator_fingerprint != run.evaluator_fingerprint
            ):
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
            replacement_signals.update({
                signal.signal_id: copy.deepcopy(signal) for signal in signals
            })
            self._signals = replacement_signals
            self._drift_runs[run.run_id] = copy.deepcopy(run)

    def get_latest_drift_run_snapshot(
        self,
        evaluator_fingerprint: str,
    ) -> tuple[DriftRun, list[DriftSignal]] | None:
        with self._drift_lock:
            candidates = [
                run for run in self._drift_runs.values()
                if run.evaluator_fingerprint == evaluator_fingerprint
            ]
            if not candidates:
                return None
            latest = max(
                candidates,
                key=lambda run: (run.analysis_time, run.completed_at, run.run_id),
            )
            signals = sorted(
                (
                    signal for signal in self._signals.values()
                    if signal.run_id == latest.run_id
                ),
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
                return (
                    start <= signal.detected_at < end
                    and (
                        evaluator_fingerprint is None
                        or signal.evaluator_fingerprint == evaluator_fingerprint
                    )
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
                if not (
                    signal.run_id in matched_run_ids
                    or (not signal.run_id and matches(signal))
                )
            }
            for run_id in matched_run_ids:
                self._drift_runs.pop(run_id, None)

    def list_drift_signals(self, *, limit: int = 100) -> list[DriftSignal]:
        with self._drift_lock:
            items = sorted(
                self._signals.values(), key=lambda s: s.detected_at, reverse=True
            )
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

    def close(self) -> None:
        self._traces.clear()
        self._judgments.clear()
        self._evaluator_health.clear()
        self._signals.clear()
        self._drift_runs.clear()
        self._cluster_registries.clear()
        self._spans.clear()
        self._user_signals.clear()
