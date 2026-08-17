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
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
    redaction_mode: str = "redact"          # "redact" | "hash"  ("encrypt" planned)
    redaction_secret: str | None = None     # required for hash mode

    # Sampling
    sample_rate: float = 1.0                # 1.0 = capture everything

    # Multi-tenancy — stamped onto every trace from this process if set.
    tenant_id: str | None = None

    # Storage
    storage: Storage = field(default_factory=lambda: SQLiteStorage("./verdict.db"))

    # Which instrumentors to enable (defaults to all installed)
    enabled_instrumentors: list[str] = field(default_factory=list)

    # Internal — bound instrumentors after init() runs
    _instrumentors: list[BaseInstrumentor] = field(default_factory=list, repr=False)
    _initialized: bool = False


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
                f"sample_rate={sample_rate!r} is out of range. "
                "It must be a number in [0.0, 1.0]."
            )

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

        # Install instrumentors that are available
        _install_instrumentors(client)

        _client = client
        client._initialized = True
        log.info(
            "Verdict initialized: service=%s env=%s instrumentors=%s capture_content=%s",
            service_name, environment, [i.name for i in client._instrumentors], capture_content,
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
    "verdict_session_id", default=None,
)
_ctx_user_id_hash: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "verdict_user_id_hash", default=None,
)
_ctx_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "verdict_trace_id", default=None,
)


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


def get_context_session_id() -> str | None:
    """Return the session_id bound to the current context, if any."""
    return _ctx_session_id.get()


def get_context_user_id_hash() -> str | None:
    """Return the hashed user id bound to the current context, if any."""
    return _ctx_user_id_hash.get()


def get_context_trace_id() -> str | None:
    """Return the trace_id bound to the current logical request/task, if any."""
    return _ctx_trace_id.get()


@contextmanager
def trace_context(trace_id: str | None) -> Iterator[None]:
    """Temporarily bind a trace ID and restore the prior value on every exit."""
    token = _ctx_trace_id.set(trace_id or None)
    try:
        yield
    finally:
        _ctx_trace_id.reset(token)


def clear_context() -> None:
    """Clear any per-request context (useful between requests / in tests)."""
    _ctx_session_id.set(None)
    _ctx_user_id_hash.set(None)
    _ctx_trace_id.set(None)


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
