from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from verdict.dashboard import create_app
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.local_cli import main as capture_main
from verdict_eval.cli.local import main as local_main


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(row) + "\n" for row in rows).encode()
    path.write_bytes(content)
    return content


def _codex_session(root: Path, index: int, at: datetime, response: str) -> tuple[Path, bytes]:
    turn = f"turn-{index}"
    stamp = at.isoformat().replace("+00:00", "Z")
    path = root / f"session-{index}.jsonl"
    return path, _write_jsonl(
        path,
        [
            {
                "timestamp": stamp,
                "type": "session_meta",
                "payload": {
                    "originator": "Codex Desktop",
                    "source": "vscode",
                    "id": f"session-{index}",
                    "cli_version": "0.148.0",
                },
            },
            {
                "timestamp": stamp,
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn, "started_at": stamp},
            },
            {
                "timestamp": stamp,
                "type": "turn_context",
                "payload": {"turn_id": turn, "model": "codex-test"},
            },
            {
                "timestamp": stamp,
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Repair the parser"},
            },
            {
                "timestamp": (at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": turn,
                    "completed_at": (at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                    "last_agent_message": response,
                },
            },
        ],
    )


def _claude_session(root: Path, index: int, at: datetime, response: str) -> tuple[Path, bytes]:
    stamp = at.isoformat().replace("+00:00", "Z")
    path = root / f"session-{index}.jsonl"
    common = {
        "cwd": "/synthetic/project",
        "gitBranch": "main",
        "isSidechain": False,
        "sessionId": f"session-{index}",
        "version": "2.1.143",
    }
    return path, _write_jsonl(
        path,
        [
            {
                **common,
                "type": "user",
                "uuid": f"prompt-{index}",
                "timestamp": stamp,
                "message": {"role": "user", "content": "Repair the parser"},
            },
            {
                **common,
                "type": "assistant",
                "uuid": f"answer-{index}",
                "timestamp": (at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "message": {
                    "id": f"message-{index}",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": response}],
                    "usage": {"input_tokens": 10, "output_tokens": len(response.split())},
                    "stop_reason": "end_turn",
                },
            },
        ],
    )


def _histories(tmp_path: Path) -> tuple[Path, Path, dict[Path, bytes]]:
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    originals: dict[Path, bytes] = {}
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for index in range(16):
        writer = _codex_session if index % 2 == 0 else _claude_session
        root = codex_root if index % 2 == 0 else claude_root
        response = "short useful answer" if index < 8 else "I cannot comply. " + "verbose " * 120
        path, content = writer(root, index, start + timedelta(days=index * 4), response)
        originals[path] = content
    return codex_root, claude_root, originals


def test_standalone_capture_then_verdict_server_reads_canonical_traces(
    tmp_path: Path, capsys
) -> None:
    codex_root, claude_root, originals = _histories(tmp_path)
    database = tmp_path / "verdict.db"
    storage_url = f"sqlite:///{database}"

    assert (
        capture_main(
            [
                "--storage",
                storage_url,
                "--codex-root",
                str(codex_root),
                "--claude-root",
                str(claude_root),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["stored"] == 16
    assert payload["sources"]["codex"]["stored"] == 8
    assert payload["sources"]["claude"]["stored"] == 8
    assert database.stat().st_mode & 0o777 == 0o600
    assert all(path.read_bytes() == content for path, content in originals.items())

    storage = SQLiteStorage(str(database))
    assert len(storage.list_traces(limit=100)) == 16
    storage.close()
    client = TestClient(create_app(storage=storage_url))
    assert client.get("/api/health").json()["configured"] is True
    dashboard = client.get("/api/data").json()
    assert dashboard["meta"]["totalTraces"] == 16


def test_one_local_command_imports_detects_drift_persists_and_retries_idempotently(
    tmp_path: Path, capsys
) -> None:
    codex_root, claude_root, _ = _histories(tmp_path)
    database = tmp_path / "verdict.db"
    arguments = [
        "--storage",
        f"sqlite:///{database}",
        "--codex-root",
        str(codex_root),
        "--claude-root",
        str(claude_root),
        "--no-serve",
        "--json",
    ]

    assert local_main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["capture"]["stored"] == 16
    assert first["analysis"]["scopes"][0]["status"] == "drift_detected"
    [series_id] = first["analysis"]["active_series_ids"]
    assert first["analysis"]["scheduled"]["status"] == "no_op"

    assert local_main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["analysis"]["active_series_ids"] == [series_id]
    assert second["analysis"]["scheduled"]["status"] == "no_op"

    dashboard = TestClient(create_app(storage=f"sqlite:///{database}")).get("/api/data").json()
    assert dashboard["meta"]["totalTraces"] == 16
    assert dashboard["driftRun"]["signalCount"] > 0
