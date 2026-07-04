"""Parser for the ChatGPT data export.

The export file (`conversations.json`) is a JSON array of conversation
records. Each conversation has a `mapping` field — a flat dict of message
nodes keyed by ID, linked together by `parent` / `children` references
forming a tree. To get the visible linear conversation we walk from the
root following the `current_node` pointer at the conversation level (or
walking down through the first-listed child at each step).

Schema reference (informal — OpenAI doesn't publish a stable schema):

    [
      {
        "title": "Conversation title",
        "create_time": 1701234567.890,
        "update_time": 1701234890.123,
        "mapping": {
            "<id>": {
                "id": "<id>",
                "message": {
                    "id": "<id>",
                    "author": {"role": "user" | "assistant" | "system" | "tool"},
                    "content": {"content_type": "text", "parts": ["..."]},
                    "create_time": 1701234567.890,
                    "metadata": {"model_slug": "gpt-4-turbo"}
                },
                "parent": "<parent-id>",
                "children": ["<child-id>", ...]
            }
        },
        "current_node": "<leaf-id>",
        "default_model_slug": "gpt-4-turbo"
      }
    ]

This parser handles:
  - Multi-part content (concatenates `parts`)
  - Multimodal content (extracts text, ignores image/audio blocks)
  - Tool messages (counted but not included as text)
  - Branched conversations (follows `current_node` back to root)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict_inspect.parsers.base import ParsedConversation, ParsedTurn


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _extract_text(content: Any) -> str:
    """Extract visible text from a ChatGPT message content block.

    Handles plaintext-style `{content_type: "text", parts: [str, ...]}`
    and multimodal `{content_type: "multimodal_text", parts: [{...}, ...]}`.
    """
    if not isinstance(content, dict):
        return ""
    ctype = content.get("content_type", "")
    parts = content.get("parts") or []
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            # Multimodal: text-bearing blocks have "text"
            text = p.get("text") or p.get("transcript")
            if isinstance(text, str):
                out.append(text)
    return "\n".join(out)


def _walk_linear_path(mapping: dict[str, Any], leaf_id: str) -> list[dict[str, Any]]:
    """Return the linear chain of nodes from root → leaf_id (chronological)."""
    chain: list[dict[str, Any]] = []
    cur = leaf_id
    visited: set[str] = set()
    while cur and cur not in visited:
        visited.add(cur)
        node = mapping.get(cur)
        if not isinstance(node, dict):
            break
        chain.append(node)
        cur = node.get("parent")
    chain.reverse()
    return chain


def _walk_first_child_path(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Fallback when `current_node` isn't set: walk root → first child each time."""
    # Find the root (the one with no parent)
    roots = [nid for nid, node in mapping.items()
             if isinstance(node, dict) and not node.get("parent")]
    if not roots:
        return []
    chain: list[dict[str, Any]] = []
    cur = roots[0]
    visited: set[str] = set()
    while cur and cur not in visited:
        visited.add(cur)
        node = mapping.get(cur)
        if not isinstance(node, dict):
            break
        chain.append(node)
        children = node.get("children") or []
        cur = children[0] if children else None
    return chain


def parse_chatgpt_export(path: str | Path) -> list[ParsedConversation]:
    """Parse a ChatGPT data-export `conversations.json` file."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError("ChatGPT export must be a JSON array at top level")

    conversations: list[ParsedConversation] = []
    for conv in data:
        if not isinstance(conv, dict):
            continue
        mapping = conv.get("mapping")
        if not isinstance(mapping, dict):
            continue
        leaf = conv.get("current_node")
        chain = _walk_linear_path(mapping, leaf) if leaf else _walk_first_child_path(mapping)

        title = str(conv.get("title") or "(untitled)")
        conv_id = str(conv.get("id") or conv.get("conversation_id") or title)
        started = _ts(conv.get("create_time"))
        model = conv.get("default_model_slug")

        turns: list[ParsedTurn] = []
        pending_user_text: str | None = None
        pending_user_ts: datetime | None = None
        turn_idx = 0

        for node in chain:
            msg = node.get("message")
            if not isinstance(msg, dict):
                continue
            author = (msg.get("author") or {}).get("role")
            text = _extract_text(msg.get("content"))
            ts = _ts(msg.get("create_time"))
            if author == "user":
                if text.strip():
                    pending_user_text = text
                    pending_user_ts = ts
            elif author == "assistant":
                if not text.strip():
                    continue
                this_model = ((msg.get("metadata") or {}).get("model_slug")) or model
                turns.append(ParsedTurn(
                    conversation_id=conv_id,
                    turn_index=turn_idx,
                    timestamp=ts or pending_user_ts,
                    user_text=pending_user_text or "",
                    assistant_text=text,
                    model=this_model,
                    raw={},
                ))
                turn_idx += 1
                pending_user_text = None
                pending_user_ts = None
            # `system` and `tool` messages: ignore for turn-pairing purposes

        conversations.append(ParsedConversation(
            conversation_id=conv_id,
            title=title,
            started_at=started,
            model=model,
            turns=turns,
        ))
    return conversations
