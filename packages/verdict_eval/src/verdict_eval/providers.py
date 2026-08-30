"""LLMProvider port for the eval engine.

The eval engine calls judges via this Protocol. Real Anthropic/OpenAI adapters
implement it; tests use a FakeProvider that returns deterministic answers.

This is the canonical port for any code in verdict_eval that needs to call
an LLM. Never `from anthropic import ...` outside an adapter.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class CompletionRequest:
    model: str
    messages: list[dict[str, str]]    # [{role, content}]
    temperature: float = 0.0
    max_tokens: int = 1024


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


class BudgetExceededError(RuntimeError):
    """Raised before a provider call that could exceed its declared ceiling."""


class BudgetedProvider:
    """Serial provider guard using a byte-safe preflight and actual token usage."""

    _INPUT_PROTOCOL_TOKEN_CEILING = 1_024

    def __init__(
        self,
        provider: LLMProvider,
        *,
        budget_usd: float,
        input_usd_per_million: float,
        output_usd_per_million: float,
    ) -> None:
        if budget_usd <= 0 or input_usd_per_million < 0 or output_usd_per_million < 0:
            raise ValueError("budget and token prices must be nonnegative")
        self._provider = provider
        self.name = provider.name
        self.budget_usd = float(budget_usd)
        self.input_usd_per_million = float(input_usd_per_million)
        self.output_usd_per_million = float(output_usd_per_million)
        self.spent_usd = 0.0
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._blocked = False

    def upper_bound_usd(self, req: CompletionRequest) -> float:
        input_token_ceiling = self._INPUT_PROTOCOL_TOKEN_CEILING + sum(
            len(message.get("content", "").encode("utf-8", "surrogatepass"))
            for message in req.messages
        )
        return (
            input_token_ceiling * self.input_usd_per_million
            + req.max_tokens * self.output_usd_per_million
        ) / 1_000_000

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        if self._blocked:
            raise BudgetExceededError("judge budget accounting is blocked")
        if self.spent_usd + self.upper_bound_usd(req) > self.budget_usd:
            raise BudgetExceededError("judge budget ceiling reached before provider call")
        response = self._provider.complete(req)
        if response.input_tokens is None or response.output_tokens is None:
            self._blocked = True
            raise RuntimeError("provider omitted usage; budget accounting cannot continue")
        cost = (
            response.input_tokens * self.input_usd_per_million
            + response.output_tokens * self.output_usd_per_million
        ) / 1_000_000
        if self.spent_usd + cost > self.budget_usd:
            self._blocked = True
            raise RuntimeError("provider usage exceeded the pre-call byte-safe budget bound")
        self.spent_usd += cost
        self.calls += 1
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        return response


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

    Includes automatic exponential-backoff retry on transient errors.
    """

    name = "anthropic"

    def __init__(self, api_key: str | None = None, *, max_retries: int = 4) -> None:
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise ImportError(
                "AnthropicAdapter requires `pip install anthropic`"
            ) from e
        self._client = Anthropic(api_key=api_key) if api_key else Anthropic()
        self._max_retries = max_retries
        self._supports_temperature = (
            "temperature" in inspect.signature(self._client.messages.create).parameters
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
            "max_tokens": req.max_tokens,
        }
        if getattr(self, "_supports_temperature", True):
            kwargs["temperature"] = req.temperature
        else:
            # Anthropic Python SDK 1.x removed the typed sampling kwargs. Older
            # Claude models still accept them at the HTTP boundary.
            kwargs["extra_body"] = {"temperature": req.temperature}
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        resp = self._client.messages.create(**kwargs)
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

    Includes automatic exponential-backoff retry on transient errors.
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

    def _complete_once(self, req: CompletionRequest) -> CompletionResponse:
        resp = self._client.chat.completions.create(
            model=req.model,
            messages=req.messages,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
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
            max_tokens=req.max_tokens,
        )
        choice = resp["choices"][0]
        usage = resp.get("usage")
        return CompletionResponse(
            text=choice["message"]["content"] or "",
            input_tokens=usage.get("prompt_tokens") if usage else None,
            output_tokens=usage.get("completion_tokens") if usage else None,
            finish_reason=choice.get("finish_reason"),
        )


def _is_retryable_error(exc: Exception) -> bool:
    """Identify transient errors worth retrying.

    Catches: 503 UNAVAILABLE (capacity), 429 (rate limit), 500/502/504 (transient),
    connection timeouts, connection resets. Does NOT retry on 4xx auth errors
    or model-not-found errors — those won't get better with a wait.
    """
    msg = str(exc).lower()
    # Common transient indicators across SDKs
    transient_signals = [
        "503", "unavailable",
        "429", "rate limit", "ratelimit", "too many requests",
        "500", "502", "504",
        "timeout", "timed out",
        "connection reset", "connection error",
        "temporarily", "try again",
        "high demand",
    ]
    # Hard-fail signals (don't retry)
    fatal_signals = [
        "401", "unauthorized",
        "403", "permission_denied",
        "404", "not_found", "model not found",
        "400", "invalid_argument",
    ]
    if any(s in msg for s in fatal_signals):
        return False
    return any(s in msg for s in transient_signals)


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
    (503 UNAVAILABLE, 429 rate limit, 5xx, timeouts). Gemini AI Studio's
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
            max_output_tokens=req.max_tokens,
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
