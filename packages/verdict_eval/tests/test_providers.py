"""Provider adapter tests that exercise request and response translation."""

from __future__ import annotations

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


def test_anthropic_adapter_omits_unsupported_temperature() -> None:
    from verdict_eval.providers import AnthropicAdapter, CompletionRequest

    captured = {}
    adapter = object.__new__(AnthropicAdapter)
    adapter.supports_temperature = False
    adapter._client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: (
            captured.update(kwargs)
            or SimpleNamespace(
                content=[SimpleNamespace(text="ok")],
                usage=SimpleNamespace(input_tokens=2, output_tokens=1),
                stop_reason="end_turn",
            )
        )
    ))

    response = adapter._complete_once(CompletionRequest(
        model="claude-test", messages=[{"role": "user", "content": "q"}], temperature=0.0,
    ))

    assert "temperature" not in captured
    assert captured["model"] == "claude-test"
    assert response.text == "ok"


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


@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
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
