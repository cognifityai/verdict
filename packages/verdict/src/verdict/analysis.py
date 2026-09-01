"""Deterministic, judge-free analysis of typed agent evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from verdict.evidence import AgentEvent, AgentEventType, AgentRunBundle, EvidenceState

Severity = Literal["info", "warning", "error"]


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


def analyze_agent_run(
    bundle: AgentRunBundle, policy: AnalysisPolicy | None = None
) -> AgentRunAnalysis:
    """Derive only facts supported by typed evidence and explicit policy."""
    selected_policy = policy or AnalysisPolicy()
    findings: list[Finding] = []
    events_by_type = {
        event_type: [event for event in bundle.events if event.event_type is event_type]
        for event_type in AgentEventType
    }
    tool_calls = events_by_type[AgentEventType.TOOL_CALL]
    tool_errors = [
        event
        for event in events_by_type[AgentEventType.TOOL_RESULT]
        if event.status.value == "failed" or event.attributes.get("is_error") is True
    ]
    command_failures = [
        event
        for event in events_by_type[AgentEventType.COMMAND]
        if event.status.value == "failed"
        or (
            isinstance(event.attributes.get("exit_code"), int)
            and event.attributes["exit_code"] != 0
        )
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
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model_latency_ms": sum(latencies),
    }
    request_states = {turn.request_state for turn in bundle.turns}
    response_states = {turn.response_state for turn in bundle.turns}
    capture_markers = [
        event for event in bundle.events if event.provenance == "verdict:capture_limit"
    ]
    coverage = {
        "request": _coverage(request_states),
        "response": _coverage(response_states),
        "event_stream": "partial" if capture_markers else "present",
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
    if tool_errors:
        findings.append(_finding(
            "tool_error", "error",
            f"{len(tool_errors)} tool result(s) reported failure.", tool_errors,
        ))
    if command_failures:
        findings.append(
            _finding(
                "command_failed", "error",
                f"{len(command_failures)} command(s) returned a non-zero status.",
                command_failures,
            )
        )

    required = set(selected_policy.required_event_types)
    for missing in sorted(required - {event.event_type for event in bundle.events}, key=str):
        findings.append(
            _finding(
                "required_step_missing",
                "error",
                f"Required evidence type {missing.value!r} was not observed.",
            )
        )
    prohibited = {name.casefold() for name in selected_policy.prohibited_tools}
    prohibited_calls = [
        event for event in tool_calls
        if str(event.attributes.get("tool_name") or "").casefold() in prohibited
    ]
    if prohibited_calls:
        names = sorted({str(event.attributes.get("tool_name")) for event in prohibited_calls})
        findings.append(_finding(
            "prohibited_tool_used", "error",
            f"Prohibited tool(s) {', '.join(names)} were called {len(prohibited_calls)} time(s).",
            prohibited_calls,
        ))
    findings.extend(_loop_findings(tool_calls, selected_policy.repeated_tool_call_threshold))
    if selected_policy.expected_response_format == "json":
        invalid_responses = 0
        for turn in bundle.turns:
            if turn.response_state is not EvidenceState.PRESENT:
                continue
            try:
                json.loads(turn.final_response_redacted or "")
            except (json.JSONDecodeError, RecursionError):
                invalid_responses += 1
        if invalid_responses:
            findings.append(_finding(
                "response_schema_invalid", "error",
                f"{invalid_responses} response(s) were not valid JSON as required by policy.",
            ))
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
        if not isinstance(arguments, str) or not arguments:
            continue
        signature = (
            event.turn_id,
            str(event.attributes.get("tool_name") or "").casefold(),
            arguments,
        )
        grouped.setdefault(signature, []).append(event)
    loops = [events for events in grouped.values() if len(events) >= threshold]
    if not loops:
        return []
    evidence = [event for events in loops for event in events]
    return [_finding(
        "possible_tool_loop", "warning",
        f"{len(loops)} identical turn/tool/argument call(s) reached the repeated-call threshold.",
        evidence,
    )]
