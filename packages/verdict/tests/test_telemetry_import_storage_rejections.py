"""Storage-time record rejection contract for the synchronous import runner.

A record the storage adapter rejects (``ValueError``: invalid content, size
limit, digest mismatch) is counted as a skip and the import continues, exactly
as the Voice adapter already counts a mapping-time snapshot rejection. Any
other storage failure still aborts with progress counters.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.conversations import validate_conversation
from verdict.storage.memory import InMemoryStorage
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.cli import main as import_main
from verdict.telemetry.model import ImportContext
from verdict.telemetry.runner import ImportRunError, import_into_storage
from verdict.telemetry.sources.voice import map_voice_conversation

# Digits of pi grouped by ten: 0.1.0a23 redacted this differently on every
# scan, so the snapshot digest computed at mapping time never matched the
# digest recomputed by storage and the whole import aborted at this record.
DIGIT_RUN = "1816334467 7522431712 1992458631 5030286182 9627117"


def _record(name: str, assistant_text: str = "first reply") -> dict:
    return {
        "conversation_id": name,
        "end_status": "complete",
        "ended_at": "2026-09-20T12:00:00Z",
        "turns": [
            {"speaker": "caller", "text": "hello"},
            {
                "speaker": "agent",
                "text": assistant_text,
                "status": "completed",
                "started_at": "2026-09-20T11:59:00Z",
                "ended_at": "2026-09-20T11:59:01Z",
            },
        ],
    }


def _context(tenant: str = "tenant-a") -> ImportContext:
    return ImportContext(adapter="file", source_scope="fixture", tenant_id=tenant)


@pytest.fixture(params=["memory", "sqlite"])
def storage(request, tmp_path):
    value = (
        InMemoryStorage()
        if request.param == "memory"
        else SQLiteStorage(str(tmp_path / "rejections.sqlite"))
    )
    try:
        yield value
    finally:
        value.close()


def test_digit_run_transcript_imports_and_stored_snapshot_is_its_own_fixed_point(storage):
    summary = import_into_storage(
        map_voice_conversation(_record("pi", f"digits: {DIGIT_RUN}"), _context()), storage
    )

    assert summary.conversations_stored == 1
    assert summary.stored == 1
    assert summary.skipped == 0
    rows, _cursor = storage.list_conversations("tenant-a")
    assert len(rows) == 1
    stored = {key: value for key, value in rows[0].items() if key != "retention_at"}
    assert validate_conversation(deepcopy(stored)) == stored
    content = stored["messages"][-1]["content"]
    assert content.startswith("digits: 1816334467 7522431712 1992458631 5030286")
    assert content.count("<PHONE>") == 1
    trace = storage.list_traces(tenant_id="tenant-a", limit=10)[0]
    assert trace.response_redacted == content


def test_storage_rejected_snapshot_is_counted_and_import_continues(storage):
    results = map_voice_conversation(_record("one"), _context())
    tampered = deepcopy(results[0].conversation)
    tampered["revision"] = "0" * 64
    results[0] = replace(results[0], conversation=tampered)
    results += map_voice_conversation(_record("two"), _context())
    clean = import_into_storage(map_voice_conversation(_record("two"), _context()), InMemoryStorage())

    summary = import_into_storage(results, storage)

    assert summary.skip_reasons == {"conversation_rejected": 1}
    assert summary.skipped == 1
    assert summary.conversations_stored == 1
    assert summary.stored == 2 * clean.stored
    # The rejected snapshot counts as one more seen unit that was skipped, the
    # same accounting the Voice adapter uses for a mapping-time rejection.
    assert summary.seen == summary.stored + summary.skipped
    rows, _cursor = storage.list_conversations("tenant-a")
    assert [row["id"] for row in rows] == [_context().trace_id("two")]
    assert len(storage.list_traces(tenant_id="tenant-a", limit=10)) == 2


def test_storage_rejected_trace_is_counted_and_import_continues(storage):
    results = map_voice_conversation(_record("one"), _context())
    results += map_voice_conversation(_record("two"), _context())
    rejected_id = results[0].trace.trace_id
    original_insert = storage.insert_trace

    def reject_one(trace):
        if trace.trace_id == rejected_id:
            raise ValueError("trace rejected by adapter validation")
        original_insert(trace)

    storage.insert_trace = reject_one

    summary = import_into_storage(results, storage)

    assert summary.skip_reasons == {"trace_rejected": 1}
    assert summary.stored == 1
    assert summary.conversations_stored == 2
    assert summary.seen == summary.stored + summary.skipped
    assert [t.trace_id for t in storage.list_traces(tenant_id="tenant-a", limit=10)] == [
        results[1].trace.trace_id
    ]


def test_non_record_storage_failures_still_abort(storage):
    results = map_voice_conversation(_record("one"), _context())

    def unavailable(_conversation):
        raise RuntimeError("database unavailable canary")

    storage.save_conversation = unavailable
    with pytest.raises(ImportRunError) as caught:
        import_into_storage(results, storage)

    assert caught.value.stage == "conversation_storage"
    assert caught.value.summary.seen == 1
    assert "canary" not in str(caught.value)


def test_live_postgres_digit_run_import_and_storage_rejection_skip():
    dsn, reason = validate_test_dsn(
        os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False
    )
    if not dsn:
        pytest.skip(reason)
    tenant = f"rejections-{uuid4().hex}"
    context = _context(tenant)
    with isolated_test_dsn(dsn) as isolated_dsn:
        storage = PostgresStorage(isolated_dsn, min_pool=1, max_pool=2)
        try:
            results = map_voice_conversation(_record("pi", f"digits: {DIGIT_RUN}"), context)
            tampered = map_voice_conversation(_record("bad"), context)
            conversation = deepcopy(tampered[0].conversation)
            conversation["revision"] = "0" * 64
            tampered[0] = replace(tampered[0], conversation=conversation)
            results += tampered + map_voice_conversation(_record("after"), context)

            summary = import_into_storage(results, storage)

            assert summary.skip_reasons == {"conversation_rejected": 1}
            assert summary.conversations_stored == 2
            assert summary.stored == 3
            assert summary.seen == summary.stored + summary.skipped
            rows, _cursor = storage.list_conversations(tenant)
            assert {row["id"] for row in rows} == {
                context.trace_id("pi"), context.trace_id("after")
            }
            pi_row = next(row for row in rows if row["id"] == context.trace_id("pi"))
            stored = {key: value for key, value in pi_row.items() if key != "retention_at"}
            assert validate_conversation(deepcopy(stored)) == stored
            assert pi_row["messages"][-1]["content"].count("<PHONE>") == 1
        finally:
            storage.close()


def test_cli_imports_digit_run_file_end_to_end(tmp_path, capsys):
    path = tmp_path / "voice.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                _record("before"),
                _record("pi", f"digits: {DIGIT_RUN}"),
                _record("after"),
            )
        )
        + "\n"
    )

    result = import_main(
        [
            "file",
            str(path),
            "--format",
            "voice",
            "--storage",
            f"sqlite:///{tmp_path / 'cli.sqlite'}",
            "--tenant-id",
            "tenant-a",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0, captured.err
    summary = json.loads(captured.out)
    assert summary["conversations_stored"] == 3
    assert summary["stored"] == 3
    assert summary["skipped"] == 0
