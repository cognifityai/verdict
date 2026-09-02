"""Shared deterministic facts derived from one stored LLM trace."""

from __future__ import annotations

from verdict.structural import (
    count_hedges,
    is_apology_start,
    is_refusal,
    is_valid_json,
)


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


def deterministic_trace_facts(
    *,
    error: object,
    prompt: object,
    response: object,
) -> dict[str, object]:
    """Return bounded, judge-free facts for one stored LLM trace."""
    prompt_present = text_is_present(prompt)
    response_present = text_is_present(response)
    reason = trace_evidence_reason(error=error, prompt=prompt, response=response)
    response_text = response if isinstance(response, str) else None
    return {
        "provider_outcome": "failed" if error else "succeeded",
        "prompt_present": prompt_present,
        "response_present": response_present,
        "judge_eligible": reason is None,
        "not_evaluable_reason": reason,
        "response_characters": len(response_text) if response_text is not None else None,
        "valid_json": is_valid_json(response_text) if response_present else None,
        "refusal_signature": is_refusal(response_text) if response_present else None,
        "apology_start": is_apology_start(response_text) if response_present else None,
        "hedge_phrases": count_hedges(response_text) if response_present else None,
    }
