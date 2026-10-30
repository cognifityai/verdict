"""Newest local-history inspection is bounded and safe before persistence."""

import json
import os
import sqlite3
from contextlib import ExitStack

import pytest
import verdict.telemetry.local_agents as local_agents
import zstandard
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from fastapi.testclient import TestClient
from verdict.dashboard.app import create_app
from verdict.storage import SQLiteStorage

KEY = "sk-" + "FAKECANARY" * 4
NEWEST = "NEWEST_COMPLETE_SYNTHETIC_MESSAGE"


def prefix():
    return [
        {
            "timestamp": "2026-08-30T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "synthetic-sample-session", "originator": "synthetic"},
        },
        {
            "timestamp": "2026-08-30T10:01:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "synthetic-turn"},
        },
        {
            "timestamp": "2026-08-30T10:01:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Inspect a synthetic source"},
        },
        {
            "timestamp": "2026-08-30T10:01:02Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "synthetic-turn",
                "last_agent_message": "Earlier synthetic work complete",
            },
        },
    ]


def prepare(tmp_path, monkeypatch, compressed, tail, *, newline=True, suffix=b""):
    records = prefix()
    cap = sum(len((json.dumps(row) + "\n").encode()) for row in records)
    raw = (
        b"\n".join(json.dumps(row).encode() for row in [*records, tail])
        + (b"\n" if newline else b"")
        + suffix
    )
    root = tmp_path / "source"
    root.mkdir()
    path = root / ("session.jsonl.zst" if compressed else "session.jsonl")
    path.write_bytes(zstandard.ZstdCompressor().compress(raw) if compressed else raw)
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", cap)
    return root


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    with ExitStack() as stack:
        if request.param == "postgres":
            import psycopg
            from verdict.storage import PostgresStorage

            test_dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                allow_any_database=False,
            )
            if test_dsn is None:
                pytest.skip(reason)
            dsn = stack.enter_context(isolated_test_dsn(test_dsn))
            storage = PostgresStorage(dsn, min_pool=1, max_pool=1)

            def read_sample():
                with psycopg.connect(dsn) as connection:
                    return json.loads(
                        connection.execute(
                            "SELECT sample_json FROM local_recent_events"
                        ).fetchone()[0]
                    )

            api_storage = dsn
        else:
            path = tmp_path / "synthetic.db"
            storage = SQLiteStorage(str(path))

            def read_sample():
                with sqlite3.connect(path) as connection:
                    return json.loads(
                        connection.execute(
                            "SELECT sample_json FROM local_recent_events"
                        ).fetchone()[0]
                    )

            api_storage = str(path)
        stack.callback(storage.close)
        yield storage, read_sample, api_storage


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("layers", [1, 2])
def test_newest_preview_redacts_encoded_structured_values_before_storage(
    tmp_path, monkeypatch, store, compressed, layers
):
    encoded = "".join(f"\\u{ord(c):04x}" for c in KEY)
    output = json.dumps({"note": json.dumps(encoded) if layers == 2 else encoded})
    tail = {
        "timestamp": "2026-08-30T11:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "synthetic-tail-call",
            "output": output,
        },
    }
    root = prepare(tmp_path, monkeypatch, compressed, tail)
    storage, read_sample, _ = store
    result = local_agents.capture_local_agents(storage, tenant_id="local", codex_root=root)
    assert result.stored == result.source_partial_files == 1
    sample = read_sample()["latestSample"]

    def recover(value):
        try:
            note = json.loads(value)["note"]
            if layers == 2:
                note = json.loads(note)
            return note.encode("ascii").decode("unicode_escape")
        except (ValueError, KeyError, TypeError):
            return None

    assert all(recover(row["preview"]) != KEY for row in sample["records"])


@pytest.mark.parametrize("compressed", [True])
@pytest.mark.parametrize("newline", [False, True])
def test_latest_valid_record_is_visible_with_or_without_final_newline(
    tmp_path, monkeypatch, store, compressed, newline
):
    tail = {
        "timestamp": "2026-08-30T11:00:00Z",
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": NEWEST},
    }
    root = prepare(tmp_path, monkeypatch, compressed, tail, newline=newline)
    storage, _, api_storage = store
    result = local_agents.capture_local_agents(storage, tenant_id="local", codex_root=root)
    assert result.stored == result.source_partial_files == 1
    [bundle] = storage.list_agent_run_bundles("local")
    with TestClient(create_app(storage=api_storage, tenant_id="local")) as client:
        response = client.get(f"/api/runs/{bundle.run.run_id}/recent-local-events")
    assert response.status_code == 200
    assert response.json()["latestSample"]["status"] == "sampled"
    assert NEWEST in response.text


def test_incomplete_final_line_is_ignored_without_losing_previous_complete_record(
    tmp_path,
    monkeypatch,
    store,
):
    tail = {
        "timestamp": "2026-08-30T11:00:00Z",
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": NEWEST},
    }
    root = prepare(tmp_path, monkeypatch, True, tail, suffix=b'{"type":')
    storage, _read_sample, api_storage = store
    assert (
        local_agents.capture_local_agents(
            storage,
            tenant_id="local",
            codex_root=root,
        ).stored
        == 1
    )
    [bundle] = storage.list_agent_run_bundles("local")
    with TestClient(create_app(storage=api_storage, tenant_id="local")) as client:
        response = client.get(f"/api/runs/{bundle.run.run_id}/recent-local-events")
    assert response.status_code == 200
    assert response.json()["latestSample"]["status"] == "sampled"
    assert NEWEST in response.text


def test_metadata_only_newest_preview_has_no_content(tmp_path, monkeypatch, store):
    tail = {
        "timestamp": "2026-08-30T11:00:00Z",
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": KEY},
    }
    root = prepare(tmp_path, monkeypatch, True, tail)
    storage, read_sample, _api_storage = store
    assert (
        local_agents.capture_local_agents(
            storage,
            tenant_id="local",
            codex_root=root,
            capture_content=False,
        ).stored
        == 1
    )
    assert all(not row["preview"] for row in read_sample()["latestSample"]["records"])
