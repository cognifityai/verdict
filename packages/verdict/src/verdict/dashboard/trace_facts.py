"""Shared deterministic facts derived from one stored LLM trace."""

from __future__ import annotations


def text_is_present(value: object) -> bool:
    """Return whether captured text contains non-whitespace evidence."""
    return isinstance(value, str) and bool(value.strip())


def trace_evidence_reason(*, error: object, prompt: object, response: object) -> str | None:
    """Return the first reason a trace is not eligible for response judging."""
    if error:
        return "provider_call_failed"
    if not text_is_present(prompt):
        return "prompt_not_captured"
    if not text_is_present(response):
        return "response_not_captured"
    return None
