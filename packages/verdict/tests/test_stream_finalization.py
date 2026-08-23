from __future__ import annotations

import asyncio
import gc
import time
from types import SimpleNamespace

import pytest
from verdict.instrumentors.anthropic import (
    _AsyncMessageStreamManagerWrapper,
    _MessageStreamManagerWrapper,
)
from verdict.instrumentors.anthropic import (
    _AsyncStreamingWrapper as AnthropicAsyncWrapper,
)
from verdict.instrumentors.anthropic import _StreamingWrapper as AnthropicWrapper
from verdict.instrumentors.google import _AsyncStreamingWrapper as GoogleAsyncWrapper
from verdict.instrumentors.google import _StreamingWrapper as GoogleWrapper
from verdict.instrumentors.openai import _AsyncStreamingWrapper as OpenAIAsyncWrapper
from verdict.instrumentors.openai import _StreamingWrapper as OpenAIWrapper
from verdict.schema import Trace

SYNC_WRAPPERS = [AnthropicWrapper, OpenAIWrapper, GoogleWrapper]
ASYNC_WRAPPERS = [AnthropicAsyncWrapper, OpenAIAsyncWrapper, GoogleAsyncWrapper]


class FakeInstrumentor:
    def __init__(self) -> None:
        self.persisted: list[Trace] = []
        self.client = SimpleNamespace(
            capture_content=False,
            redaction_mode="redact",
            redaction_secret=None,
        )

    def _should_sample(self, _trace=None) -> bool:
        return True

    def _safe_persist(self, trace: Trace) -> None:
        self.persisted.append(trace)

    def _build_input_trace(self, _kwargs) -> Trace:
        return Trace(provider="anthropic")

    def _fill_input_trace(self, _trace: Trace, _kwargs) -> None:
        pass


class SyncStream:
    def __init__(self, chunks=()) -> None:
        self.chunks = list(chunks)
        self.closed = False
        self.entered = False
        self.exited = False

    def __iter__(self):
        yield from self.chunks

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        return False


class AsyncStream:
    def __init__(self, chunks=()) -> None:
        self.chunks = list(chunks)
        self.index = 0
        self.closed = False
        self.entered = False
        self.exited = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.chunks):
            raise StopAsyncIteration
        chunk = self.chunks[self.index]
        self.index += 1
        return chunk

    async def aclose(self) -> None:
        self.closed = True

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False


def _wrapper(wrapper_type, inner):
    instrumentor = FakeInstrumentor()
    wrapped = wrapper_type(inner, Trace(provider="test"), time.perf_counter(), instrumentor)
    return wrapped, instrumentor


@pytest.mark.parametrize("wrapper_type", SYNC_WRAPPERS)
def test_sync_stream_full_consumption_finalizes(wrapper_type):
    chunk = SimpleNamespace()
    wrapped, instrumentor = _wrapper(wrapper_type, SyncStream([chunk]))

    assert list(wrapped) == [chunk]
    assert len(instrumentor.persisted) == 1
    assert instrumentor.persisted[0].ended_at is not None


@pytest.mark.parametrize("wrapper_type", SYNC_WRAPPERS)
def test_sync_partial_iterator_close_finalizes(wrapper_type):
    wrapped, instrumentor = _wrapper(
        wrapper_type, SyncStream([SimpleNamespace(), SimpleNamespace()])
    )
    iterator = iter(wrapped)
    next(iterator)
    assert instrumentor.persisted == []

    iterator.close()

    assert len(instrumentor.persisted) == 1


@pytest.mark.parametrize("wrapper_type", SYNC_WRAPPERS)
def test_sync_wrapper_close_finalizes_never_iterated_stream(wrapper_type):
    inner = SyncStream([SimpleNamespace()])
    wrapped, instrumentor = _wrapper(wrapper_type, inner)

    wrapped.close()
    wrapped.close()

    assert inner.closed is True
    assert len(instrumentor.persisted) == 1


def test_sync_helper_close_defers_finalization_until_manager_cleanup_error():
    class FailingManager:
        def __init__(self) -> None:
            self.stream = SyncStream()

        def __enter__(self):
            return self.stream

        def __exit__(self, exc_type, exc, tb):
            raise RuntimeError("manager cleanup failed")

    instrumentor = FakeInstrumentor()
    wrapped = _MessageStreamManagerWrapper(
        FailingManager(), {}, instrumentor
    )

    with pytest.raises(RuntimeError, match="manager cleanup failed"):
        with wrapped as stream:
            stream.close()

    assert len(instrumentor.persisted) == 1
    assert instrumentor.persisted[0].error == "RuntimeError: manager cleanup failed"


@pytest.mark.parametrize("wrapper_type", SYNC_WRAPPERS)
def test_sync_context_exit_finalizes(wrapper_type):
    inner = SyncStream()
    wrapped, instrumentor = _wrapper(wrapper_type, inner)

    with wrapped:
        assert inner.entered is True

    assert inner.exited is True
    assert len(instrumentor.persisted) == 1


@pytest.mark.parametrize("wrapper_type", SYNC_WRAPPERS)
def test_sync_mid_stream_error_is_persisted_and_reraised(wrapper_type):
    def broken():
        yield SimpleNamespace()
        raise RuntimeError("stream broke")

    wrapped, instrumentor = _wrapper(wrapper_type, broken())

    with pytest.raises(RuntimeError, match="stream broke"):
        list(wrapped)

    assert len(instrumentor.persisted) == 1
    assert instrumentor.persisted[0].error == "RuntimeError: stream broke"


@pytest.mark.parametrize("wrapper_type", SYNC_WRAPPERS)
def test_sync_never_iterated_garbage_collection_is_not_a_finalization_contract(
    wrapper_type,
):
    wrapped, instrumentor = _wrapper(wrapper_type, SyncStream())

    del wrapped
    gc.collect()

    assert instrumentor.persisted == []


@pytest.mark.parametrize("wrapper_type", ASYNC_WRAPPERS)
def test_async_stream_full_consumption_finalizes(wrapper_type):
    async def run():
        chunk = SimpleNamespace()
        wrapped, instrumentor = _wrapper(wrapper_type, AsyncStream([chunk]))
        seen = [item async for item in wrapped]
        return chunk, seen, instrumentor

    chunk, seen, instrumentor = asyncio.run(run())
    assert seen == [chunk]
    assert len(instrumentor.persisted) == 1


@pytest.mark.parametrize("wrapper_type", ASYNC_WRAPPERS)
def test_async_partial_stream_aclose_finalizes(wrapper_type):
    async def run():
        inner = AsyncStream([SimpleNamespace(), SimpleNamespace()])
        wrapped, instrumentor = _wrapper(wrapper_type, inner)
        iterator = wrapped.__aiter__()
        await anext(iterator)
        assert instrumentor.persisted == []
        await wrapped.aclose()
        await iterator.aclose()
        return inner, instrumentor

    inner, instrumentor = asyncio.run(run())
    assert inner.closed is True
    assert len(instrumentor.persisted) == 1


def test_async_helper_close_defers_finalization_until_manager_cleanup_error():
    class FailingManager:
        def __init__(self) -> None:
            self.stream = AsyncStream()

        async def __aenter__(self):
            return self.stream

        async def __aexit__(self, exc_type, exc, tb):
            raise RuntimeError("async manager cleanup failed")

    async def run():
        instrumentor = FakeInstrumentor()
        wrapped = _AsyncMessageStreamManagerWrapper(
            FailingManager(), {}, instrumentor
        )
        with pytest.raises(RuntimeError, match="async manager cleanup failed"):
            async with wrapped as stream:
                await stream.close()
        return instrumentor

    instrumentor = asyncio.run(run())
    assert len(instrumentor.persisted) == 1
    assert (
        instrumentor.persisted[0].error
        == "RuntimeError: async manager cleanup failed"
    )


@pytest.mark.parametrize("wrapper_type", ASYNC_WRAPPERS)
def test_async_context_exit_finalizes(wrapper_type):
    async def run():
        inner = AsyncStream()
        wrapped, instrumentor = _wrapper(wrapper_type, inner)
        async with wrapped:
            assert inner.entered is True
        return inner, instrumentor

    inner, instrumentor = asyncio.run(run())
    assert inner.exited is True
    assert len(instrumentor.persisted) == 1


@pytest.mark.parametrize("wrapper_type", ASYNC_WRAPPERS)
def test_async_mid_stream_error_is_persisted_and_reraised(wrapper_type):
    class BrokenAsyncStream(AsyncStream):
        async def __anext__(self):
            raise RuntimeError("async stream broke")

    async def run():
        wrapped, instrumentor = _wrapper(wrapper_type, BrokenAsyncStream())
        with pytest.raises(RuntimeError, match="async stream broke"):
            async for _ in wrapped:
                pass
        return instrumentor

    instrumentor = asyncio.run(run())
    assert len(instrumentor.persisted) == 1
    assert instrumentor.persisted[0].error == "RuntimeError: async stream broke"


@pytest.mark.parametrize("wrapper_type", ASYNC_WRAPPERS)
def test_async_cancellation_is_persisted_as_error(wrapper_type):
    class PendingAsyncStream(AsyncStream):
        async def __anext__(self):
            await asyncio.Event().wait()

    async def run():
        wrapped, instrumentor = _wrapper(wrapper_type, PendingAsyncStream())

        async def consume():
            async for _ in wrapped:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return instrumentor

    instrumentor = asyncio.run(run())
    assert len(instrumentor.persisted) == 1
    assert instrumentor.persisted[0].error is not None
    assert instrumentor.persisted[0].error.startswith("CancelledError:")


@pytest.mark.parametrize("wrapper_type", ASYNC_WRAPPERS)
def test_async_never_iterated_garbage_collection_is_not_a_finalization_contract(
    wrapper_type,
):
    wrapped, instrumentor = _wrapper(wrapper_type, AsyncStream())

    del wrapped
    gc.collect()

    assert instrumentor.persisted == []
