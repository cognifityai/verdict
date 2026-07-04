"""Common schema for parsed conversations.

All format parsers produce a list of `ParsedConversation`, each containing
`ParsedTurn` records in chronological order. Downstream code never sees the
vendor-specific structure — it works against this schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ParsedTurn:
    """One user→assistant exchange flattened into a single record.

    For multi-turn structures where user and assistant interleave, each
    ParsedTurn represents the assistant's response paired with the user
    message that immediately preceded it.

    `tool_calls` is set when the assistant invoked tools; the conversation
    may be of interest even if the visible text is short.
    """
    conversation_id: str
    turn_index: int
    timestamp: datetime | None
    user_text: str
    assistant_text: str
    model: str | None = None
    tool_calls: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedConversation:
    """One conversation thread — multiple turns in chronological order."""
    conversation_id: str
    title: str
    started_at: datetime | None
    model: str | None
    turns: list[ParsedTurn] = field(default_factory=list)

    @property
    def n_turns(self) -> int:
        return len(self.turns)

    def all_assistant_text(self) -> list[str]:
        return [t.assistant_text for t in self.turns if t.assistant_text.strip()]


def flatten(conversations: list[ParsedConversation]) -> list[ParsedTurn]:
    """Flatten many conversations into one chronologically-ordered turn list.

    Used when we want to run drift detection across the user's entire
    conversation history regardless of which thread each turn came from.
    """
    all_turns: list[ParsedTurn] = []
    for c in conversations:
        all_turns.extend(c.turns)
    # Sort by timestamp where available; fall back to (conversation_id, turn_index)
    all_turns.sort(
        key=lambda t: (
            t.timestamp or datetime.min,
            t.conversation_id,
            t.turn_index,
        )
    )
    return all_turns
