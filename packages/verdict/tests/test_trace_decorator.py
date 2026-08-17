import asyncio
from types import SimpleNamespace

import pytest
import verdict.client as client_mod
from verdict.schema import Trace
from verdict.storage.buffered import BufferedStorage
from verdict.storage.memory import InMemoryStorage
from verdict.storage.sqlite import SQLiteStorage
from verdict.trace import (
    clear_context,
    current_span,
    set_context,
    span,
    trace,
    trace_context,
)


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


def test_nested_span_prefers_new_explicit_context_over_outer_span_context():
    set_context(trace_id="outer-trace")
    try:
        with span("outer") as outer:
            set_context(trace_id="unrelated-mutation")
            with span("inner") as inner:
                pass
        assert outer.trace_id == "outer-trace"
        assert inner.trace_id == "unrelated-mutation"
    finally:
        clear_context()


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


def test_manual_span_inherits_active_trace_context():
    """P0-5: a span recorded in an active trace context must not be orphaned."""
    storage = InMemoryStorage()
    client_mod.shutdown()
    client_mod.init(storage=storage)
    try:
        storage.insert_trace(Trace(trace_id="active-trace-123"))
        set_context(trace_id="active-trace-123")
        with span("retrieve"):
            pass

        [record] = storage.list_spans()
        assert record.trace_id == "active-trace-123"
    finally:
        clear_context()
        client_mod.shutdown()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_parent_and_trace_ids_round_trip_in_storage(backend, tmp_path):
    storage = (
        InMemoryStorage()
        if backend == "memory"
        else SQLiteStorage(str(tmp_path / "spans.db"))
    )
    client_mod.shutdown()
    client_mod.init(storage=storage)
    try:
        storage.insert_trace(Trace(trace_id="trace-round-trip"))
        with trace_context("trace-round-trip"):
            with span("outer"):
                with span("inner"):
                    pass

        records = {record.name: record for record in storage.list_spans()}
        assert records["outer"].trace_id == "trace-round-trip"
        assert records["outer"].parent_name is None
        assert records["inner"].trace_id == "trace-round-trip"
        assert records["inner"].parent_name == "outer"
    finally:
        client_mod.shutdown()
        clear_context()


def test_trace_context_restores_previous_value_after_success_and_exception():
    storage = InMemoryStorage()
    client_mod.shutdown()
    client_mod.init(storage=storage)
    try:
        for trace_id in ("request-outer", "request-inner", "request-error"):
            storage.insert_trace(Trace(trace_id=trace_id))
        set_context(trace_id="request-outer")
        with trace_context("request-inner"):
            with span("inner-success"):
                pass
        with span("restored-after-success"):
            pass

        with pytest.raises(RuntimeError):
            with trace_context("request-error"):
                with span("inner-error"):
                    raise RuntimeError("expected")
        with span("restored-after-error"):
            pass

        records = {record.name: record for record in storage.list_spans()}
        assert records["inner-success"].trace_id == "request-inner"
        assert records["inner-error"].trace_id == "request-error"
        assert records["inner-error"].error == "RuntimeError: expected"
        assert records["restored-after-success"].trace_id == "request-outer"
        assert records["restored-after-error"].trace_id == "request-outer"
    finally:
        clear_context()
        client_mod.shutdown()


def test_standalone_span_stays_unlinked_after_context_is_cleared():
    storage = InMemoryStorage()
    client_mod.shutdown()
    client_mod.init(storage=storage)
    try:
        set_context(trace_id="old-request")
        clear_context()
        with span("standalone"):
            pass

        [record] = storage.list_spans()
        assert record.trace_id is None
    finally:
        clear_context()
        client_mod.shutdown()


def test_async_tasks_keep_trace_context_isolated():
    storage = InMemoryStorage()
    client_mod.shutdown()
    client_mod.init(storage=storage)
    storage.insert_trace(Trace(trace_id="request-a"))
    storage.insert_trace(Trace(trace_id="request-b"))

    async def worker(trace_id):
        with trace_context(trace_id):
            await asyncio.sleep(0)
            with span(f"span-{trace_id}"):
                await asyncio.sleep(0)

    async def run():
        await asyncio.gather(worker("request-a"), worker("request-b"))
        with span("parent-task-standalone"):
            pass

    try:
        asyncio.run(run())
        records = {record.name: record for record in storage.list_spans()}
        assert records["span-request-a"].trace_id == "request-a"
        assert records["span-request-b"].trace_id == "request-b"
        assert records["parent-task-standalone"].trace_id is None
    finally:
        clear_context()
        client_mod.shutdown()


def test_async_child_tasks_override_a_shared_parent_span_context():
    """A copied parent Span object must not override each task's trace context."""
    storage = InMemoryStorage()
    client_mod.shutdown()
    client_mod.init(storage=storage, instrumentors=["none"])
    storage.insert_trace(Trace(trace_id="request-a"))
    storage.insert_trace(Trace(trace_id="request-b"))

    async def worker(trace_id):
        with trace_context(trace_id):
            await asyncio.sleep(0)
            with span(f"child-{trace_id}"):
                await asyncio.sleep(0)

    async def run():
        with span("shared-parent"):
            await asyncio.gather(worker("request-a"), worker("request-b"))

    try:
        asyncio.run(run())
        records = {record.name: record for record in storage.list_spans()}
        assert records["child-request-a"].trace_id == "request-a"
        assert records["child-request-b"].trace_id == "request-b"
        assert records["shared-parent"].trace_id is None
    finally:
        clear_context()
        client_mod.shutdown()


def test_invalid_explicit_trace_context_never_persists_an_orphan_link():
    storage = InMemoryStorage()
    client_mod.shutdown()
    client_mod.init(storage=storage, instrumentors=["none"])
    try:
        with trace_context("missing-trace"):
            with span("invalid-link"):
                pass

        [record] = storage.list_spans()
        assert record.trace_id is None
        assert record.attributes["verdict.link_status"] == "trace_not_found"
    finally:
        clear_context()
        client_mod.shutdown()


def test_cancelled_async_span_persists_its_own_trace_without_leaking_context():
    storage = InMemoryStorage()
    client_mod.shutdown()
    client_mod.init(storage=storage)
    storage.insert_trace(Trace(trace_id="cancelled-request"))

    async def run():
        started = asyncio.Event()

        async def pending():
            with trace_context("cancelled-request"):
                with span("cancelled-span"):
                    started.set()
                    await asyncio.Event().wait()

        task = asyncio.create_task(pending())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with span("after-cancel"):
            pass

    try:
        asyncio.run(run())
        records = {record.name: record for record in storage.list_spans()}
        assert records["cancelled-span"].trace_id == "cancelled-request"
        assert records["cancelled-span"].duration_ms is not None
        assert records["after-cancel"].trace_id is None
    finally:
        clear_context()
        client_mod.shutdown()


def _openai_response():
    return SimpleNamespace(
        model="gpt-test",
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="ok"),
        )],
    )


def test_instrumented_provider_trace_points_to_active_manual_span():
    from verdict.instrumentors.openai import OpenAIInstrumentor

    storage = InMemoryStorage()
    client_mod.shutdown()
    client = client_mod.init(storage=storage, instrumentors=["none"])
    instrumentor = OpenAIInstrumentor(client)
    try:
        with span("manual-parent"):
            instrumentor._wrap_create_sync(
                lambda *_args, **_kwargs: _openai_response(),
                None,
                (),
                {"model": "gpt-test", "messages": []},
            )

        [captured] = storage.list_traces()
        [manual] = storage.list_spans()
        assert captured.parent_span_id == manual.span_id
        assert manual.trace_id is None
    finally:
        client_mod.shutdown()


def test_automatic_trace_link_does_not_read_storage_when_span_closes():
    """A known provider write must not turn buffered span close into a queue flush."""
    from verdict.instrumentors.openai import OpenAIInstrumentor

    class ReadCountingStorage(InMemoryStorage):
        def __init__(self):
            super().__init__()
            self.get_trace_calls = 0

        def get_trace(self, trace_id):
            self.get_trace_calls += 1
            return super().get_trace(trace_id)

    storage = ReadCountingStorage()
    client_mod.shutdown()
    client = client_mod.init(storage=storage, instrumentors=["none"])
    instrumentor = OpenAIInstrumentor(client)
    try:
        with span("manual-parent"):
            instrumentor._wrap_create_sync(
                lambda *_args, **_kwargs: _openai_response(),
                None,
                (),
                {"model": "gpt-test", "messages": []},
            )

        assert storage.get_trace_calls == 0
        [record] = storage.list_spans()
        assert record.trace_id is None
    finally:
        client_mod.shutdown()


def test_explicit_trace_spans_do_not_flush_buffer_on_close():
    """Manual spans must preserve buffered writes instead of forcing 50 reads."""

    class FlushCountingBuffer(BufferedStorage):
        def __init__(self, inner):
            self.flush_calls = 0
            super().__init__(inner, flush_interval=10.0, batch_size=100)

        def flush(self, *args, **kwargs):
            self.flush_calls += 1
            return super().flush(*args, **kwargs)

    inner = InMemoryStorage()
    inner.insert_trace(Trace(trace_id="request-buffered"))
    buffered = FlushCountingBuffer(inner)
    client_mod.shutdown()
    client_mod.init(storage=buffered, instrumentors=["none"])
    try:
        with trace_context("request-buffered"):
            for i in range(50):
                with span(f"manual-{i}"):
                    pass

        assert buffered.flush_calls == 0
        buffered.flush()
        assert len(inner.list_spans()) == 50
    finally:
        client_mod.shutdown()


def test_buffered_trace_failure_never_creates_a_dangling_span_link():
    """A failed provider write cannot affect independent manual-span storage."""
    from verdict.instrumentors.openai import OpenAIInstrumentor

    class FailingTraceStorage(InMemoryStorage):
        def insert_trace(self, trace):
            raise RuntimeError("database unavailable")

    inner = FailingTraceStorage()
    buffered = BufferedStorage(inner, flush_interval=10.0, batch_size=100)
    client_mod.shutdown()
    client = client_mod.init(storage=buffered, instrumentors=["none"])
    instrumentor = OpenAIInstrumentor(client)
    try:
        with span("failed-provider-write"):
            instrumentor._wrap_create_sync(
                lambda *_args, **_kwargs: _openai_response(),
                None,
                (),
                {"model": "gpt-test", "messages": []},
            )

        buffered.flush()
        [record] = inner.list_spans()
        assert record.trace_id is None
        assert "verdict.link_status" not in record.attributes
        assert buffered.write_errors == 1
        assert inner.list_traces() == []
    finally:
        client_mod.shutdown()


def test_invalid_explicit_context_stays_fail_closed_during_provider_call():
    """Automatic correlation must not replace a caller's invalid explicit link."""
    from verdict.instrumentors.openai import OpenAIInstrumentor

    storage = InMemoryStorage()
    client_mod.shutdown()
    client = client_mod.init(storage=storage, instrumentors=["none"])
    instrumentor = OpenAIInstrumentor(client)
    try:
        with trace_context("missing-explicit-trace"):
            with span("provider-parent"):
                instrumentor._wrap_create_sync(
                    lambda *_args, **_kwargs: _openai_response(),
                    None,
                    (),
                    {"model": "gpt-test", "messages": []},
                )

        [captured] = storage.list_traces()
        [record] = storage.list_spans()
        assert captured.parent_span_id == record.span_id
        assert record.trace_id is None
        assert record.attributes["verdict.link_status"] == "trace_not_found"
    finally:
        client_mod.shutdown()


def test_stream_finalized_after_manual_span_exit_keeps_parent_span_id():
    """Late stream persistence uses the captured parent ID without span repair."""
    from verdict.instrumentors.openai import OpenAIInstrumentor

    storage = InMemoryStorage()
    client_mod.shutdown()
    client = client_mod.init(storage=storage, instrumentors=["none"])
    instrumentor = OpenAIInstrumentor(client)
    chunk = SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            finish_reason="stop",
            delta=SimpleNamespace(content="ok"),
        )],
    )
    try:
        with span("stream-parent"):
            stream = instrumentor._wrap_create_sync(
                lambda *_args, **_kwargs: iter([chunk]),
                None,
                (),
                {"model": "gpt-test", "messages": [], "stream": True},
            )

        [before] = storage.list_spans()
        assert before.trace_id is None
        assert list(stream) == [chunk]

        [captured] = storage.list_traces()
        [after] = storage.list_spans()
        assert captured.parent_span_id == after.span_id
        assert after.trace_id is None
    finally:
        client_mod.shutdown()


def test_unsampled_provider_success_does_not_create_orphan_span_trace_id():
    from verdict.instrumentors.openai import OpenAIInstrumentor

    storage = InMemoryStorage()
    client_mod.shutdown()
    client = client_mod.init(
        storage=storage,
        instrumentors=["none"],
        sample_rate=0.0,
    )
    instrumentor = OpenAIInstrumentor(client)
    try:
        with span("unsampled-parent"):
            instrumentor._wrap_create_sync(
                lambda *_args, **_kwargs: _openai_response(),
                None,
                (),
                {"model": "gpt-test", "messages": []},
            )

        assert storage.list_traces() == []
        [manual] = storage.list_spans()
        assert manual.trace_id is None
    finally:
        client_mod.shutdown()


def test_nested_manual_spans_link_provider_trace_to_innermost_span():
    from verdict.instrumentors.openai import OpenAIInstrumentor

    storage = InMemoryStorage()
    client_mod.shutdown()
    client = client_mod.init(storage=storage, instrumentors=["none"])
    instrumentor = OpenAIInstrumentor(client)
    try:
        with span("outer"):
            with span("inner"):
                instrumentor._wrap_create_sync(
                    lambda *_args, **_kwargs: _openai_response(),
                    None,
                    (),
                    {"model": "gpt-test", "messages": []},
                )

        [captured] = storage.list_traces()
        records = {record.name: record for record in storage.list_spans()}
        assert captured.parent_span_id == records["inner"].span_id
        assert records["inner"].trace_id is None
        assert records["outer"].trace_id is None
    finally:
        client_mod.shutdown()


def test_nested_provider_calls_point_to_their_exact_manual_parents():
    from verdict.instrumentors.openai import OpenAIInstrumentor

    storage = InMemoryStorage()
    client_mod.shutdown()
    client = client_mod.init(storage=storage, instrumentors=["none"])
    instrumentor = OpenAIInstrumentor(client)
    try:
        with span("outer"):
            instrumentor._wrap_create_sync(
                lambda *_args, **_kwargs: _openai_response(),
                None,
                (),
                {"model": "outer-model", "messages": []},
            )
            with span("inner"):
                instrumentor._wrap_create_sync(
                    lambda *_args, **_kwargs: _openai_response(),
                    None,
                    (),
                    {"model": "inner-model", "messages": []},
                )

        records = {record.name: record for record in storage.list_spans()}
        traces = {trace.parent_span_id: trace for trace in storage.list_traces()}
        assert traces[records["outer"].span_id].request_model == "outer-model"
        assert traces[records["inner"].span_id].request_model == "inner-model"
        assert records["outer"].trace_id is None
        assert records["inner"].trace_id is None
    finally:
        client_mod.shutdown()


def test_multiple_provider_calls_point_to_one_manual_span_without_reusing_trace_ids():
    from verdict.instrumentors.openai import OpenAIInstrumentor

    storage = InMemoryStorage()
    client_mod.shutdown()
    client = client_mod.init(storage=storage, instrumentors=["none"])
    instrumentor = OpenAIInstrumentor(client)
    try:
        with span("batch"):
            for _ in range(2):
                instrumentor._wrap_create_sync(
                    lambda *_args, **_kwargs: _openai_response(),
                    None,
                    (),
                    {"model": "gpt-test", "messages": []},
                )

        traces = storage.list_traces()
        [record] = storage.list_spans()
        assert len({trace.trace_id for trace in traces}) == 2
        assert {trace.parent_span_id for trace in traces} == {record.span_id}
        assert record.trace_id is None
    finally:
        client_mod.shutdown()


def test_concurrent_async_provider_calls_keep_automatic_span_links_isolated():
    from verdict.instrumentors.openai import OpenAIInstrumentor

    storage = InMemoryStorage()
    client_mod.shutdown()
    client = client_mod.init(storage=storage, instrumentors=["none"])
    instrumentor = OpenAIInstrumentor(client)

    async def wrapped(*_args, **_kwargs):
        await asyncio.sleep(0)
        return _openai_response()

    async def worker(name):
        with span(name):
            await instrumentor._wrap_create_async(
                wrapped,
                None,
                (),
                {"model": "gpt-test", "messages": []},
            )

    async def run():
        await asyncio.gather(worker("request-a"), worker("request-b"))

    try:
        asyncio.run(run())
        spans = {record.span_id: record for record in storage.list_spans()}
        traces = storage.list_traces()
        assert len(traces) == 2
        assert {trace.parent_span_id for trace in traces} == set(spans)
        assert all(record.trace_id is None for record in spans.values())
    finally:
        client_mod.shutdown()
