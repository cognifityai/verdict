"""Bounded generic voice-conversation transcript normalization."""

from __future__ import annotations

from verdict.telemetry.model import ImportContext, MappingResult, safe_routing_id
from verdict.telemetry.normalize import first, make_trace, parse_datetime

_USER_SPEAKERS = {"caller", "customer", "human", "user"}
_ASSISTANT_SPEAKERS = {"agent", "ai", "assistant", "bot"}
_MAX_TURNS = 1_000


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
        role = (
            "user"
            if speaker in _USER_SPEAKERS
            else "assistant"
            if speaker in _ASSISTANT_SPEAKERS
            else None
        )
        text = first(turn, "text", "transcript", "content")
        if role is None or not isinstance(text, str) or not text:
            results.append(MappingResult.skipped("unsupported_turn"))
            continue
        if role == "user":
            history.append({"role": "user", "content": text})
            continue
        status = str(turn.get("status") or "completed").lower()
        if status not in {"complete", "completed", "final"}:
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
        if result.trace is not None:
            history.append(assistant)
    if len(turns) > _MAX_TURNS:
        results.append(MappingResult.skipped("conversation_turn_limit"))
    return results or [MappingResult.skipped("no_assistant_turn")]
