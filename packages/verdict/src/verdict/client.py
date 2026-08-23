"""Public entrypoint: verdict.init() and the VerdictClient configuration object.

The init() function is intentionally the entire public API surface for v0.
Customer integration is:

    import verdict
    verdict.init(storage="sqlite:///./verdict.db")
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import logging
import math
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from verdict.runtime_metrics import RuntimeMetrics
from verdict.storage.base import Storage
from verdict.storage.sqlite import SQLiteStorage

if TYPE_CHECKING:
    from verdict.instrumentors.base import BaseInstrumentor

log = logging.getLogger("verdict")


@dataclass
class VerdictClient:
    """Process-wide configuration for the Verdict SDK.

    Held as a module-level singleton (set by init()), accessed by instrumentors.
    """

    api_key: str = ""
    service_name: str = "unknown-service"
    environment: str = "production"

    # Content capture — OFF by default. PII risk surface.
    capture_content: bool = False
    redaction_mode: str = "redact"  # "redact" | "hash"  ("encrypt" planned)
    redaction_secret: str | None = None  # required for hash mode

    # Sampling
    sample_rate: float = 1.0  # 1.0 = capture everything

    # Multi-tenancy — stamped onto every trace from this process if set.
    tenant_id: str | None = None

    # Storage
    storage: Storage = field(default_factory=lambda: SQLiteStorage("./verdict.db"))

    # Which instrumentors to enable (defaults to all installed)
    enabled_instrumentors: list[str] = field(default_factory=list)

    # Internal — bound instrumentors after init() runs
    _instrumentors: list[BaseInstrumentor] = field(default_factory=list, repr=False)
    _initialized: bool = False
    runtime_metrics: RuntimeMetrics = field(
        default_factory=RuntimeMetrics,
        init=False,
        repr=False,
        compare=False,
    )


_client: VerdictClient | None = None
_lock = threading.Lock()


def init(
    *,
    api_key: str | None = None,
    service_name: str = "unknown-service",
    environment: str = "production",
    storage: str | Storage = "sqlite:///./verdict.db",
    capture_content: bool = False,
    redaction_mode: str = "redact",
    redaction_secret: str | None = None,
    sample_rate: float = 1.0,
    tenant_id: str | None = None,
    instrumentors: list[str] | None = None,
    buffered_writes: bool = False,
    tenant_mode: str = "fixed",
) -> VerdictClient:
    """Initialize Verdict and install auto-instrumentation.

    Call once at application startup. Subsequent calls are no-ops with a warning.

    Args:
        api_key: Reserved for API-compatible integrations; currently unused.
        service_name: Identifies your service in traces.
        environment: e.g. "production", "staging", "dev".
        storage: Either a URL ("sqlite:///path.db") or a Storage instance.
        capture_content: If True, prompts/completions are stored (redacted).
        redaction_mode: "redact" | "hash" ("encrypt" is planned, not implemented).
        redaction_secret: HMAC secret (required for hash mode).
        sample_rate: Fraction of traces to capture (0.0-1.0). Default 1.0.
        tenant_id: Optional tenant identifier stamped onto every captured trace.
        instrumentors: Optional explicit list, e.g. ["anthropic", "openai"].
                       Default: install all available.
        buffered_writes: If True, wrap storage in BufferedStorage so captured
                       traces are written on a background thread (batched) instead
                       of synchronously on the request hot path. Recommended for
                       high-volume production; adds a background flush thread.
        tenant_mode: ``fixed`` preserves tenant_id behavior. ``request`` requires
                     an authorized tenant in every request_context and forbids a
                     process-wide tenant_id.

    Returns:
        The VerdictClient singleton.
    """
    global _client

    with _lock:
        if _client is not None and _client._initialized:
            log.warning("verdict.init() called more than once; ignoring subsequent call")
            return _client

        # Validate redaction config up front. "encrypt" is documented as a
        # future mode but not implemented, so reject it loudly here rather than
        # letting redact() raise NotImplementedError on every captured call
        # (which, depending on the code path, either crashes the request or is
        # silently swallowed and drops all content capture).
        if redaction_mode not in ("redact", "hash"):
            raise ValueError(
                f"Unsupported redaction_mode={redaction_mode!r}. "
                "Supported modes are 'redact' and 'hash' "
                "('encrypt' is planned but not yet implemented)."
            )
        if redaction_mode == "hash" and not redaction_secret:
            raise ValueError("redaction_mode='hash' requires a redaction_secret")

        # Validate sampling fraction up front (mirrors the redaction_mode check
        # above): an out-of-range sample_rate is a configuration error, not
        # something to silently clamp. _should_sample treats >=1.0 as "always"
        # and <=0.0 as "never", so a stray 2.0 or -0.5 would quietly change
        # capture behavior. Reject it loudly.
        if not isinstance(sample_rate, (int, float)) or not (0.0 <= sample_rate <= 1.0):
            raise ValueError(
                f"sample_rate={sample_rate!r} is out of range. It must be a number in [0.0, 1.0]."
            )
        if tenant_mode not in {"fixed", "request"}:
            raise ValueError("tenant_mode must be 'fixed' or 'request'")
        if tenant_mode == "request" and tenant_id is not None:
            raise ValueError("tenant_mode='request' cannot use a fixed tenant_id")

        # Resolve storage URL → instance
        if isinstance(storage, str):
            storage_inst = _resolve_storage(storage)
        else:
            storage_inst = storage

        # Optionally move writes off the request hot path onto a background
        # batched writer. Opt-in because it starts a flush thread.
        if buffered_writes:
            from verdict.storage.buffered import BufferedStorage

            storage_inst = BufferedStorage(storage_inst)

        client = VerdictClient(
            api_key=api_key or "",
            service_name=service_name,
            environment=environment,
            storage=storage_inst,
            capture_content=capture_content,
            redaction_mode=redaction_mode,
            redaction_secret=redaction_secret,
            sample_rate=sample_rate,
            tenant_id=tenant_id,
            enabled_instrumentors=instrumentors or [],
        )
        client._tenant_mode = tenant_mode

        # Install instrumentors that are available
        _install_instrumentors(client)

        _client = client
        client._initialized = True
        log.info(
            "Verdict initialized: service=%s env=%s instrumentors=%s capture_content=%s",
            service_name,
            environment,
            [i.name for i in client._instrumentors],
            capture_content,
        )
        return client


def _resolve_storage(url: str) -> Storage:
    """Parse a storage URL into a concrete Storage adapter."""
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///") :]
        return SQLiteStorage(path)
    if url == "memory://" or url.startswith("memory://"):
        from verdict.storage.memory import InMemoryStorage

        return InMemoryStorage()
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        from verdict.storage.postgres import PostgresStorage

        return PostgresStorage(url)
    raise ValueError(f"Unsupported storage URL: {url!r}")


def _install_instrumentors(client: VerdictClient) -> None:
    """Discover and install available instrumentors.

    Each instrumentor is best-effort: if the underlying SDK is not installed,
    that instrumentor is silently skipped.
    """
    from verdict.instrumentors.anthropic import AnthropicInstrumentor
    from verdict.instrumentors.google import GoogleInstrumentor
    from verdict.instrumentors.openai import OpenAIInstrumentor

    candidates: list[type] = [AnthropicInstrumentor, OpenAIInstrumentor, GoogleInstrumentor]
    requested = set(client.enabled_instrumentors)

    for cls in candidates:
        instr = cls(client)
        if requested and instr.name not in requested:
            continue
        if not instr.available():
            log.debug("Skipping instrumentor %s (SDK not installed)", instr.name)
            continue
        try:
            instr.install()
            client._instrumentors.append(instr)
        except Exception as e:  # pragma: no cover — defensive
            log.warning("Failed to install %s instrumentor: %s", instr.name, e)


def get_client() -> VerdictClient | None:
    """Return the active client, or None if init() has not been called."""
    return _client


# ---------------------------------------------------------------------------
# Per-call request context (session_id / user id) — propagated via contextvars
# so the value is bound to the logical request/task, not the process. The raw
# user id is NEVER stored: it is HMAC-hashed (with the redaction secret when
# available, else SHA-256) before it ever reaches a Trace.
# ---------------------------------------------------------------------------

_ctx_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "verdict_session_id",
    default=None,
)
_ctx_tenant_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "verdict_tenant_id",
    default=None,
)
_ctx_user_id_hash: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "verdict_user_id_hash",
    default=None,
)
_ctx_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "verdict_trace_id",
    default=None,
)
_ctx_workload: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "verdict_workload",
    default=None,
)
_ctx_intent_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "verdict_intent_key",
    default=None,
)
_ctx_sample_rate: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "verdict_sample_rate",
    default=None,
)
_ctx_success_sampling: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "verdict_success_sampling",
    default=None,
)
_WORKLOAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


def _validate_workload(workload: str) -> None:
    if workload and _WORKLOAD.fullmatch(workload) is None:
        raise ValueError(
            "workload must be a 1-64 character identifier containing only "
            "letters, numbers, '.', '_', ':', or '-'"
        )
    if workload:
        from verdict.redaction import redact

        if redact(workload, mode="redact") != workload:
            raise ValueError("workload must be a redaction-safe routing identifier")


def _validate_intent_key(intent_key: str) -> None:
    _validate_workload(intent_key)
    from verdict.redaction import redact

    if redact(intent_key, mode="redact") != intent_key:
        raise ValueError("intent_key must be a redaction-safe routing identifier")


def _hash_user_id(user_id: str) -> str:
    """Hash a raw user id so it can be correlated without being stored raw.

    Uses HMAC-SHA-256 keyed with the active client's redaction_secret when one
    is configured; otherwise falls back to a plain SHA-256 digest. Either way
    the raw id never leaves this function.
    """
    secret = _client.redaction_secret if _client is not None else None
    if secret:
        digest = hmac.new(
            secret.encode("utf-8"), user_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    else:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return digest


def set_context(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
    workload: str | None = None,
) -> None:
    """Bind per-request context for subsequently captured traces.

    Call this at the start of handling a request/task. ``session_id`` is stored
    as-is; ``user_id`` is HMAC/SHA-256 hashed (never stored raw). Passing None
    leaves the existing value unchanged; pass "" to clear a value. ``trace_id``
    links manual spans to an existing captured LLM trace without using a
    process-global mutable value.
    """
    if session_id is not None:
        _ctx_session_id.set(session_id or None)
    if user_id is not None:
        _ctx_user_id_hash.set(_hash_user_id(user_id) if user_id else None)
    if trace_id is not None:
        _ctx_trace_id.set(trace_id or None)
    if workload is not None:
        _validate_workload(workload)
        _ctx_workload.set(workload or None)


def get_context_session_id() -> str | None:
    """Return the session_id bound to the current context, if any."""
    return _ctx_session_id.get()


def get_context_tenant_id() -> str | None:
    """Return the request-scoped authorized tenant, if any."""
    return _ctx_tenant_id.get()


def get_context_user_id_hash() -> str | None:
    """Return the hashed user id bound to the current context, if any."""
    return _ctx_user_id_hash.get()


def get_context_trace_id() -> str | None:
    """Return the trace_id bound to the current logical request/task, if any."""
    return _ctx_trace_id.get()


def get_context_workload() -> str | None:
    """Return the workload provenance bound to the current request."""
    return _ctx_workload.get()


def get_context_intent_key() -> str | None:
    """Return the explicit intent key bound to the current request."""
    return _ctx_intent_key.get()


def get_context_sample_rate() -> float | None:
    """Return the request-start success sample rate override, if any."""
    return _ctx_sample_rate.get()


def get_context_success_sampling() -> str | None:
    """Return ``call`` or ``session`` for the current request."""
    return _ctx_success_sampling.get()


def _validate_context_text(name: str, value: str, *, max_bytes: int) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if len(encoded) > max_bytes or "\x00" in value:
        raise ValueError(f"{name} exceeds its safe bound")


@contextmanager
def request_context(
    *,
    tenant_id: str,
    session_id: str,
    workload: str,
    sample_rate: float,
    success_sampling: str,
    user_id: str | None = None,
    intent_key: str | None = None,
) -> Iterator[None]:
    """Bind one validated request snapshot and restore every prior token.

    Validation and user-ID hashing finish before any ContextVar is mutated.
    """
    _validate_context_text("tenant_id", tenant_id, max_bytes=128)
    _validate_context_text("session_id", session_id, max_bytes=256)
    _validate_workload(workload)
    if not isinstance(sample_rate, (int, float)) or isinstance(sample_rate, bool):
        raise ValueError("sample_rate must be a finite number in [0,1]")
    normalized_rate = float(sample_rate)
    if not math.isfinite(normalized_rate) or not 0.0 <= normalized_rate <= 1.0:
        raise ValueError("sample_rate must be a finite number in [0,1]")
    if success_sampling not in {"call", "session"}:
        raise ValueError("success_sampling must be 'call' or 'session'")
    if intent_key is not None:
        _validate_intent_key(intent_key)
    user_hash = None
    if user_id is not None:
        _validate_context_text("user_id", user_id, max_bytes=1024)
        user_hash = _hash_user_id(user_id)

    client = _client
    tenant_mode = getattr(client, "_tenant_mode", "fixed") if client is not None else "fixed"
    fixed_tenant = client.tenant_id if client is not None else None
    if tenant_mode == "fixed" and tenant_id != fixed_tenant:
        raise ValueError("request tenant does not match the fixed Verdict tenant")
    if tenant_mode == "request" and fixed_tenant is not None:
        raise ValueError("request tenant mode cannot use a fixed Verdict tenant")

    tokens = (
        (_ctx_tenant_id, _ctx_tenant_id.set(tenant_id)),
        (_ctx_session_id, _ctx_session_id.set(session_id)),
        (_ctx_user_id_hash, _ctx_user_id_hash.set(user_hash)),
        (_ctx_workload, _ctx_workload.set(workload)),
        (_ctx_sample_rate, _ctx_sample_rate.set(normalized_rate)),
        (_ctx_success_sampling, _ctx_success_sampling.set(success_sampling)),
        (_ctx_intent_key, _ctx_intent_key.set(intent_key)),
    )
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


@contextmanager
def trace_context(trace_id: str | None) -> Iterator[None]:
    """Temporarily bind a trace ID and restore the prior value on every exit."""
    token = _ctx_trace_id.set(trace_id or None)
    try:
        yield
    finally:
        _ctx_trace_id.reset(token)


@contextmanager
def workload_context(workload: str) -> Iterator[None]:
    """Temporarily identify captured LLM calls by workload provenance."""
    _validate_workload(workload)
    token = _ctx_workload.set(workload or None)
    try:
        yield
    finally:
        _ctx_workload.reset(token)


@contextmanager
def intent_context(intent_key: str) -> Iterator[None]:
    """Temporarily bind a validated explicit clustering key."""
    _validate_intent_key(intent_key)
    token = _ctx_intent_key.set(intent_key)
    try:
        yield
    finally:
        _ctx_intent_key.reset(token)


def clear_context() -> None:
    """Clear any per-request context (useful between requests / in tests)."""
    _ctx_session_id.set(None)
    _ctx_tenant_id.set(None)
    _ctx_user_id_hash.set(None)
    _ctx_trace_id.set(None)
    _ctx_workload.set(None)
    _ctx_intent_key.set(None)
    _ctx_sample_rate.set(None)
    _ctx_success_sampling.set(None)


def shutdown() -> None:
    """Uninstall instrumentation and close storage. Useful in tests."""
    global _client
    with _lock:
        if _client is None:
            clear_context()
            return
        for instr in _client._instrumentors:
            try:
                instr.uninstall()
            except Exception as e:  # pragma: no cover
                log.warning("Error uninstalling %s: %s", instr.name, e)
        try:
            _client.storage.close()
        except Exception:
            pass
        _client = None
        clear_context()
