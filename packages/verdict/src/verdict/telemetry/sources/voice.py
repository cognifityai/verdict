"""Bounded generic voice-conversation transcript normalization."""

from __future__ import annotations

from dataclasses import replace

from verdict.telemetry.model import ImportContext, MappingResult, safe_routing_id
from verdict.telemetry.normalize import first, make_trace, parse_datetime

_USER_SPEAKERS = {"caller", "customer", "human", "user"}
_ASSISTANT_SPEAKERS = {"agent", "ai", "assistant", "bot"}
_MAX_TURNS = 1_000


def _role(speaker):
    return "user" if speaker in _USER_SPEAKERS else "assistant" if speaker in _ASSISTANT_SPEAKERS else "system" if speaker == "system" else None


def _status(value):
    name=str(value).lower()
    return "completed" if name in {"complete","completed","final"} else "interrupted" if name in {"interrupted","cancelled"} else "unknown"


def map_voice_conversation(record: object, context: ImportContext) -> list[MappingResult]:
    if not isinstance(record, dict):
        return [MappingResult.skipped("malformed_record")]
    conversation_id = first(record, "conversation_id", "session_id", "id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return [MappingResult.skipped("missing_source_id")]
    turns = record.get("turns")
    if not isinstance(turns, list):
        turns = record.get("messages")
    if not isinstance(turns, list):
        return [MappingResult.skipped("missing_turns")]
    history: list[dict[str, str]] = []
    results: list[MappingResult] = []
    for index, turn in enumerate(turns[:_MAX_TURNS]):
        if not isinstance(turn, dict):
            results.append(MappingResult.skipped("malformed_turn"))
            continue
        speaker = str(first(turn, "speaker", "role", "participant") or "").lower()
        role = _role(speaker)
        text = first(turn, "text", "transcript", "content")
        if role is None or not isinstance(text, str) or not text:
            results.append(MappingResult.skipped("unsupported_turn"))
            continue
        if role in {"user", "system"}:
            history.append({"role": role, "content": text})
            continue
        status = _status(turn["status"] if "status" in turn else "completed")
        if status != "completed":
            history.append({"role": "assistant", "content": text})
            results.append(MappingResult.skipped("incomplete_assistant_turn"))
            continue
        turn_id = first(turn, "id", "turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            turn_id = f"{conversation_id}:assistant:{index}"
        assistant = {"role": "assistant", "content": text}
        started = parse_datetime(first(turn, "started_at", "start_time", "timestamp"))
        ended = parse_datetime(first(turn, "ended_at", "end_time"))
        result = make_trace(
            context=context,
            external_id=turn_id,
            external_trace_id=conversation_id,
            started_at=started,
            ended_at=ended,
            provider=first(turn, "provider") or record.get("provider") or "voice-agent",
            operation="chat",
            request_model=first(turn, "model") or record.get("model") or "voice-agent",
            response_model=first(turn, "model") or record.get("model") or "voice-agent",
            input_value=list(history),
            output_value=assistant,
            session_id=safe_routing_id(conversation_id),
            input_tokens=first(turn, "input_tokens"),
            output_tokens=first(turn, "output_tokens"),
            cost_usd=first(turn, "cost_usd"),
        )
        results.append(result)
        history.append(assistant)
    if len(turns) > _MAX_TURNS:
        results.append(MappingResult.skipped("conversation_turn_limit"))
    results = results or [MappingResult.skipped("no_assistant_turn")]
    try:
        snapshot = _session_snapshot(record, turns, conversation_id, context)
    except (KeyError, TypeError, ValueError, UnicodeError):
        results.append(MappingResult.skipped("invalid_session_snapshot"))
    else:
        # Independent durable evidence: no foreign link claims to provider calls.
        # Persist the independent snapshot before the first tagged reply can exist.
        results[0] = replace(results[0], session=snapshot)
        for result in results:
            if result.trace is not None:
                result.trace.tags.update({"verdict.session_id": snapshot["id"],
                                          "verdict.session_revision": snapshot["revision"]})
    return results


def _session_snapshot(record, turns, conversation_id, context):
    from verdict.sessions import event_time, validate_session
    messages, issues = [], []
    for index, turn in enumerate(turns[:_MAX_TURNS]):
        if not isinstance(turn, dict):
            issues.append("malformed_turn")
            continue
        speaker = str(first(turn, "speaker", "role", "participant") or "").lower()
        role = _role(speaker)
        content = first(turn, "text", "transcript", "content")
        if role is None or not isinstance(content, str) or not content.strip():
            issues.append("unsupported_turn")
            continue
        raw_status = str(turn["status"] if "status" in turn else "completed").lower()
        status = _status(raw_status)
        if status == "unknown":
            issues.append("unknown_message_status")
        try:
            at = event_time(first(turn, "started_at", "start_time", "timestamp"))
        except (TypeError, ValueError):
            at = None
            issues.append("invalid_message_timestamp")
        messages.append({"id": f"message-{index}", "role": role, "content": content,
                         "status": status, "event_at": at})
    if len(turns) > _MAX_TURNS or record.get("truncated") is True:
        issues.append("truncated_transcript")
    raw_end = record.get("end_status", record.get("conversation_status", "unknown"))
    end = {"completed": "complete", "complete": "complete", "open": "open", "unknown": "unknown",
           "premature": "premature", "user_stop": "user_stop", "technical_failure": "technical_failure",
           "handoff": "handoff"}.get(raw_end, "unknown")
    if raw_end != end and raw_end != "completed":
        issues.append("unknown_ending_status")
    try:
        at = event_time(first(record, "event_at", "ended_at", "timestamp", "started_at"))
    except (TypeError, ValueError):
        at = None
        issues.append("invalid_session_timestamp")
    return validate_session({
        "id": context.trace_id("session", conversation_id),
        "tenant_id": context.tenant_id or "__verdict_local__", "source_scope": f"scope_{context.scope_digest}",
        "messages": messages, "event_at": at, "end_status": end, "input_issues": issues,
        **{name: record.get(name) for name in ("language", "workflow", "provider", "model", "prompt_version")},
    })
