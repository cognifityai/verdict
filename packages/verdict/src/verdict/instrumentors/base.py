"""Base class shared by every Verdict instrumentor."""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from verdict.client import VerdictClient
    from verdict.schema import Trace


def is_verdict_wrapt_wrapper(obj: object, *, owner: object | None = None) -> bool:
    """Return True only for wrappers installed by Verdict instrumentors.

    Provider SDK methods may carry their own ``__wrapped__`` attributes from
    decorators/overloads. Those are not Verdict wrappers and must not cause us
    to skip installation or unwrap SDK-native functions on shutdown.
    """
    try:
        import wrapt
    except ImportError:
        return False

    if not isinstance(obj, (wrapt.FunctionWrapper, wrapt.BoundFunctionWrapper)):
        return False

    wrapper = getattr(obj, "_self_wrapper", None)
    wrapper_owner = getattr(wrapper, "__self__", None)
    if owner is not None:
        return wrapper_owner is owner

    owner_cls = type(wrapper_owner)
    return owner_cls.__module__.startswith("verdict.instrumentors.")


def apply_routing_context(client: VerdictClient, trace: Trace) -> None:
    """Stamp tenant_id (from client config) and session_id / user_id_hash (from
    the per-request contextvars) onto a freshly built trace.

    Best-effort: routing metadata is never worth crashing a request over.
    """
    try:
        trace.tenant_id = getattr(client, "tenant_id", None)
        # Imported lazily to avoid an import cycle (client imports instrumentors).
        from verdict.client import (
            get_context_session_id,
            get_context_user_id_hash,
        )

        sid = get_context_session_id()
        if sid is not None:
            trace.session_id = sid
        uid = get_context_user_id_hash()
        if uid is not None:
            trace.user_id_hash = uid

        # Capture the exact manual span active when the provider call begins.
        # The object reference is in-memory only; parent_span_id is the durable
        # direction of the link on the Trace record.
        from verdict.trace import current_span

        parent_span = current_span()
        if parent_span is not None:
            trace.parent_span_id = parent_span.span_id
            trace._verdict_parent_span = parent_span  # type: ignore[attr-defined]
    except Exception:
        pass


def mark_trace_persisted(trace: Trace) -> None:
    """Attach a successfully persisted provider trace to its manual span chain.

    Running after the storage write prevents sampling or persistence failures
    from leaving ``SpanRecord.trace_id`` pointed at a nonexistent Trace.
    Explicit trace context is preserved when already present.
    """
    from verdict.trace import TraceLinkState

    span = getattr(trace, "_verdict_parent_span", None)
    direct_parent = True
    while span is not None:
        replace = span.trace_id is None
        if direct_parent and span.trace_link_state in {
            TraceLinkState.INHERITED_PROVIDER,
            TraceLinkState.INHERITED_PENDING_PROVIDER,
        }:
            replace = True
        if span.trace_link_state in {
            TraceLinkState.PENDING_PROVIDER,
            TraceLinkState.INHERITED_PENDING_PROVIDER,
        }:
            replace = span.trace_id == trace.trace_id
        if span.trace_link_state in {
            TraceLinkState.EXPLICIT,
            TraceLinkState.INHERITED_EXPLICIT,
        }:
            replace = not _trace_exists(trace, span.trace_id)

        if replace:
            span.trace_id = trace.trace_id
            span.trace_link_state = (
                TraceLinkState.PROVIDER
                if direct_parent
                else TraceLinkState.INHERITED_PROVIDER
            )
            span.attributes.pop("verdict.link_status", None)
            # A streaming response can be consumed only after its enclosing
            # manual span has exited. In that case the span record already
            # exists, so upsert it now with the newly durable trace link.
            if span.ended_at is not None:
                from verdict.trace import _persist_span

                _persist_span(span)
        direct_parent = False
        span = span.parent


def _trace_exists(trace: Trace, trace_id: str | None) -> bool:
    if trace_id is None:
        return False
    span = getattr(trace, "_verdict_parent_span", None)
    if span is None:
        return False
    try:
        from verdict.client import get_client

        client = get_client()
        if client is None:
            return False
        exists = getattr(client.storage, "trace_exists", None)
        if callable(exists):
            return bool(exists(trace_id))
        return client.storage.get_trace(trace_id) is not None
    except Exception:
        return False


def mark_trace_pending(trace: Trace) -> None:
    """Reserve an automatic link without representing it as durable yet."""
    from verdict.trace import TraceLinkState

    span = getattr(trace, "_verdict_parent_span", None)
    direct_parent = True
    while span is not None:
        claim = span.trace_link_state == TraceLinkState.NONE
        if direct_parent and span.trace_link_state in {
            TraceLinkState.INHERITED_PROVIDER,
            TraceLinkState.INHERITED_PENDING_PROVIDER,
        }:
            claim = True
        if claim:
            span.trace_id = trace.trace_id
            span.trace_link_state = (
                TraceLinkState.PENDING_PROVIDER
                if direct_parent
                else TraceLinkState.INHERITED_PENDING_PROVIDER
            )
        direct_parent = False
        span = span.parent


def mark_trace_failed(trace: Trace) -> None:
    """Clear reservations for a provider trace whose durable write failed."""
    from verdict.trace import TraceLinkState, _persist_span

    span = getattr(trace, "_verdict_parent_span", None)
    while span is not None:
        if (
            span.trace_id == trace.trace_id
            and span.trace_link_state
            in {
                TraceLinkState.PENDING_PROVIDER,
                TraceLinkState.INHERITED_PENDING_PROVIDER,
            }
        ):
            span.trace_id = None
            span.trace_link_state = TraceLinkState.NONE
            span.attributes.setdefault("verdict.link_status", "trace_write_failed")
            if span.ended_at is not None:
                _persist_span(span)
        span = span.parent


def persist_trace(client: VerdictClient, trace: Trace) -> None:
    """Persist a trace and link manual spans only after durable acknowledgement."""
    mark_trace_pending(trace)
    insert_with_ack = getattr(client.storage, "insert_trace_with_ack", None)
    if callable(insert_with_ack):
        insert_with_ack(
            trace,
            on_success=lambda: mark_trace_persisted(trace),
            on_failure=lambda _exc: mark_trace_failed(trace),
        )
        return
    try:
        client.storage.insert_trace(trace)
    except Exception:
        mark_trace_failed(trace)
        raise
    mark_trace_persisted(trace)


def normalize_finish_reason(raw: object) -> str | None:
    """Normalize a provider finish-reason into a lowercase member name.

    Different SDKs report finish reasons differently:
      - Anthropic: plain strings like "end_turn"
      - OpenAI: plain strings like "stop"
      - Google: enum values whose ``str()`` is "FinishReason.STOP"

    This strips any "ClassName." enum prefix and lowercases the result so
    downstream code has a comparable token. It does NOT attempt to unify the
    provider vocabularies themselves (e.g. "end_turn" vs "stop").

    Returns None for None/empty input.
    """
    if raw is None:
        return None
    # Prefer the enum member's ``.name`` when present (handles enum instances
    # regardless of how their ``__str__`` is defined).
    name = getattr(raw, "name", None)
    if isinstance(name, str) and name:
        return name.lower()
    text = str(raw)
    if not text:
        return None
    # Strip a leading "ClassName." enum-style prefix (take the last segment).
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    text = text.strip()
    if not text:
        return None
    return text.lower()


def decide_persist(raised: bool, should_sample: bool) -> tuple[bool, bool]:
    """Decide whether to persist a trace and whether it is an error trace.

    Centralizes the control-flow rule shared by every provider wrapper:
      - On exception (``raised`` True): always persist, marked as an error.
        Sampling is bypassed so failures are never dropped.
      - On success (``raised`` False): persist only if sampled, never an error.

    Returns ``(should_persist, is_error)``.
    """
    if raised:
        return True, True
    return should_sample, False


class BaseInstrumentor(abc.ABC):
    """Shared lifecycle for all instrumentors.

    Subclasses must declare `name` and implement `available`, `install`, `uninstall`.
    """

    name: str = ""

    def __init__(self, client: VerdictClient) -> None:
        self.client = client
        self._installed: bool = False

    @abc.abstractmethod
    def available(self) -> bool:
        """Return True if the underlying SDK is importable in this process."""

    @abc.abstractmethod
    def install(self) -> None:
        """Patch the target SDK. Idempotent."""

    @abc.abstractmethod
    def uninstall(self) -> None:
        """Reverse the patch. Idempotent."""
