"""Create a wholly synthetic local Agent Profiler demo database.

Usage: python examples/agent_profiler_demo.py --database /tmp/verdict-profiler-demo.db
The file must not already exist. No provider calls, user histories, or network
requests are made.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import verdict
from verdict.dashboard.control_plane import ControlStore
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


def _synthetic_bundle(now: datetime) -> AgentRunBundle:
    tenant = "__verdict_local__"
    source_id = "synthetic-codex-session"
    run_id = "synthetic-expensive-run"
    turns = (
        AgentTurn("synthetic-turn-1", run_id, 0, now - timedelta(days=2, hours=3),
                  ExecutionStatus.COMPLETED, now - timedelta(days=2, hours=2, minutes=55),
                  "Inspect a small project", "Initial inspection complete.",
                  EvidenceState.PRESENT, EvidenceState.PRESENT,
                  input_tokens=1_200, cached_input_tokens=200,
                  output_tokens=300, total_tokens=1_500,
                  token_usage_basis="codex_turn_delta"),
        AgentTurn("synthetic-turn-2", run_id, 1, now - timedelta(days=1, hours=2),
                  ExecutionStatus.COMPLETED, now - timedelta(days=1, hours=1, minutes=55),
                  "Investigate the failing parser", "The parser still fails one test.",
                  EvidenceState.PRESENT, EvidenceState.PRESENT,
                  input_tokens=45_000, cached_input_tokens=30_000,
                  output_tokens=1_800, total_tokens=46_800,
                  token_usage_basis="codex_turn_delta"),
    )
    events = (
        AgentEvent("synthetic-model-1", "synthetic-turn-1", 0, turns[0].started_at,
                   AgentEventType.CONTEXT, ExecutionStatus.COMPLETED, "synthetic:fixture",
                   {"name": "model_configuration", "source": "gpt-4o-mini"},
                   PrivacyClassification.METADATA),
        AgentEvent("synthetic-model-2", "synthetic-turn-2", 0, turns[1].started_at,
                   AgentEventType.CONTEXT, ExecutionStatus.COMPLETED, "synthetic:fixture",
                   {"name": "model_configuration", "source": "gpt-4o-mini"},
                   PrivacyClassification.METADATA),
        AgentEvent("synthetic-tool-contract", "synthetic-turn-2", 1,
                   turns[1].started_at + timedelta(seconds=30), AgentEventType.INSTRUCTION,
                   ExecutionStatus.COMPLETED, "synthetic:fixture",
                   {"name": "tool_contract", "text": "read_file(path) reads one existing project file"},
                   PrivacyClassification.REDACTED),
        AgentEvent("synthetic-tool-call", "synthetic-turn-2", 2,
                   turns[1].started_at + timedelta(minutes=1), AgentEventType.TOOL_CALL,
                   ExecutionStatus.COMPLETED, "synthetic:fixture",
                   {"tool_name": "read_file", "call_id": "synthetic-call",
                    "arguments": {"path": "parser.py"}},
                   PrivacyClassification.REDACTED),
        AgentEvent("synthetic-tool-result", "synthetic-turn-2", 3,
                   turns[1].started_at + timedelta(minutes=2), AgentEventType.TOOL_RESULT,
                   ExecutionStatus.FAILED, "synthetic:fixture",
                   {"tool_name": "read_file", "call_id": "synthetic-call",
                    "is_error": True, "result": "File unavailable"},
                   PrivacyClassification.REDACTED),
        AgentEvent("synthetic-failed-test", "synthetic-turn-2", 4,
                   turns[1].started_at + timedelta(minutes=4), AgentEventType.TEST_RESULT,
                   ExecutionStatus.FAILED, "synthetic:fixture",
                   {"failed": 1, "passed": 7}, PrivacyClassification.METADATA),
    )
    return AgentRunBundle(
        SourceSession(source_id, tenant, "synthetic-codex", hashlib.sha256(source_id.encode()).hexdigest(),
                      turns[0].started_at, now, turns[-1].ended_at),
        AgentRun(run_id, source_id, tenant, turns[0].started_at,
                 ExecutionStatus.UNKNOWN, turns[-1].ended_at,
                 session_id="synthetic-session", agent_name="Synthetic coding agent"),
        turns, events,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    path = args.database.expanduser().resolve()
    if path.exists():
        parser.error("database already exists; choose a new disposable path")
    path.parent.mkdir(parents=True, exist_ok=True)
    storage = SQLiteStorage(str(path))
    try:
        storage.replace_agent_run_bundle(_synthetic_bundle(datetime.now(timezone.utc)))
        now = datetime.now(timezone.utc)
        followup_source = "synthetic-codex-followup-source"
        followup_run = "synthetic-followup-run"
        followup_turn = AgentTurn(
            "synthetic-followup-turn", followup_run, 0, now - timedelta(minutes=30),
            ExecutionStatus.COMPLETED, now - timedelta(minutes=25),
            "Review the parser failure and propose a focused next check",
            "Check the parser fixture and rerun the failing test after the change.",
            EvidenceState.PRESENT, EvidenceState.PRESENT,
            input_tokens=400, output_tokens=100, total_tokens=500,
            token_usage_basis="codex_turn_delta",
        )
        storage.replace_agent_run_bundle(AgentRunBundle(
            SourceSession(followup_source, "__verdict_local__", "synthetic-codex",
                          hashlib.sha256(followup_source.encode()).hexdigest(),
                          followup_turn.started_at, now, followup_turn.ended_at),
            AgentRun(followup_run, followup_source, "__verdict_local__",
                     followup_turn.started_at, ExecutionStatus.COMPLETED,
                     followup_turn.ended_at, session_id="synthetic-session",
                     agent_name="Synthetic coding agent"),
            (followup_turn,),
            (AgentEvent("synthetic-followup-model", followup_turn.turn_id, 0,
                        followup_turn.started_at, AgentEventType.CONTEXT,
                        ExecutionStatus.COMPLETED, "synthetic:fixture",
                        {"name": "model_configuration", "source": "gpt-4o-mini"},
                        PrivacyClassification.METADATA),),
        ))
        verdict.init(storage=storage, tenant_id="__verdict_local__",
                     service_name="synthetic-application", environment="demo",
                     instrumentors=[])
        with verdict.agent_run(name="Synthetic application agent", version="sample-v1",
                               session_id="synthetic-app-session") as run:
            with run.turn(user_input="Check whether the synthetic parser task is complete") as turn:
                with turn.tool("lookup_task", arguments={"task": "parser"}) as tool:
                    tool.set_output({"state": "needs_test"})
                turn.record_test(command="synthetic-check", failed=0, passed=2, exit_code=0)
                turn.set_output("Two checks passed; final acceptance is still pending.")
            run.record_business_outcome("review_required", True)
    finally:
        verdict.shutdown()
        storage.close()
    ControlStore(f"sqlite:///{path}").append(
        "__verdict_local__", kind="case_set", document_id="synthetic_routing",
        state="active", expected_revision=None, payload={
            "title": "Synthetic incident-routing regression",
            "reviewed": True,
            "cases": [
                {"id": "critical_outage",
                 "input": "Synthetic incident: checkout unavailable for all users; severity P1",
                 "expectedBehavior": "Page the on-call engineer",
                 "checks": [{"kind": "exact", "value": "page_on_call"}]},
                {"id": "routine_edit",
                 "input": "Synthetic request: correct a dashboard label; severity P3",
                 "expectedBehavior": "Create a backlog ticket without paging",
                 "checks": [{"kind": "exact", "value": "create_backlog_ticket"}]},
            ],
        },
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
