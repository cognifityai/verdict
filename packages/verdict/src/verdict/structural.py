"""Lightweight structural predicates shared by core monitoring and eval."""

from __future__ import annotations

import json
import re

REFUSAL_SIGNATURES: tuple[str, ...] = (
    r"\bi can'?t (?:help|assist|do that|do this|provide|create|generate|write)\b",
    r"\bi won'?t (?:help|assist|provide|create|write|do|generate)\b",
    r"\bi'?m (?:not able|unable) to (?:help|assist|do|provide|comply|fulfill|generate)\b",
    r"\bi (?:must|have to) decline\b",
    r"\bi (?:cannot|can'?t) comply\b",
    r"\bnot (?:something|able) i (?:can|will) (?:help|do|assist)\b",
    r"\bi'?m sorry,? but (?:i can'?t|i won'?t|i'?m unable|i can not)\b",
)

HEDGE_PATTERNS: tuple[str, ...] = (
    r"\bi think\b", r"\bi believe\b", r"\bperhaps\b", r"\bmaybe\b",
    r"\bmight\b", r"\bcould\b", r"\bpossibly\b", r"\bseems like\b",
    r"\bappears (?:to|that)\b", r"\bi'?m not sure\b", r"\bnot certain\b",
    r"\bpotentially\b", r"\bit depends\b", r"\bwould say\b",
    r"\bin some sense\b",
)

APOLOGY_STARTERS: tuple[str, ...] = (
    r"^\s*i apologi[sz]e\b", r"^\s*i'?m sorry\b", r"^\s*sorry,?\s",
    r"^\s*my apologies\b",
)


def is_refusal(text: str) -> bool:
    """Return whether text matches a contextual refusal signature."""
    lower = text.lower()
    return any(re.search(pattern, lower) for pattern in REFUSAL_SIGNATURES)


def count_hedges(text: str) -> int:
    lower = text.lower()
    return sum(len(re.findall(pattern, lower)) for pattern in HEDGE_PATTERNS)


def is_apology_start(text: str) -> bool:
    lower = text.lower()
    return any(re.search(pattern, lower) for pattern in APOLOGY_STARTERS)


def is_valid_json(text: str) -> bool:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"```\s*$", "", value)
    if not value:
        return False
    try:
        json.loads(value)
        return True
    except (json.JSONDecodeError, ValueError):
        return False
