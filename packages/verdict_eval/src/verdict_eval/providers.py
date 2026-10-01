"""LLMProvider port for the eval engine.

The eval engine calls judges via this Protocol. Real Anthropic/OpenAI adapters
implement it; tests use a FakeProvider that returns deterministic answers.

This is the canonical port for any code in verdict_eval that needs to call
an LLM. Never `from anthropic import ...` outside an adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class CompletionRequest:
    model: str
    messages: list[dict[str, str]]    # [{role, content}]
    temperature: float = 0.0
    # The answer budget. Adapters send ``output_token_ceiling(max_tokens)`` to
    # the provider so a model's built-in reasoning cannot consume the answer.
    max_tokens: int = 1024


# Models with built-in reasoning spend output tokens on thinking before the
# answer, and every provider counts that thinking against the one output
# ceiling. The judge's ``max_tokens`` is its answer budget; for the families
# below the adapters add this fixed allowance on top. Every one of these
# families accepts at least 64k output tokens, so budget plus allowance never
# exceeds the model's capacity. Any other model, including one that cannot
# accept a large ceiling (gpt-4o-mini stops at 16,384), gets the budget as is;
# a reply that still hits the ceiling is reported through
# ``CompletionResponse.finish_reason`` as a retryable judge error.
REASONING_TOKEN_ALLOWANCE = 16_384
# Name prefixes, matched on the model leaf after any ``provider/`` prefix.
_REASONING_FAMILIES: tuple[str, ...] = (
    "gpt-5", "gpt-6", "o1", "o3", "o4",  # OpenAI reasoning models
    "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-sonnet-5",  # adaptive thinking
    "gemini-2.5", "gemini-3",  # thinking on by default
)


def reasons_by_default(model: object) -> bool:
    """Whether this model spends output tokens on built-in reasoning."""
    name = str(model or "").strip().lower().rsplit("/", 1)[-1]
    return any(
        name == family or name.startswith((family + "-", family + "."))
        for family in _REASONING_FAMILIES
    )


def output_token_ceiling(max_tokens: int, model: object = None) -> int:
    """The output ceiling an adapter sends for a judge answer budget on ``model``."""
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("max_tokens must be a positive integer")
    if reasons_by_default(model):
        return max_tokens + REASONING_TOKEN_ALLOWANCE
    return max_tokens


# OpenAI's reasoning families accept only the default sampling temperature and
# reject any other value with HTTP 400 (param "temperature"). OpenAI names the
# families consistently and its Models API exposes no capability flag, so the
# rule is a family prefix table. A judge on one of these models runs at the
# provider default, and the evaluator identity records that no temperature
# was applied.
_OPENAI_FIXED_TEMPERATURE_FAMILIES = ("gpt-5", "o1", "o3", "o4")


def openai_temperature_supported(model: str) -> bool:
    """Whether OpenAI Chat Completions accepts a temperature for this model."""
    name = str(model).strip().lower()
    return not any(
        name == family or name.startswith((family + "-", family + "."))
        for family in _OPENAI_FIXED_TEMPERATURE_FAMILIES
    )


@dataclass
class CompletionResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Provider port. All eval-engine LLM calls go through this."""

    name: str

    def complete(self, req: CompletionRequest) -> CompletionResponse: ...


# ---------------------------------------------------------------------------
# Reference adapters
# ---------------------------------------------------------------------------


class FakeProvider:
    """Deterministic adapter for tests. Returns a fixed string (or callable)."""

    name = "fake"

    def __init__(self, response: str | callable = "OK") -> None:  # type: ignore[type-arg]
        self._response = response

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        text = self._response(req) if callable(self._response) else self._response
        return CompletionResponse(text=str(text), output_tokens=len(str(text)) // 4)


class AnthropicAdapter:
    """Live Anthropic adapter. Lazy-imports the SDK.

    Requests stream and are assembled with the SDK's final-message helper:
    the output ceiling includes a reasoning allowance for models that think by
    default, and the SDK refuses large non-streaming ceilings. Thinking blocks are not part of the reply
    text. Includes automatic exponential-backoff retry on transient errors.
    """

    name = "anthropic"

    def __init__(self, api_key: str | None = None, *, max_retries: int = 4) -> None:
        import inspect
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise ImportError(
                "AnthropicAdapter requires `pip install anthropic`"
            ) from e
        self._client = Anthropic(api_key=api_key) if api_key else Anthropic()
        self._max_retries = max_retries
        # Requests go through ``messages.stream``; the installed SDK decides
        # whether that call accepts a temperature at all.
        self.supports_temperature = (
            "temperature" in inspect.signature(self._client.messages.stream).parameters
        )

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        return _with_retry(self._complete_once, req, max_attempts=self._max_retries)

    def _complete_once(self, req: CompletionRequest) -> CompletionResponse:
        # Anthropic separates "system" from "messages"; combine if needed
        system_parts = [m["content"] for m in req.messages if m["role"] == "system"]
        chat = [m for m in req.messages if m["role"] != "system"]
        kwargs: dict = {
            "model": req.model,
            "messages": chat,
            "max_tokens": output_token_ceiling(req.max_tokens, req.model),
        }
        if getattr(self, "supports_temperature", True):
            kwargs["temperature"] = req.temperature
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        with self._client.messages.stream(**kwargs) as stream:
            resp = stream.get_final_message()
        text = ""
        for block in resp.content or []:
            t = getattr(block, "text", None)
            if t:
                text += t
        usage = getattr(resp, "usage", None)
        return CompletionResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            finish_reason=getattr(resp, "stop_reason", None),
        )


class OpenAIAdapter:
    """Live OpenAI adapter. Lazy-imports the SDK.

    Sends ``max_completion_tokens``, the current Chat Completions ceiling that
    reasoning models such as gpt-5 require (they reject ``max_tokens``). An
    OpenAI-compatible server behind ``OPENAI_BASE_URL`` must honor it; Ollama
    0.34 ignores it, so a local judge there runs without an output ceiling.
    Omits the temperature for reasoning families, which accept only their
    default. Includes automatic exponential-backoff retry on transient errors.
    """

    name = "openai"

    def __init__(self, api_key: str | None = None, *, max_retries: int = 4) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("OpenAIAdapter requires `pip install openai`") from e
        self._client = OpenAI(api_key=api_key) if api_key else OpenAI()
        self._max_retries = max_retries

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        return _with_retry(self._complete_once, req, max_attempts=self._max_retries)

    def temperature_supported(self, model: str) -> bool:
        """Per-model temperature support; judges record it in the evaluator identity."""
        return openai_temperature_supported(model)

    def _complete_once(self, req: CompletionRequest) -> CompletionResponse:
        kwargs: dict = {
            "model": req.model,
            "messages": req.messages,
            "max_completion_tokens": output_token_ceiling(req.max_tokens, req.model),
        }
        if self.temperature_supported(req.model):
            kwargs["temperature"] = req.temperature
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        return CompletionResponse(
            text=choice.message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            finish_reason=choice.finish_reason,
        )


class LiteLLMAdapter:
    """Unified provider adapter via LiteLLM (MIT, BerriAI).

    Supports 100+ models (Anthropic, OpenAI, Google, Bedrock, Together,
    Mistral, Cohere, vLLM, Ollama, Replicate, etc.) through a single API.
    Reads provider-specific API keys from the standard env vars
    (ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, etc.).

    Install: `pip install litellm`

    Model strings are LiteLLM-format: "anthropic/claude-haiku-4-5",
    "openai/gpt-4o-mini", "gemini/gemini-2.5-flash", etc.

    Prefer this over the per-provider adapters above; those are kept for
    cases where direct SDK control matters (e.g. specific Anthropic features
    not yet in LiteLLM).
    """

    name = "litellm"

    def __init__(self, *, max_retries: int = 4) -> None:
        try:
            import litellm  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "LiteLLMAdapter requires `pip install litellm`"
            ) from e
        self._max_retries = max_retries

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        return _with_retry(self._complete_once, req, max_attempts=self._max_retries)

    def _complete_once(self, req: CompletionRequest) -> CompletionResponse:
        import litellm

        # LiteLLM accepts the OpenAI-shaped messages list directly
        resp = litellm.completion(
            model=req.model,
            messages=req.messages,
            temperature=req.temperature,
            max_tokens=output_token_ceiling(req.max_tokens, req.model),
        )
        choice = resp["choices"][0]
        usage = resp.get("usage")
        return CompletionResponse(
            text=choice["message"]["content"] or "",
            input_tokens=usage.get("prompt_tokens") if usage else None,
            output_tokens=usage.get("completion_tokens") if usage else None,
            finish_reason=choice.get("finish_reason"),
        )


_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504, 529})
_TRANSPORT_EXCEPTION_BASES = {
    "anthropic": frozenset({"APIConnectionError"}),
    "httpcore": frozenset({"NetworkError", "TimeoutException"}),
    "httpcore2": frozenset({"NetworkError", "TimeoutException"}),
    "httpx": frozenset({"NetworkError", "TimeoutException"}),
    "httpx2": frozenset({"NetworkError", "TimeoutException"}),
    "litellm": frozenset({"APIConnectionError", "Timeout"}),
    "openai": frozenset({"APIConnectionError"}),
    "requests": frozenset({"ConnectionError", "Timeout"}),
}


def _safe_attribute(value: object, name: str) -> object | None:
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _structured_http_status(exc: Exception) -> int | None:
    response = _safe_attribute(exc, "response")
    candidates = (
        _safe_attribute(exc, "status_code"),
        _safe_attribute(response, "status_code") if response is not None else None,
        _safe_attribute(exc, "code"),
    )
    for candidate in candidates:
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and 100 <= candidate <= 599
        ):
            return candidate
    return None


def _is_transport_error(exc: Exception) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    for cls in type(exc).__mro__:
        package = cls.__module__.partition(".")[0]
        if cls.__name__ in _TRANSPORT_EXCEPTION_BASES.get(package, ()):
            return True
    return False


def _is_retryable_error(exc: Exception) -> bool:
    """Identify transient failures from status fields or exception types."""
    status_code = _structured_http_status(exc)
    if status_code is not None:
        return status_code in _RETRYABLE_HTTP_STATUS_CODES
    return _is_transport_error(exc)


def _with_retry(fn, *args, max_attempts: int = 4, base_delay: float = 1.5, **kwargs):
    """Exponential-backoff retry with jitter.

    Defaults: 4 attempts total, base delay 1.5s, exponential factor 2,
    plus ±20% jitter to spread retries across multiple concurrent callers.
    Total wait worst case: 1.5 + 3 + 6 + 12 = 22.5s.

    Only retries errors classified as transient by `_is_retryable_error`.
    """
    import random
    import time
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if not _is_retryable_error(e):
                raise
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            delay *= (0.8 + random.random() * 0.4)  # ±20% jitter
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry loop exited without success or exception")


class GoogleAdapter:
    """Live Google Gemini adapter. Lazy-imports the `google-genai` SDK.

    Install with: `pip install google-genai`. Requires either GOOGLE_API_KEY
    or GEMINI_API_KEY env var, or pass api_key explicitly.

    Includes automatic exponential-backoff retry on transient errors
    (408 timeout, 429 rate limit, 500/502/503/504, provider overload 529,
    and transport failures).
    Gemini AI Studio's
    free tier shows ~10% transient 503s on sustained traffic; retries
    bring effective error rate to under 1%.
    """

    name = "google"

    def __init__(self, api_key: str | None = None, *, max_retries: int = 4) -> None:
        try:
            from google import genai
        except ImportError as e:
            raise ImportError(
                "GoogleAdapter requires `pip install google-genai`"
            ) from e
        import os
        key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "GoogleAdapter requires an API key via constructor arg or "
                "GOOGLE_API_KEY / GEMINI_API_KEY env var."
            )
        self._client = genai.Client(api_key=key)
        self._max_retries = max_retries

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        return _with_retry(self._complete_once, req, max_attempts=self._max_retries)

    def _complete_once(self, req: CompletionRequest) -> CompletionResponse:
        # Gemini's API separates system instruction from messages
        from google.genai import types

        system_parts = [m["content"] for m in req.messages if m["role"] == "system"]
        # Combine non-system messages into a single content stream
        contents: list[Any] = []
        for m in req.messages:
            if m["role"] == "system":
                continue
            role = "user" if m["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))

        config = types.GenerateContentConfig(
            temperature=req.temperature,
            max_output_tokens=output_token_ceiling(req.max_tokens, req.model),
            system_instruction="\n\n".join(system_parts) if system_parts else None,
        )

        resp = self._client.models.generate_content(
            model=req.model,
            contents=contents,
            config=config,
        )

        text = resp.text or ""
        usage = getattr(resp, "usage_metadata", None)
        finish = ""
        if resp.candidates:
            finish = str(getattr(resp.candidates[0], "finish_reason", "") or "")
        return CompletionResponse(
            text=text,
            input_tokens=getattr(usage, "prompt_token_count", None) if usage else None,
            output_tokens=getattr(usage, "candidates_token_count", None) if usage else None,
            finish_reason=finish,
        )
