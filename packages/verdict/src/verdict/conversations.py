"""Bounded, redacted conversation snapshots from Voice telemetry."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timezone

from verdict.redaction import redact
from verdict.telemetry.model import ImportContext, safe_routing_id
from verdict.telemetry.normalize import first, parse_datetime

MAX_MESSAGES = 1_000
MAX_CONTENT_BYTES = 65_536
MAX_CONVERSATION_BYTES = 512_000
_MESSAGE_BUDGET = MAX_CONVERSATION_BYTES - 4_096
_USER = {"caller", "customer", "human", "user"}
_ASSISTANT = {"agent", "ai", "assistant", "bot"}
_CLOSED = {"complete", "premature", "user_stop", "technical_failure", "handoff"}
_ISSUES = {
    "malformed_turn", "unsupported_turn", "invalid_message_content",
    "unknown_message_status", "truncated_transcript", "unknown_end_status",
    "invalid_end_time",
}
_LABEL_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _time(value: object) -> str | None:
    parsed = parse_datetime(value)
    return parsed.astimezone(timezone.utc).isoformat() if parsed is not None else None


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("invalid conversation text")
    result = redact(value)
    if result is None or len(result.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("conversation text exceeds limit")
    return result


def validate_conversation(value: dict) -> dict:
    """Return only allowed fields, with redaction and a digest of stored evidence."""
    if not isinstance(value, dict):
        raise ValueError("conversation must be an object")
    for field in ("id", "tenant_id", "source_scope"):
        item = value.get(field)
        if not isinstance(item, str) or not item or len(item.encode("utf-8")) > 256:
            raise ValueError(f"invalid {field}")
    if len(value["id"]) != 32 or any(c not in "0123456789abcdef" for c in value["id"]):
        raise ValueError("invalid conversation id")
    if len(value["source_scope"]) != 16 or any(
        c not in "0123456789abcdef" for c in value["source_scope"]
    ):
        raise ValueError("invalid source scope")
    if value["tenant_id"] != "__verdict_local__" and safe_routing_id(value["tenant_id"]) is None:
        raise ValueError("invalid tenant")
    source_time = value.get("event_at")
    if source_time is not None and _time(source_time) is None:
        raise ValueError("invalid event time")
    raw_messages = value.get("messages")
    if not isinstance(raw_messages, list) or not 1 <= len(raw_messages) <= MAX_MESSAGES:
        raise ValueError("invalid message count")
    messages = []
    for message in raw_messages:
        if not isinstance(message, dict) or message.get("role") not in {
            "user",
            "assistant",
            "system",
        }:
            raise ValueError("invalid message role")
        status = message.get("status", "completed")
        if status not in {"completed", "interrupted", "unknown"}:
            raise ValueError("invalid message status")
        messages.append(
            {"role": message["role"], "content": _text(message.get("content")), "status": status}
        )
    issues = value.get("input_issues", [])
    if (
        not isinstance(issues, list)
        or len(issues) > 32
        or any(not isinstance(issue, str) or issue not in _ISSUES for issue in issues)
    ):
        raise ValueError("invalid input issues")
    end_status = value.get("end_status")
    if end_status not in _CLOSED | {"open", "unknown", "incomplete"}:
        raise ValueError("invalid end status")
    if issues:
        end_status = "incomplete"
    raw_labels = value.get("labels", {})
    if not isinstance(raw_labels, dict) or len(raw_labels) > 8:
        raise ValueError("invalid conversation labels")
    labels = {}
    for key, raw_label in raw_labels.items():
        if not isinstance(key, str) or _LABEL_KEY.fullmatch(key) is None:
            raise ValueError("invalid conversation label key")
        if not isinstance(raw_label, str) or len(raw_label.encode("utf-8")) > 128:
            raise ValueError("invalid conversation label value")
        label = _text(raw_label)
        if len(label.encode("utf-8")) > 128:
            raise ValueError("invalid conversation label value")
        labels[key] = label
    result = {
        "id": value["id"],
        "tenant_id": value["tenant_id"],
        "source_scope": value["source_scope"],
        "messages": messages,
        "event_at": _time(source_time),
        "end_status": end_status,
        "input_issues": sorted(set(issues)),
    }
    if labels:
        result["labels"] = {key: labels[key] for key in sorted(labels)}
    payload = _json(result)
    if len(payload.encode("utf-8")) > MAX_CONVERSATION_BYTES:
        raise ValueError("conversation exceeds limit")
    result["revision"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if value.get("revision") not in (None, result["revision"]):
        raise ValueError("conversation digest mismatch")
    if len(_json(result).encode("utf-8")) > MAX_CONVERSATION_BYTES:
        raise ValueError("conversation exceeds stored byte limit")
    return result


def conversation_from_voice(record: dict, context: ImportContext) -> dict | None:
    """Capture the source conversation independently of legacy reply Traces."""
    source_id = first(record, "conversation_id", "session_id", "id")
    turns = record.get("turns")
    if not isinstance(turns, list):
        turns = record.get("messages")
    if not isinstance(source_id, str) or not source_id or not isinstance(turns, list):
        return None
    if len(source_id.encode("utf-8")) > 512:
        raise ValueError("source id exceeds conversation limit")
    messages = []
    issues = []
    message_bytes = 2  # JSON list brackets; leave room for the bounded envelope.
    for turn in turns[:MAX_MESSAGES]:
        if not isinstance(turn, dict):
            issues.append("malformed_turn")
            continue
        speaker = str(first(turn, "speaker", "role", "participant") or "").lower()
        role = (
            "user"
            if speaker in _USER
            else "assistant"
            if speaker in _ASSISTANT
            else "system"
            if speaker == "system"
            else None
        )
        content = first(turn, "text", "transcript", "content")
        if role is None or not isinstance(content, str) or not content.strip():
            issues.append("unsupported_turn")
            continue
        try:
            content = _text(content)
        except (ValueError, UnicodeError):
            issues.append("invalid_message_content")
            continue
        raw_status = str(turn.get("status", "completed")).lower()
        status = (
            "completed"
            if raw_status in {"complete", "completed", "final"}
            else "interrupted"
            if raw_status in {"interrupted", "cancelled"}
            else "unknown"
        )
        if status == "unknown":
            issues.append("unknown_message_status")
        message = {"role": role, "content": content, "status": status}
        next_bytes = message_bytes + len(_json(message).encode("utf-8")) + bool(messages)
        if next_bytes > _MESSAGE_BUDGET:
            issues.append("truncated_transcript")
            break
        messages.append(message)
        message_bytes = next_bytes
    if len(turns) > MAX_MESSAGES or record.get("truncated") is True:
        issues.append("truncated_transcript")
    if not messages:
        return None
    raw_end = record.get("end_status", record.get("conversation_status", "unknown"))
    end_status = "complete" if raw_end == "completed" else raw_end
    if not isinstance(end_status, str):
        end_status = "unknown"
        issues.append("unknown_end_status")
    if end_status not in _CLOSED | {"open", "unknown"}:
        end_status = "unknown"
        issues.append("unknown_end_status")
    source_time = first(record, "ended_at", "end_time")
    if source_time is not None and _time(source_time) is None:
        issues.append("invalid_end_time")
    return validate_conversation(
        {
            "id": context.trace_id(source_id),
            "tenant_id": context.tenant_id or "__verdict_local__",
            "source_scope": context.scope_digest,
            "messages": messages,
            "event_at": _time(source_time),
            "end_status": end_status,
            "input_issues": sorted(set(issues)),
            **({"labels": record["labels"]} if "labels" in record else {}),
        }
    )


def validate_conversation_query(tenant_id: str, limit: int, after: str | None = None) -> None:
    if tenant_id != "__verdict_local__" and safe_routing_id(tenant_id) is None:
        raise ValueError("invalid tenant")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    if after is not None and (
        not isinstance(after, str)
        or len(after) != 32
        or any(c not in "0123456789abcdef" for c in after)
    ):
        raise ValueError("invalid cursor")


def retention_cutoff(value: str) -> str:
    parsed = _time(value)
    if parsed is None:
        raise ValueError("retention cutoff must be an ISO-8601 time")
    return parsed
