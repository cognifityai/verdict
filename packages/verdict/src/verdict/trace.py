"""Manual instrumentation surface: @trace decorator and current_span() helper.

Auto-instrumentation covers LLM calls. Application RAG retrieval, business
logic, agent loops, and tool execution can use the manual surface — similar in
shape to Sentry's `@trace` or OpenTelemetry's `start_as_current_span`.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar
from uuid import uuid4

# Re-exported so callers can do `from verdict.trace import set_context`.
from verdict.client import (  # noqa: F401
    clear_context,
    get_client,
    get_context_trace_id,
    set_context,
    trace_context,
)

T = TypeVar("T")

_current: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "verdict_current_span", default=None,
)


class TraceLinkState(str, Enum):
    """Origin and durability state of a manual span's optional trace link."""

    NONE = "none"
    EXPLICIT = "explicit"
    INHERITED_EXPLICIT = "inherited_explicit"
    PENDING_PROVIDER = "pending_provider"
    INHERITED_PENDING_PROVIDER = "inherited_pending_provider"
    PROVIDER = "provider"
    INHERITED_PROVIDER = "inherited_provider"


@dataclass
class Span:
    """A lightweight manual span that can be persisted as a Verdict SpanRecord."""

    name: str
    started_at: float = field(default_factory=time.perf_counter)
    ended_at: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    parent: Span | None = None
    trace_id: str | None = None
    # Wall-clock timestamps for persistence (perf_counter above is monotonic and
    # not a real time; SpanRecord.started_at/ended_at need datetimes).
    started_wall: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    ended_wall: datetime | None = None
    span_id: str = field(default_factory=lambda: uuid4().hex)
    trace_link_state: TraceLinkState = TraceLinkState.NONE

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_error(self, error: BaseException | str) -> None:
        if isinstance(error, BaseException):
            detail = str(error)
            self.error = f"{type(error).__name__}: {detail}".rstrip()
        else:
            self.error = error

    @property
    def duration_ms(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at) * 1000.0


def current_span() -> Span | None:
    """Return the innermost active span, or None."""
    return _current.get()


# --- Unconfirmed provider-link registry ------------------------------------
#
# A span is ALWAYS written when it ends, even if the provider trace it links to
# has not been durably acknowledged yet. Deferring the write instead (waiting
# for the acknowledgement) loses every span the acknowledgement cannot reach:
# the callback walks a span's ANCESTORS, but sibling/child spans opened after
# the provider call inherit the same unconfirmed link and are not on that chain.
#
# Writing optimistically and correcting afterwards inverts the failure mode from
# "span silently lost" (unrecoverable) to "span briefly points at a trace that
# never landed" (recoverable, and retracted below). Spans that wrote with an
# unconfirmed link register here so the acknowledgement can reach them.
# Spans that wrote before their trace was acknowledged, keyed by trace id. The
# emitted SpanRecord is retained alongside the Span: a buffered write is only
# ENQUEUED when insert_span() returns, and the acknowledgement routinely fires
# while that record is still in the queue. Retracting the record itself makes
# the correction independent of which write lands first.
_unconfirmed_links: dict[
    str, list[tuple[weakref.ReferenceType[Span], Any]]
] = {}
# Acknowledged outcomes, so a span that writes AFTER the ack still learns it.
# The acknowledgement is not a barrier: a provider write can fail while the
# enclosing block is still open, before the sibling spans that inherit its link
# even exist. Draining the registry alone would never reach those.
_link_outcomes: OrderedDict[str, bool] = OrderedDict()
_unconfirmed_links_lock = threading.Lock()
# Bounds memory if a caller-supplied storage never invokes its acknowledgement
# callbacks. Registry entries are removed on every ack; outcomes evict FIFO.
_MAX_UNCONFIRMED_TRACES = 10_000
_MAX_LINK_OUTCOMES = 10_000

_PENDING_STATES = frozenset(
    {TraceLinkState.PENDING_PROVIDER, TraceLinkState.INHERITED_PENDING_PROVIDER}
)
_EXPLICIT_STATES = frozenset(
    {TraceLinkState.EXPLICIT, TraceLinkState.INHERITED_EXPLICIT}
)


def _link_already_failed(trace_id: str) -> bool:
    """True when this trace's write has already been acknowledged as failed."""
    with _unconfirmed_links_lock:
        return _link_outcomes.get(trace_id) is False


def _register_unconfirmed_link(trace_id: str, sp: Span, record: Any) -> bool:
    """Record that ``sp``/``record`` used a not-yet-acknowledged trace link.

    Returns False when the caller must retract the link itself: the write was
    acknowledged as failed in the window between this span's pre-write check and
    this registration, so the drain has already run and will not run again.
    """
    with _unconfirmed_links_lock:
        outcome = _link_outcomes.get(trace_id)
        if outcome is not None:
            return outcome  # True: durable, keep. False: caller retracts.
        if trace_id not in _unconfirmed_links and len(
            _unconfirmed_links
        ) >= _MAX_UNCONFIRMED_TRACES:
            return True  # Registry saturated; leave the optimistic link.
        _unconfirmed_links.setdefault(trace_id, []).append(
            (weakref.ref(sp), record)
        )
        return True


def resolve_unconfirmed_links(trace_id: str | None, *, durable: bool) -> None:
    """Drain the registry for ``trace_id`` after its write is acknowledged.

    ``durable=True`` simply clears the entry — the optimistic links are now
    correct. ``durable=False`` retracts each span's link and rewrites it, so a
    failed trace write can never leave a span pointing at a nonexistent trace.
    """
    if trace_id is None:
        return
    with _unconfirmed_links_lock:
        refs = _unconfirmed_links.pop(trace_id, [])
        _link_outcomes[trace_id] = durable
        _link_outcomes.move_to_end(trace_id)
        while len(_link_outcomes) > _MAX_LINK_OUTCOMES:
            _link_outcomes.popitem(last=False)
    if durable:
        return
    for ref, record in refs:
        # Retract the emitted record first. If its write is still queued, the
        # storage will persist the corrected value; if it already landed, the
        # rewrite below supersedes it. Both orderings converge on "unlinked".
        if record is not None and getattr(record, "trace_id", None) == trace_id:
            record.trace_id = None
            record.attributes.setdefault(
                "verdict.link_status", "trace_write_failed"
            )
        sp = ref()
        # Skip spans that were already retracted or re-linked elsewhere.
        if sp is None or sp.trace_id != trace_id:
            continue
        sp.trace_id = None
        sp.trace_link_state = TraceLinkState.NONE
        sp.attributes.setdefault("verdict.link_status", "trace_write_failed")
        if sp.ended_at is not None:
            _persist_span(sp)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """Open a span as a context manager.

    Usage:
        with verdict.trace.span("retrieve_documents", k=10) as s:
            docs = retriever.query(...)
            s.set_attribute("docs.count", len(docs))
    """
    parent = _current.get()
    # The task-local explicit context wins. This matters when asyncio child tasks
    # copy the same active parent Span object but bind different request traces.
    explicit_trace_id = get_context_trace_id()
    if explicit_trace_id is not None:
        active_trace_id = explicit_trace_id
        link_state = TraceLinkState.EXPLICIT
    elif parent is not None:
        active_trace_id = parent.trace_id
        if parent.trace_link_state in {
            TraceLinkState.EXPLICIT,
            TraceLinkState.INHERITED_EXPLICIT,
        }:
            link_state = TraceLinkState.INHERITED_EXPLICIT
        elif parent.trace_link_state in {
            TraceLinkState.PENDING_PROVIDER,
            TraceLinkState.INHERITED_PENDING_PROVIDER,
        }:
            link_state = TraceLinkState.INHERITED_PENDING_PROVIDER
        elif parent.trace_link_state in {
            TraceLinkState.PROVIDER,
            TraceLinkState.INHERITED_PROVIDER,
        }:
            link_state = TraceLinkState.INHERITED_PROVIDER
        else:
            link_state = TraceLinkState.NONE
    else:
        active_trace_id = None
        link_state = TraceLinkState.NONE
    sp = Span(
        name=name,
        parent=parent,
        trace_id=active_trace_id,
        trace_link_state=link_state,
        attributes=dict(attributes),
    )
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


def _persist_span(sp: Span) -> None:
    """Convert a completed Span to a SpanRecord and persist it, best-effort."""
    client = get_client()
    if client is None:
        return
    try:
        from verdict.redaction import sanitize_span
        from verdict.schema import SpanRecord

        linked_trace_id = sp.trace_id
        attributes = dict(sp.attributes)
        # An unconfirmed provider link is written optimistically and corrected by
        # resolve_unconfirmed_links() if the trace write ultimately fails. It is
        # deliberately NOT verified here: the instrumentor is mid-write, so a
        # lookup would race and report a false negative.
        unconfirmed = (
            linked_trace_id is not None and sp.trace_link_state in _PENDING_STATES
        )
        if unconfirmed and _link_already_failed(linked_trace_id):
            # The write was acknowledged as failed before this span existed.
            linked_trace_id = None
            unconfirmed = False
            attributes.setdefault("verdict.link_status", "trace_write_failed")
            sp.trace_id = None
            sp.trace_link_state = TraceLinkState.NONE
            sp.attributes.update(attributes)
        if linked_trace_id is not None and sp.trace_link_state in _EXPLICIT_STATES:
            try:
                trace_exists = getattr(client.storage, "trace_exists", None)
                if callable(trace_exists):
                    link_exists = bool(trace_exists(linked_trace_id))
                else:
                    link_exists = client.storage.get_trace(linked_trace_id) is not None
            except Exception:
                link_exists = False
                attributes.setdefault("verdict.link_status", "trace_lookup_failed")
            if not link_exists:
                linked_trace_id = None
                attributes.setdefault("verdict.link_status", "trace_not_found")
                sp.trace_id = None
                sp.trace_link_state = TraceLinkState.NONE
                sp.attributes.update(attributes)
        record = SpanRecord(
            span_id=sp.span_id,
            name=sp.name,
            trace_id=linked_trace_id,
            parent_name=sp.parent.name if sp.parent is not None else None,
            started_at=sp.started_wall,
            ended_at=sp.ended_wall,
            duration_ms=sp.duration_ms,
            attributes=attributes,
            error=sp.error,
        )
        sanitize_span(
            record,
            mode=client.redaction_mode,  # type: ignore[arg-type]
            secret=client.redaction_secret,
        )
        client.storage.insert_span(record)
        if unconfirmed and linked_trace_id is not None:
            registered = _register_unconfirmed_link(linked_trace_id, sp, record)
            # Re-check AFTER the write. The acknowledgement runs on another
            # thread and its retraction can be written before this linked record
            # lands (BufferedStorage writes synchronously from its own worker
            # while this record is still queued), so last-write-wins would keep
            # the stale link. Rewriting here is ordered after our own write.
            if not registered or _link_already_failed(linked_trace_id):
                sp.trace_id = None
                sp.trace_link_state = TraceLinkState.NONE
                sp.attributes.setdefault(
                    "verdict.link_status", "trace_write_failed"
                )
                _persist_span(sp)
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
