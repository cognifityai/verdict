"""Parser for the Claude.ai data export.

The Claude.ai export delivers a `conversations.json` whose shape is simpler
than ChatGPT's — a flat list of conversations, each with a flat list of
chat messages in chronological order (no mapping/tree structure).

Schema (observed, not officially documented):

    [
      {
        "uuid": "...",
        "name": "Conversation title",
        "created_at": "2024-01-15T22:30:00.000000+00:00",
        "updated_at": "...",
        "model": "claude-3-5-sonnet-20241022",
        "chat_messages": [
          {
            "uuid": "...",
            "text": "...",                         # alt: content [{...}]
            "content": [{"type":"text","text":"..."}],
            "sender": "human" | "assistant",
            "created_at": "..."
          }
        ]
      }
    ]
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


def _extract_text(msg: dict[str, Any]) -> str:
    """Get visible text from a Claude.ai message record.

    Older exports use a flat `text` string; newer exports have a `content`
    array of typed blocks (the canonical Anthropic API shape). Handle both.
    """
    content = msg.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        if parts:
            return "\n".join(parts)
    # Fall back to flat field
    text = msg.get("text") or ""
    return text if isinstance(text, str) else ""


def parse_claude_ai_export(path: str | Path) -> list[ParsedConversation]:
    """Parse a Claude.ai data-export `conversations.json` file."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError("Claude.ai export must be a JSON array at top level")

    conversations: list[ParsedConversation] = []
    for conv in data:
        if not isinstance(conv, dict):
            continue
        conv_id = str(conv.get("uuid") or conv.get("id") or conv.get("name") or "")
        title = str(conv.get("name") or "(untitled)")
        started = _ts(conv.get("created_at"))
        model = conv.get("model")

        msgs = conv.get("chat_messages") or conv.get("messages") or []
        if not isinstance(msgs, list):
            continue

        turns: list[ParsedTurn] = []
        pending_user_text: str | None = None
        pending_user_ts: datetime | None = None
        turn_idx = 0

        for m in msgs:
            if not isinstance(m, dict):
                continue
            sender = m.get("sender") or m.get("role") or ""
            text = _extract_text(m)
            ts = _ts(m.get("created_at") or m.get("timestamp"))
            if sender in ("human", "user"):
                if text.strip():
                    pending_user_text = text
                    pending_user_ts = ts
            elif sender == "assistant":
                if not text.strip():
                    continue
                turns.append(ParsedTurn(
                    conversation_id=conv_id,
                    turn_index=turn_idx,
                    timestamp=ts or pending_user_ts,
                    user_text=pending_user_text or "",
                    assistant_text=text,
                    model=model,
                    raw={},
                ))
                turn_idx += 1
                pending_user_text = None
                pending_user_ts = None

        conversations.append(ParsedConversation(
            conversation_id=conv_id,
            title=title,
            started_at=started,
            model=model,
            turns=turns,
        ))
    return conversations
