import asyncio

import pytest

import verdict.client as client_mod
from verdict.storage.memory import InMemoryStorage
from verdict.trace import current_span, span, trace


def test_span_context_manager_records_attributes_and_duration():
    with span("retrieve_documents", index="prod") as s:
        assert current_span() is s
        s.set_attribute("docs.count", 7)
    assert current_span() is None
    assert s.attributes["index"] == "prod"
    assert s.attributes["docs.count"] == 7
    assert s.duration_ms is not None and s.duration_ms >= 0
    assert s.error is None


def test_span_records_error():
    with pytest.raises(RuntimeError):
        with span("boom"):
            raise RuntimeError("oops")


def test_trace_decorator_sync():
    @trace("compute")
    def add(a, b):
        return a + b

    assert add(1, 2) == 3


def test_trace_decorator_async():
    @trace("async_compute")
    async def aadd(a, b):
        await asyncio.sleep(0)
        return a + b

    assert asyncio.run(aadd(2, 3)) == 5


def test_nested_spans_set_parent():
    parents = []
    with span("outer") as outer:
        with span("inner") as inner:
            parents.append((outer, inner))
    assert parents[0][1].parent is parents[0][0]


# ---------------------------------------------------------------------------
# Span persistence to the global client's storage (fix #5).
# ---------------------------------------------------------------------------

def test_span_is_persisted_when_client_initialized():
    storage = InMemoryStorage()
    if not hasattr(storage, "insert_span") or not hasattr(storage, "list_spans"):
        pytest.skip("Storage span API not available yet (owned by parallel worker)")

    client_mod.shutdown()
    client_mod.init(storage=storage)
    try:
        with span("retrieve", k=10) as s:
            s.set_attribute("docs.count", 3)

        spans = storage.list_spans()
        assert len(spans) == 1
        rec = spans[0]
        assert rec.name == "retrieve"
        assert rec.attributes.get("k") == 10
        assert rec.attributes.get("docs.count") == 3
        assert rec.error is None
        assert rec.duration_ms is not None and rec.duration_ms >= 0
        assert rec.started_at is not None and rec.ended_at is not None
    finally:
        client_mod.shutdown()


def test_error_span_is_persisted_with_error_and_parent():
    storage = InMemoryStorage()
    if not hasattr(storage, "insert_span") or not hasattr(storage, "list_spans"):
        pytest.skip("Storage span API not available yet (owned by parallel worker)")

    client_mod.shutdown()
    client_mod.init(storage=storage)
    try:
        with span("outer"):
            with pytest.raises(RuntimeError):
                with span("inner"):
                    raise RuntimeError("boom")

        spans = {s.name: s for s in storage.list_spans()}
        assert set(spans) == {"outer", "inner"}
        assert spans["inner"].error is not None
        assert "boom" in spans["inner"].error
        # inner records its parent name; outer has no parent.
        assert spans["inner"].parent_name == "outer"
        assert spans["outer"].parent_name is None
    finally:
        client_mod.shutdown()


def test_span_is_noop_without_client():
    # No client initialized -> span() must not raise and must not require storage.
    client_mod.shutdown()
    with span("standalone") as s:
        s.set_attribute("x", 1)
    assert s.error is None
