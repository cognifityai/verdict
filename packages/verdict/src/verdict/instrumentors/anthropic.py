"""Anthropic SDK auto-instrumentation.

Patches `anthropic.resources.messages.Messages.create` (and the async variant)
using `wrapt`. Captures a Trace per invocation, redacts content if enabled, and
hands the Trace to client.storage.

The wrapper records both normal responses and streaming responses while passing
provider objects through to user code with minimal behavioral change.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from verdict.instrumentors.base import (
    BaseInstrumentor,
    apply_routing_context,
    decide_persist,
    is_verdict_wrapt_wrapper,
    normalize_finish_reason,
)
from verdict.pricing import compute_cost_usd
from verdict.redaction import redact, redact_messages
from verdict.schema import Operation, Trace

if TYPE_CHECKING:
    pass

# Dedicated RNG so an app calling random.seed() can't perturb our sampling.
_rng = random.Random()


def _is_wrapped(mod: Any, cls_name: str, method: str) -> bool:
    """True only if ``mod.<cls_name>.<method>`` is Verdict's wrapt wrapper."""
    cls = getattr(mod, cls_name, None)
    if cls is None:
        return False
    bound = getattr(cls, method, None)
    return is_verdict_wrapt_wrapper(bound)


def _maybe_import_anthropic():
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


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

        import anthropic.resources.messages.messages as mod

        # Guard against double-wrapping: a re-init (or a second VerdictClient)
        # must not stack wrappers, or every call would be recorded twice.
        # Sync path
        if not _is_wrapped(mod, "Messages", "create"):
            wrapt.wrap_function_wrapper(
                "anthropic.resources.messages.messages",
                "Messages.create",
                self._wrap_create_sync,
            )
        # Async path
        if not _is_wrapped(mod, "AsyncMessages", "create"):
            wrapt.wrap_function_wrapper(
                "anthropic.resources.messages.messages",
                "AsyncMessages.create",
                self._wrap_create_async,
            )
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            import anthropic.resources.messages.messages as mod
        except ImportError:
            self._installed = False
            return
        for cls_name, method in [("Messages", "create"), ("AsyncMessages", "create")]:
            cls = getattr(mod, cls_name, None)
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
        trace = self._build_input_trace(kwargs)
        t0 = time.perf_counter()

        # Streaming path: wrap the iterator in a passthrough that collects
        # chunks for telemetry while yielding each one to the caller unchanged.
        # The wrapper persists at finalize time (success sampled, error always).
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
        trace = self._build_input_trace(kwargs)
        t0 = time.perf_counter()

        if kwargs.get("stream"):
            try:
                stream = await wrapped(*args, **kwargs)
            except Exception as e:
                trace.ended_at = datetime.now(timezone.utc)
                trace.latency_ms = (time.perf_counter() - t0) * 1000.0
                trace.error = f"{type(e).__name__}: {e}"
                self._safe_persist(trace)
                raise
            return _AsyncStreamingWrapper(stream, trace, t0, self)

        try:
            resp = await wrapped(*args, **kwargs)
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

    # -- helpers -----------------------------------------------------------

    def _build_input_trace(self, kwargs: dict[str, Any]) -> Trace:
        model = str(kwargs.get("model", ""))
        temperature = kwargs.get("temperature")
        max_tokens = kwargs.get("max_tokens")
        messages = kwargs.get("messages") or []

        trace = Trace(
            provider="anthropic",
            operation=Operation.CHAT,
            request_model=model,
            response_model=model,  # may be updated from response
            temperature=temperature,
            max_tokens=max_tokens,
        )
        apply_routing_context(self.client, trace)

        if self.client.capture_content:
            # Flatten the user message text(s) for storage
            try:
                joined = "\n".join(
                    (m.get("content", "") if isinstance(m.get("content", ""), str)
                     else _flatten_content(m.get("content", [])))
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
                messages, mode=self.client.redaction_mode, secret=self.client.redaction_secret,
            )
        return trace

    def _fill_output(self, trace: Trace, resp: Any) -> None:
        trace.ended_at = datetime.now(timezone.utc)
        # Anthropic Message has: id, model, role, content (list of blocks),
        # stop_reason, usage.input_tokens, usage.output_tokens
        try:
            trace.response_model = getattr(resp, "model", trace.request_model) or trace.request_model
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

    def _safe_persist(self, trace: Trace) -> None:
        try:
            self.client.storage.insert_trace(trace)
        except Exception:
            # Telemetry must not propagate exceptions
            pass


class _StreamingWrapper:
    """Pass-through iterator wrapper around an Anthropic streaming response.

    Yields each upstream event unchanged so the caller's streaming UX is
    preserved. Accumulates text + usage + stop_reason for the trace; finalizes
    the trace in a `finally` block so cancellation still produces a record.
    """

    def __init__(self, inner: Any, trace: Trace, t0: float, instr: "AnthropicInstrumentor") -> None:
        self._inner = inner
        self._trace = trace
        self._t0 = t0
        self._instr = instr
        self._text_chunks: list[str] = []
        self._usage: Any = None
        self._stop_reason: str | None = None
        self._finalized = False
        self._error: str | None = None

    def __iter__(self):
        try:
            for event in self._inner:
                self._on_event(event)
                yield event
        except Exception as e:
            # A stream that fails mid-iteration is an error, not a truncated
            # success. Record it (leaving finish_reason as None) before the
            # finally finalizes, then re-raise so the caller still sees it.
            self._error = f"{type(e).__name__}: {e}"
            raise
        finally:
            self._finalize()

    # Some Anthropic SDK versions expose .text_stream as a convenience iterator.
    # Forward attribute access to the wrapped object so we don't break that.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    # Streaming context-manager support: `with client.messages.stream(...) as s:`
    def __enter__(self):
        if hasattr(self._inner, "__enter__"):
            self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and self._error is None:
            # Block exited via an exception: record it as an error stream.
            self._error = f"{type(exc).__name__}: {exc}"
        if hasattr(self._inner, "__exit__"):
            try:
                self._inner.__exit__(exc_type, exc, tb)
            finally:
                self._finalize()
        else:
            self._finalize()
        return False

    def _on_event(self, event: Any) -> None:
        try:
            etype = getattr(event, "type", "")
            if etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                if delta is not None:
                    txt = getattr(delta, "text", None)
                    if txt:
                        self._text_chunks.append(txt)
            elif etype == "message_start":
                msg = getattr(event, "message", None)
                if msg is not None:
                    u = getattr(msg, "usage", None)
                    if u is not None:
                        self._usage = u
            elif etype == "message_delta":
                # Final usage often arrives here for output_tokens
                u = getattr(event, "usage", None)
                if u is not None:
                    self._usage = u
                d = getattr(event, "delta", None)
                if d is not None:
                    sr = getattr(d, "stop_reason", None)
                    if sr:
                        self._stop_reason = normalize_finish_reason(sr)
        except Exception:
            pass

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True

        raised = self._error is not None
        should_persist, is_error = decide_persist(raised, self._instr._should_sample())
        if not should_persist:
            # Sampled-out success: nothing to record.
            return

        self._trace.ended_at = datetime.now(timezone.utc)
        self._trace.latency_ms = (time.perf_counter() - self._t0) * 1000.0
        if is_error:
            # Failed stream: record the error, leave finish_reason as None.
            self._trace.error = self._error
            self._instr._safe_persist(self._trace)
            return
        try:
            if self._usage is not None:
                in_tok = getattr(self._usage, "input_tokens", None)
                out_tok = getattr(self._usage, "output_tokens", None)
                if in_tok is not None:
                    self._trace.input_tokens = in_tok
                if out_tok is not None:
                    self._trace.output_tokens = out_tok
            if self._stop_reason is not None:
                self._trace.finish_reason = self._stop_reason
            self._trace.cost_usd = compute_cost_usd(
                self._trace.response_model or self._trace.request_model,
                self._trace.input_tokens,
                self._trace.output_tokens,
            )
            if self._instr.client.capture_content and self._text_chunks:
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
        return self._async_gen()

    async def _async_gen(self):
        try:
            async for event in self._inner:
                self._on_event(event)
                yield event
        except Exception as e:
            # Failed mid-stream: record as an error, not a truncated success.
            self._error = f"{type(e).__name__}: {e}"
            raise
        finally:
            self._finalize()

    async def __aenter__(self):
        if hasattr(self._inner, "__aenter__"):
            await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc is not None and self._error is None:
            # Block exited via an exception: record it as an error stream.
            self._error = f"{type(exc).__name__}: {exc}"
        if hasattr(self._inner, "__aexit__"):
            try:
                await self._inner.__aexit__(exc_type, exc, tb)
            finally:
                self._finalize()
        else:
            self._finalize()
        return False


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
