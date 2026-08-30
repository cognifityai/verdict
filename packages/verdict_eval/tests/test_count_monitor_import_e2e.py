from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from verdict.storage import SQLiteStorage
from verdict.telemetry.cli import main as import_main
from verdict_eval.cli.monitor import main as monitor_main


def _langfuse_row(index: int, when: datetime, response: str) -> dict[str, object]:
    identifier = str(uuid5(NAMESPACE_URL, f"count-monitor-import-{index}"))
    return {
        "id": identifier,
        "traceId": str(uuid5(NAMESPACE_URL, f"count-monitor-trace-{index}")),
        "type": "GENERATION",
        "startTime": when.isoformat(),
        "endTime": (when + timedelta(seconds=1)).isoformat(),
        "providedModelName": "future-provider-model",
        "input": [{"role": "user", "content": "summarize the incident report"}],
        "output": {"role": "assistant", "content": response},
        "usageDetails": {"input": 20, "output": 10},
        "sessionId": f"session-{index}",
    }


def test_imported_history_bootstraps_immediately_and_reimport_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "langfuse.jsonl"
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    rows = [
        _langfuse_row(index, start + timedelta(days=index), "short useful answer")
        for index in range(8)
    ] + [
        _langfuse_row(
            index,
            start + timedelta(days=40 + index),
            "I cannot comply. " + "verbose " * 120,
        )
        for index in range(8, 16)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    db = tmp_path / "verdict.db"
    command = [
        "file",
        str(path),
        "--format",
        "langfuse",
        "--storage",
        f"sqlite:///{db}",
        "--tenant-id",
        "tenant-poc",
    ]

    assert import_main(command) == 0
    first_import = json.loads(capsys.readouterr().out)
    assert import_main(command) == 0
    second_import = json.loads(capsys.readouterr().out)
    assert first_import["seen"] == second_import["seen"] == 16

    storage = SQLiteStorage(str(db))
    try:
        assert len(storage.list_traces(tenant_id="tenant-poc", limit=100)) == 16
    finally:
        storage.close()

    assert monitor_main(["--storage", f"sqlite:///{db}", "bootstrap", "--json"]) == 0
    analysis = json.loads(capsys.readouterr().out)
    report = analysis["scopes"][0]
    assert report["scope"]["tenant_id"] == "tenant-poc"
    assert report["baseline_units"] == report["current_units"] == 8
    assert report["status"] == "drift_detected"
