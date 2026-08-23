"""Storage port — every adapter implements this Protocol.

Critically: this is a Protocol, not an ABC. Adapters need not inherit; they
just need to provide the methods. This keeps the port lightweight and avoids
import cycles.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from verdict.schema import (
    ConversationDriftRun,
    ConversationDriftSample,
    ConversationDriftSignal,
    ConversationTraceCandidate,
    ConversationTraceContent,
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
        raise ValueError("drift run signal_count does not match the provided signal list")
    signal_ids: set[str] = set()
    for signal in signals:
        if signal.run_id != run.run_id:
            raise ValueError("every drift signal must reference the owning run_id")
        if signal.evaluator_fingerprint != run.evaluator_fingerprint:
            raise ValueError("every drift signal must match the run evaluator_fingerprint")
        if signal.signal_id in signal_ids:
            raise ValueError("drift run contains duplicate signal_id values")
        signal_ids.add(signal.signal_id)


def _validate_conversation_drift_snapshot(
    run: ConversationDriftRun,
    samples: list[ConversationDriftSample],
    signals: list[ConversationDriftSignal],
) -> None:
    """Validate one immutable conversation snapshot before adapter writes."""
    import json

    if run.sample_count != len(samples) or run.signal_count != len(signals):
        raise ValueError("conversation snapshot counts do not match its rows")
    expected_dimensions = set(json.loads(run.evaluator_definition_json)["dimensions"])
    sample_keys: set[tuple[str, int]] = set()
    trace_ids: set[str] = set()
    for sample in samples:
        if (
            sample.tenant_id != run.tenant_id
            or sample.run_id != run.run_id
            or sample.registry_version != run.registry_version
        ):
            raise ValueError("conversation sample does not match its owning run")
        if not run.started_at <= sample.attempt_terminal_at <= run.completed_at:
            raise ValueError("conversation sample attempt terminal time is invalid")
        lower, upper = (
            (run.baseline_start, run.baseline_end)
            if sample.window == "baseline"
            else (run.current_start, run.current_end)
        )
        if not lower <= sample.event_time < upper:
            raise ValueError("conversation sample event time is outside its window")
        outcomes = json.loads(sample.outcomes_json)
        if sample.attempt_status == "completed" and set(outcomes) != expected_dimensions:
            raise ValueError("conversation sample outcomes do not match expected dimensions")
        key = (sample.cluster_id, sample.session_ordinal)
        if key in sample_keys:
            raise ValueError("conversation snapshot repeats a session within one cluster")
        if sample.trace_id in trace_ids:
            raise ValueError("conversation snapshot repeats a trace")
        sample_keys.add(key)
        trace_ids.add(sample.trace_id)

    signal_ids: set[str] = set()
    hypotheses: set[tuple[str, str, str, str]] = set()
    for signal in signals:
        if (
            signal.tenant_id != run.tenant_id
            or signal.run_id != run.run_id
            or signal.registry_version != run.registry_version
        ):
            raise ValueError("conversation signal does not match its owning run")
        if signal.dimension not in expected_dimensions:
            raise ValueError("conversation signal dimension is not expected")
        hypothesis = (
            signal.cluster_id,
            signal.dimension,
            signal.statistic_name,
            signal.direction,
        )
        if signal.signal_id in signal_ids or hypothesis in hypotheses:
            raise ValueError("conversation snapshot contains duplicate signals")
        signal_ids.add(signal.signal_id)
        hypotheses.add(hypothesis)


@runtime_checkable
class ConversationDriftStorage(Protocol):
    """Optional built-in capability for immutable conversation snapshots."""

    def insert_conversation_drift_snapshot(
        self,
        run: ConversationDriftRun,
        samples: list[ConversationDriftSample],
        signals: list[ConversationDriftSignal],
    ) -> None: ...

    def get_conversation_drift_snapshot(
        self,
        authorized_tenant: str,
        run_id: str,
    ) -> (
        tuple[
            ConversationDriftRun,
            list[ConversationDriftSample],
            list[ConversationDriftSignal],
        ]
        | None
    ): ...

    def list_conversation_drift_runs(
        self,
        authorized_tenant: str,
        *,
        limit: int = 100,
    ) -> list[ConversationDriftRun]: ...

    def list_conversation_trace_candidates(
        self,
        authorized_tenant: str,
        registry_version: str,
        baseline_start_us: int,
        current_end_us: int,
        *,
        target_workload: str,
        limit: int,
    ) -> list[ConversationTraceCandidate]: ...

    def get_conversation_trace_contents(
        self,
        authorized_tenant: str,
        trace_ids: list[str],
    ) -> dict[str, ConversationTraceContent]: ...


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
        self,
        *,
        evaluator_fingerprint: str | None = None,
        limit: int = 100,
    ) -> list[EvaluatorHealthRecord]: ...

    def insert_drift_signal(self, signal: DriftSignal) -> None: ...

    def replace_drift_run(
        self,
        run: DriftRun,
        signals: list[DriftSignal],
    ) -> None: ...

    def get_latest_drift_run_snapshot(
        self,
        evaluator_fingerprint: str,
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
        self,
        *,
        trace_id: str | None = None,
        limit: int = 100,
    ) -> list[SpanRecord]: ...

    # -- User signals (thumbs/regenerate/abandon, for the correlator) -------

    def insert_user_signal(self, sig: UserSignalRecord) -> None: ...

    def list_user_signals(self, *, limit: int = 1000) -> list[UserSignalRecord]: ...

    def close(self) -> None: ...
