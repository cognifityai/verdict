"""OpenAI SDK auto-instrumentation. Mirrors AnthropicInstrumentor in structure."""

from __future__ import annotations

import asyncio
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


class OpenAIInstrumentor(BaseInstrumentor):
    name = "openai"

    def available(self) -> bool:
        return _maybe_import_openai()

    def install(self) -> None:
        if self._installed:
            return
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
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            import openai.resources.chat.completions as mod
        except ImportError:
            self._installed = False
            return
        for cls_name, method in [("Completions", "create"), ("AsyncCompletions", "create")]:
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
                    _flatten_content(m.get("content", ""))
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
        try:
            trace.response_model = getattr(resp, "model", trace.request_model) or trace.request_model
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
                trace.finish_reason = normalize_finish_reason(
                    getattr(first, "finish_reason", None)
                )
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

    def __iter__(self):
        try:
            for chunk in self._inner:
                self._on_chunk(chunk)
                yield chunk
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            raise
        finally:
            self._finalize()

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
            self._finalize()

    def _on_chunk(self, chunk: Any) -> None:
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
                    if txt:
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
                    inner_close()
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
