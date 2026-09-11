from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Event, Thread

import pytest
import verdict
import verdict.client as client_module
from verdict.client import VerdictClient
from verdict.instrumentors.base import apply_routing_context
from verdict.schema import Trace


def _trace(client: VerdictClient) -> Trace:
    trace = Trace()
    apply_routing_context(client, trace)
    return trace


def test_public_model_call_context_claims_exactly_one_trace() -> None:
    client = VerdictClient(storage=None)

    with verdict.model_call_context() as correlation_id:
        assert len(correlation_id) == 32
        assert correlation_id == correlation_id.lower()
        assert set(correlation_id) <= set("0123456789abcdef")
        first = _trace(client)
        second = _trace(client)

    after = _trace(client)
    assert first.trace_id == correlation_id
    assert second.trace_id != correlation_id
    assert after.trace_id != correlation_id
    assert len({first.trace_id, second.trace_id, after.trace_id}) == 3


def test_nested_model_call_contexts_restore_independent_reservations() -> None:
    client = VerdictClient(storage=None)

    with verdict.model_call_context() as outer_id:
        with verdict.model_call_context() as inner_id:
            inner = _trace(client)
        outer = _trace(client)

    assert inner.trace_id == inner_id
    assert outer.trace_id == outer_id
    assert outer_id != inner_id


@pytest.mark.parametrize("raises", [False, True])
def test_exit_closes_reservation_shared_with_delayed_copied_context(raises: bool) -> None:
    copied: contextvars.Context | None = None

    with pytest.raises(RuntimeError) if raises else nullcontext():
        with verdict.model_call_context():
            copied = contextvars.copy_context()
            if raises:
                raise RuntimeError("expected")

    assert copied is not None
    assert copied.run(client_module._claim_model_call_correlation_id) is None


def test_clear_context_closes_reservation_shared_with_copied_context() -> None:
    with verdict.model_call_context():
        copied = contextvars.copy_context()
        verdict.clear_context()

    assert copied.run(client_module._claim_model_call_correlation_id) is None


def test_copied_threads_can_claim_reservation_only_once() -> None:
    with verdict.model_call_context() as correlation_id:
        copied = [contextvars.copy_context(), contextvars.copy_context()]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda context: context.run(client_module._claim_model_call_correlation_id),
                    copied,
                )
            )

    assert results.count(correlation_id) == 1
    assert results.count(None) == 1


@pytest.mark.parametrize("operation", ["claim", "close"])
def test_claim_and_close_share_the_reservation_lock(operation: str) -> None:
    reservation = client_module._ModelCallReservation("a" * 32)
    started = Event()
    finished = Event()
    returned: list[str | None] = []

    def run() -> None:
        started.set()
        if operation == "claim":
            returned.append(reservation.claim())
        else:
            reservation.close()
        finished.set()

    reservation._lock.acquire()
    worker = Thread(target=run)
    try:
        worker.start()
        assert started.wait(timeout=2)
        assert not finished.wait(timeout=0.1)
    finally:
        reservation._lock.release()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert finished.is_set()
    if operation == "claim":
        assert returned == ["a" * 32]
        assert reservation.claim() is None
    else:
        assert returned == []
        assert reservation.claim() is None


def test_delayed_copied_thread_cannot_claim_after_owner_exit() -> None:
    release = Event()

    def delayed_claim() -> str | None:
        assert release.wait(timeout=2)
        return client_module._claim_model_call_correlation_id()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with verdict.model_call_context():
            copied = contextvars.copy_context()
            future = executor.submit(copied.run, delayed_claim)
        release.set()

        assert future.result(timeout=2) is None


def test_delayed_copied_task_cannot_claim_after_owner_exit() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def delayed_claim() -> str | None:
            await release.wait()
            return client_module._claim_model_call_correlation_id()

        with verdict.model_call_context():
            task = asyncio.create_task(delayed_claim())
        release.set()

        assert await task is None

    asyncio.run(scenario())


def test_claim_before_close_is_the_only_permitted_winner() -> None:
    with verdict.model_call_context() as correlation_id:
        copied = contextvars.copy_context()
        assert copied.run(client_module._claim_model_call_correlation_id) == correlation_id

    assert copied.run(client_module._claim_model_call_correlation_id) is None


def test_cancelled_owner_closes_delayed_child_reservation() -> None:
    async def scenario() -> None:
        ready = asyncio.Event()
        copied: contextvars.Context | None = None

        async def owner() -> None:
            nonlocal copied
            with verdict.model_call_context():
                copied = contextvars.copy_context()
                ready.set()
                await asyncio.Future()

        task = asyncio.create_task(owner())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert copied is not None
        assert copied.run(client_module._claim_model_call_correlation_id) is None

    asyncio.run(scenario())


def test_manual_trace_context_remains_separate() -> None:
    client = VerdictClient(storage=None)

    with verdict.trace_context("manual-span-link"):
        ordinary = _trace(client)
        with verdict.model_call_context() as correlation_id:
            reserved = _trace(client)

    assert ordinary.trace_id != "manual-span-link"
    assert reserved.trace_id == correlation_id
