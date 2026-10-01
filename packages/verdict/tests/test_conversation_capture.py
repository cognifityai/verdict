"""Real Voice import and native conversation lifecycle contracts."""

from __future__ import annotations

import json
import os
import random
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.conversations import conversation_from_voice, validate_conversation
from verdict.storage.buffered import BufferedStorage
from verdict.storage.memory import InMemoryStorage
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.model import ImportContext
from verdict.telemetry.runner import ImportRunError, import_into_storage
from verdict.telemetry.sources.voice import map_voice_conversation


def _source(name: str = "one") -> dict:
    return {
        "conversation_id": name,
        "end_status": "complete",
        "ended_at": "2026-09-20T12:00:00Z",
        "turns": [
            {"speaker": "system", "text": "internal instruction"},
            {"speaker": "caller", "text": "hello"},
            {
                "speaker": "agent",
                "text": "first reply",
                "status": "completed",
                "started_at": "2026-09-20T11:59:00Z",
                "ended_at": "2026-09-20T11:59:01Z",
            },
            {"speaker": "agent", "text": "interrupted reply", "status": "interrupted"},
            {"speaker": "caller", "text": "again"},
            {
                "speaker": "agent",
                "text": "second reply",
                "status": "completed",
                "started_at": "2026-09-20T11:59:02Z",
                "ended_at": "2026-09-20T11:59:03Z",
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
        else SQLiteStorage(str(tmp_path / "db.sqlite"))
    )
    yield value
    value.close()


def test_voice_trace_projection_stays_published_while_conversation_is_captured(storage):
    mapped = map_voice_conversation(_source(), _context())
    traces = [item.trace for item in mapped if item.trace is not None]
    assert len(traces) == 2
    assert traces[0].raw_messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "first reply"},
    ]
    assert traces[1].raw_messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "second reply"},
    ]
    assert all("verdict.conversation_id" not in trace.tags for trace in traces)

    summary = import_into_storage(mapped, storage)
    assert summary.stored == 2
    assert summary.conversations_stored == 1
    rows, cursor = storage.list_conversations("tenant-a", limit=10)
    assert cursor is None
    assert len(rows) == 1
    assert [message["role"] for message in rows[0]["messages"]] == [
        "system",
        "user",
        "assistant",
        "assistant",
        "user",
        "assistant",
    ]
    assert rows[0]["end_status"] == "complete"


def test_explicit_voice_labels_are_bounded_and_part_of_snapshot_revision(storage):
    source = _source()
    source["labels"] = {"persona": "avatar_a", "group": "site_one"}
    import_into_storage(map_voice_conversation(source, _context()), storage)
    [original], _ = storage.list_conversations("tenant-a")
    assert original["labels"] == {"group": "site_one", "persona": "avatar_a"}
    import_into_storage(map_voice_conversation(source, _context()), storage)
    [same], _ = storage.list_conversations("tenant-a")
    assert same["revision"] == original["revision"]
    source["labels"]["group"] = "site_two"
    import_into_storage(map_voice_conversation(source, _context()), storage)
    [changed], _ = storage.list_conversations("tenant-a")
    assert changed["labels"]["group"] == "site_two"
    assert changed["revision"] != original["revision"]


def test_source_enabled_phases_are_stored_and_bound_to_snapshot_revision(storage):
    source = _source()
    source["enabled_phases"] = ["history", "intake"]
    import_into_storage(map_voice_conversation(source, _context()), storage)
    [original], _ = storage.list_conversations("tenant-a")
    assert original["enabled_phases"] == ["history", "intake"]
    source["enabled_phases"] = ["intake"]
    import_into_storage(map_voice_conversation(source, _context()), storage)
    [changed], _ = storage.list_conversations("tenant-a")
    assert changed["enabled_phases"] == ["intake"]
    assert changed["revision"] != original["revision"]


@pytest.mark.parametrize("labels", [
    {"Bad key": "one"}, {"group": ""}, {"group": "x" * 129},
    {f"key_{chr(97 + index)}": "value" for index in range(9)},
    {"patient_123456789": "one"},
])
def test_invalid_voice_labels_do_not_create_snapshot(storage, labels):
    source = _source()
    source["labels"] = labels
    mapped = map_voice_conversation(source, _context())
    assert any(item.skip_reason == "invalid_conversation_snapshot" for item in mapped)
    import_into_storage(mapped, storage)
    assert storage.list_conversations("tenant-a")[0] == []


def test_voice_label_value_is_redacted_before_storage(storage):
    source = _source()
    source["labels"] = {"group": "person@example.com"}
    import_into_storage(map_voice_conversation(source, _context()), storage)
    [row], _ = storage.list_conversations("tenant-a")
    assert "person@example.com" not in repr(row)
    assert "person@example.com" not in repr(storage.get_conversation("tenant-a", row["id"]))


def test_last_successful_reimport_sets_current_but_never_extends_retention(storage):
    source = _source()
    first = map_voice_conversation(source, _context())
    import_into_storage(first, storage)
    original = storage.list_conversations("tenant-a", limit=10)[0][0]
    modified = deepcopy(source)
    modified["turns"][-1]["text"] = "revised reply"
    modified["ended_at"] = "2026-09-21T12:00:00Z"
    import_into_storage(map_voice_conversation(modified, _context()), storage)
    updated = storage.list_conversations("tenant-a", limit=10)[0][0]
    assert updated["revision"] != original["revision"]
    assert updated["messages"][-1]["content"] == "revised reply"
    assert updated["retention_at"] == original["retention_at"]
    import_into_storage(map_voice_conversation(source, _context()), storage)
    restored = storage.list_conversations("tenant-a", limit=10)[0][0]
    assert restored["revision"] == original["revision"]
    assert restored["retention_at"] == original["retention_at"]


def test_concurrent_revisions_keep_one_current_row_and_earliest_retention(storage):
    older = _source()
    newer = deepcopy(older)
    newer["turns"][-1]["text"] = "newer answer"
    newer["ended_at"] = "2026-09-22T12:00:00Z"
    snapshots = [conversation_from_voice(source, _context()) for source in (older, newer)]
    assert all(snapshot is not None for snapshot in snapshots)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(storage.save_conversation, snapshots))
    rows, cursor = storage.list_conversations("tenant-a")
    assert cursor is None
    assert len(rows) == 1
    assert rows[0]["revision"] in {snapshot["revision"] for snapshot in snapshots}
    assert rows[0]["retention_at"] == "2026-09-20T12:00:00+00:00"


def test_retention_deletes_transcript_and_preserves_trace_count(storage):
    import_into_storage(map_voice_conversation(_source(), _context()), storage)
    assert storage.prune_before("2026-09-21T00:00:00+00:00") == 2
    assert storage.list_conversations("tenant-a", limit=10) == ([], None)


def test_conversation_retention_uses_exact_source_end_boundary(storage):
    import_into_storage(map_voice_conversation(_source(), _context()), storage)
    storage.prune_before("2026-09-20T12:00:00Z")
    assert len(storage.list_conversations("tenant-a")[0]) == 1
    storage.prune_before("2026-09-20T12:00:00.000001Z")
    assert storage.list_conversations("tenant-a") == ([], None)


def test_tenant_isolation_and_explicit_conversation_deletion(storage):
    for tenant in ("tenant-a", "tenant-b"):
        import_into_storage(map_voice_conversation(_source(), _context(tenant)), storage)
    a = storage.list_conversations("tenant-a", limit=10)[0][0]
    b = storage.list_conversations("tenant-b", limit=10)[0][0]
    assert a["id"] != b["id"]
    storage.delete_conversation("tenant-a", a["id"])
    assert storage.get_conversation("tenant-a", a["id"]) is None
    assert storage.get_conversation("tenant-b", b["id"]) is not None


def test_trace_deletion_does_not_pretend_to_erase_independent_conversation(storage):
    import_into_storage(map_voice_conversation(_source(), _context()), storage)
    trace = storage.list_traces(tenant_id="tenant-a", limit=10)[0]
    conversation = storage.list_conversations("tenant-a")[0][0]
    storage.delete_trace(trace.trace_id)
    assert storage.get_conversation("tenant-a", conversation["id"]) is not None
    storage.delete_conversation("tenant-a", conversation["id"])
    assert storage.get_conversation("tenant-a", conversation["id"]) is None


def test_default_local_tenant_captures_conversation(storage):
    context = ImportContext(adapter="file", source_scope="local-voice")
    import_into_storage(map_voice_conversation(_source(), context), storage)
    rows, cursor = storage.list_conversations("__verdict_local__")
    assert len(rows) == 1
    assert cursor is None


def test_malformed_and_truncated_transcripts_are_not_complete(storage):
    source = _source()
    source["turns"].append({"speaker": "unknown", "text": "ignored"})
    source["truncated"] = True
    import_into_storage(map_voice_conversation(source, _context()), storage)
    row = storage.list_conversations("tenant-a", limit=10)[0][0]
    assert row["end_status"] == "incomplete"
    assert "unsupported_turn" in row["input_issues"]
    assert "truncated_transcript" in row["input_issues"]


@pytest.mark.parametrize(
    "content",
    [pytest.param("a" * 60_000, id="ascii"), pytest.param("é" * 30_000, id="multibyte")],
)
def test_oversize_voice_snapshot_keeps_a_bounded_incomplete_prefix(storage, content):
    source = {
        "conversation_id": "long-voice",
        "end_status": "complete",
        "turns": [
            turn
            for _ in range(9)
            for turn in (
                {"speaker": "caller", "text": content},
                {"speaker": "agent", "text": "reply", "status": "completed", "started_at": "2026-09-20T11:59:00Z"},
            )
        ],
    }
    summary = import_into_storage(map_voice_conversation(source, _context()), storage)
    assert summary.stored == 9
    assert summary.conversations_stored == 1
    row = storage.list_conversations("tenant-a")[0][0]
    assert 1 <= len(row["messages"]) < 18
    assert row["end_status"] == "incomplete"
    assert "truncated_transcript" in row["input_issues"]
    assert len(json.dumps(row, ensure_ascii=False).encode("utf-8")) <= 512_000


def test_snapshot_budget_uses_redacted_output_size(storage):
    source = {
        "conversation_id": "redacted-length",
        "end_status": "complete",
        "turns": [
            *[{"speaker": "caller", "text": "a" * 60_000} for _ in range(8)],
            {"speaker": "caller", "text": "person@example.com " * 3_000},
        ],
    }
    import_into_storage(map_voice_conversation(source, _context()), storage)
    row = storage.list_conversations("tenant-a")[0][0]
    assert len(row["messages"]) == 9
    assert "truncated_transcript" not in row["input_issues"]
    assert "person@example.com" not in repr(row)


def test_many_malformed_turns_and_unknown_end_remain_reviewable(storage):
    source = _source()
    source["turns"].extend([{"speaker": "unknown", "text": "ignored"}] * 100)
    source["end_status"] = {"unexpected": True}
    import_into_storage(map_voice_conversation(source, _context()), storage)
    row = storage.list_conversations("tenant-a")[0][0]
    assert row["end_status"] == "incomplete"
    assert row["input_issues"] == ["unknown_end_status", "unsupported_turn"]


def test_voice_snapshot_parser_handles_json_shaped_variants():
    rng = random.Random(48)
    contents = ["hello", "person@example.com", "a\x00b", "x" * 70_000, None, [], {"text": "x"}]
    endings = ["complete", "completed", "open", "mystery", [], {"bad": True}]
    speakers = ["caller", "agent", "system", "unknown", None]
    for index in range(200):
        record = {
            "conversation_id": f"fuzz-{index}",
            "end_status": rng.choice(endings),
            "turns": [
                {"speaker": "caller", "text": "valid"},
                {"speaker": rng.choice(speakers), "text": rng.choice(contents)},
            ],
        }
        captured = conversation_from_voice(record, _context())
        assert captured is not None
        assert validate_conversation(captured) == captured
        assert "person@example.com" not in repr(captured)
        assert "unknown_payload" not in repr(captured)


def test_pagination_continues_past_5000_lifetime_records_without_tenant_leak(storage):
    for index in range(5001):
        source = {
            "conversation_id": f"voice-{index}",
            "turns": [{"speaker": "caller", "text": "hello"}],
        }
        import_into_storage(map_voice_conversation(source, _context()), storage)
    seen = set()
    cursor = None
    while True:
        rows, cursor = storage.list_conversations("tenant-a", after=cursor, limit=19)
        seen.update(row["id"] for row in rows)
        if cursor is None:
            break
    assert len(seen) == 5001
    assert storage.list_conversations("tenant-b", limit=10) == ([], None)
    with pytest.raises(ValueError, match="limit"):
        storage.list_conversations("tenant-a", limit=21)


def test_missing_source_time_uses_first_import_time_without_extension(storage):
    source = _source()
    source.pop("ended_at")
    before = datetime.now(timezone.utc)
    import_into_storage(map_voice_conversation(source, _context()), storage)
    row = storage.list_conversations("tenant-a")[0][0]
    first_retention = row["retention_at"]
    assert before <= datetime.fromisoformat(first_retention) <= datetime.now(timezone.utc)
    import_into_storage(map_voice_conversation(source, _context()), storage)
    assert storage.list_conversations("tenant-a")[0][0]["retention_at"] == first_retention


def test_redaction_and_allowlist_reach_storage_boundary(storage):
    source = _source()
    source["turns"][1]["text"] = "Contact person@example.com; SSN 123-45-6789"
    source["turns"][1]["audio_base64"] = "never-store-audio-canary"
    source["secret_metadata"] = "never-store-metadata-canary"
    import_into_storage(map_voice_conversation(source, _context()), storage)
    row = storage.list_conversations("tenant-a")[0][0]
    assert "person@example.com" not in repr(row)
    assert "123-45-6789" not in repr(row)
    assert "never-store-audio-canary" not in repr(row)
    assert "never-store-metadata-canary" not in repr(row)
    injected = deepcopy(row)
    injected["input_issues"] = ["person@example.com"]
    with pytest.raises(ValueError, match="input issues"):
        storage.save_conversation(injected)


def test_sqlite_files_do_not_contain_voice_privacy_canaries(tmp_path):
    path = tmp_path / "privacy.sqlite"
    source = _source()
    source["turns"][1]["text"] = "person@example.com and SSN 123-45-6789"
    source["turns"][1]["audio_base64"] = "audio-canary-no-store"
    source["private_metadata"] = "metadata-canary-no-store"
    storage = SQLiteStorage(str(path))
    try:
        import_into_storage(map_voice_conversation(source, _context()), storage)
    finally:
        storage.close()
    stored = b"".join(file.read_bytes() for file in tmp_path.glob("privacy.sqlite*"))
    for canary in (b"person@example.com", b"123-45-6789", b"audio-canary-no-store", b"metadata-canary-no-store"):
        assert canary not in stored


def test_partial_trace_write_is_reported_and_retry_is_idempotent(storage):
    mapped = map_voice_conversation(_source(), _context())
    original_insert = storage.insert_trace

    def fail_once(trace):
        storage.insert_trace = original_insert
        raise OSError("injected write failure")

    storage.insert_trace = fail_once
    with pytest.raises(ImportRunError) as caught:
        import_into_storage(mapped, storage)
    assert caught.value.stage == "storage"
    assert caught.value.summary.conversations_stored == 1
    assert caught.value.summary.stored == 0
    import_into_storage(map_voice_conversation(_source(), _context()), storage)
    assert len(storage.list_conversations("tenant-a")[0]) == 1
    assert len(storage.list_traces(tenant_id="tenant-a", limit=10)) == 2


def test_buffered_import_is_rejected_before_source_is_consumed(tmp_path):
    inner = SQLiteStorage(str(tmp_path / "buffered.db"))
    storage = BufferedStorage(inner)
    consumed = []

    def records():
        consumed.append(True)
        yield from map_voice_conversation(_source(), _context())

    try:
        with pytest.raises(ImportRunError) as caught:
            import_into_storage(records(), storage)
        assert caught.value.stage == "requires_direct_storage"
        assert caught.value.summary.seen == 0
        assert consumed == []
        assert storage.list_conversations("tenant-a") == ([], None)
    finally:
        storage.close()


def test_buffered_adapter_can_silently_fail_after_trace_enqueue(tmp_path):
    inner = SQLiteStorage(str(tmp_path / "buffered-failure.db"))
    storage = BufferedStorage(inner)
    trace = next(item.trace for item in map_voice_conversation(_source(), _context()) if item.trace)
    def fail_write(_trace):
        raise OSError("injected durable write failure")
    inner.insert_trace = fail_write
    try:
        storage.insert_trace(trace)
        storage.flush()
        assert storage.write_errors == 1
        assert storage.get_trace(trace.trace_id) is None
    finally:
        storage.close()


def test_buffered_conversation_storage_lifecycle_is_synchronous(tmp_path):
    inner = SQLiteStorage(str(tmp_path / "buffered-conversation.db"))
    storage = BufferedStorage(inner)
    snapshot = conversation_from_voice(_source(), _context())
    assert snapshot is not None
    try:
        storage.save_conversation(snapshot)
        assert storage.get_conversation("tenant-a", snapshot["id"]) is not None
        storage.prune_before("2026-09-21T00:00:00+00:00")
        assert storage.get_conversation("tenant-a", snapshot["id"]) is None
    finally:
        storage.close()


def test_live_postgres_conversation_lifecycle():
    dsn, reason = validate_test_dsn(
        os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False
    )
    if not dsn:
        pytest.skip(reason)
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    tenant = f"conversation-{uuid4().hex}"
    with isolated_test_dsn(dsn) as isolated_dsn:
        options = conninfo_to_dict(isolated_dsn)["options"]
        non_utc_dsn = make_conninfo(
            isolated_dsn, options=f"{options} -cTimeZone=America/Los_Angeles"
        )
        storage = PostgresStorage(non_utc_dsn, min_pool=1, max_pool=2)
        try:
            assert storage._fetchone("SHOW TIME ZONE", ())[0] == "America/Los_Angeles"
            context = _context(tenant)
            for name in ("first", "second", "third"):
                import_into_storage(map_voice_conversation(_source(name), context), storage)
            first_page, cursor = storage.list_conversations(tenant, limit=2)
            second_page, end = storage.list_conversations(tenant, after=cursor, limit=2)
            assert len(first_page) == 2
            assert len(second_page) == 1
            assert end is None
            assert {row["id"] for row in first_page + second_page} == {
                context.trace_id(name) for name in ("first", "second", "third")
            }
            assert all(row["retention_at"] == "2026-09-20T12:00:00+00:00"
                       for row in first_page + second_page)
            older = conversation_from_voice(_source("parallel"), context)
            changed = _source("parallel")
            changed["turns"][-1]["text"] = "parallel revision"
            changed["ended_at"] = "2026-09-22T12:00:00Z"
            newer = conversation_from_voice(changed, context)
            assert older is not None and newer is not None
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(storage.save_conversation, (older, newer)))
            parallel = storage.get_conversation(tenant, older["id"])
            assert parallel is not None
            assert parallel["revision"] in {older["revision"], newer["revision"]}
            assert parallel["retention_at"] == "2026-09-20T12:00:00+00:00"
            storage.delete_conversation(tenant, context.trace_id("first"))
            assert storage.get_conversation(tenant, context.trace_id("first")) is None
            assert storage.prune_before("2026-09-21T00:00:00+00:00") == 6
            assert storage.list_conversations(tenant) == ([], None)
            untimed = _source("untimed")
            untimed.pop("ended_at")
            before = datetime.now(timezone.utc)
            import_into_storage(map_voice_conversation(untimed, context), storage)
            fallback = storage.get_conversation(tenant, context.trace_id("untimed"))
            assert fallback is not None
            assert fallback["retention_at"].endswith("+00:00")
            assert before <= datetime.fromisoformat(fallback["retention_at"]) <= datetime.now(timezone.utc)
            assert storage.list_conversations(tenant)[0][0]["retention_at"] == fallback["retention_at"]
        finally:
            storage.close()
