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

from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceBundleTooLarge,
    EvidenceState,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
    agent_run_bundle_to_json,
    stable_evidence_id,
)
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


@dataclass
class _RawTurn:
    source_id: str
    started_at: datetime
    ended_at: datetime | None = None
    status: ExecutionStatus = ExecutionStatus.UNKNOWN
    request: str = ""
    response: str = ""
    events: list[_RawEvent] = field(default_factory=list)


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


def _safe_text(value: object, *, home: Path | None) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value
    while match := _AMBIENT_BLOCK.match(cleaned):
        cleaned = cleaned[match.end() :]
    cleaned = _REQUEST_HEADING.sub("", cleaned)
    home_text = str(home or Path.home())
    if home_text and home_text != "/":
        cleaned = cleaned.replace(home_text, "~")
    return _bounded_utf8(cleaned.strip()[:_MAX_CONTENT_CHARS], 4_000)


def _message_text(message: dict[str, object], *, home: Path | None) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return _safe_text(content, home=home)
    if not isinstance(content, list):
        return ""
    return "\n".join(
        text
        for item in content
        if (block := _mapping(item)) is not None
        and block.get("type") == "text"
        and (text := _safe_text(block.get("text"), home=home))
    )[:_MAX_CONTENT_CHARS]


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
            provider_response_id, response_text,
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


def _parse_codex(path: Path, *, home: Path | None) -> tuple[str, str, list[_RawTurn]]:
    records, omitted_partial_lines = _records(path)
    session_id = ""
    version = ""
    active: _RawTurn | None = None
    turns: list[_RawTurn] = []
    calls: dict[str, tuple[str, str]] = {}
    for row in records:
        payload = _mapping(row.get("payload"))
        outer = row.get("type")
        occurred_at = _time(row.get("timestamp"))
        if outer == "session_meta" and payload is not None and not session_id:
            source = payload.get("source")
            if (
                payload.get("parent_thread_id")
                or payload.get("forked_from_id")
                or isinstance(source, dict)
            ):
                raise ValueError("child_session")
            raw_id = payload.get("session_id") or payload.get("id")
            if not isinstance(raw_id, str) or not raw_id:
                raise ValueError("missing_session_id")
            session_id = raw_id
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
            if text:
                active.request = "\n\n".join(filter(None, (active.request, text)))[
                    :_MAX_CONTENT_CHARS
                ]
        elif (
            outer == "event_msg"
            and inner == "agent_message"
            and payload.get("phase") == "final_answer"
        ):
            active.response = _safe_text(payload.get("message"), home=home)
        elif outer == "event_msg" and inner == "turn_aborted":
            active.status = ExecutionStatus.CANCELLED
            active.ended_at = occurred_at
            turns.append(active)
            active = None
        elif outer == "event_msg" and inner == "task_complete":
            if payload.get("turn_id") != active.source_id:
                continue
            active.response = (
                _safe_text(payload.get("last_agent_message"), home=home) or active.response
            )
            active.status = ExecutionStatus.COMPLETED
            active.ended_at = _time(payload.get("completed_at")) or occurred_at
            turns.append(active)
            active = None
    if active is not None:
        turns.append(active)
    if not session_id:
        raise ValueError("unsupported_history")
    _record_partial_source_line(turns, omitted_partial_lines)
    return session_id, version, turns


def _parse_claude(path: Path, *, home: Path | None) -> tuple[str, str, list[_RawTurn]]:
    records, omitted_partial_lines = _records(path)
    session_id = ""
    version = ""
    active: _RawTurn | None = None
    turns: list[_RawTurn] = []
    seen_messages: set[str] = set()
    tool_names: dict[str, str] = {}
    for row in records:
        if (
            row.get("isSidechain") is True
            or row.get("isMeta") is True
            or row.get("type") not in {"user", "assistant"}
        ):
            continue
        message = _mapping(row.get("message"))
        occurred_at = _time(row.get("timestamp"))
        if message is None:
            continue
        if not session_id and isinstance(row.get("sessionId"), str):
            session_id = str(row["sessionId"])
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
            active = _RawTurn(source_id, occurred_at, request=prompt)
            seen_messages = set()
            tool_names = {}
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
        message_id = message.get("id")
        model = message.get("model")
        if isinstance(message_id, str) and message_id not in seen_messages:
            seen_messages.add(message_id)
            usage = _mapping(message.get("usage")) or {}
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
                response_text=_message_text(message, home=home),
            )
        for item in content:
            block = _mapping(item)
            if block is None:
                continue
            if block.get("type") == "tool_use":
                call_id = _bounded_utf8(str(block.get("id") or ""), 256)
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
            active.response = _message_text(message, home=home)
            active.status = ExecutionStatus.COMPLETED
            active.ended_at = occurred_at
    if active is not None:
        turns.append(active)
    if not session_id:
        raise ValueError("unsupported_history")
    _record_partial_source_line(turns, omitted_partial_lines)
    return session_id, version, turns


def _bundle(
    *,
    source_kind: str,
    source_scope: str,
    path: Path,
    session_id: str,
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
    )
    total_source_events = sum(len(turn.events) for turn in raw_turns)
    stored_event_count = min(total_source_events, _MAX_STORED_EVENTS - 1)
    omitted_event_count = total_source_events - stored_event_count
    turns: list[AgentTurn] = []
    events: list[AgentEvent] = []
    remaining_events = stored_event_count
    for turn_sequence, raw in enumerate(raw_turns):
        turn_id = stable_evidence_id("turn", source_kind, source_scope, raw.source_id)
        turns.append(
            AgentTurn(
                turn_id=turn_id,
                run_id=run_id,
                sequence=turn_sequence,
                started_at=raw.started_at,
                ended_at=raw.ended_at,
                status=raw.status,
                user_request_redacted=(
                    redact(raw.request) if capture_content and raw.request else None
                ),
                final_response_redacted=(
                    redact(raw.response) if capture_content and raw.response else None
                ),
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
                f"{raw.source_id}:{event_sequence}:{raw_event.provenance}",
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
                        "event", source_kind, source_scope, f"{raw.source_id}:capture-limit"
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
    try:
        agent_run_bundle_to_json(bundle)
    except EvidenceBundleTooLarge:
        if not capture_content:
            raise
        return _bundle(
            source_kind=source_kind,
            source_scope=source_scope,
            path=path,
            session_id=session_id,
            version=version,
            raw_turns=raw_turns,
            tenant_id=tenant_id,
            capture_content=False,
            home=home,
            content_omission_reason="content_exceeded_bundle_limit",
        )
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
        for event_sequence, raw_event in enumerate(raw_turn.events):
            event_id = stable_evidence_id(
                "event",
                source_kind,
                source_scope,
                f"{raw_turn.source_id}:{event_sequence}:{raw_event.provenance}",
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
        request = redact(raw_turn.request) if content_present and raw_turn.request else None
        response = (
            redact(raw_event.response_text)
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


def capture_local_agents(
    storage: Storage,
    *,
    tenant_id: str,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
    capture_content: bool = True,
    home: Path | None = None,
) -> LocalCaptureSummary:
    """Rescan selected local roots and atomically replace normalized sessions."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    sources = (("claude-code", claude_root, _parse_claude), ("codex", codex_root, _parse_codex))
    summary = LocalCaptureSummary()
    for source_kind, root, parser in sources:
        if root is None:
            continue
        source_scope = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
        try:
            paths = _iter_paths(root)
            for path in paths or ():
                summary.files += 1
                try:
                    session_id, version, turns = parser(path, home=home)
                    bundle = _bundle(
                        source_kind=source_kind,
                        source_scope=source_scope,
                        path=path,
                        session_id=session_id,
                        version=version,
                        raw_turns=turns,
                        tenant_id=tenant_id,
                        capture_content=capture_content,
                        home=home,
                    )
                    # Trace rows are written before the evidence link. A crash
                    # can therefore leave a recoverable unlinked source Trace,
                    # but can never leave an AgentEvent pointing at a missing
                    # Trace. Deterministic IDs make the next full rescan repair
                    # the projection idempotently.
                    for trace in _linked_traces(
                        bundle, turns, source_kind=source_kind, source_scope=source_scope
                    ):
                        storage.insert_trace(trace)
                    storage.replace_agent_run_bundle(bundle)
                except (OSError, ValueError) as exc:
                    summary.add_skip(str(exc) or type(exc).__name__)
                    continue
                summary.stored += 1
        except ValueError as exc:
            summary.add_skip(str(exc))
    return summary
