"""The disposable public demo follows the same chronological evidence path as the UI."""

import subprocess
import sys
from pathlib import Path

from verdict.dashboard.app import (
    build_agent_runs_bundle,
    build_agent_session_bundle,
    build_agent_usage_bundle,
)
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.scoped_evaluation import preview_scoped
from verdict.storage import SQLiteStorage


def test_synthetic_demo_has_valid_order_and_visible_profiler_workflow(tmp_path):
    path = tmp_path / "sample.db"
    script = Path(__file__).resolve().parents[2] / "examples" / "agent_profiler_demo.py"
    completed = subprocess.run([sys.executable, str(script), "--database", str(path)],
                               check=True, text=True, capture_output=True)
    assert completed.stdout.strip() == str(path)
    storage = SQLiteStorage(str(path))
    try:
        bundle = storage.get_agent_run_bundle("__verdict_local__", "synthetic-expensive-run")
        assert bundle is not None
        assert all(turn.started_at <= turn.ended_at for turn in bundle.turns)
        preview = preview_scoped(storage, tenant="__verdict_local__", config={
            "scope": "run", "runId": "synthetic-expensive-run", "provider": "anthropic",
            "model": "claude-haiku-4-5", "rubric": {"name": "task_review", "version": "1",
                "dimensions": [{"name": "completion", "description": "Did work meet the task?"}]},
        })
        timeline = preview["evidencePreview"]["response"]
        assert timeline.index("[event:synthetic-model-1]") < timeline.index("Initial inspection complete.")
        assert preview["plannedCalls"] == 1
        tool_preview = preview_scoped(storage, tenant="__verdict_local__", config={
            "scope": "tool_action", "runId": "synthetic-expensive-run",
            "eventId": "synthetic-tool-call", "provider": "anthropic",
            "model": "claude-haiku-4-5", "rubric": {"name": "tool_review", "version": "1",
                "dimensions": [{"name": "tool_fit", "description": "Did the action fit the task?"}]},
        })
        assert tool_preview["plannedCalls"] == 1
        assert "read_file(path)" in tool_preview["evidencePreview"]["query"]
        session_preview = preview_scoped(storage, tenant="__verdict_local__", storage_url=f"sqlite:///{path}", config={
            "scope": "session", "sourceKind": "synthetic-codex", "sessionId": "synthetic-session",
            "provider": "anthropic", "model": "claude-haiku-4-5",
            "rubric": {"name": "task_review", "version": "1",
                       "dimensions": [{"name": "completion", "description": "Did the session meet its task?"}]},
        })
        assert session_preview["plannedCalls"] == 1
        assert session_preview["evidencePreview"]["runIds"] == [
            "synthetic-expensive-run", "synthetic-followup-run",
        ]
    finally:
        storage.close()
    usage = build_agent_usage_bundle(path, tenant="__verdict_local__")
    runs = build_agent_runs_bundle(path, tenant="__verdict_local__")
    assert sum(day["totalTokens"] or 0 for day in usage["days"]) == 48_800
    assert sum(bool(day["totalTokens"]) for day in usage["days"]) >= 2
    session = build_agent_session_bundle(
        path, tenant="__verdict_local__", source_kind="synthetic-codex",
        session_id="synthetic-session",
    )
    assert session["usage"]["totalTokens"] == 48_800
    assert len(session["runIds"]) == 2
    assert any(run["runId"] == "synthetic-expensive-run" and run["recommendations"]
               for run in runs["runs"])
    assert any(run["sourceKind"] == "verdict_sdk" for run in runs["runs"])
    case_set = next(row for row in ControlStore(f"sqlite:///{path}").list_current("__verdict_local__")
                    if row["kind"] == "case_set")
    assert case_set["documentId"] == "synthetic_routing"
    assert {case["id"] for case in case_set["payload"]["cases"]} == {
        "critical_outage", "routine_edit",
    }
