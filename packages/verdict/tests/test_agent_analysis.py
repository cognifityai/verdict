from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from verdict import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
)
from verdict.analysis import AnalysisPolicy, analyze_agent_run

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _bundle(*, response: str | None = "done") -> AgentRunBundle:
    session = SourceSession("session", "tenant", "codex", "a" * 64, NOW, NOW)
    run = AgentRun("run", "session", "tenant", NOW, ExecutionStatus.UNKNOWN)
    turn = AgentTurn(
        "turn",
        "run",
        0,
        NOW,
        ExecutionStatus.COMPLETED,
        NOW + timedelta(seconds=5),
        "fix tests",
        response,
        EvidenceState.PRESENT,
        EvidenceState.PRESENT if response is not None else EvidenceState.MISSING,
    )
    events = (
        AgentEvent(
            "e0",
            "turn",
            0,
            NOW,
            AgentEventType.MODEL_CALL,
            ExecutionStatus.COMPLETED,
            "test:model",
            {
                "provider": "anthropic",
                "response_model": "sonnet",
                "input_tokens": 10,
                "output_tokens": 5,
                "latency_ms": 250,
            },
        ),
        AgentEvent(
            "e1",
            "turn",
            1,
            NOW,
            AgentEventType.TOOL_CALL,
            ExecutionStatus.UNKNOWN,
            "test:tool",
            {"tool_name": "shell", "call_id": "c1", "arguments": '{"cmd":"pytest"}'},
            PrivacyClassification.REDACTED,
        ),
        AgentEvent(
            "e2",
            "turn",
            2,
            NOW,
            AgentEventType.TOOL_RESULT,
            ExecutionStatus.FAILED,
            "test:result",
            {"tool_name": "shell", "call_id": "c1", "is_error": True},
        ),
        AgentEvent(
            "e3",
            "turn",
            3,
            NOW,
            AgentEventType.COMMAND,
            ExecutionStatus.FAILED,
            "test:command",
            {"exit_code": 1},
        ),
    )
    return AgentRunBundle(session, run, (turn,), events)


def test_analysis_reports_observed_failures_and_metrics_without_judge() -> None:
    report = analyze_agent_run(_bundle())

    assert report.metrics == {
        "turns": 1,
        "model_calls": 1,
        "tool_calls": 1,
        "tool_errors": 1,
        "command_failures": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "model_latency_ms": 250.0,
    }
    assert {finding.code for finding in report.findings} == {
        "run_status_unknown",
        "tool_error",
        "command_failed",
    }
    assert all(finding.judge_used is False for finding in report.findings)


def test_missing_evidence_is_not_misclassified_as_failure() -> None:
    bundle = _bundle(response=None)

    report = analyze_agent_run(bundle)

    assert report.evidence_coverage["response"] == "missing"
    assert any(finding.code == "response_not_evaluable" for finding in report.findings)
    assert not any(finding.code == "response_failed" for finding in report.findings)


def test_policy_checks_required_steps_prohibited_tools_and_json() -> None:
    report = analyze_agent_run(
        _bundle(response="not json"),
        AnalysisPolicy(
            required_event_types=(AgentEventType.TEST_RESULT,),
            prohibited_tools=("shell",),
            expected_response_format="json",
        ),
    )

    codes = {finding.code for finding in report.findings}
    assert {"required_step_missing", "prohibited_tool_used", "response_schema_invalid"} <= codes


def test_loop_detection_is_explicit_and_policy_controlled() -> None:
    bundle = _bundle()
    repeats = tuple(
        replace(event, event_id=f"loop-{index}", sequence=4 + index)
        for index, event in enumerate([bundle.events[1]] * 3)
    )

    report = analyze_agent_run(
        replace(bundle, events=bundle.events + repeats),
        AnalysisPolicy(repeated_tool_call_threshold=4),
    )

    loop = next(finding for finding in report.findings if finding.code == "possible_tool_loop")
    assert loop.evidence_event_ids == ("e1", "loop-0", "loop-1", "loop-2")


def test_different_calls_to_the_same_tool_are_not_called_a_loop() -> None:
    bundle = _bundle()
    calls = tuple(
        replace(
            bundle.events[1],
            event_id=f"different-{index}",
            sequence=4 + index,
            attributes={
                "tool_name": "shell",
                "call_id": f"different-{index}",
                "arguments": f'{{"cmd":"step-{index}"}}',
            },
        )
        for index in range(5)
    )

    report = analyze_agent_run(
        replace(bundle, events=bundle.events + calls),
        AnalysisPolicy(repeated_tool_call_threshold=4),
    )

    assert not any(finding.code == "possible_tool_loop" for finding in report.findings)


def test_omitted_capture_is_exposed_as_a_limit_not_silent_completeness() -> None:
    bundle = _bundle()
    marker = AgentEvent(
        "limit",
        "turn",
        4,
        NOW,
        AgentEventType.CONTEXT,
        ExecutionStatus.UNKNOWN,
        "verdict:capture_limit",
        {"name": "source_events_omitted", "source": "12", "available": False},
        PrivacyClassification.METADATA,
    )

    report = analyze_agent_run(replace(bundle, events=(*bundle.events, marker)))

    assert report.evidence_coverage["event_stream"] == "partial"
    assert any(finding.code == "event_capture_partial" for finding in report.findings)
