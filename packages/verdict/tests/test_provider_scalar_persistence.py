"""Real-SDK regressions for provider scalar values reaching durable storage.

The provider clients are real installed SDKs.  Only their HTTP transport is
replaced, so request construction (including SDK-owned unset sentinels),
response parsing, Verdict wrapping, and SQLite persistence all execute.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import math
import threading
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import httpx
import pytest
from verdict.client import VerdictClient
from verdict.instrumentors.base import persist_trace
from verdict.schema import Trace
from verdict.storage.sqlite import SQLiteStorage


class _SinglePassMessages:
    """Valid iterable that becomes empty after its first iteration."""

    def __init__(self, content: str = "one-shot@example.com") -> None:
        self.content = content
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations > 1:
            return iter(())
        return iter([{"role": "user", "content": self.content}])


class _RaisingMessages:
    """Re-iterable input whose provider-owned traversal fails after one item."""

    def __init__(self) -> None:
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        yield {"role": "user", "content": "prefix@example.com"}
        raise RuntimeError("message iteration failed for failure@example.com")


def _provider_response(request: httpx.Request) -> httpx.Response:
    """Return provider-shaped responses at the SDK transport boundary."""
    if request.url.path.endswith("/chat/completions"):
        payload = json.loads(request.content)
        if payload.get("stream"):
            events = [
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "OK"},
                        "finish_reason": None,
                    }],
                },
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }],
                },
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                },
            ]
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            body += "data: [DONE]\n\n"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body.encode(),
            )
        return httpx.Response(200, json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        })

    if request.url.path.endswith("/messages"):
        payload = json.loads(request.content)
        if payload.get("stream"):
            events = [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_test",
                            "type": "message",
                            "role": "assistant",
                            "model": "claude-haiku-4-5-20251001",
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 2, "output_tokens": 0},
                        },
                    },
                ),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "text_delta",
                            "text": "OK stream@example.com",
                        },
                    },
                ),
                (
                    "content_block_stop",
                    {"type": "content_block_stop", "index": 0},
                ),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                        },
                        # A valid delta-only usage shape pins field-wise merging:
                        # the earlier input count must survive this event.
                        "usage": {"output_tokens": 3},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            body = "".join(
                f"event: {name}\ndata: {json.dumps(event)}\n\n"
                for name, event in events
            )
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "request-id": "request_test",
                },
                content=body.encode(),
            )
        return httpx.Response(200, json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 2, "output_tokens": 1},
        })

    if request.url.path.endswith(":generateContent"):
        return httpx.Response(200, json={
            "candidates": [{
                "content": {"parts": [{"text": "OK"}], "role": "model"},
                "finishReason": "STOP",
                "index": 0,
            }],
            "usageMetadata": {
                "promptTokenCount": 2,
                "candidatesTokenCount": 1,
                "totalTokenCount": 3,
            },
            "modelVersion": "gemini-2.5-flash",
            "responseId": "google-test",
        })

    return httpx.Response(404, json={"error": {"message": request.url.path}})


def _empty_anthropic_provider_response(request: httpx.Request) -> httpx.Response:
    """Return the valid Anthropic SSE fixture with a captured empty text value."""
    response = _provider_response(request)
    if request.url.path.endswith("/messages") and json.loads(request.content).get(
        "stream"
    ):
        expected = b'"text": "OK stream@example.com"'
        assert expected in response.content
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content.replace(expected, b'"text": ""'),
        )
    return response


def _anthropic_transport_module(anthropic_module):
    client_base = anthropic_module.DefaultHttpxClient.__mro__[1]
    return importlib.import_module(client_base.__module__.split(".", 1)[0])


def _anthropic_mock_transport(anthropic_module, responder=_provider_response):
    transport_module = _anthropic_transport_module(anthropic_module)

    def respond(request):
        response = responder(request)
        return transport_module.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )

    return transport_module.MockTransport(respond)


def _anthropic_temperature_arg(create, sentinel) -> dict[str, object]:
    if "temperature" not in inspect.signature(create).parameters:
        return {}
    return {"temperature": sentinel}


def _sqlite_client(tmp_path, name: str) -> tuple[VerdictClient, SQLiteStorage]:
    storage = SQLiteStorage(str(tmp_path / f"{name}.db"))
    return VerdictClient(storage=storage), storage


@contextmanager
def _real_anthropic_client(
    tmp_path,
    name: str,
    *,
    responder=_provider_response,
    max_retries: int = 0,
    capture_content: bool = False,
    sample_rate: float = 1.0,
    storage=None,
):
    anthropic = pytest.importorskip("anthropic")
    from verdict.instrumentors.anthropic import AnthropicInstrumentor

    owns_storage = storage is None
    if storage is None:
        verdict_client, storage = _sqlite_client(tmp_path, name)
    else:
        verdict_client = VerdictClient(storage=storage)
    verdict_client.capture_content = capture_content
    verdict_client.sample_rate = sample_rate
    instrumentor = AnthropicInstrumentor(verdict_client)
    http_client = anthropic.DefaultHttpxClient(
        transport=_anthropic_mock_transport(anthropic, responder)
    )
    instrumentor.install()
    try:
        provider = anthropic.Anthropic(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=max_retries,
            http_client=http_client,
        )
        yield anthropic, provider, storage
    finally:
        instrumentor.uninstall()
        http_client.close()
        if owns_storage:
            storage.close()


@asynccontextmanager
async def _real_async_anthropic_client(
    tmp_path,
    name: str,
    *,
    responder=_provider_response,
    max_retries: int = 0,
    capture_content: bool = False,
    sample_rate: float = 1.0,
):
    anthropic = pytest.importorskip("anthropic")
    from verdict.instrumentors.anthropic import AnthropicInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, name)
    verdict_client.capture_content = capture_content
    verdict_client.sample_rate = sample_rate
    instrumentor = AnthropicInstrumentor(verdict_client)
    http_client = anthropic.DefaultAsyncHttpxClient(
        transport=_anthropic_mock_transport(anthropic, responder)
    )
    instrumentor.install()
    try:
        provider = anthropic.AsyncAnthropic(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=max_retries,
            http_client=http_client,
        )
        yield anthropic, provider, storage
    finally:
        instrumentor.uninstall()
        await http_client.aclose()
        storage.close()


def test_real_openai_chat_stream_with_sdk_omit_values_persists_exactly_one_row(tmp_path):
    openai = pytest.importorskip("openai")
    from verdict.instrumentors.openai import OpenAIInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, "openai-stream")
    instrumentor = OpenAIInstrumentor(verdict_client)
    http_client = httpx.Client(transport=httpx.MockTransport(_provider_response))
    instrumentor.install()
    try:
        provider = openai.OpenAI(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=0,
            http_client=http_client,
        )
        with provider.chat.completions.stream(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            stream_options={"include_usage": True},
        ) as stream:
            events = list(stream)

        assert events
        [trace] = storage.list_traces()
        assert trace.temperature is None
        assert trace.max_tokens is None
        assert trace.input_tokens == 2
        assert trace.output_tokens == 1
        assert trace.cost_usd is not None
    finally:
        instrumentor.uninstall()
        http_client.close()
        storage.close()


async def test_real_async_openai_chat_stream_with_sdk_omit_values_persists_one_row(
    tmp_path,
):
    openai = pytest.importorskip("openai")
    from verdict.instrumentors.openai import OpenAIInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, "async-openai-stream")
    instrumentor = OpenAIInstrumentor(verdict_client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_provider_response))
    instrumentor.install()
    try:
        provider = openai.AsyncOpenAI(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=0,
            http_client=http_client,
        )
        async with provider.chat.completions.stream(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            stream_options={"include_usage": True},
        ) as stream:
            events = [event async for event in stream]

        assert events
        [trace] = storage.list_traces()
        assert trace.temperature is None
        assert trace.max_tokens is None
        assert trace.input_tokens == 2
        assert trace.output_tokens == 1
        assert trace.cost_usd is not None
    finally:
        instrumentor.uninstall()
        await http_client.aclose()
        storage.close()


@pytest.mark.parametrize("sentinel_name", ["omit", "NOT_GIVEN"])
def test_real_openai_explicit_unset_temperature_persists_exactly_one_row(
    tmp_path,
    sentinel_name,
):
    openai = pytest.importorskip("openai")
    from verdict.instrumentors.openai import OpenAIInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, f"openai-{sentinel_name}")
    instrumentor = OpenAIInstrumentor(verdict_client)
    http_client = httpx.Client(transport=httpx.MockTransport(_provider_response))
    instrumentor.install()
    try:
        provider = openai.OpenAI(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=0,
            http_client=http_client,
        )
        response = provider.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            temperature=getattr(openai, sentinel_name),
        )

        assert response.choices[0].message.content == "OK"
        [trace] = storage.list_traces()
        assert trace.temperature is None
        assert trace.input_tokens == 2
        assert trace.output_tokens == 1
        assert trace.cost_usd is not None
    finally:
        instrumentor.uninstall()
        http_client.close()
        storage.close()


@pytest.mark.parametrize("sentinel_name", ["omit", "NOT_GIVEN"])
def test_real_anthropic_unset_temperature_persists_exactly_one_row(
    tmp_path,
    sentinel_name,
):
    anthropic = pytest.importorskip("anthropic")
    from verdict.instrumentors.anthropic import AnthropicInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, f"anthropic-{sentinel_name}")
    instrumentor = AnthropicInstrumentor(verdict_client)
    http_client = anthropic.DefaultHttpxClient(
        transport=_anthropic_mock_transport(anthropic)
    )
    instrumentor.install()
    try:
        provider = anthropic.Anthropic(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=0,
            http_client=http_client,
        )
        response = provider.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
            **_anthropic_temperature_arg(
                provider.messages.create,
                getattr(anthropic, sentinel_name),
            ),
        )

        assert response.content[0].text == "OK"
        [trace] = storage.list_traces()
        assert trace.temperature is None
        assert trace.max_tokens == 8
        assert trace.input_tokens == 2
        assert trace.output_tokens == 1
        assert trace.cost_usd is not None
    finally:
        instrumentor.uninstall()
        http_client.close()
        storage.close()


async def test_real_async_anthropic_unset_temperature_persists_exactly_one_row(
    tmp_path,
):
    anthropic = pytest.importorskip("anthropic")
    from verdict.instrumentors.anthropic import AnthropicInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, "async-anthropic")
    instrumentor = AnthropicInstrumentor(verdict_client)
    http_client = anthropic.DefaultAsyncHttpxClient(
        transport=_anthropic_mock_transport(anthropic)
    )
    instrumentor.install()
    try:
        provider = anthropic.AsyncAnthropic(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=0,
            http_client=http_client,
        )
        response = await provider.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
            **_anthropic_temperature_arg(
                provider.messages.create,
                anthropic.omit,
            ),
        )

        assert response.content[0].text == "OK"
        [trace] = storage.list_traces()
        assert trace.temperature is None
        assert trace.max_tokens == 8
        assert trace.input_tokens == 2
        assert trace.output_tokens == 1
        assert trace.cost_usd is not None
    finally:
        instrumentor.uninstall()
        await http_client.aclose()
        storage.close()


@pytest.mark.parametrize(
    "consumer",
    ["events", "text_stream", "until_done", "final_message", "final_text"],
)
def test_real_anthropic_messages_stream_helper_persists_complete_trace(
    tmp_path,
    consumer,
):
    with _real_anthropic_client(
        tmp_path,
        f"anthropic-stream-{consumer}",
        capture_content=True,
    ) as (_, provider, storage):
        with provider.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "prompt@example.com"}],
        ) as stream:
            assert stream.response.headers["request-id"] == "request_test"
            if consumer == "events":
                assert list(stream)
            elif consumer == "text_stream":
                assert "".join(stream.text_stream) == "OK stream@example.com"
            elif consumer == "until_done":
                assert stream.until_done() is None
            elif consumer == "final_message":
                assert stream.get_final_message().content[0].text == "OK stream@example.com"
            else:
                assert stream.get_final_text() == "OK stream@example.com"

        [trace] = storage.list_traces()
        assert trace.request_model == "claude-haiku-4-5-20251001"
        assert trace.response_model == "claude-haiku-4-5-20251001"
        assert trace.input_tokens == 2
        assert trace.output_tokens == 3
        assert trace.finish_reason == "end_turn"
        assert trace.cost_usd is not None
        assert trace.tags["verdict.stream_completion"] == "complete"
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.response_redacted == "OK <EMAIL>"
        assert "prompt@example.com" not in repr(trace.raw_messages)


@pytest.mark.parametrize(
    "consumer",
    ["events", "text_stream", "until_done", "final_message", "final_text"],
)
async def test_real_async_anthropic_messages_stream_helper_persists_complete_trace(
    tmp_path,
    consumer,
):
    async with _real_async_anthropic_client(
        tmp_path,
        f"async-anthropic-stream-{consumer}",
        capture_content=True,
    ) as (_, provider, storage):
        async with provider.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "prompt@example.com"}],
        ) as stream:
            assert stream.response.headers["request-id"] == "request_test"
            if consumer == "events":
                assert [event async for event in stream]
            elif consumer == "text_stream":
                chunks = [text async for text in stream.text_stream]
                assert "".join(chunks) == "OK stream@example.com"
            elif consumer == "until_done":
                assert await stream.until_done() is None
            elif consumer == "final_message":
                final_message = await stream.get_final_message()
                assert final_message.content[0].text == "OK stream@example.com"
            else:
                assert await stream.get_final_text() == "OK stream@example.com"

        [trace] = storage.list_traces()
        assert trace.request_model == "claude-haiku-4-5-20251001"
        assert trace.response_model == "claude-haiku-4-5-20251001"
        assert trace.input_tokens == 2
        assert trace.output_tokens == 3
        assert trace.finish_reason == "end_turn"
        assert trace.cost_usd is not None
        assert trace.tags["verdict.stream_completion"] == "complete"
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.response_redacted == "OK <EMAIL>"
        assert "prompt@example.com" not in repr(trace.raw_messages)


@pytest.mark.parametrize("iterable_kind", ["iterator", "single-pass-iterable"])
@pytest.mark.parametrize("surface", ["create", "messages_stream_helper"])
def test_real_anthropic_messages_stream_helper_and_create_preserve_one_shot_input(
    tmp_path,
    surface,
    iterable_kind,
):
    wire_bodies = []

    def capture_wire(request):
        wire_bodies.append(json.loads(request.content))
        return _provider_response(request)

    with _real_anthropic_client(
        tmp_path,
        f"anthropic-one-shot-{surface}",
        responder=capture_wire,
        capture_content=True,
    ) as (_, provider, storage):
        if iterable_kind == "iterator":
            messages = (
                {"role": "user", "content": "one-shot@example.com"}
                for _ in range(1)
            )
        else:
            messages = _SinglePassMessages()
        if surface == "create":
            provider.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=messages,
            )
        else:
            with provider.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=messages,
            ) as stream:
                stream.until_done()

        assert wire_bodies[0]["messages"] == [
            {"role": "user", "content": "one-shot@example.com"}
        ]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.raw_messages == [
            {"role": "user", "content": "<EMAIL>"}
        ]
        if iterable_kind == "single-pass-iterable":
            assert messages.iterations == 1


@pytest.mark.parametrize("iterable_kind", ["iterator", "single-pass-iterable"])
@pytest.mark.parametrize("surface", ["create", "messages_stream_helper"])
async def test_real_async_anthropic_messages_stream_helper_and_create_preserve_one_shot_input(
    tmp_path,
    surface,
    iterable_kind,
):
    wire_bodies = []

    def capture_wire(request):
        wire_bodies.append(json.loads(request.content))
        return _provider_response(request)

    async with _real_async_anthropic_client(
        tmp_path,
        f"async-anthropic-one-shot-{surface}",
        responder=capture_wire,
        capture_content=True,
    ) as (_, provider, storage):
        if iterable_kind == "iterator":
            messages = (
                {"role": "user", "content": "one-shot@example.com"}
                for _ in range(1)
            )
        else:
            messages = _SinglePassMessages()
        if surface == "create":
            await provider.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=messages,
            )
        else:
            async with provider.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=messages,
            ) as stream:
                await stream.until_done()

        assert wire_bodies[0]["messages"] == [
            {"role": "user", "content": "one-shot@example.com"}
        ]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.raw_messages == [
            {"role": "user", "content": "<EMAIL>"}
        ]
        if iterable_kind == "single-pass-iterable":
            assert messages.iterations == 1


@pytest.mark.parametrize("surface", ["create", "raw-stream", "messages-stream-helper"])
def test_real_anthropic_messages_stream_helper_preserves_raising_iterable_boundary(
    tmp_path,
    surface,
):
    with _real_anthropic_client(
        tmp_path,
        f"anthropic-raising-messages-{surface}",
        capture_content=True,
    ) as (_, provider, storage):
        messages = _RaisingMessages()
        request = dict(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=messages,
        )
        if surface == "messages-stream-helper":
            assert messages.iterations == 0
            assert storage.list_traces() == []
            with pytest.raises(RuntimeError, match=r"failure@example\.com"):
                provider.messages.stream(**request)
        else:
            with pytest.raises(RuntimeError, match=r"failure@example\.com"):
                provider.messages.create(
                    **request,
                    stream=surface == "raw-stream",
                )

        assert messages.iterations == 1
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.raw_messages == [{"role": "user", "content": "<EMAIL>"}]
        assert trace.error == "RuntimeError: message iteration failed for <EMAIL>"
        expected_completion = None if surface == "create" else "error"
        assert trace.tags.get("verdict.stream_completion") == expected_completion


@pytest.mark.parametrize("surface", ["create", "raw-stream", "messages-stream-helper"])
async def test_real_async_anthropic_messages_stream_helper_preserves_raising_iterable_boundary(
    tmp_path,
    surface,
):
    async with _real_async_anthropic_client(
        tmp_path,
        f"async-anthropic-raising-messages-{surface}",
        capture_content=True,
    ) as (_, provider, storage):
        messages = _RaisingMessages()
        request = dict(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=messages,
        )
        if surface == "messages-stream-helper":
            assert messages.iterations == 0
            assert storage.list_traces() == []
            with pytest.raises(RuntimeError, match=r"failure@example\.com"):
                provider.messages.stream(**request)
        else:
            with pytest.raises(RuntimeError, match=r"failure@example\.com"):
                await provider.messages.create(
                    **request,
                    stream=surface == "raw-stream",
                )

        assert messages.iterations == 1
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.raw_messages == [{"role": "user", "content": "<EMAIL>"}]
        assert trace.error == "RuntimeError: message iteration failed for <EMAIL>"
        expected_completion = None if surface == "create" else "error"
        assert trace.tags.get("verdict.stream_completion") == expected_completion


def test_real_anthropic_messages_stream_helper_binds_context_on_each_entry(tmp_path):
    from verdict.client import clear_context, set_context
    from verdict.trace import span

    clear_context()
    with _real_anthropic_client(
        tmp_path,
        "anthropic-entry-context",
    ) as (_, provider, storage):
        manager = provider.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
        )
        set_context(session_id="entry-session", workload="entry-workload")
        try:
            with span("entry-parent") as parent:
                with manager as stream:
                    stream.until_done()
        finally:
            clear_context()

        [trace] = storage.list_traces()
        assert trace.session_id == "entry-session"
        assert trace.tags["verdict.workload"] == "entry-workload"
        assert trace.parent_span_id == parent.span_id


async def test_real_async_anthropic_messages_stream_helper_isolates_task_contexts(
    tmp_path,
):
    from verdict.client import clear_context, set_context

    clear_context()
    async with _real_async_anthropic_client(
        tmp_path,
        "async-anthropic-entry-context",
    ) as (_, provider, storage):
        async def consume(session_id):
            manager = provider.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=[{"role": "user", "content": session_id}],
            )
            set_context(session_id=session_id, workload=f"workload-{session_id}")
            async with manager as stream:
                await stream.until_done()

        await asyncio.gather(consume("one"), consume("two"))
        clear_context()

        traces = storage.list_traces()
        assert {trace.session_id for trace in traces} == {"one", "two"}
        assert {
            (trace.session_id, trace.tags["verdict.workload"])
            for trace in traces
        } == {("one", "workload-one"), ("two", "workload-two")}


def test_real_anthropic_messages_stream_helper_reentry_persists_two_traces(tmp_path):
    from verdict.client import clear_context, set_context

    request_bodies = []

    def responder(request):
        if request.url.path.endswith("/messages"):
            request_bodies.append(json.loads(request.content))
        return _provider_response(request)

    def messages():
        yield {"role": "user", "content": "repeatable"}

    with _real_anthropic_client(
        tmp_path,
        "anthropic-manager-reentry",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        manager = provider.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=messages(),
        )
        for entry in range(2):
            set_context(
                session_id=f"entry-{entry}",
                workload=f"workload-{entry}",
            )
            try:
                with manager as stream:
                    stream.until_done()
            finally:
                clear_context()

        traces = storage.list_traces()
        assert len(traces) == 2
        assert len({trace.trace_id for trace in traces}) == 2
        assert {
            (trace.session_id, trace.tags["verdict.workload"])
            for trace in traces
        } == {
            ("entry-0", "workload-0"),
            ("entry-1", "workload-1"),
        }
        assert {trace.prompt_redacted for trace in traces} == {"repeatable"}
        assert [body["messages"] for body in request_bodies] == [
            [{"role": "user", "content": "repeatable"}],
            [{"role": "user", "content": "repeatable"}],
        ]


def test_real_anthropic_messages_stream_helper_preserves_captured_empty_text(
    tmp_path,
):
    with _real_anthropic_client(
        tmp_path,
        "anthropic-empty-text",
        responder=_empty_anthropic_provider_response,
        capture_content=True,
    ) as (_, provider, storage):
        with provider.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            assert stream.get_final_text() == ""

        [trace] = storage.list_traces()
        assert trace.response_redacted == ""
        assert trace.tags["verdict.stream_completion"] == "complete"


@pytest.mark.parametrize("is_async", [False, True])
async def test_real_anthropic_messages_stream_helper_does_not_buffer_disabled_content(
    tmp_path,
    is_async,
):
    messages = _SinglePassMessages("hi")
    if is_async:
        async with _real_async_anthropic_client(
            tmp_path,
            "async-anthropic-no-buffer",
            capture_content=False,
        ) as (_, provider, storage):
            async with provider.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=messages,
            ) as stream:
                await stream.until_done()
                assert stream._text_chunks == []
            [trace] = storage.list_traces()
    else:
        with _real_anthropic_client(
            tmp_path,
            "anthropic-no-buffer",
            capture_content=False,
        ) as (_, provider, storage):
            with provider.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=messages,
            ) as stream:
                stream.until_done()
                assert stream._text_chunks == []
            [trace] = storage.list_traces()

    assert trace.response_redacted is None
    assert messages.iterations == 1


def test_real_anthropic_create_stream_preserves_raw_stream_surface(tmp_path):
    with _real_anthropic_client(
        tmp_path, "anthropic-raw-stream"
    ) as (_, provider, storage):
        stream = provider.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            stream=True,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert not hasattr(stream, "text_stream")
        assert not hasattr(stream, "get_final_message")
        assert list(stream)

        [trace] = storage.list_traces()
        assert trace.input_tokens == 2
        assert trace.output_tokens == 3
        assert trace.tags["verdict.stream_completion"] == "complete"


async def test_real_async_anthropic_create_stream_preserves_raw_stream_surface(
    tmp_path,
):
    async with _real_async_anthropic_client(
        tmp_path, "async-anthropic-raw-stream"
    ) as (_, provider, storage):
        stream = await provider.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            stream=True,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert not hasattr(stream, "text_stream")
        assert not hasattr(stream, "get_final_message")
        assert [event async for event in stream]

        [trace] = storage.list_traces()
        assert trace.input_tokens == 2
        assert trace.output_tokens == 3
        assert trace.tags["verdict.stream_completion"] == "complete"


@pytest.mark.parametrize("action", ["no-consumption", "partial", "explicit-close"])
def test_real_anthropic_messages_stream_helper_persists_one_partial_trace(
    tmp_path,
    action,
):
    with _real_anthropic_client(
        tmp_path, f"anthropic-partial-{action}"
    ) as (_, provider, storage):
        manager = provider.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert storage.list_traces() == []
        with manager as stream:
            if action == "partial":
                next(stream)
            elif action == "explicit-close":
                stream.close()

        [trace] = storage.list_traces()
        assert trace.finish_reason is None
        assert trace.error is None
        assert trace.tags["verdict.stream_completion"] == "partial"


@pytest.mark.parametrize("action", ["no-consumption", "partial", "explicit-close"])
async def test_real_async_anthropic_messages_stream_helper_persists_one_partial_trace(
    tmp_path,
    action,
):
    async with _real_async_anthropic_client(
        tmp_path, f"async-anthropic-partial-{action}"
    ) as (_, provider, storage):
        manager = provider.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert storage.list_traces() == []
        async with manager as stream:
            if action == "partial":
                await anext(stream)
            elif action == "explicit-close":
                await stream.close()

        [trace] = storage.list_traces()
        assert trace.finish_reason is None
        assert trace.error is None
        assert trace.tags["verdict.stream_completion"] == "partial"


@pytest.mark.parametrize(
    "surface",
    ["messages_stream_helper", "raw-create-stream", "non-stream-create"],
)
@pytest.mark.parametrize("failure", ["provider-status", "transport-timeout"])
def test_real_anthropic_stream_and_nonstream_entry_failure_tags_match_boundary(
    tmp_path,
    surface,
    failure,
):
    anthropic = pytest.importorskip("anthropic")

    transport_module = _anthropic_transport_module(anthropic)

    def fail(request):
        if failure == "provider-status":
            return httpx.Response(
                500,
                json={"error": {"message": "failed for provider-error@example.com"}},
            )
        raise transport_module.ReadTimeout(
            "timed out for provider-timeout@example.com",
            request=request,
        )

    with _real_anthropic_client(
        tmp_path,
        f"anthropic-{failure}",
        responder=fail,
        sample_rate=0.0,
    ) as (_, provider, storage):
        with pytest.raises(anthropic.APIError):
            request = dict(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=[{"role": "user", "content": "hi"}],
            )
            if surface == "messages_stream_helper":
                with provider.messages.stream(**request):
                    pass
            elif surface == "raw-create-stream":
                provider.messages.create(**request, stream=True)
            else:
                provider.messages.create(**request)

        [trace] = storage.list_traces()
        assert trace.error
        expected_completion = None if surface == "non-stream-create" else "error"
        assert trace.tags.get("verdict.stream_completion") == expected_completion
        assert "provider-error@example.com" not in trace.error
        assert "provider-timeout@example.com" not in trace.error


def test_real_anthropic_messages_stream_helper_retry_persists_exactly_one_trace(
    tmp_path,
):
    attempts = 0

    def fail_once(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                500,
                headers={"retry-after-ms": "0"},
                json={"error": {"message": "retryable"}},
            )
        return _provider_response(request)

    with _real_anthropic_client(
        tmp_path,
        "anthropic-stream-retry",
        responder=fail_once,
        max_retries=1,
    ) as (_, provider, storage):
        with provider.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            stream.until_done()

        assert attempts == 2
        [trace] = storage.list_traces()
        assert trace.error is None
        assert trace.tags["verdict.stream_completion"] == "complete"


def test_real_anthropic_messages_stream_helper_block_error_persists_once(tmp_path):
    with _real_anthropic_client(
        tmp_path, "anthropic-stream-block-error"
    ) as (_, provider, storage):
        with pytest.raises(RuntimeError, match="application block failed"):
            with provider.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=[{"role": "user", "content": "hi"}],
            ):
                raise RuntimeError("application block failed for block@example.com")

        [trace] = storage.list_traces()
        assert trace.tags["verdict.stream_completion"] == "error"
        assert trace.error == "RuntimeError: application block failed for <EMAIL>"


def test_real_anthropic_messages_stream_helper_ignores_persistence_failure(caplog):
    class StreamFailureStorage:
        def insert_trace(self, _trace):
            raise RuntimeError("storage failed for persistence@example.com")

    caplog.set_level(logging.WARNING, logger="verdict.instrumentors")
    with _real_anthropic_client(
        None,
        "anthropic-persistence-failure",
        storage=StreamFailureStorage(),
    ) as (_, provider, _storage):
        with provider.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            assert stream.get_final_text() == "OK stream@example.com"

        [record] = [
            record
            for record in caplog.records
            if record.name == "verdict.instrumentors"
            and "StreamFailureStorage" in record.getMessage()
        ]
        assert "persistence@example.com" not in record.getMessage()


@pytest.mark.parametrize(
    "surface",
    ["messages-stream-helper", "raw-create-stream", "non-stream-create"],
)
async def test_real_async_anthropic_messages_stream_helper_and_create_entry_cancellation_persists(
    tmp_path,
    surface,
):
    anthropic = pytest.importorskip("anthropic")
    from verdict.instrumentors.anthropic import AnthropicInstrumentor

    transport_module = _anthropic_transport_module(anthropic)
    request_started = asyncio.Event()

    async def pending_response(_request):
        request_started.set()
        await asyncio.Event().wait()

    verdict_client, storage = _sqlite_client(tmp_path, "async-anthropic-cancel")
    instrumentor = AnthropicInstrumentor(verdict_client)
    http_client = anthropic.DefaultAsyncHttpxClient(
        transport=transport_module.MockTransport(pending_response)
    )
    instrumentor.install()
    try:
        provider = anthropic.AsyncAnthropic(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=0,
            http_client=http_client,
        )

        async def make_request():
            request = dict(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                messages=[{"role": "user", "content": "hi"}],
            )
            if surface == "messages-stream-helper":
                async with provider.messages.stream(**request):
                    pass
            elif surface == "raw-create-stream":
                await provider.messages.create(**request, stream=True)
            else:
                await provider.messages.create(**request)

        task = asyncio.create_task(make_request())
        await request_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        [trace] = storage.list_traces()
        assert trace.error is not None
        assert trace.error.startswith("CancelledError:")
        expected_completion = None if surface == "non-stream-create" else "error"
        assert trace.tags.get("verdict.stream_completion") == expected_completion
    finally:
        instrumentor.uninstall()
        await http_client.aclose()
        storage.close()


def test_real_google_config_persists_exactly_one_row(tmp_path):
    pytest.importorskip("google.genai")
    from google import genai
    from google.genai import types
    from verdict.instrumentors.google import GoogleInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, "google")
    instrumentor = GoogleInstrumentor(verdict_client)
    http_client = httpx.Client(transport=httpx.MockTransport(_provider_response))
    instrumentor.install()
    try:
        provider = genai.Client(
            api_key="test",
            http_options=types.HttpOptions(
                base_url="http://provider.test",
                httpx_client=http_client,
            ),
        )
        response = provider.models.generate_content(
            model="gemini-2.5-flash",
            contents="hi",
            config=types.GenerateContentConfig(max_output_tokens=8),
        )

        assert response.text == "OK"
        [trace] = storage.list_traces()
        assert trace.temperature is None
        assert trace.max_tokens == 8
        assert trace.input_tokens == 2
        assert trace.output_tokens == 1
        assert trace.cost_usd is not None
    finally:
        instrumentor.uninstall()
        http_client.close()
        storage.close()


def test_google_rejects_nonprimitive_config_scalars_before_sqlite(tmp_path):
    from verdict.instrumentors.google import GoogleInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, "google-invalid-config")
    instrumentor = GoogleInstrumentor(verdict_client)
    response = SimpleNamespace(
        text="OK",
        usage_metadata=SimpleNamespace(prompt_token_count=2, candidates_token_count=1),
        candidates=[SimpleNamespace(finish_reason="STOP")],
    )
    config = SimpleNamespace(temperature=object(), max_output_tokens=True)
    try:
        returned = instrumentor._wrap_genai_generate(
            lambda *_args, **_kwargs: response,
            None,
            (),
            {"model": "gemini-2.5-flash", "contents": "hi", "config": config},
        )

        assert returned is response
        [trace] = storage.list_traces()
        assert trace.temperature is None
        assert trace.max_tokens is None
        assert trace.input_tokens == 2
        assert trace.output_tokens == 1
        assert trace.cost_usd is not None
    finally:
        storage.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", object()),
        ("temperature", True),
        ("temperature", math.nan),
        ("temperature", math.inf),
        ("max_tokens", object()),
        ("max_tokens", True),
        ("max_tokens", 2**31),
        ("input_tokens", object()),
        ("input_tokens", -1),
        ("output_tokens", True),
        ("latency_ms", math.inf),
        ("cost_usd", -0.01),
    ],
)
def test_trace_schema_normalizes_unstorable_or_invalid_scalars(field, value):
    trace = Trace(**{field: value})
    assert getattr(trace, field) is None


def test_trace_schema_preserves_valid_scalar_values_and_types():
    trace = Trace(
        temperature=1,
        max_tokens=0,
        input_tokens=0,
        output_tokens=1,
        latency_ms=0,
        cost_usd=0,
    )
    assert trace.temperature == 1.0
    assert isinstance(trace.temperature, float)
    assert trace.max_tokens == 0
    assert trace.input_tokens == 0
    assert trace.output_tokens == 1
    assert trace.latency_ms == 0.0
    assert trace.cost_usd == 0.0


def test_persist_trace_revalidates_scalars_mutated_after_construction(tmp_path):
    verdict_client, storage = _sqlite_client(tmp_path, "mutated-trace")
    trace = Trace(provider="custom-provider")
    trace.temperature = object()
    trace.input_tokens = object()
    try:
        persist_trace(verdict_client, trace)
        [stored] = storage.list_traces()
        assert stored.temperature is None
        assert stored.input_tokens is None
    finally:
        storage.close()


def test_persistence_failure_warns_once_under_concurrency_without_raising(caplog):
    from verdict.instrumentors.openai import OpenAIInstrumentor

    class ForcedPersistenceError(RuntimeError):
        pass

    class FailingStorage:
        def insert_trace(self, _trace):
            raise ForcedPersistenceError(
                "do-not-log-this-secret@example.com from 2001:db8::1: timeout"
            )

    instrumentor = OpenAIInstrumentor(VerdictClient(storage=FailingStorage()))
    errors: list[BaseException] = []

    def persist() -> None:
        try:
            instrumentor._safe_persist(Trace(provider="openai"))
        except BaseException as exc:  # pragma: no cover - assertion records failure
            errors.append(exc)

    caplog.set_level(logging.WARNING, logger="verdict.instrumentors")
    threads = [threading.Thread(target=persist) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = [
        record for record in caplog.records
        if record.name == "verdict.instrumentors"
    ]
    assert errors == []
    assert len(records) == 1
    assert "ForcedPersistenceError" in records[0].getMessage()
    assert "openai" in records[0].getMessage()
    assert "do-not-log-this-secret@example.com" not in records[0].getMessage()
    assert "2001:db8::1" not in records[0].getMessage()
