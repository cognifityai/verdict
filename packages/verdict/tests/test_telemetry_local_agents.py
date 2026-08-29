from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry import local_agents
from verdict.telemetry.local_agents import capture_local_history, capture_local_history_url
from verdict.telemetry.local_cli import main as capture_main
from verdict.telemetry.model import ImportContext
from verdict.telemetry.sources import agent_common
from verdict.telemetry.sources.claude_code import iter_claude_history
from verdict.telemetry.sources.codex import iter_codex_history


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(row) + "\n" for row in rows).encode()
    path.write_bytes(content)
    return content


def _codex_row(outer: str, inner: str, **payload: object) -> dict[str, object]:
    return {
        "timestamp": "2026-08-20T10:00:00Z",
        "type": outer,
        "payload": {"type": inner, **payload},
    }


def test_codex_interprets_events_but_shared_importer_owns_trace_and_storage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    source = root / "session.jsonl"
    original = _write_jsonl(
        source,
        [
            _codex_row(
                "session_meta",
                "ignored",
                originator="Codex Desktop",
                source="vscode",
                id="session-1",
                cli_version="0.148.0",
                cwd="/Users/person/private/repo",
            ),
            _codex_row(
                "event_msg",
                "task_started",
                turn_id="turn-1",
                started_at=177777.25,
            ),
            _codex_row("turn_context", "ignored", turn_id="turn-1", model="future-codex"),
            _codex_row(
                "event_msg",
                "user_message",
                message=(
                    "<environment_context>private ambient data</environment_context>\n"
                    "Please repair the parser for owner@example.com."
                ),
            ),
            _codex_row(
                "response_item",
                "function_call",
                name="shell",
                arguments="TOOL-SECRET",
            ),
            _codex_row(
                "event_msg",
                "token_count",
                info={
                    "total_token_usage": {
                        "input_tokens": 120,
                        "output_tokens": 30,
                        "cached_input_tokens": 80,
                    }
                },
            ),
            _codex_row(
                "event_msg",
                "task_complete",
                turn_id="turn-1",
                completed_at="2026-08-20T10:00:02Z",
                last_agent_message="The parser is repaired for 415-555-1212.",
            ),
        ],
    )
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))

    first = capture_local_history(
        storage,
        codex_root=root,
        claude_root=tmp_path / "missing-claude",
        source="codex",
        home=Path("/Users/person"),
    )
    second = capture_local_history(
        storage,
        codex_root=root,
        claude_root=tmp_path / "missing-claude",
        source="codex",
        home=Path("/Users/person"),
    )
    rows = storage.list_traces(limit=10)
    storage.close()

    assert first["codex"].stored == second["codex"].stored == 1
    assert len(rows) == 1
    trace = rows[0]
    assert trace.provider == "openai"
    assert trace.request_model == "future-codex"
    assert trace.tags["verdict.source"] == "codex"
    assert trace.tags["verdict.workload"] == "agent"
    assert trace.tags["capture.granularity"] == "agent-turn"
    assert trace.tags["capture.tool_calls"] == "1"
    assert trace.input_tokens == 120
    assert trace.output_tokens == 30
    assert "private ambient data" not in repr(trace)
    assert "TOOL-SECRET" not in repr(trace)
    assert "owner@example.com" not in repr(trace)
    assert "415-555-1212" not in repr(trace)
    assert source.read_bytes() == original


def test_claude_uses_only_root_prompt_and_visible_final_response(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    common = {
        "cwd": "/Users/person/private/repo",
        "gitBranch": "secret-branch",
        "isSidechain": False,
        "sessionId": "claude-session",
        "version": "2.1.143",
    }
    _write_jsonl(
        root / "claude.jsonl",
        [
            {
                **common,
                "type": "user",
                "uuid": "prompt-1",
                "timestamp": "2026-08-20T10:00:00Z",
                "message": {"role": "user", "content": "Fix the parser."},
            },
            {
                **common,
                "type": "assistant",
                "uuid": "assistant-1",
                "timestamp": "2026-08-20T10:00:01Z",
                "message": {
                    "id": "message-1",
                    "model": "future-claude",
                    "content": [
                        {"type": "thinking", "thinking": "THINKING-SECRET"},
                        {"type": "tool_use", "id": "tool-1", "input": "TOOL-SECRET"},
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 50,
                    },
                    "stop_reason": "tool_use",
                },
            },
            {
                **common,
                "type": "user",
                "uuid": "tool-result",
                "sourceToolAssistantUUID": "assistant-1",
                "toolUseResult": {"content": "RESULT-SECRET"},
                "timestamp": "2026-08-20T10:00:02Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "RESULT-SECRET"}],
                },
            },
            {
                **common,
                "type": "assistant",
                "uuid": "assistant-2",
                "timestamp": "2026-08-20T10:00:03Z",
                "message": {
                    "id": "message-2",
                    "model": "future-claude",
                    "content": [{"type": "text", "text": "The parser is repaired."}],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                    "stop_reason": "end_turn",
                },
            },
        ],
    )
    context = ImportContext(adapter="claude", source_scope=str(root))

    [result] = list(iter_claude_history(root, context=context, home=Path("/Users/person")))

    assert result.trace is not None
    trace = result.trace
    assert trace.provider == "anthropic"
    assert trace.input_tokens == 15
    assert trace.output_tokens == 7
    assert trace.tags["capture.cached_input_tokens"] == "50"
    assert trace.tags["capture.tool_calls"] == "1"
    assert trace.tags["capture.assistant_calls"] == "2"
    assert all(
        marker not in repr(trace) for marker in ("THINKING-SECRET", "TOOL-SECRET", "RESULT-SECRET")
    )


def test_child_sidechain_incomplete_and_unknown_histories_have_terminal_outcomes(
    tmp_path: Path,
) -> None:
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    _write_jsonl(
        codex_root / "child.jsonl",
        [
            _codex_row(
                "session_meta",
                "ignored",
                originator="Codex Desktop",
                source={"subagent": "reviewer"},
                id="child",
            )
        ],
    )
    _write_jsonl(
        codex_root / "incomplete.jsonl",
        [
            _codex_row(
                "session_meta",
                "ignored",
                originator="Codex Desktop",
                source="vscode",
                id="root",
            ),
            _codex_row("event_msg", "task_started", turn_id="turn"),
        ],
    )
    _write_jsonl(
        claude_root / "sidechain.jsonl",
        [
            {
                "type": "user",
                "isSidechain": True,
                "uuid": "prompt",
                "sessionId": "session",
                "timestamp": "2026-08-20T10:00:00Z",
                "message": {"role": "user", "content": "Private sidechain"},
            }
        ],
    )

    codex = list(
        iter_codex_history(
            codex_root,
            context=ImportContext(adapter="codex", source_scope=str(codex_root)),
        )
    )
    claude = list(
        iter_claude_history(
            claude_root,
            context=ImportContext(adapter="claude", source_scope=str(claude_root)),
        )
    )

    assert sorted(result.skip_reason for result in codex) == [
        "child_session",
        "incomplete_turn",
    ]
    assert [result.skip_reason for result in claude] == ["no_root_turns"]


def test_malformed_jsonl_uses_shared_reader_error_contract(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    path = root / "broken.jsonl"
    _write_jsonl(
        path,
        [
            _codex_row(
                "session_meta",
                "ignored",
                originator="Codex Desktop",
                source="vscode",
                id="root",
            )
        ],
    )
    with path.open("a") as handle:
        handle.write('{"broken":')

    with pytest.raises(ValueError, match="invalid NDJSON"):
        list(
            iter_codex_history(
                root,
                context=ImportContext(adapter="codex", source_scope=str(root)),
            )
        )


def test_local_sqlite_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("platform does not expose no-follow file creation")
    target = tmp_path / "must-not-change"
    target.write_text("sentinel")
    database = tmp_path / "verdict.db"
    database.symlink_to(target)

    with pytest.raises(OSError):
        capture_local_history_url(
            f"sqlite:///{database}",
            codex_root=tmp_path / "missing-codex",
            claude_root=tmp_path / "missing-claude",
        )

    assert target.read_text() == "sentinel"


def test_capture_cli_never_echoes_credential_bearing_storage_url(capsys) -> None:
    result = capture_main(
        [
            "--storage",
            "unsupported://user:canary-password@example.invalid/verdict",
            "--json",
        ]
    )

    output = capsys.readouterr().out
    assert result == 2
    assert "canary-password" not in output
    assert "example.invalid" not in output


def test_capture_cli_bounds_unexpected_backend_error(
    capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_safely(_storage_url: str):
        raise RuntimeError("postgresql://user:canary-password@example.invalid/verdict")

    monkeypatch.setattr(local_agents, "_resolve_storage", fail_safely)
    result = capture_main(
        ["--storage", "postgresql://user:canary-password@example.invalid/verdict", "--json"]
    )

    output = capsys.readouterr().out
    assert result == 2
    assert output == "ERROR: local capture failed (RuntimeError)\n"


@pytest.mark.parametrize("storage_url", ["memory://", "memory://named", "sqlite:///:memory:"])
def test_standalone_capture_rejects_non_durable_storage(tmp_path: Path, storage_url: str) -> None:
    with pytest.raises(ValueError, match="requires durable"):
        capture_local_history_url(
            storage_url,
            codex_root=tmp_path / "codex",
            claude_root=tmp_path / "claude",
        )


def test_claude_bounds_assistant_content_across_the_whole_turn(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    common = {
        "cwd": "/synthetic/project",
        "gitBranch": "main",
        "isSidechain": False,
        "sessionId": "bounded-session",
        "version": "2.1.143",
    }
    _write_jsonl(
        root / "bounded.jsonl",
        [
            {
                **common,
                "type": "user",
                "uuid": "prompt",
                "timestamp": "2026-08-20T10:00:00Z",
                "message": {"role": "user", "content": "Repair the parser"},
            },
            {
                **common,
                "type": "assistant",
                "uuid": "tool-phase",
                "timestamp": "2026-08-20T10:00:01Z",
                "message": {
                    "id": "message-1",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "a" * 60_000}],
                },
            },
            {
                **common,
                "type": "assistant",
                "uuid": "final",
                "timestamp": "2026-08-20T10:00:02Z",
                "message": {
                    "id": "message-2",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "b" * 60_000}],
                    "stop_reason": "end_turn",
                },
            },
        ],
    )

    [result] = list(
        iter_claude_history(
            root,
            context=ImportContext(adapter="claude", source_scope=str(root)),
        )
    )

    assert result.trace is not None
    assert result.trace.response_redacted == "b" * 40_000


def test_local_history_file_count_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    (root / "one.jsonl").write_text("{}\n")
    (root / "two.jsonl").write_text("{}\n")
    monkeypatch.setattr(agent_common, "MAX_HISTORY_FILES", 1)

    with pytest.raises(ValueError, match="history exceeds 1 files"):
        list(
            iter_codex_history(
                root,
                context=ImportContext(adapter="codex", source_scope=str(root)),
            )
        )
