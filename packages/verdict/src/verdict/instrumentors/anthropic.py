"""Anthropic SDK auto-instrumentation.

Patches `anthropic.resources.messages.Messages.create` and `Messages.stream`
(plus their async variants) using `wrapt`. Captures a Trace per provider
request, redacts content if enabled, and hands the Trace to client.storage.

The wrapper records both normal responses and streaming responses while passing
provider objects through to user code with minimal behavioral change.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from verdict.instrumentors.base import (
    BaseInstrumentor,
    apply_routing_context,
    decide_persist,
    is_verdict_wrapt_wrapper,
    normalize_finish_reason,
    should_sample_success,
)
from verdict.pricing import compute_cost_usd
from verdict.redaction import redact, redact_messages
from verdict.schema import (
    Operation,
    Trace,
    normalize_optional_float,
    normalize_optional_integer,
)

if TYPE_CHECKING:
    pass

# Dedicated RNG so an app calling random.seed() can't perturb our sampling.


def _is_wrapped(mod: Any, cls_name: str, method: str) -> bool:
    """True only if ``mod.<cls_name>.<method>`` is Verdict's wrapt wrapper."""
    cls = getattr(mod, cls_name, None)
    if cls is None:
        return False
    bound = getattr(cls, method, None)
    return is_verdict_wrapt_wrapper(bound)


def _has_method(mod: Any, cls_name: str, method: str) -> bool:
    """Return whether this installed SDK exposes the optional method."""
    cls = getattr(mod, cls_name, None)
    return cls is not None and callable(getattr(cls, method, None))


def _message_resource_module() -> tuple[Any, str]:
    """Resolve the Anthropic resource layout across the declared SDK range."""
    paths = (
        "anthropic.resources.messages.messages",
        "anthropic.resources.messages",
    )
    for path in paths:
        try:
            return importlib.import_module(path), path
        except ModuleNotFoundError as exc:
            if exc.name != path:
                raise
            continue
    raise ImportError("Anthropic Messages resources are unavailable")


def _maybe_import_anthropic():
    try:
        import anthropic  # noqa: F401

        return True
    except ImportError:
        return False


class _CapturedMessages:
    """Replay provider-consumed messages without driving the source early."""

    def __init__(self, source: Any) -> None:
        self._source = source
        self._source_iterator: Any = None
        self._cache: list[Any] = []
        self._complete = False
        self._error: BaseException | None = None

    def __iter__(self):
        return self._iterate()

    def _iterate(self):
        index = 0
        while True:
            if index < len(self._cache):
                item = self._cache[index]
                index += 1
                yield item
                continue
            if self._error is not None:
                raise self._error
            if self._complete:
                return
            if self._source_iterator is None:
                try:
                    self._source_iterator = iter(self._source)
                except BaseException as exc:
                    self._error = exc
                    raise
            try:
                item = next(self._source_iterator)
            except StopIteration:
                self._complete = True
                return
            except BaseException as exc:
                self._error = exc
                raise
            self._cache.append(item)

    def snapshot(self) -> list[Any]:
        """Return only values the provider has already requested."""
        return list(self._cache)


class AnthropicInstrumentor(BaseInstrumentor):
    name = "anthropic"

    def available(self) -> bool:
        return _maybe_import_anthropic()

    def install(self) -> None:
        if self._installed:
            return
        try:
            import wrapt
        except ImportError as e:
            raise RuntimeError("wrapt is required") from e

        mod, module_path = _message_resource_module()

        # Guard against double-wrapping: a re-init (or a second VerdictClient)
        # must not stack wrappers, or every call would be recorded twice.
        methods = (
            ("Messages", "create", self._wrap_create_sync),
            ("AsyncMessages", "create", self._wrap_create_async),
            ("Messages", "stream", self._wrap_stream_sync),
            ("AsyncMessages", "stream", self._wrap_stream_async),
        )
        for cls_name, method, wrapper in methods:
            # ``messages.stream`` is feature-detected for the declared
            # anthropic>=0.30 range instead of making installation all-or-none.
            if _has_method(mod, cls_name, method) and not _is_wrapped(mod, cls_name, method):
                wrapt.wrap_function_wrapper(
                    module_path,
                    f"{cls_name}.{method}",
                    wrapper,
                )
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            mod, _module_path = _message_resource_module()
        except ImportError:
            self._installed = False
            return
        for cls_name, method in (
            ("Messages", "create"),
            ("AsyncMessages", "create"),
            ("Messages", "stream"),
            ("AsyncMessages", "stream"),
        ):
            cls = getattr(mod, cls_name, None)
            if cls is not None:
                bound = getattr(cls, method, None)
                if is_verdict_wrapt_wrapper(bound, owner=self):
                    setattr(cls, method, bound.__wrapped__)
        self._installed = False

    # -- wrappers ----------------------------------------------------------

    def _should_sample(self, trace: Trace) -> bool:
        return should_sample_success(self.client, trace)

    def _wrap_create_sync(self, wrapped, instance, args, kwargs):
        trace_kwargs, call_kwargs = self._split_capture_kwargs(kwargs)
        trace = self._build_input_trace(trace_kwargs)
        t0 = time.perf_counter()

        # Streaming path: wrap the iterator in a passthrough that collects
        # chunks for telemetry while yielding each one to the caller unchanged.
        # The wrapper persists at finalize time (success sampled, error always).
        if kwargs.get("stream"):
            try:
                stream = wrapped(*args, **call_kwargs)
            except Exception as e:
                self._fill_input_trace(trace, trace_kwargs)
                self._persist_error(trace, t0, e, stream_completion="error")
                raise
            self._fill_input_trace(trace, trace_kwargs)
            return _StreamingWrapper(stream, trace, t0, self)

        # Always call through; capture every error, sample successes.
        try:
            resp = wrapped(*args, **call_kwargs)
        except Exception as e:
            self._fill_input_trace(trace, trace_kwargs)
            self._persist_error(trace, t0, e)
            raise
        should_persist, _is_error = decide_persist(False, self._should_sample(trace))
        if should_persist:
            self._fill_input_trace(trace, trace_kwargs)
            self._fill_output(trace, resp)
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._safe_persist(trace)
        return resp

    async def _wrap_create_async(self, wrapped, instance, args, kwargs):
        trace_kwargs, call_kwargs = self._split_capture_kwargs(kwargs)
        trace = self._build_input_trace(trace_kwargs)
        t0 = time.perf_counter()

        if kwargs.get("stream"):
            try:
                stream = await wrapped(*args, **call_kwargs)
            except asyncio.CancelledError as e:
                self._fill_input_trace(trace, trace_kwargs)
                self._persist_error(trace, t0, e, stream_completion="error")
                raise
            except Exception as e:
                self._fill_input_trace(trace, trace_kwargs)
                self._persist_error(trace, t0, e, stream_completion="error")
                raise
            self._fill_input_trace(trace, trace_kwargs)
            return _AsyncStreamingWrapper(stream, trace, t0, self)

        try:
            resp = await wrapped(*args, **call_kwargs)
        except asyncio.CancelledError as e:
            self._fill_input_trace(trace, trace_kwargs)
            self._persist_error(trace, t0, e)
            raise
        except Exception as e:
            self._fill_input_trace(trace, trace_kwargs)
            self._persist_error(trace, t0, e)
            raise
        should_persist, _is_error = decide_persist(False, self._should_sample(trace))
        if should_persist:
            self._fill_input_trace(trace, trace_kwargs)
            self._fill_output(trace, resp)
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._safe_persist(trace)
        return resp

    def _wrap_stream_sync(self, wrapped, instance, args, kwargs):
        """Wrap the lazy manager returned by ``Messages.stream``."""
        trace_kwargs, call_kwargs = self._split_capture_kwargs(kwargs)
        t0 = time.perf_counter()
        try:
            manager = wrapped(*args, **call_kwargs)
        except Exception as exc:
            trace = self._build_input_trace(trace_kwargs)
            self._persist_error(trace, t0, exc, stream_completion="error")
            raise
        return _MessageStreamManagerWrapper(manager, trace_kwargs, self)

    def _wrap_stream_async(self, wrapped, instance, args, kwargs):
        """Wrap ``AsyncMessages.stream`` (the method itself is synchronous)."""
        trace_kwargs, call_kwargs = self._split_capture_kwargs(kwargs)
        t0 = time.perf_counter()
        try:
            manager = wrapped(*args, **call_kwargs)
        except Exception as exc:
            trace = self._build_input_trace(trace_kwargs)
            self._persist_error(trace, t0, exc, stream_completion="error")
            raise
        return _AsyncMessageStreamManagerWrapper(manager, trace_kwargs, self)

    # -- helpers -----------------------------------------------------------

    def _persist_error(
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

    def _split_capture_kwargs(
        self,
        kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Observe provider iteration lazily and retain a replayable snapshot."""
        if not self.client.capture_content:
            return kwargs, kwargs
        messages = kwargs.get("messages")
        if messages is None:
            return kwargs, kwargs
        captured = _CapturedMessages(messages)
        trace_kwargs = {**kwargs, "messages": captured}
        call_kwargs = {**kwargs, "messages": captured}
        return trace_kwargs, call_kwargs

    def _build_input_trace(self, kwargs: dict[str, Any]) -> Trace:
        model = str(kwargs.get("model", ""))
        temperature = normalize_optional_float(kwargs.get("temperature"))
        max_tokens = normalize_optional_integer(kwargs.get("max_tokens"))
        trace = Trace(
            provider="anthropic",
            operation=Operation.CHAT,
            request_model=model,
            response_model=model,  # may be updated from response
            temperature=temperature,
            max_tokens=max_tokens,
        )
        apply_routing_context(self.client, trace)

        self._fill_input_trace(trace, kwargs)
        return trace

    def _fill_input_trace(self, trace: Trace, kwargs: dict[str, Any]) -> None:
        """Capture the provider-consumed message prefix without iterating it."""
        if not self.client.capture_content:
            return
        source = kwargs.get("messages")
        if isinstance(source, _CapturedMessages):
            messages = source.snapshot()
        else:
            if source is None:
                source = []
            try:
                messages = list(source)
            except Exception:
                messages = []
        try:
            joined = "\n".join(
                (
                    m.get("content", "")
                    if isinstance(m.get("content", ""), str)
                    else _flatten_content(m.get("content", []))
                )
                for m in messages
                if isinstance(m, dict)
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

    def _fill_output(self, trace: Trace, resp: Any) -> None:
        trace.ended_at = datetime.now(timezone.utc)
        # Anthropic Message has: id, model, role, content (list of blocks),
        # stop_reason, usage.input_tokens, usage.output_tokens
        try:
            trace.response_model = (
                getattr(resp, "model", trace.request_model) or trace.request_model
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                trace.input_tokens = getattr(usage, "input_tokens", None)
                trace.output_tokens = getattr(usage, "output_tokens", None)
            stop = getattr(resp, "stop_reason", None)
            if stop is not None:
                trace.finish_reason = normalize_finish_reason(stop)
            trace.cost_usd = compute_cost_usd(
                trace.response_model or trace.request_model,
                trace.input_tokens,
                trace.output_tokens,
            )
            if self.client.capture_content:
                blocks = getattr(resp, "content", None) or []
                text = _flatten_content(blocks)
                trace.response_redacted = redact(
                    text,
                    mode=self.client.redaction_mode,  # type: ignore[arg-type]
                    secret=self.client.redaction_secret,
                )
        except Exception:
            # Never let telemetry break the user's request path
            pass


class _MessageStreamManagerWrapper:
    """Start capture when a synchronous lazy stream manager is entered."""

    def __init__(
        self,
        inner: Any,
        trace_kwargs: dict[str, Any],
        instr: AnthropicInstrumentor,
    ) -> None:
        self._inner = inner
        self._trace_kwargs = trace_kwargs
        self._instr = instr
        self._stream: _MessageStreamingWrapper | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __enter__(self):
        trace = self._instr._build_input_trace(self._trace_kwargs)
        trace.started_at = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        try:
            stream = self._inner.__enter__()
        except Exception as exc:
            self._instr._fill_input_trace(trace, self._trace_kwargs)
            self._instr._persist_error(
                trace,
                t0,
                exc,
                stream_completion="error",
            )
            raise
        self._instr._fill_input_trace(trace, self._trace_kwargs)
        self._stream = _MessageStreamingWrapper(
            stream,
            trace,
            t0,
            self._instr,
            finalize_on_iteration=False,
        )
        return self._stream

    def __exit__(self, exc_type, exc, tb):
        stream = self._stream
        if stream is None:
            return self._inner.__exit__(exc_type, exc, tb)
        if exc is not None:
            stream._set_error(exc)
        try:
            return self._inner.__exit__(exc_type, exc, tb)
        except Exception as inner_exc:
            stream._set_error(inner_exc)
            raise
        finally:
            stream._finalize()
            self._stream = None


class _AsyncMessageStreamManagerWrapper:
    """Start capture when an asynchronous lazy stream manager is entered."""

    def __init__(
        self,
        inner: Any,
        trace_kwargs: dict[str, Any],
        instr: AnthropicInstrumentor,
    ) -> None:
        self._inner = inner
        self._trace_kwargs = trace_kwargs
        self._instr = instr
        self._stream: _AsyncMessageStreamingWrapper | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def __aenter__(self):
        trace = self._instr._build_input_trace(self._trace_kwargs)
        trace.started_at = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        try:
            stream = await self._inner.__aenter__()
        except asyncio.CancelledError as exc:
            self._instr._fill_input_trace(trace, self._trace_kwargs)
            self._instr._persist_error(
                trace,
                t0,
                exc,
                stream_completion="error",
            )
            raise
        except Exception as exc:
            self._instr._fill_input_trace(trace, self._trace_kwargs)
            self._instr._persist_error(
                trace,
                t0,
                exc,
                stream_completion="error",
            )
            raise
        self._instr._fill_input_trace(trace, self._trace_kwargs)
        self._stream = _AsyncMessageStreamingWrapper(
            stream,
            trace,
            t0,
            self._instr,
            finalize_on_iteration=False,
        )
        return self._stream

    async def __aexit__(self, exc_type, exc, tb):
        stream = self._stream
        if stream is None:
            return await self._inner.__aexit__(exc_type, exc, tb)
        if exc is not None:
            stream._set_error(exc)
        try:
            return await self._inner.__aexit__(exc_type, exc, tb)
        except asyncio.CancelledError as inner_exc:
            stream._set_error(inner_exc)
            raise
        except Exception as inner_exc:
            stream._set_error(inner_exc)
            raise
        finally:
            stream._finalize()
            self._stream = None


class _StreamingWrapper:
    """Pass-through iterator wrapper around an Anthropic streaming response.

    Yields each upstream event unchanged so the caller's streaming UX is
    preserved. Normal exhaustion, iteration failure, explicit close, and context
    exit finalize deterministically. Async cancellation is recorded as an error.
    A dropped, never-iterated stream is not a supported finalization boundary.
    """

    def __init__(
        self,
        inner: Any,
        trace: Trace,
        t0: float,
        instr: AnthropicInstrumentor,
        *,
        finalize_on_iteration: bool = True,
    ) -> None:
        self._inner = inner
        self._trace = trace
        self._t0 = t0
        self._instr = instr
        self._finalize_on_iteration = finalize_on_iteration
        self._text_chunks: list[str] = []
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._stop_reason: str | None = None
        self._saw_message_stop = False
        self._finalized = False
        self._error: str | None = None
        self._iterator: Any = None
        self._text_stream_iterator: Any = None

    def __iter__(self):
        try:
            for event in self._inner:
                self._on_event(event)
                yield event
        except Exception as e:
            # A stream that fails mid-iteration is an error, not a truncated
            # success. Record it (leaving finish_reason as None) before the
            # finally finalizes, then re-raise so the caller still sees it.
            self._set_error(e)
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

    def _set_error(self, exc: BaseException) -> None:
        if self._error is None:
            self._error = f"{type(exc).__name__}: {exc}"

    # Streaming context-manager support: `with client.messages.stream(...) as s:`
    def __enter__(self):
        if hasattr(self._inner, "__enter__"):
            self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and self._error is None:
            # Block exited via an exception: record it as an error stream.
            self._set_error(exc)
        suppressed = False
        if hasattr(self._inner, "__exit__"):
            try:
                suppressed = bool(self._inner.__exit__(exc_type, exc, tb))
            except Exception as inner_exc:
                self._set_error(inner_exc)
                raise
            finally:
                self._finalize()
        else:
            self._finalize()
        return suppressed

    def close(self) -> None:
        """Close upstream; a helper manager finalizes after its own cleanup."""
        try:
            inner_close = getattr(self._inner, "close", None)
            if inner_close is not None:
                inner_close()
        except Exception as exc:
            self._set_error(exc)
            raise
        finally:
            if self._finalize_on_iteration:
                self._finalize()

    def _on_event(self, event: Any) -> None:
        try:
            etype = getattr(event, "type", "")
            if etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                if delta is not None:
                    txt = getattr(delta, "text", None)
                    if txt and self._instr.client.capture_content:
                        self._text_chunks.append(txt)
            elif etype == "message_start":
                msg = getattr(event, "message", None)
                if msg is not None:
                    model = getattr(msg, "model", None)
                    if model:
                        self._trace.response_model = str(model)
                    u = getattr(msg, "usage", None)
                    if u is not None:
                        self._merge_usage(u)
            elif etype == "message_delta":
                u = getattr(event, "usage", None)
                if u is not None:
                    self._merge_usage(u)
                d = getattr(event, "delta", None)
                if d is not None:
                    sr = getattr(d, "stop_reason", None)
                    if sr:
                        self._stop_reason = normalize_finish_reason(sr)
            elif etype == "message_stop":
                self._saw_message_stop = True
        except Exception:
            pass

    def _merge_usage(self, usage: Any) -> None:
        input_tokens = normalize_optional_integer(getattr(usage, "input_tokens", None))
        output_tokens = normalize_optional_integer(getattr(usage, "output_tokens", None))
        if input_tokens is not None:
            self._input_tokens = input_tokens
        if output_tokens is not None:
            self._output_tokens = output_tokens

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True

        raised = self._error is not None
        should_persist, is_error = decide_persist(raised, self._instr._should_sample(self._trace))
        if not should_persist:
            # Sampled-out success: nothing to record.
            return

        self._trace.ended_at = datetime.now(timezone.utc)
        self._trace.latency_ms = (time.perf_counter() - self._t0) * 1000.0
        if is_error:
            # Failed stream: record the error, leave finish_reason as None.
            self._trace.error = self._error
            self._trace.tags = {
                **self._trace.tags,
                "verdict.stream_completion": "error",
            }
            self._instr._safe_persist(self._trace)
            return
        try:
            if self._input_tokens is not None:
                self._trace.input_tokens = self._input_tokens
            if self._output_tokens is not None:
                self._trace.output_tokens = self._output_tokens
            if self._stop_reason is not None:
                self._trace.finish_reason = self._stop_reason
            self._trace.tags = {
                **self._trace.tags,
                "verdict.stream_completion": ("complete" if self._saw_message_stop else "partial"),
            }
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


class _MessageStreamingWrapper(_StreamingWrapper):
    """Anthropic helper lenses that must consume the instrumented iterator."""

    @property
    def text_stream(self):
        if self._text_stream_iterator is None:
            self._text_stream_iterator = self._stream_text()
        return self._text_stream_iterator

    def _stream_text(self):
        for event in self:
            delta = getattr(event, "delta", None)
            if (
                getattr(event, "type", "") == "content_block_delta"
                and getattr(delta, "type", "") == "text_delta"
            ):
                yield getattr(delta, "text", "")

    def until_done(self) -> None:
        for _ in self:
            pass

    def get_final_message(self):
        self.until_done()
        return self._inner.get_final_message()

    def get_final_text(self) -> str:
        self.until_done()
        return self._inner.get_final_text()


class _AsyncStreamingWrapper(_StreamingWrapper):
    """Async variant — supports `async for` and `async with`."""

    def __aiter__(self):
        return self._async_gen()

    async def __anext__(self):
        async_iterator = getattr(self, "_async_iterator", None)
        if async_iterator is None:
            async_iterator = self._async_gen()
            self._async_iterator = async_iterator
        return await anext(async_iterator)

    async def _async_gen(self):
        try:
            async for event in self._inner:
                self._on_event(event)
                yield event
        except asyncio.CancelledError as e:
            self._set_error(e)
            raise
        except Exception as e:
            # Failed mid-stream: record as an error, not a truncated success.
            self._set_error(e)
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
            # Block exited via an exception: record it as an error stream.
            self._set_error(exc)
        suppressed = False
        if hasattr(self._inner, "__aexit__"):
            try:
                suppressed = bool(await self._inner.__aexit__(exc_type, exc, tb))
            except asyncio.CancelledError as inner_exc:
                self._set_error(inner_exc)
                raise
            except Exception as inner_exc:
                self._set_error(inner_exc)
                raise
            finally:
                self._finalize()
        else:
            self._finalize()
        return suppressed

    async def close(self) -> None:
        """Close upstream; a helper manager finalizes after its own cleanup."""
        try:
            inner_aclose = getattr(self._inner, "aclose", None)
            if inner_aclose is not None:
                result = inner_aclose()
            else:
                inner_close = getattr(self._inner, "close", None)
                result = inner_close() if inner_close is not None else None
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError as exc:
            self._set_error(exc)
            raise
        except Exception as exc:
            self._set_error(exc)
            raise
        finally:
            if self._finalize_on_iteration:
                self._finalize()

    async def aclose(self) -> None:
        """Compatibility alias used by raw async stream variants."""
        await self.close()


class _AsyncMessageStreamingWrapper(_AsyncStreamingWrapper):
    """Async Anthropic helper lenses routed through the event accumulator."""

    @property
    def text_stream(self):
        if self._text_stream_iterator is None:
            self._text_stream_iterator = self._stream_text()
        return self._text_stream_iterator

    async def _stream_text(self):
        async for event in self:
            delta = getattr(event, "delta", None)
            if (
                getattr(event, "type", "") == "content_block_delta"
                and getattr(delta, "type", "") == "text_delta"
            ):
                yield getattr(delta, "text", "")

    async def until_done(self) -> None:
        async for _ in self:
            pass

    async def get_final_message(self):
        await self.until_done()
        return await self._inner.get_final_message()

    async def get_final_text(self) -> str:
        await self.until_done()
        return await self._inner.get_final_text()


def _flatten_content(content: Any) -> str:
    """Reduce a list of Anthropic content blocks (or a string) to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for block in content:
        if isinstance(block, str):
            out.append(block)
            continue
        text = getattr(block, "text", None)
        if text:
            out.append(text)
            continue
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text", ""))
    return "\n".join(out)
