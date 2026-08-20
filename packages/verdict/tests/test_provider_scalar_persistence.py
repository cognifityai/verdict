"""Real-SDK regressions for provider scalar values reaching durable storage.

The provider clients are real installed SDKs.  Only their HTTP transport is
replaced, so request construction (including SDK-owned unset sentinels),
response parsing, Verdict wrapping, and SQLite persistence all execute.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import math
import threading
from types import SimpleNamespace

import httpx
import pytest
from verdict.client import VerdictClient
from verdict.instrumentors.base import persist_trace
from verdict.schema import Trace
from verdict.storage.sqlite import SQLiteStorage


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


def _anthropic_mock_transport(anthropic_module):
    client_base = anthropic_module.DefaultHttpxClient.__mro__[1]
    transport_module = importlib.import_module(client_base.__module__.split(".", 1)[0])
    def respond(request):
        response = _provider_response(request)
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
            raise ForcedPersistenceError("do-not-log-this-secret@example.com")

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
