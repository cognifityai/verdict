"""Format parsers for chat exports.

Each parser converts a vendor-specific export into a `ParsedConversation`
list of `ParsedTurn` records, which downstream pipeline code treats uniformly.

To add a new format:
1. Subclass or write a function in a new module here.
2. Register it in autodetect.py with a `sniff(path) -> bool` predicate.
3. Add a unit test covering the format's edge cases.
"""

from verdict_inspect.parsers.autodetect import detect_format, parse_auto
from verdict_inspect.parsers.base import ParsedConversation, ParsedTurn
from verdict_inspect.parsers.chatgpt import parse_chatgpt_export
from verdict_inspect.parsers.claude_ai import parse_claude_ai_export
from verdict_inspect.parsers.cowork import parse_cowork_jsonl
from verdict_inspect.parsers.openai_jsonl import parse_openai_jsonl

__all__ = [
    "ParsedConversation",
    "ParsedTurn",
    "detect_format",
    "parse_auto",
    "parse_chatgpt_export",
    "parse_claude_ai_export",
    "parse_cowork_jsonl",
    "parse_openai_jsonl",
]
