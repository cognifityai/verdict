"""Regression contracts for manual-span/provider-trace correlation.

Automatic capture is intentionally one-way: every provider ``Trace`` records
the active manual span in ``parent_span_id``. ``SpanRecord.trace_id`` is reserved
for a validated caller-supplied ``trace_context``. That shape represents several
provider calls inside one span without callbacks, ownership arbitration, repair
writes, or permanent pending states.
"""

from __future__ import annotations

import asyncio
import copy
import pickle
import random
import threading
from collections import Counter
from pathlib import Path

import pytest
import verdict
import verdict.client as client_module
from verdict.instrumentors.base import apply_routing_context, persist_trace
from verdict.schema import Trace
from verdict.storage import BufferedStorage, InMemoryStorage, SQLiteStorage
from verdict.trace import TraceLinkState, span, trace_context


class FailingTraceStore(InMemoryStorage):
    """Storage whose trace writes fail while manual span writes still work."""

    def insert_trace(self, trace: Trace) -> None:
        raise RuntimeError("simulated durable trace failure")


class DeferredTraceStore(InMemoryStorage):
    """Test adapter that accepts trace intents and commits them explicitly."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: dict[str, Trace] = {}

    def insert_trace(self, trace: Trace) -> None:
        self.pending[trace.trace_id] = copy.deepcopy(trace)

    def resolve(self, trace_id: str, *, durable: bool) -> None:
        trace = self.pending.pop(trace_id)
        if durable:
            super().insert_trace(trace)


@pytest.fixture(autouse=True)
def _reset_client():
    client_module._client = None
    yield
    client_module._client = None


def _init(storage):
    verdict.init(storage=storage, service_name="span-link-test")
    return client_module.get_client()


def _capture(client, trace_id: str) -> Trace:
    """Drive the shared real instrumentor routing/persistence sequence."""
    trace = Trace(trace_id=trace_id)
    apply_routing_context(client, trace)
    persist_trace(client, trace)
    return trace


def _spans_by_name(storage):
    return {record.name: record for record in storage.list_spans(limit=1000)}


def test_automatic_provider_correlation_is_trace_to_span_only():
    """Two provider calls must not invent one reverse-link owner."""
    storage = InMemoryStorage()
    client = _init(storage)

    with span("agent-turn") as parent:
        first = _capture(client, "TRACE-A")
        second = _capture(client, "TRACE-B")
        with span("retrieve"):
            pass

    records = _spans_by_name(storage)
    assert first.parent_span_id == parent.span_id
    assert second.parent_span_id == parent.span_id
    assert records["agent-turn"].trace_id is None
    assert records["retrieve"].trace_id is None
    assert all("verdict.link_status" not in item.attributes for item in records.values())


@pytest.mark.parametrize("kind", ["memory", "sqlite", "buffered-memory", "buffered-sqlite"])
def test_every_ended_span_survives_one_provider_call(kind: str, tmp_path: Path):
    """Exercise the contract through serializing and buffered adapters."""
    if kind.endswith("sqlite"):
        inner = SQLiteStorage(str(tmp_path / f"{kind}.db"))
    else:
        inner = InMemoryStorage()
    storage = BufferedStorage(inner, flush_interval=10.0) if kind.startswith("buffered") else inner
    client = _init(storage)
    try:
        with span("agent-turn") as parent:
            trace = _capture(client, "TRACE-ONE")
            with span("before"):
                pass
            with span("after"):
                pass

        if isinstance(storage, BufferedStorage):
            storage.flush(timeout=5)
        records = _spans_by_name(storage)
        assert set(records) == {"agent-turn", "before", "after"}
        assert all(record.trace_id is None for record in records.values())
        assert storage.get_trace(trace.trace_id).parent_span_id == parent.span_id
    finally:
        storage.close()


def test_provider_write_failure_cannot_change_or_drop_manual_spans():
    storage = FailingTraceStore()
    client = _init(storage)

    with span("outer"):
        trace = Trace(trace_id="TRACE-DEAD")
        apply_routing_context(client, trace)
        with pytest.raises(RuntimeError, match="durable trace failure"):
            persist_trace(client, trace)
        with span("inner"):
            pass

    records = _spans_by_name(storage)
    assert set(records) == {"outer", "inner"}
    assert storage.get_trace(trace.trace_id) is None
    assert all(record.trace_id is None for record in records.values())
    assert all("verdict.link_status" not in record.attributes for record in records.values())


def test_buffered_provider_failure_cannot_change_or_drop_manual_spans():
    inner = FailingTraceStore()
    storage = BufferedStorage(inner, flush_interval=10.0)
    client = _init(storage)
    try:
        with span("outer"):
            _capture(client, "TRACE-DEAD")
            with span("inner"):
                pass
        storage.flush(timeout=5)

        records = _spans_by_name(storage)
        assert set(records) == {"outer", "inner"}
        assert all(record.trace_id is None for record in records.values())
        assert storage.write_errors == 1
    finally:
        storage.close()


def test_deleting_one_of_two_provider_traces_preserves_the_shared_span():
    storage = InMemoryStorage()
    client = _init(storage)

    with span("shared-parent") as parent:
        _capture(client, "TRACE-A")
        _capture(client, "TRACE-B")

    storage.delete_trace("TRACE-A")

    [remaining_trace] = storage.list_traces()
    [remaining_span] = storage.list_spans()
    assert remaining_span.span_id == parent.span_id
    assert remaining_span.trace_id is None
    assert remaining_trace.trace_id == "TRACE-B"
    assert remaining_trace.parent_span_id == parent.span_id


def test_delete_preserves_explicit_span_referenced_by_another_trace():
    """Even explicit ownership cannot delete another trace's manual parent."""
    storage = InMemoryStorage()
    client = _init(storage)
    explicit = Trace(trace_id="EXPLICIT")
    storage.insert_trace(explicit)

    with trace_context("EXPLICIT"):
        with span("shared") as parent:
            child = _capture(client, "PROVIDER")

    storage.delete_trace("EXPLICIT")

    [record] = storage.list_spans()
    assert record.span_id == parent.span_id
    assert record.trace_id is None
    assert storage.get_trace(child.trace_id).parent_span_id == parent.span_id


def test_buffered_provider_correlation_inserts_each_span_physically_once():
    """Upsert semantics must not conceal duplicate span write attempts."""
    trace_started = threading.Event()
    allow_trace_write = threading.Event()

    class CountingBlockingStore(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.span_writes: Counter[str] = Counter()

        def insert_trace(self, trace: Trace) -> None:
            trace_started.set()
            assert allow_trace_write.wait(timeout=5), "test did not release trace write"
            super().insert_trace(trace)

        def insert_span(self, record) -> None:
            self.span_writes[record.span_id] += 1
            super().insert_span(record)

    inner = CountingBlockingStore()
    storage = BufferedStorage(inner, flush_interval=10.0, batch_size=100)
    client = _init(storage)
    try:
        with span("parent"):
            _capture(client, "TRACE-DELAYED")
            assert trace_started.wait(timeout=5), "trace write never reached storage"
            with span("child"):
                pass

        allow_trace_write.set()
        storage.flush(timeout=5)

        assert set(inner.span_writes.values()) == {1}
        assert len(inner.span_writes) == 2
    finally:
        allow_trace_write.set()
        storage.close()


@pytest.mark.parametrize("first_durable", [True, False])
@pytest.mark.parametrize("second_durable", [True, False])
@pytest.mark.parametrize("reverse_resolution", [True, False])
def test_two_delayed_provider_writes_are_order_independent(
    first_durable: bool,
    second_durable: bool,
    reverse_resolution: bool,
):
    storage = DeferredTraceStore()
    client = _init(storage)

    with span("parent") as parent:
        first = _capture(client, "TRACE-A")
        with span("before-second"):
            pass
        second = _capture(client, "TRACE-B")
        with span("before-resolution"):
            pass
        resolutions = [
            (first.trace_id, first_durable),
            (second.trace_id, second_durable),
        ]
        if reverse_resolution:
            resolutions.reverse()
        for trace_id, durable in resolutions:
            storage.resolve(trace_id, durable=durable)
        with span("after-resolution"):
            pass

    records = storage.list_spans(limit=20)
    assert len(records) == 4
    assert all(record.trace_id is None for record in records)
    assert {trace.trace_id for trace in storage.list_traces()} == {
        trace_id
        for trace_id, durable in (("TRACE-A", first_durable), ("TRACE-B", second_durable))
        if durable
    }
    assert all(trace.parent_span_id == parent.span_id for trace in storage.list_traces())


def test_randomized_trace_resolution_never_loses_or_duplicates_spans():
    for seed in range(50):
        rng = random.Random(seed)
        storage = DeferredTraceStore()
        client_module._client = None
        client = _init(storage)
        outcomes = {f"TRACE-{index}": rng.choice([True, False]) for index in range(4)}

        with span(f"parent-{seed}") as parent:
            traces = [_capture(client, trace_id) for trace_id in outcomes]
            unresolved = list(outcomes)
            for index in range(12):
                if unresolved and rng.choice([True, False]):
                    trace_id = unresolved.pop(rng.randrange(len(unresolved)))
                    storage.resolve(trace_id, durable=outcomes[trace_id])
                with span(f"child-{seed}-{index}"):
                    pass
            rng.shuffle(unresolved)
            for trace_id in unresolved:
                storage.resolve(trace_id, durable=outcomes[trace_id])

        records = storage.list_spans(limit=20)
        assert len(records) == 13
        assert len({record.span_id for record in records}) == 13
        assert all(record.trace_id is None for record in records)
        assert all(trace.parent_span_id == parent.span_id for trace in storage.list_traces())
        assert {trace.trace_id for trace in storage.list_traces()} == {
            trace.trace_id for trace in traces if outcomes[trace.trace_id]
        }


def test_trace_persistence_and_child_creation_do_not_share_mutable_link_state():
    """A real thread barrier cannot race a callback that no longer exists."""
    entered = threading.Event()
    release = threading.Event()

    class ThreadedStore(InMemoryStorage):
        def insert_trace(self, trace: Trace) -> None:
            entered.set()
            assert release.wait(timeout=5)
            super().insert_trace(trace)

    storage = ThreadedStore()
    client = _init(storage)
    with span("parent") as parent:
        trace = Trace(trace_id="THREADED")
        apply_routing_context(client, trace)
        worker = threading.Thread(target=persist_trace, args=(client, trace))
        worker.start()
        assert entered.wait(timeout=5)
        with span("child"):
            pass
        release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()

    records = _spans_by_name(storage)
    assert all(record.trace_id is None for record in records.values())
    assert storage.get_trace(trace.trace_id).parent_span_id == parent.span_id


def test_trace_remains_serializable_after_routing_context():
    storage = InMemoryStorage()
    client = _init(storage)
    with span("parent") as parent:
        trace = Trace(trace_id="SERIALIZABLE")
        apply_routing_context(client, trace)

    restored = pickle.loads(pickle.dumps(trace))
    assert restored.parent_span_id == parent.span_id
    assert vars(restored).keys() == vars(trace).keys()


def test_span_write_does_not_flush_buffer_for_explicit_link_validation():
    inner = InMemoryStorage()
    storage = BufferedStorage(inner)
    calls = 0
    original_flush = storage.flush

    def counted_flush(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_flush(*args, **kwargs)

    storage.flush = counted_flush  # type: ignore[method-assign]
    _init(storage)
    inner.insert_trace(Trace(trace_id="EXPLICIT"))
    try:
        with trace_context("EXPLICIT"):
            for index in range(25):
                with span(f"explicit-{index}"):
                    pass
        assert calls == 0
    finally:
        storage.close()


def test_valid_explicit_context_is_inherited_and_persisted():
    storage = InMemoryStorage()
    _init(storage)
    storage.insert_trace(Trace(trace_id="EXPLICIT"))

    with trace_context("EXPLICIT"):
        with span("outer"):
            with span("inner"):
                pass

    records = _spans_by_name(storage)
    assert records["outer"].trace_id == "EXPLICIT"
    assert records["inner"].trace_id == "EXPLICIT"


def test_missing_explicit_context_fails_closed_with_breadcrumb():
    storage = InMemoryStorage()
    _init(storage)

    with trace_context("MISSING"):
        with span("orphan"):
            pass

    [record] = storage.list_spans()
    assert record.trace_id is None
    assert record.attributes["verdict.link_status"] == "trace_not_found"


def test_concurrent_explicit_contexts_do_not_cross_attribute_spans():
    storage = InMemoryStorage()
    _init(storage)
    storage.insert_trace(Trace(trace_id="TA"))
    storage.insert_trace(Trace(trace_id="TB"))

    async def worker(trace_id: str, name: str):
        with trace_context(trace_id):
            with span(name):
                await asyncio.sleep(0)

    async def run():
        await asyncio.gather(worker("TA", "task-a"), worker("TB", "task-b"))

    asyncio.run(run())
    records = _spans_by_name(storage)
    assert records["task-a"].trace_id == "TA"
    assert records["task-b"].trace_id == "TB"


def test_trace_link_state_has_no_provider_lifecycle_states():
    assert set(TraceLinkState) == {
        TraceLinkState.NONE,
        TraceLinkState.EXPLICIT,
        TraceLinkState.INHERITED_EXPLICIT,
    }
