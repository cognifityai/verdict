"""Google / Gemini SDK auto-instrumentation.

Targets the modern `google-genai` SDK (the one published as `google-genai` on
PyPI, which exposes `from google import genai; client = genai.Client(...)`).

The older `google-generativeai` package has a different shape; we cover that
with a second wrap site below.

Modern-SDK streaming note: the modern `google-genai` SDK streams via a SEPARATE
method — ``client.models.generate_content_stream(...)`` (and the async
``AsyncModels.generate_content_stream``) — NOT via a ``stream=True`` kwarg on
``generate_content``. So modern-SDK streaming is captured by wrapping
``generate_content_stream`` directly (see below), reusing the same
``_StreamingWrapper`` / ``_AsyncStreamingWrapper`` accumulation path as the
legacy ``stream=True`` branch. The legacy `google-generativeai` SDK still
streams via ``generate_content(stream=True)``, handled in that branch.

Mirrors AnthropicInstrumentor in structure for consistency.
"""

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
    persist_trace,
)
from verdict.pricing import compute_cost_usd
from verdict.redaction import redact, redact_messages, sanitize_trace
from verdict.schema import Operation, Trace

# Dedicated RNG so an app calling random.seed() can't perturb our sampling.
_rng = random.Random()


def _is_wrapped(cls: Any, method: str) -> bool:
    """True only if ``cls.<method>`` is Verdict's wrapt wrapper."""
    if cls is None:
        return False
    bound = getattr(cls, method, None)
    return is_verdict_wrapt_wrapper(bound)


def _has_google_genai() -> bool:
    try:
        import google.genai  # noqa: F401
        return True
    except ImportError:
        return False


def _has_google_generativeai() -> bool:
    try:
        import google.generativeai  # noqa: F401
        return True
    except ImportError:
        return False


class GoogleInstrumentor(BaseInstrumentor):
    """Auto-instrument Google Gemini SDKs (both `google-genai` and
    `google-generativeai`). Becomes a no-op if neither is installed."""

    name = "google"

    def available(self) -> bool:
        return _has_google_genai() or _has_google_generativeai()

    def install(self) -> None:
        if self._installed:
            return
        try:
            import wrapt
        except ImportError as e:
            raise RuntimeError("wrapt is required") from e

        # Modern google-genai SDK: client.models.generate_content(...)
        # Guard against double-wrapping on re-init so calls aren't recorded twice.
        if _has_google_genai():
            try:
                import google.genai.models as gmod
                if not _is_wrapped(getattr(gmod, "Models", None), "generate_content"):
                    wrapt.wrap_function_wrapper(
                        "google.genai.models",
                        "Models.generate_content",
                        self._wrap_genai_generate,
                    )
            except Exception:
                pass  # Defensive: SDK internals can change
            try:
                import google.genai.models as gmod
                if not _is_wrapped(getattr(gmod, "AsyncModels", None), "generate_content"):
                    wrapt.wrap_function_wrapper(
                        "google.genai.models",
                        "AsyncModels.generate_content",
                        self._wrap_genai_generate_async,
                    )
            except Exception:
                pass
            # Modern google-genai streams via a SEPARATE method:
            # client.models.generate_content_stream(...) (sync) and
            # AsyncModels.generate_content_stream(...) (async). generate_content
            # has no `stream` kwarg in this SDK, so streaming would otherwise be
            # missed. Wrap the stream methods too, reusing the same
            # _StreamingWrapper / _AsyncStreamingWrapper accumulation path.
            #
            # Keep this covered by scripts/live_capture_check.py; provider
            # stream chunk shapes can drift across SDK releases.
            try:
                import google.genai.models as gmod
                if not _is_wrapped(getattr(gmod, "Models", None), "generate_content_stream"):
                    wrapt.wrap_function_wrapper(
                        "google.genai.models",
                        "Models.generate_content_stream",
                        self._wrap_genai_generate_stream,
                    )
            except Exception:
                pass  # Defensive: SDK internals can change
            try:
                import google.genai.models as gmod
                if not _is_wrapped(getattr(gmod, "AsyncModels", None), "generate_content_stream"):
                    wrapt.wrap_function_wrapper(
                        "google.genai.models",
                        "AsyncModels.generate_content_stream",
                        self._wrap_genai_generate_stream_async,
                    )
            except Exception:
                pass

        # Legacy google-generativeai SDK: GenerativeModel.generate_content(...)
        if _has_google_generativeai():
            try:
                import google.generativeai.generative_models as lmod
                if not _is_wrapped(getattr(lmod, "GenerativeModel", None), "generate_content"):
                    wrapt.wrap_function_wrapper(
                        "google.generativeai.generative_models",
                        "GenerativeModel.generate_content",
                        self._wrap_legacy_generate,
                    )
            except Exception:
                pass

        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        # Best-effort unwrap; if the SDK isn't importable we silently skip.
        try:
            import google.genai.models as mod
            for cls_name in ["Models", "AsyncModels"]:
                cls = getattr(mod, cls_name, None)
                if cls is not None:
                    # Unwrap both the non-stream and the modern streaming method.
                    for method in ["generate_content", "generate_content_stream"]:
                        bound = getattr(cls, method, None)
                        if is_verdict_wrapt_wrapper(bound, owner=self):
                            setattr(cls, method, bound.__wrapped__)
        except ImportError:
            pass
        try:
            import google.generativeai.generative_models as mod
            cls = getattr(mod, "GenerativeModel", None)
            if cls is not None:
                bound = getattr(cls, "generate_content", None)
                if is_verdict_wrapt_wrapper(bound, owner=self):
                    cls.generate_content = bound.__wrapped__
        except ImportError:
            pass
        self._installed = False

    # -- wrappers ---------------------------------------------------------

    def _should_sample(self) -> bool:
        rate = self.client.sample_rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return _rng.random() < rate

    def _wrap_genai_generate(self, wrapped, instance, args, kwargs):
        trace = self._build_input_trace(args, kwargs, sdk="google-genai")
        t0 = time.perf_counter()

        # Streaming path: wrap the response generator so we accumulate text +
        # usage + finish_reason and finalize in a `finally`. Mirrors Anthropic's
        # _StreamingWrapper (cancellation still records; mid-stream raise is an
        # error, not a truncated success).
        if kwargs.get("stream"):
            try:
                stream = wrapped(*args, **kwargs)
            except Exception as e:
                self._record_error(trace, t0, e)
                raise
            return _StreamingWrapper(stream, trace, t0, self)

        # Always call through; capture every error, sample successes.
        try:
            resp = wrapped(*args, **kwargs)
        except Exception as e:
            self._record_error(trace, t0, e)
            raise
        should_persist, _is_error = decide_persist(False, self._should_sample())
        if should_persist:
            self._fill_output(trace, resp)
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._safe_persist(trace)
        return resp

    async def _wrap_genai_generate_async(self, wrapped, instance, args, kwargs):
        trace = self._build_input_trace(args, kwargs, sdk="google-genai")
        t0 = time.perf_counter()

        if kwargs.get("stream"):
            try:
                stream = await wrapped(*args, **kwargs)
            except Exception as e:
                self._record_error(trace, t0, e)
                raise
            return _AsyncStreamingWrapper(stream, trace, t0, self)

        try:
            resp = await wrapped(*args, **kwargs)
        except Exception as e:
            self._record_error(trace, t0, e)
            raise
        should_persist, _is_error = decide_persist(False, self._should_sample())
        if should_persist:
            self._fill_output(trace, resp)
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._safe_persist(trace)
        return resp

    def _wrap_genai_generate_stream(self, wrapped, instance, args, kwargs):
        # Modern google-genai: generate_content_stream(model=..., contents=...)
        # returns an iterator/generator of GenerateContentResponse chunks. This
        # is the streaming counterpart to generate_content; there is no `stream`
        # kwarg here — being called at all means the user wants a stream.
        #
        # Strictly parallel to the kwargs.get("stream") branch of
        # _wrap_genai_generate above. The returned chunks are the same
        # GenerateContentResponse shape _StreamingWrapper already accumulates
        # (usage_metadata / candidates / text), so no chunk-extraction changes.
        # _on_chunk keeps the last non-None usage because cumulative totals
        # usually arrive late in the stream.
        trace = self._build_input_trace(args, kwargs, sdk="google-genai")
        t0 = time.perf_counter()
        try:
            stream = wrapped(*args, **kwargs)
        except Exception as e:
            self._record_error(trace, t0, e)
            raise
        return _StreamingWrapper(stream, trace, t0, self)

    async def _wrap_genai_generate_stream_async(self, wrapped, instance, args, kwargs):
        # Async counterpart: AsyncModels.generate_content_stream returns an
        # awaitable that resolves to an async iterator of response chunks.
        # Strictly parallel to the kwargs.get("stream") branch of
        # _wrap_genai_generate_async above.
        trace = self._build_input_trace(args, kwargs, sdk="google-genai")
        t0 = time.perf_counter()
        try:
            stream = await wrapped(*args, **kwargs)
        except Exception as e:
            self._record_error(trace, t0, e)
            raise
        return _AsyncStreamingWrapper(stream, trace, t0, self)

    def _wrap_legacy_generate(self, wrapped, instance, args, kwargs):
        # Legacy SDK: model name is on the GenerativeModel instance
        model_name = getattr(instance, "model_name", "") or ""
        trace = self._build_input_trace(args, kwargs, sdk="google-generativeai",
                                        model_override=model_name)
        t0 = time.perf_counter()

        # Legacy SDK streams when stream=True (returns an iterable of chunks).
        if kwargs.get("stream"):
            try:
                stream = wrapped(*args, **kwargs)
            except Exception as e:
                self._record_error(trace, t0, e)
                raise
            return _StreamingWrapper(stream, trace, t0, self)

        try:
            resp = wrapped(*args, **kwargs)
        except Exception as e:
            self._record_error(trace, t0, e)
            raise
        should_persist, _is_error = decide_persist(False, self._should_sample())
        if should_persist:
            self._fill_output(trace, resp)
            trace.latency_ms = (time.perf_counter() - t0) * 1000.0
            self._safe_persist(trace)
        return resp

    # -- helpers ----------------------------------------------------------

    def _build_input_trace(
        self,
        args: tuple,
        kwargs: dict[str, Any],
        *,
        sdk: str,
        model_override: str = "",
    ) -> Trace:
        # google-genai shape: client.models.generate_content(model=..., contents=...)
        # legacy shape: model.generate_content(contents=...) with model on the instance
        model = model_override or str(kwargs.get("model", "")) or ""
        contents = kwargs.get("contents", args[0] if args else None)
        config = kwargs.get("config")
        temperature = None
        max_tokens = None
        if config is not None:
            temperature = getattr(config, "temperature", None)
            max_tokens = getattr(config, "max_output_tokens", None)

        trace = Trace(
            provider="google",
            operation=Operation.CHAT,
            request_model=model,
            response_model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        apply_routing_context(self.client, trace)
        if self.client.capture_content:
            prompt_text = _flatten_genai_contents(contents)
            trace.prompt_redacted = redact(
                prompt_text,
                mode=self.client.redaction_mode,  # type: ignore[arg-type]
                secret=self.client.redaction_secret,
            )
            # Mirror anthropic/openai: also retain the structured (redacted)
            # messages. google-genai's `contents` isn't a list of role/content
            # dicts, so normalize it into one user message before redacting via
            # the same redact_messages path the other instrumentors use.
            messages = _genai_contents_to_messages(contents)
            trace.raw_messages = redact_messages(
                messages,
                mode=self.client.redaction_mode,
                secret=self.client.redaction_secret,
            )
        return trace

    def _fill_output(self, trace: Trace, resp: Any) -> None:
        trace.ended_at = datetime.now(timezone.utc)
        try:
            # google-genai: response.text, response.usage_metadata.prompt_token_count, etc.
            usage = getattr(resp, "usage_metadata", None)
            if usage is not None:
                trace.input_tokens = getattr(usage, "prompt_token_count", None)
                trace.output_tokens = getattr(usage, "candidates_token_count", None)
            trace.cost_usd = compute_cost_usd(
                trace.response_model or trace.request_model,
                trace.input_tokens,
                trace.output_tokens,
            )
            # Candidate finish reason
            candidates = getattr(resp, "candidates", None) or []
            if candidates:
                trace.finish_reason = normalize_finish_reason(
                    getattr(candidates[0], "finish_reason", None)
                )
            if self.client.capture_content:
                text = getattr(resp, "text", "") or ""
                if not text and candidates:
                    text = _flatten_genai_contents(candidates[0])
                trace.response_redacted = redact(
                    text,
                    mode=self.client.redaction_mode,  # type: ignore[arg-type]
                    secret=self.client.redaction_secret,
                )
        except Exception:
            pass  # never break the user's request path

    def _record_error(self, trace: Trace, t0: float, e: BaseException) -> None:
        trace.ended_at = datetime.now(timezone.utc)
        trace.latency_ms = (time.perf_counter() - t0) * 1000.0
        trace.error = f"{type(e).__name__}: {e}"
        self._safe_persist(trace)

    def _safe_persist(self, trace: Trace) -> None:
        try:
            sanitize_trace(
                trace,
                mode=self.client.redaction_mode,  # type: ignore[arg-type]
                secret=self.client.redaction_secret,
            )
            persist_trace(self.client, trace)
        except Exception:
            pass


class _StreamingWrapper:
    """Pass-through iterator around a Google Gemini streaming response.

    Works for both google-genai (``generate_content(..., stream=True)`` yields
    GenerateContentResponse chunks) and the legacy google-generativeai SDK
    (``stream=True`` yields response chunks). Yields each chunk unchanged while
    accumulating text + usage_metadata + finish_reason. Normal exhaustion,
    iteration failure, explicit close, and context exit finalize deterministically.
    Async cancellation is recorded as an error. A dropped, never-iterated stream
    is not a supported finalization boundary.
    """

    def __init__(self, inner: Any, trace: Trace, t0: float, instr: GoogleInstrumentor) -> None:
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
            usage = getattr(chunk, "usage_metadata", None)
            if usage is not None:
                # Last non-None usage wins (cumulative totals arrive late).
                self._usage = usage
            candidates = getattr(chunk, "candidates", None) or []
            if candidates:
                fr = getattr(candidates[0], "finish_reason", None)
                if fr:
                    self._finish_reason = normalize_finish_reason(fr)
            if self._instr.client.capture_content:
                txt = getattr(chunk, "text", None)
                if not txt and candidates:
                    txt = _flatten_genai_contents(candidates[0])
                if txt:
                    self._text_chunks.append(txt)
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
                in_tok = getattr(self._usage, "prompt_token_count", None)
                out_tok = getattr(self._usage, "candidates_token_count", None)
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


def _genai_contents_to_messages(contents: Any) -> list[dict[str, Any]]:
    """Normalize google-genai's `contents` into chat-message dicts.

    google-genai accepts a str, a list of strings/Parts/Content objects, or a
    single Content/Part. We flatten the whole thing to text and wrap it in a
    single user message so it can flow through the shared redact_messages path
    that anthropic/openai use for `raw_messages`.
    """
    text = _flatten_genai_contents(contents)
    if not text:
        return []
    return [{"role": "user", "content": text}]


def _flatten_genai_contents(contents: Any) -> str:
    """Reduce google-genai's `contents` shape (str, list, list-of-Part, etc.)
    to plain text."""
    if contents is None:
        return ""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, (list, tuple)):
        parts: list[str] = []
        for item in contents:
            parts.append(_flatten_genai_contents(item))
        return "\n".join(p for p in parts if p)
    text = getattr(contents, "text", None)
    if text:
        return text
    # google.genai Content with parts list
    parts_attr = getattr(contents, "parts", None)
    if parts_attr:
        return "\n".join(_flatten_genai_contents(p) for p in parts_attr)
    return str(contents) if contents else ""
