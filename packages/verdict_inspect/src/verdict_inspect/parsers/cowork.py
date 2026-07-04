"""Parser for agent-session JSONL files.

Some local agent tools write one JSON record per line with multiple message
types per session (user turns, assistant turns with content blocks, tool_use,
tool_result, thinking, system reminders, etc.). We extract only the visible
user-to-assistant exchanges.

This parser is exposed through the historical `cowork` format name for CLI
compatibility.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from verdict_inspect.parsers.base import ParsedConversation, ParsedTurn


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_assistant_text(message: dict[str, Any]) -> str:
    """Pull text content out of an assistant message; skip thinking + tool_use."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text", "")
            if isinstance(t, str) and t.strip():
                parts.append(t)
    return "\n".join(parts)


def _extract_user_text(message: dict[str, Any]) -> str:
    """Pull text out of a user record; skip tool_result wrappers."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                # This is a tool result, not a real user turn
                return ""
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def parse_cowork_jsonl(path: str | Path) -> list[ParsedConversation]:
    """Parse an agent-session JSONL into a single-conversation list."""
    p = Path(path)
    session_id = p.stem  # filename without extension is the session id

    turns: list[ParsedTurn] = []
    pending_user: str | None = None
    pending_user_ts: datetime | None = None
    turn_idx = 0
    first_ts: datetime | None = None
    model: str | None = None
    title = "Agent session"

    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            ts = _ts(rec.get("timestamp"))
            if first_ts is None and ts is not None:
                first_ts = ts
            if rtype == "ai-title":
                t = rec.get("aiTitle")
                if isinstance(t, str):
                    title = t
                continue
            msg = rec.get("message")
            if rtype == "user" and isinstance(msg, dict):
                text = _extract_user_text(msg)
                if text.strip():
                    pending_user = text
                    pending_user_ts = ts
            elif rtype == "assistant" and isinstance(msg, dict):
                text = _extract_assistant_text(msg)
                if not text.strip():
                    continue
                if model is None:
                    model = msg.get("model")
                turns.append(ParsedTurn(
                    conversation_id=session_id,
                    turn_index=turn_idx,
                    timestamp=ts or pending_user_ts,
                    user_text=pending_user or "",
                    assistant_text=text,
                    model=msg.get("model") or model,
                    raw={},
                ))
                turn_idx += 1
                pending_user = None
                pending_user_ts = None

    return [ParsedConversation(
        conversation_id=session_id,
        title=title,
        started_at=first_ts,
        model=model,
        turns=turns,
    )]
