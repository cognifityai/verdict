"""Bounded, metadata-only fleet read contract for trusted integrations.

The DTO and protocol in this module intentionally import no PostgreSQL code.
The PostgreSQL adapter and explicit preparation command are opt-in boundaries.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

VERDICT_FLEET_READ_SCHEMA_VERSION = "verdict.trace-window-read.v1"

_ERROR_CODES = frozenset(
    {
        "invalid_query",
        "unsupported_backend",
        "unsupported_version",
        "read_unavailable",
        "invalid_read_model",
        "window_too_dense",
        "response_limit_exceeded",
    }
)
_REQUEST_STATUSES = frozenset({"in_progress", "succeeded", "failed"})
_AGENT_LINK_STATES = frozenset({"exact", "not_found"})
_TENANT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_MAX_IDENTIFIER_BYTES = 256
_MAX_SOURCE_BYTES = 128
_MAX_MODEL_BYTES = 256
_MAX_ITEMS = 2_000
_MAX_COUNT = 2**63 - 1
_MAX_LATENCY_US = 86_400_000_000
_MAX_COST_MICRO_USD = 10**15
_MAX_WINDOW_SECONDS = 900
_MAX_RESPONSE_BYTES = 2_097_152


class VerdictFleetReadError(RuntimeError):
    """Stable Fleet ReadPort failure without storage exception details."""

    code: str

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in _ERROR_CODES:
            raise ValueError("unsupported Verdict fleet read error code")
        self.code = code
        super().__init__(code)


def _text(value: object, *, maximum: int, field_name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field_name} must be bounded text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{field_name} must be bounded text") from None
    if len(encoded) > maximum:
        raise ValueError(f"{field_name} must be bounded text")
    return value


def _optional_text(
    value: object,
    *,
    maximum: int,
    field_name: str,
    normalize: bool,
) -> str | None:
    if value == "" and normalize:
        return None
    if value is None:
        return None
    return _text(value, maximum=maximum, field_name=field_name)


def _utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    try:
        normalized = value.astimezone(timezone.utc)
    except Exception:
        raise ValueError(f"{field_name} must be a valid timezone-aware datetime") from None
    return normalized


def _bounded_integer(value: object, *, maximum: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{field_name} must be a bounded integer")
    return value


def _optional_integer(value: object, *, maximum: int, field_name: str) -> int | None:
    if value is None:
        return None
    return _bounded_integer(value, maximum=maximum, field_name=field_name)


def _tenant(value: object) -> str:
    if not isinstance(value, str) or value == "local" or _TENANT_PATTERN.fullmatch(value) is None:
        raise ValueError("tenant_id must be a non-local bounded ASCII identifier")
    return value


@dataclass(frozen=True)
class TraceContributionReadV1:
    tenant_id: str
    trace_id: str
    started_at: datetime
    ended_at: datetime | None
    request_status: str
    provider: str | None
    request_model: str | None
    response_model: str | None
    service_name: str | None
    environment: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_us: int | None
    cost_micro_usd: int | None
    parent_span_id: str | None
    agent_link_state: str
    agent_event_id: str | None
    agent_turn_id: str | None
    agent_run_id: str | None

    def __post_init__(self) -> None:
        _validate_trace_contribution(self, normalize=True)


def _validate_trace_contribution(value: TraceContributionReadV1, *, normalize: bool) -> None:
    _tenant(value.tenant_id)
    _text(value.trace_id, maximum=_MAX_IDENTIFIER_BYTES, field_name="trace_id")
    started_at = _utc(value.started_at, field_name="started_at")
    ended_at = None if value.ended_at is None else _utc(value.ended_at, field_name="ended_at")
    if normalize:
        object.__setattr__(value, "started_at", started_at)
        object.__setattr__(value, "ended_at", ended_at)

    if not isinstance(value.request_status, str) or value.request_status not in _REQUEST_STATUSES:
        raise ValueError("request_status is unsupported")
    if value.request_status == "in_progress":
        if ended_at is not None:
            raise ValueError("in_progress requires ended_at to be null")
    elif ended_at is None:
        raise ValueError("terminal request status requires ended_at")
    if ended_at is not None and not started_at <= ended_at <= started_at + timedelta(hours=24):
        raise ValueError("ended_at must be within 24 hours of started_at")

    source_fields = (
        ("provider", _MAX_SOURCE_BYTES),
        ("request_model", _MAX_MODEL_BYTES),
        ("response_model", _MAX_MODEL_BYTES),
        ("service_name", _MAX_SOURCE_BYTES),
        ("environment", _MAX_SOURCE_BYTES),
    )
    for field_name, maximum in source_fields:
        normalized = _optional_text(
            getattr(value, field_name),
            maximum=maximum,
            field_name=field_name,
            normalize=normalize,
        )
        if normalize:
            object.__setattr__(value, field_name, normalized)

    _optional_integer(value.input_tokens, maximum=_MAX_COUNT, field_name="input_tokens")
    _optional_integer(value.output_tokens, maximum=_MAX_COUNT, field_name="output_tokens")
    _optional_integer(value.latency_us, maximum=_MAX_LATENCY_US, field_name="latency_us")
    _optional_integer(
        value.cost_micro_usd,
        maximum=_MAX_COST_MICRO_USD,
        field_name="cost_micro_usd",
    )
    if value.parent_span_id is not None:
        _text(value.parent_span_id, maximum=_MAX_IDENTIFIER_BYTES, field_name="parent_span_id")

    if not isinstance(value.agent_link_state, str) or value.agent_link_state not in (
        _AGENT_LINK_STATES
    ):
        raise ValueError("agent_link_state is unsupported")
    agent_ids = (value.agent_event_id, value.agent_turn_id, value.agent_run_id)
    if value.agent_link_state == "exact":
        if any(item is None for item in agent_ids):
            raise ValueError("exact agent link requires event, turn, and run IDs")
    elif any(item is not None for item in agent_ids):
        raise ValueError("not_found agent link must clear event, turn, and run IDs")
    for field_name, item in zip(
        ("agent_event_id", "agent_turn_id", "agent_run_id"), agent_ids, strict=True
    ):
        if item is not None:
            _text(item, maximum=_MAX_IDENTIFIER_BYTES, field_name=field_name)


@dataclass(frozen=True)
class TraceWindowReadV1:
    schema_version: str
    tenant_id: str
    window_start: datetime
    window_end: datetime
    read_started_at: datetime
    read_completed_at: datetime
    item_count: int
    items: tuple[TraceContributionReadV1, ...]

    def __post_init__(self) -> None:
        _validate_trace_window(self, normalize=True)


def _validate_trace_window(value: TraceWindowReadV1, *, normalize: bool) -> None:
    if value.schema_version != VERDICT_FLEET_READ_SCHEMA_VERSION:
        raise ValueError("schema_version is unsupported")
    tenant_id = _tenant(value.tenant_id)
    window_start = _utc(value.window_start, field_name="window_start")
    window_end = _utc(value.window_end, field_name="window_end")
    read_started_at = _utc(value.read_started_at, field_name="read_started_at")
    read_completed_at = _utc(value.read_completed_at, field_name="read_completed_at")
    if normalize:
        object.__setattr__(value, "window_start", window_start)
        object.__setattr__(value, "window_end", window_end)
        object.__setattr__(value, "read_started_at", read_started_at)
        object.__setattr__(value, "read_completed_at", read_completed_at)
    if not window_start < window_end <= window_start + timedelta(seconds=_MAX_WINDOW_SECONDS):
        raise ValueError("window must be nonempty and no longer than 900 seconds")
    if read_completed_at < read_started_at:
        raise ValueError("read_completed_at cannot precede read_started_at")

    item_count = _bounded_integer(value.item_count, maximum=_MAX_ITEMS, field_name="item_count")
    if type(value.items) is not tuple or any(
        type(item) is not TraceContributionReadV1 for item in value.items
    ):
        raise ValueError("items must contain exact TraceContributionReadV1 values")
    if item_count != len(value.items):
        raise ValueError("item_count must equal the number of items")

    previous_key: tuple[datetime, str] | None = None
    for item in value.items:
        _validate_trace_contribution(item, normalize=normalize)
        if item.tenant_id != tenant_id:
            raise ValueError("item tenant must match the result tenant")
        if not window_start <= item.started_at < window_end:
            raise ValueError("item started_at must lie in the requested window")
        item_key = (item.started_at, item.trace_id)
        if previous_key is not None and item_key <= previous_key:
            raise ValueError("items must be unique and strictly ordered")
        previous_key = item_key


class VerdictFleetReadPortV1(Protocol):
    """Read one bounded tenant-scoped trace window."""

    def read_trace_window(
        self,
        *,
        tenant_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> TraceWindowReadV1: ...


class PostgresVerdictFleetReadPortV1:
    """PostgreSQL Fleet ReadPort with one bounded, read-only connection."""

    def __init__(self, database_url: str, *, tenant_id: str) -> None:
        from verdict._fleet_postgres import FleetPostgresAdapter

        self._adapter = FleetPostgresAdapter(database_url, tenant_id=tenant_id)

    def read_trace_window(
        self,
        *,
        tenant_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> TraceWindowReadV1:
        return self._adapter.read_trace_window(
            tenant_id=tenant_id,
            window_start=window_start,
            window_end=window_end,
        )

    def close(self) -> None:
        self._adapter.close()


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="auto").replace("+00:00", "Z")


def _contribution_payload(value: TraceContributionReadV1) -> dict[str, object]:
    return {
        "tenant_id": value.tenant_id,
        "trace_id": value.trace_id,
        "started_at": _rfc3339(value.started_at),
        "ended_at": None if value.ended_at is None else _rfc3339(value.ended_at),
        "request_status": value.request_status,
        "provider": value.provider,
        "request_model": value.request_model,
        "response_model": value.response_model,
        "service_name": value.service_name,
        "environment": value.environment,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "latency_us": value.latency_us,
        "cost_micro_usd": value.cost_micro_usd,
        "parent_span_id": value.parent_span_id,
        "agent_link_state": value.agent_link_state,
        "agent_event_id": value.agent_event_id,
        "agent_turn_id": value.agent_turn_id,
        "agent_run_id": value.agent_run_id,
    }


def trace_window_read_to_json(value: TraceWindowReadV1) -> str:
    """Serialize an exact Fleet ReadPort V1 result to bounded canonical JSON."""

    invalid_read_model = False
    try:
        if type(value) is not TraceWindowReadV1:
            raise ValueError("value must be an exact TraceWindowReadV1")
        _validate_trace_window(value, normalize=False)
        payload = {
            "schema_version": value.schema_version,
            "tenant_id": value.tenant_id,
            "window_start": _rfc3339(value.window_start),
            "window_end": _rfc3339(value.window_end),
            "read_started_at": _rfc3339(value.read_started_at),
            "read_completed_at": _rfc3339(value.read_completed_at),
            "item_count": value.item_count,
            "items": [_contribution_payload(item) for item in value.items],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except Exception:
        encoded = ""
        invalid_read_model = True
    if invalid_read_model:
        raise VerdictFleetReadError("invalid_read_model")
    if len(encoded.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise VerdictFleetReadError("response_limit_exceeded")
    return encoded


def main(argv: list[str] | None = None) -> int:
    """Prepare or disable the explicit PostgreSQL Fleet ReadPort boundary."""

    import argparse
    import os
    import sys

    from verdict._fleet_postgres import FleetPostgresConfigurationError, configure_fleet

    parser = argparse.ArgumentParser(prog="python -m verdict.fleet_read_port")
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "disable"):
        command = subparsers.add_parser(action)
        command.add_argument("--reader-role", required=True)
        command.add_argument("--tenant-id", required=True)
    args = parser.parse_args(argv)
    database_url = os.environ.get("VERDICT_DATABASE_URL")
    if not database_url:
        print("VERDICT_DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        configure_fleet(
            database_url,
            action=args.action,
            reader_role=args.reader_role,
            tenant_id=args.tenant_id,
        )
    except (FleetPostgresConfigurationError, VerdictFleetReadError, ImportError, ValueError):
        print(f"fleet {args.action} failed", file=sys.stderr)
        return 1
    return 0


__all__ = [
    "VERDICT_FLEET_READ_SCHEMA_VERSION",
    "PostgresVerdictFleetReadPortV1",
    "TraceContributionReadV1",
    "TraceWindowReadV1",
    "VerdictFleetReadError",
    "VerdictFleetReadPortV1",
    "trace_window_read_to_json",
]


if __name__ == "__main__":  # pragma: no cover - exercised through the module CLI
    raise SystemExit(main())
