from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import verdict.telemetry.sources.codex as codex_source
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from fastapi.testclient import TestClient
from verdict.dashboard.app import build_bundle, create_app
from verdict.service import run_cycle
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.cli import main
from verdict.telemetry.local_agents import capture_local_agents

DSN, POSTGRES_SKIP_REASON = validate_test_dsn(
    os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
    allow_any_database=os.environ.get("VERDICT_TEST_POSTGRES_ALLOW_ANY_DB") == "1",
)


def _codex_log_body(
    *, thread_id: str, turn_id: str, model: str = "gpt-5.6-sol", suffix: str = ""
) -> str:
    return (
        f"run{{thread.id={thread_id} turn.id={turn_id} model={model}}}: "
        "post sampling token usage "
        f"turn_id={turn_id} total_usage_tokens=42 model_needs_follow_up=false{suffix}"
    )


def _create_log_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE logs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts INTEGER NOT NULL,
               ts_nanos INTEGER NOT NULL,
               level TEXT NOT NULL,
               target TEXT NOT NULL,
               feedback_log_body TEXT,
               module_path TEXT,
               file TEXT,
               line INTEGER,
               thread_id TEXT,
               process_uuid TEXT,
               estimated_bytes INTEGER NOT NULL DEFAULT 0
           )"""
    )
    return connection


def _insert_log(
    connection: sqlite3.Connection,
    *,
    timestamp: int,
    body: str,
    target: str = "codex_core::session::turn",
    nanos: int = 123_000_000,
    process_uuid: str = "process-a",
) -> int:
    cursor = connection.execute(
        """INSERT INTO logs (
               ts, ts_nanos, level, target, feedback_log_body,
               module_path, file, line, thread_id, process_uuid
           ) VALUES (?, ?, 'INFO', ?, ?, '', '', 1, '', ?)""",
        (timestamp, nanos, target, body, process_uuid),
    )
    connection.commit()
    return int(cursor.lastrowid)


def _write_codex_usage(
    path: Path,
    *,
    thread_id: str,
    turn_id: str,
    snapshots: list[tuple[str, int, int, int, int]],
    cached_inputs: list[object] | None = None,
    user_message: str | None = None,
) -> None:
    records = [
        {
            "timestamp": "2026-08-29T10:39:59Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "originator": "Codex Desktop"},
        },
        {
            "timestamp": "2026-08-29T10:39:59Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_id},
        },
    ]
    if user_message is not None:
        records.append(
            {
                "timestamp": "2026-08-29T10:39:59.500Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": user_message},
            }
        )
    assert cached_inputs is None or len(cached_inputs) == len(snapshots)
    for index, (timestamp, input_tokens, output_tokens, total_input, total_output) in enumerate(snapshots):
        last_usage: dict[str, object] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        if cached_inputs is not None:
            last_usage["cached_input_tokens"] = cached_inputs[index]
        records.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": last_usage,
                        "total_token_usage": {
                            "input_tokens": total_input,
                            "output_tokens": total_output,
                            "total_tokens": total_input + total_output,
                        },
                    },
                },
            }
        )
    records.append(
        {
            "timestamp": "2026-08-29T10:40:03Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": turn_id},
        }
    )
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _codex_home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    database = home / "logs_2.sqlite"
    connection = _create_log_database(database)
    for timestamp, thread_id, turn_id, model in (
        (1_788_000_000, "thread-a", "turn-a", "gpt-5.6-sol"),
        (1_788_000_001, "thread-a", "turn-a", "gpt-5.6-sol"),
        (1_788_000_002, "thread-b", "turn-b", "future-model"),
    ):
        _insert_log(
            connection,
            timestamp=timestamp,
            body=_codex_log_body(thread_id=thread_id, turn_id=turn_id, model=model),
        )
    connection.close()
    _write_codex_usage(
        sessions / "thread-a.jsonl",
        thread_id="thread-a",
        turn_id="turn-a",
        snapshots=[
            ("2026-08-29T10:40:00.123Z", 10, 2, 10, 2),
            # Codex can repeat the prior usage with a rate-limit-only event.
            ("2026-08-29T10:40:00.124Z", 10, 2, 10, 2),
            ("2026-08-29T10:40:01.123Z", 20, 3, 30, 5),
        ],
        cached_inputs=[8, 8, 15],
    )
    _write_codex_usage(
        sessions / "thread-b.jsonl",
        thread_id="thread-b",
        turn_id="turn-b",
        snapshots=[("2026-08-29T10:40:02.123Z", 30, 4, 30, 4)],
        cached_inputs=[0],
    )
    return sessions, database


def _write_codex_session(path: Path) -> None:
    records = [
        {
            "timestamp": "2026-08-30T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "session-a", "originator": "Codex Desktop"},
        },
        {
            "timestamp": "2026-08-30T10:01:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-a"},
        },
        {
            "timestamp": "2026-08-30T10:01:01Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-a"},
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_codex_completed_model_responses_reach_management_report(tmp_path: Path) -> None:
    codex_root, _ = _codex_home(tmp_path)
    verdict_database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(verdict_database))
    try:
        summary = capture_local_agents(
            storage,
            tenant_id="tenant-a",
            codex_root=codex_root,
            capture_content=False,
        )
        traces = storage.list_traces(tenant_id="tenant-a")
    finally:
        storage.close()

    assert summary.as_dict()["codex_model_calls"] == {
        "status": "complete",
        "seen": 3,
        "stored": 3,
        "skipped": 0,
        "skip_reasons": {},
        "error": None,
    }
    assert len(traces) == 3
    assert all(trace.provider == "openai" and trace.service_name == "codex" for trace in traces)
    assert all(
        trace.prompt_redacted is None
        and trace.response_redacted is None
        and trace.raw_messages is None
        and trace.latency_ms is None
        and trace.cost_usd is None
        for trace in traces
    )
    assert sorted(
        (
            trace.input_tokens,
            trace.output_tokens,
            trace.tags.get("verdict.cached_input_tokens"),
        )
        for trace in traces
    ) == [(10, 2, "8"), (20, 3, "15"), (30, 4, "0")]

    report = build_bundle(
        verdict_database,
        registry_tenant="tenant-a",
        report_days=0,
    )["managementReport"]
    assert report["scope"]["calls"] == 3
    assert report["scope"]["tokenKnownCalls"] == 3
    assert report["scope"]["inputTokens"] == 60
    assert report["scope"]["outputTokens"] == 9
    assert report["scope"]["totalTokens"] == 69
    assert report["scope"]["cachedInputTokens"] == 23
    assert report["scope"]["uncachedInputTokens"] == 37
    assert report["scope"]["inputBreakdownKnownCalls"] == 3
    assert report["scope"]["latencyKnownCalls"] == 0
    assert report["scope"]["costKnownCalls"] == 0
    assert [(row["provider"], row["model"], row["calls"]) for row in report["models"]["rows"]] == [
        ("openai", "gpt-5.6-sol", 2),
        ("openai", "future-model", 1),
    ]
    assert report["applications"]["rows"][0]["name"] == "codex"


def test_rejected_codex_history_revision_preserves_accepted_diagnostic_usage(
    tmp_path: Path,
) -> None:
    codex_root, diagnostic_path = _codex_home(tmp_path)
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    try:
        first = capture_local_agents(
            storage, tenant_id="tenant-a", codex_root=codex_root,
            capture_content=False,
        )
        assert first.stored == 2
        before = {
            trace.trace_id: (trace.input_tokens, trace.output_tokens)
            for trace in storage.list_traces(tenant_id="tenant-a")
        }
        assert sorted(before.values()) == [(10, 2), (20, 3), (30, 4)]

        source = codex_root / "thread-a.jsonl"
        accepted_bytes = source.read_bytes()
        assert b'"input_tokens": 20' in accepted_bytes
        source.write_bytes(accepted_bytes.replace(
            b'"input_tokens": 20', b'"input_tokens": 90', 1,
        ))
        with sqlite3.connect(diagnostic_path) as connection:
            _insert_log(
                connection, timestamp=1_788_000_003,
                body=_codex_log_body(thread_id="thread-a", turn_id="turn-a"),
            )

        second = capture_local_agents(
            storage, tenant_id="tenant-a", codex_root=codex_root,
            capture_content=False,
        )
        assert second.skip_reasons == {"local_source_conflict": 1}
        after = {
            trace.trace_id: (trace.input_tokens, trace.output_tokens)
            for trace in storage.list_traces(tenant_id="tenant-a")
        }
        assert all(after[trace_id] == usage for trace_id, usage in before.items())
        assert len(after) == 4
        assert sorted(usage for usage in after.values() if usage[0] is not None) == [
            (10, 2), (20, 3), (30, 4),
        ]
        assert sum(usage == (None, None) for usage in after.values()) == 1
        report = build_bundle(database, registry_tenant="tenant-a", report_days=0)[
            "managementReport"
        ]
        assert report["scope"]["calls"] == 4
        assert report["scope"]["tokenKnownCalls"] == 3
        assert report["scope"]["totalTokens"] == 69
    finally:
        storage.close()
    with TestClient(create_app(storage=database, tenant_id="tenant-a")) as client:
        response = client.get("/api/data", params={"report_days": 0})
        assert response.status_code == 200
        scope = response.json()["managementReport"]["scope"]
        assert (scope["calls"], scope["tokenKnownCalls"], scope["totalTokens"]) == (
            4, 3, 69,
        )


def test_unchanged_codex_history_preserves_usage_and_imports_diagnostic_only_call(
    tmp_path: Path,
) -> None:
    codex_root, diagnostic_path = _codex_home(tmp_path)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(
            storage, tenant_id="tenant-a", codex_root=codex_root,
            capture_content=False,
        ).stored == 2
        before = {
            trace.trace_id: (trace.input_tokens, trace.output_tokens)
            for trace in storage.list_traces(tenant_id="tenant-a")
        }
        with sqlite3.connect(diagnostic_path) as connection:
            _insert_log(
                connection, timestamp=1_788_000_004,
                body=_codex_log_body(thread_id="diagnostic-only", turn_id="turn-c"),
            )
        second = capture_local_agents(
            storage, tenant_id="tenant-a", codex_root=codex_root,
            capture_content=False,
        )
        assert second.stored == 2 and second.scan_incomplete is False
        after = {
            trace.trace_id: (trace.input_tokens, trace.output_tokens)
            for trace in storage.list_traces(tenant_id="tenant-a")
        }
        assert all(after[trace_id] == usage for trace_id, usage in before.items())
        assert len(after) == 4
        assert sum(usage == (None, None) for usage in after.values()) == 1
    finally:
        storage.close()


def test_rejected_codex_rescan_does_not_overwrite_newer_trace_update(
    tmp_path: Path, monkeypatch,
) -> None:
    codex_root, _ = _codex_home(tmp_path)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(
            storage, tenant_id="tenant-a", codex_root=codex_root,
            capture_content=False,
        ).stored == 2
        source = codex_root / "thread-a.jsonl"
        source.write_bytes(source.read_bytes().replace(
            b'"input_tokens": 20', b'"input_tokens": 90', 1,
        ))
        original_get = storage.get_trace
        raced = []

        def concurrent_update(trace_id):
            existing = original_get(trace_id)
            if existing is not None and existing.input_tokens == 20 and not raced:
                storage.insert_trace(replace(existing, input_tokens=77, output_tokens=8))
                raced.append(trace_id)
            return existing

        monkeypatch.setattr(storage, "get_trace", concurrent_update)
        second = capture_local_agents(
            storage, tenant_id="tenant-a", codex_root=codex_root,
            capture_content=False,
        )
        assert second.skip_reasons == {"local_source_conflict": 1}
        assert len(raced) == 1
        updated = original_get(raced[0])
        assert (updated.input_tokens, updated.output_tokens) == (77, 8)
    finally:
        storage.close()


def test_diagnostic_rescan_preserves_prior_usage_after_history_file_disappears(
    tmp_path: Path,
) -> None:
    codex_root, _ = _codex_home(tmp_path)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(
            storage, tenant_id="tenant-a", codex_root=codex_root,
            capture_content=False,
        ).stored == 2
        before = {
            trace.trace_id: (trace.input_tokens, trace.output_tokens)
            for trace in storage.list_traces(tenant_id="tenant-a")
        }
        for source in codex_root.glob("*.jsonl"):
            source.unlink()
        second = capture_local_agents(
            storage, tenant_id="tenant-a", codex_root=codex_root,
            capture_content=False,
        )
        assert second.files == 0 and second.skipped == 0
        assert second.codex_model_calls is not None
        assert second.codex_model_calls.skip_reasons == {"prior_usage_retained": 3}
        assert second.as_dict()["codex_model_calls"]["status"] == "partial"
        assert {
            trace.trace_id: (trace.input_tokens, trace.output_tokens)
            for trace in storage.list_traces(tenant_id="tenant-a")
        } == before
    finally:
        storage.close()


@pytest.mark.skipif(DSN is None, reason=POSTGRES_SKIP_REASON)
def test_postgres_rejected_codex_revision_does_not_rewrite_prior_call_usage(
    tmp_path: Path,
) -> None:
    assert DSN is not None
    codex_root, _ = _codex_home(tmp_path)
    with isolated_test_dsn(DSN) as scoped_dsn:
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=2)
        try:
            assert capture_local_agents(
                storage, tenant_id="tenant-a", codex_root=codex_root,
                capture_content=False,
            ).stored == 2
            before = {
                trace.trace_id: (trace.input_tokens, trace.output_tokens)
                for trace in storage.list_traces(tenant_id="tenant-a")
            }
            source = codex_root / "thread-a.jsonl"
            source.write_bytes(source.read_bytes().replace(
                b'"input_tokens": 20', b'"input_tokens": 90', 1,
            ))
            second = capture_local_agents(
                storage, tenant_id="tenant-a", codex_root=codex_root,
                capture_content=False,
            )
            assert second.skip_reasons == {"local_source_conflict": 1}
            assert {
                trace.trace_id: (trace.input_tokens, trace.output_tokens)
                for trace in storage.list_traces(tenant_id="tenant-a")
            } == before
        finally:
            storage.close()


def test_codex_call_tokens_remain_unavailable_when_usage_match_is_ambiguous(
    tmp_path: Path,
) -> None:
    codex_root, _ = _codex_home(tmp_path)
    _write_codex_usage(
        codex_root / "thread-b.jsonl",
        thread_id="thread-b",
        turn_id="turn-b",
        snapshots=[
            ("2026-08-29T10:40:02.100Z", 30, 4, 30, 4),
            ("2026-08-29T10:40:02.150Z", 31, 5, 61, 9),
        ],
        cached_inputs=[20, 21],
    )
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        capture_local_agents(storage, tenant_id="tenant-a", codex_root=codex_root)
        [trace] = [
            trace
            for trace in storage.list_traces(tenant_id="tenant-a")
            if trace.request_model == "future-model"
        ]
    finally:
        storage.close()

    assert trace.input_tokens is None
    assert trace.output_tokens is None
    assert "verdict.cached_input_tokens" not in trace.tags


def test_codex_call_keeps_tokens_but_omits_impossible_cached_input(
    tmp_path: Path,
) -> None:
    codex_root, _ = _codex_home(tmp_path)
    _write_codex_usage(
        codex_root / "thread-b.jsonl",
        thread_id="thread-b",
        turn_id="turn-b",
        snapshots=[("2026-08-29T10:40:02.123Z", 30, 4, 30, 4)],
        cached_inputs=[31],
    )
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        capture_local_agents(storage, tenant_id="tenant-a", codex_root=codex_root)
        [trace] = [
            trace
            for trace in storage.list_traces(tenant_id="tenant-a")
            if trace.request_model == "future-model"
        ]
    finally:
        storage.close()

    assert (trace.input_tokens, trace.output_tokens) == (30, 4)
    assert "verdict.cached_input_tokens" not in trace.tags


def test_codex_call_tokens_do_not_depend_on_replacing_an_existing_agent_run(
    tmp_path: Path,
) -> None:
    codex_root, source_database = _codex_home(tmp_path)
    _write_codex_usage(
        codex_root / "thread-a.jsonl",
        thread_id="thread-a",
        turn_id="turn-a",
        snapshots=[
            ("2026-08-29T10:40:00.123Z", 10, 2, 10, 2),
            ("2026-08-29T10:40:01.123Z", 20, 3, 30, 5),
        ],
        user_message="original request",
    )
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        capture_local_agents(storage, tenant_id="tenant-a", codex_root=codex_root)
        _write_codex_usage(
            codex_root / "thread-a.jsonl",
            thread_id="thread-a",
            turn_id="turn-a",
            snapshots=[
                ("2026-08-29T10:40:00.123Z", 10, 2, 10, 2),
                ("2026-08-29T10:40:01.123Z", 20, 3, 30, 5),
                ("2026-08-29T10:40:03.123Z", 10, 2, 40, 7),
            ],
            user_message="replacement request",
        )
        with sqlite3.connect(source_database) as connection:
            _insert_log(
                connection,
                timestamp=1_788_000_003,
                body=_codex_log_body(thread_id="thread-a", turn_id="turn-a"),
            )
        summary = capture_local_agents(
            storage, tenant_id="tenant-a", codex_root=codex_root
        )
        [new_trace] = [
            trace
            for trace in storage.list_traces(tenant_id="tenant-a")
            if trace.started_at.timestamp() == 1_788_000_003.123
        ]
    finally:
        storage.close()

    assert summary.skipped == 1
    assert summary.skip_reasons == {"local_source_conflict": 1}
    assert new_trace.input_tokens == 10
    assert new_trace.output_tokens == 2


def test_codex_diagnostic_rescan_is_idempotent_and_source_pruning_is_not_deletion(
    tmp_path: Path,
) -> None:
    codex_root, source_database = _codex_home(tmp_path)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        capture_local_agents(storage, tenant_id="tenant-a", codex_root=codex_root)
        capture_local_agents(storage, tenant_id="tenant-a", codex_root=codex_root)
        assert len(storage.list_traces(tenant_id="tenant-a")) == 3

        with sqlite3.connect(source_database) as connection:
            connection.execute("DELETE FROM logs WHERE id = 1")
        capture_local_agents(storage, tenant_id="tenant-a", codex_root=codex_root)
        assert len(storage.list_traces(tenant_id="tenant-a")) == 3

        with sqlite3.connect(source_database) as connection:
            _insert_log(
                connection,
                timestamp=1_788_000_003,
                body=_codex_log_body(thread_id="thread-c", turn_id="turn-c"),
            )
        capture_local_agents(storage, tenant_id="tenant-a", codex_root=codex_root)
        assert len(storage.list_traces(tenant_id="tenant-a")) == 4
    finally:
        storage.close()


def test_codex_diagnostics_fail_closed_and_never_persist_the_log_body(tmp_path: Path) -> None:
    codex_root, source_database = _codex_home(tmp_path)
    with sqlite3.connect(source_database) as connection:
        connection.execute("DELETE FROM logs")
        _insert_log(
            connection,
            timestamp=1_788_000_000,
            body=_codex_log_body(
                thread_id="raw-thread-canary",
                turn_id="turn-a",
                suffix=" secret=DO_NOT_STORE process_secret=ALSO_PRIVATE",
            ),
            process_uuid="raw-process-canary",
        )
        _insert_log(
            connection,
            timestamp=1_788_000_001,
            body="run{thread.id=t turn.id=a}: post sampling token usage turn_id=a",
        )
        _insert_log(
            connection,
            timestamp=1_788_000_002,
            body=_codex_log_body(thread_id="t", turn_id="a").replace("turn_id=a", "turn_id=b"),
        )
        marker = _codex_log_body(thread_id="t", turn_id="a")
        _insert_log(
            connection,
            timestamp=1_788_000_003,
            body=f"{marker} post sampling token usage turn_id=a",
        )
        _insert_log(
            connection,
            timestamp=1_788_000_004,
            body=marker + "x" * (codex_source._MAX_BODY_BYTES + 1),
        )
        _insert_log(connection, timestamp=1_788_000_005, body=marker, nanos=-1)

    verdict_database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(verdict_database))
    try:
        summary = capture_local_agents(storage, tenant_id="tenant-a", codex_root=codex_root)
        traces = storage.list_traces(tenant_id="tenant-a")
    finally:
        storage.close()

    calls = summary.as_dict()["codex_model_calls"]
    assert calls["seen"] == 6
    assert calls["stored"] == 1
    assert calls["skip_reasons"] == {
        "invalid_call_boundary": 3,
        "invalid_diagnostic_body": 1,
        "invalid_start_time": 1,
    }
    assert len(traces) == 1
    persisted = verdict_database.read_bytes()
    for secret in (
        b"DO_NOT_STORE",
        b"ALSO_PRIVATE",
        b"raw-thread-canary",
        b"raw-process-canary",
    ):
        assert secret not in persisted


def test_broken_or_symlinked_diagnostics_do_not_block_codex_history(tmp_path: Path) -> None:
    for case in ("broken", "symlink"):
        home = tmp_path / case
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        _write_codex_session(sessions / "session.jsonl")
        database = home / "logs_2.sqlite"
        if case == "broken":
            sqlite3.connect(database).close()
        else:
            outside = tmp_path / "outside.sqlite"
            sqlite3.connect(outside).close()
            database.symlink_to(outside)
        storage = SQLiteStorage(str(home / "verdict.db"))
        try:
            summary = capture_local_agents(storage, tenant_id="tenant-a", codex_root=sessions)
            bundles = storage.list_agent_run_bundles("tenant-a")
        finally:
            storage.close()
        assert summary.stored == 1
        assert len(bundles) == 1
        assert summary.as_dict()["codex_model_calls"]["status"] == "unavailable"


def test_codex_diagnostics_are_tenant_isolated_and_readable_during_wal_writes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    writer = _create_log_database(home / "logs_2.sqlite")
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    _insert_log(
        writer,
        timestamp=1_788_000_000,
        body=_codex_log_body(thread_id="thread-a", turn_id="turn-a"),
    )
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        capture_local_agents(storage, tenant_id="tenant-a", codex_root=sessions)
        _insert_log(
            writer,
            timestamp=1_788_000_001,
            body=_codex_log_body(thread_id="thread-b", turn_id="turn-b"),
        )
        capture_local_agents(storage, tenant_id="tenant-a", codex_root=sessions)
        capture_local_agents(storage, tenant_id="tenant-b", codex_root=sessions)
        tenant_a = storage.list_traces(tenant_id="tenant-a")
        tenant_b = storage.list_traces(tenant_id="tenant-b")
    finally:
        writer.close()
        storage.close()

    assert len(tenant_a) == len(tenant_b) == 2
    assert {trace.trace_id for trace in tenant_a}.isdisjoint(trace.trace_id for trace in tenant_b)


def test_codex_diagnostic_limit_keeps_the_newest_calls(tmp_path: Path, monkeypatch) -> None:
    codex_root, _ = _codex_home(tmp_path)
    monkeypatch.setattr(codex_source, "_MAX_CALLS", 2)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        summary = capture_local_agents(storage, tenant_id="tenant-a", codex_root=codex_root)
        traces = storage.list_traces(tenant_id="tenant-a")
    finally:
        storage.close()

    assert summary.as_dict()["codex_model_calls"] == {
        "status": "partial",
        "seen": 3,
        "stored": 2,
        "skipped": 1,
        "skip_reasons": {"source_record_limit": 1},
        "error": None,
    }
    assert {trace.started_at.timestamp() for trace in traces} == {
        1_788_000_001.123,
        1_788_000_002.123,
    }


def test_local_cli_imports_codex_calls_without_an_export(tmp_path: Path, capsys) -> None:
    codex_root, _ = _codex_home(tmp_path)
    verdict_database = tmp_path / "verdict.db"

    status = main(
        [
            "local",
            "--storage",
            f"sqlite:///{verdict_database}",
            "--tenant-id",
            "tenant-cli",
            "--claude-root",
            str(tmp_path / "missing-claude"),
            "--codex-root",
            str(codex_root),
            "--no-capture-content",
        ]
    )

    assert status == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["codex_model_calls"]["stored"] == 3
    report = build_bundle(
        verdict_database,
        registry_tenant="tenant-cli",
        report_days=0,
    )["managementReport"]
    assert report["scope"]["calls"] == 3


def test_scheduled_local_capture_imports_new_codex_calls(tmp_path: Path) -> None:
    codex_root, source_database = _codex_home(tmp_path)
    storage_url = f"sqlite:///{tmp_path / 'verdict.db'}"

    first = run_cycle(storage_url, {"codexRoot": str(codex_root), "runMonitor": False})
    with sqlite3.connect(source_database) as connection:
        _insert_log(
            connection,
            timestamp=1_788_000_003,
            body=_codex_log_body(thread_id="thread-c", turn_id="turn-c"),
        )
    second = run_cycle(storage_url, {"codexRoot": str(codex_root), "runMonitor": False})

    assert first["capture"]["codex_model_calls"]["stored"] == 3
    assert second["capture"]["codex_model_calls"]["stored"] == 4
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert len(storage.list_traces(tenant_id="__verdict_local__")) == 4
    finally:
        storage.close()


def test_worker_rescan_rejects_history_change_without_erasing_call_tokens(
    tmp_path: Path,
) -> None:
    codex_root, _ = _codex_home(tmp_path)
    database = tmp_path / "verdict.db"
    storage_url = f"sqlite:///{database}"
    first = run_cycle(storage_url, {"codexRoot": str(codex_root), "runMonitor": False})
    assert first["capture"]["stored"] == 2
    before = build_bundle(database, report_days=0)["managementReport"]["scope"]
    assert (before["tokenKnownCalls"], before["totalTokens"]) == (3, 69)

    source = codex_root / "thread-a.jsonl"
    source.write_bytes(source.read_bytes().replace(
        b'"input_tokens": 20', b'"input_tokens": 90', 1,
    ))
    second = run_cycle(storage_url, {"codexRoot": str(codex_root), "runMonitor": False})
    assert second["capture"]["skip_reasons"] == {"local_source_conflict": 1}
    after = build_bundle(database, report_days=0)["managementReport"]["scope"]
    assert (after["tokenKnownCalls"], after["totalTokens"]) == (3, 69)
