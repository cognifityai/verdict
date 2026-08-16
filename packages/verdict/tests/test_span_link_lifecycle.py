"""Regression tests for the manual-span trace-link lifecycle.

Every test here is a counterexample to a defect that shipped: each one fails on
the pre-fix code for the reason it names. The invariant they collectively pin is:

    A span that ends is ALWAYS written, exactly once, and never carries a link
    to a trace that failed to persist.

Deferring the write until the provider trace is acknowledged violates the first
half (the acknowledgement walks a span's ancestors, so siblings and children of
the provider call are never reached). Writing without retraction violates the
second half.
"""

from __future__ import annotations

import asyncio

import pytest
import verdict
import verdict.client as client_module
from verdict.instrumentors.base import apply_routing_context, persist_trace
from verdict.schema import Trace
from verdict.storage import BufferedStorage, InMemoryStorage
from verdict.trace import TraceLinkState, span, trace_context


class FailingTraceStore(InMemoryStorage):
    """In-memory storage whose trace writes always fail. Spans still persist."""

    def insert_trace(self, trace: Trace) -> None:
        raise RuntimeError("simulated durable write failure")


@pytest.fixture(autouse=True)
def _reset_client():
    client_module._client = None
    yield
    client_module._client = None


def _init(storage):
    verdict.init(storage=storage, service_name="lifecycle-test")
    return client_module.get_client()


def _fake_provider_call(client, storage, trace_id: str) -> Trace:
    """Drive the real instrumentor sequence for one captured LLM call."""
    trace = Trace(trace_id=trace_id)
    apply_routing_context(client, trace)
    persist_trace(client, trace)
    return trace


def _spans_by_name(storage):
    return {s.name: s for s in storage.list_spans()}


# --------------------------------------------------------------------------
# 1. The blocker: spans opened after a provider call must not be dropped.
# --------------------------------------------------------------------------

def test_sibling_spans_after_provider_call_are_persisted_when_ack_is_async():
    """Siblings/children of a provider call are NOT on the ack's ancestor walk.

    Pre-fix these stayed INHERITED_PENDING_PROVIDER forever and were never
    written at all. This is the 74%-loss case in a real agent loop.
    """
    inner = InMemoryStorage()
    storage = BufferedStorage(inner)
    client = _init(storage)

    with span("agent_turn"):
        _fake_provider_call(client, storage, "TRACE-1")
        with span("retrieve_documents"):
            pass
        with span("rerank"):
            pass
        with span("execute_tool"):
            pass
    storage.flush()

    persisted = _spans_by_name(storage)
    assert set(persisted) == {
        "agent_turn",
        "retrieve_documents",
        "rerank",
        "execute_tool",
    }
    # And they are linked to the trace, not merely written unlinked.
    for name in ("retrieve_documents", "rerank", "execute_tool"):
        assert persisted[name].trace_id == "TRACE-1", name


def test_nested_agent_loop_persists_every_span():
    """Quantified version of the same defect across many turns."""
    inner = InMemoryStorage()
    storage = BufferedStorage(inner)
    client = _init(storage)

    turns = 20
    for i in range(turns):
        with span(f"turn_{i}"):
            _fake_provider_call(client, storage, f"TRACE-{i}")
            with span(f"retrieve_{i}"):
                pass
            with span(f"rerank_{i}"):
                pass
            with span(f"tool_{i}"):
                pass
    storage.flush()

    assert len(storage.list_spans()) == turns * 4


# --------------------------------------------------------------------------
# 2. The property the deferral existed to protect must still hold.
# --------------------------------------------------------------------------

def test_failed_trace_write_retracts_links_on_already_written_spans():
    """Writing optimistically must not leave a dangling link behind."""
    inner = FailingTraceStore()
    storage = BufferedStorage(inner)
    client = _init(storage)

    with span("agent_turn"):
        _fake_provider_call(client, storage, "TRACE-DEAD")
        with span("retrieve_documents"):
            pass
    storage.flush()

    persisted = _spans_by_name(storage)
    assert set(persisted) == {"agent_turn", "retrieve_documents"}
    assert inner.get_trace("TRACE-DEAD") is None
    for name, record in persisted.items():
        assert record.trace_id is None, f"{name} kept a dangling link"
        assert (
            record.attributes.get("verdict.link_status") == "trace_write_failed"
        ), f"{name} lost its retraction breadcrumb"


def test_failed_trace_write_retracts_links_with_synchronous_storage():
    """Same guarantee on the non-buffered path."""
    storage = FailingTraceStore()
    client = _init(storage)

    with span("outer"):
        trace = Trace(trace_id="TRACE-SYNC")
        apply_routing_context(client, trace)
        with pytest.raises(RuntimeError):
            persist_trace(client, trace)
        with span("inner"):
            pass

    persisted = _spans_by_name(storage)
    assert set(persisted) == {"outer", "inner"}
    assert all(record.trace_id is None for record in persisted.values())


def test_successful_trace_write_leaves_links_intact():
    inner = InMemoryStorage()
    storage = BufferedStorage(inner)
    client = _init(storage)

    with span("outer"):
        _fake_provider_call(client, storage, "TRACE-OK")
        with span("inner"):
            pass
    storage.flush()

    persisted = _spans_by_name(storage)
    assert persisted["outer"].trace_id == "TRACE-OK"
    assert persisted["inner"].trace_id == "TRACE-OK"
    assert "verdict.link_status" not in persisted["inner"].attributes


# --------------------------------------------------------------------------
# 3. Adjacent invariants that previous rounds regressed.
# --------------------------------------------------------------------------

def test_span_write_does_not_flush_the_buffer():
    """Verifying a link must not drain the async write queue on the hot path."""
    inner = InMemoryStorage()
    storage = BufferedStorage(inner)
    calls = {"flush": 0}
    original_flush = storage.flush
    storage.flush = lambda *a, **k: (  # type: ignore[method-assign]
        calls.__setitem__("flush", calls["flush"] + 1),
        original_flush(*a, **k),
    )[1]
    _init(storage)
    inner.insert_trace(Trace(trace_id="T1"))

    with trace_context(trace_id="T1"):
        for i in range(25):
            with span(f"retrieve_{i}"):
                pass

    assert calls["flush"] == 0


def test_nested_span_honours_its_own_trace_context():
    storage = InMemoryStorage()
    _init(storage)
    storage.insert_trace(Trace(trace_id="REAL"))

    with span("outer"):
        with trace_context(trace_id="REAL"):
            with span("inner"):
                pass

    assert _spans_by_name(storage)["inner"].trace_id == "REAL"


def test_explicit_link_to_missing_trace_is_dropped_with_a_breadcrumb():
    storage = InMemoryStorage()
    _init(storage)

    with trace_context(trace_id="NEVER-WRITTEN"):
        with span("orphan"):
            pass

    record = _spans_by_name(storage)["orphan"]
    assert record.trace_id is None
    assert record.attributes.get("verdict.link_status") == "trace_not_found"


def test_registry_is_drained_after_acknowledgement():
    """A resolved trace must not retain registry entries (unbounded growth)."""
    from verdict.trace import _unconfirmed_links

    inner = InMemoryStorage()
    storage = BufferedStorage(inner)
    client = _init(storage)

    for i in range(50):
        with span(f"outer_{i}"):
            _fake_provider_call(client, storage, f"TRACE-{i}")
            with span(f"inner_{i}"):
                pass
    storage.flush()

    assert _unconfirmed_links == {}


def test_concurrent_tasks_do_not_cross_attribute_spans():
    storage = InMemoryStorage()
    _init(storage)
    storage.insert_trace(Trace(trace_id="TA"))
    storage.insert_trace(Trace(trace_id="TB"))

    async def worker(trace_id: str, name: str):
        with trace_context(trace_id=trace_id):
            with span(name):
                await asyncio.sleep(0)

    async def main():
        await asyncio.gather(worker("TA", "task_a"), worker("TB", "task_b"))

    asyncio.run(main())

    persisted = _spans_by_name(storage)
    assert persisted["task_a"].trace_id == "TA"
    assert persisted["task_b"].trace_id == "TB"


def test_span_state_after_retraction_is_not_reused():
    """A retracted span must not re-register on its rewrite (infinite loop guard)."""
    inner = FailingTraceStore()
    storage = BufferedStorage(inner)
    client = _init(storage)

    with span("outer") as sp:
        _fake_provider_call(client, storage, "TRACE-X")
    storage.flush()

    assert sp.trace_link_state is TraceLinkState.NONE
    assert sp.trace_id is None
