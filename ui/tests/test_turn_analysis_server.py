"""On-demand Turn review through the real dashboard and normalized store."""

import asyncio
from datetime import datetime, timezone

import httpx
from verdict.dashboard.app import create_app
from verdict.dashboard.turn_analysis import analyze_turn_events
from verdict.evidence import (
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
from verdict.storage import SQLiteStorage


def _bundle(tenant: str, *, run_id: str = "run", events=()) -> AgentRunBundle:
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    return AgentRunBundle(
        SourceSession(f"source-{tenant}", tenant, "synthetic-test-agent", "a" * 64, now, now),
        AgentRun(run_id, f"source-{tenant}", tenant, now, ExecutionStatus.UNKNOWN),
        (AgentTurn("turn", run_id, 0, now, ExecutionStatus.COMPLETED, now,
                   "Fix the parser", "Done", EvidenceState.PRESENT, EvidenceState.PRESENT),),
        tuple(events),
    )


def _event(event_id: str, event_type: AgentEventType, sequence: int,
           status: ExecutionStatus, attributes: dict) -> AgentEvent:
    return AgentEvent(
        event_id, "turn", sequence, datetime(2026, 10, 8, tzinfo=timezone.utc),
        event_type, status, "codex:synthetic", attributes,
        PrivacyClassification.REDACTED,
    )


def _get(path, route, tenant):
    async def request():
        transport = httpx.ASGITransport(app=create_app(
            storage=f"sqlite:///{path}", tenant_id=tenant,
        ))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(route)
    return asyncio.run(request())


def test_turn_analysis_links_actions_and_failures_without_inventing_reasoning(tmp_path):
    path = tmp_path / "turn-analysis.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle("local", events=(
        _event("call", AgentEventType.TOOL_CALL, 0, ExecutionStatus.COMPLETED,
               {"tool_name": "read_alice@example.com", "call_id": "call-1",
                "arguments": {"path": "secret"}}),
        _event("result", AgentEventType.TOOL_RESULT, 1, ExecutionStatus.COMPLETED,
               {"tool_name": "read_file", "call_id": "call-1", "result": "contents", "is_error": False}),
        _event("test", AgentEventType.TEST_RESULT, 2, ExecutionStatus.FAILED,
               {"command": "pytest", "failed": 1, "output": "failure"}),
    )))
    storage.close()
    response = _get(path, "/api/runs/run/turns/turn/analysis", "local")
    assert response.status_code == 200
    analysis = response.json()
    assert analysis["runId"] == "run" and analysis["turnId"] == "turn"
    assert analysis["actions"][0]["eventId"] == "call"
    assert analysis["actions"][0]["resultEventId"] == "result"
    assert analysis["reviewSignals"][0]["eventId"] == "test"
    assert analysis["reviewSignals"][0]["code"] == "test_failed"
    assert analysis["decisionReview"][0]["eventId"] == "test"
    assert analysis["eventCoverage"]["complete"] is True
    assert "reasoning" not in str(analysis).lower()
    assert "secret" not in str(analysis) and "contents" not in str(analysis)
    assert "alice@example.com" not in str(analysis)


def test_turn_analysis_is_tenant_scoped_and_missing_turn_is_404(tmp_path):
    path = tmp_path / "turn-analysis-isolation.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle("other", events=(
        _event("other-event", AgentEventType.COMMAND, 0, ExecutionStatus.COMPLETED,
               {"command": "private"}),
    )))
    storage.close()
    assert _get(path, "/api/runs/run/turns/turn/analysis", "local").status_code == 404
    assert _get(path, "/api/runs/run/turns/missing/analysis", "other").status_code == 404
    visible = _get(path, "/api/runs/run/turns/turn/analysis", "other")
    assert visible.status_code == 200
    assert visible.json()["actions"][0]["eventId"] == "other-event"


def test_turn_analysis_reports_partial_capture_and_empty_evidence(tmp_path):
    path = tmp_path / "turn-analysis-partial.db"
    storage = SQLiteStorage(str(path))
    events = tuple(_event(f"event-{index}", AgentEventType.COMMAND, index,
                          ExecutionStatus.COMPLETED, {"command": "echo test"})
                   for index in range(105))
    storage.replace_agent_run_bundle(_bundle("local", events=events))
    storage.close()
    response = _get(path, "/api/runs/run/turns/turn/analysis", "local")
    assert response.status_code == 200
    analysis = response.json()
    assert analysis["eventCoverage"]["complete"] is False
    assert analysis["eventCoverage"]["shown"] == 100
    assert analysis["eventCoverage"]["available"] == 105
    assert len(analysis["actions"]) <= 20

    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle("local", run_id="empty"))
    storage.close()
    empty = _get(path, "/api/runs/empty/turns/turn/analysis", "local").json()
    assert empty["actions"] == [] and empty["reviewSignals"] == []
    assert empty["eventCoverage"]["complete"] is True


def test_turn_after_a_source_capture_limit_does_not_claim_complete_evidence(tmp_path):
    path = tmp_path / "later-turn-limit.db"
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(AgentRunBundle(
        SourceSession("source-local", "local", "synthetic-test-agent", "a" * 64, now, now),
        AgentRun("run", "source-local", "local", now, ExecutionStatus.UNKNOWN),
        tuple(AgentTurn(name, "run", index, now, ExecutionStatus.COMPLETED, now)
              for index, name in enumerate(("before", "marker", "later"))),
        (
            AgentEvent("early", "before", 0, now, AgentEventType.COMMAND,
                       ExecutionStatus.COMPLETED, "synthetic", {"exit_code": 0},
                       PrivacyClassification.METADATA),
            AgentEvent("limit", "marker", 0, now, AgentEventType.CONTEXT,
                       ExecutionStatus.UNKNOWN, "verdict:capture_limit",
                       {"name": "source_events_omitted", "source": "3", "available": False},
                       PrivacyClassification.METADATA),
        ),
    ))
    storage.replace_agent_run_bundle(AgentRunBundle(
        SourceSession("source-other", "other", "synthetic-test-agent", "b" * 64, now, now),
        AgentRun("clean", "source-other", "other", now, ExecutionStatus.UNKNOWN),
        (AgentTurn("other", "clean", 0, now, ExecutionStatus.COMPLETED, now),),
        (AgentEvent("foreign-limit", "other", 0, now, AgentEventType.CONTEXT,
                    ExecutionStatus.UNKNOWN, "verdict:capture_limit",
                    {"name": "source_events_omitted", "source": "1", "available": False},
                    PrivacyClassification.METADATA),),
    ))
    storage.replace_agent_run_bundle(AgentRunBundle(
        SourceSession("source-clean", "local", "synthetic-test-agent", "c" * 64, now, now),
        AgentRun("clean", "source-clean", "local", now, ExecutionStatus.UNKNOWN),
        (AgentTurn("local-clean", "clean", 0, now, ExecutionStatus.COMPLETED, now),),
        (),
    ))
    storage.close()

    before = _get(path, "/api/runs/run/turns/before/analysis", "local").json()
    later = _get(path, "/api/runs/run/turns/later/analysis", "local").json()
    clean = _get(path, "/api/runs/clean/turns/local-clean/analysis", "local").json()
    assert before["eventCoverage"]["complete"] is True
    assert before["eventCoverage"]["sourcePartial"] is False
    assert later["eventCoverage"]["available"] == 0
    assert later["eventCoverage"]["sourcePartial"] is True
    assert later["eventCoverage"]["complete"] is False
    assert clean["eventCoverage"]["sourcePartial"] is False
    assert clean["eventCoverage"]["complete"] is True


def test_turn_analysis_does_not_link_ambiguous_calls_or_claim_capture_complete():
    rows = [
        {"event_id": "call-1", "event_type": "tool_call", "status": "completed",
         "provenance": "synthetic", "attributes": {"tool_name": "lookup", "call_id": "same"}},
        {"event_id": "call-2", "event_type": "tool_call", "status": "completed",
         "provenance": "synthetic", "attributes": {"tool_name": "lookup", "call_id": "same"}},
        {"event_id": "result", "event_type": "tool_result", "status": "completed",
         "provenance": "synthetic", "attributes": {"call_id": "same", "is_error": False}},
        {"event_id": "limit", "event_type": "context", "status": "unknown",
         "provenance": "verdict:capture_limit", "attributes": {}},
    ]
    analysis = analyze_turn_events(
        run_id="run", turn_id="turn", rows=rows, available=len(rows),
        request_state="present", response_state="present",
    )
    assert all("resultEventId" not in action for action in analysis["actions"])
    assert analysis["eventCoverage"]["complete"] is False
    assert analysis["eventCoverage"]["sourcePartial"] is True

    placeholder = [
        {"event_id": "call", "event_type": "tool_call", "status": "completed",
         "provenance": "synthetic", "attributes": {"call_id": "call-<REDACTED>"}},
        {"event_id": "result", "event_type": "tool_result", "status": "completed",
         "provenance": "synthetic", "attributes": {"call_id": "call-<REDACTED>"}},
    ]
    redacted_ids = analyze_turn_events(
        run_id="run", turn_id="turn", rows=placeholder, available=2,
        request_state="present", response_state="present",
    )
    assert "resultEventId" not in redacted_ids["actions"][0]


def test_turn_analysis_withholds_final_test_inference_when_recovered_or_order_is_uncertain():
    failed = {"event_id": "failed", "event_type": "test_result", "status": "failed",
              "provenance": "synthetic", "attributes": {"failed": 1}}
    passed = {"event_id": "passed", "event_type": "test_result", "status": "completed",
              "provenance": "synthetic", "attributes": {"failed": 0, "passed": 2}}
    def analyze(rows, *, producers=1, available=None):
        return analyze_turn_events(
            run_id="run", turn_id="turn", rows=rows,
            available=len(rows) if available is None else available,
            request_state="present", response_state="present", producer_count=producers,
        )
    assert analyze([failed])["decisionReview"]
    assert analyze([failed, passed])["decisionReview"] == []
    assert analyze([failed], available=2)["decisionReview"] == []
    assert analyze([failed], producers=2)["decisionReview"] == []
