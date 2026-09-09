from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from verdict import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    SourceSession,
    Trace,
    agent_run_bundle_to_json,
)
from verdict.capture import AgentCaptureService
from verdict.evidence import AgentCaptureBatch
from verdict.storage import SQLiteStorage

NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)


def _capture(*, tenant_id: str = "tenant-a") -> tuple[AgentRunBundle, Trace]:
    source = SourceSession(
        source_session_id="source-1",
        tenant_id=tenant_id,
        source_kind="unknown-agent",
        source_locator_hash="a" * 64,
        started_at=NOW,
        observed_at=NOW,
        ended_at=NOW,
    )
    run = AgentRun(
        run_id="run-1",
        source_session_id=source.source_session_id,
        tenant_id=tenant_id,
        started_at=NOW,
        status=ExecutionStatus.COMPLETED,
        ended_at=NOW,
        agent_name="support-agent",
        agent_version="2026-09-06",
        session_id="conversation-1",
        parent_run_id="parent-run",
        service_name="support-service",
        environment="test",
        instance_id="host-1",
    )
    turn = AgentTurn(
        turn_id="turn-1",
        run_id=run.run_id,
        sequence=0,
        started_at=NOW,
        status=ExecutionStatus.COMPLETED,
        ended_at=NOW,
        user_request_redacted="answer the customer",
        final_response_redacted="resolved",
        request_state=EvidenceState.PRESENT,
        response_state=EvidenceState.PRESENT,
    )
    event = AgentEvent(
        event_id="event-1",
        turn_id=turn.turn_id,
        sequence=0,
        occurred_at=NOW,
        event_type=AgentEventType.MODEL_CALL,
        status=ExecutionStatus.COMPLETED,
        provenance="unknown-agent:model",
        attributes={"provider": "unknown-provider", "response_model": "model-x"},
        trace_id="trace-1",
        producer_id="worker-1",
        producer_sequence=7,
        parent_event_id="parent-event",
    )
    trace = Trace(
        trace_id="trace-1",
        tenant_id=tenant_id,
        started_at=NOW,
        ended_at=NOW,
        provider="unknown-provider",
        request_model="model-x",
        response_model="model-x",
        prompt_redacted="MODEL_PROMPT_CANARY",
        response_redacted="MODEL_RESPONSE_CANARY",
        raw_messages=[
            {"role": "user", "content": "MODEL_PROMPT_CANARY"},
            {"role": "assistant", "content": "MODEL_RESPONSE_CANARY"},
        ],
        tags={"verdict.workload": "agent", "verdict.agent_run_id": run.run_id},
    )
    return AgentRunBundle(source, run, (turn,), (event,)), trace


def _table_count(path: Path, table: str) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_capture_persists_normalized_rows_without_copying_model_content(tmp_path: Path) -> None:
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    bundle, trace = _capture()

    AgentCaptureService(storage).capture(bundle, traces=(trace,))

    assert storage.get_agent_run_bundle("tenant-a", "run-1") == bundle
    assert _table_count(database, "import_sources") == 1
    assert _table_count(database, "agent_runs") == 1
    assert _table_count(database, "agent_turns") == 1
    assert _table_count(database, "agent_events") == 1
    assert _table_count(database, "agent_run_bundles") == 0
    with sqlite3.connect(database) as connection:
        attributes = connection.execute(
            "SELECT attributes_json FROM agent_events WHERE event_id='event-1'"
        ).fetchone()[0]
        prompt = connection.execute(
            "SELECT prompt_redacted FROM traces WHERE trace_id='trace-1'"
        ).fetchone()[0]
    assert "MODEL_PROMPT_CANARY" not in attributes
    assert "MODEL_RESPONSE_CANARY" not in attributes
    assert prompt == "MODEL_PROMPT_CANARY"


def test_capture_rolls_back_trace_when_normalized_event_write_fails(tmp_path: Path) -> None:
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    bundle, trace = _capture()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TRIGGER reject_agent_event BEFORE INSERT ON agent_events
               BEGIN SELECT RAISE(ABORT, 'injected event failure'); END"""
        )

    with pytest.raises(ValueError, match="conflicts with existing evidence"):
        AgentCaptureService(storage).capture(bundle, traces=(trace,))

    assert storage.get_trace("trace-1") is None
    assert storage.get_agent_run_bundle("tenant-a", "run-1") is None
    assert _table_count(database, "import_sources") == 0
    assert _table_count(database, "agent_runs") == 0
    assert _table_count(database, "agent_turns") == 0
    assert _table_count(database, "agent_events") == 0


def test_capture_appends_one_run_revision_without_duplicate_children(tmp_path: Path) -> None:
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    bundle, trace = _capture()
    appended_turn = replace(
        bundle.turns[0],
        turn_id="turn-2",
        sequence=1,
        user_request_redacted="follow up",
        final_response_redacted="completed",
    )
    replacement = replace(
        bundle,
        turns=(*bundle.turns, appended_turn),
    )

    service = AgentCaptureService(storage)
    service.capture(bundle, traces=(trace,))
    service.capture(replacement, traces=(trace,))

    loaded = storage.get_agent_run_bundle("tenant-a", "run-1")
    assert loaded == replacement
    assert _table_count(database, "agent_runs") == 1
    assert _table_count(database, "agent_turns") == 2
    assert _table_count(database, "agent_events") == 1
    assert _table_count(database, "traces") == 1


def test_capture_advances_in_progress_lifecycle_without_replacing_prior_facts(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    completed, completed_trace = _capture()
    pending = replace(
        completed,
        run=replace(completed.run, status=ExecutionStatus.UNKNOWN, ended_at=None),
        turns=(
            replace(
                completed.turns[0],
                status=ExecutionStatus.UNKNOWN,
                ended_at=None,
                final_response_redacted=None,
                response_state=EvidenceState.NOT_CAPTURED,
            ),
        ),
        events=(
            replace(
                completed.events[0],
                status=ExecutionStatus.UNKNOWN,
                attributes={"provider": "unknown-provider"},
                trace_id=None,
                producer_id="",
                producer_sequence=None,
                parent_event_id=None,
            ),
        ),
    )
    storage.replace_agent_capture(pending)
    storage.replace_agent_capture(completed, (completed_trace,))

    assert storage.get_agent_run_bundle("tenant-a", "run-1") == completed
    stored_trace = storage.get_trace("trace-1")
    assert stored_trace is not None
    assert stored_trace.response_redacted == "MODEL_RESPONSE_CANARY"
    assert stored_trace.raw_messages == completed_trace.raw_messages


def test_capture_adds_response_without_replacing_an_existing_terminal_end(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    completed, completed_trace = _capture()
    prompt_only = replace(
        completed,
        turns=(
            replace(
                completed.turns[0],
                final_response_redacted=None,
                response_state=EvidenceState.MISSING,
            ),
        ),
    )
    prompt_only_trace = replace(
        completed_trace,
        response_redacted=None,
        raw_messages=completed_trace.raw_messages[:1],
    )
    earlier_completion = replace(
        completed,
        turns=(replace(completed.turns[0], ended_at=NOW - timedelta(seconds=1)),),
    )

    storage.replace_agent_capture(prompt_only, (prompt_only_trace,))
    before = storage.get_trace("trace-1")
    storage.replace_agent_capture(earlier_completion, (completed_trace,))

    loaded = storage.get_agent_run_bundle("tenant-a", "run-1")
    stored = storage.get_trace("trace-1")
    assert loaded is not None
    assert before is not None
    assert stored is not None
    assert loaded.turns[0].ended_at == NOW
    assert loaded.turns[0].final_response_redacted == "resolved"
    assert stored.response_redacted == "MODEL_RESPONSE_CANARY"
    assert stored.raw_messages == completed_trace.raw_messages
    assert stored.analysis_raw_messages_state == "valid"
    assert before.analysis_raw_messages_utf8_bytes is not None
    assert stored.analysis_raw_messages_utf8_bytes is not None
    assert stored.analysis_raw_messages_utf8_bytes > before.analysis_raw_messages_utf8_bytes


def test_capture_advances_open_run_end_as_source_grows(tmp_path: Path) -> None:
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    completed, trace = _capture()
    first_end = NOW - timedelta(minutes=1)
    open_run = replace(
        completed,
        run=replace(completed.run, status=ExecutionStatus.UNKNOWN, ended_at=first_end),
    )
    extended = replace(
        open_run,
        session=replace(open_run.session, observed_at=NOW + timedelta(minutes=1)),
        run=replace(open_run.run, ended_at=NOW),
    )

    storage.replace_agent_capture(open_run, (trace,))
    storage.replace_agent_capture(extended, (trace,))

    loaded = storage.get_agent_run_bundle("tenant-a", "run-1")
    assert loaded is not None
    assert loaded.run.status is ExecutionStatus.UNKNOWN
    assert loaded.run.ended_at == NOW
    assert loaded.session.observed_at == NOW + timedelta(minutes=1)


def test_capture_rejects_conflicting_completed_evidence_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    bundle, trace = _capture()
    conflicting = replace(
        bundle,
        turns=(replace(bundle.turns[0], final_response_redacted="different"),),
    )
    service = AgentCaptureService(storage)
    service.capture(bundle, traces=(trace,))

    with pytest.raises(ValueError, match="turn response"):
        service.capture(conflicting, traces=(trace,))

    assert storage.get_agent_run_bundle("tenant-a", "run-1") == bundle


def test_capture_rejects_conflicting_trace_completion_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    bundle, trace = _capture()
    service = AgentCaptureService(storage)
    service.capture(bundle, traces=(trace,))
    stored_trace = storage.get_trace(trace.trace_id)

    with pytest.raises(ValueError, match="Trace response"):
        service.capture(
            bundle,
            traces=(replace(trace, response_redacted="different"),),
        )

    assert storage.get_trace(trace.trace_id) == stored_trace
    assert storage.get_agent_run_bundle("tenant-a", "run-1") == bundle


def test_capture_rejects_rewritten_trace_messages_without_mutation(tmp_path: Path) -> None:
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    bundle, trace = _capture()
    service = AgentCaptureService(storage)
    service.capture(bundle, traces=(trace,))
    stored_trace = storage.get_trace(trace.trace_id)

    with pytest.raises(ValueError, match="Trace messages"):
        service.capture(
            bundle,
            traces=(replace(trace, raw_messages=[{"role": "user", "content": "changed"}]),),
        )

    assert storage.get_trace(trace.trace_id) == stored_trace


def test_idempotent_capture_does_not_rewrite_trace_messages(tmp_path: Path) -> None:
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    bundle, trace = _capture()
    storage.replace_agent_capture(bundle, (trace,))
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TRIGGER reject_message_rewrite
               BEFORE UPDATE OF raw_messages_json ON traces
               BEGIN SELECT RAISE(ABORT, 'message rewrite'); END"""
        )

    storage.replace_agent_capture(bundle, (trace,))


def test_storage_rejects_unlinked_trace_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    bundle, trace = _capture()

    with pytest.raises(ValueError, match="unlinked Trace"):
        storage.replace_agent_capture(
            replace(bundle, events=(replace(bundle.events[0], trace_id=None),)),
            (trace,),
        )

    assert storage.get_trace(trace.trace_id) is None
    assert storage.get_agent_run_bundle("tenant-a", "run-1") is None


def test_append_does_not_reconstruct_the_existing_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    bundle, trace = _capture()
    storage.replace_agent_capture(bundle, (trace,))
    appended_turn = replace(
        bundle.turns[0],
        turn_id="turn-2",
        sequence=1,
        user_request_redacted="follow up",
        final_response_redacted="completed",
    )
    monkeypatch.setattr(
        storage,
        "_read_normalized_bundles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("whole-run read")),
    )

    storage.append_agent_capture(
        AgentCaptureBatch(bundle.session, bundle.run, (appended_turn,)),
    )

    assert _table_count(database, "agent_turns") == 2


def test_normalized_agent_identities_are_tenant_scoped(tmp_path: Path) -> None:
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    first, _trace = _capture(tenant_id="tenant-a")
    second, _trace = _capture(tenant_id="tenant-b")
    first = replace(first, events=(replace(first.events[0], trace_id=None),))
    second = replace(second, events=(replace(second.events[0], trace_id=None),))

    storage.replace_agent_run_bundle(first)
    storage.replace_agent_run_bundle(second)

    assert storage.get_agent_run_bundle("tenant-a", "run-1") == first
    assert storage.get_agent_run_bundle("tenant-b", "run-1") == second


def test_opening_a17_bundle_store_migrates_it_without_changing_identity(tmp_path: Path) -> None:
    database = tmp_path / "verdict.db"
    first, _trace = _capture()
    first = replace(
        first,
        run=replace(
            first.run,
            session_id=None,
            parent_run_id=None,
            service_name="",
            environment="",
            instance_id="",
        ),
        events=(
            replace(
                first.events[0],
                producer_id="",
                producer_sequence=None,
                parent_event_id=None,
            ),
        ),
    )
    expected_first = replace(first, events=(replace(first.events[0], trace_id=None),))
    second = replace(
        first,
        session=replace(
            first.session,
            source_session_id="source-2",
            source_locator_hash="b" * 64,
        ),
        run=replace(first.run, run_id="run-2", source_session_id="source-2"),
        turns=(replace(first.turns[0], run_id="run-2"),),
    )
    expected_second = replace(second, events=(replace(second.events[0], trace_id=None),))
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE agent_run_bundles (
                tenant_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source_session_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, run_id)
            )"""
        )
        connection.executemany(
            """INSERT INTO agent_run_bundles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    bundle.run.tenant_id,
                    bundle.run.run_id,
                    bundle.session.source_session_id,
                    bundle.session.source_kind,
                    bundle.run.started_at.isoformat(),
                    bundle.run.ended_at.isoformat(),
                    bundle.run.status.value,
                    bundle.content_hash,
                    agent_run_bundle_to_json(bundle),
                    NOW.isoformat(),
                )
                for bundle in (first, second)
            ],
        )

    storage = SQLiteStorage(str(database))

    assert storage.get_agent_run_bundle("tenant-a", "run-1") == expected_first
    assert storage.get_agent_run_bundle("tenant-a", "run-2") == expected_second
    assert _table_count(database, "agent_runs") == 2
    assert _table_count(database, "agent_turns") == 2
    assert _table_count(database, "agent_events") == 2
    assert _table_count(database, "agent_run_bundles") == 2
    storage.close()


def test_concurrent_sqlite_legacy_bundle_migration_has_one_winner(tmp_path: Path) -> None:
    database = tmp_path / "verdict.db"
    bundle, _trace = _capture()
    bundle = replace(bundle, events=(replace(bundle.events[0], trace_id=None),))
    SQLiteStorage(str(database)).close()
    storages = [SQLiteStorage(str(database)), SQLiteStorage(str(database))]
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER reject_legacy_agent_run_bundle_insert")
        connection.execute("DROP TRIGGER reject_legacy_agent_run_bundle_update")
        connection.execute("DELETE FROM verdict_schema_migrations")
        connection.execute(
            "INSERT INTO agent_run_bundles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bundle.run.tenant_id,
                bundle.run.run_id,
                bundle.session.source_session_id,
                bundle.session.source_kind,
                bundle.run.started_at.isoformat(),
                bundle.run.ended_at.isoformat(),
                bundle.run.status.value,
                bundle.content_hash,
                agent_run_bundle_to_json(bundle),
                NOW.isoformat(),
            ),
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda storage: storage._migrate_agent_run_bundles(), storages))
        assert storages[0].get_agent_run_bundle("tenant-a", "run-1") == bundle
    finally:
        for storage in storages:
            storage.close()
    assert _table_count(database, "agent_runs") == 1


def test_sqlite_rejects_legacy_writes_after_normalized_migration(tmp_path: Path) -> None:
    database = tmp_path / "verdict.db"
    bundle, _trace = _capture()
    SQLiteStorage(str(database)).close()

    with (
        sqlite3.connect(database) as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match=r"^legacy agent evidence writer detected after normalized migration$",
        ),
    ):
        connection.execute(
            "INSERT INTO agent_run_bundles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bundle.run.tenant_id,
                bundle.run.run_id,
                bundle.session.source_session_id,
                bundle.session.source_kind,
                bundle.run.started_at.isoformat(),
                bundle.run.ended_at.isoformat(),
                bundle.run.status.value,
                bundle.content_hash,
                agent_run_bundle_to_json(bundle),
                NOW.isoformat(),
            ),
        )


def test_sqlite_detects_orphan_from_an_earlier_normalized_writer(tmp_path: Path) -> None:
    database = tmp_path / "verdict.db"
    bundle, _trace = _capture()
    SQLiteStorage(str(database)).close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER reject_legacy_agent_run_bundle_insert")
        connection.execute(
            "DELETE FROM verdict_schema_migrations "
            "WHERE name='block_legacy_agent_evidence_writes_v1'"
        )
        connection.execute(
            "INSERT INTO agent_run_bundles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bundle.run.tenant_id,
                bundle.run.run_id,
                bundle.session.source_session_id,
                bundle.session.source_kind,
                bundle.run.started_at.isoformat(),
                bundle.run.ended_at.isoformat(),
                bundle.run.status.value,
                bundle.content_hash,
                agent_run_bundle_to_json(bundle),
                NOW.isoformat(),
            ),
        )

    with pytest.raises(
        RuntimeError,
        match=r"^legacy agent evidence writer detected after normalized migration$",
    ):
        SQLiteStorage(str(database))

    with (
        sqlite3.connect(database) as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match=r"^legacy agent evidence writer detected after normalized migration$",
        ),
    ):
        connection.execute("UPDATE agent_run_bundles SET status='failed'")


def test_corrupt_a17_bundle_aborts_migration_without_marking_it_complete(
    tmp_path: Path,
) -> None:
    database = tmp_path / "verdict.db"
    bundle, _trace = _capture()
    payload = agent_run_bundle_to_json(bundle)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE agent_run_bundles (
                tenant_id TEXT NOT NULL, run_id TEXT NOT NULL,
                source_session_id TEXT NOT NULL, source_kind TEXT NOT NULL,
                started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
                content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, run_id)
            )"""
        )
        connection.execute(
            "INSERT INTO agent_run_bundles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bundle.run.tenant_id,
                bundle.run.run_id,
                bundle.session.source_session_id,
                bundle.session.source_kind,
                bundle.run.started_at.isoformat(),
                bundle.run.ended_at.isoformat(),
                bundle.run.status.value,
                "0" * 64,
                payload,
                NOW.isoformat(),
            ),
        )

    with pytest.raises(RuntimeError, match="content hash"):
        SQLiteStorage(str(database))

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT payload_json FROM agent_run_bundles").fetchone()[0]
            == payload
        )
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM verdict_schema_migrations").fetchone()[0] == 0
        )
