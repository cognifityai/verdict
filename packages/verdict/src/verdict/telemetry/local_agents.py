"""Bounded adapters for local Claude Code and Codex history files.

The source session is an ``AgentRun``. User interactions are ``AgentTurn``
records and observable source facts are typed events. Claude assistant messages
with an explicit provider response boundary are also projected into genuine
LLM ``Trace`` records; an agent turn or session is never promoted into a trace.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.capture import AgentCaptureService
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
    stable_evidence_id,
)
from verdict.normalized_evidence import _LegacyLocalTextUpgrade
from verdict.redaction import redact, redact_structure
from verdict.schema import Operation, Trace
from verdict.storage.base import Storage

_AMBIENT_BLOCK = re.compile(
    r"\A\s*<(environment_context|in-app-browser-context)\b[^>]*>.*?</\1>\s*",
    flags=re.DOTALL,
)
_REQUEST_HEADING = re.compile(r"\A\s*##\s+My request:\s*", flags=re.IGNORECASE)
_COMMAND_TOOLS = frozenset({"bash", "shell", "exec_command", "run_command"})
_MAX_CONTENT_CHARS = 1_000
_MAX_TURN_CONTENT_BYTES = 65_536
_MAX_FILES = 100_000
_MAX_EVENTS = 250_000
_MAX_STORED_EVENTS = 1_500
_MAX_LINE_BYTES = 16 * 1024 * 1024


@dataclass
class LocalCaptureSummary:
    files: int = 0
    stored: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def add_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "files": self.files,
            "stored": self.stored,
            "skipped": self.skipped,
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
        }


@dataclass
class _RawEvent:
    occurred_at: datetime
    event_type: AgentEventType
    status: ExecutionStatus
    provenance: str
    attributes: dict[str, Any]
    has_content: bool = False
    provider_response_id: str = ""
    response_text: str = ""
    legacy_response_text: str = ""


@dataclass
class _RawTurn:
    source_id: str
    started_at: datetime
    ended_at: datetime | None = None
    status: ExecutionStatus = ExecutionStatus.UNKNOWN
    request: str = ""
    response: str = ""
    legacy_request: str = ""
    legacy_response: str = ""
    request_truncated: bool = False
    response_truncated: bool = False
    token_usage: dict[str, int] = field(default_factory=dict)
    token_usage_basis: str = ""
    usage_invalid: bool = False
    codex_usage_snapshots: list[tuple[dict[str, int], dict[str, int]]] = field(
        default_factory=list
    )
    codex_latest_boundary: dict[str, int] | None = None
    claude_usage_by_response_id: dict[str, dict[str, int]] = field(default_factory=dict)
    claude_invalid_response_ids: set[str] = field(default_factory=set)
    events: list[_RawEvent] = field(default_factory=list)


@dataclass(frozen=True)
class _ParsedHistory:
    session_id: str
    version: str
    turns: list[_RawTurn]
    parent_session_id: str | None = None
    logical_session_id: str | None = None


@dataclass(frozen=True)
class _BoundedText:
    value: str
    truncated: bool = False
    legacy_value: str = ""


def _mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _bounded_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")[:maximum_bytes]
    return encoded.decode("utf-8", errors="ignore")


def _clean_text(value: object, *, home: Path | None) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value
    while match := _AMBIENT_BLOCK.match(cleaned):
        cleaned = cleaned[match.end() :]
    cleaned = _REQUEST_HEADING.sub("", cleaned)
    home_text = str(home or Path.home())
    if home_text and home_text != "/":
        cleaned = cleaned.replace(home_text, "~")
    return cleaned.strip()


def _legacy_bounded_text(value: str) -> str:
    """Reproduce the a17 local-history text cutoff for upgrade validation."""
    return _bounded_utf8(value[:_MAX_CONTENT_CHARS], 4_000)


def _bounded_turn_text(value: str, *, source_truncated: bool = False) -> _BoundedText:
    redacted = redact(value) or ""
    encoded = redacted.encode("utf-8")
    return _BoundedText(
        _bounded_utf8(redacted, _MAX_TURN_CONTENT_BYTES),
        source_truncated or len(encoded) > _MAX_TURN_CONTENT_BYTES,
        _legacy_bounded_text(value),
    )


def _safe_text(value: object, *, home: Path | None) -> _BoundedText:
    cleaned = _clean_text(value, home=home)
    return _bounded_turn_text(
        cleaned,
        source_truncated=len(cleaned.encode("utf-8")) > _MAX_TURN_CONTENT_BYTES,
    )


def _message_text(message: dict[str, object], *, home: Path | None) -> _BoundedText:
    content = message.get("content")
    if isinstance(content, str):
        return _safe_text(content, home=home)
    if not isinstance(content, list):
        return _BoundedText("")
    texts = [
        text
        for item in content
        if (block := _mapping(item)) is not None
        and block.get("type") == "text"
        and (text := _clean_text(block.get("text"), home=home))
    ]
    joined = "\n".join(texts)
    bounded = _bounded_turn_text(
        joined,
        source_truncated=len(joined.encode("utf-8")) > _MAX_TURN_CONTENT_BYTES,
    )
    legacy = "\n".join(_legacy_bounded_text(text) for text in texts)[:_MAX_CONTENT_CHARS]
    return _BoundedText(bounded.value, bounded.truncated, legacy)


def _append_turn_text(
    current: str,
    current_legacy: str,
    incoming: _BoundedText,
) -> _BoundedText:
    joined = "\n\n".join(filter(None, (current, incoming.value)))
    encoded = joined.encode("utf-8")
    legacy = "\n\n".join(
        filter(None, (current_legacy, incoming.legacy_value))
    )[:_MAX_CONTENT_CHARS]
    return _BoundedText(
        _bounded_utf8(joined, _MAX_TURN_CONTENT_BYTES),
        incoming.truncated or len(encoded) > _MAX_TURN_CONTENT_BYTES,
        legacy,
    )


_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _token_usage(
    value: object,
    *,
    aliases: dict[str, str] | None = None,
) -> tuple[dict[str, int], bool]:
    mapped = _mapping(value)
    if mapped is None:
        return {}, value is not None
    canonical: dict[str, int] = {}
    source_names = aliases or {name: name for name in _TOKEN_FIELDS}
    for source_name, target_name in source_names.items():
        if source_name not in mapped:
            continue
        count = mapped[source_name]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= 2**63 - 1
        ):
            return {}, True
        canonical[target_name] = count
    return canonical, False


def _provider_response_id(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    try:
        return value if len(value.encode("utf-8")) <= 256 else None
    except UnicodeError:
        return None


def _record_codex_usage(turn: _RawTurn, info: object) -> None:
    mapped = _mapping(info)
    if mapped is None:
        turn.codex_latest_boundary = None
        if info is not None:
            turn.usage_invalid = True
        return
    last, invalid_last = _token_usage(mapped.get("last_token_usage"))
    total, invalid_total = _token_usage(mapped.get("total_token_usage"))
    boundary_invalid = bool(
        total
        and not invalid_total
        and (
            any(
                name in turn.codex_latest_boundary
                and total[name] < turn.codex_latest_boundary[name]
                for name in total
            )
            if turn.codex_latest_boundary is not None
            else False
        )
    )
    if last and total and not invalid_last and not invalid_total:
        boundary_invalid = boundary_invalid or any(
            name in total and total[name] < count for name, count in last.items()
        )
    if boundary_invalid:
        turn.usage_invalid = True
        turn.codex_latest_boundary = None
    else:
        turn.codex_latest_boundary = (
            dict(total) if total and not invalid_total else None
        )
    if invalid_last or invalid_total:
        turn.usage_invalid = True
        return
    if last and total and not boundary_invalid:
        turn.codex_usage_snapshots.append((last, total))


def _finalize_codex_usage(
    turn: _RawTurn,
    *,
    previous_turn_total: dict[str, int] | None,
    has_previous_turn: bool,
) -> dict[str, int] | None:
    safe_boundary = (
        dict(turn.codex_latest_boundary)
        if turn.codex_latest_boundary is not None
        else None
    )
    if turn.usage_invalid or not turn.codex_usage_snapshots:
        return safe_boundary
    if (
        has_previous_turn
        and previous_turn_total is None
        and len(turn.codex_usage_snapshots) < 2
    ):
        return safe_boundary
    first_last, first_total = turn.codex_usage_snapshots[0]
    final_total = turn.codex_usage_snapshots[-1][1]
    if any(
        name in first_last
        and name in first_total
        and first_total[name] < first_last[name]
        for name in _TOKEN_FIELDS
    ):
        turn.usage_invalid = True
        return safe_boundary
    previous_total = first_total
    for _, total in turn.codex_usage_snapshots[1:]:
        if any(
            name in previous_total and name in total and total[name] < previous_total[name]
            for name in _TOKEN_FIELDS
        ):
            turn.usage_invalid = True
            return safe_boundary
        previous_total = total
    usage: dict[str, int] = {}
    for name in _TOKEN_FIELDS:
        if name not in first_last or name not in first_total or name not in final_total:
            continue
        if has_previous_turn and previous_turn_total is None:
            # The first valid cumulative observation re-establishes a boundary;
            # only subsequent growth can safely be assigned to this turn.
            delta = final_total[name] - first_total[name]
        elif has_previous_turn and name not in (previous_turn_total or {}):
            continue
        elif previous_turn_total is not None and previous_turn_total.get(name) == first_total[name]:
            # Codex can begin a turn by repeating the preceding turn's final
            # cumulative snapshot and ``last_token_usage``. In that shape the
            # last usage belongs to the preceding turn and must not be counted
            # again. When the cumulative value advanced or reset between turns,
            # retain the source-local first-snapshot calculation instead.
            delta = final_total[name] - first_total[name]
        else:
            delta = first_last[name] + final_total[name] - first_total[name]
        if delta < 0 or delta > 2**63 - 1:
            turn.usage_invalid = True
            return safe_boundary
        usage[name] = delta
    if usage:
        turn.token_usage = usage
        turn.token_usage_basis = "codex_turn_delta"
    return safe_boundary


def _record_claude_usage(
    turn: _RawTurn,
    response_id: str,
    value: object,
) -> dict[str, int] | None:
    if response_id in turn.claude_invalid_response_ids:
        return None
    usage, invalid = _token_usage(
        value,
        aliases={
            "input_tokens": "input_tokens",
            "cache_read_input_tokens": "cached_input_tokens",
            "cache_creation_input_tokens": "cache_write_input_tokens",
            "output_tokens": "output_tokens",
        },
    )
    if invalid:
        turn.usage_invalid = True
        turn.claude_invalid_response_ids.add(response_id)
        return None
    current = turn.claude_usage_by_response_id.get(response_id)
    if current is None:
        turn.claude_usage_by_response_id[response_id] = usage
    else:
        for name, count in usage.items():
            if name in current and current[name] != count:
                turn.usage_invalid = True
                turn.claude_invalid_response_ids.add(response_id)
                return None
            current[name] = count
    return usage


def _finalize_claude_usage(turn: _RawTurn) -> None:
    if turn.usage_invalid or not turn.claude_usage_by_response_id:
        return
    usage = {
        name: sum(item[name] for item in turn.claude_usage_by_response_id.values() if name in item)
        for name in _TOKEN_FIELDS
        if name != "total_tokens"
        and any(name in item for item in turn.claude_usage_by_response_id.values())
    }
    if not usage:
        return
    complete_responses = all(
        "input_tokens" in item and "output_tokens" in item
        for item in turn.claude_usage_by_response_id.values()
    )
    if complete_responses and turn.status is not ExecutionStatus.UNKNOWN:
        usage["total_tokens"] = sum(usage.values())
    if any(value > 2**63 - 1 for value in usage.values()):
        turn.usage_invalid = True
        return
    turn.token_usage = usage
    turn.token_usage_basis = "claude_provider_response_sum"


def _records(path: Path) -> tuple[list[dict[str, object]], int]:
    records: list[dict[str, object]] = []
    omitted_partial_lines = 0
    with path.open("rb") as handle:
        while raw := handle.readline(_MAX_LINE_BYTES + 1):
            if len(raw) > _MAX_LINE_BYTES:
                raise ValueError("history_line_too_large")
            if not raw.strip():
                continue
            if len(records) >= _MAX_EVENTS:
                raise ValueError("history_event_limit")
            try:
                row = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                # A live writer commonly leaves one incomplete final record.
                # Preserve every complete record and expose the omission; a
                # malformed newline-terminated record still rejects the file.
                if not raw.endswith((b"\n", b"\r")) and not handle.read(1):
                    omitted_partial_lines += 1
                    break
                raise ValueError("malformed_jsonl") from exc
            mapped = _mapping(row)
            if mapped is None:
                raise ValueError("malformed_record")
            records.append(mapped)
    return records, omitted_partial_lines


def _iter_paths(root: Path):
    if not root.is_dir() or root.is_symlink():
        return
    count = 0
    for path in root.rglob("*.jsonl"):
        if path.is_symlink() or not path.is_file():
            continue
        count += 1
        if count > _MAX_FILES:
            raise ValueError("history_file_limit")
        yield path


def _content_attributes(
    metadata: dict[str, Any],
    content: dict[str, Any],
    *,
    capture_content: bool,
    home: Path | None,
    omission_reason: str = "content_capture_disabled",
) -> tuple[dict[str, Any], PrivacyClassification, str | None]:
    if not content:
        return metadata, PrivacyClassification.METADATA, None
    if capture_content:
        return (
            metadata | {key: _safe_content(value, home=home) for key, value in content.items()},
            PrivacyClassification.REDACTED,
            None,
        )
    return metadata, PrivacyClassification.OMITTED, omission_reason


def _safe_content(value: object, *, home: Path | None) -> object:
    """Normalize local paths in every nested captured-content string."""
    if isinstance(value, str):
        home_text = str(home or Path.home())
        return value.replace(home_text, "~") if home_text and home_text != "/" else value
    if isinstance(value, list):
        return [_safe_content(item, home=home) for item in value]
    if isinstance(value, dict):
        return {
            key: _safe_content(item, home=home)
            for key, item in value.items()
            if isinstance(key, str)
        }
    return value


def _tool_name(payload: dict[str, object]) -> str:
    value = payload.get("name") or payload.get("tool_name")
    return _bounded_utf8(value, 256) if isinstance(value, str) and value else "unknown"


def _command_from_arguments(value: object) -> str:
    if isinstance(value, dict):
        command = value.get("cmd") or value.get("command")
        return command[:_MAX_CONTENT_CHARS] if isinstance(command, str) else ""
    if not isinstance(value, str):
        return ""
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return value[:_MAX_CONTENT_CHARS]
    return _command_from_arguments(decoded)


def _result_details(value: object) -> tuple[int | None, bool | None, str]:
    decoded = value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            return None, None, value[:_MAX_CONTENT_CHARS]
    if not isinstance(decoded, dict):
        return None, None, str(decoded)[:_MAX_CONTENT_CHARS]
    exit_code = decoded.get("exit_code")
    if (
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or abs(exit_code) > 2**31 - 1
    ):
        exit_code = None
    is_error = decoded.get("is_error")
    if not isinstance(is_error, bool):
        is_error = exit_code != 0 if exit_code is not None else None
    output = decoded.get("output") or decoded.get("result") or decoded.get("stdout") or ""
    return exit_code, is_error, str(output)[:_MAX_CONTENT_CHARS]


def _event(
    turn: _RawTurn,
    occurred_at: datetime | None,
    event_type: AgentEventType,
    provenance: str,
    attributes: dict[str, Any],
    *,
    status: ExecutionStatus = ExecutionStatus.UNKNOWN,
    has_content: bool = False,
    provider_response_id: str = "",
    response_text: str = "",
    legacy_response_text: str = "",
) -> None:
    if occurred_at is None:
        turn.events.append(
            _RawEvent(
                turn.started_at,
                AgentEventType.CONTEXT,
                ExecutionStatus.UNKNOWN,
                "verdict:missing_event_timestamp",
                {
                    "name": "source_event_omitted",
                    "source": event_type.value,
                    "available": False,
                },
            )
        )
        return
    turn.events.append(
        _RawEvent(
            occurred_at, event_type, status, provenance, attributes, has_content,
            provider_response_id, response_text, legacy_response_text,
        )
    )


def _record_partial_source_line(
    turns: list[_RawTurn], omitted_partial_lines: int
) -> None:
    if not omitted_partial_lines or not turns:
        return
    target = turns[-1]
    _event(
        target,
        target.ended_at or target.started_at,
        AgentEventType.CONTEXT,
        "verdict:partial_source_line",
        {
            "name": "source_lines_omitted",
            "source": str(omitted_partial_lines),
            "available": False,
        },
    )


def _parse_codex(path: Path, *, home: Path | None) -> _ParsedHistory:
    records, omitted_partial_lines = _records(path)
    session_id = ""
    parent_session_id: str | None = None
    logical_session_id: str | None = None
    version = ""
    active: _RawTurn | None = None
    turns: list[_RawTurn] = []
    calls: dict[str, tuple[str, str]] = {}
    for row in records:
        payload = _mapping(row.get("payload"))
        outer = row.get("type")
        occurred_at = _time(row.get("timestamp"))
        if outer == "session_meta" and payload is not None and not session_id:
            # Current Codex histories use ``id`` for the physical run and may
            # reuse ``session_id`` across its root and child histories.
            raw_id = payload.get("id") or payload.get("session_id")
            if not isinstance(raw_id, str) or not raw_id:
                raise ValueError("missing_session_id")
            session_id = raw_id
            raw_logical_id = payload.get("session_id")
            if raw_logical_id is not None:
                if not isinstance(raw_logical_id, str) or not raw_logical_id:
                    raise ValueError("invalid_logical_session_id")
                logical_session_id = raw_logical_id
            parent_id = payload.get("parent_thread_id") or payload.get("forked_from_id")
            if parent_id is not None:
                if not isinstance(parent_id, str) or not parent_id:
                    raise ValueError("invalid_parent_session_id")
                if parent_id == session_id:
                    raise ValueError("run cannot be its own parent")
                parent_session_id = parent_id
            version = str(payload.get("cli_version") or "")[:256]
            continue
        if not session_id or payload is None:
            continue
        inner = payload.get("type")
        if outer == "event_msg" and inner == "task_started":
            if active is not None:
                turns.append(active)
            turn_id = payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id or occurred_at is None:
                active = None
                continue
            active = _RawTurn(turn_id, occurred_at)
            calls = {}
        elif active is None:
            continue
        elif outer == "turn_context" and isinstance(payload.get("model"), str):
            _event(
                active,
                occurred_at,
                AgentEventType.CONTEXT,
                "codex:turn_context",
                {"name": "model_configuration", "source": payload["model"], "available": True},
            )
            _event(
                active,
                occurred_at,
                AgentEventType.CONTEXT,
                "codex:model_call_boundary",
                {
                    "name": "model_call_boundaries",
                    "source": "not_exposed_by_codex_history",
                    "available": False,
                },
            )
        elif outer == "response_item" and inner in {"custom_tool_call", "function_call"}:
            name = _tool_name(payload)
            call_id = _bounded_utf8(
                str(payload.get("call_id") or payload.get("id") or ""), 256
            )
            arguments = payload.get("arguments") or payload.get("input")
            command = _command_from_arguments(arguments)
            calls[call_id] = (name, command)
            _event(
                active,
                occurred_at,
                AgentEventType.TOOL_CALL,
                "codex:response_item",
                {"tool_name": name, "call_id": call_id, "arguments": arguments},
                has_content=True,
            )
            if name.lower() in _COMMAND_TOOLS:
                _event(
                    active,
                    occurred_at,
                    AgentEventType.COMMAND,
                    "codex:response_item",
                    {"command": command},
                    has_content=True,
                )
        elif outer == "response_item" and inner in {
            "custom_tool_call_output",
            "function_call_output",
        }:
            call_id = _bounded_utf8(str(payload.get("call_id") or ""), 256)
            name, _ = calls.get(call_id, ("unknown", ""))
            raw_output = payload.get("output")
            exit_code, is_error, output = _result_details(raw_output)
            status = (
                ExecutionStatus.FAILED
                if is_error is True
                else ExecutionStatus.COMPLETED
                if is_error is False
                else ExecutionStatus.UNKNOWN
            )
            _event(
                active,
                occurred_at,
                AgentEventType.TOOL_RESULT,
                "codex:response_item",
                {"tool_name": name, "call_id": call_id, "is_error": is_error, "result": output},
                status=status,
                has_content=True,
            )
            if name.lower() in _COMMAND_TOOLS:
                _event(
                    active,
                    occurred_at,
                    AgentEventType.COMMAND,
                    "codex:response_item_output",
                    {"exit_code": exit_code, "stdout": output},
                    status=status,
                    has_content=True,
                )
        elif outer == "event_msg" and inner == "user_message":
            text = _safe_text(payload.get("message"), home=home)
            if text.value:
                appended = _append_turn_text(
                    active.request, active.legacy_request, text
                )
                active.request = appended.value
                active.legacy_request = appended.legacy_value
                active.request_truncated = active.request_truncated or appended.truncated
        elif (
            outer == "event_msg"
            and inner == "agent_message"
            and payload.get("phase") == "final_answer"
        ):
            response = _safe_text(payload.get("message"), home=home)
            active.response = response.value
            active.legacy_response = response.legacy_value
            active.response_truncated = response.truncated
        elif outer == "event_msg" and inner == "token_count":
            _record_codex_usage(active, payload.get("info"))
        elif outer == "event_msg" and inner == "turn_aborted":
            active.status = ExecutionStatus.CANCELLED
            active.ended_at = occurred_at
            turns.append(active)
            active = None
        elif outer == "event_msg" and inner == "task_complete":
            if payload.get("turn_id") != active.source_id:
                continue
            response = _safe_text(payload.get("last_agent_message"), home=home)
            if response.value:
                active.response = response.value
                active.legacy_response = response.legacy_value
                active.response_truncated = response.truncated
            active.status = ExecutionStatus.COMPLETED
            active.ended_at = _time(payload.get("completed_at")) or occurred_at
            turns.append(active)
            active = None
    if active is not None:
        turns.append(active)
    if not session_id:
        raise ValueError("unsupported_history")
    previous_turn_total: dict[str, int] | None = None
    for turn_index, turn in enumerate(turns):
        previous_turn_total = _finalize_codex_usage(
            turn,
            previous_turn_total=previous_turn_total,
            has_previous_turn=turn_index > 0,
        )
    _record_partial_source_line(turns, omitted_partial_lines)
    return _ParsedHistory(
        session_id, version, turns, parent_session_id, logical_session_id
    )


def _parse_claude(path: Path, *, home: Path | None) -> _ParsedHistory:
    records, omitted_partial_lines = _records(path)
    session_id = ""
    parent_session_id: str | None = None
    version = ""
    active: _RawTurn | None = None
    turns: list[_RawTurn] = []
    model_events: dict[str, _RawEvent] = {}
    tool_names: dict[str, str] = {}
    seen_tool_calls: set[str] = set()
    for row in records:
        if row.get("isMeta") is True or row.get("type") not in {"user", "assistant"}:
            continue
        message = _mapping(row.get("message"))
        occurred_at = _time(row.get("timestamp"))
        if message is None:
            continue
        if not session_id and isinstance(row.get("sessionId"), str):
            root_session_id = str(row["sessionId"])
            if row.get("isSidechain") is True:
                agent_id = row.get("agentId")
                if not isinstance(agent_id, str) or not agent_id:
                    raise ValueError("missing_child_agent_id")
                session_id = agent_id
                parent_session_id = root_session_id
            else:
                session_id = root_session_id
            version = str(row.get("version") or "")[:256]
        is_human = (
            row.get("type") == "user"
            and row.get("sourceToolAssistantUUID") is None
            and row.get("toolUseResult") is None
            and not any(
                (_mapping(item) or {}).get("type") == "tool_result"
                for item in (
                    message.get("content") if isinstance(message.get("content"), list) else []
                )
            )
        )
        if is_human:
            if active is not None:
                turns.append(active)
            source_id = row.get("uuid")
            prompt = _message_text(message, home=home)
            if not isinstance(source_id, str) or occurred_at is None:
                active = None
                continue
            active = _RawTurn(
                source_id,
                occurred_at,
                request=prompt.value,
                legacy_request=prompt.legacy_value,
                request_truncated=prompt.truncated,
            )
            model_events = {}
            tool_names = {}
            seen_tool_calls = set()
            continue
        if active is None:
            continue
        content = message.get("content")
        if row.get("type") == "user" and isinstance(content, list):
            for item in content:
                block = _mapping(item)
                if block is None or block.get("type") != "tool_result":
                    continue
                call_id = _bounded_utf8(str(block.get("tool_use_id") or ""), 256)
                name = tool_names.get(call_id, "unknown")
                is_error = (
                    block.get("is_error") if isinstance(block.get("is_error"), bool) else None
                )
                status = (
                    ExecutionStatus.FAILED
                    if is_error is True
                    else ExecutionStatus.COMPLETED
                    if is_error is False
                    else ExecutionStatus.UNKNOWN
                )
                result = block.get("content")
                _event(
                    active,
                    occurred_at,
                    AgentEventType.TOOL_RESULT,
                    "claude:tool_result",
                    {"tool_name": name, "call_id": call_id, "is_error": is_error, "result": result},
                    status=status,
                    has_content=True,
                )
            continue
        if row.get("type") != "assistant" or not isinstance(content, list):
            continue
        message_id = _provider_response_id(message.get("id"))
        model = message.get("model")
        response_text = _message_text(message, home=home)
        model_event = model_events.get(message_id) if message_id is not None else None
        if message_id is not None and model_event is None:
            usage = _record_claude_usage(active, message_id, message.get("usage")) or {}
            _event(
                active,
                occurred_at,
                AgentEventType.MODEL_CALL,
                "claude:assistant_message",
                {
                    "provider": "anthropic",
                    "request_model": model or "unknown",
                    "response_model": model or "unknown",
                    "operation": "chat",
                    "finish_reason": message.get("stop_reason") or "",
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                },
                status=ExecutionStatus.COMPLETED,
                provider_response_id=message_id,
                response_text=response_text.value,
                legacy_response_text=response_text.legacy_value,
            )
            if occurred_at is not None:
                model_events[message_id] = active.events[-1]
        elif model_event is not None:
            usage = _record_claude_usage(active, message_id, message.get("usage"))
            if usage is None:
                for name in ("input_tokens", "output_tokens"):
                    model_event.attributes[name] = None
            else:
                for name in ("input_tokens", "output_tokens"):
                    if name in usage:
                        model_event.attributes[name] = usage[name]
            if response_text.value and not model_event.response_text:
                model_event.response_text = response_text.value
                model_event.legacy_response_text = response_text.legacy_value
        for item in content:
            block = _mapping(item)
            if block is None:
                continue
            if block.get("type") == "tool_use":
                call_id = _bounded_utf8(str(block.get("id") or ""), 256)
                if call_id and call_id in seen_tool_calls:
                    continue
                if call_id:
                    seen_tool_calls.add(call_id)
                name = _tool_name(block)
                tool_names[call_id] = name
                arguments = block.get("input")
                command = _command_from_arguments(arguments)
                _event(
                    active,
                    occurred_at,
                    AgentEventType.TOOL_CALL,
                    "claude:tool_use",
                    {"tool_name": name, "call_id": call_id, "arguments": arguments},
                    has_content=True,
                )
                if name.lower() in _COMMAND_TOOLS:
                    _event(
                        active,
                        occurred_at,
                        AgentEventType.COMMAND,
                        "claude:tool_use",
                        {"command": command},
                        has_content=True,
                    )
        if message.get("stop_reason") == "end_turn":
            if response_text.value:
                active.response = response_text.value
                active.legacy_response = response_text.legacy_value
                active.response_truncated = response_text.truncated
            active.status = ExecutionStatus.COMPLETED
            if active.ended_at is None:
                active.ended_at = occurred_at
    if active is not None:
        turns.append(active)
    if not session_id:
        raise ValueError("unsupported_history")
    for turn in turns:
        _finalize_claude_usage(turn)
    _record_partial_source_line(turns, omitted_partial_lines)
    return _ParsedHistory(
        session_id, version, turns, parent_session_id, parent_session_id or session_id
    )


def _redacted_turn_text(value: str, *, truncated: bool) -> _BoundedText:
    if not value:
        return _BoundedText("")
    encoded = value.encode("utf-8")
    return _BoundedText(
        _bounded_utf8(value, _MAX_TURN_CONTENT_BYTES),
        truncated or len(encoded) > _MAX_TURN_CONTENT_BYTES,
    )


def _turn_source_identity(bundle: AgentRunBundle, raw: _RawTurn) -> str:
    """Scope child-local turn IDs without changing published root identities."""
    return (
        f"{bundle.run.run_id}:{raw.source_id}"
        if bundle.run.parent_run_id is not None
        else raw.source_id
    )


def _bundle(
    *,
    source_kind: str,
    source_scope: str,
    path: Path,
    session_id: str,
    parent_session_id: str | None,
    logical_session_id: str | None,
    version: str,
    raw_turns: list[_RawTurn],
    tenant_id: str,
    capture_content: bool,
    home: Path | None,
    content_omission_reason: str = "content_capture_disabled",
) -> AgentRunBundle:
    if not raw_turns:
        raise ValueError("no_root_turns")
    source_session_id = stable_evidence_id("session", source_kind, source_scope, session_id)
    run_id = stable_evidence_id("run", source_kind, source_scope, session_id)
    logical_run_session_id = stable_evidence_id(
        "session",
        source_kind,
        source_scope,
        logical_session_id or parent_session_id or session_id,
    )
    parent_run_id = (
        stable_evidence_id("run", source_kind, source_scope, parent_session_id)
        if parent_session_id is not None
        else None
    )
    observed_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    session = SourceSession(
        source_session_id=source_session_id,
        tenant_id=tenant_id,
        source_kind=source_kind,
        source_locator_hash=hashlib.sha256(str(path.resolve()).encode()).hexdigest(),
        started_at=min(turn.started_at for turn in raw_turns),
        observed_at=observed_at,
    )
    run = AgentRun(
        run_id=run_id,
        source_session_id=source_session_id,
        tenant_id=tenant_id,
        started_at=session.started_at,
        status=ExecutionStatus.UNKNOWN,
        agent_name=source_kind,
        agent_version=version,
        session_id=logical_run_session_id,
        parent_run_id=parent_run_id,
        service_name=source_kind,
    )
    total_source_events = sum(len(turn.events) for turn in raw_turns)
    stored_event_count = min(total_source_events, _MAX_STORED_EVENTS - 1)
    omitted_event_count = total_source_events - stored_event_count
    turns: list[AgentTurn] = []
    events: list[AgentEvent] = []
    remaining_events = stored_event_count
    for turn_sequence, raw in enumerate(raw_turns):
        source_turn_id = (
            f"{run_id}:{raw.source_id}" if parent_run_id is not None else raw.source_id
        )
        turn_id = stable_evidence_id("turn", source_kind, source_scope, source_turn_id)
        request = _redacted_turn_text(
            raw.request, truncated=raw.request_truncated
        ) if capture_content else _BoundedText("")
        response = _redacted_turn_text(
            raw.response, truncated=raw.response_truncated
        ) if capture_content else _BoundedText("")
        turns.append(
            AgentTurn(
                turn_id=turn_id,
                run_id=run_id,
                sequence=turn_sequence,
                started_at=raw.started_at,
                ended_at=raw.ended_at,
                status=raw.status,
                user_request_redacted=request.value or None,
                final_response_redacted=response.value or None,
                request_state=(
                    EvidenceState.PRESENT
                    if capture_content and raw.request
                    else EvidenceState.MISSING
                    if capture_content
                    else EvidenceState.NOT_CAPTURED
                ),
                response_state=(
                    EvidenceState.PRESENT
                    if capture_content and raw.response
                    else EvidenceState.MISSING
                    if capture_content
                    else EvidenceState.NOT_CAPTURED
                ),
                input_tokens=raw.token_usage.get("input_tokens"),
                cached_input_tokens=raw.token_usage.get("cached_input_tokens"),
                cache_write_input_tokens=raw.token_usage.get("cache_write_input_tokens"),
                output_tokens=raw.token_usage.get("output_tokens"),
                reasoning_output_tokens=raw.token_usage.get("reasoning_output_tokens"),
                total_tokens=raw.token_usage.get("total_tokens"),
                token_usage_basis=raw.token_usage_basis or None,
                request_truncated=request.truncated if request.value else False,
                response_truncated=response.truncated if response.value else False,
            )
        )
        selected_events = raw.events[:remaining_events]
        remaining_events -= len(selected_events)
        for event_sequence, raw_event in enumerate(selected_events):
            metadata = dict(raw_event.attributes)
            content: dict[str, Any] = {}
            if raw_event.has_content:
                for key in ("arguments", "result", "command", "stdout", "stderr", "output", "text"):
                    if key in metadata:
                        content[key] = metadata.pop(key)
            attributes, privacy, omission = _content_attributes(
                metadata,
                content,
                capture_content=capture_content,
                home=home,
                omission_reason=content_omission_reason,
            )
            attributes = {
                key: redact_structure(value)
                for key, value in attributes.items()
            }
            event_id = stable_evidence_id(
                "event",
                source_kind,
                source_scope,
                f"{source_turn_id}:{event_sequence}:{raw_event.provenance}",
            )
            try:
                trace_id = (
                    stable_evidence_id("trace", source_kind, source_scope, event_id)
                    if raw_event.event_type is AgentEventType.MODEL_CALL
                    and raw_event.provider_response_id
                    else None
                )
                event = AgentEvent(
                    event_id=event_id,
                    turn_id=turn_id,
                    sequence=event_sequence,
                    occurred_at=raw_event.occurred_at,
                    event_type=raw_event.event_type,
                    status=raw_event.status,
                    provenance=raw_event.provenance,
                    attributes=attributes,
                    privacy_classification=privacy,
                    omission_reason=omission,
                    trace_id=trace_id,
                )
            except ValueError:
                if not capture_content or not raw_event.has_content:
                    raise
                event = AgentEvent(
                    event_id=event_id,
                    turn_id=turn_id,
                    sequence=event_sequence,
                    occurred_at=raw_event.occurred_at,
                    event_type=raw_event.event_type,
                    status=raw_event.status,
                    provenance=raw_event.provenance,
                    attributes={
                        key: redact_structure(value)
                        for key, value in metadata.items()
                    },
                    privacy_classification=PrivacyClassification.OMITTED,
                    omission_reason="content_exceeded_evidence_contract",
                    trace_id=trace_id,
                )
            events.append(event)
        if omitted_event_count and remaining_events == 0 and len(events) == stored_event_count:
            events.append(
                AgentEvent(
                    event_id=stable_evidence_id(
                        "event", source_kind, source_scope, f"{source_turn_id}:capture-limit"
                    ),
                    turn_id=turn_id,
                    sequence=len(selected_events),
                    occurred_at=raw.ended_at or raw.started_at,
                    event_type=AgentEventType.CONTEXT,
                    status=ExecutionStatus.UNKNOWN,
                    provenance="verdict:capture_limit",
                    attributes={
                        "name": "source_events_omitted",
                        "source": str(omitted_event_count),
                        "available": False,
                    },
                    privacy_classification=PrivacyClassification.METADATA,
                )
            )
            omitted_event_count = 0
    bundle = AgentRunBundle(session=session, run=run, turns=tuple(turns), events=tuple(events))
    return bundle


def _linked_traces(
    bundle: AgentRunBundle,
    raw_turns: list[_RawTurn],
    *,
    source_kind: str,
    source_scope: str,
) -> list[Trace]:
    """Project only explicit provider model-call boundaries into Trace rows."""
    raw_by_event_id: dict[str, tuple[_RawTurn, _RawEvent]] = {}
    for raw_turn in raw_turns:
        source_turn_id = _turn_source_identity(bundle, raw_turn)
        for event_sequence, raw_event in enumerate(raw_turn.events):
            event_id = stable_evidence_id(
                "event",
                source_kind,
                source_scope,
                f"{source_turn_id}:{event_sequence}:{raw_event.provenance}",
            )
            raw_by_event_id[event_id] = (raw_turn, raw_event)
    turns = {turn.turn_id: turn for turn in bundle.turns}
    traces: list[Trace] = []
    for event in bundle.events:
        if event.event_type is not AgentEventType.MODEL_CALL or event.trace_id is None:
            continue
        raw_pair = raw_by_event_id.get(event.event_id)
        turn = turns.get(event.turn_id)
        if raw_pair is None or turn is None:
            continue
        raw_turn, raw_event = raw_pair
        content_present = turn.request_state is EvidenceState.PRESENT
        attributes = event.attributes
        request_model = str(attributes.get("request_model") or "")
        response_model = str(attributes.get("response_model") or "")
        input_tokens = attributes.get("input_tokens")
        output_tokens = attributes.get("output_tokens")
        request = raw_turn.request if content_present and raw_turn.request else None
        response = (
            raw_event.response_text
            if content_present and raw_event.response_text else None
        )
        messages = []
        if request is not None:
            messages.append({"role": "user", "content": request})
        if response is not None:
            messages.append({"role": "assistant", "content": response})
        traces.append(Trace(
            trace_id=event.trace_id,
            started_at=event.occurred_at,
            ended_at=event.occurred_at,
            provider=str(attributes.get("provider") or ""),
            operation=Operation.CHAT,
            request_model=request_model,
            response_model=response_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=str(attributes.get("finish_reason") or "") or None,
            prompt_redacted=request,
            response_redacted=response,
            raw_messages=messages or None,
            tenant_id=bundle.run.tenant_id,
            session_id=bundle.session.source_session_id,
            tags={
                "verdict.source": source_kind,
                "verdict.workload": "agent",
                "verdict.agent_run_id": bundle.run.run_id,
                "verdict.agent_event_id": event.event_id,
                "verdict.input_evidence": "turn_request_only",
                "verdict.time_evidence": "response_observed_at",
            },
        ))
    return traces


def _capture_text_compatibility(
    bundle: AgentRunBundle,
    raw_turns: list[_RawTurn],
    traces: list[Trace],
    *,
    source_kind: str,
    source_scope: str,
    capture_content: bool,
) -> _LegacyLocalTextUpgrade | None:
    """Build redacted, non-persistent a17 projections for exact upgrade checks."""
    if not capture_content:
        return None

    def boundary_projection(legacy: str, incoming: str) -> str | None:
        projected = redact(legacy) if legacy else None
        if (
            projected is None
            or not incoming
            or projected == incoming
            or incoming.startswith(projected)
            or projected.startswith(incoming)
        ):
            return None
        return projected

    turn_previews: dict[str, tuple[str | None, str | None]] = {}
    raw_by_event_id: dict[str, tuple[_RawTurn, _RawEvent]] = {}
    for raw_turn in raw_turns:
        source_turn_id = _turn_source_identity(bundle, raw_turn)
        turn_id = stable_evidence_id("turn", source_kind, source_scope, source_turn_id)
        preview = (
            boundary_projection(raw_turn.legacy_request, raw_turn.request),
            boundary_projection(raw_turn.legacy_response, raw_turn.response),
        )
        if preview != (None, None):
            turn_previews[turn_id] = preview
        for event_sequence, raw_event in enumerate(raw_turn.events):
            event_id = stable_evidence_id(
                "event",
                source_kind,
                source_scope,
                f"{source_turn_id}:{event_sequence}:{raw_event.provenance}",
            )
            raw_by_event_id[event_id] = (raw_turn, raw_event)
    trace_previews = {}
    for trace in traces:
        raw_pair = raw_by_event_id.get(trace.tags.get("verdict.agent_event_id", ""))
        if raw_pair is None:
            continue
        raw_turn, raw_event = raw_pair
        preview = (
            boundary_projection(raw_turn.legacy_request, raw_turn.request),
            boundary_projection(
                raw_event.legacy_response_text,
                raw_event.response_text,
            ),
        )
        if preview != (None, None):
            trace_previews[trace.trace_id] = preview
    if not turn_previews and not trace_previews:
        return None
    return _LegacyLocalTextUpgrade(
        version="a17-truncate-before-redact-v1",
        turn_previews=turn_previews,
        trace_previews=trace_previews,
    )


def capture_local_agents(
    storage: Storage,
    *,
    tenant_id: str,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
    capture_content: bool = True,
    home: Path | None = None,
) -> LocalCaptureSummary:
    """Rescan selected local roots into the canonical agent capture boundary."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    sources = (("claude-code", claude_root, _parse_claude), ("codex", codex_root, _parse_codex))
    summary = LocalCaptureSummary()
    capture_service = AgentCaptureService(storage)
    for source_kind, root, parser in sources:
        if root is None:
            continue
        source_scope = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
        try:
            paths = _iter_paths(root)
            for path in paths or ():
                summary.files += 1
                try:
                    parsed = parser(path, home=home)
                    bundle = _bundle(
                        source_kind=source_kind,
                        source_scope=source_scope,
                        path=path,
                        session_id=parsed.session_id,
                        parent_session_id=parsed.parent_session_id,
                        logical_session_id=parsed.logical_session_id,
                        version=parsed.version,
                        raw_turns=parsed.turns,
                        tenant_id=tenant_id,
                        capture_content=capture_content,
                        home=home,
                    )
                    traces = _linked_traces(
                        bundle, parsed.turns, source_kind=source_kind,
                        source_scope=source_scope,
                    )
                    capture_service._capture_local_history(
                        bundle,
                        traces=traces,
                        legacy_text_upgrade=_capture_text_compatibility(
                            bundle,
                            parsed.turns,
                            traces,
                            source_kind=source_kind,
                            source_scope=source_scope,
                            capture_content=capture_content,
                        ),
                    )
                except (OSError, ValueError) as exc:
                    summary.add_skip(str(exc) or type(exc).__name__)
                    continue
                summary.stored += 1
        except ValueError as exc:
            summary.add_skip(str(exc))
    return summary
