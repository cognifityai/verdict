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


def text_is_present(value: object) -> bool:
    """Return whether captured text contains non-whitespace evidence."""
    return isinstance(value, str) and bool(value.strip())


def trace_conversation_history(trace) -> str | None:
    """Return bounded prior messages; canonical imports append the target output."""
    rows = trace.raw_messages
    if not isinstance(rows, list) or not rows:
        if trace.tags.get("verdict.source") == "voice":
            raise ValueError("incoherent imported voice reply evidence")
        return None
    messages = [{"role": m["role"], "content": m["content"]} for m in rows
                if isinstance(m, dict) and m.get("role") in {"user", "assistant", "system", "tool"}
                and isinstance(m.get("content"), str)]
    if trace.tags.get("verdict.source") == "voice":
        from verdict.redaction import redact
        from verdict.telemetry.normalize import message_text

        # Published imports project user text, then storage redacts the joined
        # prompt. Validate both sides before using legacy rows as judge context.
        if (
            len(messages) != len(rows) or not messages
            or messages[-1]["role"] != "assistant"
            or redact(message_text(messages[-1:], "assistant")) != trace.response_redacted
            or redact(message_text(messages[:-1], "user")) != trace.prompt_redacted
        ):
            raise ValueError("incoherent imported voice reply evidence")
    if trace.tags.get("verdict.source") and messages and messages[-1]["role"] == "assistant" and messages[-1]["content"] == trace.response_redacted:
        messages = messages[:-1]
    # No tail truncation: it could discard the initial instruction or role order.
    encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 512_000:
        raise ValueError("conversation history exceeds judge evidence budget")
    return encoded if messages else None


def trace_evidence_reason(*, error: object, prompt: object, response: object) -> str | None:
    """Return the first reason a trace is not eligible for response judging."""
    if error:
        return "provider_call_failed"
    if not text_is_present(prompt):
        return "prompt_not_captured"
    if not text_is_present(response):
        return "response_not_captured"
    return None


def trace_judge_evidence_digest(*, error: object, prompt: object, response: object, history: str | None = None) -> str:
    """Identify the exact response-judge evidence without retaining its content."""
    payload = [_JUDGE_EVIDENCE_DIGEST_VERSION, error, prompt, response]
    if history is not None:
        payload = ["trace_response_history_v2", error, prompt, response, history]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


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
