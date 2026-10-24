"""Deterministic, judge-free analysis of typed agent evidence."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRunBundle,
    EvidenceState,
    ExecutionStatus,
    event_order_key,
)
from verdict.local_source_revision import LocalSourceRevision, local_scope_partial

Severity = Literal["info", "warning", "error"]
_UNSPECIFIED_LOCAL_REVISION = object()


@dataclass(frozen=True)
class AnalysisPolicy:
    required_event_types: tuple[AgentEventType, ...] = ()
    prohibited_tools: tuple[str, ...] = ()
    expected_response_format: Literal["json"] | None = None
    repeated_tool_call_threshold: int = 4

    def __post_init__(self) -> None:
        if self.repeated_tool_call_threshold < 2:
            raise ValueError("repeated_tool_call_threshold must be at least 2")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    message: str
    evidence_event_ids: tuple[str, ...] = ()
    judge_used: bool = False


@dataclass(frozen=True)
class AgentRunAnalysis:
    run_id: str
    metrics: dict[str, int | float]
    evidence_coverage: dict[str, str]
    findings: tuple[Finding, ...] = field(default_factory=tuple)


def _finding(
    code: str,
    severity: Severity,
    message: str,
    events: list[AgentEvent] | tuple[AgentEvent, ...] = (),
) -> Finding:
    return Finding(code, severity, message, tuple(event.event_id for event in events[:20]))


def outcome_reported(event: AgentEvent) -> bool:
    """Whether the source reported success or failure for an execution event.

    This mirrors the failure predicates below: a zero failure count only means
    "no failures" when at least one event of that type reported an outcome.
    """
    if event.status in {
        ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT,
    }:
        return True
    attributes = event.attributes
    return (
        isinstance(attributes.get("is_error"), bool)
        or type(attributes.get("exit_code")) is int
        or type(attributes.get("failed")) is int
    )


def recorded_event_issue(event_type: str, status: str, attributes: dict) -> str | None:
    """Classify an explicit event failure without interpreting free-form text."""
    if event_type == AgentEventType.TOOL_RESULT.value and (
        status in {"failed", "timed_out"} or attributes.get("is_error") is True
    ):
        return "tool_error"
    if event_type == AgentEventType.COMMAND.value and (
        status in {"failed", "timed_out"} or (
            type(attributes.get("exit_code")) is int and attributes["exit_code"] != 0
        )
    ):
        return "command_failed"
    if event_type == AgentEventType.TEST_RESULT.value and (
        status in {"failed", "timed_out"} or (
            type(attributes.get("failed")) is int and attributes["failed"] > 0
        ) or (
            type(attributes.get("exit_code")) is int and attributes["exit_code"] != 0
        )
    ):
        return "test_failed"
    if event_type == AgentEventType.OUTCOME.value and status == "failed":
        return "business_outcome_failed"
    return None


def recorded_tool_pairing_issues(
    calls: list[object], results: list[object],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Indexes of records that prevent one-to-one pairing by source call ID.

    Result-only legacy evidence has no call denominator; this predicate does
    not turn that absence into an execution failure or a completeness claim.
    """
    if not calls:
        return (), ()

    def valid(value: object) -> bool:
        if not isinstance(value, str) or not value or "\x00" in value:
            return False
        try:
            return len(value.encode("utf-8")) <= 256
        except UnicodeError:
            return False

    call_counts = Counter(value for value in calls if valid(value))
    result_counts = Counter(value for value in results if valid(value))
    bad_calls = tuple(
        index for index, value in enumerate(calls)
        if not valid(value) or call_counts[value] != 1 or result_counts[value] != 1
    )
    bad_results = tuple(
        index for index, value in enumerate(results)
        if not valid(value) or result_counts[value] != 1 or call_counts[value] != 1
    )
    return bad_calls, bad_results


def recorded_tool_pairing_complete(calls: list[object], results: list[object]) -> bool:
    """Whether recorded calls have exactly one result each by bounded source ID."""
    bad_calls, bad_results = recorded_tool_pairing_issues(calls, results)
    return not bad_calls and not bad_results


def analyze_agent_run(
    bundle: AgentRunBundle, policy: AnalysisPolicy | None = None, *,
    include_tool_outcome_coverage: bool = False,
    local_revision: object = _UNSPECIFIED_LOCAL_REVISION,
) -> AgentRunAnalysis:
    """Derive only facts supported by typed evidence and explicit policy."""
    selected_policy = policy or AnalysisPolicy()
    findings: list[Finding] = []
    events_by_type = {
        event_type: [event for event in bundle.events if event.event_type is event_type]
        for event_type in AgentEventType
    }
    tool_calls = events_by_type[AgentEventType.TOOL_CALL]
    tool_results = events_by_type[AgentEventType.TOOL_RESULT]
    tool_errors = [
        event
        for event in tool_results
        if recorded_event_issue(event.event_type.value, event.status.value, event.attributes)
        == "tool_error"
    ]
    command_failures = [
        event
        for event in events_by_type[AgentEventType.COMMAND]
        if recorded_event_issue(event.event_type.value, event.status.value, event.attributes)
        == "command_failed"
    ]
    test_failures = [
        event
        for event in events_by_type[AgentEventType.TEST_RESULT]
        if recorded_event_issue(event.event_type.value, event.status.value, event.attributes)
        == "test_failed"
    ]
    retries = events_by_type[AgentEventType.RETRY]
    failed_business_outcomes = [
        event
        for event in events_by_type[AgentEventType.OUTCOME]
        if recorded_event_issue(event.event_type.value, event.status.value, event.attributes)
        == "business_outcome_failed"
    ]
    model_calls = events_by_type[AgentEventType.MODEL_CALL]
    input_tokens = sum(
        value
        for event in model_calls
        if isinstance((value := event.attributes.get("input_tokens")), int)
    )
    output_tokens = sum(
        value
        for event in model_calls
        if isinstance((value := event.attributes.get("output_tokens")), int)
    )
    latencies = [
        float(value)
        for event in model_calls
        if isinstance((value := event.attributes.get("latency_ms")), (int, float))
    ]
    metrics: dict[str, int | float] = {
        "turns": len(bundle.turns),
        "model_calls": len(model_calls),
        "tool_calls": len(tool_calls),
        "tool_errors": len(tool_errors),
        "command_failures": len(command_failures),
        "test_failures": len(test_failures),
        "retries": len(retries),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model_latency_ms": sum(latencies),
    }
    request_states = {turn.request_state for turn in bundle.turns}
    response_states = {turn.response_state for turn in bundle.turns}
    capture_markers = [
        event for event in bundle.events if event.provenance == "verdict:capture_limit"
    ]
    if local_revision is _UNSPECIFIED_LOCAL_REVISION:
        source_partial = False
    else:
        if local_revision is not None and not isinstance(local_revision, LocalSourceRevision):
            raise ValueError("invalid local source revision")
        source_partial = local_scope_partial(
            bundle.session.source_kind, local_revision,
            max((turn.sequence for turn in bundle.turns), default=-1), None,
        )
    coverage = {
        "request": _coverage(request_states),
        "response": _coverage(response_states),
        "event_stream": "partial" if capture_markers or source_partial else "present",
    }

    if bundle.run.status.value == "unknown":
        findings.append(
            _finding(
                "run_status_unknown",
                "info",
                "The source does not expose a terminal status for this agent session.",
            )
        )
    if coverage["response"] in {"missing", "not_captured", "mixed"}:
        findings.append(
            _finding(
                "response_not_evaluable",
                "info",
                "At least one response is unavailable; response quality is not evaluable.",
            )
        )
    if capture_markers:
        findings.append(
            _finding(
                "event_capture_partial",
                "warning",
                "The source exceeded the bounded event limit; event-level analysis is partial.",
                capture_markers,
            )
        )
    if source_partial:
        findings.append(_finding(
            "source_capture_partial", "warning",
            "The local source contains evidence outside this historical prefix, an incomplete final line, or unverified coverage. Whole-run analysis is partial.",
        ))
    if include_tool_outcome_coverage and tool_calls and not (capture_markers or source_partial):
        call_ids = [event.attributes.get("call_id") for event in tool_calls]
        result_ids = [event.attributes.get("call_id") for event in tool_results]
        bad_calls, bad_results = recorded_tool_pairing_issues(call_ids, result_ids)
        if bad_calls or bad_results:
            affected = sorted(
                [*(tool_calls[index] for index in bad_calls),
                 *(tool_results[index] for index in bad_results)],
                key=event_order_key,
            )
            findings.append(_finding(
                "tool_outcomes_incomplete", "warning",
                "Recorded tool calls and results cannot be paired one-to-one; their outcomes are unclear.",
                affected,
            ))
    if tool_errors:
        findings.append(
            _finding(
                "tool_error",
                "error",
                f"{len(tool_errors)} tool result(s) reported failure.",
                tool_errors,
            )
        )
    if command_failures:
        findings.append(
            _finding(
                "command_failed",
                "error",
                f"{len(command_failures)} command(s) reported failure, timeout, or a non-zero exit.",
                command_failures,
            )
        )
    if test_failures:
        findings.append(
            _finding(
                "test_failed",
                "error",
                f"{len(test_failures)} test execution(s) reported failure or timeout.",
                test_failures,
            )
        )
    if failed_business_outcomes:
        findings.append(
            _finding(
                "business_outcome_failed",
                "error",
                f"{len(failed_business_outcomes)} explicit business outcome(s) reported failure.",
                failed_business_outcomes,
            )
        )

    required = set(selected_policy.required_event_types)
    for missing in sorted(
        (required - {event.event_type for event in bundle.events})
        if not (capture_markers or source_partial) else (), key=str,
    ):
        findings.append(
            _finding(
                "required_step_missing",
                "error",
                f"Required evidence type {missing.value!r} was not observed.",
            )
        )
    prohibited = {name.casefold() for name in selected_policy.prohibited_tools}
    prohibited_calls = [
        event
        for event in tool_calls
        if str(event.attributes.get("tool_name") or "").casefold() in prohibited
    ]
    if prohibited_calls:
        names = sorted({str(event.attributes.get("tool_name")) for event in prohibited_calls})
        findings.append(
            _finding(
                "prohibited_tool_used",
                "error",
                f"Prohibited tool(s) {', '.join(names)} were called {len(prohibited_calls)} time(s).",
                prohibited_calls,
            )
        )
    findings.extend(_loop_findings(tool_calls, selected_policy.repeated_tool_call_threshold))
    if selected_policy.expected_response_format == "json":
        invalid_responses = 0
        last_source_sequence = (
            local_revision.source_turn_count - 1
            if local_revision is not _UNSPECIFIED_LOCAL_REVISION
            and local_revision is not None
            and local_revision.source_turn_count is not None else
            max((turn.sequence for turn in bundle.turns), default=-1)
        )
        for turn in bundle.turns:
            if (local_revision is not _UNSPECIFIED_LOCAL_REVISION
                    and local_revision is not None
                    and local_revision.partial_final_line
                    and turn.sequence == last_source_sequence):
                continue
            if turn.response_state is not EvidenceState.PRESENT:
                continue
            try:
                json.loads(turn.final_response_redacted or "")
            except (json.JSONDecodeError, RecursionError):
                invalid_responses += 1
        if invalid_responses:
            findings.append(
                _finding(
                    "response_schema_invalid",
                    "error",
                    f"{invalid_responses} response(s) were not valid JSON as required by policy.",
                )
            )
    return AgentRunAnalysis(bundle.run.run_id, metrics, coverage, tuple(findings))


def _coverage(states: set[EvidenceState]) -> str:
    if not states:
        return "not_applicable"
    if len(states) == 1:
        return next(iter(states)).value
    return "mixed"


def _loop_findings(tool_calls: list[AgentEvent], threshold: int) -> list[Finding]:
    grouped: dict[tuple[str, str, str], list[AgentEvent]] = {}
    for event in tool_calls:
        arguments = event.attributes.get("arguments")
        if arguments in (None, ""):
            continue
        signature_arguments = (
            arguments
            if isinstance(arguments, str)
            else json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        signature = (
            event.turn_id,
            str(event.attributes.get("tool_name") or "").casefold(),
            signature_arguments,
        )
        grouped.setdefault(signature, []).append(event)
    loops = [events for events in grouped.values() if len(events) >= threshold]
    if not loops:
        return []
    evidence = [event for events in loops for event in events]
    return [
        _finding(
            "possible_tool_loop",
            "warning",
            f"{len(loops)} tool-call pattern{'s' if len(loops) != 1 else ''} "
            f"repeated at least {threshold} times within a turn "
            f"({len(evidence)} recorded calls total).",
            evidence,
        )
    ]
