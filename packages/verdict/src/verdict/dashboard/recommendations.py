"""Conservative, evidence-linked suggestions for one captured agent run."""

from __future__ import annotations

import hashlib
from itertools import pairwise

from verdict.evidence import AgentEventType, AgentRunBundle, ExecutionStatus, event_order_key
from verdict.redaction import _PLACEHOLDER


def run_recommendations(bundle: AgentRunBundle) -> list[dict[str, object]]:
    """Suggest a review action only when ordered evidence supports the pattern."""
    events = sorted(bundle.events, key=event_order_key)
    # Ordering across independent producers is only a display convention.
    producers = {event.producer_id for event in events if event.producer_id}
    recommendations: list[dict[str, object]] = []
    if len(producers) <= 1 and not any(
        event.provenance == "verdict:capture_limit" for event in events
    ):
        tests = [event for event in events if event.event_type is AgentEventType.TEST_RESULT]
        if tests:
            last = tests[-1]
            failed = last.status is ExecutionStatus.FAILED or (
                type(last.attributes.get("failed")) is int
                and last.attributes["failed"] > 0
            )
            if failed:
                recommendations.append({
                    "code": "review_last_failed_test", "status": "suggested",
                    "observation": "The last recorded test execution reported failure.",
                    "action": "Inspect the failure, fix its cause, and rerun the relevant test.",
                    "rationale": "No later passing test is recorded in this run.",
                    "tradeoff": "A later test outside this captured run may already have resolved it.",
                    "evidenceEventIds": [last.event_id], "evidenceTurnIds": [last.turn_id],
                })
    failed_results = [event for event in events if (
        event.event_type is AgentEventType.TOOL_RESULT
        and (event.status is ExecutionStatus.FAILED
             or event.attributes.get("is_error") is True)
    )]
    if len(failed_results) >= 2:
        recommendations.append({
            "code": "review_repeated_tool_failures", "status": "suggested",
            "observation": f"{len(failed_results)} recorded tool results reported errors.",
            "action": "Inspect the linked tool arguments and results before changing the workflow.",
            "rationale": "The source recorded repeated failures; their cause is not established.",
            "tradeoff": "Retries may be necessary and later recovery may be valid.",
            "evidenceEventIds": [event.event_id for event in failed_results[:20]],
            "evidenceTurnIds": list(dict.fromkeys(event.turn_id for event in failed_results[:20])),
        })
    ordered_turns = sorted(bundle.turns, key=lambda turn: (turn.sequence, turn.turn_id))
    input_turns = [turn for turn in ordered_turns if (
        turn.token_usage_basis in {"codex_turn_delta", "claude_provider_response_sum"}
        and turn.input_tokens is not None
    )]
    if (len(input_turns) >= 3
            and len({turn.token_usage_basis for turn in input_turns}) == 1
            and input_turns[-1].input_tokens >= 10_000
            and input_turns[0].input_tokens > 0
            and input_turns[-1].input_tokens >= 2 * input_turns[0].input_tokens
            and all(later.input_tokens >= earlier.input_tokens
                    for earlier, later in pairwise(input_turns))):
        first, last = input_turns[0], input_turns[-1]
        recommendations.append({
            "code": "review_input_growth", "status": "suggested",
            "observation": (f"Reported input grew from {first.input_tokens:,} to "
                            f"{last.input_tokens:,} tokens across {len(input_turns)} recorded turns."),
            "action": "Inspect the linked turns for reusable context; test a smaller input on the same task and checks before changing the workflow.",
            "rationale": "The source reported a sustained input increase; the capture does not identify which content caused it.",
            "tradeoff": "Longer context may be necessary, and cached input can cost less than new input. No savings or quality equivalence is established.",
            "evidenceEventIds": [], "evidenceTurnIds": [first.turn_id, last.turn_id],
        })
    repeated: dict[str, list] = {}
    for event in events:
        if event.event_type is not AgentEventType.TOOL_RESULT:
            continue
        content = event.attributes.get("result")
        if not isinstance(content, str) or len(content) < 2_048 or _PLACEHOLDER.search(content):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        repeated.setdefault(digest, []).append(event)
    group = next((group for group in repeated.values() if len(group) >= 2), None)
    if group:
        linked = group[:20]
        recommendations.append({
            "code": "review_repeated_large_result", "status": "suggested",
            "observation": (f"The same captured tool result of {len(group[0].attributes['result']):,} "
                            f"characters appeared {len(group)} times."),
            "action": "Check whether later steps need the full result each time; compare a reviewed summary or reference against the same task checks.",
            "rationale": "The result text repeats in captured evidence; its contribution to model input tokens is not directly measured.",
            "tradeoff": "Repeated reads can be necessary, and summarization can remove needed detail. No token savings are verified.",
            "evidenceEventIds": [event.event_id for event in linked],
            "evidenceTurnIds": list(dict.fromkeys(event.turn_id for event in linked)),
        })
    return recommendations
