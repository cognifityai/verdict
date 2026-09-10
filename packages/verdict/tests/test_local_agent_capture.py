from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import verdict.evidence as evidence_contract
from verdict import AgentEventType, EvidenceState, ExecutionStatus, PrivacyClassification
from verdict.storage import SQLiteStorage
from verdict.telemetry.cli import main
from verdict.telemetry.local_agents import capture_local_agents


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _codex_records(secret: str = "private payload") -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-08-30T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "codex-session-1",
                "session_id": "codex-session-1",
                "originator": "Codex Desktop",
                "cli_version": "1.2.3",
                "cwd": "/Users/example/project",
            },
        },
        {
            "timestamp": "2026-08-30T10:01:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-a"},
        },
        {
            "timestamp": "2026-08-30T10:01:01Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-a", "model": "gpt-5"},
        },
        {
            "timestamp": "2026-08-30T10:01:02Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": f"<environment_context>ignore me</environment_context>\n"
                f"## My request:\nfix the test {secret}",
            },
        },
        {
            "timestamp": "2026-08-30T10:01:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-1",
                "arguments": json.dumps({"cmd": f"pytest {secret}"}),
            },
        },
        {
            "timestamp": "2026-08-30T10:01:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": json.dumps({"exit_code": 1, "output": f"failed {secret}"}),
            },
        },
        {
            "timestamp": "2026-08-30T10:01:05Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "phase": "final_answer", "message": "fixed"},
        },
        {
            "timestamp": "2026-08-30T10:01:06Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 20,
                        "cached_input_tokens": 8,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 4,
                        "reasoning_output_tokens": 2,
                        "total_tokens": 24,
                    },
                    "total_token_usage": {
                        "input_tokens": 120,
                        "cached_input_tokens": 48,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 24,
                        "reasoning_output_tokens": 12,
                        "total_tokens": 144,
                    },
                },
            },
        },
        {
            "timestamp": "2026-08-30T10:01:07Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-a",
                "last_agent_message": "fixed",
            },
        },
    ]


def _claude_records() -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-08-30T11:00:00Z",
            "type": "user",
            "uuid": "user-1",
            "sessionId": "claude-session-1",
            "message": {"content": "diagnose build"},
        },
        {
            "timestamp": "2026-08-30T11:00:01Z",
            "type": "assistant",
            "uuid": "assistant-1",
            "sessionId": "claude-session-1",
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-4-5",
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 12,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 4,
                    "output_tokens": 3,
                },
                "content": [
                    {"type": "thinking", "thinking": "never retain this reasoning"},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "pytest"},
                    },
                ],
            },
        },
        {
            "timestamp": "2026-08-30T11:00:01.100000Z",
            "type": "assistant",
            "uuid": "assistant-1-text",
            "sessionId": "claude-session-1",
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-4-5",
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 12,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 4,
                    "output_tokens": 3,
                },
                "content": [{"type": "text", "text": "I will run the tests."}],
            },
        },
        {
            "timestamp": "2026-08-30T11:00:01.200000Z",
            "type": "assistant",
            "uuid": "assistant-1-duplicate-tool",
            "sessionId": "claude-session-1",
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-4-5",
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 12,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 4,
                    "output_tokens": 3,
                },
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "do not replace the first call"},
                    },
                ],
            },
        },
        {
            "timestamp": "2026-08-30T11:00:02Z",
            "type": "user",
            "sessionId": "claude-session-1",
            "sourceToolAssistantUUID": "assistant-1",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "is_error": False,
                        "content": "2 passed",
                    }
                ]
            },
        },
        {
            "timestamp": "2026-08-30T11:00:03Z",
            "type": "assistant",
            "uuid": "assistant-2-empty",
            "sessionId": "claude-session-1",
            "message": {
                "id": "msg-2",
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 18,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 6,
                    "output_tokens": 5,
                },
                "content": [],
            },
        },
        {
            "timestamp": "2026-08-30T11:00:03.100000Z",
            "type": "assistant",
            "uuid": "assistant-2-text",
            "sessionId": "claude-session-1",
            "message": {
                "id": "msg-2",
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 18,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 6,
                    "output_tokens": 5,
                },
                "content": [{"type": "text", "text": "The build passes."}],
            },
        },
        {
            "timestamp": "2026-08-30T11:00:03.200000Z",
            "type": "assistant",
            "uuid": "assistant-2-trailing-empty",
            "sessionId": "claude-session-1",
            "message": {
                "id": "msg-2",
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 18,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 6,
                    "output_tokens": 5,
                },
                "content": [],
            },
        },
    ]


def test_codex_capture_is_idempotent_and_content_on_by_default(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    _write_jsonl(root / "session.jsonl", _codex_records())
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    first = capture_local_agents(storage, tenant_id="local", codex_root=root)
    second = capture_local_agents(storage, tenant_id="local", codex_root=root)

    assert first.as_dict() == {"files": 1, "stored": 1, "skipped": 0, "skip_reasons": {}}
    assert second.as_dict() == first.as_dict()
    bundles = storage.list_agent_run_bundles("local")
    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.run.status is ExecutionStatus.UNKNOWN
    assert bundle.turns[0].status is ExecutionStatus.COMPLETED
    assert bundle.turns[0].request_state is EvidenceState.PRESENT
    assert bundle.turns[0].response_state is EvidenceState.PRESENT
    assert bundle.turns[0].user_request_redacted == "fix the test private payload"
    assert bundle.turns[0].final_response_redacted == "fixed"
    assert bundle.turns[0].input_tokens == 20
    assert bundle.turns[0].cached_input_tokens == 8
    assert bundle.turns[0].output_tokens == 4
    assert bundle.turns[0].reasoning_output_tokens == 2
    assert bundle.turns[0].total_tokens == 24
    assert bundle.turns[0].token_usage_basis == "codex_turn_delta"
    assert {event.event_type for event in bundle.events} >= {
        AgentEventType.CONTEXT,
        AgentEventType.TOOL_CALL,
        AgentEventType.TOOL_RESULT,
        AgentEventType.COMMAND,
    }
    assert all(event.trace_id is None for event in bundle.events)
    assert "<environment_context>" not in repr(bundle)
    assert any(event.privacy_classification is PrivacyClassification.REDACTED for event in bundle.events)


def test_codex_capture_can_explicitly_disable_content(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    _write_jsonl(root / "session.jsonl", _codex_records())
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root, capture_content=False)

    [bundle] = storage.list_agent_run_bundles("local")
    assert bundle.turns[0].request_state is EvidenceState.NOT_CAPTURED
    assert bundle.turns[0].response_state is EvidenceState.NOT_CAPTURED
    assert bundle.turns[0].user_request_redacted is None
    assert bundle.turns[0].final_response_redacted is None
    assert "private payload" not in repr(bundle)
    assert any(
        event.privacy_classification is PrivacyClassification.OMITTED for event in bundle.events
    )


def test_claude_capture_preserves_typed_evidence_without_thinking(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    records = _claude_records()
    tool_result = next(
        row for row in records
        if row.get("type") == "user" and row.get("sourceToolAssistantUUID") is not None
    )
    tool_result["message"]["content"][0]["content"] = (
        f"failure at {tmp_path}/private/project.py"
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(
        storage,
        tenant_id="local",
        claude_root=root,
        capture_content=True,
        home=tmp_path,
    )

    assert summary.stored == 1
    bundle = storage.list_agent_run_bundles("local")[0]
    assert bundle.turns[0].user_request_redacted == "diagnose build"
    assert bundle.turns[0].final_response_redacted == "The build passes."
    assert bundle.turns[0].input_tokens == 30
    assert bundle.turns[0].cached_input_tokens == 50
    assert bundle.turns[0].cache_write_input_tokens == 10
    assert bundle.turns[0].output_tokens == 8
    assert bundle.turns[0].total_tokens == 98
    assert bundle.turns[0].token_usage_basis == "claude_provider_response_sum"
    assert [event.sequence for event in bundle.events] == list(range(len(bundle.events)))
    assert [event.event_type for event in bundle.events].count(AgentEventType.MODEL_CALL) == 2
    assert [event.event_type for event in bundle.events].count(AgentEventType.TOOL_CALL) == 1
    assert AgentEventType.TOOL_CALL in {event.event_type for event in bundle.events}
    assert AgentEventType.TOOL_RESULT in {event.event_type for event in bundle.events}
    [tool_call] = [
        event for event in bundle.events if event.event_type is AgentEventType.TOOL_CALL
    ]
    assert tool_call.attributes["arguments"] == {"command": "pytest"}
    assert "never retain this reasoning" not in repr(bundle)
    assert str(tmp_path) not in repr(bundle)
    assert "~/private/project.py" in repr(bundle)
    traces = storage.list_traces(tenant_id="local", limit=10)
    assert len(traces) == 2
    model_events = [
        event for event in bundle.events if event.event_type is AgentEventType.MODEL_CALL
    ]
    assert {event.trace_id for event in model_events} == {trace.trace_id for trace in traces}
    assert all(trace.provider == "anthropic" for trace in traces)
    assert all(trace.tags["verdict.source"] == "claude-code" for trace in traces)
    assert all(trace.tags["verdict.workload"] == "agent" for trace in traces)
    assert all(trace.tags["verdict.agent_run_id"] == bundle.run.run_id for trace in traces)
    assert all(trace.cost_usd is None for trace in traces)
    assert all(trace.prompt_redacted == "diagnose build" for trace in traces)
    assert {trace.response_redacted for trace in traces} == {
        "I will run the tests.", "The build passes.",
    }
    assert all(trace.raw_messages[0] == {"role": "user", "content": "diagnose build"} for trace in traces)
    assert any(
        trace.raw_messages[-1] == {"role": "assistant", "content": "The build passes."}
        for trace in traces
    )
    assert all(trace.tags["verdict.input_evidence"] == "turn_request_only" for trace in traces)


def test_claude_rescan_fills_split_responses_without_changing_evidence_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude"
    path = root / "session.jsonl"
    complete = _claude_records()
    initial = [
        row for row in complete
        if row.get("uuid") not in {
            "assistant-1-text", "assistant-2-text", "assistant-2-trailing-empty",
        }
    ]
    _write_jsonl(path, initial)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", claude_root=root)
    [before_bundle] = storage.list_agent_run_bundles("local")
    before_traces = storage.list_traces(tenant_id="local", limit=10)
    before_trace_ids = {
        trace.finish_reason: trace.trace_id for trace in before_traces
    }
    before_event_ids = [event.event_id for event in before_bundle.events]
    assert all(trace.response_redacted is None for trace in before_traces)

    _write_jsonl(path, complete)
    revised_mtime = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(revised_mtime, revised_mtime))
    capture_local_agents(storage, tenant_id="local", claude_root=root)

    [after_bundle] = storage.list_agent_run_bundles("local")
    after_traces = storage.list_traces(tenant_id="local", limit=10)
    assert {trace.finish_reason: trace.trace_id for trace in after_traces} == before_trace_ids
    assert [event.event_id for event in after_bundle.events] == before_event_ids
    assert after_bundle.turns[0].final_response_redacted == "The build passes."
    assert {trace.response_redacted for trace in after_traces} == {
        "I will run the tests.", "The build passes.",
    }


def test_claude_metadata_only_capture_links_genuine_traces_without_content(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    _write_jsonl(root / "session.jsonl", _claude_records())
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(
        storage, tenant_id="local", claude_root=root, capture_content=False, home=tmp_path
    )

    [bundle] = storage.list_agent_run_bundles("local")
    traces = storage.list_traces(tenant_id="local", limit=10)
    assert len(traces) == 2
    assert all(trace.prompt_redacted is None for trace in traces)
    assert all(trace.response_redacted is None for trace in traces)
    assert all(trace.raw_messages is None for trace in traces)
    assert all(
        event.trace_id is not None
        for event in bundle.events
        if event.event_type is AgentEventType.MODEL_CALL
    )


def test_child_codex_and_malformed_histories_are_accounted_for(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    _write_jsonl(
        codex / "child.jsonl",
        [
            {
                "timestamp": "2026-08-30T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "child",
                    "originator": "Codex Desktop",
                    "parent_thread_id": "parent",
                },
            },
            {
                "timestamp": "2026-08-30T10:01:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "child-turn"},
            },
            {
                "timestamp": "2026-08-30T10:01:01Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "child-turn"},
            },
        ],
    )
    (codex / "broken.jsonl").write_text("{broken\n")
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", codex_root=codex)

    assert summary.files == 2
    assert summary.stored == 1
    assert summary.skipped == 1
    assert summary.skip_reasons == {"malformed_jsonl": 1}
    [bundle] = storage.list_agent_run_bundles("local")
    assert bundle.run.parent_run_id is not None
    assert bundle.run.parent_run_id != bundle.run.run_id


def test_codex_uses_unique_run_id_and_shared_logical_session_for_children(
    tmp_path: Path,
) -> None:
    codex = tmp_path / "codex"
    root_records = _codex_records()
    root_records[0]["payload"].update({
        "id": "root-run",
        "session_id": "root-run",
    })
    child_records = [
        {
            "timestamp": "2026-08-30T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "child-run",
                "session_id": "root-run",
                "parent_thread_id": "root-run",
                "originator": "Codex Desktop",
            },
        },
        {
            "timestamp": "2026-08-30T10:01:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-a"},
        },
        {
            "timestamp": "2026-08-30T10:01:01Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-a",
                "last_agent_message": "child done",
            },
        },
    ]
    _write_jsonl(codex / "root.jsonl", root_records)
    _write_jsonl(codex / "child.jsonl", child_records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", codex_root=codex)

    assert summary.as_dict() == {
        "files": 2, "stored": 2, "skipped": 0, "skip_reasons": {},
    }
    bundles = storage.list_agent_run_bundles("local")
    root = next(bundle for bundle in bundles if bundle.run.parent_run_id is None)
    child = next(bundle for bundle in bundles if bundle.run.parent_run_id is not None)
    assert child.run.run_id != root.run.run_id
    assert child.run.parent_run_id == root.run.run_id
    assert child.run.session_id == root.run.session_id
    assert child.session.source_session_id != root.session.source_session_id
    assert child.turns[0].turn_id != root.turns[0].turn_id


def test_codex_turn_usage_uses_within_turn_delta_without_double_counting(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    first_snapshot = records[-2]
    repeated = json.loads(json.dumps(first_snapshot))
    repeated["timestamp"] = "2026-08-30T10:01:06.100000Z"
    final = json.loads(json.dumps(first_snapshot))
    final["timestamp"] = "2026-08-30T10:01:06.200000Z"
    final_usage = final["payload"]["info"]["total_token_usage"]
    final_usage.update({
        "input_tokens": 130,
        "cached_input_tokens": 54,
        "output_tokens": 29,
        "reasoning_output_tokens": 15,
        "total_tokens": 159,
    })
    records[-1:-1] = [repeated, final]
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    [turn] = storage.list_agent_run_bundles("local")[0].turns
    assert turn.input_tokens == 30
    assert turn.cached_input_tokens == 14
    assert turn.output_tokens == 9
    assert turn.reasoning_output_tokens == 5
    assert turn.total_tokens == 39


def test_invalid_codex_usage_is_unavailable_without_discarding_the_turn(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[-2]["payload"]["info"]["last_token_usage"]["input_tokens"] = -1
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)

    assert summary.stored == 1
    [turn] = storage.list_agent_run_bundles("local")[0].turns
    assert turn.token_usage_basis is None
    assert turn.total_tokens is None


def test_irreconcilable_codex_cumulative_usage_is_unavailable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[-2]["payload"]["info"]["total_token_usage"]["input_tokens"] = 10
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    [turn] = storage.list_agent_run_bundles("local")[0].turns
    assert turn.input_tokens is None
    assert turn.total_tokens is None
    assert turn.token_usage_basis is None


def test_claude_sidechain_is_a_distinct_child_run(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    root_records = _claude_records()
    child_records = json.loads(json.dumps(root_records))
    for row in child_records:
        row["isSidechain"] = True
        row["agentId"] = "child-agent-1"
    _write_jsonl(root / "root.jsonl", root_records)
    _write_jsonl(root / "child.jsonl", child_records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", claude_root=root)

    assert summary.stored == 2
    bundles = storage.list_agent_run_bundles("local")
    root_bundle = next(bundle for bundle in bundles if bundle.run.parent_run_id is None)
    child_bundle = next(bundle for bundle in bundles if bundle.run.parent_run_id is not None)
    assert child_bundle.run.run_id != root_bundle.run.run_id
    assert child_bundle.run.parent_run_id == root_bundle.run.run_id
    assert child_bundle.run.session_id == root_bundle.run.session_id


def test_turn_content_is_bounded_and_marks_truncation(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[3]["payload"]["message"] = "x" * 80_000
    records[-1]["payload"]["last_agent_message"] = "y" * 80_000
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    [turn] = storage.list_agent_run_bundles("local")[0].turns
    assert turn.request_truncated is True
    assert turn.response_truncated is True
    assert 1_000 < len(turn.user_request_redacted.encode("utf-8")) <= 65_536
    assert 1_000 < len(turn.final_response_redacted.encode("utf-8")) <= 65_536


def test_rescan_completes_an_older_bounded_response_prefix(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    path = root / "session.jsonl"
    records = _codex_records()
    records[-1]["payload"]["last_agent_message"] = "y" * 1_000
    _write_jsonl(path, records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    capture_local_agents(storage, tenant_id="local", codex_root=root)

    records[-1]["payload"]["last_agent_message"] = "y" * 2_000
    _write_jsonl(path, records)
    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)

    assert summary.stored == 1
    [turn] = storage.list_agent_run_bundles("local")[0].turns
    assert turn.final_response_redacted == "y" * 2_000
    assert turn.response_truncated is False


def test_symlinked_history_file_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    _write_jsonl(outside, _codex_records("SYMLINK_CANARY"))
    root = tmp_path / "codex"
    root.mkdir()
    (root / "linked.jsonl").symlink_to(outside)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)

    assert summary.stored == 0
    assert storage.list_agent_run_bundles("local") == []


def test_local_cli_captures_both_sources_without_echoing_content(tmp_path: Path, capsys) -> None:
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    _write_jsonl(codex / "session.jsonl", _codex_records("DO_NOT_ECHO"))
    _write_jsonl(claude / "session.jsonl", _claude_records())

    status = main(
        [
            "local",
            "--storage",
            f"sqlite:///{tmp_path / 'verdict.db'}",
            "--codex-root",
            str(codex),
            "--claude-root",
            str(claude),
        ]
    )

    output = capsys.readouterr()
    assert status == 0
    assert json.loads(output.out) == {
        "files": 2,
        "stored": 2,
        "skipped": 0,
        "skip_reasons": {},
    }
    assert "DO_NOT_ECHO" not in output.out + output.err


def test_large_session_is_bounded_and_marks_omitted_events(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 1_600
    _write_jsonl(root / "large.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)

    assert summary.stored == 1
    bundle = storage.list_agent_run_bundles("local")[0]
    assert len(bundle.events) == 1_500
    marker = bundle.events[-1]
    assert marker.provenance == "verdict:capture_limit"
    assert marker.attributes["name"] == "source_events_omitted"
    assert int(marker.attributes["source"]) > 0


def test_oversized_tool_content_omits_only_content_not_the_session(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[4]["payload"]["arguments"] = json.dumps({"cmd": "x" * 20_000})
    _write_jsonl(root / "oversized.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(
        storage, tenant_id="local", codex_root=root, capture_content=True,
    )

    assert summary.stored == 1
    bundle = storage.list_agent_run_bundles("local")[0]
    [tool_call] = [
        event for event in bundle.events if event.event_type is AgentEventType.TOOL_CALL
    ]
    assert tool_call.privacy_classification is PrivacyClassification.OMITTED
    assert tool_call.omission_reason == "content_exceeded_evidence_contract"
    assert "arguments" not in tool_call.attributes


def test_normalized_capture_is_not_limited_by_legacy_bundle_serialization(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records("x" * 3_000)
    _write_jsonl(root / "bounded.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    monkeypatch.setattr(evidence_contract, "_MAX_BUNDLE_JSON_BYTES", 5_000)

    summary = capture_local_agents(
        storage, tenant_id="local", codex_root=root, capture_content=True,
    )

    assert summary.stored == 1
    bundle = storage.list_agent_run_bundles("local")[0]
    assert bundle.turns[0].request_state is EvidenceState.PRESENT
    assert bundle.turns[0].response_state is EvidenceState.PRESENT
    assert bundle.turns[0].user_request_redacted is not None
    with pytest.raises(evidence_contract.EvidenceBundleTooLarge):
        evidence_contract.agent_run_bundle_to_json(bundle)


def test_live_partial_final_line_preserves_complete_records_and_marks_omission(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    path = root / "live.jsonl"
    _write_jsonl(path, _codex_records())
    with path.open("a") as handle:
        handle.write('{"timestamp":"2026-08-30T10:02:00Z","type":"event_msg"')
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)

    assert summary.stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    assert any(
        event.provenance == "verdict:partial_source_line"
        and event.attributes["name"] == "source_lines_omitted"
        for event in bundle.events
    )


def test_claude_meta_and_image_only_rows_do_not_fabricate_or_drop_turns(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude"
    records = _claude_records()
    records.insert(0, {
        "timestamp": "2026-08-30T10:59:00Z", "type": "user", "uuid": "meta",
        "sessionId": "claude-session-1", "isMeta": True,
        "message": {"content": "fabricated wrapper"},
    })
    records.extend([
        {
            "timestamp": "2026-08-30T12:00:00Z", "type": "user", "uuid": "image-user",
            "sessionId": "claude-session-1",
            "message": {"content": [{"type": "image", "source": {"type": "base64"}}]},
        },
        {
            "timestamp": "2026-08-30T12:00:01Z", "type": "assistant",
            "sessionId": "claude-session-1",
            "message": {
                "id": "msg-image", "model": "claude-sonnet-4", "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "text", "text": "I can inspect it."}],
            },
        },
    ])
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", claude_root=root)

    assert summary.stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    assert len(bundle.turns) == 2
    assert all(turn.user_request_redacted != "fabricated wrapper" for turn in bundle.turns)
    assert bundle.turns[1].request_state is EvidenceState.MISSING
    assert bundle.turns[1].final_response_redacted == "I can inspect it."


def test_unbounded_source_scalars_degrade_to_unknown_instead_of_dropping_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[5]["payload"]["output"] = json.dumps({
        "exit_code": 2**40, "output": "bounded result"
    })
    records[4]["payload"]["call_id"] = "🔥" * 300
    records[5]["payload"]["call_id"] = "🔥" * 300
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)

    assert summary.stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    results = [
        event for event in bundle.events
        if event.event_type is AgentEventType.TOOL_RESULT
    ]
    assert results and results[0].status is ExecutionStatus.UNKNOWN


def test_default_capture_and_findings_path_makes_no_external_network_call(
    tmp_path: Path, monkeypatch,
) -> None:
    import socket

    from verdict.dashboard.app import build_agent_insights_bundle

    root = tmp_path / "codex"
    _write_jsonl(root / "session.jsonl", _codex_records())
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))

    def reject_network(*_args, **_kwargs):
        raise AssertionError("judge-free capture attempted external network access")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
    storage.close()
    insights = build_agent_insights_bundle(
        f"sqlite:///{database}", tenant="local"
    )

    assert summary.stored == 1
    assert insights["scope"]["availableRuns"] == 1
