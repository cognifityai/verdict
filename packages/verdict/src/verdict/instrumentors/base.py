"""Base class shared by every Verdict instrumentor."""

from __future__ import annotations

import abc
import logging
import threading
import time
from typing import TYPE_CHECKING

from verdict.redaction import sanitize_trace
from verdict.schema import Trace

if TYPE_CHECKING:
    from verdict.client import VerdictClient


log = logging.getLogger("verdict.instrumentors")
_persistence_warning_lock = threading.Lock()
_warned_persistence_failures: set[tuple[str, str, type[BaseException]]] = set()


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
            get_context_workload,
        )

        sid = get_context_session_id()
        if sid is not None:
            trace.session_id = sid
        uid = get_context_user_id_hash()
        if uid is not None:
            trace.user_id_hash = uid
        workload = get_context_workload()
        if workload is not None:
            trace.tags = {**trace.tags, "verdict.workload": workload}

        # Automatic provider/manual-span correlation is deliberately one-way.
        # Multiple provider traces can share one manual parent, so choosing one
        # Trace as a reverse SpanRecord.trace_id owner would be lossy.
        from verdict.trace import current_span

        parent_span = current_span()
        if parent_span is not None:
            trace.parent_span_id = parent_span.span_id
    except Exception:
        pass


def persist_trace(client: VerdictClient, trace: Trace) -> None:
    """Persist a provider trace; parent_span_id already carries correlation."""
    # Provider response fields are filled after Trace.__post_init__, so repeat
    # the schema guard at the final synchronous persistence boundary.
    trace.normalize_scalars()
    client.storage.insert_trace(trace)


def _warn_persistence_failure_once(
    client: VerdictClient,
    trace: Trace,
    exc: BaseException,
) -> None:
    """Emit one non-sensitive warning for each provider/backend/error class."""
    provider = trace.provider if isinstance(trace.provider, str) else type(trace.provider).__name__
    storage_type = type(client.storage)
    storage_name = f"{storage_type.__module__}.{storage_type.__qualname__}"
    key = (provider, storage_name, type(exc))
    with _persistence_warning_lock:
        if key in _warned_persistence_failures:
            return
        _warned_persistence_failures.add(key)
    try:
        log.warning(
            "Verdict discarded a %s trace after %s persistence failed with %s",
            provider or "unknown-provider",
            storage_name,
            type(exc).__name__,
        )
    except Exception:
        # A user-supplied logging handler must not turn telemetry into an
        # application-path exception.
        pass


def safe_persist_trace(client: VerdictClient, trace: Trace) -> None:
    """Sanitize and persist telemetry without raising into provider calls."""
    started = time.perf_counter()
    failed = False
    try:
        sanitize_trace(
            trace,
            mode=client.redaction_mode,  # type: ignore[arg-type]
            secret=client.redaction_secret,
        )
        persist_trace(client, trace)
    except Exception as exc:
        failed = True
        _warn_persistence_failure_once(client, trace, exc)
    finally:
        client.runtime_metrics.record_capture(
            (time.perf_counter() - started) * 1000.0,
            failed=failed,
        )


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

    def _safe_persist(self, trace: Trace) -> None:
        """Persist through the single non-raising instrumentor sink."""
        safe_persist_trace(self.client, trace)

    @abc.abstractmethod
    def available(self) -> bool:
        """Return True if the underlying SDK is importable in this process."""

    @abc.abstractmethod
    def install(self) -> None:
        """Patch the target SDK. Idempotent."""

    @abc.abstractmethod
    def uninstall(self) -> None:
        """Reverse the patch. Idempotent."""
