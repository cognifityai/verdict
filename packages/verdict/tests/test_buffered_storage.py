"""Tests for BufferedStorage — the async/batched write buffer.

These run under pytest, but also stand alone (``python3 test_buffered_storage.py``)
using only the stdlib, wrapping the real InMemoryStorage. No scipy/sklearn/wrapt.

Coverage:
  * writes are eventually visible after flush(); many concurrent writers all land
  * read-after-write: insert then get_trace returns it (reads flush)
  * close() flushes remaining writes
  * queue-full fallback: some writes go dropped_to_sync, but NONE are lost
  * a background write exception is counted, thread survives, later writes work
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from verdict.schema import DriftRun, DriftSignal, Trace
from verdict.storage.buffered import BufferedStorage
from verdict.storage.memory import InMemoryStorage


def _trace(trace_id: str | None = None) -> Trace:
    t = Trace(provider="anthropic", request_model="claude-x")
    if trace_id is not None:
        t.trace_id = trace_id
    return t


# ---------------------------------------------------------------------------
# 1. Eventually-visible after flush + many concurrent writers all land
# ---------------------------------------------------------------------------

def test_writes_visible_after_flush():
    inner = InMemoryStorage()
    buf = BufferedStorage(inner, flush_interval=0.05, batch_size=10)
    try:
        for i in range(50):
            buf.insert_trace(_trace(f"t{i}"))
        buf.flush()
        # Read directly off inner (bypassing buf's read-flush) to prove the
        # background thread actually applied them.
        assert len(inner.list_traces(limit=1000)) == 50
        assert buf.written == 50
        assert buf.write_errors == 0
    finally:
        buf.close()


def test_each_queued_operation_reaches_inner_exactly_once():
    """Idempotent upserts must not hide duplicate worker invocations."""
    class CountingInner(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.insert_calls = 0

        def insert_trace(self, trace: Trace) -> None:
            self.insert_calls += 1
            super().insert_trace(trace)

    inner = CountingInner()
    buf = BufferedStorage(inner, flush_interval=10.0, batch_size=100)
    try:
        buf.insert_trace(_trace("exactly-once"))
        buf.flush()

        assert inner.insert_calls == 1
        assert buf.written == 1
    finally:
        buf.close()


def test_many_concurrent_writers_all_land():
    inner = InMemoryStorage()
    buf = BufferedStorage(inner, max_queue=10000, flush_interval=0.05, batch_size=50)
    n_threads = 8
    per_thread = 500
    expected = n_threads * per_thread

    def worker(tid: int) -> None:
        for i in range(per_thread):
            buf.insert_trace(_trace(f"w{tid}-{i}"))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    try:
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        buf.flush()
        got = len(inner.list_traces(limit=expected + 1000))
        assert got == expected, f"expected {expected}, got {got}"
        # Every accepted write is either written async or synced; none vanish.
        assert buf.written == expected
        assert buf.enqueued + buf.dropped_to_sync >= expected
    finally:
        buf.close()


# ---------------------------------------------------------------------------
# 2. Read-after-write correctness (reads flush first)
# ---------------------------------------------------------------------------

def test_read_after_write_via_get_trace():
    inner = InMemoryStorage()
    # Big flush_interval so if reads did NOT flush, the write would still be in
    # the queue and get_trace would return None.
    buf = BufferedStorage(inner, flush_interval=10.0, batch_size=100)
    try:
        buf.insert_trace(_trace("rw1"))
        got = buf.get_trace("rw1")  # must flush internally
        assert got is not None
        assert got.trace_id == "rw1"
    finally:
        buf.close()


def test_read_after_write_via_list():
    inner = InMemoryStorage()
    buf = BufferedStorage(inner, flush_interval=10.0)
    try:
        buf.insert_trace(_trace("l1"))
        buf.insert_trace(_trace("l2"))
        traces = buf.list_traces(limit=100)
        ids = {t.trace_id for t in traces}
        assert {"l1", "l2"} <= ids
    finally:
        buf.close()


# ---------------------------------------------------------------------------
# 3. close() flushes remaining writes
# ---------------------------------------------------------------------------

def test_close_flushes_remaining():
    inner = InMemoryStorage()
    buf = BufferedStorage(inner, flush_interval=10.0, batch_size=100)
    for i in range(37):
        buf.insert_trace(_trace(f"c{i}"))
    # Do NOT flush() explicitly — rely on close() to drain.
    buf.close()
    # inner is closed by BufferedStorage.close() -> InMemoryStorage.close()
    # clears everything, so we can't read from inner afterwards. Instead assert
    # via the counter that everything was written before close cleared it.
    assert buf.written == 37, f"expected 37 written, got {buf.written}"
    assert buf.write_errors == 0


def test_close_flushes_remaining_observable():
    # Same as above but with an inner that does NOT wipe on close, so we can
    # observe the rows survived the flush-on-close.
    class NonWipingInner(InMemoryStorage):
        def close(self) -> None:  # keep data around for assertion
            pass

    inner = NonWipingInner()
    buf = BufferedStorage(inner, flush_interval=10.0, batch_size=100)
    for i in range(37):
        buf.insert_trace(_trace(f"cc{i}"))
    buf.close()
    assert len(inner.list_traces(limit=1000)) == 37


# ---------------------------------------------------------------------------
# 4. Queue-full fallback: some go dropped_to_sync, NONE lost
# ---------------------------------------------------------------------------

def test_queue_full_falls_back_to_sync_no_loss():
    inner = InMemoryStorage()

    # Slow the inner insert so the background thread can't keep up and the queue
    # fills, forcing the sync fallback path.
    real_insert = inner.insert_trace
    slow_lock = threading.Lock()

    def slow_insert(trace: Trace) -> None:
        time.sleep(0.01)
        with slow_lock:
            real_insert(trace)

    inner.insert_trace = slow_insert  # type: ignore[method-assign]

    # Tiny queue so it fills almost immediately.
    buf = BufferedStorage(inner, max_queue=5, flush_interval=0.05, batch_size=2)
    total = 200
    try:
        for i in range(total):
            buf.insert_trace(_trace(f"q{i}"))
        buf.flush()
        got = len(inner.list_traces(limit=total + 100))
        assert got == total, f"lost writes: expected {total}, got {got}"
        # The whole point: overload actually exercised the sync fallback.
        assert buf.dropped_to_sync > 0, "expected some sync fallbacks under overload"
        # No datum lost: async-written + (dropped_to_sync all written) == total.
        assert buf.written == total, f"written {buf.written} != {total}"
        assert buf.write_errors == 0
    finally:
        # restore so close()'s inner.close() path is clean
        buf.close()


# ---------------------------------------------------------------------------
# 5. Background write exception is counted, thread survives, later writes work
# ---------------------------------------------------------------------------

def test_background_exception_counted_thread_survives():
    inner = InMemoryStorage()
    real_insert = inner.insert_trace
    boom_ids = {"boom1", "boom2"}

    def maybe_boom(trace: Trace) -> None:
        if trace.trace_id in boom_ids:
            raise RuntimeError("simulated write failure")
        real_insert(trace)

    inner.insert_trace = maybe_boom  # type: ignore[method-assign]

    buf = BufferedStorage(inner, flush_interval=0.05, batch_size=5)
    try:
        # Interleave a couple of poisoned writes with good ones.
        buf.insert_trace(_trace("ok1"))
        buf.insert_trace(_trace("boom1"))
        buf.insert_trace(_trace("ok2"))
        buf.insert_trace(_trace("boom2"))
        buf.flush()

        # Both poisoned writes counted as errors; thread still alive.
        assert buf.write_errors == 2, f"expected 2 errors, got {buf.write_errors}"
        assert buf._thread.is_alive(), "background thread died on exception"

        # Later good writes STILL land after the failures.
        buf.insert_trace(_trace("ok3"))
        buf.insert_trace(_trace("ok4"))
        buf.flush()

        ids = {t.trace_id for t in inner.list_traces(limit=100)}
        assert {"ok1", "ok2", "ok3", "ok4"} <= ids
        assert "boom1" not in ids and "boom2" not in ids
    finally:
        buf.close()


# ---------------------------------------------------------------------------
# 6. flush() is a point-in-time barrier, not a wait for future producers
# ---------------------------------------------------------------------------

def test_flush_does_not_wait_for_writes_enqueued_after_its_barrier():
    """A live producer must not be able to extend an in-progress flush forever."""
    inner = InMemoryStorage()
    first_started = threading.Event()
    release_first = threading.Event()
    late_started = threading.Event()
    release_late = threading.Event()
    real_insert = inner.insert_trace

    def controlled_insert(trace: Trace) -> None:
        if trace.trace_id == "first":
            first_started.set()
            assert release_first.wait(timeout=2.0)
        elif trace.trace_id == "late":
            late_started.set()
            assert release_late.wait(timeout=2.0)
        real_insert(trace)

    inner.insert_trace = controlled_insert  # type: ignore[method-assign]
    buf = BufferedStorage(inner, flush_interval=10.0, batch_size=100)
    flush_returned = threading.Event()
    late_enqueue_returned = threading.Event()

    def do_flush() -> None:
        buf.flush()
        flush_returned.set()

    def enqueue_late() -> None:
        buf.insert_trace(_trace("late"))
        late_enqueue_returned.set()

    try:
        buf.insert_trace(_trace("first"))
        assert first_started.wait(timeout=1.0)
        flush_thread = threading.Thread(target=do_flush)
        flush_thread.start()

        # The flush marker is now queued behind the blocked first write.
        deadline = time.monotonic() + 1.0
        while buf._queue.qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert buf._queue.qsize() >= 1
        late_thread = threading.Thread(target=enqueue_late)
        late_thread.start()
        assert not late_enqueue_returned.wait(timeout=0.05), (
            "producer crossed the lifecycle boundary during flush"
        )

        release_first.set()
        assert flush_returned.wait(timeout=1.0), (
            "flush incorrectly waited for a write queued after its barrier"
        )
        flush_thread.join(timeout=1.0)
        assert inner.get_trace("first") is not None

        # The producer can enqueue only after the snapshot flush releases the
        # lifecycle lock, so the later write cannot extend that flush.
        assert late_enqueue_returned.wait(timeout=1.0)
        assert late_started.wait(timeout=1.0)
        late_thread.join(timeout=1.0)
    finally:
        release_first.set()
        release_late.set()
        buf.close()


# ---------------------------------------------------------------------------
# 7. Keyword-only write contracts survive the async queue
# ---------------------------------------------------------------------------

def test_delete_drift_signals_forwards_keyword_only_evaluator_filter():
    """Buffered writes must preserve keyword-only adapter arguments.

    InMemoryStorage intentionally makes ``evaluator_fingerprint`` keyword-only.
    Passing it positionally makes the background write fail silently and leaves
    the stale drift signal in place.
    """
    inner = InMemoryStorage()
    buf = BufferedStorage(inner, flush_interval=10.0, batch_size=100)
    start = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    try:
        for signal_id, fingerprint in (("delete-me", "eval-a"), ("keep-me", "eval-b")):
            buf.insert_drift_signal(DriftSignal(
                signal_id=signal_id,
                detected_at=start,
                evaluator_fingerprint=fingerprint,
            ))

        buf.delete_drift_signals_between(
            start,
            end,
            evaluator_fingerprint="eval-a",
        )

        assert [signal.signal_id for signal in buf.list_drift_signals()] == ["keep-me"]
        assert buf.write_errors == 0
    finally:
        buf.close()


def test_flush_after_close_is_an_immediate_noop():
    buf = BufferedStorage(InMemoryStorage(), flush_interval=0.01)
    buf.close()

    # A timeout keeps the regression test finite on the old implementation,
    # whose worker is gone but whose flush still queues a barrier.
    buf.flush(timeout=0.05)


def test_reads_and_writes_after_close_raise_instead_of_touching_closed_inner():
    buf = BufferedStorage(InMemoryStorage(), flush_interval=0.01)
    buf.close()

    with pytest.raises(RuntimeError, match="closed"):
        buf.insert_trace(_trace("after-close"))
    with pytest.raises(RuntimeError, match="closed"):
        buf.trace_exists("after-close")


def test_close_drains_accepted_write_and_rejects_concurrent_late_operations():
    class ControlledInner(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.write_started = threading.Event()
            self.release_write = threading.Event()
            self.close_calls = 0

        def insert_trace(self, trace: Trace) -> None:
            self.write_started.set()
            assert self.release_write.wait(timeout=2.0)
            super().insert_trace(trace)

        def close(self) -> None:
            self.close_calls += 1

    inner = ControlledInner()
    buf = BufferedStorage(inner, flush_interval=10.0)
    buf.insert_trace(_trace("accepted-before-close"))
    assert inner.write_started.wait(timeout=1.0)

    close_returned = threading.Event()
    close_thread = threading.Thread(
        target=lambda: (buf.close(), close_returned.set())
    )
    close_thread.start()
    deadline = time.monotonic() + 1.0
    while not buf._closed and time.monotonic() < deadline:
        time.sleep(0.001)
    assert buf._closed

    late_errors = []

    def late_write() -> None:
        try:
            buf.insert_trace(_trace("late-write"))
        except Exception as exc:  # assertion inspects the exact type below
            late_errors.append(exc)

    def late_read() -> None:
        try:
            buf.trace_exists("accepted-before-close")
        except Exception as exc:  # assertion inspects the exact type below
            late_errors.append(exc)

    late_threads = [
        threading.Thread(target=late_write),
        threading.Thread(target=late_read),
    ]
    for thread in late_threads:
        thread.start()
    assert not close_returned.wait(timeout=0.05)

    inner.release_write.set()
    assert close_returned.wait(timeout=1.0)
    close_thread.join(timeout=1.0)
    for thread in late_threads:
        thread.join(timeout=1.0)

    assert inner.get_trace("accepted-before-close") is not None
    assert inner.get_trace("late-write") is None
    assert len(late_errors) == 2
    assert all(isinstance(error, RuntimeError) for error in late_errors)
    assert inner.close_calls == 1
    assert not buf._thread.is_alive()


def test_concurrent_close_calls_close_inner_once():
    class CountingCloseInner(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    inner = CountingCloseInner()
    buf = BufferedStorage(inner, flush_interval=0.01)
    gate = threading.Barrier(3)

    def close_together() -> None:
        gate.wait()
        buf.close()

    threads = [threading.Thread(target=close_together) for _ in range(2)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(timeout=1.0)

    assert all(not thread.is_alive() for thread in threads)
    assert inner.close_calls == 1


def test_buffered_drift_run_replacement_is_one_synchronous_inner_operation():
    class CountingRunInner(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.replace_calls = 0

        def replace_drift_run(self, run, signals) -> None:
            self.replace_calls += 1
            super().replace_drift_run(run, signals)

    inner = CountingRunInner()
    buf = BufferedStorage(inner, flush_interval=10.0)
    run = DriftRun(
        run_id="buffered-run",
        evaluator_fingerprint="buffered-evaluator",
        signal_count=1,
    )
    signal = DriftSignal(
        signal_id="buffered-signal",
        evaluator_fingerprint=run.evaluator_fingerprint,
        run_id=run.run_id,
    )
    try:
        buf.replace_drift_run(run, [signal])
        snapshot = inner.get_latest_drift_run_snapshot(run.evaluator_fingerprint)

        assert inner.replace_calls == 1
        assert snapshot is not None
        assert [item.signal_id for item in snapshot[1]] == [signal.signal_id]
        assert buf.enqueued == 0
    finally:
        buf.close()


# ---------------------------------------------------------------------------
# Standalone harness (stdlib only) — runs every test above and reports.
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [
        test_writes_visible_after_flush,
        test_each_queued_operation_reaches_inner_exactly_once,
        test_many_concurrent_writers_all_land,
        test_read_after_write_via_get_trace,
        test_read_after_write_via_list,
        test_close_flushes_remaining,
        test_close_flushes_remaining_observable,
        test_queue_full_falls_back_to_sync_no_loss,
        test_background_exception_counted_thread_survives,
        test_flush_does_not_wait_for_writes_enqueued_after_its_barrier,
        test_delete_drift_signals_forwards_keyword_only_evaluator_filter,
        test_flush_after_close_is_an_immediate_noop,
        test_reads_and_writes_after_close_raise_instead_of_touching_closed_inner,
        test_close_drains_accepted_write_and_rejects_concurrent_late_operations,
        test_concurrent_close_calls_close_inner_once,
        test_buffered_drift_run_replacement_is_one_synchronous_inner_operation,
    ]
    failures = 0
    for t in tests:
        name = t.__name__
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # unexpected
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
        else:
            print(f"PASS {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    import sys

    sys.exit(1 if _run_all() else 0)
