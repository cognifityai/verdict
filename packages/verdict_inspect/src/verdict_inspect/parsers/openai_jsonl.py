"""Parser for generic OpenAI-format messages JSONL.

This is the lowest-common-denominator format — one JSON object per line,
each containing a `messages` array of `{role, content}` records. Many
agent frameworks (LangChain, LlamaIndex, custom logs) dump in this
format, so accepting it widens what verdict-inspect can analyze.

Two accepted shapes per line:

    # Shape A — single conversation per line
    {"messages": [{"role":"user","content":"..."}, ...],
     "model": "gpt-4o", "timestamp": "...", "conversation_id": "..."}

    # Shape B — single message per line, grouped by conversation_id
    {"role":"user","content":"...","conversation_id":"abc","timestamp":"..."}
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from verdict_inspect.parsers.base import ParsedConversation, ParsedTurn


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _content_text(content: Any) -> str:
    """OpenAI content can be a string or an array of typed blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in ("text", "input_text", "output_text"):
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
        return "\n".join(parts)
    return ""


def _turns_from_messages(
    messages: list[dict[str, Any]],
    conv_id: str,
    default_model: str | None,
) -> list[ParsedTurn]:
    out: list[ParsedTurn] = []
    pending_user_text: str | None = None
    pending_user_ts: datetime | None = None
    turn_idx = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or ""
        text = _content_text(m.get("content"))
        ts = _ts(m.get("timestamp") or m.get("created_at"))
        if role in ("user", "human"):
            if text.strip():
                pending_user_text = text
                pending_user_ts = ts
        elif role in ("assistant", "ai"):
            if not text.strip():
                continue
            out.append(ParsedTurn(
                conversation_id=conv_id,
                turn_index=turn_idx,
                timestamp=ts or pending_user_ts,
                user_text=pending_user_text or "",
                assistant_text=text,
                model=m.get("model") or default_model,
                raw={},
            ))
            turn_idx += 1
            pending_user_text = None
            pending_user_ts = None
    return out


def parse_openai_jsonl(path: str | Path) -> list[ParsedConversation]:
    """Parse a JSONL file in OpenAI-format messages (either shape).

    Auto-detects whether each line is a single full conversation (Shape A)
    or a single message (Shape B). If both shapes appear in the same file
    they're treated independently — Shape A records produce their own
    conversation, Shape B records are grouped by `conversation_id`.
    """
    p = Path(path)
    shape_b_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    conversations: list[ParsedConversation] = []

    with p.open() as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if "messages" in rec and isinstance(rec["messages"], list):
                # Shape A
                conv_id = str(rec.get("conversation_id") or rec.get("id") or f"line-{lineno}")
                model = rec.get("model")
                started = _ts(rec.get("created_at") or rec.get("timestamp"))
                turns = _turns_from_messages(rec["messages"], conv_id, model)
                if turns:
                    conversations.append(ParsedConversation(
                        conversation_id=conv_id,
                        title=str(rec.get("title") or conv_id),
                        started_at=started,
                        model=model,
                        turns=turns,
                    ))
            elif "role" in rec:
                # Shape B
                conv_id = str(rec.get("conversation_id") or "default")
                shape_b_groups[conv_id].append(rec)

    # Process Shape B groups
    for conv_id, msgs in shape_b_groups.items():
        msgs_sorted = sorted(msgs, key=lambda m: _ts(m.get("timestamp")
                                                     or m.get("created_at")) or datetime.min)
        turns = _turns_from_messages(msgs_sorted, conv_id, None)
        if turns:
            started = turns[0].timestamp if turns else None
            conversations.append(ParsedConversation(
                conversation_id=conv_id,
                title=conv_id,
                started_at=started,
                model=None,
                turns=turns,
            ))

    return conversations
