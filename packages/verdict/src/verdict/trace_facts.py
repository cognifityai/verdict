"""Shared deterministic facts derived from one stored LLM trace."""

from __future__ import annotations

import hashlib
import json

from verdict.structural import (
    count_hedges,
    is_apology_start,
    is_refusal,
    is_valid_json,
)

_JUDGE_EVIDENCE_DIGEST_VERSION = "judge-evidence-v1"
_END_NOT_SUPPLIED = object()


def trace_evidence_state(*, error: object, ended_at: object) -> str:
    """Classify recorded execution evidence without inferring provider success."""
    if error is not None:
        return "error_recorded"
    if ended_at is not None:
        return "ended_no_error_recorded"
    return "unfinished"


def text_is_present(value: object) -> bool:
    """Return whether captured text contains non-whitespace evidence."""
    return isinstance(value, str) and bool(value.strip())


def trace_evidence_reason(
    *, error: object, prompt: object, response: object,
    ended_at: object = _END_NOT_SUPPLIED,
) -> str | None:
    """Return the first reason a trace is not eligible for response judging."""
    if error is not None:
        return "provider_call_failed"
    if ended_at is None:
        return "trace_unfinished"
    if not text_is_present(prompt):
        return "prompt_not_captured"
    if not text_is_present(response):
        return "response_not_captured"
    return None


def trace_judge_evidence_digest(*, error: object, prompt: object, response: object) -> str:
    """Identify the exact response-judge evidence without retaining its content."""
    payload = [_JUDGE_EVIDENCE_DIGEST_VERSION, error, prompt, response]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def deterministic_trace_facts(
    *,
    error: object,
    prompt: object,
    response: object,
    ended_at: object = _END_NOT_SUPPLIED,
) -> dict[str, object]:
    """Return bounded, judge-free facts for one stored LLM trace."""
    prompt_present = text_is_present(prompt)
    response_present = text_is_present(response)
    reason = trace_evidence_reason(
        error=error, prompt=prompt, response=response, ended_at=ended_at,
    )
    response_text = response if isinstance(response, str) else None
    return {
        "provider_outcome": "failed" if error is not None else "no_error_recorded",
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
