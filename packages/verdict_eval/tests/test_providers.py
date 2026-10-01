"""Provider adapter tests that exercise request and response translation."""

from __future__ import annotations

import contextlib
import json
import sys
import time
from types import SimpleNamespace

import pytest


class _ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | None = None,
        response: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.response = response


def _fake_google_types():
    class Part:
        def __init__(self, *, text):
            self.text = text

    class Content:
        def __init__(self, *, role, parts):
            self.role = role
            self.parts = parts

    class GenerateContentConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    return SimpleNamespace(
        Part=Part,
        Content=Content,
        GenerateContentConfig=GenerateContentConfig,
    )


def test_google_adapter_translates_messages_config_and_usage(monkeypatch) -> None:
    from verdict_eval.providers import CompletionRequest, GoogleAdapter, output_token_ceiling

    monkeypatch.setitem(
        sys.modules,
        "google.genai",
        SimpleNamespace(types=_fake_google_types()),
    )
    captured = {}

    def generate_content(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            text="translated",
            usage_metadata=SimpleNamespace(
                prompt_token_count=7,
                candidates_token_count=3,
            ),
            candidates=[SimpleNamespace(finish_reason="STOP")],
        )

    adapter = object.__new__(GoogleAdapter)
    adapter._client = SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content)
    )
    response = adapter._complete_once(CompletionRequest(
        model="gemini-test",
        messages=[
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ],
        temperature=0.25,
        max_tokens=77,
    ))

    assert captured["model"] == "gemini-test"
    assert [content.role for content in captured["contents"]] == ["user", "model"]
    assert [content.parts[0].text for content in captured["contents"]] == [
        "question", "answer",
    ]
    assert captured["config"].system_instruction == "policy"
    assert captured["config"].temperature == 0.25
    assert captured["config"].max_output_tokens == output_token_ceiling(77)
    assert response.text == "translated"
    assert response.input_tokens == 7
    assert response.output_tokens == 3
    assert response.finish_reason == "STOP"


def test_completion_request_constructs() -> None:
    from verdict_eval.providers import CompletionRequest

    req = CompletionRequest(
        model="gemini/gemini-2.5-flash",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert req.model == "gemini/gemini-2.5-flash"
    assert req.temperature == 0.0
    assert req.max_tokens == 1024


def _fake_anthropic_stream(captured: dict, *, stop_reason: str = "end_turn", content=None):
    """A ``messages.stream`` stand-in: records the request, yields a final message."""

    @contextlib.contextmanager
    def stream(**kwargs):
        captured.update(kwargs)
        yield SimpleNamespace(get_final_message=lambda: SimpleNamespace(
            content=content if content is not None else [SimpleNamespace(text="ok")],
            usage=SimpleNamespace(input_tokens=2, output_tokens=1),
            stop_reason=stop_reason,
        ))

    return SimpleNamespace(messages=SimpleNamespace(stream=stream))


def test_anthropic_adapter_omits_unsupported_temperature() -> None:
    from verdict_eval.providers import AnthropicAdapter, CompletionRequest, output_token_ceiling

    captured = {}
    adapter = object.__new__(AnthropicAdapter)
    adapter.supports_temperature = False
    adapter._client = _fake_anthropic_stream(captured)

    response = adapter._complete_once(CompletionRequest(
        model="claude-test", messages=[{"role": "user", "content": "q"}], temperature=0.0,
    ))

    assert "temperature" not in captured
    assert captured["model"] == "claude-test"
    assert captured["max_tokens"] == output_token_ceiling(1024, "claude-test") == 1024
    assert response.text == "ok"
    assert response.finish_reason == "end_turn"


def test_anthropic_adapter_sends_temperature_when_the_sdk_accepts_it() -> None:
    from verdict_eval.providers import AnthropicAdapter, CompletionRequest

    captured = {}
    adapter = object.__new__(AnthropicAdapter)
    adapter.supports_temperature = True
    adapter._client = _fake_anthropic_stream(captured)

    adapter._complete_once(CompletionRequest(
        model="claude-test", messages=[{"role": "system", "content": "policy"},
                                       {"role": "user", "content": "q"}], temperature=0.25,
    ))

    assert captured["temperature"] == 0.25
    assert captured["system"] == "policy"
    assert captured["messages"] == [{"role": "user", "content": "q"}]


def test_anthropic_adapter_reads_only_text_blocks_and_reports_the_stop_reason() -> None:
    from verdict_eval.judge_output import is_truncated
    from verdict_eval.providers import AnthropicAdapter, CompletionRequest

    adapter = object.__new__(AnthropicAdapter)
    adapter.supports_temperature = False
    adapter._client = _fake_anthropic_stream({}, stop_reason="max_tokens", content=[
        SimpleNamespace(type="thinking", thinking="long deliberation"),
        SimpleNamespace(type="text", text="{\"relevance\": "),
    ])

    response = adapter._complete_once(CompletionRequest(model="m", messages=[]))

    assert response.text == "{\"relevance\": "
    assert response.finish_reason == "max_tokens"
    assert is_truncated(response.finish_reason)


def _sse(events: list[dict]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode("utf-8")


def _anthropic_stream_body(text_chunks: list[str], *, stop_reason: str, thinking: bool) -> bytes:
    events = [{"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 25, "output_tokens": 1},
    }}]
    index = 0
    if thinking:
        events += [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "weighing the evidence"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "sig"}},
            {"type": "content_block_stop", "index": 0},
        ]
        index = 1
    if text_chunks:
        events.append({"type": "content_block_start", "index": index,
                       "content_block": {"type": "text", "text": ""}})
        events += [{"type": "content_block_delta", "index": index,
                    "delta": {"type": "text_delta", "text": chunk}} for chunk in text_chunks]
        events.append({"type": "content_block_stop", "index": index})
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": 40}},
        {"type": "message_stop"},
    ]
    return _sse(events)


def _anthropic_with_transport(respond):
    anthropic = pytest.importorskip("anthropic")
    from verdict_eval.providers import AnthropicAdapter

    sdk_httpx = getattr(anthropic._base_client, "httpx2", None) or anthropic._base_client.httpx
    client = sdk_httpx.Client(transport=sdk_httpx.MockTransport(respond))
    adapter = AnthropicAdapter(api_key="local-test", max_retries=1)
    adapter._client.close()
    adapter._client = anthropic.Anthropic(api_key="local-test", http_client=client)
    return adapter, sdk_httpx


def test_anthropic_adapter_streams_the_installed_sdk_with_the_reasoning_allowance() -> None:
    """Real SDK serialization and stream parsing; only the HTTP transport is faked."""
    from verdict_eval.providers import CompletionRequest, output_token_ceiling

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return sdk_httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=_anthropic_stream_body(
                                      ['{"relevance": ', '{"reasoning": "ok", "verdict": "PASS"}}'],
                                      stop_reason="end_turn", thinking=True))

    adapter, sdk_httpx = _anthropic_with_transport(respond)
    response = adapter.complete(CompletionRequest(
        model="claude-opus-5-5", max_tokens=512,
        messages=[{"role": "system", "content": "policy"}, {"role": "user", "content": "q"}],
    ))

    [body] = requests
    assert body["max_tokens"] == output_token_ceiling(512, "claude-opus-5-5") == 512 + 16_384
    assert body["stream"] is True
    assert body["system"] == "policy"
    assert body["messages"] == [{"role": "user", "content": "q"}]
    assert ("temperature" in body) is adapter.supports_temperature
    assert response.text == '{"relevance": {"reasoning": "ok", "verdict": "PASS"}}'
    assert response.finish_reason == "end_turn"
    assert response.input_tokens == 25
    assert response.output_tokens == 40


def test_anthropic_adapter_reports_a_reply_spent_entirely_on_thinking() -> None:
    from verdict_eval.judge_output import is_truncated
    from verdict_eval.providers import CompletionRequest

    def respond(request):
        return sdk_httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=_anthropic_stream_body(
                                      [], stop_reason="max_tokens", thinking=True))

    adapter, sdk_httpx = _anthropic_with_transport(respond)
    response = adapter.complete(CompletionRequest(model="claude-test", messages=[
        {"role": "user", "content": "q"},
    ]))

    assert response.text == ""
    assert response.finish_reason == "max_tokens"
    assert is_truncated(response.finish_reason)


def _openai_with_transport(respond):
    openai = pytest.importorskip("openai")
    from verdict_eval.providers import OpenAIAdapter

    sdk_httpx = getattr(openai._base_client, "httpx2", None) or openai._base_client.httpx
    client = sdk_httpx.Client(transport=sdk_httpx.MockTransport(respond))
    adapter = OpenAIAdapter(api_key="local-test", max_retries=1)
    adapter._client.close()
    adapter._client = openai.OpenAI(api_key="local-test", http_client=client)
    return adapter, sdk_httpx


def _chat_completion(content: str, finish_reason: str) -> dict:
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "gpt-test",
        "choices": [{"index": 0, "finish_reason": finish_reason,
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
    }


def test_openai_adapter_sends_max_completion_tokens_and_never_max_tokens() -> None:
    from verdict_eval.providers import CompletionRequest, output_token_ceiling

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return sdk_httpx.Response(200, json=_chat_completion('{"relevance": {}}', "stop"))

    adapter, sdk_httpx = _openai_with_transport(respond)
    response = adapter.complete(CompletionRequest(
        model="gpt-test", max_tokens=256, temperature=0.0,
        messages=[{"role": "system", "content": "policy"}, {"role": "user", "content": "q"}],
    ))

    [body] = requests
    assert body["max_completion_tokens"] == output_token_ceiling(256, "gpt-test") == 256
    assert "max_tokens" not in body
    assert body["temperature"] == 0.0
    assert body["messages"][0] == {"role": "system", "content": "policy"}
    assert response.text == '{"relevance": {}}'
    assert response.finish_reason == "stop"
    assert response.input_tokens == 9
    assert response.output_tokens == 4


def test_openai_adapter_reports_a_reply_cut_off_by_reasoning() -> None:
    from verdict_eval.judge_output import is_truncated
    from verdict_eval.providers import CompletionRequest

    def respond(request):
        return sdk_httpx.Response(200, json=_chat_completion("", "length"))

    adapter, sdk_httpx = _openai_with_transport(respond)
    response = adapter.complete(CompletionRequest(model="gpt-test", messages=[
        {"role": "user", "content": "q"},
    ]))

    assert response.text == ""
    assert is_truncated(response.finish_reason)


def test_litellm_adapter_sends_the_reasoning_allowance(monkeypatch) -> None:
    from verdict_eval.providers import CompletionRequest, LiteLLMAdapter, output_token_ceiling

    captured = {}

    def completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))

    LiteLLMAdapter(max_retries=1).complete(CompletionRequest(
        model="custom/model", messages=[{"role": "user", "content": "hi"}], max_tokens=300,
    ))

    assert captured["max_tokens"] == output_token_ceiling(300, "custom/model") == 300


@pytest.mark.parametrize("model", [
    "gpt-5-mini", "gpt-5.4", "o3-mini", "o4-mini", "claude-opus-5-5", "claude-sonnet-5-5",
    "claude-fable-5-1", "anthropic/claude-opus-5", "gemini-2.5-flash", "gemini-3-pro",
])
def test_output_token_ceiling_adds_the_allowance_for_models_that_reason(model) -> None:
    from verdict_eval.providers import REASONING_TOKEN_ALLOWANCE, output_token_ceiling

    assert output_token_ceiling(1, model) == 1 + REASONING_TOKEN_ALLOWANCE
    assert output_token_ceiling(32_768, model) == 32_768 + REASONING_TOKEN_ALLOWANCE


@pytest.mark.parametrize("model", [
    "gpt-4o-mini", "gpt-4o", "gpt-4.1", "claude-haiku-4-5", "claude-3-5-haiku-20241022",
    "claude-sonnet-4-5", "gemini-2.0-flash", "custom/model", "", None,
])
def test_output_token_ceiling_keeps_the_budget_for_other_models(model) -> None:
    from verdict_eval.providers import output_token_ceiling

    # gpt-4o-mini accepts at most 16,384 output tokens; the budget alone fits.
    assert output_token_ceiling(1_024, model) == 1_024
    assert output_token_ceiling(16_384, model) == 16_384


@pytest.mark.parametrize("budget", [0, -1, True, 2.5, "1024", None])
def test_output_token_ceiling_rejects_non_budgets(budget) -> None:
    from verdict_eval.providers import output_token_ceiling

    with pytest.raises(ValueError):
        output_token_ceiling(budget, "gpt-5-mini")


def test_google_adapter_handles_empty_optional_response_fields(monkeypatch) -> None:
    from verdict_eval.providers import CompletionRequest, GoogleAdapter

    monkeypatch.setitem(
        sys.modules,
        "google.genai",
        SimpleNamespace(types=_fake_google_types()),
    )
    adapter = object.__new__(GoogleAdapter)
    adapter._client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **_kwargs: SimpleNamespace(
            text=None,
            usage_metadata=None,
            candidates=[],
        )
    ))

    response = adapter._complete_once(CompletionRequest(
        model="gemini-test",
        messages=[],
    ))

    assert response.text == ""
    assert response.input_tokens is None
    assert response.output_tokens is None
    assert response.finish_reason == ""


def test_litellm_adapter_retries_transient_errors(monkeypatch) -> None:
    from verdict_eval.providers import CompletionRequest, LiteLLMAdapter

    attempts = []

    def completion(**_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise _ProviderError(
                "429 Too Many Requests. Limit 40000 tokens; request req-404abc",
                status_code=429,
            )
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    response = LiteLLMAdapter(max_retries=2).complete(CompletionRequest(
        model="custom/model",
        messages=[{"role": "user", "content": "hi"}],
    ))

    assert len(attempts) == 2
    assert response.text == "ok"


def test_litellm_adapter_does_not_retry_fatal_errors(monkeypatch) -> None:
    from verdict_eval.providers import CompletionRequest, LiteLLMAdapter

    attempts = []

    def completion(**_kwargs):
        attempts.append(1)
        raise _ProviderError(
            "429 rate limit temporarily unavailable",
            status_code=401,
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))

    adapter = LiteLLMAdapter(max_retries=4)
    try:
        adapter.complete(CompletionRequest(model="custom/model", messages=[]))
    except RuntimeError as exc:
        assert "rate limit" in str(exc)
    else:  # pragma: no cover - the adapter must propagate fatal errors
        raise AssertionError("fatal LiteLLM error was swallowed")

    assert len(attempts) == 1


def test_litellm_adapter_does_not_infer_http_status_from_message_digits(
    monkeypatch,
) -> None:
    from verdict_eval.providers import CompletionRequest, LiteLLMAdapter

    attempts = []

    def completion(**_kwargs):
        attempts.append(1)
        raise RuntimeError("429 rate limit exceeded for org-1400")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))

    adapter = LiteLLMAdapter(max_retries=4)
    try:
        adapter.complete(CompletionRequest(model="custom/model", messages=[]))
    except RuntimeError as exc:
        assert "org-1400" in str(exc)
    else:  # pragma: no cover - the adapter must propagate unclassified errors
        raise AssertionError("unstructured LiteLLM error was swallowed")

    assert len(attempts) == 1


def test_retry_classifier_accepts_typed_timeout() -> None:
    from verdict_eval.providers import _is_retryable_error

    assert _is_retryable_error(TimeoutError("request deadline exceeded"))


def test_retry_classifier_reads_response_status() -> None:
    from verdict_eval.providers import _is_retryable_error

    exc = _ProviderError(
        "opaque provider error",
        response=SimpleNamespace(status_code=503),
    )

    assert _is_retryable_error(exc)


@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504, 529])
def test_retry_classifier_accepts_only_known_transient_http_statuses(
    status_code: int,
) -> None:
    from verdict_eval.providers import _is_retryable_error

    assert _is_retryable_error(_ProviderError("opaque provider error", code=status_code))


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422, 501, 505])
def test_retry_classifier_rejects_nontransient_http_statuses(status_code: int) -> None:
    from verdict_eval.providers import _is_retryable_error

    assert not _is_retryable_error(
        _ProviderError("429 rate limit temporarily unavailable", status_code=status_code)
    )


def test_retry_classifier_prefers_direct_status_over_conflicting_response() -> None:
    from verdict_eval.providers import _is_retryable_error

    exc = _ProviderError(
        "provider error",
        status_code=401,
        response=SimpleNamespace(status_code=503),
    )

    assert not _is_retryable_error(exc)


def test_retry_classifier_accepts_real_anthropic_sdk_errors() -> None:
    anthropic = pytest.importorskip("anthropic")
    httpx = pytest.importorskip("httpx")
    from verdict_eval.providers import _is_retryable_error

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    overloaded = anthropic.InternalServerError(
        "overloaded",
        response=httpx.Response(529, request=request),
        body={"type": "error"},
    )

    assert _is_retryable_error(anthropic.APIConnectionError(request=request))
    assert _is_retryable_error(anthropic.APITimeoutError(request))
    assert overloaded.status_code == 529
    assert _is_retryable_error(overloaded)


def test_retry_classifier_accepts_real_openai_sdk_transport_errors() -> None:
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    from verdict_eval.providers import _is_retryable_error

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    assert _is_retryable_error(openai.APIConnectionError(request=request))
    assert _is_retryable_error(openai.APITimeoutError(request))


@pytest.mark.parametrize(("model", "expected"), [
    ("gpt-5-mini", False), ("gpt-5", False), ("gpt-5.1-codex", False), ("GPT-5-nano", False),
    ("o1", False), ("o1-mini", False), ("o3-mini", False), ("o4-mini", False),
    ("gpt-4.1-mini", True), ("gpt-4o", True), ("gpt-4o-mini", True), ("chatgpt-4o-latest", True),
    ("gpt-3.5-turbo", True), ("qwen3:4b-instruct", True), ("omni-moderation-latest", True),
    ("o10-future", True), ("", True),
])
def test_openai_temperature_rule_follows_the_reasoning_families(model, expected) -> None:
    from verdict_eval.providers import openai_temperature_supported

    assert openai_temperature_supported(model) is expected


def test_openai_adapter_omits_temperature_for_reasoning_models() -> None:
    from verdict_eval.providers import CompletionRequest

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return sdk_httpx.Response(200, json=_chat_completion("{}", "stop"))

    adapter, sdk_httpx = _openai_with_transport(respond)
    adapter.complete(CompletionRequest(model="gpt-5-mini", temperature=0.0, messages=[
        {"role": "user", "content": "q"},
    ]))
    adapter.complete(CompletionRequest(model="gpt-4.1-mini", temperature=0.0, messages=[
        {"role": "user", "content": "q"},
    ]))

    reasoning, sampling = requests
    assert "temperature" not in reasoning
    assert reasoning["max_completion_tokens"] > 0
    assert sampling["temperature"] == 0.0
    assert adapter.temperature_supported("gpt-5-mini") is False
    assert adapter.temperature_supported("gpt-4.1-mini") is True
