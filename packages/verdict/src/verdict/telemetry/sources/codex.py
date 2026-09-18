"""Read completed model-response metadata from a local Codex diagnostic database."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from verdict.schema import Operation, Trace
from verdict.telemetry.model import ImportContext, MappingResult

_TARGET = "codex_core::session::turn"
_MARKER = "post sampling token usage"
_MAX_BODY_BYTES = 64 * 1024
_MAX_CALLS = 100_000
_TOKEN_MATCH_WINDOW_SECONDS = 0.25
_VALUE = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}"
_REQUIRED_COLUMNS = {
    "id",
    "ts",
    "ts_nanos",
    "target",
    "feedback_log_body",
    "process_uuid",
}


def codex_diagnostic_path(codex_root: Path) -> Path | None:
    """Return the sibling diagnostic database for a conventional Codex root."""
    if codex_root.name != "sessions":
        return None
    candidate = codex_root.parent / "logs_2.sqlite"
    return candidate if candidate.exists() or candidate.is_symlink() else None


def _field(text: str, name: str) -> str | None:
    matches = set(
        re.findall(
            rf"(?<![A-Za-z0-9_.]){re.escape(name)}=({_VALUE})(?=\s|[}},:]|$)",
            text,
        )
    )
    return next(iter(matches)) if len(matches) == 1 else None


def _timestamp(seconds: object, nanos: object) -> datetime | None:
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or isinstance(nanos, bool)
        or not isinstance(nanos, int)
        or seconds < 0
        or not 0 <= nanos < 1_000_000_000
    ):
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=nanos // 1_000)
    except (OverflowError, OSError, ValueError):
        return None


def _matched_tokens(
    thread_id: str,
    observed_at: datetime,
    token_usage_by_thread: Mapping[
        str, Sequence[tuple[float, int, int, int | None]]
    ],
) -> tuple[int | None, int | None, int | None]:
    usage = token_usage_by_thread.get(thread_id, ())
    timestamp = observed_at.timestamp()
    lower = bisect_left(
        usage, timestamp - _TOKEN_MATCH_WINDOW_SECONDS, key=lambda item: item[0]
    )
    upper = bisect_right(
        usage,
        timestamp + _TOKEN_MATCH_WINDOW_SECONDS,
        key=lambda item: item[0],
    )
    if upper - lower != 1:
        return None, None, None
    _, input_tokens, output_tokens, cached_input_tokens = usage[lower]
    return input_tokens, output_tokens, cached_input_tokens


def _map_row(
    row: tuple[object, ...],
    context: ImportContext,
    token_usage_by_thread: Mapping[
        str, Sequence[tuple[float, int, int, int | None]]
    ],
) -> MappingResult:
    row_id, seconds, nanos, process_uuid, body = row
    if (
        isinstance(row_id, bool)
        or not isinstance(row_id, int)
        or row_id < 1
        or not isinstance(process_uuid, str)
        or re.fullmatch(_VALUE, process_uuid) is None
    ):
        return MappingResult.skipped("invalid_source_id")
    try:
        body_bytes = len(body.encode("utf-8")) if isinstance(body, str) else 0
    except UnicodeEncodeError:
        body_bytes = 0
    if not isinstance(body, str) or not body_bytes or body_bytes > _MAX_BODY_BYTES:
        return MappingResult.skipped("invalid_diagnostic_body")
    if body.count(_MARKER) != 1:
        return MappingResult.skipped("invalid_call_boundary")
    span, event = body.split(_MARKER, 1)
    thread_id = _field(span, "thread.id")
    turn_id = _field(span, "turn.id")
    event_turn_id = _field(event, "turn_id")
    model = _field(span, "model")
    if not thread_id or not turn_id or turn_id != event_turn_id or not model:
        return MappingResult.skipped("invalid_call_boundary")
    observed_at = _timestamp(seconds, nanos)
    if observed_at is None:
        return MappingResult.skipped("invalid_start_time")

    external_id = f"{process_uuid}:{row_id}"
    tags = context.provenance_tags(external_id, thread_id)
    tags.update(
        {
            "verdict.workload": "agent",
            "verdict.input_evidence": "unavailable",
            "verdict.output_evidence": "unavailable",
            "verdict.time_evidence": "response_observed_at",
        }
    )
    input_tokens, output_tokens, cached_input_tokens = _matched_tokens(
        thread_id, observed_at, token_usage_by_thread
    )
    if cached_input_tokens is not None:
        tags["verdict.cached_input_tokens"] = str(cached_input_tokens)
    return MappingResult.mapped(
        Trace(
            trace_id=context.trace_id(external_id, thread_id),
            started_at=observed_at,
            provider="openai",
            operation=Operation.CHAT,
            request_model=model,
            response_model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tenant_id=context.tenant_id,
            session_id=hashlib.sha256(thread_id.encode()).hexdigest(),
            tags=tags,
            service_name="codex",
        )
    )


def iter_codex_diagnostic_calls(
    path: Path,
    *,
    context: ImportContext,
    token_usage_by_thread: Mapping[
        str, Sequence[tuple[float, int, int, int | None]]
    ] | None = None,
) -> Iterator[MappingResult]:
    """Yield one metadata-only Trace for each completed Codex sampling response."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsupported Codex diagnostic path")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1) as connection:
        connection.execute("PRAGMA query_only=ON")
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(logs)")
            if len(row) > 1 and isinstance(row[1], str)
        }
        if not _REQUIRED_COLUMNS.issubset(columns):
            raise ValueError("unsupported Codex diagnostic schema")
        rows = connection.execute(
            """SELECT id, ts, ts_nanos, process_uuid,
                      CASE
                        WHEN typeof(feedback_log_body) = 'text'
                         AND length(CAST(feedback_log_body AS BLOB)) <= ?
                        THEN feedback_log_body
                      END
                 FROM logs
                WHERE target=? AND feedback_log_body LIKE ?
                ORDER BY id DESC LIMIT ?""",
            (_MAX_BODY_BYTES, _TARGET, f"%{_MARKER}%", _MAX_CALLS + 1),
        )
        for index, row in enumerate(rows):
            if index == _MAX_CALLS:
                yield MappingResult.skipped("source_record_limit")
                break
            yield _map_row(row, context, token_usage_by_thread or {})
