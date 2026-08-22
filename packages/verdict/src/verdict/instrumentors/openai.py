"""OpenAI SDK auto-instrumentation. Mirrors AnthropicInstrumentor in structure."""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import inspect
import json
import random
import time
from datetime import datetime, timezone
from typing import Any

from verdict.instrumentors.base import (
    BaseInstrumentor,
    apply_routing_context,
    decide_persist,
    is_verdict_wrapt_wrapper,
    normalize_finish_reason,
)
from verdict.pricing import compute_cost_usd
from verdict.redaction import redact, redact_messages
from verdict.schema import (
    Operation,
    Trace,
    normalize_optional_float,
    normalize_optional_integer,
)

# Dedicated RNG so an app calling random.seed() can't perturb our sampling.
_rng = random.Random()


class _ResponseRequestAttempt:
    """Task-local ownership for one supported Responses resource call."""

    def __init__(self, sdk_client: Any) -> None:
        self.sdk_client = sdk_client
        self.http_client = getattr(sdk_client, "_client", None)
        self.options: Any = None
        self.attempted = False
        self.request_kwargs: dict[str, Any] | None = None


_response_request_attempt: contextvars.ContextVar[_ResponseRequestAttempt | None] = (
    contextvars.ContextVar("verdict_openai_response_request_attempt", default=None)
)
_active_response_request: contextvars.ContextVar[_ResponseRequestAttempt | None] = (
    contextvars.ContextVar("verdict_openai_active_response_request", default=None)
)


def _flatten_content(content: Any) -> str:
    """Reduce a chat message's ``content`` (str or list of parts) to plain text.

    OpenAI multimodal messages use a list of dict parts; we extract the "text"
    field from any text parts and join them. Mirrors the Anthropic flattener.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for block in content:
        if isinstance(block, str):
            out.append(block)
            continue
        if isinstance(block, dict):
            text = block.get("text")
            if text:
                out.append(text)
            continue
        text = getattr(block, "text", None)
        if text:
            out.append(text)
    return "\n".join(out)


def _responses_input_messages(kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize supported Responses input into Verdict's message envelope.

    The SDK contract accepts a string or a concrete list. Avoid probing any
    other iterable here: provider-owned traversal must remain on the provider
    request path, and telemetry must never consume a one-shot input first.
    """
    messages: list[dict[str, Any]] = []
    instructions = kwargs.get("instructions")
    if isinstance(instructions, str):
        messages.append({"role": "system", "content": instructions})

    value = kwargs.get("input")
    if isinstance(value, str):
        messages.append({"role": "user", "content": value})
    elif isinstance(value, list):
        # ``list.copy`` reads the concrete list storage without invoking an
        # overridden, potentially single-pass ``__iter__`` implementation.
        for item in list.copy(value):
            if isinstance(item, dict):
                role = item.get("role")
                if isinstance(role, str) and "content" in item:
                    messages.append(item)
                else:
                    messages.append(
                        {
                            "role": str(role or item.get("type") or "input"),
                            "content": item,
                        }
                    )
            else:
                messages.append({"role": "input", "content": item})
    return messages


def _flatten_responses_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text for item in value if (text := _flatten_responses_value(item)))
    if isinstance(value, dict):
        return "\n".join(
            text
            for key in (
                "text",
                "content",
                "input",
                "output",
                "arguments",
                "refusal",
            )
            if key in value
            if (text := _flatten_responses_value(value[key]))
        )
    return ""


def _responses_output_text(response: Any) -> str:
    text = getattr(response, "output_text", "")
    parts = [text] if isinstance(text, str) and text else []
    for item in getattr(response, "output", None) or []:
        for block in getattr(item, "content", None) or []:
            refusal = getattr(block, "refusal", None)
            if isinstance(refusal, str) and refusal:
                parts.append(refusal)
    return "\n".join(parts)


def _response_error_text(value: Any) -> str | None:
    if value is None:
        return None
    code = getattr(value, "code", None)
    message = getattr(value, "message", None)
    if isinstance(value, dict):
        code = value.get("code", code)
        message = value.get("message", message)
    parts = [str(part) for part in (code, message) if part not in (None, "")]
    return ": ".join(parts) if parts else str(value)


def _is_wrapped(mod: Any, cls_name: str, method: str) -> bool:
    """True only if ``mod.<cls_name>.<method>`` is Verdict's wrapt wrapper."""
    cls = getattr(mod, cls_name, None)
    if cls is None:
        return False
    bound = getattr(cls, method, None)
    return is_verdict_wrapt_wrapper(bound)


def _maybe_import_openai() -> bool:
    try:
        import openai  # noqa: F401

        return True
    except ImportError:
        return False


def _has_method(mod: Any, cls_name: str, method: str) -> bool:
    cls = getattr(mod, cls_name, None)
    return cls is not None and callable(getattr(cls, method, None))


def _responses_resource_module() -> tuple[Any, str] | None:
    """Resolve Responses when present without raising on openai 1.30."""
    for path in (
        "openai.resources.responses.responses",
        "openai.resources.responses",
    ):
        try:
            return importlib.import_module(path), path
        except ModuleNotFoundError as exc:
            missing = exc.name
            if missing is None or not (missing == path or path.startswith(f"{missing}.")):
                raise
    return None


def _response_http_modules(base_client_mod: Any) -> tuple[Any, ...]:
    """Return OpenAI's native HTTP module plus supported injected clients."""
    modules: list[Any] = []
    seen: set[str] = set()
    for name in ("httpx2", "httpx"):
        module = getattr(base_client_mod, name, None)
        if module is None:
            try:
                module = importlib.import_module(name)
            except ImportError:
                continue
        module_name = getattr(module, "__name__", name)
        if module_name not in seen:
            seen.add(module_name)
            modules.append(module)
    return tuple(modules)


def _request_options(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Extract FinalRequestOptions from OpenAI's sync/async request call."""
    if len(args) >= 2:
        return args[1]
    return kwargs.get("options")


def _httpx_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if args:
        return args[0]
    return kwargs.get("request")


def _is_responses_request(request: Any) -> bool:
    try:
        return "responses" in str(request.url.path).split("/")
    except Exception:
        return False


def _serialized_response_kwargs(
    request: Any,
    *,
    capture_content: bool,
) -> dict[str, Any]:
    """Read an allowlisted view of OpenAI's already-serialized request body."""
    try:
        body = json.loads(request.content)
    except Exception:
        return {}
    if not isinstance(body, dict):
        return {}
    keys = [
        "model",
        "temperature",
        "max_output_tokens",
    ]
    if capture_content:
        keys.extend(("input", "instructions"))
    return {key: body[key] for key in keys if key in body}


class OpenAIInstrumentor(BaseInstrumentor):
    name = "openai"

    def __init__(self, client) -> None:
        super().__init__(client)
        self._disabled = False

    def available(self) -> bool:
        return _maybe_import_openai()

    def install(self) -> None:
        if self._installed:
            return
        self._disabled = False
        try:
            import wrapt
        except ImportError as e:
            raise RuntimeError("wrapt is required") from e

        import openai.resources.chat.completions as mod

        # OpenAI v1+ structure. Guard against double-wrapping: if a prior
        # install (or another VerdictClient) already wrapped the target, a
        # second wrap would stack and double-record every call.
        if not _is_wrapped(mod, "Completions", "create"):
            wrapt.wrap_function_wrapper(
                "openai.resources.chat.completions",
                "Completions.create",
                self._wrap_create_sync,
            )
        if not _is_wrapped(mod, "AsyncCompletions", "create"):
            wrapt.wrap_function_wrapper(
                "openai.resources.chat.completions",
                "AsyncCompletions.create",
                self._wrap_create_async,
            )

        for cls_name, wrapper in (
            ("Completions", self._wrap_stream_manager_sync),
            ("AsyncCompletions", self._wrap_stream_manager_async),
        ):
            if _has_method(mod, cls_name, "stream") and not _is_wrapped(mod, cls_name, "stream"):
                wrapt.wrap_function_wrapper(
                    "openai.resources.chat.completions",
                    f"{cls_name}.stream",
                    wrapper,
                )

        responses_resource = _responses_resource_module()
        if responses_resource is not None:
            import openai._base_client as base_client_mod

            for cls_name, wrapper in (
                ("SyncAPIClient", self._wrap_openai_request_sync),
                ("AsyncAPIClient", self._wrap_openai_request_async),
            ):
                if _has_method(base_client_mod, cls_name, "request") and not _is_wrapped(
                    base_client_mod,
                    cls_name,
                    "request",
                ):
                    wrapt.wrap_function_wrapper(
                        "openai._base_client",
                        f"{cls_name}.request",
                        wrapper,
                    )
            for http_module in _response_http_modules(base_client_mod):
                for cls_name, wrapper in (
                    ("Client", self._wrap_httpx_send_sync),
                    ("AsyncClient", self._wrap_httpx_send_async),
                ):
                    cls = getattr(http_module, cls_name, None)
                    bound = getattr(cls, "_send_single_request", None)
                    if cls is not None and callable(bound) and not is_verdict_wrapt_wrapper(bound):
                        wrapt.wrap_function_wrapper(
                            http_module.__name__,
                            f"{cls_name}._send_single_request",
                            wrapper,
                        )
            responses_mod, responses_path = responses_resource
            response_methods = (
                ("Responses", "create", self._wrap_responses_sync),
                ("Responses", "parse", self._wrap_responses_sync),
                ("Responses", "retrieve", self._wrap_responses_retrieve_sync),
                ("Responses", "stream", self._wrap_stream_manager_sync),
                ("AsyncResponses", "create", self._wrap_responses_async),
                ("AsyncResponses", "parse", self._wrap_responses_async),
                ("AsyncResponses", "retrieve", self._wrap_responses_retrieve_async),
                ("AsyncResponses", "stream", self._wrap_stream_manager_async),
            )
            for cls_name, method, wrapper in response_methods:
                if _has_method(responses_mod, cls_name, method) and not _is_wrapped(
                    responses_mod, cls_name, method
                ):
                    wrapt.wrap_function_wrapper(
                        responses_path,
                        f"{cls_name}.{method}",
                        wrapper,
                    )
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        self._disabled = True
        try:
            import openai.resources.chat.completions as mod
        except ImportError:
            self._installed = False
            return
        for cls_name, method in (
            ("Completions", "create"),
            ("AsyncCompletions", "create"),
            ("Completions", "stream"),
            ("AsyncCompletions", "stream"),
        ):
            cls = getattr(mod, cls_name, None)
            if cls is not None:
                bound = getattr(cls, method, None)
                if is_verdict_wrapt_wrapper(bound, owner=self):
                    setattr(cls, method, bound.__wrapped__)
        responses_resource = _responses_resource_module()
        if responses_resource is not None:
            try:
                import openai._base_client as base_client_mod
            except ImportError:
                base_client_mod = None
            if base_client_mod is not None:
                for cls_name in ("SyncAPIClient", "AsyncAPIClient"):
                    cls = getattr(base_client_mod, cls_name, None)
                    if cls is not None:
                        bound = getattr(cls, "request", None)
                        if is_verdict_wrapt_wrapper(bound, owner=self):
                            cls.request = bound.__wrapped__
                for http_module in _response_http_modules(base_client_mod):
                    for cls_name in ("Client", "AsyncClient"):
                        cls = getattr(http_module, cls_name, None)
                        bound = getattr(cls, "_send_single_request", None)
                        if is_verdict_wrapt_wrapper(bound, owner=self):
                            cls._send_single_request = bound.__wrapped__
            responses_mod, _responses_path = responses_resource
            for cls_name, method in (
                ("Responses", "create"),
                ("Responses", "parse"),
                ("Responses", "retrieve"),
                ("Responses", "stream"),
                ("AsyncResponses", "create"),
                ("AsyncResponses", "parse"),
                ("AsyncResponses", "retrieve"),
                ("AsyncResponses", "stream"),
            ):
                cls = getattr(responses_mod, cls_name, None)
                if cls is not None:
                    bound = getattr(cls, method, None)
                    if is_verdict_wrapt_wrapper(bound, owner=self):
                        setattr(cls, method, bound.__wrapped__)
        self._installed = False

    # -- wrappers ----------------------------------------------------------

    def _should_sample(self) -> bool:
        rate = self.client.sample_rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return _rng.random() < rate

    def _wrap_create_sync(self, wrapped, instance, args, kwargs):
        if self._disabled:
            return wrapped(*args, **kwargs)
        trace = self._build_input_trace(kwargs)
        t0 = time.perf_counter()

        # Streaming path: wrap the iterator so we accumulate text + usage +
        # finish_reason and finalize in the wrapper's `finally`. Mirrors the
        # Anthropic _StreamingWrapper contract (cancellation still records;
        # a mid-stream raise is recorded as an error, not a truncated success).
        if kwargs.get("stream"):
            try:
                stream = wrapped(*args, **kwargs)
            except Exception as e:
                trace.ended_at = datetime.now(timezone.utc)
                trace.latency_ms = (time.perf_counter() - t0) * 1000.0
                trace.error = f"{type(e).__name__}: {e}"
                self._safe_persist(trace)
                raise
            return _StreamingWrapper(stream, trace, t0, self)

        # Always call through; capture every error, sample successes.
        try:
            resp = wrapped(*args, **kwargs)
        except Exception as e:
            trace.ended_at = datetime.now(timezone.utc)
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            trace.error = f"{type(e).__name__}: {e}"
            self._safe_persist(trace)
            raise
        should_persist, _is_error = decide_persist(False, self._should_sample())
        if should_persist:
            self._fill_output(trace, resp)
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._safe_persist(trace)
        return resp

    async def _wrap_create_async(self, wrapped, instance, args, kwargs):
        if self._disabled:
            return await wrapped(*args, **kwargs)
        trace = self._build_input_trace(kwargs)
        t0 = time.perf_counter()

        if kwargs.get("stream"):
            try:
                stream = await wrapped(*args, **kwargs)
            except asyncio.CancelledError as e:
                trace.ended_at = datetime.now(timezone.utc)
                trace.latency_ms = (time.perf_counter() - t0) * 1000.0
                trace.error = f"{type(e).__name__}: {e}"
                self._safe_persist(trace)
                raise
            except Exception as e:
                trace.ended_at = datetime.now(timezone.utc)
                trace.latency_ms = (time.perf_counter() - t0) * 1000.0
                trace.error = f"{type(e).__name__}: {e}"
                self._safe_persist(trace)
                raise
            return _AsyncStreamingWrapper(stream, trace, t0, self)

        try:
            resp = await wrapped(*args, **kwargs)
        except asyncio.CancelledError as e:
            trace.ended_at = datetime.now(timezone.utc)
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            trace.error = f"{type(e).__name__}: {e}"
            self._safe_persist(trace)
            raise
        except Exception as e:
            trace.ended_at = datetime.now(timezone.utc)
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            trace.error = f"{type(e).__name__}: {e}"
            self._safe_persist(trace)
            raise
        should_persist, _is_error = decide_persist(False, self._should_sample())
        if should_persist:
            self._fill_output(trace, resp)
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._safe_persist(trace)
        return resp

    def _wrap_openai_request_sync(self, wrapped, instance, args, kwargs):
        attempt = _response_request_attempt.get()
        selected = None
        if attempt is not None and attempt.sdk_client is instance:
            options = _request_options(args, kwargs)
            if attempt.options is None:
                attempt.options = options
            if options is attempt.options:
                selected = attempt
        token = _active_response_request.set(selected)
        try:
            return wrapped(*args, **kwargs)
        finally:
            _active_response_request.reset(token)

    async def _wrap_openai_request_async(self, wrapped, instance, args, kwargs):
        attempt = _response_request_attempt.get()
        selected = None
        if attempt is not None and attempt.sdk_client is instance:
            options = _request_options(args, kwargs)
            if attempt.options is None:
                attempt.options = options
            if options is attempt.options:
                selected = attempt
        token = _active_response_request.set(selected)
        try:
            return await wrapped(*args, **kwargs)
        finally:
            _active_response_request.reset(token)

    def _wrap_httpx_send_sync(self, wrapped, instance, args, kwargs):
        attempt = _active_response_request.get()
        request = _httpx_request(args, kwargs)
        if (
            attempt is not None
            and attempt.http_client is instance
            and _is_responses_request(request)
        ):
            attempt.attempted = True
            attempt.request_kwargs = _serialized_response_kwargs(
                request,
                capture_content=self.client.capture_content,
            )
        return wrapped(*args, **kwargs)

    async def _wrap_httpx_send_async(self, wrapped, instance, args, kwargs):
        attempt = _active_response_request.get()
        request = _httpx_request(args, kwargs)
        if (
            attempt is not None
            and attempt.http_client is instance
            and _is_responses_request(request)
        ):
            attempt.attempted = True
            attempt.request_kwargs = _serialized_response_kwargs(
                request,
                capture_content=self.client.capture_content,
            )
        return await wrapped(*args, **kwargs)

    def _wrap_responses_sync(self, wrapped, instance, args, kwargs):
        if self._disabled:
            return wrapped(*args, **kwargs)
        trace_kwargs, call_kwargs = self._split_responses_capture_kwargs(kwargs)
        trace = self._build_responses_input_trace(trace_kwargs)
        t0 = time.perf_counter()
        streaming = kwargs.get("stream") is True
        attempt = _ResponseRequestAttempt(getattr(instance, "_client", None))
        attempt_token = _response_request_attempt.set(attempt)
        try:
            response = wrapped(*args, **call_kwargs)
        except Exception as exc:
            if attempt.attempted:
                self._fill_responses_input_trace(trace, attempt.request_kwargs or {})
                self._persist_request_error(
                    trace,
                    t0,
                    exc,
                    stream_completion="error" if streaming else None,
                )
            raise
        finally:
            _response_request_attempt.reset(attempt_token)
        if not attempt.attempted:
            return response
        self._fill_responses_input_trace(trace, attempt.request_kwargs or {})
        if streaming:
            return _ResponsesStreamingWrapper(response, trace, t0, self)

        self._fill_responses_output(trace, response)
        should_persist, _ = decide_persist(
            trace.error is not None,
            self._should_sample(),
        )
        if should_persist:
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._safe_persist(trace)
        return response

    async def _wrap_responses_async(self, wrapped, instance, args, kwargs):
        if self._disabled:
            return await wrapped(*args, **kwargs)
        trace_kwargs, call_kwargs = self._split_responses_capture_kwargs(kwargs)
        trace = self._build_responses_input_trace(trace_kwargs)
        t0 = time.perf_counter()
        streaming = kwargs.get("stream") is True
        attempt = _ResponseRequestAttempt(getattr(instance, "_client", None))
        attempt_token = _response_request_attempt.set(attempt)
        try:
            response = await wrapped(*args, **call_kwargs)
        except asyncio.CancelledError as exc:
            if attempt.attempted:
                self._fill_responses_input_trace(trace, attempt.request_kwargs or {})
                self._persist_request_error(
                    trace,
                    t0,
                    exc,
                    stream_completion="error" if streaming else None,
                )
            raise
        except Exception as exc:
            if attempt.attempted:
                self._fill_responses_input_trace(trace, attempt.request_kwargs or {})
                self._persist_request_error(
                    trace,
                    t0,
                    exc,
                    stream_completion="error" if streaming else None,
                )
            raise
        finally:
            _response_request_attempt.reset(attempt_token)
        if not attempt.attempted:
            return response
        self._fill_responses_input_trace(trace, attempt.request_kwargs or {})
        if streaming:
            return _AsyncResponsesStreamingWrapper(response, trace, t0, self)

        self._fill_responses_output(trace, response)
        should_persist, _ = decide_persist(
            trace.error is not None,
            self._should_sample(),
        )
        if should_persist:
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._safe_persist(trace)
        return response

    def _wrap_responses_retrieve_sync(self, wrapped, instance, args, kwargs):
        if self._disabled:
            return wrapped(*args, **kwargs)
        if kwargs.get("stream") is True:
            return self._wrap_responses_sync(wrapped, instance, args, kwargs)
        return wrapped(*args, **kwargs)

    async def _wrap_responses_retrieve_async(self, wrapped, instance, args, kwargs):
        if self._disabled:
            return await wrapped(*args, **kwargs)
        if kwargs.get("stream") is True:
            return await self._wrap_responses_async(wrapped, instance, args, kwargs)
        return await wrapped(*args, **kwargs)

    def _wrap_stream_manager_sync(self, wrapped, instance, args, kwargs):
        if self._disabled:
            return wrapped(*args, **kwargs)
        return _OpenAIStreamManagerWrapper(wrapped(*args, **kwargs), self)

    def _wrap_stream_manager_async(self, wrapped, instance, args, kwargs):
        if self._disabled:
            return wrapped(*args, **kwargs)
        # OpenAI's async helper method is synchronous and returns a lazy async
        # context manager. Its nested awaited create/retrieve owns the trace.
        return _AsyncOpenAIStreamManagerWrapper(wrapped(*args, **kwargs), self)

    # -- helpers -----------------------------------------------------------

    def _persist_request_error(
        self,
        trace: Trace,
        t0: float,
        exc: BaseException,
        *,
        stream_completion: str | None = None,
    ) -> None:
        trace.ended_at = datetime.now(timezone.utc)
        trace.latency_ms = (time.perf_counter() - t0) * 1000.0
        trace.error = f"{type(exc).__name__}: {exc}"
        if stream_completion is not None:
            trace.tags = {
                **trace.tags,
                "verdict.stream_completion": stream_completion,
            }
        self._safe_persist(trace)

    def _split_responses_capture_kwargs(
        self,
        kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Keep provider kwargs untouched; capture the serialized request later."""
        return kwargs, kwargs

    def _build_responses_input_trace(self, kwargs: dict[str, Any]) -> Trace:
        model_value = kwargs.get("model")
        model = model_value if isinstance(model_value, str) else ""
        trace = Trace(
            provider="openai",
            operation=Operation.CHAT,
            request_model=model,
            response_model=model,
            temperature=normalize_optional_float(kwargs.get("temperature")),
            max_tokens=normalize_optional_integer(kwargs.get("max_output_tokens")),
        )
        apply_routing_context(self.client, trace)
        return trace

    def _fill_responses_input_trace(
        self,
        trace: Trace,
        kwargs: dict[str, Any],
    ) -> None:
        """Fill trace input from the allowlisted serialized outbound body."""
        model = kwargs.get("model")
        if isinstance(model, str):
            trace.request_model = model
            trace.response_model = model
        trace.temperature = normalize_optional_float(kwargs.get("temperature"))
        trace.max_tokens = normalize_optional_integer(kwargs.get("max_output_tokens"))
        if self.client.capture_content:
            messages = _responses_input_messages(kwargs)
            trace.prompt_redacted = redact(
                "\n".join(_flatten_responses_value(message.get("content")) for message in messages),
                mode=self.client.redaction_mode,  # type: ignore[arg-type]
                secret=self.client.redaction_secret,
            )
            trace.raw_messages = redact_messages(
                messages,
                mode=self.client.redaction_mode,
                secret=self.client.redaction_secret,
            )

    def _fill_responses_output(self, trace: Trace, response: Any) -> None:
        trace.ended_at = datetime.now(timezone.utc)
        try:
            model = getattr(response, "model", None)
            if isinstance(model, str) and model:
                trace.response_model = model
            usage = getattr(response, "usage", None)
            if usage is not None:
                trace.input_tokens = normalize_optional_integer(
                    getattr(usage, "input_tokens", None)
                )
                trace.output_tokens = normalize_optional_integer(
                    getattr(usage, "output_tokens", None)
                )
            status = getattr(response, "status", None)
            if status == "completed":
                trace.finish_reason = "completed"
            elif status == "incomplete":
                details = getattr(response, "incomplete_details", None)
                reason = getattr(details, "reason", None)
                trace.finish_reason = normalize_finish_reason(reason or "incomplete")
            elif status == "failed":
                error_text = _response_error_text(getattr(response, "error", None))
                trace.error = redact(
                    error_text or "OpenAI response failed",
                    mode=self.client.redaction_mode,  # type: ignore[arg-type]
                    secret=self.client.redaction_secret,
                )
            elif status == "cancelled":
                trace.finish_reason = "cancelled"
                trace.error = "OpenAI response cancelled"
            elif status in ("queued", "in_progress"):
                trace.finish_reason = status
            trace.cost_usd = compute_cost_usd(
                trace.response_model or trace.request_model,
                trace.input_tokens,
                trace.output_tokens,
            )
            if self.client.capture_content:
                text = _responses_output_text(response)
                trace.response_redacted = redact(
                    text,
                    mode=self.client.redaction_mode,  # type: ignore[arg-type]
                    secret=self.client.redaction_secret,
                )
        except Exception:
            pass

    def _build_input_trace(self, kwargs: dict[str, Any]) -> Trace:
        model = str(kwargs.get("model", ""))
        max_tokens = normalize_optional_integer(kwargs.get("max_tokens"))
        if max_tokens is None:
            max_tokens = normalize_optional_integer(kwargs.get("max_completion_tokens"))
        trace = Trace(
            provider="openai",
            operation=Operation.CHAT,
            request_model=model,
            response_model=model,
            temperature=normalize_optional_float(kwargs.get("temperature")),
            max_tokens=max_tokens,
        )
        apply_routing_context(self.client, trace)
        if self.client.capture_content:
            messages = kwargs.get("messages") or []
            try:
                joined = "\n".join(
                    _flatten_content(m.get("content", "")) for m in messages if isinstance(m, dict)
                )
            except Exception:
                joined = ""
            trace.prompt_redacted = redact(
                joined,
                mode=self.client.redaction_mode,  # type: ignore[arg-type]
                secret=self.client.redaction_secret,
            )
            trace.raw_messages = redact_messages(
                messages,
                mode=self.client.redaction_mode,
                secret=self.client.redaction_secret,
            )
        return trace

    def _fill_output(self, trace: Trace, resp: Any) -> None:
        trace.ended_at = datetime.now(timezone.utc)
        try:
            trace.response_model = (
                getattr(resp, "model", trace.request_model) or trace.request_model
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                trace.input_tokens = getattr(usage, "prompt_tokens", None)
                trace.output_tokens = getattr(usage, "completion_tokens", None)
            trace.cost_usd = compute_cost_usd(
                trace.response_model or trace.request_model,
                trace.input_tokens,
                trace.output_tokens,
            )
            choices = getattr(resp, "choices", None) or []
            if choices:
                first = choices[0]
                trace.finish_reason = normalize_finish_reason(getattr(first, "finish_reason", None))
                if self.client.capture_content:
                    msg = getattr(first, "message", None)
                    text = getattr(msg, "content", "") if msg is not None else ""
                    trace.response_redacted = redact(
                        text,
                        mode=self.client.redaction_mode,  # type: ignore[arg-type]
                        secret=self.client.redaction_secret,
                    )
        except Exception:
            pass


class _StreamingWrapper:
    """Pass-through iterator around an OpenAI chat-completions stream.

    Yields every upstream chunk unchanged so the caller's streaming UX is
    preserved, while accumulating text + finish_reason + (optional) usage for
    the trace. Normal exhaustion, iteration failure, explicit close, and context
    exit finalize deterministically. Async cancellation is recorded as an error.
    A dropped, never-iterated stream is not a supported finalization boundary.

    Token usage on streamed responses is only present when the caller passes
    ``stream_options={"include_usage": True}`` (the SDK then emits a final
    chunk with ``choices == []`` and a populated ``usage``). If usage isn't
    available we record text + finish_reason and leave tokens None.
    """

    def __init__(self, inner: Any, trace: Trace, t0: float, instr: OpenAIInstrumentor) -> None:
        self._inner = inner
        self._trace = trace
        self._t0 = t0
        self._instr = instr
        self._text_chunks: list[str] = []
        self._usage: Any = None
        self._finish_reason: str | None = None
        self._finalized = False
        self._error: str | None = None
        self._iterator: Any = None
        self._finalize_on_iteration = True

    def __iter__(self):
        try:
            for chunk in self._inner:
                self._on_chunk(chunk)
                yield chunk
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            raise
        finally:
            if self._finalize_on_iteration:
                self._finalize()

    def __next__(self):
        if self._iterator is None:
            self._iterator = self.__iter__()
        return next(self._iterator)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __enter__(self):
        if hasattr(self._inner, "__enter__"):
            self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and self._error is None:
            self._error = f"{type(exc).__name__}: {exc}"
        suppressed = False
        if hasattr(self._inner, "__exit__"):
            try:
                suppressed = bool(self._inner.__exit__(exc_type, exc, tb))
            except Exception as inner_exc:
                if self._error is None:
                    self._error = f"{type(inner_exc).__name__}: {inner_exc}"
                raise
            finally:
                self._finalize()
        else:
            self._finalize()
        return suppressed

    def close(self) -> None:
        """Close the upstream stream and finalize this trace exactly once."""
        try:
            inner_close = getattr(self._inner, "close", None)
            if inner_close is not None:
                inner_close()
        except Exception as exc:
            if self._error is None:
                self._error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if self._finalize_on_iteration:
                self._finalize()

    def _on_chunk(self, chunk: Any) -> None:
        if getattr(self._instr, "_disabled", False):
            return
        try:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                # Final usage chunk (include_usage). choices is typically [].
                self._usage = usage
            choices = getattr(chunk, "choices", None) or []
            if choices:
                first = choices[0]
                delta = getattr(first, "delta", None)
                if delta is not None:
                    txt = getattr(delta, "content", None)
                    if txt and self._instr.client.capture_content:
                        self._text_chunks.append(txt)
                fr = getattr(first, "finish_reason", None)
                if fr:
                    self._finish_reason = normalize_finish_reason(fr)
        except Exception:
            pass

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if getattr(self._instr, "_disabled", False):
            self._text_chunks.clear()
            return

        raised = self._error is not None
        should_persist, is_error = decide_persist(raised, self._instr._should_sample())
        if not should_persist:
            return

        self._trace.ended_at = datetime.now(timezone.utc)
        self._trace.latency_ms = (time.perf_counter() - self._t0) * 1000.0
        if is_error:
            self._trace.error = self._error
            self._instr._safe_persist(self._trace)
            return
        try:
            if self._usage is not None:
                in_tok = getattr(self._usage, "prompt_tokens", None)
                out_tok = getattr(self._usage, "completion_tokens", None)
                if in_tok is not None:
                    self._trace.input_tokens = in_tok
                if out_tok is not None:
                    self._trace.output_tokens = out_tok
            if self._finish_reason is not None:
                self._trace.finish_reason = self._finish_reason
            self._trace.cost_usd = compute_cost_usd(
                self._trace.response_model or self._trace.request_model,
                self._trace.input_tokens,
                self._trace.output_tokens,
            )
            if self._instr.client.capture_content:
                text = "".join(self._text_chunks)
                self._trace.response_redacted = redact(
                    text,
                    mode=self._instr.client.redaction_mode,  # type: ignore[arg-type]
                    secret=self._instr.client.redaction_secret,
                )
        except Exception:
            pass
        self._instr._safe_persist(self._trace)


class _AsyncStreamingWrapper(_StreamingWrapper):
    """Async variant — supports `async for` and `async with`."""

    def __aiter__(self):
        if self._iterator is None:
            self._iterator = self._async_gen()
        return self._iterator

    async def __anext__(self):
        return await self.__aiter__().__anext__()

    async def _async_gen(self):
        try:
            async for chunk in self._inner:
                self._on_chunk(chunk)
                yield chunk
        except asyncio.CancelledError as e:
            self._error = f"{type(e).__name__}: {e}"
            raise
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            raise
        finally:
            if self._finalize_on_iteration:
                self._finalize()

    async def __aenter__(self):
        if hasattr(self._inner, "__aenter__"):
            await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc is not None and self._error is None:
            self._error = f"{type(exc).__name__}: {exc}"
        suppressed = False
        if hasattr(self._inner, "__aexit__"):
            try:
                suppressed = bool(await self._inner.__aexit__(exc_type, exc, tb))
            except asyncio.CancelledError as inner_exc:
                if self._error is None:
                    self._error = f"{type(inner_exc).__name__}: {inner_exc}"
                raise
            except Exception as inner_exc:
                if self._error is None:
                    self._error = f"{type(inner_exc).__name__}: {inner_exc}"
                raise
            finally:
                self._finalize()
        else:
            self._finalize()
        return suppressed

    async def aclose(self) -> None:
        """Close the upstream async stream and finalize this trace exactly once."""
        try:
            inner_aclose = getattr(self._inner, "aclose", None)
            if inner_aclose is not None:
                await inner_aclose()
            else:
                inner_close = getattr(self._inner, "close", None)
                if inner_close is not None:
                    result = inner_close()
                    if inspect.isawaitable(result):
                        await result
        except asyncio.CancelledError as exc:
            if self._error is None:
                self._error = f"{type(exc).__name__}: {exc}"
            raise
        except Exception as exc:
            if self._error is None:
                self._error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._finalize()

    async def close(self) -> None:
        """Match OpenAI AsyncStream's public close surface."""
        await self.aclose()


class _ResponsesStreamingWrapper(_StreamingWrapper):
    """Accumulate OpenAI Responses events while preserving the SDK stream."""

    def __init__(self, inner: Any, trace: Trace, t0: float, instr: OpenAIInstrumentor) -> None:
        super().__init__(inner, trace, t0, instr)
        self._terminal_status: str | None = None
        self._content_order: list[tuple[str, Any, Any, Any]] = []
        self._content_values: dict[tuple[str, Any, Any, Any], str] = {}

    def _on_chunk(self, event: Any) -> None:
        if getattr(self._instr, "_disabled", False):
            return
        try:
            event_type = getattr(event, "type", "")
            if event_type in ("response.output_text.delta", "response.refusal.delta"):
                delta = getattr(event, "delta", None)
                if isinstance(delta, str) and self._instr.client.capture_content:
                    key = (
                        event_type.rsplit(".", 1)[0],
                        getattr(event, "item_id", None),
                        getattr(event, "output_index", None),
                        getattr(event, "content_index", None),
                    )
                    if key not in self._content_values:
                        self._content_order.append(key)
                        self._content_values[key] = ""
                    self._content_values[key] += delta
                return
            if event_type in ("response.output_text.done", "response.refusal.done"):
                if self._instr.client.capture_content:
                    key = (
                        event_type.rsplit(".", 1)[0],
                        getattr(event, "item_id", None),
                        getattr(event, "output_index", None),
                        getattr(event, "content_index", None),
                    )
                    value = getattr(
                        event,
                        "text" if event_type == "response.output_text.done" else "refusal",
                        None,
                    )
                    if isinstance(value, str):
                        if key not in self._content_values:
                            self._content_order.append(key)
                        # A done event carries the authoritative full value.
                        # Replacing accumulated deltas preserves resumed streams
                        # whose first observed delta is only a suffix.
                        self._content_values[key] = value
                return
            if event_type in (
                "response.completed",
                "response.incomplete",
                "response.failed",
            ):
                self._terminal_status = event_type.rsplit(".", 1)[-1]
                response = getattr(event, "response", None)
                if response is not None:
                    # Extract/redact the terminal values immediately.  Never
                    # retain the provider Response (and its raw content) on the
                    # wrapper after yielding the event to the application.
                    self._instr._fill_responses_output(self._trace, response)
                else:
                    self._trace.ended_at = datetime.now(timezone.utc)
                    if event_type == "response.failed":
                        self._error = "OpenAI response failed"
                return
            if event_type == "error":
                self._error = _response_error_text(event) or "OpenAI stream error"
                return
            response = getattr(event, "response", None)
            model = getattr(response, "model", None)
            if isinstance(model, str) and model:
                self._trace.response_model = model
        except Exception:
            pass

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if getattr(self._instr, "_disabled", False):
            self._text_chunks.clear()
            self._content_order.clear()
            self._content_values.clear()
            return

        if self._terminal_status is None:
            self._trace.ended_at = datetime.now(timezone.utc)

        if self._error is not None and self._trace.error is None:
            self._trace.error = redact(
                self._error,
                mode=self._instr.client.redaction_mode,  # type: ignore[arg-type]
                secret=self._instr.client.redaction_secret,
            )

        raised = self._trace.error is not None
        should_persist, is_error = decide_persist(raised, self._instr._should_sample())
        if not should_persist:
            return

        self._trace.latency_ms = (time.perf_counter() - self._t0) * 1000.0
        completion = (
            "error"
            if is_error
            else ("complete" if self._terminal_status is not None else "partial")
        )
        self._trace.tags = {
            **self._trace.tags,
            "verdict.stream_completion": completion,
        }
        if self._instr.client.capture_content and self._terminal_status is None:
            self._trace.response_redacted = redact(
                "".join(self._content_values[key] for key in self._content_order),
                mode=self._instr.client.redaction_mode,  # type: ignore[arg-type]
                secret=self._instr.client.redaction_secret,
            )
        self._instr._safe_persist(self._trace)


class _AsyncResponsesStreamingWrapper(_ResponsesStreamingWrapper, _AsyncStreamingWrapper):
    """Async Responses stream using the shared cancellation-safe iterator."""


def _mark_stream_error(stream: Any, exc: BaseException) -> None:
    if stream is None:
        return
    setter = getattr(stream, "_set_error", None)
    if callable(setter):
        setter(exc)
    elif getattr(stream, "_error", None) is None:
        stream._error = f"{type(exc).__name__}: {exc}"


def _discard_stream_capture(stream: Any) -> None:
    """Release Verdict-owned state when its instrumentor is no longer active."""
    if stream is None:
        return
    stream._finalized = True
    for name in ("_text_chunks", "_content_order", "_content_values"):
        value = getattr(stream, name, None)
        clear = getattr(value, "clear", None)
        if callable(clear):
            clear()


class _OpenAIStreamManagerWrapper:
    """Finalize the nested raw request when an SDK helper context exits."""

    def __init__(self, inner: Any, instr: OpenAIInstrumentor) -> None:
        self._inner = inner
        self._instr = instr
        self._entries: list[tuple[Any, Any, bool]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __enter__(self):
        stream = self._inner.__enter__()
        active = not getattr(self._instr, "_disabled", False)
        raw_stream = getattr(stream, "_raw_stream", None) if active else None
        if active and hasattr(raw_stream, "_finalize_on_iteration"):
            raw_stream._finalize_on_iteration = False
        self._entries.append((stream, raw_stream, active))
        return stream

    def __exit__(self, exc_type, exc, tb):
        stream, raw_stream, active = self._entries.pop()
        instrumented = active and not getattr(self._instr, "_disabled", False)
        if exc is not None and instrumented:
            _mark_stream_error(raw_stream, exc)
        try:
            # The SDK manager itself retains only its most recent stream.
            # Close the stream belonging to this LIFO entry directly so nested
            # reuse cannot overwrite another request's finalization boundary.
            return stream.__exit__(exc_type, exc, tb)
        except Exception as inner_exc:
            if instrumented:
                _mark_stream_error(raw_stream, inner_exc)
            raise
        finally:
            if instrumented:
                finalize = getattr(raw_stream, "_finalize", None)
                if callable(finalize):
                    finalize()
            else:
                _discard_stream_capture(raw_stream)


class _AsyncOpenAIStreamManagerWrapper:
    """Async helper manager counterpart with cancellation preservation."""

    def __init__(self, inner: Any, instr: OpenAIInstrumentor) -> None:
        self._inner = inner
        self._instr = instr
        self._entries: list[tuple[Any, Any, bool]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def __aenter__(self):
        stream = await self._inner.__aenter__()
        active = not getattr(self._instr, "_disabled", False)
        raw_stream = getattr(stream, "_raw_stream", None) if active else None
        if active and hasattr(raw_stream, "_finalize_on_iteration"):
            raw_stream._finalize_on_iteration = False
        self._entries.append((stream, raw_stream, active))
        return stream

    async def __aexit__(self, exc_type, exc, tb):
        stream, raw_stream, active = self._entries.pop()
        instrumented = active and not getattr(self._instr, "_disabled", False)
        if exc is not None and instrumented:
            _mark_stream_error(raw_stream, exc)
        try:
            return await stream.__aexit__(exc_type, exc, tb)
        except asyncio.CancelledError as inner_exc:
            if instrumented:
                _mark_stream_error(raw_stream, inner_exc)
            raise
        except Exception as inner_exc:
            if instrumented:
                _mark_stream_error(raw_stream, inner_exc)
            raise
        finally:
            if instrumented:
                finalize = getattr(raw_stream, "_finalize", None)
                if callable(finalize):
                    finalize()
            else:
                _discard_stream_capture(raw_stream)
