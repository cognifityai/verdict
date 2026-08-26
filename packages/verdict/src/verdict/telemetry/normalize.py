"""Shared, allowlisted normalization for untrusted telemetry values."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.schema import Operation, Trace, normalize_optional_float, normalize_optional_integer
from verdict.telemetry.model import ImportContext, MappingResult, safe_routing_id

_MAX_CONTENT_CHARS = 100_000
_MAX_MESSAGES = 1_000
_MAX_EXTERNAL_ID_BYTES = 1_024
_MAX_MODEL_CHARS = 256
_ROLE_ALIASES = {
    "ai": "assistant",
    "agent": "assistant",
    "assistant": "assistant",
    "bot": "assistant",
    "caller": "user",
    "human": "user",
    "model": "assistant",
    "system": "system",
    "tool": "tool",
    "user": "user",
}


def as_mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def coalesce(*values: object) -> object:
    """Return the first present value, preserving valid false and zero values."""
    return next((value for value in values if value is not None), None)


def nested(mapping: dict[str, Any], *path: str) -> Any:
    value: Any = mapping
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def parse_json_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in '[{"':
        return value
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        return value


def optional_int(value: object) -> int | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and (stripped.isdecimal() or (stripped[0] == "-" and stripped[1:].isdecimal())):
            try:
                value = int(stripped)
            except ValueError:
                return None
    return normalize_optional_integer(value)


def optional_float(value: object, *, minimum: float | None = None) -> float | None:
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    return normalize_optional_float(value, minimum=minimum)


def parse_datetime(value: object, *, numeric_unit: str | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
    if numeric_unit is None and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            return None
        return (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc)
        )
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(value) if numeric_unit in {"ns", "us", "ms"} else float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    divisors = {"ns": 1_000_000_000, "us": 1_000_000, "ms": 1_000, "s": 1}
    divisor = divisors.get(numeric_unit or "")
    if divisor is None:
        return None
    seconds, remainder = divmod(number, divisor)
    microseconds = int(remainder * 1_000_000 / divisor)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=microseconds)
    except (ValueError, OverflowError, OSError):
        return None


def end_from_duration(started_at: datetime | None, duration_ms: float | None) -> datetime | None:
    if started_at is None or duration_ms is None:
        return None
    try:
        return started_at + timedelta(milliseconds=duration_ms)
    except OverflowError:
        return None


def derive_latency_ms(
    started_at: datetime,
    ended_at: datetime | None,
    explicit_ms: object = None,
) -> float | None:
    explicit = optional_float(explicit_ms, minimum=0.0)
    if explicit is not None:
        return explicit
    if ended_at is None or ended_at < started_at:
        return None
    return (ended_at - started_at).total_seconds() * 1000.0


def infer_provider(model: object, explicit: object = None) -> str:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()[:64]
    normalized = model.lower() if isinstance(model, str) else ""
    if "claude" in normalized or "anthropic" in normalized:
        return "anthropic"
    if any(value in normalized for value in ("gpt", "openai", "o1", "o3", "o4")):
        return "openai"
    if "gemini" in normalized or "google" in normalized or "palm" in normalized:
        return "google"
    if "mistral" in normalized:
        return "mistral"
    if "cohere" in normalized or "command-r" in normalized:
        return "cohere"
    if "llama" in normalized:
        return "meta"
    return "unknown"


def operation_from(value: object) -> Operation:
    normalized = str(value or "").lower()
    if normalized in {"embedding", "embeddings"}:
        return Operation.EMBEDDING
    if normalized in {"completion", "text_completion", "completions"}:
        return Operation.TEXT_COMPLETION
    return Operation.CHAT


def _bounded_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.encode("utf-8", errors="replace").decode("utf-8")
    return normalized[:maximum]


def _text_content(value: object) -> str | None:
    value = parse_json_value(value)
    if isinstance(value, str):
        return _bounded_text(value, _MAX_CONTENT_CHARS)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        character_count = 0
        for block in value:
            if isinstance(block, str):
                text = block
            elif isinstance(block, dict):
                block_type = str(block.get("type") or "text").lower()
                if block_type in {"text", "input_text", "output_text"}:
                    text = first(block, "text", "content", "value")
                    if not isinstance(text, str):
                        continue
                else:
                    continue
            else:
                continue
            remaining = _MAX_CONTENT_CHARS - character_count
            bounded = _bounded_text(text, remaining)
            if bounded:
                parts.append(bounded)
                character_count += len(bounded)
            if character_count >= _MAX_CONTENT_CHARS:
                break
        combined = "\n".join(parts)
        return combined[:_MAX_CONTENT_CHARS] if combined else None
    if isinstance(value, dict):
        content = first(value, "content", "text", "value")
        if content is not None and content is not value:
            return _text_content(content)
    return None


def _message_from_mapping(value: dict[str, Any], default_role: str) -> dict[str, str] | None:
    role_value = first(value, "role", "type", "speaker", "author")
    role = _ROLE_ALIASES.get(str(role_value or default_role).lower(), default_role)
    content = _text_content(first(value, "content", "text", "value", "message"))
    if content is None:
        return None
    return {"role": role, "content": content}


def messages_from(value: object, *, default_role: str) -> list[dict[str, str]]:
    """Normalize supported text message forms without copying unknown fields."""
    messages: list[dict[str, str]] = []
    character_count = 0

    def append(role: str, content: str) -> None:
        nonlocal character_count
        if len(messages) >= _MAX_MESSAGES or character_count >= _MAX_CONTENT_CHARS:
            return
        bounded = _bounded_text(content, _MAX_CONTENT_CHARS - character_count)
        if not bounded:
            return
        messages.append({"role": role, "content": bounded})
        character_count += len(bounded)

    def visit(item: object, depth: int) -> None:
        if depth > 16 or len(messages) >= _MAX_MESSAGES or character_count >= _MAX_CONTENT_CHARS:
            return
        item = parse_json_value(item)
        if isinstance(item, str):
            append(default_role, item)
            return
        if isinstance(item, list):
            for child in item[:_MAX_MESSAGES]:
                visit(child, depth + 1)
            return
        if not isinstance(item, dict):
            return
        for key in ("messages", "generations", "choices"):
            if key in item:
                visit(item[key], depth + 1)
                return
        if "message" in item and isinstance(item["message"], (dict, list)):
            visit(item["message"], depth + 1)
            return
        message = _message_from_mapping(item, default_role)
        if message is not None:
            append(message["role"], message["content"])

    visit(value, 0)
    return messages


def message_text(messages: list[dict[str, str]], preferred_role: str) -> str | None:
    preferred = [item["content"] for item in messages if item.get("role") == preferred_role]
    values = preferred or [item["content"] for item in messages]
    if not values:
        return None
    return "\n".join(values)[:_MAX_CONTENT_CHARS]


def make_trace(
    *,
    context: ImportContext,
    external_id: object,
    external_trace_id: object = None,
    started_at: datetime | None,
    ended_at: datetime | None = None,
    explicit_latency_ms: object = None,
    provider: object = None,
    operation: object = None,
    request_model: object = None,
    response_model: object = None,
    input_tokens: object = None,
    output_tokens: object = None,
    temperature: object = None,
    max_tokens: object = None,
    finish_reason: object = None,
    error: object = None,
    input_value: object = None,
    output_value: object = None,
    session_id: object = None,
    cost_usd: object = None,
) -> MappingResult:
    if not isinstance(external_id, str) or not external_id:
        return MappingResult.skipped("missing_source_id")
    try:
        external_id_size = len(external_id.encode("utf-8"))
    except UnicodeEncodeError:
        return MappingResult.skipped("invalid_source_id")
    if external_id_size > _MAX_EXTERNAL_ID_BYTES:
        return MappingResult.skipped("invalid_source_id")
    if started_at is None:
        return MappingResult.skipped("invalid_start_time")
    if ended_at is not None and ended_at < started_at:
        return MappingResult.skipped("invalid_time_interval")
    external_trace = None
    if isinstance(external_trace_id, str):
        try:
            if len(external_trace_id.encode("utf-8")) <= _MAX_EXTERNAL_ID_BYTES:
                external_trace = external_trace_id
        except UnicodeEncodeError:
            pass
    request_name = _bounded_text(request_model, _MAX_MODEL_CHARS) or ""
    response_name = _bounded_text(response_model, _MAX_MODEL_CHARS) or ""
    selected_model = response_name or request_name
    input_messages = messages_from(input_value, default_role="user")
    output_messages = messages_from(output_value, default_role="assistant")
    normalized_error = _bounded_text(error, 10_000)
    normalized_finish = _bounded_text(finish_reason, 256)
    trace = Trace(
        trace_id=context.trace_id(external_id, external_trace),
        started_at=started_at,
        ended_at=ended_at,
        provider=infer_provider(selected_model, provider),
        operation=operation_from(operation),
        request_model=request_name or selected_model,
        response_model=response_name or selected_model,
        input_tokens=optional_int(input_tokens),
        output_tokens=optional_int(output_tokens),
        temperature=optional_float(temperature),
        max_tokens=optional_int(max_tokens),
        finish_reason=normalized_finish,
        error=normalized_error,
        latency_ms=derive_latency_ms(started_at, ended_at, explicit_latency_ms),
        prompt_redacted=message_text(input_messages, "user"),
        response_redacted=message_text(output_messages, "assistant"),
        raw_messages=[*input_messages, *output_messages] or None,
        tenant_id=context.tenant_id,
        session_id=safe_routing_id(session_id),
        tags=context.provenance_tags(external_id, external_trace),
        cost_usd=optional_float(cost_usd, minimum=0.0),
    )
    return MappingResult.mapped(trace)


def finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
