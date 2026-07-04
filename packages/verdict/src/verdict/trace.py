"""Manual instrumentation surface: @trace decorator and current_span() helper.

Auto-instrumentation covers LLM calls. Application RAG retrieval, business
logic, agent loops, and tool execution can use the manual surface — similar in
shape to Sentry's `@trace` or OpenTelemetry's `start_as_current_span`.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeVar

# Re-exported so callers can do `from verdict.trace import set_context`.
from verdict.client import (  # noqa: F401
    clear_context,
    get_client,
    set_context,
)

T = TypeVar("T")

_current: contextvars.ContextVar["Span | None"] = contextvars.ContextVar(
    "verdict_current_span", default=None,
)


@dataclass
class Span:
    """A lightweight manual span that can be persisted as a Verdict SpanRecord."""

    name: str
    started_at: float = field(default_factory=time.perf_counter)
    ended_at: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    parent: "Span | None" = None
    # Wall-clock timestamps for persistence (perf_counter above is monotonic and
    # not a real time; SpanRecord.started_at/ended_at need datetimes).
    started_wall: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    ended_wall: datetime | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_error(self, error: BaseException | str) -> None:
        self.error = str(error) if isinstance(error, BaseException) else error

    @property
    def duration_ms(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at) * 1000.0


def current_span() -> Span | None:
    """Return the innermost active span, or None."""
    return _current.get()


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """Open a span as a context manager.

    Usage:
        with verdict.trace.span("retrieve_documents", k=10) as s:
            docs = retriever.query(...)
            s.set_attribute("docs.count", len(docs))
    """
    parent = _current.get()
    sp = Span(name=name, parent=parent, attributes=dict(attributes))
    token = _current.set(sp)
    try:
        yield sp
    except BaseException as exc:
        sp.set_error(exc)
        raise
    finally:
        sp.ended_at = time.perf_counter()
        sp.ended_wall = datetime.now(timezone.utc)
        _current.reset(token)
        # Persist the completed span via the global client's storage, if a
        # client has been initialized. Stay a no-op (never crash the caller's
        # code path) when there's no client or persistence fails.
        _persist_span(sp)


def _persist_span(sp: "Span") -> None:
    """Convert a completed Span to a SpanRecord and persist it, best-effort."""
    client = get_client()
    if client is None:
        return
    try:
        from verdict.schema import SpanRecord

        record = SpanRecord(
            name=sp.name,
            parent_name=sp.parent.name if sp.parent is not None else None,
            started_at=sp.started_wall,
            ended_at=sp.ended_wall,
            duration_ms=sp.duration_ms,
            attributes=dict(sp.attributes),
            error=sp.error,
        )
        client.storage.insert_span(record)
    except Exception:
        # Telemetry must never propagate into the caller's path.
        pass


def trace(name: str | None = None, **attributes: Any) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator that wraps a function call in a span.

    Usage:
        @verdict.trace("retrieve_documents", index="prod")
        def retrieve(query: str): ...
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"
        is_coro = inspect.iscoroutinefunction(fn)

        if is_coro:
            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> T:
                with span(span_name, **attributes):
                    return await fn(*args, **kwargs)  # type: ignore[return-value, misc]

            return awrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            with span(span_name, **attributes):
                return fn(*args, **kwargs)

        return wrapper

    return decorator
