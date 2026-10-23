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
        "test_failures": 0,
        "retries": 0,
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


def test_timed_out_tool_result_is_a_recorded_failure() -> None:
    bundle = _bundle()
    timeout = replace(bundle.events[2], status=ExecutionStatus.TIMED_OUT,
                      attributes={"is_error": False})
    report = analyze_agent_run(replace(bundle, events=(*bundle.events[:2], timeout)))
    assert report.metrics["tool_errors"] == 1
    assert any(finding.code == "tool_error" and finding.evidence_event_ids == ("e2",)
               for finding in report.findings)


def test_unmatched_tool_call_is_a_coverage_finding_not_an_execution_failure() -> None:
    bundle = _bundle()
    completed = replace(bundle.events[2], status=ExecutionStatus.COMPLETED,
                        attributes={"tool_name": "shell", "call_id": "c1", "is_error": False})
    second_call = replace(bundle.events[1], event_id="unmatched-call", sequence=3,
                          attributes={"tool_name": "shell", "call_id": "c2"})
    report = analyze_agent_run(replace(
        bundle, events=(bundle.events[0], bundle.events[1], completed, second_call),
    ), include_tool_outcome_coverage=True)
    gap = next(finding for finding in report.findings
               if finding.code == "tool_outcomes_incomplete")
    assert gap.severity == "warning"
    assert "unmatched-call" in gap.evidence_event_ids
    assert not any(finding.code == "tool_error" for finding in report.findings)
    from verdict.dashboard.recommendations import run_recommendations

    suggestion = next(item for item in run_recommendations(replace(
        bundle, events=(bundle.events[0], bundle.events[1], completed, second_call),
    )) if item["code"] == "review_missing_tool_outcomes")
    assert "unmatched-call" in suggestion["evidenceEventIds"]
    assert "source capture" in suggestion["action"]
    paired = analyze_agent_run(replace(bundle, events=(
        replace(bundle.events[1], sequence=0), replace(completed, sequence=1),
    )), include_tool_outcome_coverage=True)
    assert not any(finding.code == "tool_outcomes_incomplete" for finding in paired.findings)


def test_recorded_tool_failure_is_the_first_run_action_even_with_capture_gap() -> None:
    from verdict.dashboard.recommendations import run_recommendations

    bundle = _bundle()
    unmatched = replace(bundle.events[1], event_id="unmatched-call", sequence=4,
                        attributes={"tool_name": "shell", "call_id": "c2"})
    suggestions = run_recommendations(replace(bundle, events=(*bundle.events, unmatched)))

    assert [item["code"] for item in suggestions[:2]] == [
        "review_tool_failure", "review_missing_tool_outcomes",
    ]
    assert suggestions[0]["evidenceEventIds"] == ["e2"]
    assert "cause" not in suggestions[0]["observation"].lower()


def test_tool_action_uses_only_positive_reported_failure_evidence() -> None:
    from verdict.dashboard.recommendations import run_recommendations

    bundle = _bundle()
    result = bundle.events[2]
    cases = (
        (ExecutionStatus.TIMED_OUT, {"is_error": False}, True),
        (ExecutionStatus.COMPLETED, {"is_error": True}, True),
        (ExecutionStatus.UNKNOWN, {"is_error": False}, False),
        (ExecutionStatus.UNKNOWN, {}, False),
    )
    for status, attributes, expected in cases:
        changed = replace(result, status=status,
                          sequence=1, attributes={"call_id": "c1", **attributes})
        recommendations = run_recommendations(replace(
            bundle, events=(replace(bundle.events[1], sequence=0), changed),
        ))
        has_failure = any(item["code"] == "review_tool_failure"
                          for item in recommendations)
        assert has_failure is expected, (status, attributes)

    from verdict.analysis import recorded_event_issue
    assert recorded_event_issue("tool_result", "unknown", {"is_error": 1}) is None


def test_repeated_tool_failure_keeps_existing_recommendation_code() -> None:
    from verdict.dashboard.recommendations import run_recommendations

    bundle = _bundle()
    second = replace(bundle.events[2], event_id="second-failure", sequence=4,
                     attributes={"tool_name": "shell", "call_id": "c2", "is_error": True})
    suggestions = run_recommendations(replace(bundle, events=(*bundle.events, second)))
    assert suggestions[0]["code"] == "review_repeated_tool_failures"
    assert suggestions[0]["evidenceEventIds"] == ["e2", "second-failure"]


def test_capture_limit_suppresses_gap_advice_but_keeps_recorded_failure() -> None:
    from verdict.dashboard.recommendations import run_recommendations

    bundle = _bundle()
    unmatched = replace(bundle.events[1], event_id="unmatched-call", sequence=4,
                        attributes={"tool_name": "shell", "call_id": "c2"})
    marker = replace(bundle.events[0], event_id="capture-limit", sequence=5,
                     provenance="verdict:capture_limit")
    suggestions = run_recommendations(replace(
        bundle, events=(*bundle.events, unmatched, marker),
    ))
    assert suggestions[0]["code"] == "review_tool_failure"
    assert not any(item["code"] == "review_missing_tool_outcomes"
                   for item in suggestions)


def test_recorded_failed_test_precedes_tool_capture_gap_advice() -> None:
    from verdict.dashboard.recommendations import run_recommendations

    bundle = _bundle()
    completed = replace(bundle.events[2], status=ExecutionStatus.COMPLETED,
                        attributes={"tool_name": "shell", "call_id": "c1", "is_error": False})
    failed_test = replace(bundle.events[3], event_id="failed-test", sequence=4,
                          event_type=AgentEventType.TEST_RESULT,
                          attributes={"failed": 1})
    unmatched = replace(bundle.events[1], event_id="unmatched-call", sequence=5,
                        attributes={"tool_name": "shell", "call_id": "c2"})
    suggestions = run_recommendations(replace(bundle, events=(
        bundle.events[0], bundle.events[1], completed, bundle.events[3],
        failed_test, unmatched,
    )))
    assert [item["code"] for item in suggestions[:2]] == [
        "review_last_failed_test", "review_missing_tool_outcomes",
    ]
    assert suggestions[0]["evidenceEventIds"] == ["failed-test"]


def test_pairing_gap_links_the_actual_unmatched_call_beyond_twenty_events() -> None:
    from verdict.dashboard.recommendations import run_recommendations

    bundle = _bundle()
    calls = tuple(replace(bundle.events[1], event_id=f"call-{index}", sequence=index,
                          attributes={"tool_name": "shell", "call_id": f"c{index}"})
                  for index in range(21))
    results = tuple(replace(bundle.events[2], event_id=f"result-{index}",
                            sequence=21 + index, status=ExecutionStatus.COMPLETED,
                            attributes={"tool_name": "shell", "call_id": f"c{index}",
                                        "is_error": False})
                    for index in range(20))
    captured = replace(bundle, events=(*calls, *results))

    finding = next(item for item in analyze_agent_run(
        captured, include_tool_outcome_coverage=True,
    ).findings if item.code == "tool_outcomes_incomplete")
    suggestion = next(item for item in run_recommendations(captured)
                      if item["code"] == "review_missing_tool_outcomes")
    assert finding.evidence_event_ids == ("call-20",)
    assert suggestion["evidenceEventIds"] == ["call-20"]


def test_pairing_gap_links_duplicate_invalid_and_orphan_record_evidence() -> None:
    from verdict.dashboard.recommendations import run_recommendations

    bundle = _bundle()
    calls = (
        replace(bundle.events[1], event_id="call-a", sequence=0,
                attributes={"call_id": "a"}),
        replace(bundle.events[1], event_id="call-a-duplicate", sequence=1,
                attributes={"call_id": "a"}),
        replace(bundle.events[1], event_id="call-no-id", sequence=2,
                attributes={"tool_name": "shell"}),
    )
    results = (
        replace(bundle.events[2], event_id="result-a", sequence=3,
                status=ExecutionStatus.COMPLETED,
                attributes={"call_id": "a", "is_error": False}),
        replace(bundle.events[2], event_id="result-orphan", sequence=4,
                status=ExecutionStatus.COMPLETED,
                attributes={"call_id": "orphan", "is_error": False}),
    )
    captured = replace(bundle, events=(*calls, *results))
    expected = {"call-a", "call-a-duplicate", "call-no-id", "result-a", "result-orphan"}
    finding = next(item for item in analyze_agent_run(
        captured, include_tool_outcome_coverage=True,
    ).findings if item.code == "tool_outcomes_incomplete")
    suggestion = next(item for item in run_recommendations(captured)
                      if item["code"] == "review_missing_tool_outcomes")
    assert set(finding.evidence_event_ids) == expected
    assert set(suggestion["evidenceEventIds"]) == expected


def test_pairing_classifier_handles_orphan_duplicate_result_and_legacy_result_only() -> None:
    from verdict.analysis import recorded_tool_pairing_issues

    assert recorded_tool_pairing_issues(["a"], ["a", "orphan"]) == ((), (1,))
    assert recorded_tool_pairing_issues(["a"], ["a", "a"]) == ((0,), (0, 1))
    assert recorded_tool_pairing_issues([], ["orphan"]) == ((), ())
    assert recorded_tool_pairing_issues(["a", None], ["a"]) == ((1,), ())


def test_boolean_numeric_attributes_are_not_reported_command_or_test_outcomes() -> None:
    from types import SimpleNamespace

    from verdict.analysis import outcome_reported, recorded_event_issue

    for event_type, attributes in (
        ("command", {"exit_code": True}),
        ("test_result", {"failed": True}),
        ("test_result", {"exit_code": True}),
    ):
        assert recorded_event_issue(event_type, "completed", attributes) is None
        event = SimpleNamespace(status=ExecutionStatus.UNKNOWN, attributes=attributes)
        assert outcome_reported(event) is False
    assert recorded_event_issue("command", "completed", {"exit_code": 1}) == "command_failed"
    assert recorded_event_issue("test_result", "completed", {"failed": 1}) == "test_failed"


def test_missing_evidence_is_not_misclassified_as_failure() -> None:
    bundle = _bundle(response=None)

    report = analyze_agent_run(bundle)

    assert report.evidence_coverage["response"] == "missing"
    assert any(finding.code == "response_not_evaluable" for finding in report.findings)
    assert not any(finding.code == "response_failed" for finding in report.findings)


def test_explicit_failed_business_outcome_is_reported() -> None:
    bundle = _bundle()
    failed_outcome = AgentEvent(
        "business-outcome",
        "turn",
        4,
        NOW,
        AgentEventType.OUTCOME,
        ExecutionStatus.FAILED,
        "verdict:sdk",
        {"name": "workflow_success", "value": False, "source": "application"},
        PrivacyClassification.REDACTED,
    )

    report = analyze_agent_run(replace(bundle, events=(*bundle.events, failed_outcome)))

    finding = next(
        finding for finding in report.findings if finding.code == "business_outcome_failed"
    )
    assert finding.severity == "error"
    assert finding.evidence_event_ids == ("business-outcome",)


def test_completed_outcomes_do_not_imply_business_failure() -> None:
    bundle = _bundle()
    descriptive_outcome = AgentEvent(
        "descriptive-outcome",
        "turn",
        4,
        NOW,
        AgentEventType.OUTCOME,
        ExecutionStatus.COMPLETED,
        "verdict:sdk",
        {"name": "step_accepted", "value": False, "source": "application"},
        PrivacyClassification.REDACTED,
    )

    report = analyze_agent_run(replace(bundle, events=(*bundle.events, descriptive_outcome)))

    assert not any(
        finding.code == "business_outcome_failed" for finding in report.findings
    )


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
    assert loop.message == (
        "1 tool-call pattern repeated at least 4 times within a turn "
        "(4 recorded calls total)."
    )


def test_loop_detection_canonicalizes_structured_tool_arguments() -> None:
    bundle = _bundle()
    repeats = tuple(
        replace(
            bundle.events[1],
            event_id=f"structured-{index}",
            sequence=4 + index,
            attributes={
                "tool_name": "shell",
                "call_id": f"structured-{index}",
                "arguments": {"cwd": "/tmp", "cmd": "pytest"},
            },
        )
        for index in range(4)
    )

    report = analyze_agent_run(
        replace(bundle, events=bundle.events + repeats),
        AnalysisPolicy(repeated_tool_call_threshold=4),
    )

    assert any(finding.code == "possible_tool_loop" for finding in report.findings)


def test_analysis_counts_explicit_retries_and_failed_tests() -> None:
    bundle = _bundle()
    retry = AgentEvent(
        "retry",
        "turn",
        4,
        NOW,
        AgentEventType.RETRY,
        ExecutionStatus.COMPLETED,
        "verdict:sdk",
        {"reason": "rate limit", "attempt": 1},
        PrivacyClassification.REDACTED,
    )
    test_result = AgentEvent(
        "test",
        "turn",
        5,
        NOW,
        AgentEventType.TEST_RESULT,
        ExecutionStatus.FAILED,
        "verdict:sdk",
        {"command": "pytest", "exit_code": 1, "passed": 3, "failed": 1, "skipped": 0},
        PrivacyClassification.REDACTED,
    )

    report = analyze_agent_run(replace(bundle, events=(*bundle.events, retry, test_result)))

    assert report.metrics["retries"] == 1
    assert report.metrics["test_failures"] == 1
    assert any(finding.code == "test_failed" for finding in report.findings)


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
    extra_call = replace(bundle.events[1], event_id="unmatched-after-limit", sequence=5,
                         attributes={"call_id": "c2"})
    partial = replace(bundle, events=(*bundle.events, marker, extra_call))
    report = analyze_agent_run(partial, include_tool_outcome_coverage=True)
    assert not any(finding.code == "tool_outcomes_incomplete" for finding in report.findings)
    from verdict.dashboard.recommendations import run_recommendations

    assert not any(item["code"] == "review_missing_tool_outcomes"
                   for item in run_recommendations(partial))
