"""Storage port — every adapter implements this Protocol.

Critically: this is a Protocol, not an ABC. Adapters need not inherit; they
just need to provide the methods. This keeps the port lightweight and avoids
import cycles.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from verdict.schema import (
    DriftSignal,
    EvaluatorHealthRecord,
    Judgment,
    SpanRecord,
    Trace,
    UserSignalRecord,
)


@runtime_checkable
class Storage(Protocol):
    """Persistent backing store for Traces, Judgments, and DriftSignals.

    All methods are sync for v0. An AsyncStorage Protocol may follow.
    """

    def insert_trace(self, trace: Trace) -> None: ...

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
