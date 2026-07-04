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


def apply_routing_context(client: "VerdictClient", trace: "Trace") -> None:
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
    except Exception:
        pass


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

    def __init__(self, client: "VerdictClient") -> None:
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
