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

# Capture records how many tool calls a provider response contained. Text
# capture keeps only answer text, so without this fact a tool-calling reply
# is indistinguishable from a blank one.
RESPONSE_TOOL_CALLS_TAG = "verdict.response_tool_calls"
# Finish reasons that name a tool call, for traces captured before the tag and
# for imports: OpenAI ``tool_calls``/``function_call``, Anthropic ``tool_use``,
# OpenTelemetry GenAI ``tool_call``.
_TOOL_CALL_FINISH_REASONS = frozenset({"tool_calls", "tool_use", "function_call", "tool_call"})


# OpenAI Responses reports its lifecycle status where other providers report a
# stop reason, so an empty "completed" response may have been a tool call.
_STATUS_ONLY_FINISH_REASONS = frozenset({"completed", "in_progress", "queued"})
# A reply stopped at its output limit (OpenAI Responses ``incomplete``, OpenAI
# ``length``, Anthropic ``max_tokens``, Gemini ``MAX_TOKENS``) was cut off, so
# an empty one does not show whether the model would have answered.
_CUT_OFF_FINISH_REASONS = frozenset({"incomplete", "length", "max_tokens"})


def _recorded_tool_call_count(tags: object) -> int | None:
    count = tags.get(RESPONSE_TOOL_CALLS_TAG) if isinstance(tags, dict) else None
    # ASCII digits only: ``str.isdigit`` also accepts characters such as "²"
    # that ``int`` rejects, and the length bound keeps ``int`` within its limit.
    if isinstance(count, str) and count.isascii() and count.isdigit() and len(count) <= 9:
        return int(count)
    return None


def response_has_tool_calls(*, tags: object, finish_reason: object) -> bool:
    """Return whether the provider answered with at least one client tool call."""
    count = _recorded_tool_call_count(tags)
    return (count is not None and count > 0) or finish_reason in _TOOL_CALL_FINISH_REASONS


def response_is_blank(*, response: object, tags: object, finish_reason: object) -> bool | None:
    """Classify a completed reply as blank, answered, or not decidable (None).

    A reply is blank only when it has no answer text, no client tool call, and
    the capture establishes that no call was omitted: a recorded tool-call count
    of zero, or a provider stop reason. A status-only or missing finish reason
    without a recorded count cannot distinguish a tool call from a blank reply,
    and an empty reply cut off at its output limit is not decidable either.
    """
    if not isinstance(response, str):
        return None
    if text_is_present(response) or response_has_tool_calls(
        tags=tags, finish_reason=finish_reason,
    ):
        return False
    if isinstance(finish_reason, str) and (
        finish_reason.rsplit(".", 1)[-1].strip().lower() in _CUT_OFF_FINISH_REASONS
    ):
        return None
    if _recorded_tool_call_count(tags) == 0:
        return True
    if isinstance(finish_reason, str) and finish_reason and (
        finish_reason not in _STATUS_ONLY_FINISH_REASONS
    ):
        return True
    return None


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
