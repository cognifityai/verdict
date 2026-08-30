"""Shared source-only helpers for local agent history adapters."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.normalize import make_trace, parse_datetime

_AMBIENT_BLOCK = re.compile(
    r"\A\s*<(environment_context|in-app-browser-context)\b[^>]*>.*?</\1>\s*",
    flags=re.DOTALL,
)
_REQUEST_HEADING = re.compile(r"\A\s*##\s+My request:\s*", flags=re.IGNORECASE)
_MAX_TAG_VALUE_BYTES = 256
MAX_AGENT_CONTENT_CHARS = 100_000
MAX_HISTORY_EVENTS = 250_000
MAX_HISTORY_FILES = 100_000


def object_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def parse_time(value: object) -> datetime | None:
    parsed = parse_datetime(value)
    return parsed.astimezone(timezone.utc) if parsed is not None else None


def safe_agent_text(value: object, *, home: Path | None = None) -> str:
    """Remove source-injected context and local identity before normalization."""
    if not isinstance(value, str):
        return ""
    cleaned = value
    while match := _AMBIENT_BLOCK.match(cleaned):
        cleaned = cleaned[match.end() :]
    cleaned = _REQUEST_HEADING.sub("", cleaned)
    home_text = str(home or Path.home())
    if home_text and home_text != "/":
        cleaned = cleaned.replace(home_text, "~")
    return cleaned.strip()[:MAX_AGENT_CONTENT_CHARS]


def iter_history_paths(root: Path):
    """Walk a local history root without materializing an unbounded path list."""
    for index, path in enumerate(root.rglob("*.jsonl")):
        if index >= MAX_HISTORY_FILES:
            raise ValueError(f"local agent history exceeds {MAX_HISTORY_FILES} files")
        yield path


def private_identity(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:32]


def bounded_tag(value: object) -> str:
    if not isinstance(value, str):
        return ""
    encoded = value.encode("utf-8", errors="replace")[:_MAX_TAG_VALUE_BYTES]
    return encoded.decode("utf-8", errors="ignore")


def sum_tokens(values: list[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    total = sum(present)
    return total if total <= 2**31 - 1 else None


def token_delta(current: int | None, previous: int | None) -> int | None:
    if current is None:
        return None
    if previous is None or current < previous:
        return current
    return current - previous


def map_completed_turn(
    *,
    context: ImportContext,
    external_id: str,
    session_id: str,
    started_at: datetime | None,
    ended_at: datetime | None,
    provider: str,
    model: str,
    prompt: str,
    response: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_input_tokens: int | None,
    finish_reason: str,
    tool_calls: int,
    assistant_calls: int,
    project: object,
    branch: object,
    source_version: object,
) -> MappingResult:
    """Map source interpretation through Verdict's canonical trace normalizer."""
    result = make_trace(
        context=context,
        external_id=external_id,
        external_trace_id=session_id,
        started_at=started_at,
        ended_at=ended_at,
        provider=provider,
        operation="chat",
        request_model=model,
        response_model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        input_value=prompt,
        output_value=response,
        session_id=f"{context.adapter}:{private_identity(session_id)}",
    )
    if result.trace is None:
        return result
    tags = result.trace.tags
    tags.update(
        {
            "verdict.workload": "agent",
            "capture.granularity": "agent-turn",
            "capture.agent": context.adapter,
            "capture.tool_calls": str(max(0, tool_calls)),
            "capture.assistant_calls": str(max(0, assistant_calls)),
            "capture.project_hash": private_identity(project),
            "capture.branch_hash": private_identity(branch),
            "capture.source_version": bounded_tag(source_version),
        }
    )
    if cached_input_tokens is not None:
        tags["capture.cached_input_tokens"] = str(cached_input_tokens)
    return result


def nonnegative_token(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 2**31 - 1 else None


def message_text(message: Mapping[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for block in content:
        item = object_mapping(block)
        text = item.get("text") if item else None
        if item and item.get("type") == "text" and isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)
