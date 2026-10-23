"""On-demand, evidence-linked review of one captured agent turn."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

MAX_TURN_ANALYSIS_EVENTS = 100
MAX_TURN_ANALYSIS_ACTIONS = 20
MAX_TURN_ANALYSIS_SIGNALS = 20

_ACTION_LABELS = {
    "tool_call": "Called a tool",
    "command": "Ran a command",
    "subagent": "Delegated work",
    "retry": "Retried an operation",
}
_FAILURE_LABELS = {
    "tool_result": ("tool_error", "Tool result reported an error"),
    "command": ("command_failed", "Command reported failure"),
    "test_result": ("test_failed", "Test execution reported failure"),
    "outcome": ("outcome_failed", "Recorded outcome reported failure"),
}


def analyze_turn_events(
    *, run_id: str, turn_id: str, rows: Sequence[Mapping[str, Any]],
    available: int, request_state: str, response_state: str, producer_count: int = 1,
    source_partial_from_run: bool = False,
) -> dict[str, Any]:
    """Describe observations only; source histories cannot reveal hidden intent."""
    events = list(rows[:MAX_TURN_ANALYSIS_EVENTS])
    results_by_call_id: dict[str, list[str]] = {}
    calls_by_call_id: dict[str, int] = {}
    for event in events:
        attributes = event["attributes"]
        call_id = attributes.get("call_id")
        if not isinstance(call_id, str) or not call_id or "<" in call_id:
            continue
        if event["event_type"] == "tool_result":
            results_by_call_id.setdefault(call_id, []).append(event["event_id"])
        elif event["event_type"] == "tool_call":
            calls_by_call_id[call_id] = calls_by_call_id.get(call_id, 0) + 1

    actions = []
    action_count = 0
    signal_count = 0
    signals = []
    for event in events:
        kind = event["event_type"]
        status = event["status"]
        attributes = event["attributes"]
        if kind in _ACTION_LABELS:
            action_count += 1
            if len(actions) < MAX_TURN_ANALYSIS_ACTIONS:
                action = {"eventId": event["event_id"], "kind": kind,
                          "label": _ACTION_LABELS[kind]}
                if kind == "tool_call":
                    name = attributes.get("tool_name")
                    if isinstance(name, str) and name:
                        action["label"] = f"Called {name[:80]}"
                    call_id = attributes.get("call_id")
                    if (isinstance(call_id, str) and "<" not in call_id
                            and calls_by_call_id.get(call_id) == 1
                            and len(results_by_call_id.get(call_id, ())) == 1):
                        action["resultEventId"] = results_by_call_id[call_id][0]
                actions.append(action)

        failure = _FAILURE_LABELS.get(kind)
        if failure is None:
            continue
        observed_failure = status == "failed"
        if kind == "tool_result":
            observed_failure = observed_failure or attributes.get("is_error") is True
        elif kind == "command":
            exit_code = attributes.get("exit_code")
            observed_failure = observed_failure or (
                type(exit_code) is int and exit_code != 0
            )
        elif kind == "test_result":
            failed = attributes.get("failed")
            observed_failure = observed_failure or (type(failed) is int and failed > 0)
        if observed_failure:
            signal_count += 1
            if len(signals) < MAX_TURN_ANALYSIS_SIGNALS:
                signals.append({"eventId": event["event_id"], "code": failure[0],
                                "label": failure[1]})

    source_partial = source_partial_from_run or any(
        event["provenance"] == "verdict:capture_limit" for event in events
    )
    complete = available <= len(events) and not source_partial
    last_test = next((event for event in reversed(events)
                      if event["event_type"] == "test_result"), None)
    decision_review = []
    if (complete and producer_count <= 1 and response_state == "present"
            and last_test is not None):
        test_attributes = last_test["attributes"]
        failed = test_attributes.get("failed")
        if last_test["status"] == "failed" or (type(failed) is int and failed > 0):
            decision_review.append({
                "eventId": last_test["event_id"],
                "label": "The last recorded test in this turn failed. A final response is "
                         "captured, but no later passing test is recorded.",
            })

    return {
        "runId": run_id,
        "turnId": turn_id,
        "requestState": request_state,
        "responseState": response_state,
        "eventCoverage": {
            "shown": len(events), "available": available,
            "complete": complete,
            "sourcePartial": source_partial,
            "multipleProducers": producer_count > 1,
        },
        "actions": actions,
        "actionsTruncated": action_count > len(actions),
        "reviewSignals": signals,
        "signalsTruncated": signal_count > len(signals),
        "decisionReview": decision_review,
    }
