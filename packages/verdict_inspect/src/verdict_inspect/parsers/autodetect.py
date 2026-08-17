"""Format auto-detection.

We look at the first 64 KB of the file and check structural fingerprints:

  - JSONL where the first record has `messages` or `role` field → openai_jsonl
  - JSONL where the first record has `type` field and agent-session keys → cowork
  - JSON array where first element has `mapping` and `current_node` → chatgpt
  - JSON array where first element has `chat_messages` → claude_ai
  - JSON array where first element has `messages` (single shape A) → openai_jsonl

If detection fails the user can pass `--format` explicitly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from verdict_inspect.parsers.base import ParsedConversation
from verdict_inspect.parsers.chatgpt import parse_chatgpt_export
from verdict_inspect.parsers.claude_ai import parse_claude_ai_export
from verdict_inspect.parsers.cowork import parse_cowork_jsonl
from verdict_inspect.parsers.openai_jsonl import parse_openai_jsonl

FormatName = Literal["chatgpt", "claude_ai", "cowork", "openai_jsonl"]


_PARSERS: dict[str, Callable[[str | Path], list[ParsedConversation]]] = {
    "chatgpt": parse_chatgpt_export,
    "claude_ai": parse_claude_ai_export,
    "cowork": parse_cowork_jsonl,
    "openai_jsonl": parse_openai_jsonl,
}


def detect_format(path: str | Path) -> FormatName:
    """Sniff the file and return the most likely format name."""
    p = Path(path)
    with p.open("rb") as f:
        head = f.read(64 * 1024).decode("utf-8", errors="ignore")

    stripped = head.lstrip()
    if not stripped:
        raise ValueError(f"File appears empty: {path}")

    if stripped.startswith("["):
        # JSON array — ChatGPT or Claude.ai export
        try:
            # We may not have the whole array — parse what we can
            # by closing the array prematurely. Cheaper: load the full file.
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"File looks like JSON but can't be parsed: {e}") from e
        if not isinstance(data, list) or not data:
            raise ValueError(f"Top-level JSON array is empty or non-list: {path}")
        first = data[0]
        if not isinstance(first, dict):
            raise ValueError("First element of JSON array isn't an object")
        if "mapping" in first and "current_node" in first:
            return "chatgpt"
        if "mapping" in first:  # ChatGPT export without current_node
            return "chatgpt"
        if "chat_messages" in first or first.get("model", "").startswith("claude"):
            return "claude_ai"
        if "messages" in first:
            return "openai_jsonl"
        # Default for JSON array
        raise ValueError(
            "Unrecognized JSON array format. Use --format to specify "
            "{chatgpt,claude_ai,openai_jsonl}."
        )

    if stripped.startswith("{"):
        # JSONL — read first line
        first_line = stripped.split("\n", 1)[0].strip()
        try:
            first = json.loads(first_line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Looks like JSONL but first line isn't valid JSON"
            ) from exc
        # Agent-session records have type + (sessionId or message)
        if isinstance(first, dict) and first.get("type") in (
            "user", "assistant", "queue-operation", "ai-title", "last-prompt"
        ) and ("sessionId" in first or "message" in first):
            return "cowork"
        # Generic OpenAI JSONL — has messages or role
        if isinstance(first, dict) and ("messages" in first or "role" in first):
            return "openai_jsonl"
        raise ValueError(
            "Unrecognized JSONL format. Use --format to specify "
            "{cowork,openai_jsonl}."
        )

    raise ValueError(f"Can't detect format — file doesn't start with [ or {{: {path}")


def parse_auto(path: str | Path) -> tuple[FormatName, list[ParsedConversation]]:
    """Auto-detect format and parse. Returns (format_name, conversations)."""
    fmt = detect_format(path)
    return fmt, _PARSERS[fmt](path)


def parse(path: str | Path, fmt: FormatName) -> list[ParsedConversation]:
    """Parse a file with an explicitly-named format."""
    if fmt not in _PARSERS:
        raise ValueError(f"Unknown format: {fmt}. Supported: {list(_PARSERS)}")
    return _PARSERS[fmt](path)
