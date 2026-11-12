"""Capture records how many client tool calls a provider reply contained."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import verdict
from test_provider_scalar_persistence import (
    _openai_response_payload,
    _provider_response,
    _real_anthropic_client,
    _real_openai_client,
)
from verdict.schema import Trace
from verdict.trace_facts import RESPONSE_TOOL_CALLS_TAG

_CALL_ARGUMENTS = '{"order_id": "A-100"}'


def _sse(events: list[dict[str, object]], *, named: bool = False) -> httpx.Response:
    body = "".join(
        (f"event: {event['type']}\n" if named else "") + f"data: {json.dumps(event)}\n\n"
        for event in events
    )
    if not named:
        body += "data: [DONE]\n\n"
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=body.encode(),
    )


def _chat_tool_calls(request: httpx.Request) -> httpx.Response:
    if not request.url.path.endswith("/chat/completions"):
        return _provider_response(request)
    calls = [
        {"id": f"call_{index}", "type": "function",
         "function": {"name": "lookup_order", "arguments": _CALL_ARGUMENTS}}
        for index in range(2)
    ]
    if not json.loads(request.content).get("stream"):
        return httpx.Response(200, json={
            "id": "chatcmpl-tools", "object": "chat.completion", "created": 1,
            "model": "gpt-4o-mini",
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None,
                                     "tool_calls": calls}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        })
    chunk = {"id": "chatcmpl-tools", "object": "chat.completion.chunk", "created": 1,
             "model": "gpt-4o-mini"}
    events = [{**chunk, "choices": [{"index": 0, "finish_reason": None, "delta": {
        "role": "assistant", "content": None,
        "tool_calls": [{"index": index, "id": call["id"], "type": "function",
                        "function": {"name": "lookup_order", "arguments": ""}}],
    }}]} for index, call in enumerate(calls)]
    # Argument fragments arrive as further deltas on the same call index.
    events += [{**chunk, "choices": [{"index": 0, "finish_reason": None, "delta": {
        "tool_calls": [{"index": index, "function": {"arguments": _CALL_ARGUMENTS}}],
    }}]} for index in range(2)]
    events.append({**chunk, "choices": [{"index": 0, "delta": {},
                                         "finish_reason": "tool_calls"}]})
    return _sse(events)


def _responses_function_call(request: httpx.Request) -> httpx.Response:
    if not request.url.path.endswith("/responses"):
        return _provider_response(request)
    response = _openai_response_payload(text="")
    response["output"] = [{
        "id": "fc_1", "type": "function_call", "status": "completed",
        "call_id": "call_1", "name": "lookup_order", "arguments": _CALL_ARGUMENTS,
    }]
    if not json.loads(request.content).get("stream"):
        return httpx.Response(200, json=response)
    created = {**response, "status": "in_progress", "output": [], "usage": None}
    return _sse([
        {"type": "response.created", "response": created, "sequence_number": 0},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**response["output"][0], "status": "in_progress", "arguments": ""},
         "sequence_number": 1},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1",
         "output_index": 0, "delta": _CALL_ARGUMENTS, "sequence_number": 2},
        {"type": "response.completed", "response": response, "sequence_number": 3},
    ])


def _anthropic_tool_use(request: httpx.Request) -> httpx.Response:
    if not request.url.path.endswith("/messages"):
        return _provider_response(request)
    message = {
        "id": "msg_tools", "type": "message", "role": "assistant",
        "model": "claude-haiku-4-5", "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 7},
    }
    block = {"type": "tool_use", "id": "toolu_1", "name": "lookup_order",
             "input": json.loads(_CALL_ARGUMENTS)}
    if not json.loads(request.content).get("stream"):
        return httpx.Response(200, json={**message, "content": [block]})
    return _sse([
        {"type": "message_start", "message": {**message, "content": [],
                                              "stop_reason": None,
                                              "usage": {"input_tokens": 5,
                                                        "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {**block, "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": _CALL_ARGUMENTS}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use",
                                            "stop_sequence": None},
         "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    ], named=True)


def _assert_tool_call_reply(storage, expected_calls: int) -> Trace:
    [trace] = storage.list_traces()
    assert trace.tags[RESPONSE_TOOL_CALLS_TAG] == str(expected_calls)
    assert trace.response_redacted in ("", None)
    return trace


@pytest.mark.parametrize("stream", [False, True])
def test_openai_chat_tool_calls_are_not_blank(tmp_path, stream) -> None:
    with _real_openai_client(
        tmp_path, f"chat-tools-{stream}", responder=_chat_tool_calls, capture_content=True,
    ) as (_, provider, storage):
        response = provider.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "find A-100"}],
            stream=stream,
        )
        if stream:
            assert len(list(response)) == 5
        _assert_tool_call_reply(storage, 2)


@pytest.mark.parametrize("stream", [False, True])
def test_openai_responses_function_calls_are_not_blank(tmp_path, stream) -> None:
    with _real_openai_client(
        tmp_path, f"responses-tools-{stream}", responder=_responses_function_call,
        capture_content=True,
    ) as (_, provider, storage):
        response = provider.responses.create(
            model="gpt-4o-mini", input="find A-100", stream=stream,
        )
        if stream:
            assert [event.type for event in response][-1] == "response.completed"
        trace = _assert_tool_call_reply(storage, 1)
        assert trace.finish_reason == "completed"


@pytest.mark.parametrize("stream", [False, True])
def test_anthropic_tool_use_is_not_blank(tmp_path, stream) -> None:
    with _real_anthropic_client(
        tmp_path, f"anthropic-tools-{stream}", responder=_anthropic_tool_use,
        capture_content=True,
    ) as (_, provider, storage):
        response = provider.messages.create(
            model="claude-haiku-4-5", max_tokens=64, stream=stream,
            messages=[{"role": "user", "content": "find A-100"}],
        )
        if stream:
            assert list(response)
        _assert_tool_call_reply(storage, 1)


def test_tool_call_count_is_kept_without_content_capture(tmp_path) -> None:
    with _real_openai_client(
        tmp_path, "chat-tools-metadata", responder=_chat_tool_calls, capture_content=False,
    ) as (_, provider, storage):
        provider.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "find A-100"}],
        )
        [trace] = storage.list_traces()
        assert trace.tags[RESPONSE_TOOL_CALLS_TAG] == "2"
        assert trace.response_redacted is None


def test_text_reply_records_zero_tool_calls(tmp_path) -> None:
    with _real_openai_client(tmp_path, "chat-text", capture_content=True) as (
        _, provider, storage,
    ):
        provider.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
        )
        [trace] = storage.list_traces()
        assert trace.tags[RESPONSE_TOOL_CALLS_TAG] == "0"


def _google_response(types, parts: list[object]):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=parts), finish_reason="STOP",
        )],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=5, candidates_token_count=7,
        ),
    )


def _google_parts(types) -> list[object]:
    return [
        types.Part(thought=True, text="private reasoning"),
        types.Part(function_call=types.FunctionCall(
            name="lookup_order", args=json.loads(_CALL_ARGUMENTS),
        )),
    ]


def _google_instrumentor(capture_content: bool = True):
    from verdict.client import VerdictClient
    from verdict.instrumentors.google import GoogleInstrumentor
    from verdict.storage.memory import InMemoryStorage

    storage = InMemoryStorage()
    client = VerdictClient(storage=storage)
    client.capture_content = capture_content
    return GoogleInstrumentor(client), storage


def test_google_function_call_reply_is_not_rendered_as_text() -> None:
    types = pytest.importorskip("google.genai.types")
    instrumentor, _storage = _google_instrumentor()
    trace = Trace(provider="google", request_model="gemini-2.5-flash")

    instrumentor._fill_output(trace, _google_response(types, _google_parts(types)))

    assert trace.tags[RESPONSE_TOOL_CALLS_TAG] == "1"
    # Neither the thought nor the call's object representation is answer text.
    assert trace.response_redacted == ""
    assert "A-100" not in repr(trace.response_redacted)


def test_google_stream_counts_function_calls() -> None:
    types = pytest.importorskip("google.genai.types")
    from verdict.instrumentors.google import _StreamingWrapper

    instrumentor, storage = _google_instrumentor()
    trace = Trace(provider="google", request_model="gemini-2.5-flash",
                  started_at=datetime.now(timezone.utc))
    chunks = [_google_response(types, [part]) for part in _google_parts(types)]
    wrapper = _StreamingWrapper(iter(chunks), trace, 0.0, instrumentor)

    assert len(list(wrapper)) == 2
    [stored] = storage.list_traces()
    assert stored.tags[RESPONSE_TOOL_CALLS_TAG] == "1"
    assert not stored.response_redacted


def test_model_call_context_keeps_the_tool_call_tag(tmp_path) -> None:
    with _real_openai_client(
        tmp_path, "chat-tools-context", responder=_chat_tool_calls, capture_content=True,
    ) as (_, provider, storage):
        with verdict.model_call_context() as correlation_id:
            provider.chat.completions.create(
                model="gpt-4o-mini", messages=[{"role": "user", "content": "find A-100"}],
            )
        [trace] = storage.list_traces()
        assert trace.trace_id == correlation_id
        assert trace.tags[RESPONSE_TOOL_CALLS_TAG] == "2"


def _responses_hosted_call_only(request: httpx.Request) -> httpx.Response:
    if not request.url.path.endswith("/responses"):
        return _provider_response(request)
    response = _openai_response_payload(text="")
    response["output"] = [{
        "id": "ws_1", "type": "web_search_call", "status": "completed",
        "action": {"type": "search", "query": "order A-100"},
    }]
    return httpx.Response(200, json=response)


def test_hosted_tool_calls_do_not_replace_an_answer(tmp_path) -> None:
    """A provider-run search with no message is still a blank answer."""
    with _real_openai_client(
        tmp_path, "responses-hosted", responder=_responses_hosted_call_only,
        capture_content=True,
    ) as (_, provider, storage):
        provider.responses.create(model="gpt-4o-mini", input="find A-100")
        [trace] = storage.list_traces()
        assert trace.tags[RESPONSE_TOOL_CALLS_TAG] == "0"
        assert trace.response_redacted == ""
