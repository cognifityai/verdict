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

from verdict.schema import (
    DriftSignal,
    EvaluatorHealthRecord,
    Judgment,
    SpanRecord,
    Trace,
    UserSignalRecord,
)


@dataclass(frozen=True)
class _Op:
    """One queued write plus acknowledgements of the *inner* write result."""

    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    on_success: Callable[[], None] | None = None
    on_failure: Callable[[BaseException], None] | None = None


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
        self.callback_errors = 0

        # Serializes actual calls into `inner`. The bg thread holds it while
        # draining; the sync fallback and read-flush paths take it too, so
        # there is never more than one thread inside `inner` at a time.
        self._write_lock = threading.Lock()

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
                self._queue.task_done()

            if barrier is not None:
                # All earlier FIFO items (including callback-derived writes, which
                # execute synchronously on this worker) are durable now. Later
                # producers cannot extend this flush.
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
            self._queue.task_done()

    def _execute(self, op: _Op) -> None:
        """Apply one write and notify its acknowledgement outside the lock."""
        try:
            with self._write_lock:
                op.fn(*op.args, **op.kwargs)
            self.written += 1
        except Exception as exc:
            self.write_errors += 1
            if op.on_failure is not None:
                try:
                    op.on_failure(exc)
                except Exception:
                    self.callback_errors += 1
            return

        if op.on_success is not None:
            try:
                op.on_success()
            except Exception:
                self.callback_errors += 1

    # -- internal: enqueue with sync fallback -------------------------------

    def _enqueue(
        self,
        fn: Callable[..., Any],
        *args: Any,
        _on_success: Callable[[], None] | None = None,
        _on_failure: Callable[[BaseException], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """Queue a write, or apply it synchronously if the queue is full.

        Non-blocking on the happy path. On overload, falls back to a synchronous
        write (counted in dropped_to_sync) rather than blocking the caller
        forever or dropping the datum.
        """
        op = _Op(fn, args, kwargs, _on_success, _on_failure)
        if self._closed or threading.current_thread() is self._thread:
            # After close() the thread is gone; be safe and write through.
            # Callback-derived writes also execute here on the worker so they
            # complete before a following flush barrier is acknowledged.
            self._execute(op)
            return
        try:
            self._queue.put_nowait(op)
            self.enqueued += 1
        except queue.Full:
            # Back-pressure: apply this one write inline. Not lost, just slow.
            self.dropped_to_sync += 1
            self._execute(op)

    # -- internal: flush ----------------------------------------------------

    def flush(self, timeout: float | None = None) -> None:
        """Block until all currently-enqueued writes have been applied.

        A FIFO barrier gives point-in-time semantics: writes enqueued after the
        barrier do not extend this wait. ``timeout`` bounds both placing and
        waiting for the barrier and raises ``TimeoutError`` if it expires.
        """
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

    def insert_trace_with_ack(
        self,
        trace: Trace,
        *,
        on_success: Callable[[], None],
        on_failure: Callable[[BaseException], None],
    ) -> None:
        """Enqueue a trace and acknowledge only the actual inner write result."""
        self._enqueue(
            self._inner.insert_trace,
            trace,
            _on_success=on_success,
            _on_failure=on_failure,
        )

    def insert_judgment(self, judgment: Judgment) -> None:
        self._enqueue(self._inner.insert_judgment, judgment)

    def insert_evaluator_health(self, record: EvaluatorHealthRecord) -> None:
        self._enqueue(self._inner.insert_evaluator_health, record)

    def insert_drift_signal(self, signal: DriftSignal) -> None:
        self._enqueue(self._inner.insert_drift_signal, signal)

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

    # ======================================================================
    # Storage Protocol — READ methods (flush-then-delegate)
    # ======================================================================

    def get_trace(self, trace_id: str) -> Trace | None:
        self.flush()
        with self._write_lock:
            return self._inner.get_trace(trace_id)

    def trace_exists(self, trace_id: str) -> bool:
        """Check durable inner state without flushing unrelated queued writes."""
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
        self.flush()
        with self._write_lock:
            return self._inner.list_traces(
                tenant_id=tenant_id, cluster_id=cluster_id, limit=limit
            )

    def list_judgments_for_cluster(
        self,
        cluster_id: str,
        *,
        since_iso: str | None = None,
        limit: int = 1000,
    ) -> list[Judgment]:
        self.flush()
        with self._write_lock:
            return self._inner.list_judgments_for_cluster(
                cluster_id, since_iso=since_iso, limit=limit
            )

    def list_drift_signals(self, *, limit: int = 100) -> list[DriftSignal]:
        self.flush()
        with self._write_lock:
            return self._inner.list_drift_signals(limit=limit)

    def list_evaluator_health(
        self,
        *,
        evaluator_fingerprint: str | None = None,
        limit: int = 100,
    ) -> list[EvaluatorHealthRecord]:
        self.flush()
        with self._write_lock:
            return self._inner.list_evaluator_health(
                evaluator_fingerprint=evaluator_fingerprint, limit=limit
            )

    def list_spans(
        self, *, trace_id: str | None = None, limit: int = 100
    ) -> list[SpanRecord]:
        self.flush()
        with self._write_lock:
            return self._inner.list_spans(trace_id=trace_id, limit=limit)

    def list_user_signals(self, *, limit: int = 1000) -> list[UserSignalRecord]:
        self.flush()
        with self._write_lock:
            return self._inner.list_user_signals(limit=limit)

    def load_cluster_registry(self, version: str) -> str | None:
        self.flush()
        with self._write_lock:
            return self._inner.load_cluster_registry(version)

    # ======================================================================
    # Storage Protocol — mutating-but-rare (flush-then-delegate, synchronous)
    # ======================================================================
    # These are not on the capture hot path. We flush first so they observe all
    # prior enqueued writes, then apply synchronously for straightforward
    # ordering semantics.

    def delete_trace(self, trace_id: str) -> None:
        self.flush()
        with self._write_lock:
            self._inner.delete_trace(trace_id)

    def prune_before(self, cutoff_iso: str) -> int:
        self.flush()
        with self._write_lock:
            return self._inner.prune_before(cutoff_iso)

    def save_cluster_registry(self, version: str, payload_json: str) -> None:
        self.flush()
        with self._write_lock:
            self._inner.save_cluster_registry(version, payload_json)

    # ======================================================================
    # Lifecycle
    # ======================================================================

    def close(self) -> None:
        """Flush remaining writes, stop the background thread, close ``inner``.

        Idempotent: safe to call more than once.
        """
        if self._closed:
            return
        # Prevent new writes from joining the async queue behind our barrier.
        self._closed = True
        # Flush what's already queued so nothing is lost.
        self.flush()
        # Signal shutdown and wake the drainer with a sentinel.
        self._stopping.set()
        try:
            self._queue.put_nowait(_SHUTDOWN)
        except queue.Full:
            # Queue somehow full; the timeout-based wakeup in _run will still
            # notice _stopping and exit once drained.
            pass
        self._thread.join(timeout=self._flush_interval * 4 + 5.0)
        # Any stragglers the thread didn't get to (shouldn't happen after
        # flush + join, but be safe): apply synchronously.
        self._drain_remaining()
        self._inner.close()
