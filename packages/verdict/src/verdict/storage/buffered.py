"""BufferedStorage — async/batched write buffer in front of any Storage.

Motivation
----------
Today the instrumentors call ``storage.insert_trace(...)`` synchronously and
inline on the request hot path, behind a global lock. Every captured LLM call
pays the full write latency (a SQLite commit, a Postgres round-trip) before the
user's request can return. That violates the SDK-overhead budget in AGENTS.md
(p99 added latency < 2ms non-streaming).

``BufferedStorage`` wraps any ``Storage`` adapter and moves WRITES onto a single
background daemon thread. Callers enqueue and return immediately; the background
thread drains the queue and applies writes to ``inner`` in batches. Ordinary
reads flush the queue first, so read-after-write stays correct. The internal
durability check ``trace_exists`` deliberately does not flush unrelated writes.

Design choices (called out because they trade correctness for latency)
----------------------------------------------------------------------
* **One writer.** The background thread is the ONLY thing that calls the inner
  adapter for enqueued writes. Adapters like ``SQLiteStorage`` are not
  necessarily safe to write from many threads; funnelling every enqueued write
  through one thread sidesteps that entirely. Reads flush first (see below) and
  then call ``inner`` on the *calling* thread — that is safe because after a
  flush the background thread is idle w.r.t. the queue, and the inner adapters
  we ship (memory, sqlite via its own connection handling, postgres via a pool)
  tolerate a read concurrent with an idle writer. If a given inner adapter is
  strictly single-threaded, wrap it so all access is serialized; that is out of
  scope for the buffer itself.

* **Queue-full fallback = synchronous write, never drop.** Telemetry must not
  break the caller (so we don't raise or block forever when the queue is full),
  but it must not *silently* vanish either. When ``put_nowait`` fails because the
  queue is at ``max_queue``, we apply that single write synchronously on the
  calling thread and bump ``dropped_to_sync``. "dropped" here means "dropped out
  of the async fast-path", NOT "lost" — the datum is still written. This adds
  back-pressure latency to the caller only under sustained overload, which is the
  correct place to feel it.

* **Reads flush, except link checks.** ``get_trace`` / ``list_*`` /
  ``load_cluster_registry`` and the
  mutating-but-rare ``delete_trace`` / ``prune_before`` / ``save_cluster_registry``
  drain all pending writes before delegating, so a read never misses a write that
  was already enqueued. ``trace_exists`` checks durable state without flushing,
  because manual-span finalization is on the request path.

* **Background exceptions never kill the thread.** A write that raises is caught,
  counted in ``write_errors``, and the thread keeps draining. Losing the buffer
  thread would silently stop all persistence, which is worse than dropping one
  bad row.

Counters (all plain ints, read them for telemetry/tests):
    enqueued        - writes accepted onto the async queue
    written         - writes successfully applied to inner by the bg thread
    dropped_to_sync - writes applied synchronously because the queue was full
    write_errors    - writes that raised inside inner (async OR sync fallback)
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

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


@dataclass(frozen=True)
class _Op:
    """One queued storage write."""

    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class _FlushBarrier:
    """FIFO marker used to wait only for writes preceding a flush call."""

    reached: threading.Event

# Sentinel pushed on close() so the drain loop wakes up and exits promptly
# instead of waiting out a full flush_interval.
_SHUTDOWN = object()


class BufferedStorage:
    """Wrap a ``Storage`` and move write methods onto a background thread.

    Implements the full Storage Protocol by delegating to ``inner``. See the
    module docstring for the batching / back-pressure / read-flush semantics.
    """

    def __init__(
        self,
        inner: Any,  # Storage (Protocol; avoid importing for isinstance)
        *,
        max_queue: int = 10000,
        flush_interval: float = 0.5,
        batch_size: int = 100,
    ) -> None:
        self._inner = inner
        self._flush_interval = flush_interval
        self._batch_size = max(1, batch_size)

        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_queue)

        # Counters. Only ever incremented; ints are atomic enough for read-only
        # telemetry, but we still guard writes to inner with _write_lock so the
        # sync fallback and the bg thread don't call inner concurrently.
        self.enqueued = 0
        self.written = 0
        self.dropped_to_sync = 0
        self.write_errors = 0
        self._pending_writes = 0
        self._metrics_lock = threading.Lock()

        # Serializes actual calls into `inner`. The bg thread holds it while
        # draining; the sync fallback and read-flush paths take it too, so
        # there is never more than one thread inside `inner` at a time.
        self._write_lock = threading.Lock()
        # One lifecycle boundary makes check+enqueue, flush/read, and close
        # mutually ordered. The worker deliberately never takes this lock: it
        # must be able to reach a flush barrier while the caller waits here.
        self._lifecycle_lock = threading.RLock()

        self._stopping = threading.Event()
        # Signalled by the drainer each time queue.unfinished_tasks hits zero,
        # so flush() can block precisely rather than poll.
        self._closed = False

        self._thread = threading.Thread(
            target=self._run,
            name="verdict-buffered-storage",
            daemon=True,
        )
        self._thread.start()

    # -- internal: the background drain loop --------------------------------

    def _run(self) -> None:
        while True:
            batch: list[_Op] = []
            got_shutdown = False
            barrier: _FlushBarrier | None = None

            # Block for the first item (up to flush_interval) so an idle buffer
            # doesn't spin. timeout=None-ish: we bound the wait so the loop can
            # notice _stopping even without a sentinel.
            try:
                item = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                # No work this interval. If we're shutting down and the queue is
                # truly empty, exit; otherwise loop and wait again.
                if self._stopping.is_set() and self._queue.empty():
                    return
                continue

            if item is _SHUTDOWN:
                self._queue.task_done()
                got_shutdown = True
            elif isinstance(item, _FlushBarrier):
                barrier = item
            else:
                batch.append(item)

            # Greedily pull up to batch_size-1 more without blocking, to batch.
            while (
                not got_shutdown
                and barrier is None
                and len(batch) < self._batch_size
            ):
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is _SHUTDOWN:
                    self._queue.task_done()
                    got_shutdown = True
                    break
                if isinstance(nxt, _FlushBarrier):
                    barrier = nxt
                    break
                batch.append(nxt)

            # Apply the batch. Each op is task_done'd individually so flush()'s
            # join() accounts for exactly the items it enqueued.
            for op in batch:
                self._execute(op)
                with self._metrics_lock:
                    self._pending_writes -= 1
                self._queue.task_done()

            if barrier is not None:
                # All earlier FIFO items are durable now. Later producers cannot
                # extend this flush.
                barrier.reached.set()
                self._queue.task_done()

            if got_shutdown:
                # Drain anything already queued before the sentinel arrived so
                # close() loses nothing, then exit.
                self._drain_remaining()
                return

    def _drain_remaining(self) -> None:
        """Apply every remaining queued op (called on shutdown)."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _SHUTDOWN:
                self._queue.task_done()
                continue
            if isinstance(item, _FlushBarrier):
                item.reached.set()
                self._queue.task_done()
                continue
            self._execute(item)
            with self._metrics_lock:
                self._pending_writes -= 1
            self._queue.task_done()

    def _execute(self, op: _Op) -> None:
        """Apply one write without letting failures kill the worker."""
        try:
            with self._write_lock:
                op.fn(*op.args, **op.kwargs)
            with self._metrics_lock:
                self.written += 1
        except Exception:
            with self._metrics_lock:
                self.write_errors += 1

    # -- internal: enqueue with sync fallback -------------------------------

    def _enqueue(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Queue a write, or apply it synchronously if the queue is full.

        Non-blocking on the happy path. On overload, falls back to a synchronous
        write (counted in dropped_to_sync) rather than blocking the caller
        forever or dropping the datum.
        """
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("BufferedStorage is closed")
            op = _Op(fn, args, kwargs)
            if threading.current_thread() is self._thread:
                self._execute(op)
                return
            try:
                self._queue.put_nowait(op)
                with self._metrics_lock:
                    self.enqueued += 1
                    self._pending_writes += 1
            except queue.Full:
                # Back-pressure: apply this one write inline. Not lost, just slow.
                with self._metrics_lock:
                    self.dropped_to_sync += 1
                self._execute(op)

    def telemetry_snapshot(self) -> dict[str, int | bool]:
        """Return non-blocking process-local queue and outcome counters."""
        with self._metrics_lock:
            return {
                "enabled": True,
                "queue_depth": self._pending_writes,
                "accepted": self.enqueued,
                "written": self.written,
                "sync_fallbacks": self.dropped_to_sync,
                "write_errors": self.write_errors,
                "closed": self._closed,
            }

    # -- internal: flush ----------------------------------------------------

    def flush(self, timeout: float | None = None) -> None:
        """Block until all currently-enqueued writes have been applied.

        A FIFO barrier gives point-in-time semantics: writes enqueued after the
        barrier do not extend this wait. ``timeout`` bounds both placing and
        waiting for the barrier and raises ``TimeoutError`` if it expires.
        """
        with self._lifecycle_lock:
            if self._closed:
                return
            self._flush_locked(timeout)

    def _flush_locked(self, timeout: float | None = None) -> None:
        """Flush accepted work while the caller holds ``_lifecycle_lock``."""
        if threading.current_thread() is self._thread:
            return
        barrier = _FlushBarrier(threading.Event())
        try:
            self._queue.put(barrier, timeout=timeout)
        except queue.Full as exc:
            raise TimeoutError("timed out while enqueueing storage flush barrier") from exc
        if not barrier.reached.wait(timeout=timeout):
            raise TimeoutError("timed out waiting for buffered storage flush")

    # ======================================================================
    # Storage Protocol — WRITE methods (async, enqueued)
    # ======================================================================

    def insert_trace(self, trace: Trace) -> None:
        self._enqueue(self._inner.insert_trace, trace)

    def insert_judgment(self, judgment: Judgment) -> None:
        self._enqueue(self._inner.insert_judgment, judgment)

    def insert_evaluator_health(self, record: EvaluatorHealthRecord) -> None:
        self._enqueue(self._inner.insert_evaluator_health, record)

    def insert_drift_signal(self, signal: DriftSignal) -> None:
        self._enqueue(self._inner.insert_drift_signal, signal)

    def replace_drift_run(
        self,
        run: DriftRun,
        signals: list[DriftSignal],
    ) -> None:
        # A completed run and its exact signal set are one transaction. Queueing
        # delete/insert fragments would expose partial or signal-less snapshots.
        self._maintenance(self._inner.replace_drift_run, run, signals)

    def delete_drift_signals_between(
        self,
        start: datetime,
        end: datetime,
        *,
        evaluator_fingerprint: str | None = None,
    ) -> None:
        self._enqueue(
            self._inner.delete_drift_signals_between,
            start,
            end,
            evaluator_fingerprint=evaluator_fingerprint,
        )

    def insert_span(self, span: SpanRecord) -> None:
        self._enqueue(self._inner.insert_span, span)

    def insert_user_signal(self, sig: UserSignalRecord) -> None:
        self._enqueue(self._inner.insert_user_signal, sig)

    def replace_agent_run_bundle(self, bundle: AgentRunBundle) -> None:
        # A normalized run is one replacement snapshot; never queue fragments.
        self._maintenance(self._inner.replace_agent_run_bundle, bundle)

    def save_deterministic_analysis_run(self, run: DeterministicAnalysisRun) -> None:
        # Terminal immutable snapshots are rare control-plane writes. Persist
        # synchronously so callers never announce analysis that is only queued.
        self._maintenance(self._inner.save_deterministic_analysis_run, run)

    def save_notification_delivery_attempt(
        self, attempt: NotificationDeliveryAttempt,
    ) -> None:
        # The durable terminal outcome is part of retry ownership, not capture
        # telemetry, so it must be visible before the caller attempts a retry.
        self._maintenance(self._inner.save_notification_delivery_attempt, attempt)

    # ======================================================================
    # Storage Protocol — READ methods (flush-then-delegate)
    # ======================================================================

    def get_trace(self, trace_id: str) -> Trace | None:
        return self._read(self._inner.get_trace, trace_id)

    def get_agent_run_bundle(
        self,
        tenant_id: str,
        run_id: str,
    ) -> AgentRunBundle | None:
        return self._read(self._inner.get_agent_run_bundle, tenant_id, run_id)

    def list_agent_run_bundles(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> list[AgentRunBundle]:
        return self._read(
            self._inner.list_agent_run_bundles,
            tenant_id,
            limit=limit,
        )

    def get_latest_deterministic_analysis_run(
        self, tenant_id: str, scope_key: str,
    ) -> DeterministicAnalysisRun | None:
        return self._read(
            self._inner.get_latest_deterministic_analysis_run, tenant_id, scope_key,
        )

    def list_notification_delivery_attempts(
        self,
        notification_id: str,
        destination_fingerprint: str,
        *,
        limit: int = 100,
    ) -> list[NotificationDeliveryAttempt]:
        return self._read(
            self._inner.list_notification_delivery_attempts,
            notification_id,
            destination_fingerprint,
            limit=limit,
        )

    def notification_was_delivered(
        self, notification_id: str, destination_fingerprint: str,
    ) -> bool:
        return self._read(
            self._inner.notification_was_delivered,
            notification_id,
            destination_fingerprint,
        )

    def list_notification_delivery_attempts_for_tenant(
        self, tenant_id: str, *, limit: int = 100,
    ) -> list[NotificationDeliveryAttempt]:
        return self._read(
            self._inner.list_notification_delivery_attempts_for_tenant,
            tenant_id,
            limit=limit,
        )

    def get_monitor_policy(self, policy_id: str) -> tuple[MonitorPolicy, str] | None:
        return self._read(self._inner.get_monitor_policy, policy_id)

    def get_active_monitor_policy(self, scope_key: str) -> MonitorPolicy | None:
        return self._read(self._inner.get_active_monitor_policy, scope_key)

    def get_latest_monitor_snapshot(
        self, policy_id: str
    ) -> tuple[CohortManifest, MonitorComparison] | None:
        return self._read(self._inner.get_latest_monitor_snapshot, policy_id)

    def get_latest_monitor_alert(self, policy_id: str):
        return self._read(self._inner.get_latest_monitor_alert, policy_id)

    def trace_exists(self, trace_id: str) -> bool:
        """Check durable inner state without flushing unrelated queued writes."""
        with self._lifecycle_lock:
            self._require_open_locked()
            with self._write_lock:
                exists = getattr(self._inner, "trace_exists", None)
                if callable(exists):
                    return bool(exists(trace_id))
                return self._inner.get_trace(trace_id) is not None

    def list_traces(
        self,
        *,
        tenant_id: str | None = None,
        cluster_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        return self._read(
            self._inner.list_traces,
            tenant_id=tenant_id,
            cluster_id=cluster_id,
            limit=limit,
        )

    def list_judgments_for_cluster(
        self,
        cluster_id: str,
        *,
        since_iso: str | None = None,
        limit: int = 1000,
    ) -> list[Judgment]:
        return self._read(
            self._inner.list_judgments_for_cluster,
            cluster_id,
            since_iso=since_iso,
            limit=limit,
        )

    def list_judgments_for_trace(
        self, trace_id: str, *, limit: int = 100,
    ) -> list[Judgment]:
        return self._read(self._inner.list_judgments_for_trace, trace_id, limit=limit)

    def has_completed_judgment(
        self, trace_id: str, evaluator_fingerprint: str,
    ) -> bool:
        return self._read(
            self._inner.has_completed_judgment, trace_id, evaluator_fingerprint
        )

    def list_drift_signals(self, *, limit: int = 100) -> list[DriftSignal]:
        return self._read(self._inner.list_drift_signals, limit=limit)

    def get_latest_drift_run_snapshot(
        self,
        evaluator_fingerprint: str,
    ) -> tuple[DriftRun, list[DriftSignal]] | None:
        return self._read(
            self._inner.get_latest_drift_run_snapshot,
            evaluator_fingerprint,
        )

    def list_evaluator_health(
        self,
        *,
        evaluator_fingerprint: str | None = None,
        limit: int = 100,
    ) -> list[EvaluatorHealthRecord]:
        return self._read(
            self._inner.list_evaluator_health,
            evaluator_fingerprint=evaluator_fingerprint,
            limit=limit,
        )

    def list_spans(
        self, *, trace_id: str | None = None, limit: int = 100
    ) -> list[SpanRecord]:
        return self._read(self._inner.list_spans, trace_id=trace_id, limit=limit)

    def list_user_signals(self, *, limit: int = 1000) -> list[UserSignalRecord]:
        return self._read(self._inner.list_user_signals, limit=limit)

    def load_cluster_registry(self, version: str) -> str | None:
        return self._read(self._inner.load_cluster_registry, version)

    # ======================================================================
    # Storage Protocol — mutating-but-rare (flush-then-delegate, synchronous)
    # ======================================================================
    # These are not on the capture hot path. We flush first so they observe all
    # prior enqueued writes, then apply synchronously for straightforward
    # ordering semantics.

    def _require_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("BufferedStorage is closed")

    def _read(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self._lifecycle_lock:
            self._require_open_locked()
            self._flush_locked()
            with self._write_lock:
                return fn(*args, **kwargs)

    def _maintenance(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        with self._lifecycle_lock:
            self._require_open_locked()
            self._flush_locked()
            with self._write_lock:
                return fn(*args, **kwargs)

    def delete_trace(self, trace_id: str) -> None:
        self._maintenance(self._inner.delete_trace, trace_id)

    def prune_before(self, cutoff_iso: str) -> int:
        return self._maintenance(self._inner.prune_before, cutoff_iso)

    def save_cluster_registry(self, version: str, payload_json: str) -> None:
        self._maintenance(self._inner.save_cluster_registry, version, payload_json)

    def save_monitor_policy(self, policy: MonitorPolicy) -> None:
        self._maintenance(self._inner.save_monitor_policy, policy)

    def activate_monitor_policy(
        self, scope_key: str, policy_id: str, *, expected_active_policy_id: str | None
    ) -> MonitorPolicy:
        return self._maintenance(
            self._inner.activate_monitor_policy,
            scope_key,
            policy_id,
            expected_active_policy_id=expected_active_policy_id,
        )

    def save_monitor_snapshot(
        self, policy_id: str, manifest: CohortManifest, comparison: MonitorComparison
    ) -> None:
        self._maintenance(
            self._inner.save_monitor_snapshot, policy_id, manifest, comparison,
        )

    # ======================================================================
    # Lifecycle
    # ======================================================================

    def close(self) -> None:
        """Flush remaining writes, stop the background thread, close ``inner``.

        Idempotent: safe to call more than once.
        """
        if threading.current_thread() is self._thread:
            raise RuntimeError("BufferedStorage cannot close from its worker thread")
        with self._lifecycle_lock:
            if self._closed:
                return
            # Reject new operations before draining. _flush_locked is private
            # specifically so close can flush accepted work in this state.
            self._closed = True
            self._flush_locked()
            self._queue.put(_SHUTDOWN)
            self._stopping.set()
            self._thread.join()
            self._inner.close()
