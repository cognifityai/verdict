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
thread drains the queue and applies writes to ``inner`` in batches. Reads flush
the queue first, so read-after-write stays correct.

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

* **Reads flush.** ``get_trace`` / ``list_*`` / ``load_cluster_registry`` and the
  mutating-but-rare ``delete_trace`` / ``prune_before`` / ``save_cluster_registry``
  drain all pending writes before delegating, so a read never misses a write that
  was already enqueued. Simplicity over throughput: these are not hot-path calls.

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
from datetime import datetime
from typing import Any

from verdict.schema import DriftSignal, Judgment, SpanRecord, Trace, UserSignalRecord

# A queued write op: (bound-method-on-inner, args-tuple). The background thread
# just calls fn(*args). Keeping it this generic means adding a new write method
# is a one-line enqueue, no new op type.
_Op = tuple[Callable[..., Any], tuple[Any, ...]]

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
            else:
                batch.append(item)

            # Greedily pull up to batch_size-1 more without blocking, to batch.
            while not got_shutdown and len(batch) < self._batch_size:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is _SHUTDOWN:
                    self._queue.task_done()
                    got_shutdown = True
                    break
                batch.append(nxt)

            # Apply the batch. Each op is task_done'd individually so flush()'s
            # join() accounts for exactly the items it enqueued.
            for fn, args in batch:
                try:
                    with self._write_lock:
                        fn(*args)
                    self.written += 1
                except Exception:
                    # Never let a bad write kill the drain thread. Count and move on.
                    self.write_errors += 1
                finally:
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
            fn, args = item
            try:
                with self._write_lock:
                    fn(*args)
                self.written += 1
            except Exception:
                self.write_errors += 1
            finally:
                self._queue.task_done()

    # -- internal: enqueue with sync fallback -------------------------------

    def _enqueue(self, fn: Callable[..., Any], *args: Any) -> None:
        """Queue a write, or apply it synchronously if the queue is full.

        Non-blocking on the happy path. On overload, falls back to a synchronous
        write (counted in dropped_to_sync) rather than blocking the caller
        forever or dropping the datum.
        """
        if self._closed:
            # After close() the thread is gone; be safe and write through.
            self._sync_write(fn, *args)
            return
        try:
            self._queue.put_nowait((fn, args))
            self.enqueued += 1
        except queue.Full:
            # Back-pressure: apply this one write inline. Not lost, just slow.
            self.dropped_to_sync += 1
            self._sync_write(fn, *args)

    def _sync_write(self, fn: Callable[..., Any], *args: Any) -> None:
        try:
            with self._write_lock:
                fn(*args)
            self.written += 1
        except Exception:
            self.write_errors += 1

    # -- internal: flush ----------------------------------------------------

    def flush(self) -> None:
        """Block until all currently-enqueued writes have been applied.

        Uses Queue.join(), which blocks until every enqueued item has had
        task_done() called. Items enqueued *after* this call are not waited on.
        """
        self._queue.join()

    # ======================================================================
    # Storage Protocol — WRITE methods (async, enqueued)
    # ======================================================================

    def insert_trace(self, trace: Trace) -> None:
        self._enqueue(self._inner.insert_trace, trace)

    def insert_judgment(self, judgment: Judgment) -> None:
        self._enqueue(self._inner.insert_judgment, judgment)

    def insert_drift_signal(self, signal: DriftSignal) -> None:
        self._enqueue(self._inner.insert_drift_signal, signal)

    def delete_drift_signals_between(self, start: datetime, end: datetime) -> None:
        self._enqueue(self._inner.delete_drift_signals_between, start, end)

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
        self._closed = True
        # Any stragglers the thread didn't get to (shouldn't happen after
        # flush + join, but be safe): apply synchronously.
        self._drain_remaining()
        self._inner.close()
