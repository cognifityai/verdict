"""Provider adapter tests that exercise request and response translation."""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace


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

    class HttpRetryOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class HttpOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    return SimpleNamespace(
        Part=Part,
        Content=Content,
        GenerateContentConfig=GenerateContentConfig,
        HttpOptions=HttpOptions,
        HttpRetryOptions=HttpRetryOptions,
    )


def test_direct_provider_clients_disable_hidden_sdk_retries(monkeypatch) -> None:
    from verdict_eval.providers import AnthropicAdapter, GoogleAdapter, OpenAIAdapter

    captured = {}

    def anthropic_client(**kwargs):
        captured["anthropic"] = kwargs
        return SimpleNamespace()

    def openai_client(**kwargs):
        captured["openai"] = kwargs
        return SimpleNamespace()

    google_types = _fake_google_types()

    def google_client(**kwargs):
        captured["google"] = kwargs
        return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=anthropic_client))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai_client))
    fake_genai = SimpleNamespace(Client=google_client, types=google_types)
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(genai=fake_genai))
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    AnthropicAdapter(api_key="test", max_retries=1, timeout_seconds=30)
    OpenAIAdapter(api_key="test", max_retries=1, timeout_seconds=30)
    GoogleAdapter(api_key="test", max_retries=1, timeout_seconds=30)

    assert captured["anthropic"]["max_retries"] == 0
    assert captured["anthropic"]["timeout"] == 30
    assert captured["openai"]["max_retries"] == 0
    assert captured["openai"]["timeout"] == 30
    google_http = captured["google"]["http_options"]
    assert google_http.timeout == 30_000
    assert google_http.retry_options.attempts == 1


def test_google_adapter_translates_messages_config_and_usage(monkeypatch) -> None:
    from verdict_eval.providers import CompletionRequest, GoogleAdapter

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
    assert captured["config"].max_output_tokens == 77
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
            raise RuntimeError("503 service unavailable")
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
        raise RuntimeError("401 unauthorized")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))

    adapter = LiteLLMAdapter(max_retries=4)
    try:
        adapter.complete(CompletionRequest(model="custom/model", messages=[]))
    except RuntimeError as exc:
        assert "401" in str(exc)
    else:  # pragma: no cover - the adapter must propagate fatal errors
        raise AssertionError("fatal LiteLLM error was swallowed")

    assert len(attempts) == 1
