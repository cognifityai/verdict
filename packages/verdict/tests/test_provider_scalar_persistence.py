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
from collections import UserDict
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import httpx
import pytest
import verdict.instrumentors.openai as openai_instrumentor
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


class _SinglePassList(list):
    """Valid list subclass whose custom traversal can be consumed only once."""

    def __init__(self, content: str = "wire@example.com") -> None:
        super().__init__([{"role": "user", "content": content}])
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations > 1:
            return iter(())
        return super().__iter__()


class _DrainingList(list):
    """Valid list subclass that removes each item as the SDK requests it."""

    def __init__(self) -> None:
        super().__init__([{"role": "user", "content": "drained@example.com"}])

    def __iter__(self):
        while self:
            yield self.pop(0)


class _CancellingList(list):
    """Valid list whose provider-owned traversal is cancelled before send."""

    def __init__(self) -> None:
        super().__init__([{"role": "user", "content": "never-sent"}])
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        raise asyncio.CancelledError("local list traversal")


class _RaisingMessages:
    """Re-iterable input whose provider-owned traversal fails after one item."""

    def __init__(self) -> None:
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        yield {"role": "user", "content": "prefix@example.com"}
        raise RuntimeError("message iteration failed for failure@example.com")


def _openai_response_payload(
    *,
    status: str = "completed",
    text: str = "OK response@example.com",
) -> dict[str, object]:
    error = None
    if status == "failed":
        error = {"code": "server_error", "message": "failed for error@example.com"}
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "status": status,
        "background": False,
        "error": error,
        "incomplete_details": ({"reason": "max_output_tokens"} if status == "incomplete" else None),
        "instructions": None,
        "max_output_tokens": 8,
        "max_tool_calls": None,
        "model": "gpt-4o-mini",
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        ],
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "prompt_cache_key": None,
        "reasoning": {"effort": None, "summary": None},
        "safety_identifier": None,
        "service_tier": "default",
        "store": True,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_logprobs": 0,
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": {
            "input_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 3,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 5,
        },
        "user": None,
        "metadata": {},
    }


def _openai_responses_sse(
    *,
    status: str = "completed",
    text: str = "OK response@example.com",
    include_terminal: bool = True,
) -> bytes:
    created_response = _openai_response_payload(status="completed", text="")
    created_response["status"] = "in_progress"
    created_response["output"] = []
    created_response["usage"] = None
    events: list[dict[str, object]] = [
        {
            "type": "response.created",
            "response": created_response,
            "sequence_number": 0,
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "msg_test",
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
            "sequence_number": 1,
        },
        {
            "type": "response.content_part.added",
            "item_id": "msg_test",
            "output_index": 0,
            "content_index": 0,
            "part": {
                "type": "output_text",
                "text": "",
                "annotations": [],
                "logprobs": [],
            },
            "sequence_number": 2,
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_test",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
            "logprobs": [],
            "sequence_number": 3,
        },
    ]
    if include_terminal:
        events.append(
            {
                "type": f"response.{status}",
                "response": _openai_response_payload(status=status, text=text),
                "sequence_number": 4,
            }
        )
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return (body + "data: [DONE]\n\n").encode()


def _openai_responses_done_only_sse(
    *,
    kind: str,
    text: str,
    include_delta: bool = False,
) -> bytes:
    events = [
        json.loads(line.removeprefix("data: "))
        for line in _openai_responses_sse(text="", include_terminal=False).decode().splitlines()
        if line.startswith("data: {")
    ]
    if not include_delta:
        events = [event for event in events if not event["type"].endswith(".delta")]
    else:
        events[-1]["delta"] = text
    event = {
        "type": f"response.{kind}.done",
        "item_id": "msg_test",
        "output_index": 0,
        "content_index": 0,
        "sequence_number": 3,
    }
    event["text" if kind == "output_text" else "refusal"] = text
    if kind == "output_text":
        event["logprobs"] = []
    events.append(event)
    body = "".join(f"data: {json.dumps(item)}\n\n" for item in events)
    return (body + "data: [DONE]\n\n").encode()


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
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "OK"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
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
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )

    if request.url.path.endswith("/responses") or "/responses/" in request.url.path:
        payload = json.loads(request.content) if request.content else {}
        if payload.get("stream") or request.url.params.get("stream") == "true":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_openai_responses_sse(),
            )
        return httpx.Response(200, json=_openai_response_payload())

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
                f"event: {name}\ndata: {json.dumps(event)}\n\n" for name, event in events
            )
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "request-id": "request_test",
                },
                content=body.encode(),
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    if request.url.path.endswith(":generateContent"):
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "OK"}], "role": "model"},
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 3,
                },
                "modelVersion": "gemini-2.5-flash",
                "responseId": "google-test",
            },
        )

    return httpx.Response(404, json={"error": {"message": request.url.path}})


def _empty_anthropic_provider_response(request: httpx.Request) -> httpx.Response:
    """Return the valid Anthropic SSE fixture with a captured empty text value."""
    response = _provider_response(request)
    if request.url.path.endswith("/messages") and json.loads(request.content).get("stream"):
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


def test_openai_minimum_default_client_constructor():
    """The declared minimum dependency set supports the ordinary SDK path."""
    openai = pytest.importorskip("openai")
    provider = openai.OpenAI(api_key="test")
    provider.close()


@contextmanager
def _real_openai_client(
    tmp_path,
    name: str,
    *,
    responder=_provider_response,
    capture_content: bool = False,
    sample_rate: float = 1.0,
    max_retries: int = 0,
    request_hooks=(),
):
    openai = pytest.importorskip("openai")
    from verdict.instrumentors.openai import OpenAIInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, name)
    verdict_client.capture_content = capture_content
    verdict_client.sample_rate = sample_rate
    instrumentor = OpenAIInstrumentor(verdict_client)
    http_client = httpx.Client(
        transport=httpx.MockTransport(responder),
        event_hooks={"request": list(request_hooks)},
    )
    instrumentor.install()
    try:
        provider = openai.OpenAI(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=max_retries,
            http_client=http_client,
        )
        yield openai, provider, storage
    finally:
        instrumentor.uninstall()
        http_client.close()
        storage.close()


@asynccontextmanager
async def _real_async_openai_client(
    tmp_path,
    name: str,
    *,
    responder=_provider_response,
    capture_content: bool = False,
    sample_rate: float = 1.0,
    max_retries: int = 0,
):
    openai = pytest.importorskip("openai")
    from verdict.instrumentors.openai import OpenAIInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, name)
    verdict_client.capture_content = capture_content
    verdict_client.sample_rate = sample_rate
    instrumentor = OpenAIInstrumentor(verdict_client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    instrumentor.install()
    try:
        provider = openai.AsyncOpenAI(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=max_retries,
            http_client=http_client,
        )
        yield openai, provider, storage
    finally:
        instrumentor.uninstall()
        await http_client.aclose()
        storage.close()


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


def test_real_openai_chat_create_stream_declared_minimum_surface_persists_one_row(
    tmp_path,
):
    openai = pytest.importorskip("openai")
    from verdict.instrumentors.openai import OpenAIInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, "openai-raw-stream")
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
        stream = provider.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        assert list(stream)

        [trace] = storage.list_traces()
        assert trace.input_tokens == 2
        assert trace.output_tokens == 1
        assert trace.finish_reason == "stop"
    finally:
        instrumentor.uninstall()
        http_client.close()
        storage.close()


async def test_real_async_openai_chat_create_stream_declared_minimum_surface_persists_one_row(
    tmp_path,
):
    openai = pytest.importorskip("openai")
    from verdict.instrumentors.openai import OpenAIInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, "async-openai-raw-stream")
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
        stream = await provider.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        assert [event async for event in stream]

        [trace] = storage.list_traces()
        assert trace.input_tokens == 2
        assert trace.output_tokens == 1
        assert trace.finish_reason == "stop"
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


@pytest.mark.parametrize("surface", ["create", "parse"])
def test_real_openai_responses_nonstream_persists_one_redacted_trace(
    tmp_path,
    surface,
):
    with _real_openai_client(
        tmp_path,
        f"openai-responses-{surface}",
        capture_content=True,
    ) as (_, provider, storage):
        method = getattr(provider.responses, surface)
        response = method(
            model="gpt-4o-mini",
            input="prompt@example.com",
            instructions="system@example.com",
            max_output_tokens=8,
        )

        assert response.output_text == "OK response@example.com"
        [trace] = storage.list_traces()
        assert trace.request_model == "gpt-4o-mini"
        assert trace.response_model == "gpt-4o-mini"
        assert trace.max_tokens == 8
        assert trace.input_tokens == 2
        assert trace.output_tokens == 3
        assert trace.finish_reason == "completed"
        assert trace.prompt_redacted == "<EMAIL>\n<EMAIL>"
        assert trace.response_redacted == "OK <EMAIL>"
        assert "prompt@example.com" not in repr(trace.raw_messages)


@pytest.mark.parametrize("surface", ["create", "parse"])
async def test_real_async_openai_responses_nonstream_persists_one_trace(
    tmp_path,
    surface,
):
    async with _real_async_openai_client(
        tmp_path,
        f"async-openai-responses-{surface}",
        capture_content=True,
    ) as (_, provider, storage):
        method = getattr(provider.responses, surface)
        response = await method(
            model="gpt-4o-mini",
            input="prompt@example.com",
            max_output_tokens=8,
        )

        assert response.output_text == "OK response@example.com"
        [trace] = storage.list_traces()
        assert trace.input_tokens == 2
        assert trace.output_tokens == 3
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.response_redacted == "OK <EMAIL>"


@pytest.mark.parametrize("consumer", ["events", "until_done", "final_response"])
def test_real_openai_responses_stream_helper_persists_exactly_one_trace(
    tmp_path,
    consumer,
):
    with _real_openai_client(
        tmp_path,
        f"openai-responses-helper-{consumer}",
        capture_content=True,
    ) as (_, provider, storage):
        with provider.responses.stream(
            model="gpt-4o-mini",
            input="prompt@example.com",
            max_output_tokens=8,
        ) as stream:
            if consumer == "events":
                assert list(stream)
            elif consumer == "until_done":
                assert stream.until_done() is stream
            else:
                assert stream.get_final_response().output_text == ("OK response@example.com")

        [trace] = storage.list_traces()
        assert trace.tags["verdict.stream_completion"] == "complete"
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.response_redacted == "OK <EMAIL>"
        assert trace.input_tokens == 2
        assert trace.output_tokens == 3


@pytest.mark.parametrize("consumer", ["events", "until_done", "final_response"])
async def test_real_async_openai_responses_stream_helper_persists_one_trace(
    tmp_path,
    consumer,
):
    async with _real_async_openai_client(
        tmp_path,
        f"async-openai-responses-helper-{consumer}",
        capture_content=True,
    ) as (_, provider, storage):
        async with provider.responses.stream(
            model="gpt-4o-mini",
            input="prompt@example.com",
            max_output_tokens=8,
        ) as stream:
            if consumer == "events":
                assert [event async for event in stream]
            elif consumer == "until_done":
                assert await stream.until_done() is stream
            else:
                response = await stream.get_final_response()
                assert response.output_text == "OK response@example.com"

        [trace] = storage.list_traces()
        assert trace.tags["verdict.stream_completion"] == "complete"
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.response_redacted == "OK <EMAIL>"


def test_real_openai_responses_existing_response_helper_uses_retrieve_trace(tmp_path):
    with _real_openai_client(
        tmp_path,
        "openai-responses-existing-helper",
        capture_content=True,
    ) as (_, provider, storage):
        with provider.responses.stream(response_id="resp_test") as stream:
            assert stream.get_final_response().output_text == "OK response@example.com"

        [trace] = storage.list_traces()
        assert trace.request_model == ""
        assert trace.response_model == "gpt-4o-mini"
        assert trace.tags["verdict.stream_completion"] == "complete"
        assert trace.response_redacted == "OK <EMAIL>"


def test_real_openai_responses_raw_stream_supports_next_and_partial_close(tmp_path):
    with _real_openai_client(
        tmp_path,
        "openai-responses-raw-partial",
        capture_content=True,
    ) as (_, provider, storage):
        stream = provider.responses.create(
            model="gpt-4o-mini",
            input="prompt@example.com",
            stream=True,
        )
        events = [next(stream) for _ in range(4)]
        assert events[-1].type == "response.output_text.delta"
        stream.close()

        [trace] = storage.list_traces()
        assert trace.tags["verdict.stream_completion"] == "partial"
        assert trace.response_redacted == "OK <EMAIL>"


async def test_real_async_openai_responses_raw_stream_supports_anext_and_partial_close(
    tmp_path,
):
    async with _real_async_openai_client(
        tmp_path,
        "async-openai-responses-raw-partial",
        capture_content=True,
    ) as (_, provider, storage):
        stream = await provider.responses.create(
            model="gpt-4o-mini",
            input="prompt@example.com",
            stream=True,
        )
        events = [await anext(stream) for _ in range(4)]
        assert events[-1].type == "response.output_text.delta"
        await stream.aclose()

        [trace] = storage.list_traces()
        assert trace.tags["verdict.stream_completion"] == "partial"
        assert trace.response_redacted == "OK <EMAIL>"


def test_openai_helpers_finalize_partial_and_do_not_buffer_content_when_disabled(
    tmp_path,
):
    with _real_openai_client(
        tmp_path,
        "openai-helper-partial-content-off",
    ) as (_, provider, storage):
        with provider.responses.stream(
            model="gpt-4o-mini",
            input="hi",
        ) as stream:
            events = [next(stream) for _ in range(4)]
            assert events[-1].type == "response.output_text.delta"
            assert stream._raw_stream._text_chunks == []

        with provider.chat.completions.stream(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            next(stream)
            assert stream._raw_stream._text_chunks == []

        traces = storage.list_traces()
        assert len(traces) == 2
        response_trace = next(
            trace for trace in traces if "verdict.stream_completion" in trace.tags
        )
        assert response_trace.tags["verdict.stream_completion"] == "partial"
        assert all(trace.response_redacted is None for trace in traces)


def test_real_openai_responses_complete_stream_retains_no_disabled_content(tmp_path):
    with _real_openai_client(
        tmp_path,
        "openai-responses-complete-content-off",
    ) as (_, provider, storage):
        with provider.responses.stream(
            model="gpt-4o-mini",
            input="hi",
        ) as stream:
            raw_stream = stream._raw_stream
            stream.until_done()

        assert raw_stream._text_chunks == []
        assert not hasattr(raw_stream, "_terminal_response")
        [trace] = storage.list_traces()
        assert trace.response_redacted is None
        assert trace.tags["verdict.stream_completion"] == "complete"


def test_real_openai_failed_response_persists_when_success_sampling_is_zero(tmp_path):
    def failed_response(request):
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json=_openai_response_payload(status="failed"))
        return _provider_response(request)

    with _real_openai_client(
        tmp_path,
        "openai-failed-response",
        responder=failed_response,
        capture_content=True,
        sample_rate=0.0,
    ) as (_, provider, storage):
        response = provider.responses.create(model="gpt-4o-mini", input="hi")
        assert response.status == "failed"

        [trace] = storage.list_traces()
        assert trace.error == "server_error: failed for <EMAIL>"


@pytest.mark.parametrize(
    ("status", "is_error"),
    [("queued", False), ("in_progress", False), ("cancelled", True)],
)
def test_real_openai_responses_preserves_every_nonstream_status(
    tmp_path,
    status,
    is_error,
):
    def responder(_request):
        return httpx.Response(200, json=_openai_response_payload(status=status))

    with _real_openai_client(
        tmp_path,
        f"openai-responses-status-{status}",
        responder=responder,
        sample_rate=0.0 if is_error else 1.0,
    ) as (_, provider, storage):
        response = provider.responses.create(model="gpt-4o-mini", input="hi")
        assert response.status == status

        [trace] = storage.list_traces()
        assert trace.finish_reason == status
        assert (trace.error is not None) is is_error


@pytest.mark.parametrize(
    ("status", "expected_completion", "expected_finish", "has_error"),
    [
        ("completed", "complete", "completed", False),
        ("incomplete", "complete", "max_output_tokens", False),
        ("failed", "error", None, True),
    ],
)
def test_real_openai_responses_stream_terminal_states_are_distinguished(
    tmp_path,
    status,
    expected_completion,
    expected_finish,
    has_error,
):
    def responder(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_openai_responses_sse(status=status),
        )

    with _real_openai_client(
        tmp_path,
        f"openai-responses-terminal-{status}",
        responder=responder,
        capture_content=True,
        sample_rate=0.0 if has_error else 1.0,
    ) as (_, provider, storage):
        stream = provider.responses.create(
            model="gpt-4o-mini",
            input="prompt@example.com",
            stream=True,
        )
        assert list(stream)

        [trace] = storage.list_traces()
        assert trace.tags["verdict.stream_completion"] == expected_completion
        assert trace.finish_reason == expected_finish
        assert (trace.error is not None) is has_error
        if has_error:
            assert trace.error == "server_error: failed for <EMAIL>"


@pytest.mark.parametrize("surface", ["create", "stream"])
def test_real_openai_responses_preserves_captured_empty_output(
    tmp_path,
    surface,
):
    def responder(request):
        payload = json.loads(request.content)
        if payload.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_openai_responses_sse(text=""),
            )
        return httpx.Response(200, json=_openai_response_payload(text=""))

    with _real_openai_client(
        tmp_path,
        f"openai-responses-empty-{surface}",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        if surface == "create":
            provider.responses.create(model="gpt-4o-mini", input="")
        else:
            with provider.responses.stream(model="gpt-4o-mini", input="") as stream:
                stream.until_done()

        [trace] = storage.list_traces()
        assert trace.prompt_redacted == ""
        assert trace.response_redacted == ""


def test_real_openai_responses_helper_records_application_block_error(tmp_path):
    with _real_openai_client(
        tmp_path,
        "openai-responses-application-error",
    ) as (_, provider, storage):
        with pytest.raises(RuntimeError, match=r"block@example\.com"):
            with provider.responses.stream(model="gpt-4o-mini", input="hi") as stream:
                stream.until_done()
                raise RuntimeError("application block failed for block@example.com")

        [trace] = storage.list_traces()
        assert trace.tags["verdict.stream_completion"] == "error"
        assert trace.error == "RuntimeError: application block failed for <EMAIL>"


def test_real_openai_responses_helper_binds_routing_context_on_entry(tmp_path):
    from verdict.client import clear_context, set_context
    from verdict.trace import span

    clear_context()
    with _real_openai_client(
        tmp_path,
        "openai-responses-entry-context",
    ) as (_, provider, storage):
        manager = provider.responses.stream(model="gpt-4o-mini", input="hi")
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


@pytest.mark.parametrize("surface", ["responses", "chat"])
def test_real_openai_preconstructed_helper_stays_inactive_after_shutdown(
    tmp_path,
    surface,
):
    import openai
    from verdict.client import init, shutdown

    database = tmp_path / f"openai-stale-helper-{surface}.db"
    storage = SQLiteStorage(str(database))
    http_client = httpx.Client(transport=httpx.MockTransport(_provider_response))
    shutdown()
    try:
        init(storage=storage, instrumentors=["openai"])
        provider = openai.OpenAI(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=0,
            http_client=http_client,
        )
        if surface == "responses":
            manager = provider.responses.stream(model="gpt-4o-mini", input="hi")
        else:
            manager = provider.chat.completions.stream(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hi"}],
            )

        shutdown()
        with manager as stream:
            assert list(stream)

        reopened = SQLiteStorage(str(database))
        try:
            assert reopened.list_traces() == []
        finally:
            reopened.close()
    finally:
        shutdown()
        http_client.close()


@pytest.mark.parametrize("surface", ["responses", "chat"])
async def test_real_async_openai_preconstructed_helper_stays_inactive_after_uninstall(
    tmp_path,
    surface,
):
    openai = pytest.importorskip("openai")
    from verdict.instrumentors.openai import OpenAIInstrumentor

    verdict_client, storage = _sqlite_client(tmp_path, f"async-stale-{surface}")
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
        if surface == "responses":
            manager = provider.responses.stream(model="gpt-4o-mini", input="hi")
        else:
            manager = provider.chat.completions.stream(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hi"}],
            )

        instrumentor.uninstall()
        async with manager as stream:
            assert [event async for event in stream]

        assert storage.list_traces() == []
    finally:
        instrumentor.uninstall()
        await http_client.aclose()
        storage.close()


def test_real_openai_responses_retry_persists_only_the_final_request_trace(tmp_path):
    attempts = 0

    def responder(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, json={"error": {"message": "retry"}})
        return _provider_response(request)

    with _real_openai_client(
        tmp_path,
        "openai-responses-retry",
        responder=responder,
        max_retries=1,
    ) as (_, provider, storage):
        provider.responses.create(model="gpt-4o-mini", input="hi")

        assert attempts == 2
        assert len(storage.list_traces()) == 1


def test_real_openai_beta_chat_alias_shares_the_instrumented_resource_class(tmp_path):
    with _real_openai_client(
        tmp_path,
        "openai-beta-aliases",
    ) as (_, provider, storage):
        with provider.beta.chat.completions.stream(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            list(stream)

        assert len(storage.list_traces()) == 1


def test_real_openai_responses_structured_tool_input_is_recursively_redacted(tmp_path):
    request_input = [
        {"role": "user", "content": "question@example.com"},
        {
            "type": "function_call_output",
            "call_id": "call_test",
            "output": '{"email":"tool@example.com"}',
        },
    ]
    with _real_openai_client(
        tmp_path,
        "openai-responses-structured-input",
        capture_content=True,
    ) as (_, provider, storage):
        provider.responses.create(model="gpt-4o-mini", input=request_input)

        [trace] = storage.list_traces()
        assert trace.prompt_redacted == '<EMAIL>\n{"email":"<EMAIL>"}'
        assert "question@example.com" not in repr(trace.raw_messages)
        assert "tool@example.com" not in repr(trace.raw_messages)


@pytest.mark.parametrize("surface", ["create", "parse", "raw", "helper"])
def test_real_openai_responses_preserves_single_pass_list_input(
    tmp_path,
    surface,
):
    request_bodies = []

    def responder(request):
        request_bodies.append(json.loads(request.content))
        return _provider_response(request)

    value = _SinglePassList()
    with _real_openai_client(
        tmp_path,
        f"openai-responses-single-pass-list-{surface}",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        if surface in ("create", "parse"):
            getattr(provider.responses, surface)(
                model="gpt-4o-mini",
                input=value,
            )
        elif surface == "raw":
            stream = provider.responses.create(
                model="gpt-4o-mini",
                input=value,
                stream=True,
            )
            list(stream)
        else:
            with provider.responses.stream(
                model="gpt-4o-mini",
                input=value,
            ) as stream:
                stream.until_done()

        assert value.iterations == 1
        assert len(request_bodies) == 1
        assert request_bodies[0]["input"] == [{"role": "user", "content": "wire@example.com"}]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"


@pytest.mark.parametrize("surface", ["create", "parse", "raw", "helper"])
async def test_real_async_openai_responses_preserves_single_pass_list_input(
    tmp_path,
    surface,
):
    request_bodies = []

    def responder(request):
        request_bodies.append(json.loads(request.content))
        return _provider_response(request)

    value = _SinglePassList()
    async with _real_async_openai_client(
        tmp_path,
        f"async-openai-responses-single-pass-list-{surface}",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        if surface in ("create", "parse"):
            await getattr(provider.responses, surface)(
                model="gpt-4o-mini",
                input=value,
            )
        elif surface == "raw":
            stream = await provider.responses.create(
                model="gpt-4o-mini",
                input=value,
                stream=True,
            )
            assert [event async for event in stream]
        else:
            async with provider.responses.stream(
                model="gpt-4o-mini",
                input=value,
            ) as stream:
                await stream.until_done()

        assert value.iterations == 1
        assert len(request_bodies) == 1
        assert request_bodies[0]["input"] == [{"role": "user", "content": "wire@example.com"}]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"


@pytest.mark.parametrize("surface", ["create", "helper"])
def test_real_openai_responses_sdk_local_error_emits_no_provider_trace(
    tmp_path,
    surface,
):
    attempts = 0

    def responder(request):
        nonlocal attempts
        attempts += 1
        return _provider_response(request)

    with _real_openai_client(
        tmp_path,
        f"openai-responses-local-error-{surface}",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        with pytest.raises(TypeError):
            if surface == "create":
                provider.responses.create(model="gpt-4o-mini", input=object())
            else:
                with provider.responses.stream(
                    model="gpt-4o-mini",
                    input=object(),
                ):
                    pass

        assert attempts == 0
        assert storage.list_traces() == []


@pytest.mark.parametrize("surface", ["create", "helper"])
async def test_real_async_openai_responses_sdk_local_error_emits_no_provider_trace(
    tmp_path,
    surface,
):
    attempts = 0

    def responder(request):
        nonlocal attempts
        attempts += 1
        return _provider_response(request)

    async with _real_async_openai_client(
        tmp_path,
        f"async-openai-responses-local-error-{surface}",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        with pytest.raises(TypeError):
            if surface == "create":
                await provider.responses.create(
                    model="gpt-4o-mini",
                    input=object(),
                )
            else:
                async with provider.responses.stream(
                    model="gpt-4o-mini",
                    input=object(),
                ):
                    pass

        assert attempts == 0
        assert storage.list_traces() == []


def test_real_openai_responses_parse_sdk_validation_emits_no_provider_trace(tmp_path):
    from pydantic import BaseModel

    class Answer(BaseModel):
        answer: str

    attempts = 0

    def responder(request):
        nonlocal attempts
        attempts += 1
        return _provider_response(request)

    with _real_openai_client(
        tmp_path,
        "openai-responses-parse-local-validation",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        with pytest.raises(TypeError, match="mix and match"):
            provider.responses.parse(
                model="gpt-4o-mini",
                input="hi",
                text_format=Answer,
                text={"format": {"type": "text"}},
            )

        assert attempts == 0
        assert storage.list_traces() == []


def test_real_openai_responses_captures_provider_consumed_draining_list(tmp_path):
    request_bodies = []

    def responder(request):
        request_bodies.append(json.loads(request.content))
        return _provider_response(request)

    value = _DrainingList()
    with _real_openai_client(
        tmp_path,
        "openai-responses-draining-list",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        provider.responses.create(model="gpt-4o-mini", input=value)

        assert value == []
        assert request_bodies[0]["input"] == [{"role": "user", "content": "drained@example.com"}]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"


async def test_real_async_openai_local_input_cancellation_emits_no_trace(tmp_path):
    attempts = 0

    def responder(request):
        nonlocal attempts
        attempts += 1
        return _provider_response(request)

    value = _CancellingList()
    async with _real_async_openai_client(
        tmp_path,
        "async-openai-local-input-cancellation",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        with pytest.raises(asyncio.CancelledError, match="local list traversal"):
            await provider.responses.create(model="gpt-4o-mini", input=value)

        assert value.iterations == 1
        assert attempts == 0
        assert storage.list_traces() == []


async def test_real_async_openai_capture_snapshots_input_at_wire_traversal(tmp_path):
    request_started = asyncio.Event()
    release_response = asyncio.Event()
    wire_bodies = []

    async def responder(request):
        wire_bodies.append(json.loads(request.content))
        request_started.set()
        await release_response.wait()
        return _provider_response(request)

    item = {"role": "user", "content": "wire-original@example.com"}
    value = [item]
    async with _real_async_openai_client(
        tmp_path,
        "async-openai-wire-snapshot",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        request = asyncio.create_task(provider.responses.create(model="gpt-4o-mini", input=value))
        await request_started.wait()
        item["content"] = "mutated-after-send@example.com"
        release_response.set()
        await request

        assert wire_bodies[0]["input"] == [{"role": "user", "content": "wire-original@example.com"}]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"
        assert "mutated-after-send" not in repr(trace.raw_messages)


def test_real_openai_responses_ignores_nested_chat_send_for_attempt_ownership(tmp_path):
    paths = []

    def responder(request):
        paths.append(request.url.path)
        return _provider_response(request)

    class NestedChatThenError(list):
        provider = None

        def __iter__(self):
            yield {"role": "user", "content": "outer-prefix@example.com"}
            self.provider.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "nested-chat@example.com"}],
            )
            raise RuntimeError("outer traversal failed locally")

    value = NestedChatThenError()
    with _real_openai_client(
        tmp_path,
        "openai-nested-chat-attempt-ownership",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        value.provider = provider
        with pytest.raises(RuntimeError, match="outer traversal failed locally"):
            provider.responses.create(model="gpt-4o-mini", input=value)

        assert paths == ["/v1/chat/completions"]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"


def test_real_openai_responses_nested_request_owns_its_exact_options(tmp_path):
    paths = []

    def responder(request):
        paths.append(request.url.path)
        return _provider_response(request)

    class NestedResponseThenError(list):
        provider = None

        def __iter__(self):
            yield {"role": "user", "content": "outer-prefix@example.com"}
            self.provider.responses.create(
                model="gpt-4o-mini",
                input="nested-response@example.com",
            )
            raise RuntimeError("outer traversal failed after nested response")

    value = NestedResponseThenError()
    with _real_openai_client(
        tmp_path,
        "openai-nested-responses-attempt-ownership",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        value.provider = provider
        with pytest.raises(RuntimeError, match="outer traversal failed after nested response"):
            provider.responses.create(model="gpt-4o-mini", input=value)

        assert paths == ["/v1/responses"]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"


def test_real_openai_responses_request_hook_error_emits_no_provider_trace(tmp_path):
    attempts = 0

    def responder(request):
        nonlocal attempts
        attempts += 1
        return _provider_response(request)

    def fail_before_transport(_request):
        raise RuntimeError("request hook failed locally")

    with _real_openai_client(
        tmp_path,
        "openai-request-hook-before-transport",
        responder=responder,
        request_hooks=(fail_before_transport,),
    ) as (openai, provider, storage):
        with pytest.raises(openai.APIConnectionError):
            provider.responses.create(model="gpt-4o-mini", input="hi")

        assert attempts == 0
        assert storage.list_traces() == []


def test_real_openai_responses_captures_shared_json_alias_twice(tmp_path):
    request_bodies = []

    def responder(request):
        request_bodies.append(json.loads(request.content))
        return _provider_response(request)

    shared = {"type": "input_text", "text": "shared@example.com"}
    value = [{"role": "user", "content": [shared, shared]}]
    with _real_openai_client(
        tmp_path,
        "openai-shared-json-alias",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        provider.responses.create(model="gpt-4o-mini", input=value)

        assert request_bodies[0]["input"][0]["content"] == [shared, shared]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>\n<EMAIL>"
        assert "<REPEATED>" not in repr(trace.raw_messages)


def test_real_openai_responses_captures_provider_serialized_mapping_subclass(tmp_path):
    class DynamicItemsDict(dict):
        def items(self):
            return {
                "role": "user",
                "content": "dynamic-wire@example.com",
            }.items()

    request_bodies = []

    def responder(request):
        request_bodies.append(json.loads(request.content))
        return _provider_response(request)

    value = [DynamicItemsDict(role="user", content="stored-snapshot@example.com")]
    with _real_openai_client(
        tmp_path,
        "openai-dynamic-mapping-wire",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        provider.responses.create(model="gpt-4o-mini", input=value)

        assert request_bodies[0]["input"] == [
            {"role": "user", "content": "dynamic-wire@example.com"}
        ]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"
        assert "stored-snapshot" not in repr(trace.raw_messages)


def test_real_openai_responses_capture_off_does_not_probe_extra_body(tmp_path):
    class HostileContainsDict(dict):
        def __contains__(self, _key):
            raise RuntimeError("telemetry contains probe")

    request_bodies = []
    retained_capture = []

    def responder(request):
        request_bodies.append(json.loads(request.content))
        attempt = openai_instrumentor._active_response_request.get()
        retained_capture.append(attempt.request_kwargs)
        return _provider_response(request)

    extra_body = HostileContainsDict(
        model="wire-model",
        input="wire-content@example.com",
    )
    with _real_openai_client(
        tmp_path,
        "openai-capture-off-extra-body",
        responder=responder,
        capture_content=False,
    ) as (_, provider, storage):
        provider.responses.create(
            model="explicit-model",
            input="explicit@example.com",
            extra_body=extra_body,
        )

        assert request_bodies[0]["model"] == "wire-model"
        assert retained_capture == [{"model": "wire-model"}]
        assert "wire-content" not in repr(retained_capture)
        [trace] = storage.list_traces()
        assert trace.request_model == "wire-model"
        assert trace.prompt_redacted is None


def test_real_openai_responses_uses_current_sdk_native_http_transport(tmp_path):
    openai = pytest.importorskip("openai")
    native_http = pytest.importorskip("httpx2")
    from verdict.instrumentors.openai import OpenAIInstrumentor

    request_bodies = []

    def responder(request):
        request_bodies.append(json.loads(request.content))
        return native_http.Response(200, json=_openai_response_payload())

    verdict_client, storage = _sqlite_client(tmp_path, "openai-native-http")
    verdict_client.capture_content = True
    instrumentor = OpenAIInstrumentor(verdict_client)
    http_client = native_http.Client(transport=native_http.MockTransport(responder))
    instrumentor.install()
    try:
        provider = openai.OpenAI(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=0,
            http_client=http_client,
        )
        provider.responses.create(
            model="gpt-4o-mini",
            input="native@example.com",
        )

        assert request_bodies[0]["input"] == "native@example.com"
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"
    finally:
        instrumentor.uninstall()
        http_client.close()
        storage.close()


def test_real_openai_responses_captures_userdict_extra_body_from_wire(tmp_path):
    request_bodies = []

    def responder(request):
        request_bodies.append(json.loads(request.content))
        return _provider_response(request)

    with _real_openai_client(
        tmp_path,
        "openai-userdict-extra-body",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        provider.responses.create(
            model="explicit-model",
            input="explicit@example.com",
            extra_body=UserDict(
                model="wire-model",
                input="wire-content@example.com",
            ),
        )

        assert request_bodies[0]["model"] == "wire-model"
        [trace] = storage.list_traces()
        assert trace.request_model == "wire-model"
        assert trace.prompt_redacted == "<EMAIL>"
        assert "explicit" not in repr(trace.raw_messages)


def test_real_openai_responses_captures_effective_extra_body_overrides(tmp_path):
    request_bodies = []

    def responder(request):
        request_bodies.append(json.loads(request.content))
        return _provider_response(request)

    with _real_openai_client(
        tmp_path,
        "openai-responses-extra-body",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        provider.responses.create(
            model="explicit-model",
            input="explicit-input@example.com",
            instructions="explicit-instructions@example.com",
            max_output_tokens=1,
            temperature=0.1,
            extra_body={
                "model": "wire-model",
                "input": "wire-override@example.com",
                "instructions": "wire-instructions@example.com",
                "max_output_tokens": 17,
                "temperature": 0.7,
            },
        )

        assert request_bodies[0]["model"] == "wire-model"
        assert request_bodies[0]["input"] == "wire-override@example.com"
        assert request_bodies[0]["instructions"] == "wire-instructions@example.com"
        [trace] = storage.list_traces()
        assert trace.request_model == "wire-model"
        assert trace.max_tokens == 17
        assert trace.temperature == 0.7
        assert trace.prompt_redacted == "<EMAIL>\n<EMAIL>"
        assert "explicit-input" not in repr(trace.raw_messages)
        assert "explicit-instructions" not in repr(trace.raw_messages)


def test_real_openai_responses_preserves_extra_body_single_pass_list(tmp_path):
    request_bodies = []

    def responder(request):
        request_bodies.append(json.loads(request.content))
        return _provider_response(request)

    value = _SinglePassList("extra-body@example.com")
    with _real_openai_client(
        tmp_path,
        "openai-responses-extra-body-list",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        provider.responses.create(
            model="gpt-4o-mini",
            input="overridden@example.com",
            extra_body={"input": value},
        )

        assert value.iterations == 1
        assert request_bodies[0]["input"] == [{"role": "user", "content": "extra-body@example.com"}]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"
        assert "overridden" not in repr(trace.raw_messages)


@pytest.mark.parametrize("surface", ["responses", "chat"])
def test_real_openai_stream_manager_nested_reentry_persists_both_traces(
    tmp_path,
    surface,
):
    attempts = 0

    def responder(request):
        nonlocal attempts
        attempts += 1
        return _provider_response(request)

    with _real_openai_client(
        tmp_path,
        f"openai-nested-manager-{surface}",
        responder=responder,
    ) as (_, provider, storage):
        if surface == "responses":
            manager = provider.responses.stream(model="gpt-4o-mini", input="hi")
        else:
            manager = provider.beta.chat.completions.stream(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hi"}],
            )

        with manager as first:
            next(first)
            with manager as second:
                next(second)

        assert attempts == 2
        traces = storage.list_traces()
        assert len(traces) == 2
        assert len({trace.trace_id for trace in traces}) == 2
        if surface == "responses":
            assert {trace.tags["verdict.stream_completion"] for trace in traces} == {"partial"}


def test_real_openai_responses_provider_error_is_persisted_when_sampled_out(tmp_path):
    def responder(_request):
        return httpx.Response(
            500,
            json={"error": {"message": "provider failed for error@example.com"}},
        )

    with _real_openai_client(
        tmp_path,
        "openai-responses-provider-error",
        responder=responder,
        sample_rate=0.0,
    ) as (openai, provider, storage):
        with pytest.raises(openai.InternalServerError):
            provider.responses.create(model="gpt-4o-mini", input="hi")

        [trace] = storage.list_traces()
        assert "error@example.com" not in (trace.error or "")
        assert "<EMAIL>" in (trace.error or "")


def test_real_openai_responses_iteration_error_persists_partial_content_as_error(
    tmp_path,
):
    class BreakingResponseBody(httpx.SyncByteStream):
        def __iter__(self):
            yield _openai_responses_sse(include_terminal=False).replace(
                b"data: [DONE]\n\n",
                b"",
            )
            raise RuntimeError("wire failed for stream@example.com")

    def responder(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BreakingResponseBody(),
        )

    with _real_openai_client(
        tmp_path,
        "openai-responses-iteration-error",
        responder=responder,
        capture_content=True,
        sample_rate=0.0,
    ) as (_, provider, storage):
        stream = provider.responses.create(
            model="gpt-4o-mini",
            input="hi",
            stream=True,
        )
        with pytest.raises(RuntimeError, match=r"stream@example\.com"):
            list(stream)

        [trace] = storage.list_traces()
        assert trace.tags["verdict.stream_completion"] == "error"
        assert trace.error == "RuntimeError: wire failed for <EMAIL>"
        assert trace.response_redacted == "OK <EMAIL>"


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("output_text", "done-only@example.com"),
        ("refusal", "refused refusal@example.com"),
    ],
)
def test_real_openai_partial_response_captures_done_only_content(
    tmp_path,
    kind,
    text,
):
    def responder(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_openai_responses_done_only_sse(kind=kind, text=text),
        )

    with _real_openai_client(
        tmp_path,
        f"openai-done-only-{kind}",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        stream = provider.responses.create(
            model="gpt-4o-mini",
            input="hi",
            stream=True,
        )
        events = list(stream)

        assert getattr(events[-1], "text" if kind == "output_text" else "refusal") == text
        [trace] = storage.list_traces()
        assert trace.tags["verdict.stream_completion"] == "partial"
        assert trace.response_redacted == text.replace(
            "done-only@example.com" if kind == "output_text" else "refusal@example.com",
            "<EMAIL>",
        )


@pytest.mark.parametrize("kind", ["output_text", "refusal"])
async def test_real_async_openai_partial_response_captures_done_only_content(
    tmp_path,
    kind,
):
    text = f"{kind} async@example.com"

    def responder(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_openai_responses_done_only_sse(kind=kind, text=text),
        )

    async with _real_async_openai_client(
        tmp_path,
        f"async-openai-done-only-{kind}",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        stream = await provider.responses.create(
            model="gpt-4o-mini",
            input="hi",
            stream=True,
        )
        assert [event async for event in stream]

        [trace] = storage.list_traces()
        assert trace.tags["verdict.stream_completion"] == "partial"
        assert trace.response_redacted == f"{kind} <EMAIL>"


def test_real_openai_partial_response_deduplicates_delta_and_done_content(tmp_path):
    text = "one-copy@example.com"

    def responder(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_openai_responses_done_only_sse(
                kind="output_text",
                text=text,
                include_delta=True,
            ),
        )

    with _real_openai_client(
        tmp_path,
        "openai-delta-done-dedup",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        stream = provider.responses.create(
            model="gpt-4o-mini",
            input="hi",
            stream=True,
        )
        list(stream)

        [trace] = storage.list_traces()
        assert trace.response_redacted == "<EMAIL>"


def test_real_openai_partial_response_done_replaces_observed_suffix(tmp_path):
    suffix = "suffix@example.com"
    complete = f"prefix@example.com {suffix}"
    events = [
        json.loads(line.removeprefix("data: "))
        for line in _openai_responses_done_only_sse(
            kind="output_text",
            text=complete,
            include_delta=True,
        )
        .decode()
        .splitlines()
        if line.startswith("data: {")
    ]
    delta = next(event for event in events if event["type"] == "response.output_text.delta")
    delta["delta"] = suffix
    content = (
        "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    ).encode()

    def responder(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=content,
        )

    with _real_openai_client(
        tmp_path,
        "openai-resumed-done-value",
        responder=responder,
        capture_content=True,
    ) as (_, provider, storage):
        stream = provider.responses.create(
            model="gpt-4o-mini",
            input="hi",
            stream=True,
        )
        list(stream)

        [trace] = storage.list_traces()
        assert trace.response_redacted == "<EMAIL> <EMAIL>"


@pytest.mark.parametrize(
    "surface",
    [
        "responses-helper",
        "responses-raw",
        "responses-create",
        "chat-helper",
        "chat-raw",
        "chat-create",
    ],
)
async def test_real_async_openai_request_cancellation_persists_exactly_one_trace(
    tmp_path,
    surface,
):
    openai = pytest.importorskip("openai")
    from verdict.instrumentors.openai import OpenAIInstrumentor

    request_started = asyncio.Event()

    async def pending_response(_request):
        request_started.set()
        await asyncio.Event().wait()

    verdict_client, storage = _sqlite_client(tmp_path, f"openai-cancel-{surface}")
    instrumentor = OpenAIInstrumentor(verdict_client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(pending_response))
    instrumentor.install()
    try:
        provider = openai.AsyncOpenAI(
            api_key="test",
            base_url="http://provider.test/v1",
            max_retries=0,
            http_client=http_client,
        )

        async def make_request():
            if surface == "responses-helper":
                async with provider.responses.stream(
                    model="gpt-4o-mini",
                    input="hi",
                ):
                    pass
            elif surface == "responses-raw":
                await provider.responses.create(
                    model="gpt-4o-mini",
                    input="hi",
                    stream=True,
                )
            elif surface == "responses-create":
                await provider.responses.create(model="gpt-4o-mini", input="hi")
            elif surface == "chat-helper":
                async with provider.chat.completions.stream(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "hi"}],
                ):
                    pass
            else:
                await provider.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "hi"}],
                    stream=surface == "chat-raw",
                )

        task = asyncio.create_task(make_request())
        await request_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        [trace] = storage.list_traces()
        assert trace.error is not None
        assert trace.error.startswith("CancelledError:")
        expected_completion = (
            "error" if surface.startswith("responses-") and surface != "responses-create" else None
        )
        assert trace.tags.get("verdict.stream_completion") == expected_completion
    finally:
        instrumentor.uninstall()
        await http_client.aclose()
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
    http_client = anthropic.DefaultHttpxClient(transport=_anthropic_mock_transport(anthropic))
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
    http_client = anthropic.DefaultAsyncHttpxClient(transport=_anthropic_mock_transport(anthropic))
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
            messages = ({"role": "user", "content": "one-shot@example.com"} for _ in range(1))
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

        assert wire_bodies[0]["messages"] == [{"role": "user", "content": "one-shot@example.com"}]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.raw_messages == [{"role": "user", "content": "<EMAIL>"}]
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
            messages = ({"role": "user", "content": "one-shot@example.com"} for _ in range(1))
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

        assert wire_bodies[0]["messages"] == [{"role": "user", "content": "one-shot@example.com"}]
        [trace] = storage.list_traces()
        assert trace.prompt_redacted == "<EMAIL>"
        assert trace.raw_messages == [{"role": "user", "content": "<EMAIL>"}]
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
        assert {(trace.session_id, trace.tags["verdict.workload"]) for trace in traces} == {
            ("one", "workload-one"),
            ("two", "workload-two"),
        }


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
        assert {(trace.session_id, trace.tags["verdict.workload"]) for trace in traces} == {
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
    with _real_anthropic_client(tmp_path, "anthropic-raw-stream") as (_, provider, storage):
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
    async with _real_async_anthropic_client(tmp_path, "async-anthropic-raw-stream") as (
        _,
        provider,
        storage,
    ):
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
    with _real_anthropic_client(tmp_path, f"anthropic-partial-{action}") as (_, provider, storage):
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
    async with _real_async_anthropic_client(tmp_path, f"async-anthropic-partial-{action}") as (
        _,
        provider,
        storage,
    ):
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
    with _real_anthropic_client(tmp_path, "anthropic-stream-block-error") as (_, provider, storage):
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

    records = [record for record in caplog.records if record.name == "verdict.instrumentors"]
    assert errors == []
    assert len(records) == 1
    assert "ForcedPersistenceError" in records[0].getMessage()
    assert "openai" in records[0].getMessage()
    assert "do-not-log-this-secret@example.com" not in records[0].getMessage()
    assert "2001:db8::1" not in records[0].getMessage()
