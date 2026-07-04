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

from verdict.schema import Trace
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
# Standalone harness (stdlib only) — runs every test above and reports.
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [
        test_writes_visible_after_flush,
        test_many_concurrent_writers_all_land,
        test_read_after_write_via_get_trace,
        test_read_after_write_via_list,
        test_close_flushes_remaining,
        test_close_flushes_remaining_observable,
        test_queue_full_falls_back_to_sync_no_loss,
        test_background_exception_counted_thread_survives,
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
