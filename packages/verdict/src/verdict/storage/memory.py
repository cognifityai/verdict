"""In-memory storage — the second adapter, used in tests.

The existence of this adapter is what makes Storage a real abstraction.
If we only had SQLiteStorage, the port wouldn't be exercised by alternative
implementations and would slowly couple to SQLite semantics.
"""

from __future__ import annotations

from datetime import datetime

from verdict.schema import DriftSignal, Judgment, SpanRecord, Trace, UserSignalRecord


class InMemoryStorage:
    """Process-local, non-persistent storage. Loses everything on close()."""

    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}
        self._judgments: dict[str, Judgment] = {}
        self._signals: dict[str, DriftSignal] = {}
        self._cluster_registries: dict[str, str] = {}
        self._spans: dict[str, SpanRecord] = {}
        self._user_signals: dict[str, UserSignalRecord] = {}

    def insert_trace(self, trace: Trace) -> None:
        # Match the SQL adapters' UPSERT semantics: a re-write that carries no
        # cluster_id (None) must not clobber an already-assigned one. The
        # clusterer assigns cluster_id after the trace is first written, and a
        # later content/usage update would otherwise erase it. (COALESCE parity
        # with sqlite/postgres ON CONFLICT.)
        existing = self._traces.get(trace.trace_id)
        if existing is not None and trace.cluster_id is None and existing.cluster_id is not None:
            trace.cluster_id = existing.cluster_id
        self._traces[trace.trace_id] = trace

    def get_trace(self, trace_id: str) -> Trace | None:
        return self._traces.get(trace_id)

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
        self._judgments = {
            jid: j for jid, j in self._judgments.items() if j.trace_id != trace_id
        }
        self._spans = {
            sid: s for sid, s in self._spans.items() if s.trace_id != trace_id
        }
        self._user_signals = {
            sid: s for sid, s in self._user_signals.items() if s.trace_id != trace_id
        }

    def prune_before(self, cutoff_iso: str) -> int:
        doomed = [
            tid for tid, t in self._traces.items()
            if t.started_at.isoformat() < cutoff_iso
        ]
        for tid in doomed:
            self.delete_trace(tid)
        return len(doomed)

    def insert_judgment(self, judgment: Judgment) -> None:
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

    def insert_drift_signal(self, signal: DriftSignal) -> None:
        self._signals[signal.signal_id] = signal

    def delete_drift_signals_between(self, start: datetime, end: datetime) -> None:
        self._signals = {
            signal_id: signal
            for signal_id, signal in self._signals.items()
            if not (start <= signal.detected_at < end)
        }

    def list_drift_signals(self, *, limit: int = 100) -> list[DriftSignal]:
        items = sorted(self._signals.values(), key=lambda s: s.detected_at, reverse=True)
        return items[:limit]

    def insert_span(self, span: SpanRecord) -> None:
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
        self._signals.clear()
        self._cluster_registries.clear()
        self._spans.clear()
        self._user_signals.clear()
