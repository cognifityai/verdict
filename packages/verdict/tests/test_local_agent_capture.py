from __future__ import annotations

import json
import os
import sqlite3
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import pytest
import verdict.evidence as evidence_contract
from fastapi.testclient import TestClient
from verdict import AgentEventType, EvidenceState, ExecutionStatus, PrivacyClassification
from verdict.agent_judgment import AgentTurnJudgment, turn_evidence_fingerprint
from verdict.capture import AgentCaptureService
from verdict.dashboard.app import (
    build_agent_run_detail,
    build_agent_runs_bundle,
    build_agent_turn_analysis,
    create_app,
)
from verdict.evidence import AgentEvent
from verdict.schema import JudgmentStatus
from verdict.storage import BufferedStorage, InMemoryStorage, SQLiteStorage
from verdict.telemetry.cli import main
from verdict.telemetry.local_agents import capture_local_agents


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_local_capture_summary_preserves_published_positional_fields() -> None:
    from verdict.telemetry.local_agents import LocalCaptureSummary

    old = LocalCaptureSummary(1, 1, 0, 1, True)
    assert old.as_dict()["deferred"] == 1
    assert old.as_dict()["scan_incomplete"] is True


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_cancel_between_files_does_not_claim_the_next_file(
    tmp_path: Path, backend: str,
) -> None:
    from verdict.telemetry.local_agents import CaptureCancelled

    root = tmp_path / "codex"
    for name in ("a", "b"):
        records = _codex_records()
        records[0]["payload"]["id"] = f"session-{name}"
        _write_jsonl(root / f"{name}.jsonl", records)
    cancelled = False

    def progress(done: int, _total: int) -> None:
        nonlocal cancelled
        if done == 1:
            cancelled = True

    with ExitStack() as stack:
        if backend == "postgres":
            from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
            from verdict.storage import PostgresStorage

            dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                            allow_any_database=False)
            if dsn is None:
                pytest.skip(reason)
            storage_url = stack.enter_context(isolated_test_dsn(dsn))
            new_storage = PostgresStorage
        else:
            storage_url = str(tmp_path / "verdict.db")
            new_storage = SQLiteStorage
        storage = new_storage(storage_url)
        try:
            with pytest.raises(CaptureCancelled) as stopped:
                capture_local_agents(storage, tenant_id="local", codex_root=root,
                                     progress=progress, should_cancel=lambda: cancelled)
            assert stopped.value.summary.files == 1
        finally:
            storage.close()
        reopened = new_storage(storage_url)
        try:
            rows = reopened.local_scan_attempt_summary("local")
            assert {code: count for code, count, _started in rows} == {
                "complete_projection": 1,
            }
            assert len(reopened.list_agent_run_bundles("local")) == 1
        finally:
            reopened.close()


def test_history_beyond_verification_bound_is_explicitly_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    path = root / "session.jsonl"
    _write_jsonl(path, _codex_records())
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_VERIFY_BYTES",
                        path.stat().st_size + 16, raising=False)
    with path.open("ab") as handle:
        handle.write(b"\n" * 32)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        result = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert result.skip_reasons == {"history_file_too_large": 1}
        assert storage.list_agent_run_bundles("local") == []
    finally:
        storage.close()


def test_history_at_verification_bound_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    path = root / "session.jsonl"
    _write_jsonl(path, _codex_records())
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_VERIFY_BYTES", path.stat().st_size,
                        raising=False)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        result = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert result.stored == 1 and result.skipped == 0
    finally:
        storage.close()


def test_history_growth_after_stat_cannot_escape_verification_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    path = root / "session.jsonl"
    _write_jsonl(path, _codex_records())
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_VERIFY_BYTES",
                        path.stat().st_size + 1, raising=False)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    claim = storage.claim_local_scan_attempt

    def grow_after_claim(*args, **kwargs):
        result = claim(*args, **kwargs)
        with path.open("ab") as handle:
            handle.write(b"\n\n")
        return result

    monkeypatch.setattr(storage, "claim_local_scan_attempt", grow_after_claim)
    try:
        result = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert result.skip_reasons == {"history_file_too_large": 1}
        assert storage.list_agent_run_bundles("local") == []
    finally:
        storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_over_verification_bound_retains_previous_evidence_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str,
) -> None:
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    originals = {}
    for name in ("a", "b"):
        records = _codex_records()
        records[0]["payload"]["id"] = f"session-{name}"
        path = root / f"{name}.jsonl"
        _write_jsonl(path, records)
        originals[name] = path.read_bytes()
    cap = max(len(value) for value in originals.values()) + 16
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_VERIFY_BYTES", cap)
    with ExitStack() as stack:
        if backend == "postgres":
            from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
            from verdict.storage import PostgresStorage

            dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                            allow_any_database=False)
            if dsn is None:
                pytest.skip(reason)
            storage = PostgresStorage(stack.enter_context(isolated_test_dsn(dsn)))
        else:
            storage = SQLiteStorage(str(tmp_path / "verdict.db"))
        stack.callback(storage.close)
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 2
        before = {
            bundle.run.session_id: storage.get_agent_capture_snapshot(
                "local", bundle.run.run_id,
            )[1]
            for bundle in storage.list_agent_run_bundles("local")
        }
        with (root / "a.jsonl").open("ab") as handle:
            handle.write(b"\n" * 32)
        partial = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert partial.stored == 1 and partial.skip_reasons == {"history_file_too_large": 1}
        after = {
            bundle.run.session_id: storage.get_agent_capture_snapshot(
                "local", bundle.run.run_id,
            )[1]
            for bundle in storage.list_agent_run_bundles("local")
        }
        assert after == before
        (root / "a.jsonl").write_bytes(originals["a"])
        retried = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert retried.stored == 2 and retried.skipped == 0


def test_history_over_per_file_parse_limit_keeps_bounded_partial_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    path = root / "session.jsonl"
    _write_jsonl(path, _codex_records())
    parsed_bytes = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b"\n" * 256)
        for row in (
            {"timestamp": "2026-08-30T10:02:00Z", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "turn-b"}},
            {"timestamp": "2026-08-30T10:02:01Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "second synthetic turn"}},
            {"timestamp": "2026-08-30T10:02:02Z", "type": "event_msg",
             "payload": {"type": "task_complete", "turn_id": "turn-b",
                         "last_agent_message": "done"}},
        ):
            handle.write((json.dumps(row) + "\n").encode())
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", parsed_bytes + 16)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.stored == 1 and summary.skipped == 0
        assert summary.as_dict()["partial_reasons"] == {"bounded_source_prefix": 1}
        [bundle] = storage.list_agent_run_bundles("local")
        _, revision = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
        assert revision is not None
        assert revision.full_bytes == path.stat().st_size
        assert revision.complete_prefix_bytes < revision.full_bytes
        assert revision.partial_final_line
        assert revision.full_sha256 == sha256(path.read_bytes()).hexdigest()
        repeated = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert repeated.stored == 1 and repeated.skipped == 0
        with path.open("ab") as handle:
            handle.write(b"\n" * 32)
        appended = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert appended.stored == 1 and appended.skipped == 0
        assert len(storage.list_agent_run_bundles("local")) == 1
        monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", path.stat().st_size + 1)
        completed = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert completed.stored == 1 and completed.skipped == 0
        [updated] = storage.list_agent_run_bundles("local")
        assert len(updated.turns) == 2
        _, complete_revision = storage.get_agent_capture_snapshot("local", updated.run.run_id)
        assert complete_revision is not None and not complete_revision.partial_final_line
    finally:
        storage.close()


@pytest.mark.parametrize("source_kind", ["claude", "codex"])
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_local_tool_result_sensitive_canaries_do_not_reach_storage_or_run_api(
    tmp_path: Path, source_kind: str, backend: str,
) -> None:
    card = "4111111111111111"
    key_body = "ZmFrZS1wZW0tYm9keQ=="
    provider_key = "AKIAIOSFODNN7EXAMPLE"
    glued_provider_key = "sk-ant-" + "A" * 24
    glued_github_key = "ghp_" + "B" * 24
    output = json.dumps({
        "card": int(card),
        "private_key": f"-----BEGIN PRIVATE KEY-----\n{key_body}\n-----END PRIVATE KEY-----",
        "db_url": "postgres://user:syntheticP%40ss@db.example.invalid/test",
        "provider_key": f"key_{provider_key}_suffix",
        "glued_provider": f"555-123-4567{glued_provider_key}",
        "glued_github": f"10.0.0.1{glued_github_key}",
        "reverse_glued_github": f"{glued_github_key}192.168.1.42",
        "reverse_grouped_card": f"{glued_github_key}3782 822463 10005",
        "card_with_status": "41111111 11111111 200 OK",
        "fixed_key_card_with_status": f"{'AKIA' + 'A' * 12 + '4111'}41111111 11111111 200 OK",
        "ordinary_status": f"{glued_github_key} 200 OK",
    })
    root = tmp_path / source_kind
    if source_kind == "claude":
        records = _claude_records()
        tool_result = next(row for row in records if row.get("sourceToolAssistantUUID"))
        tool_result["message"]["content"][0]["content"] = output
    else:
        records = _codex_records()
        tool_result = next(row for row in records if row.get("type") == "response_item"
                           and row.get("payload", {}).get("type") == "function_call_output")
        tool_result["payload"]["output"] = json.dumps({"exit_code": 1, "output": output})
    _write_jsonl(root / "session.jsonl", records)
    with ExitStack() as stack:
        if backend == "postgres":
            from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
            from verdict.storage import PostgresStorage

            dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                            allow_any_database=False)
            if dsn is None:
                pytest.skip(reason)
            storage_url = stack.enter_context(isolated_test_dsn(dsn))
            storage = PostgresStorage(storage_url)
        else:
            database = tmp_path / "verdict.db"
            storage_url = str(database)
            storage = SQLiteStorage(storage_url)
        stack.callback(storage.close)
        summary = capture_local_agents(storage, tenant_id="local", **{
            "claude_root" if source_kind == "claude" else "codex_root": root,
        })
        assert summary.stored == 1 and summary.skipped == 0
        [bundle] = storage.list_agent_run_bundles("local")
        with TestClient(create_app(storage=storage_url, tenant_id="local")) as client:
            response = client.get(f"/api/runs/{bundle.run.run_id}")
            session = client.get("/api/session", params={
                "source_kind": bundle.session.source_kind,
                "session_id": bundle.run.session_id,
            })
        assert response.status_code == 200
        assert session.status_code == 200
        for canary in (card, key_body, "syntheticP", provider_key,
                       glued_provider_key, glued_github_key):
            assert canary not in repr(bundle)
            assert canary not in response.text
            assert canary not in session.text
            if backend == "sqlite":
                assert canary.encode() not in database.read_bytes()
        assert ".168.1.42" not in repr(bundle)
        assert ".168.1.42" not in response.text
        assert ".168.1.42" not in session.text
        assert "822463 10005" not in repr(bundle)
        assert "822463 10005" not in response.text
        assert "822463 10005" not in session.text
        assert "41111111 11111111" not in repr(bundle)
        assert "41111111 11111111" not in response.text
        assert "41111111 11111111" not in session.text
        assert "200 OK" in response.text
        if backend == "sqlite":
            assert b".168.1.42" not in database.read_bytes()


def test_legacy_source_revision_without_counts_cannot_prove_whole_run_coverage() -> None:
    from verdict.local_source_revision import LocalSourceRevision, local_scope_partial

    revision = LocalSourceRevision(
        full_bytes=12, full_sha256="a" * 64,
        complete_prefix_bytes=12, complete_prefix_sha256="a" * 64,
        partial_final_line=False,
    )
    assert local_scope_partial("codex", revision, 0, None) is True
    assert local_scope_partial("claude-code", revision, 0, 0) is True


def test_recent_trace_fingerprint_uses_instant_not_display_timezone() -> None:
    from datetime import timedelta, timezone

    from verdict.local_recent_facts import trace_evidence_fingerprint
    from verdict.schema import Operation, Trace

    observed = datetime.fromisoformat("2026-08-30T11:00:02+00:00")
    trace = Trace(
        trace_id="synthetic-timezone-trace", started_at=observed,
        ended_at=observed, provider="synthetic", operation=Operation.CHAT,
        request_model="synthetic-model", response_model="synthetic-model",
    )
    local_zone = timezone(timedelta(hours=-7))
    same_instant = replace(
        trace, started_at=observed.astimezone(local_zone),
        ended_at=observed.astimezone(local_zone),
    )
    assert trace_evidence_fingerprint(trace) == trace_evidence_fingerprint(same_instant)


def _assert_legacy_writer_cannot_replace_atomic_window(storage, path: Path, tenant: str, run_id: str) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from hashlib import sha256
    from threading import Barrier

    original = path.read_bytes()
    old_sample = storage.get_recent_local_events(tenant, run_id)
    path.write_bytes(original + b"\n")
    advanced = path.read_bytes()
    new_sample = {**old_sample, "sourceRevision": sha256(advanced).hexdigest()}
    barrier = Barrier(2)

    def save(sample, source):
        barrier.wait()
        try:
            return storage.save_recent_local_events(
                tenant, run_id, json.dumps(sample, separators=(",", ":")),
                len(source), sha256(source).hexdigest(),
                lambda size: sha256(path.read_bytes()[:size]).hexdigest(),
            )
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(save, old_sample, original)
        new = executor.submit(save, new_sample, advanced)
        assert new.result() == "recent_sample_superseded"
        assert old.result() == "recent_sample_superseded"
    assert storage.get_recent_local_events(tenant, run_id) == old_sample


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


def _codex_item_message_records(content: object) -> list[dict[str, object]]:
    records = _codex_records()
    records[3] = {
        "timestamp": "2026-08-30T10:01:02Z",
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {"id": "user-1", "type": "UserMessage", "content": content},
        },
    }
    return records


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


def test_current_codex_user_message_reaches_redacted_turn_and_detail(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    content = [
        {"type": "text", "text": (
            "<environment_context>ignore me</environment_context>\n"
            "## My request:\nreview alice@example.com"
        ), "text_elements": []},
        {"type": "local_image", "path": "/tmp/synthetic-image.png"},
        {"type": "text", "text": {"unexpected": "NESTED_INVALID_TEXT"}},
        {"type": "text", "text": "then summarize", "text_elements": [
            {"text": "NESTED_CANARY_NOT_CONTENT"}
        ]},
    ]
    records = _codex_item_message_records(content)
    records.insert(1, {
        "timestamp": "2026-08-30T10:00:01Z", "type": "event_msg",
        "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "content": [{"type": "text", "text": "outside turn"}]
        }},
    })
    records.insert(5, {
        "timestamp": "2026-08-30T10:01:02Z", "type": "event_msg",
        "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "content": [{"type": "text", "text": "second request"}]
        }},
    })
    _write_jsonl(root / "session.jsonl", records)
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))

    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)

    assert summary.stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    [turn] = bundle.turns
    assert turn.request_state is EvidenceState.PRESENT
    assert turn.user_request_redacted == "review <EMAIL>\nthen summarize\n\nsecond request"
    assert turn.final_response_redacted == "fixed"
    detail = build_agent_run_detail(path, tenant="local", run_id=bundle.run.run_id)
    assert detail["turns"][0]["requestState"] == "present"
    assert detail["turns"][0]["request"] == turn.user_request_redacted
    assert "alice@example.com" not in repr(bundle) + repr(detail)
    assert "/tmp/synthetic-image.png" not in repr(bundle) + repr(detail)
    assert "NESTED_CANARY_NOT_CONTENT" not in repr(bundle) + repr(detail)
    assert "NESTED_INVALID_TEXT" not in repr(bundle) + repr(detail)
    assert storage.list_agent_run_bundles("other") == []
    storage.close()


def test_codex_escaped_prose_keeps_useful_request_after_storage(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[3]["payload"]["message"] = (
        r"Review https://example.com/pr/1 now, caf\u00e9"
    )
    _write_jsonl(root / "session.jsonl", records)
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        assert bundle.turns[0].user_request_redacted == "Review <URL> now, café"
        detail = build_agent_run_detail(path, tenant="local", run_id=bundle.run.run_id)
        assert detail["turns"][0]["request"] == bundle.turns[0].user_request_redacted
    finally:
        storage.close()


def test_codex_user_message_rescan_completes_missing_request(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    records = _codex_item_message_records([{"type": "text", "text": "current request"}])
    _write_jsonl(root / "session.jsonl", [row for row in records
                                        if row.get("payload", {}).get("type") != "item_completed"])
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [before] = storage.list_agent_run_bundles("local")[0].turns
    assert before.request_state is EvidenceState.MISSING

    _write_jsonl(root / "session.jsonl", records)
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [after] = storage.list_agent_run_bundles("local")[0].turns
    assert after.turn_id == before.turn_id
    assert after.request_state is EvidenceState.PRESENT
    assert after.user_request_redacted == "current request"
    storage.close()


@pytest.mark.parametrize("content", [None, [], "untyped synthetic request",
                                     {"type": "text", "text": "ignored"},
                                     [{"type": "local_image", "path": "/tmp/synthetic-image.png"}],
                                     [{"type": "text", "text": 42}]])
def test_codex_user_message_without_text_stays_missing(tmp_path: Path, content: object) -> None:
    root = tmp_path / "codex"
    _write_jsonl(root / "session.jsonl", _codex_item_message_records(content))
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    [turn] = bundle.turns
    assert turn.request_state is EvidenceState.MISSING
    assert turn.user_request_redacted is None
    detail = build_agent_run_detail(path, tenant="local", run_id=bundle.run.run_id)
    assert detail["turns"][0]["requestState"] == "missing"
    assert detail["turns"][0]["request"] is None
    storage.close()


@pytest.mark.parametrize("item", [None, "malformed", [],
                                  {"type": "Other", "content": [{"type": "text", "text": "no"}]}])
def test_codex_unknown_completed_items_do_not_become_requests(
    tmp_path: Path, item: object,
) -> None:
    root = tmp_path / "codex"
    records = _codex_item_message_records([{"type": "text", "text": "current request"}])
    records[3]["payload"]["item"] = item
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [turn] = storage.list_agent_run_bundles("local")[0].turns
    assert turn.request_state is EvidenceState.MISSING
    storage.close()


def test_codex_current_message_honors_content_off_and_truncation(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    records = _codex_item_message_records([{"type": "text", "text": "é" * 33_000}])
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root,
                                capture_content=False).stored == 1
    [off] = storage.list_agent_run_bundles("local")[0].turns
    assert off.request_state is EvidenceState.NOT_CAPTURED
    assert off.user_request_redacted is None
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [on] = storage.list_agent_run_bundles("local")[0].turns
    assert on.request_state is EvidenceState.PRESENT
    assert on.request_truncated is True
    assert len(on.user_request_redacted.encode("utf-8")) <= 65_536
    storage.close()


def test_codex_legacy_and_current_request_records_both_remain_observable(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    records = _codex_records(secret="legacy request")
    records.insert(4, {
        "timestamp": "2026-08-30T10:01:02Z", "type": "event_msg",
        "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "content": [{"type": "text", "text": "follow-up"}]
        }},
    })
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [turn] = storage.list_agent_run_bundles("local")[0].turns
    assert turn.user_request_redacted == "fix the test legacy request\n\nfollow-up"
    storage.close()


def test_codex_stale_first_snapshot_is_not_attributed_to_the_next_turn(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
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
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 7,
                            "cached_input_tokens": 2,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 1,
                            "total_tokens": 10,
                        },
                        "total_token_usage": {
                            "input_tokens": 127,
                            "cached_input_tokens": 50,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 27,
                            "reasoning_output_tokens": 13,
                            "total_tokens": 154,
                        },
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    [bundle] = storage.list_agent_run_bundles("local")
    assert bundle.turns[0].total_tokens == 24
    assert bundle.turns[1].input_tokens == 7
    assert bundle.turns[1].cached_input_tokens == 2
    assert bundle.turns[1].output_tokens == 3
    assert bundle.turns[1].reasoning_output_tokens == 1
    assert bundle.turns[1].total_tokens == 10


def test_codex_keeps_safe_cumulative_boundary_after_malformed_turn_usage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[-2]["payload"]["info"]["last_token_usage"]["output_tokens"] = -1
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 20,
                            "output_tokens": 4,
                            "total_tokens": 24,
                        },
                        "total_token_usage": {
                            "input_tokens": 120,
                            "output_tokens": 24,
                            "total_tokens": 144,
                        },
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 7,
                            "output_tokens": 3,
                            "total_tokens": 10,
                        },
                        "total_token_usage": {
                            "input_tokens": 127,
                            "output_tokens": 27,
                            "total_tokens": 154,
                        },
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    [bundle] = storage.list_agent_run_bundles("local")
    assert bundle.turns[0].token_usage_basis is None
    assert bundle.turns[1].input_tokens == 7
    assert bundle.turns[1].output_tokens == 3
    assert bundle.turns[1].total_tokens == 10


def test_codex_leaves_next_usage_unavailable_without_a_safe_prior_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[-2]["payload"]["info"]["total_token_usage"]["total_tokens"] = -1
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 20, "total_tokens": 24},
                        "total_token_usage": {"input_tokens": 120, "total_tokens": 144},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    [bundle] = storage.list_agent_run_bundles("local")
    assert bundle.turns[0].token_usage_basis is None
    assert bundle.turns[1].token_usage_basis is None
    assert bundle.turns[1].input_tokens is None
    assert bundle.turns[1].total_tokens is None


def test_codex_missing_latest_boundary_only_attributes_later_observed_growth(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records.insert(
        -1,
        {
            "timestamp": "2026-08-30T10:01:06.500000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"total_tokens": 24}},
            },
        },
    )
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 20},
                        "total_token_usage": {"total_tokens": 174},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 184},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    turns = storage.list_agent_run_bundles("local")[0].turns
    assert turns[0].total_tokens == 24
    assert turns[1].total_tokens == 10


def test_codex_valid_boundary_recovers_after_a_malformed_observation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records.insert(
        -1,
        {
            "timestamp": "2026-08-30T10:01:06.400000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 10},
                    "total_token_usage": {"total_tokens": -1},
                },
            },
        },
    )
    records.insert(
        -1,
        {
            "timestamp": "2026-08-30T10:01:06.500000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 10},
                    "total_token_usage": {"total_tokens": 154},
                },
            },
        },
    )
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 154},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 164},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    turns = storage.list_agent_run_bundles("local")[0].turns
    assert turns[0].total_tokens is None
    assert turns[1].total_tokens == 10


def test_codex_decreasing_total_does_not_become_the_next_turn_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records.insert(
        -1,
        {
            "timestamp": "2026-08-30T10:01:06.500000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 10},
                    "total_token_usage": {"total_tokens": 134},
                },
            },
        },
    )
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 134},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    turns = storage.list_agent_run_bundles("local")[0].turns
    assert turns[0].total_tokens is None
    assert turns[1].total_tokens is None


def test_codex_boundary_recovers_after_a_decrease_then_valid_observation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    for timestamp, total in (
        ("2026-08-30T10:01:06.400000Z", 134),
        ("2026-08-30T10:01:06.500000Z", 144),
    ):
        records.insert(
            -1,
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": total},
                    },
                },
            },
        )
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 144},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 154},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    turns = storage.list_agent_run_bundles("local")[0].turns
    assert turns[0].total_tokens is None
    assert turns[1].total_tokens == 10


def test_codex_detects_a_decrease_after_an_omitted_cumulative_field(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[-2]["payload"]["info"] = {
        "last_token_usage": {"total_tokens": 10},
        "total_token_usage": {"total_tokens": 100},
    }
    records.insert(
        -1,
        {
            "timestamp": "2026-08-30T10:01:06.400000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 1},
                    "total_token_usage": {"input_tokens": 101},
                },
            },
        },
    )
    records.insert(
        -1,
        {
            "timestamp": "2026-08-30T10:01:06.500000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 10},
                    "total_token_usage": {"total_tokens": 90},
                },
            },
        },
    )
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 90},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    turns = storage.list_agent_run_bundles("local")[0].turns
    assert turns[0].token_usage_basis is None
    assert turns[0].total_tokens is None
    assert turns[1].token_usage_basis is None
    assert turns[1].total_tokens is None


def test_codex_keeps_non_decreasing_fields_across_partial_boundaries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[-2]["payload"]["info"] = {
        "last_token_usage": {"input_tokens": 10, "total_tokens": 10},
        "total_token_usage": {"input_tokens": 100, "total_tokens": 100},
    }
    records.insert(
        -1,
        {
            "timestamp": "2026-08-30T10:01:06.400000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 10},
                    "total_token_usage": {"input_tokens": 110},
                },
            },
        },
    )
    records.insert(
        -1,
        {
            "timestamp": "2026-08-30T10:01:06.500000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 10},
                    "total_token_usage": {"total_tokens": 110},
                },
            },
        },
    )
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 10,
                            "total_tokens": 10,
                        },
                        "total_token_usage": {
                            "input_tokens": 120,
                            "total_tokens": 120,
                        },
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    turns = storage.list_agent_run_bundles("local")[0].turns
    assert turns[0].total_tokens == 20
    assert turns[1].input_tokens == 10
    assert turns[1].total_tokens == 10


def test_codex_usage_between_turns_is_not_charged_to_the_next_turn(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 174},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 184},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    [bundle] = storage.list_agent_run_bundles("local")
    assert bundle.turns[1].total_tokens == 20


def test_codex_counter_reset_between_turns_uses_current_turn_snapshots(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records.extend(
        [
            {
                "timestamp": "2026-08-30T10:02:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "timestamp": "2026-08-30T10:02:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 10},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 10},
                        "total_token_usage": {"total_tokens": 20},
                    },
                },
            },
            {
                "timestamp": "2026-08-30T10:02:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "done",
                },
            },
        ]
    )
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    [bundle] = storage.list_agent_run_bundles("local")
    assert bundle.turns[1].total_tokens == 20


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


def test_claude_rescan_upgrades_legacy_long_trace_prefixes(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    records = _claude_records()
    records[0]["message"]["content"] = "p" * 2_000
    for record in records:
        if record.get("uuid") in {"assistant-1-text", "assistant-2-text"}:
            record["message"]["content"][0]["text"] = "r" * 2_000
    _write_jsonl(root / "session.jsonl", records)

    seed = SQLiteStorage(str(tmp_path / "seed.db"))
    capture_local_agents(seed, tenant_id="local", claude_root=root)
    [current_bundle] = seed.list_agent_run_bundles("local")
    current_traces = seed.list_traces(tenant_id="local")
    seed.close()

    legacy_bundle = replace(
        current_bundle,
        turns=tuple(
            replace(
                turn,
                user_request_redacted=(turn.user_request_redacted or "")[:1_000],
                final_response_redacted=(turn.final_response_redacted or "")[:1_000],
                input_tokens=None,
                cached_input_tokens=None,
                cache_write_input_tokens=None,
                output_tokens=None,
                reasoning_output_tokens=None,
                total_tokens=None,
                token_usage_basis=None,
                request_truncated=False,
                response_truncated=False,
            )
            for turn in current_bundle.turns
        ),
    )
    legacy_traces = tuple(
        replace(
            trace,
            prompt_redacted=(trace.prompt_redacted or "")[:1_000],
            response_redacted=(trace.response_redacted or "")[:1_000],
            raw_messages=[
                {
                    **message,
                    "content": str(message.get("content") or "")[:1_000],
                }
                for message in trace.raw_messages or []
            ],
        )
        for trace in current_traces
    )
    storage = SQLiteStorage(str(tmp_path / "upgrade.db"))
    AgentCaptureService(storage).capture(legacy_bundle, traces=legacy_traces)

    summary = capture_local_agents(storage, tenant_id="local", claude_root=root)

    assert summary.as_dict() == {
        "files": 1,
        "stored": 1,
        "skipped": 0,
        "skip_reasons": {},
    }
    [upgraded] = storage.list_agent_run_bundles("local")
    assert upgraded == current_bundle
    assert storage.list_traces(tenant_id="local") == current_traces


@pytest.mark.parametrize("storage_kind", ["memory", "sqlite", "buffered"])
def test_claude_rescan_reconciles_legacy_truncate_then_redact_boundary(
    tmp_path: Path, storage_kind: str,
) -> None:
    root = tmp_path / "claude"
    records = _claude_records()
    legacy_fragment = "user@exam"
    prefix = "p" * (1_000 - len(legacy_fragment) - 1) + " "
    request = f"{prefix}user@example.com remains private"
    records[0]["message"]["content"] = request
    _write_jsonl(root / "session.jsonl", records)

    seed = SQLiteStorage(str(tmp_path / "seed.db"))
    capture_local_agents(seed, tenant_id="local", claude_root=root)
    [current_bundle] = seed.list_agent_run_bundles("local")
    current_traces = seed.list_traces(tenant_id="local")
    seed.close()
    legacy_request = request[:1_000]
    assert legacy_request.endswith(legacy_fragment)
    legacy_bundle = replace(
        current_bundle,
        turns=tuple(
            replace(turn, user_request_redacted=legacy_request)
            for turn in current_bundle.turns
        ),
    )
    legacy_traces = tuple(
        replace(
            trace,
            prompt_redacted=legacy_request,
            raw_messages=[
                {**message, "content": legacy_request}
                if message.get("role") == "user"
                else message
                for message in trace.raw_messages or []
            ],
        )
        for trace in current_traces
    )
    if storage_kind == "memory":
        storage = InMemoryStorage()
    elif storage_kind == "buffered":
        storage = BufferedStorage(InMemoryStorage())
    else:
        storage = SQLiteStorage(str(tmp_path / "upgrade.db"))
    try:
        AgentCaptureService(storage).capture(legacy_bundle, traces=legacy_traces)

        summary = capture_local_agents(storage, tenant_id="local", claude_root=root)

        assert summary.stored == 1
        assert summary.skipped == 0
        [upgraded] = storage.list_agent_run_bundles("local")
        assert upgraded.turns[0].user_request_redacted == f"{prefix}<EMAIL> remains private"
        assert all(
            trace.prompt_redacted == f"{prefix}<EMAIL> remains private"
            for trace in storage.list_traces(tenant_id="local")
        )

        records[0]["message"]["content"] = f"q{request[1:]}"
        _write_jsonl(root / "session.jsonl", records)
        conflicting = capture_local_agents(storage, tenant_id="local", claude_root=root)
        assert conflicting.stored == 0
        assert conflicting.skipped == 1
    finally:
        storage.close()


def test_claude_split_response_can_complete_trace_usage_on_a_later_row(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude"
    records = _claude_records()
    first_response = next(
        row for row in records if row.get("uuid") == "assistant-1"
    )
    first_response["message"].pop("usage")
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", claude_root=root)

    trace = next(
        item
        for item in storage.list_traces(tenant_id="local")
        if item.response_redacted == "I will run the tests."
    )
    [bundle] = storage.list_agent_run_bundles("local")
    assert bundle.turns[0].input_tokens == 30
    assert bundle.turns[0].output_tokens == 8
    assert trace.input_tokens == 12
    assert trace.output_tokens == 3


def test_conflicting_claude_split_response_usage_fails_closed_for_its_trace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude"
    records = _claude_records()
    conflicting_response = next(
        row for row in records if row.get("uuid") == "assistant-1-text"
    )
    conflicting_response["message"]["usage"]["input_tokens"] = 13
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", claude_root=root)

    trace = next(
        item
        for item in storage.list_traces(tenant_id="local")
        if item.response_redacted == "I will run the tests."
    )
    [bundle] = storage.list_agent_run_bundles("local")
    assert bundle.turns[0].token_usage_basis is None
    assert trace.input_tokens is None
    assert trace.output_tokens is None


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


@pytest.mark.parametrize("prefix_character", ["x", "🔥"])
def test_turn_redaction_precedes_utf8_preview_boundary(
    tmp_path: Path, prefix_character: str,
) -> None:
    from verdict.dashboard.app import build_agent_run_detail

    root = tmp_path / "codex"
    records = _codex_records()
    fragment = "user@exa"
    prefix_bytes = 65_536 - len(fragment.encode("utf-8"))
    separator = " "
    prefix = (
        prefix_character
        * ((prefix_bytes - len(separator)) // len(prefix_character.encode("utf-8")))
        + separator
    )
    request = f"{prefix}user@example.com remains private"
    records[3]["payload"]["message"] = request
    records[-1]["payload"]["last_agent_message"] = request
    _write_jsonl(root / "session.jsonl", records)
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    [bundle] = storage.list_agent_run_bundles("local")
    turn = bundle.turns[0]
    storage.close()
    detail = build_agent_run_detail(database, tenant="local", run_id=bundle.run.run_id)
    assert turn.request_truncated is True
    assert turn.response_truncated is True
    assert "user@" not in (turn.user_request_redacted or "")
    assert "user@" not in (turn.final_response_redacted or "")
    assert "<EMAIL>" in (turn.user_request_redacted or "")
    assert "<EMAIL>" in (turn.final_response_redacted or "")
    assert "user@" not in json.dumps(detail)


def test_codex_command_and_result_redaction_precede_content_limits(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    token = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    opaque = "opaque-nested-result-canary"
    crossing_value = f"{'x' * 994} {token}"
    records[4]["payload"]["arguments"] = json.dumps({"cmd": crossing_value})
    records[5]["payload"]["output"] = json.dumps(
        {
            "exit_code": 1,
            "output": {
                "password": {"nested": opaque},
                "stdout": crossing_value,
            },
        }
    )
    _write_jsonl(root / "session.jsonl", records)
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))

    capture_local_agents(storage, tenant_id="local", codex_root=root)

    [bundle] = storage.list_agent_run_bundles("local")
    storage.close()
    assert "ghp_" not in repr(bundle)
    assert opaque not in repr(bundle)


def test_claude_trace_redaction_precedes_the_preview_boundary(tmp_path: Path) -> None:
    from verdict.dashboard.app import build_agent_run_detail

    root = tmp_path / "claude"
    records = _claude_records()
    fragment = "user@exa"
    prefix = "p" * (65_536 - len(fragment) - 1) + " "
    content = f"{prefix}user@example.com remains private"
    records[0]["message"]["content"] = content
    next(
        row for row in records if row.get("uuid") == "assistant-2-text"
    )["message"]["content"][0]["text"] = content
    _write_jsonl(root / "session.jsonl", records)
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))

    capture_local_agents(storage, tenant_id="local", claude_root=root)

    [bundle] = storage.list_agent_run_bundles("local")
    traces = storage.list_traces(tenant_id="local")
    storage.close()
    detail = build_agent_run_detail(database, tenant="local", run_id=bundle.run.run_id)
    persisted = json.dumps(
        {
            "bundle": repr(bundle),
            "traces": [repr(trace) for trace in traces],
            "detail": detail,
        }
    )
    assert bundle.turns[0].request_truncated is True
    assert bundle.turns[0].response_truncated is True
    assert all("user@" not in (trace.prompt_redacted or "") for trace in traces)
    assert all(
        "user@" not in json.dumps(trace.raw_messages) for trace in traces
    )
    assert any("<EMAIL>" in (trace.response_redacted or "") for trace in traces)
    assert "user@" not in persisted


@pytest.mark.parametrize("missing", ["input_tokens", "output_tokens"])
def test_claude_component_only_usage_stays_partial(
    tmp_path: Path, missing: str,
) -> None:
    from verdict.dashboard.app import build_agent_insights_bundle

    root = tmp_path / "claude"
    records = _claude_records()[:2]
    records[1]["message"]["stop_reason"] = "end_turn"
    records[1]["message"]["usage"].pop(missing)
    _write_jsonl(root / "session.jsonl", records)
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))

    capture_local_agents(storage, tenant_id="local", claude_root=root)

    [bundle] = storage.list_agent_run_bundles("local")
    [trace] = storage.list_traces(tenant_id="local")
    storage.close()
    turn = bundle.turns[0]
    assert turn.total_tokens is None
    assert turn.token_usage_basis == "claude_provider_response_sum"
    assert getattr(turn, missing) is None
    assert getattr(trace, missing) is None
    [activity] = build_agent_insights_bundle(
        database, tenant="local"
    )["sourceActivity"]
    assert activity["totalTokens"] is None
    assert activity["tokenUsageState"] == "partial"


@pytest.mark.parametrize("message_id", ["", "x" * 257, "bad\x00id"])
@pytest.mark.parametrize("response_count", [1, 2])
def test_claude_usage_requires_a_valid_provider_response_identity(
    tmp_path: Path, response_count: int, message_id: str,
) -> None:
    root = tmp_path / "claude"
    records = _claude_records()[:2]
    records[1]["message"]["id"] = message_id
    records[1]["message"]["stop_reason"] = "end_turn"
    records[1]["message"]["content"] = [{"type": "text", "text": "done"}]
    if response_count == 2:
        records.append(json.loads(json.dumps(records[1])))
        records[-1]["uuid"] = "assistant-empty-id-duplicate"
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    capture_local_agents(storage, tenant_id="local", claude_root=root)

    [bundle] = storage.list_agent_run_bundles("local")
    [turn] = bundle.turns
    assert turn.total_tokens is None
    assert turn.token_usage_basis is None
    assert not any(
        event.event_type is AgentEventType.MODEL_CALL for event in bundle.events
    )
    assert storage.list_traces(tenant_id="local") == []


def test_claude_growing_turn_does_not_publish_a_stale_complete_total(
    tmp_path: Path,
) -> None:
    from verdict.dashboard.app import build_agent_insights_bundle

    root = tmp_path / "claude"
    path = root / "session.jsonl"
    records = _claude_records()[:2]
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))

    _write_jsonl(path, records)
    capture_local_agents(storage, tenant_id="local", claude_root=root)
    [first_turn] = storage.list_agent_run_bundles("local")[0].turns
    assert first_turn.status is ExecutionStatus.UNKNOWN
    assert first_turn.total_tokens is None

    partial = {
        "timestamp": "2026-08-30T11:00:02Z",
        "type": "assistant",
        "uuid": "assistant-2-partial",
        "sessionId": "claude-session-1",
        "message": {
            "id": "msg-2",
            "model": "claude-sonnet-4-5",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 3},
            "content": [],
        },
    }
    records.append(partial)
    _write_jsonl(path, records)
    capture_local_agents(storage, tenant_id="local", claude_root=root)

    [growing] = storage.list_agent_run_bundles("local")[0].turns
    assert growing.input_tokens == 15
    assert growing.output_tokens == 3
    assert growing.total_tokens is None
    [activity] = build_agent_insights_bundle(
        database, tenant="local"
    )["sourceActivity"]
    assert activity["tokenUsageState"] == "partial"

    completed = {
        **partial,
        "timestamp": "2026-08-30T11:00:03Z",
        "uuid": "assistant-2-complete",
        "message": {
            **partial["message"],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "content": [{"type": "text", "text": "done"}],
        },
    }
    records.append(completed)
    _write_jsonl(path, records)
    capture_local_agents(storage, tenant_id="local", claude_root=root)

    [finished] = storage.list_agent_run_bundles("local")[0].turns
    assert finished.status is ExecutionStatus.COMPLETED
    assert finished.total_tokens == 44


class _PublishedSignatureStorage(InMemoryStorage):
    def replace_agent_capture(self, bundle, traces=()) -> None:
        super().replace_agent_capture(bundle, traces)


@pytest.mark.parametrize("buffered", [False, True])
def test_local_capture_supports_existing_custom_storage_adapters(
    tmp_path: Path, buffered: bool,
) -> None:
    root = tmp_path / "codex"
    _write_jsonl(root / "session.jsonl", _codex_records())
    inner = _PublishedSignatureStorage()
    storage = BufferedStorage(inner) if buffered else inner
    try:
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.stored == 1
        assert len(storage.list_agent_run_bundles("local")) == 1
    finally:
        storage.close()


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


def test_local_cli_explains_how_to_open_a_non_default_tenant(tmp_path: Path, capsys) -> None:
    codex = tmp_path / "codex"
    _write_jsonl(codex / "session.jsonl", _codex_records())

    status = main([
        "local",
        "--storage", f"sqlite:///{tmp_path / 'verdict.db'}",
        "--codex-root", str(codex),
        "--claude-root", str(tmp_path / "missing-claude"),
        "--tenant-id", "customer-a",
    ])

    output = capsys.readouterr()
    assert status == 0
    assert "start verdict-dashboard with the same --tenant-id" in output.err
    assert "customer-a" in output.err


def test_local_cli_preserves_a_256_character_import_tenant(
    tmp_path: Path, capsys,
) -> None:
    codex = tmp_path / "codex"
    _write_jsonl(codex / "session.jsonl", _codex_records())
    tenant_id = "a" * 256

    status = main([
        "local",
        "--storage", f"sqlite:///{tmp_path / 'verdict.db'}",
        "--codex-root", str(codex),
        "--claude-root", str(tmp_path / "missing-claude"),
        "--tenant-id", tenant_id,
    ])

    output = capsys.readouterr()
    assert status == 0
    assert "dashboard tenant selectors are limited to 128" in output.err


@pytest.mark.parametrize("tenant_id", ["customer name", "a" * 257])
def test_local_cli_rejects_an_invalid_import_tenant(
    tmp_path: Path, capsys, tenant_id: str,
) -> None:
    status = main([
        "local",
        "--storage", f"sqlite:///{tmp_path / 'verdict.db'}",
        "--tenant-id", tenant_id,
    ])

    output = capsys.readouterr()
    assert status == 2
    assert "bounded routing identifier" in output.err
    assert not (tmp_path / "verdict.db").exists()


def test_large_session_is_bounded_and_marks_omitted_events(tmp_path: Path) -> None:
    import asyncio

    import httpx
    from verdict.dashboard.app import create_app

    root = tmp_path / "codex"
    records = _codex_records()
    records[4]["payload"]["arguments"] = json.dumps({
        "cmd": "echo ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "password": {"nested": "opaque-nested-result-canary"},
    })
    records[4:4] = [records[4]] * 1_600
    _write_jsonl(root / "large.jsonl", records)
    (root / "large.jsonl").write_bytes(b"\n" + (root / "large.jsonl").read_bytes())
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)

    assert summary.stored == 1
    assert summary.as_dict()["event_limited_runs"] == 1
    bundle = storage.list_agent_run_bundles("local")[0]
    assert len(bundle.events) == 1_500
    marker = bundle.events[-1]
    assert marker.provenance == "verdict:capture_limit"
    assert marker.attributes["name"] == "source_events_omitted"
    assert int(marker.attributes["source"]) > 0
    recent = storage.get_recent_local_events("local", bundle.run.run_id)
    assert recent is not None
    assert recent["partial"] is True
    assert recent["sourceEventCount"] > len(bundle.events)
    assert recent["firstOrdinal"] == recent["sourceEventCount"] - 50
    assert len(recent["events"]) == 50
    assert recent["events"][-1]["sourceOrdinal"] == recent["sourceEventCount"] - 1
    assert recent["events"][-1]["eventId"] not in {event.event_id for event in bundle.events}
    assert "ghp_" not in json.dumps(recent)
    assert "opaque-nested-result-canary" not in json.dumps(recent)

    async def request_sample(tenant):
        app = create_app(storage=str(tmp_path / "verdict.db"), tenant_id=tenant)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(f"/api/runs/{bundle.run.run_id}/recent-local-events")

    own = asyncio.run(request_sample("local"))
    foreign = asyncio.run(request_sample("foreign"))
    assert own.status_code == 200
    assert own.json()["events"][-1]["sourceOrdinal"] == recent["sourceEventCount"] - 1
    assert "ghp_" not in own.text
    assert "opaque-nested-result-canary" not in own.text
    assert foreign.status_code == 404

    path = root / "large.jsonl"
    original_length = path.stat().st_size
    with path.open("a") as handle:
        for row in (
            {"timestamp": "2026-08-30T10:02:00Z", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "turn-b"}},
            {"timestamp": "2026-08-30T10:02:01Z", "type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command",
                         "call_id": "later-call", "arguments": json.dumps({"cmd": "true"})}},
            {"timestamp": "2026-08-30T10:02:01Z", "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "later-call",
                         "output": json.dumps({"exit_code": 1, "output": "failed"})}},
            {"timestamp": "2026-08-30T10:02:02Z", "type": "event_msg",
             "payload": {"type": "task_complete", "turn_id": "turn-b",
                         "last_agent_message": "done"}},
        ):
            handle.write(json.dumps(row) + "\n")
    advanced = capture_local_agents(storage, tenant_id="local", codex_root=root)
    assert advanced.recent_sample_errors == 0
    newer = storage.get_recent_local_events("local", bundle.run.run_id)
    assert newer["sourceEventCount"] > recent["sourceEventCount"]
    assert newer["events"][-1]["turnSequence"] == 1
    from verdict.dashboard.app import build_recent_local_sample
    hydrated = build_recent_local_sample(
        str(tmp_path / "verdict.db"), tenant="local", run_id=bundle.run.run_id,
    )
    assert {finding["code"] for finding in hydrated["findings"]} >= {
        "tool_error", "command_failed",
    }
    assert newer["turns"][-1]["eligibleForFindings"] is True
    assert len(storage.get_agent_run_bundle("local", bundle.run.run_id).events) == 1_500
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).recent_sample_errors == 0
    assert storage.get_recent_local_events("local", bundle.run.run_id) == newer

    _assert_legacy_writer_cannot_replace_atomic_window(storage, path, "local", bundle.run.run_id)
    newer = storage.get_recent_local_events("local", bundle.run.run_id)
    retained_bundle, retained_revision = storage.get_agent_capture_snapshot(
        "local", bundle.run.run_id,
    )

    advanced_bytes = path.read_bytes()
    path.write_bytes(advanced_bytes[:original_length])
    shortened = capture_local_agents(storage, tenant_id="local", codex_root=root)
    assert shortened.stored == 0
    assert shortened.skip_reasons == {"local_source_conflict": 1}
    assert shortened.recent_sample_errors == 0
    assert storage.get_recent_local_events("local", bundle.run.run_id) == newer
    assert storage.get_agent_capture_snapshot("local", bundle.run.run_id) == (
        retained_bundle, retained_revision,
    )
    path.write_bytes(advanced_bytes)
    path.write_bytes(path.read_bytes().replace(b"/Users/example/project", b"/Users/example/projecT", 1))
    conflicting = capture_local_agents(storage, tenant_id="local", codex_root=root)
    assert conflicting.stored == 0
    assert conflicting.skip_reasons == {"local_source_conflict": 1}
    assert conflicting.recent_sample_errors == 0
    assert storage.get_recent_local_events("local", bundle.run.run_id) == newer
    assert storage.get_agent_capture_snapshot("local", bundle.run.run_id) == (
        retained_bundle, retained_revision,
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_high_event_tail_failure_reaches_durable_monitor_without_v1_inflation(
    tmp_path: Path, backend: str, monkeypatch,
) -> None:
    import asyncio
    from datetime import timedelta, timezone

    import httpx
    import verdict.telemetry.local_agents as local_agents
    from verdict.dashboard.app import build_recent_local_sample, create_app
    from verdict.dashboard.control_plane import ControlStore
    from verdict.dashboard.issue_notices import IssueNotices

    root = tmp_path / "codex"
    rows = _codex_records("synthetic-canary")
    rows[4:4] = [rows[4]] * 1_600
    rows.extend([
        {"timestamp": "2026-08-30T10:02:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "late-turn"}},
        {"timestamp": "2026-08-30T10:02:01Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "late-call", "arguments": json.dumps({"cmd": "false"})}},
        {"timestamp": "2026-08-30T10:02:02Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "late-call",
                     "output": json.dumps({"exit_code": 1, "output": "failed"})}},
        {"timestamp": "2026-08-30T10:02:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "late-turn"}},
    ])
    # Keep the event day within the API's 30-day window, including near UTC midnight.
    now = datetime.now(timezone.utc)
    base = now - timedelta(minutes=5)
    for row in rows:
        original = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
        row["timestamp"] = (base + (original - datetime.fromisoformat(
            "2026-08-30T10:00:00+00:00"
        ))).isoformat()
    # Exercise the selected-recent fallback, where issue coverage stays partial.
    monkeypatch.setattr(local_agents, "_MAX_FULL_PROJECTED_ROWS", 1_000)
    _write_jsonl(root / "large.jsonl", rows)
    with ExitStack() as stack:
        if backend == "postgres":
            from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
            from verdict.storage.postgres import PostgresStorage

            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            url = stack.enter_context(isolated_test_dsn(dsn))
            storage = PostgresStorage(url)
        else:
            db = tmp_path / "verdict.db"
            url = f"sqlite:///{db}"
            storage = SQLiteStorage(str(db))
        stack.callback(storage.close)
        control = ControlStore(url)
        control.append("local", kind="schedule", document_id="daily",
                       state="active", payload={"analyzeOnly": True},
                       expected_revision=None)
        with control._connect() as connection:
            connection.execute(
                "UPDATE product_control_documents SET created_at="
                + ("%s" if control.postgres else "?")
                + " WHERE tenant_id='local' AND kind='schedule' AND document_id='daily'",
                ((now - timedelta(minutes=10)) if control.postgres
                 else (now - timedelta(minutes=10)).isoformat(),),
            )
            connection.commit()

        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        v1_ids = {event.event_id for event in bundle.events}
        assert len(bundle.events) == 1_500
        window = storage.get_recent_local_events("local", bundle.run.run_id)
        hydrated = build_recent_local_sample(url, tenant="local", run_id=bundle.run.run_id)
        [late] = [finding for finding in hydrated["findings"]
                  if finding["code"] == "command_failed"]
        assert late["eventId"] not in v1_ids
        [linked] = [event for event in hydrated["events"]
                    if event["eventId"] == late["eventId"]]
        assert linked["firstSeenRevision"] == window["sourceRevision"]

        notices = IssueNotices(url)
        assert notices.scan("local") >= 1
        assert any(item["eventId"] == late["eventId"] and
                   item["checkCode"] == "command_failed" and
                   item["recentRevision"] == linked["firstSeenRevision"] and
                   item["recentTurnId"] == linked["turnId"]
                   for item in notices.list("local"))

        async def issue_day():
            app = create_app(storage=url, tenant_id="local")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                return await client.get("/api/usage", params={
                    "day": linked["occurredAt"][:10],
                })

        issue_response = asyncio.run(issue_day())
        assert issue_response.status_code == 200, issue_response.text
        issue_report = issue_response.json()
        issue_day_row = next(day for day in issue_report["days"]
                             if day["day"] == linked["occurredAt"][:10])
        assert issue_day_row["recordedIssueEvents"] >= 1
        selected_issues = issue_report["selectedDayIssues"]
        assert selected_issues["complete"] is False
        assert selected_issues["count"] is None
        assert selected_issues["observedCount"] >= 1
        assert any(item["eventId"] == late["eventId"] and
                   item["recentRevision"] == linked["firstSeenRevision"] and
                   item["recentTurnId"] == linked["turnId"]
                   for item in selected_issues["events"])
        async def foreign_issue_day():
            app = create_app(storage=url, tenant_id="foreign")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                return await client.get("/api/usage", params={
                    "day": linked["occurredAt"][:10],
                })

        foreign_response = asyncio.run(foreign_issue_day())
        assert foreign_response.status_code == 200
        assert foreign_response.json()["selectedDayIssues"]["events"] == []
        assert foreign_response.json()["selectedDayIssues"]["count"] == 0

        import verdict.dashboard.usage as usage_module
        with monkeypatch.context() as bounded:
            bounded.setattr(usage_module, "MAX_ISSUE_EVENTS", 1)
            truncated = asyncio.run(issue_day())
        assert truncated.status_code == 200
        assert truncated.json()["selectedDayIssues"]["complete"] is False
        assert truncated.json()["selectedDayIssues"]["events"] == []
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        assert notices.scan("local") == 0
        assert len(storage.get_agent_run_bundle("local", bundle.run.run_id).events) == 1_500
        with (root / "large.jsonl").open("a") as handle:
            handle.write(json.dumps({
                "timestamp": (base + timedelta(minutes=10)).isoformat(),
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "newer-turn"},
            }) + "\n")
            for index in range(60):
                handle.write(json.dumps({
                    "timestamp": (base + timedelta(minutes=10, seconds=index)).isoformat(),
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "exec_command",
                                "call_id": f"newer-{index}",
                                "arguments": json.dumps({"cmd": "true"})},
                }) + "\n")
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        moved = build_recent_local_sample(url, tenant="local", run_id=bundle.run.run_id)
        assert late["eventId"] not in {event["eventId"] for event in moved["events"]}

        async def old_link():
            app = create_app(storage=url, tenant_id="local")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                return await client.get(
                    f"/api/runs/{bundle.run.run_id}/recent-local-events",
                    params={"revision": linked["firstSeenRevision"],
                            "turn_id": linked["turnId"], "event_id": linked["eventId"]},
                )

        historical = asyncio.run(old_link())
        assert historical.status_code == 200
        assert historical.json()["historicalFocusedEvent"] is True
        assert historical.json()["focusedEvent"]["eventId"] == late["eventId"]

        # A malformed older fact must not leak a different tenant's Trace ID
        # through either the current window or an exact historical link.
        from verdict.schema import Trace

        owned_trace = "owned@example.test"
        storage.insert_trace(Trace(trace_id=owned_trace, tenant_id="local"))
        storage.insert_trace(Trace(trace_id="foreign-trace-synthetic", tenant_id="foreign"))
        foreign_event = moved["events"][0]["eventId"]
        owned_event = moved["events"][1]["eventId"]
        if backend == "postgres":
            with storage._pool.connection() as connection:
                for trace_id, event_id in (
                    ("foreign-trace-synthetic", foreign_event),
                    (owned_trace, owned_event),
                    ("foreign-trace-synthetic", late["eventId"]),
                ):
                    connection.execute(
                        "UPDATE local_recent_event_facts SET trace_id=%s "
                        "WHERE tenant_id=%s AND run_id=%s AND event_id=%s",
                        (trace_id, "local", bundle.run.run_id, event_id),
                    )
                connection.execute(
                    "UPDATE local_recent_event_facts SET attributes_json=%s "
                    "WHERE tenant_id=%s AND run_id=%s AND event_id=%s",
                    (json.dumps({"note": "context@example.test"}),
                     "local", bundle.run.run_id, owned_event),
                )
        else:
            for trace_id, event_id in (
                ("foreign-trace-synthetic", foreign_event),
                (owned_trace, owned_event),
                ("foreign-trace-synthetic", late["eventId"]),
            ):
                storage._conn.execute(
                    "UPDATE local_recent_event_facts SET trace_id=? "
                    "WHERE tenant_id=? AND run_id=? AND event_id=?",
                    (trace_id, "local", bundle.run.run_id, event_id),
                )
            storage._conn.execute(
                "UPDATE local_recent_event_facts SET attributes_json=? "
                "WHERE tenant_id=? AND run_id=? AND event_id=?",
                (json.dumps({"note": "context@example.test"}),
                 "local", bundle.run.run_id, owned_event),
            )

        async def recent_response():
            app = create_app(storage=url, tenant_id="local")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                return await client.get(f"/api/runs/{bundle.run.run_id}/recent-local-events")

        current_response = asyncio.run(recent_response())
        assert current_response.status_code == 200
        linked_events = {item["eventId"]: item for item in current_response.json()["events"]}
        assert linked_events[foreign_event]["traceId"] is None
        assert linked_events[owned_event]["traceId"] == owned_trace
        assert linked_events[owned_event]["attributes"]["note"] == "<EMAIL>"
        async def current_link():
            event = moved["events"][0]
            app = create_app(storage=url, tenant_id="local")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                return await client.get(
                    f"/api/runs/{bundle.run.run_id}/recent-local-events",
                    params={"revision": event.get("firstSeenRevision") or moved["sourceRevision"],
                            "turn_id": event["turnId"], "event_id": foreign_event},
                )

        current_link_response = asyncio.run(current_link())
        assert current_link_response.status_code == 200
        assert current_link_response.json()["focusedEvent"]["traceId"] is None
        historical_response = asyncio.run(old_link())
        assert historical_response.status_code == 200
        assert historical_response.json()["focusedEvent"]["traceId"] is None
        assert "foreign-trace-synthetic" not in current_response.text + historical_response.text

        if backend == "postgres":
            with storage._pool.connection() as connection:
                connection.execute(
                    "UPDATE local_recent_event_facts SET trace_id=%s "
                    "WHERE tenant_id=%s AND run_id=%s AND event_id=%s",
                    (owned_trace, "local", bundle.run.run_id, late["eventId"]),
                )
        else:
            storage._conn.execute(
                "UPDATE local_recent_event_facts SET trace_id=? "
                "WHERE tenant_id=? AND run_id=? AND event_id=?",
                (owned_trace, "local", bundle.run.run_id, late["eventId"]),
            )
        assert asyncio.run(old_link()).json()["focusedEvent"]["traceId"] == owned_trace

        # Inspection-only beta windows can carry malformed saved structural
        # links. They share the same tenant boundary and 50-event read budget.
        legacy = {**storage.get_recent_local_events("local", bundle.run.run_id),
                  "schema": "local-recent-events-v1",
                  "events": [{**moved["events"][0], "traceId": ["foreign-trace-synthetic"]},
                             {**moved["events"][1], "traceId": owned_trace}]}

        def replace_saved_window(sample):
            payload = json.dumps(sample, separators=(",", ":"))
            if backend == "postgres":
                with storage._pool.connection() as connection:
                    connection.execute(
                        "UPDATE local_recent_events SET sample_json=%s "
                        "WHERE tenant_id=%s AND run_id=%s",
                        (payload, "local", bundle.run.run_id),
                    )
            else:
                storage._conn.execute(
                    "UPDATE local_recent_events SET sample_json=? "
                    "WHERE tenant_id=? AND run_id=?",
                    (payload, "local", bundle.run.run_id),
                )

        replace_saved_window(legacy)
        legacy_response = asyncio.run(recent_response())
        assert legacy_response.status_code == 200
        assert legacy_response.json()["events"][0]["traceId"] is None
        assert legacy_response.json()["events"][1]["traceId"] == owned_trace
        replace_saved_window({**legacy, "events": legacy["events"] * 51})
        with pytest.raises(ValueError, match="bounded recent local events"):
            build_recent_local_sample(url, tenant="local", run_id=bundle.run.run_id)


@pytest.fixture(params=["sqlite", "postgres", "memory", "buffered"])
def recent_fact_storage(request, tmp_path: Path):
    with ExitStack() as stack:
        if request.param == "postgres":
            from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
            from verdict.storage.postgres import PostgresStorage

            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            storage = PostgresStorage(stack.enter_context(isolated_test_dsn(dsn)))
        elif request.param == "memory":
            storage = InMemoryStorage()
        elif request.param == "buffered":
            storage = BufferedStorage(InMemoryStorage())
        else:
            storage = SQLiteStorage(str(tmp_path / "verdict.db"))
        stack.callback(storage.close)
        yield storage


def test_claude_recent_model_call_enriches_same_fact_and_trace_on_append(
    tmp_path: Path, recent_fact_storage,
) -> None:
    from verdict.schema import Judgment

    root = tmp_path / "claude"
    path = root / "session.jsonl"
    session = "synthetic-late-claude"
    rows = [
        {"timestamp": "2026-08-30T11:00:00Z", "type": "user", "uuid": "user-1",
         "sessionId": session, "message": {"content": "inspect synthetic run"}},
        {"timestamp": "2026-08-30T11:00:01Z", "type": "user",
         "sourceToolAssistantUUID": "synthetic-tools", "sessionId": session,
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": f"tool-{index}",
              "is_error": False, "content": "ok"}
             for index in range(1_600)
         ]}},
        {"timestamp": "2026-08-30T11:00:02Z", "type": "assistant",
         "uuid": "late-assistant", "sessionId": session,
         "message": {"id": "late-msg", "model": "claude-sonnet-4-5",
                     "stop_reason": "tool_use", "usage": {"input_tokens": 12},
                     "content": []}},
    ]
    _write_jsonl(path, rows)
    storage = recent_fact_storage
    assert capture_local_agents(storage, tenant_id="local", claude_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    first = storage.get_recent_local_events("local", bundle.run.run_id)
    model = first["events"][-1]
    [initial_trace] = storage.list_traces(tenant_id="local", limit=2_000)
    assert model["eventId"] not in {event.event_id for event in bundle.events}
    assert initial_trace.input_tokens == 12
    assert initial_trace.output_tokens is None
    old_judgment = Judgment(trace_id=initial_trace.trace_id,
                            evaluator_fingerprint="a" * 64)
    storage.insert_judgment(old_judgment)
    assert storage.list_judgments_for_trace(initial_trace.trace_id)[0].judgment_id == (
        old_judgment.judgment_id
    )

    rows.append({
        "timestamp": "2026-08-30T11:00:03Z", "type": "assistant",
        "uuid": "late-assistant-text", "sessionId": session,
        "message": {"id": "late-msg", "model": "claude-sonnet-4-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                    "content": [{"type": "text", "text": "synthetic answer"}]},
    })
    _write_jsonl(path, rows)
    assert capture_local_agents(storage, tenant_id="local", claude_root=root).stored == 1
    second = storage.get_recent_local_events("local", bundle.run.run_id)
    updated = second["events"][-1]
    assert updated["eventId"] == model["eventId"]
    [trace] = [trace for trace in storage.list_traces(tenant_id="local", limit=2_000)
               if trace.trace_id == initial_trace.trace_id]
    assert trace.input_tokens == 12
    assert trace.output_tokens == 3
    assert trace.response_redacted == "synthetic answer"
    assert storage.list_judgments_for_trace(trace.trace_id) == []
    if isinstance(storage, SQLiteStorage):
        assert storage._conn.execute(
            "SELECT 1 FROM judgments WHERE judgment_id=?",
            (old_judgment.judgment_id,),
        ).fetchone() is not None
    current_judgment = Judgment(trace_id=trace.trace_id,
                                evaluator_fingerprint="a" * 64)
    storage.insert_judgment(current_judgment)
    assert [item.judgment_id for item in storage.list_judgments_for_trace(
        trace.trace_id
    )] == [current_judgment.judgment_id]
    retained = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
    rows.append({
        "timestamp": "2026-08-30T11:00:04Z", "type": "assistant",
        "uuid": "late-assistant-conflict", "sessionId": session,
        "message": {"id": "late-msg", "model": "claude-sonnet-4-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 13, "output_tokens": 3},
                    "content": []},
    })
    _write_jsonl(path, rows)
    conflicting = capture_local_agents(storage, tenant_id="local", claude_root=root)
    assert conflicting.stored == 0
    assert conflicting.skip_reasons == {"local_source_conflict": 1}
    assert storage.get_agent_capture_snapshot("local", bundle.run.run_id) == retained


def test_claude_model_fact_enrichment_after_window_moves(
    tmp_path: Path, recent_fact_storage,
) -> None:
    from verdict.schema import Judgment

    root = tmp_path / "claude"
    path = root / "session.jsonl"
    session = "synthetic-moved-model"
    rows = [
        {"timestamp": "2026-08-30T11:00:00Z", "type": "user", "uuid": "user-1",
         "sessionId": session, "message": {"content": "inspect synthetic run"}},
        {"timestamp": "2026-08-30T11:00:01Z", "type": "user",
         "sourceToolAssistantUUID": "synthetic-tools", "sessionId": session,
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": f"tool-{index}",
              "is_error": False, "content": "ok"}
             for index in range(1_600)
         ]}},
        {"timestamp": "2026-08-30T11:00:02Z", "type": "assistant",
         "uuid": "model", "sessionId": session,
         "message": {"id": "model-message", "model": "claude-sonnet-4-5",
                     "stop_reason": "tool_use", "usage": {"input_tokens": 12},
                     "content": []}},
    ]
    _write_jsonl(path, rows)
    storage = recent_fact_storage
    assert capture_local_agents(storage, tenant_id="local", claude_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    model = storage.get_recent_local_events("local", bundle.run.run_id)["events"][-1]
    [initial_trace] = storage.list_traces(tenant_id="local", limit=2_000)
    old_judgment = Judgment(trace_id=initial_trace.trace_id, evaluator_fingerprint="a" * 64)
    storage.insert_judgment(old_judgment)
    for index in range(60):
        rows.append({
            "timestamp": "2026-08-30T11:00:03Z", "type": "user",
            "sourceToolAssistantUUID": f"synthetic-later-{index}",
            "sessionId": session,
            "message": {"content": [{"type": "tool_result",
                                    "tool_use_id": f"later-{index}",
                                    "is_error": False, "content": "ok"}]},
        })
    _write_jsonl(path, rows)
    assert capture_local_agents(storage, tenant_id="local", claude_root=root).stored == 1
    assert model["eventId"] not in {event["eventId"] for event in
                                    storage.get_recent_local_events("local", bundle.run.run_id)["events"]}
    assert storage.list_judgments_for_trace(initial_trace.trace_id)[0].judgment_id == (
        old_judgment.judgment_id
    )
    rows.append({
        "timestamp": "2026-08-30T11:00:04Z", "type": "assistant",
        "uuid": "model-complete", "sessionId": session,
        "message": {"id": "model-message", "model": "claude-sonnet-4-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                    "content": [{"type": "text", "text": "synthetic answer"}]},
    })
    _write_jsonl(path, rows)
    assert capture_local_agents(storage, tenant_id="local", claude_root=root).stored == 1
    [trace] = [trace for trace in storage.list_traces(tenant_id="local", limit=2_000)
               if trace.trace_id == initial_trace.trace_id]
    assert trace.output_tokens == 3
    assert trace.response_redacted == "synthetic answer"
    assert storage.list_judgments_for_trace(trace.trace_id) == []
    current_judgment = Judgment(trace_id=trace.trace_id, evaluator_fingerprint="a" * 64)
    storage.insert_judgment(current_judgment)
    assert [item.judgment_id for item in storage.list_judgments_for_trace(
        trace.trace_id
    )] == [current_judgment.judgment_id]
    prior = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
    rows.append({
        "timestamp": "2026-08-30T11:00:05Z", "type": "assistant",
        "uuid": "model-conflict", "sessionId": session,
        "message": {"id": "model-message", "model": "claude-sonnet-4-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 13, "output_tokens": 3},
                    "content": []},
    })
    _write_jsonl(path, rows)
    conflict = capture_local_agents(storage, tenant_id="local", claude_root=root)
    assert conflict.stored == 0
    assert conflict.skip_reasons == {"local_source_conflict": 1}
    assert storage.get_agent_capture_snapshot("local", bundle.run.run_id) == prior


def test_rescan_rejects_stale_retained_fact_locator_read(
    tmp_path: Path, recent_fact_storage, monkeypatch,
) -> None:
    root = tmp_path / "codex"
    path = root / "session.jsonl"
    rows = _codex_records("synthetic-stale-locators")
    rows[4:4] = [rows[4]] * 1_600
    _write_jsonl(path, rows)
    storage = recent_fact_storage
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    prior = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
    prior_window = storage.get_recent_local_events("local", bundle.run.run_id)
    assert storage.list_local_recent_fact_positions("local", bundle.run.run_id)
    rows.append({
        "timestamp": "2026-08-30T10:10:00Z", "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "later-turn"},
    })
    for index in range(60):
        rows.append({
            "timestamp": "2026-08-30T10:10:00Z", "type": "response_item",
            "payload": {"type": "function_call", "name": "synthetic_tool",
                        "call_id": f"later-{index}", "arguments": "{}"},
        })
    _write_jsonl(path, rows)
    monkeypatch.setattr(storage, "list_local_recent_fact_positions",
                        lambda _tenant, _run: ())
    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
    assert summary.stored == 0
    assert summary.skip_reasons == {"local_source_conflict": 1}
    assert storage.get_agent_capture_snapshot("local", bundle.run.run_id) == prior
    assert storage.get_recent_local_events("local", bundle.run.run_id) == prior_window


def test_recent_sample_ignores_untrusted_duplicate_attributes(
    tmp_path: Path,
) -> None:
    from verdict.dashboard.app import build_recent_local_sample

    root = tmp_path / "codex"
    rows = _codex_records("synthetic-canary")
    rows[4:4] = [rows[4]] * 1_600
    _write_jsonl(root / "large.jsonl", rows)
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    sample = storage.get_recent_local_events("local", bundle.run.run_id)
    assert sample["durableFacts"] is True
    sample["events"][-1]["attributes"] = {"tool_name": "forged"}
    storage._conn.execute(
        "UPDATE local_recent_events SET sample_json=? WHERE tenant_id=? AND run_id=?",
        (json.dumps(sample), "local", bundle.run.run_id),
    )
    storage._conn.commit()

    hydrated = build_recent_local_sample(str(db), tenant="local", run_id=bundle.run.run_id)
    assert hydrated["events"][-1]["attributes"] != {"tool_name": "forged"}


@pytest.mark.parametrize("field,value", [
    ("eventId", "forged"), ("sourceOrdinal", 0),
])
def test_recent_sample_rejects_forged_locator(
    tmp_path: Path, field: str, value: object,
) -> None:
    from verdict.dashboard.app import build_recent_local_sample

    root = tmp_path / "codex"
    rows = _codex_records("synthetic-canary")
    rows[4:4] = [rows[4]] * 1_600
    _write_jsonl(root / "large.jsonl", rows)
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    sample = storage.get_recent_local_events("local", bundle.run.run_id)
    sample["events"][-1][field] = value
    storage._conn.execute(
        "UPDATE local_recent_events SET sample_json=? WHERE tenant_id=? AND run_id=?",
        (json.dumps(sample), "local", bundle.run.run_id),
    )
    storage._conn.commit()
    with pytest.raises(ValueError, match=r"durable recent local|retained fact"):
        build_recent_local_sample(str(db), tenant="local", run_id=bundle.run.run_id)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_recent_sample_survives_content_capture_mode_change(
    tmp_path: Path, backend: str,
) -> None:
    from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
    from verdict.dashboard.app import build_recent_local_sample
    from verdict.storage.postgres import PostgresStorage

    root = tmp_path / "codex"
    rows = _codex_records("synthetic-canary")
    rows[4:4] = [rows[4]] * 1_600
    _write_jsonl(root / "large.jsonl", rows)
    with ExitStack() as stack:
        if backend == "postgres":
            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            url = stack.enter_context(isolated_test_dsn(dsn))
            storage = PostgresStorage(url)
        else:
            db = tmp_path / "verdict.db"
            url = str(db)
            storage = SQLiteStorage(url)
        stack.callback(storage.close)
        assert capture_local_agents(
            storage, tenant_id="local", codex_root=root, capture_content=True,
        ).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        for capture_content in (False, True):
            assert capture_local_agents(
                storage, tenant_id="local", codex_root=root,
                capture_content=capture_content,
            ).stored == 1
            sample = build_recent_local_sample(
                url, tenant="local", run_id=bundle.run.run_id,
            )
            assert len(sample["events"]) == 50
            assert all(isinstance(event["attributes"], dict)
                       for event in sample["events"])


@pytest.mark.parametrize("buffered", [False, True])
def test_memory_adapters_keep_recent_facts_with_source_revision(
    tmp_path: Path, buffered: bool,
) -> None:
    root = tmp_path / "codex"
    rows = _codex_records("synthetic-canary")
    rows[4:4] = [rows[4]] * 1_600
    _write_jsonl(root / "large.jsonl", rows)
    inner = InMemoryStorage()
    storage = BufferedStorage(inner) if buffered else inner
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        sample = storage.get_recent_local_events("local", bundle.run.run_id)
        assert sample["durableFacts"] is True
        retained_ids = {item[0].event.event_id for item in inner._local_recent_facts.values()}
        assert retained_ids.issuperset({
            event["eventId"] for event in sample["events"]
            if event["sourceOrdinal"] >= sample["canonicalPrefixEvents"]
        })
        assert len(retained_ids) + sample["canonicalPrefixEvents"] == sample["sourceEventCount"]
        assert ("local", bundle.run.run_id) in inner._local_projection_complete
        first = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        assert storage.get_agent_capture_snapshot("local", bundle.run.run_id) == first
    finally:
        storage.close()


def _zero_event_codex_history(turn_count: int, session_id: str) -> list[dict]:
    records = [{"timestamp": "2026-08-30T10:00:00Z", "type": "session_meta",
                "payload": {"id": session_id}}]
    for index in range(turn_count):
        records.extend([
            {"timestamp": "2026-08-30T10:00:00Z", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": f"turn-{index}"}},
            {"timestamp": "2026-08-30T10:00:01Z", "type": "event_msg",
             "payload": {"type": "task_complete", "turn_id": f"turn-{index}"}},
        ])
    return records


def test_recent_window_recomputes_forged_stored_findings(tmp_path: Path) -> None:
    import asyncio

    import httpx
    from verdict.dashboard.app import build_recent_local_sample, create_app

    root = tmp_path / "codex"
    _write_jsonl(root / "history.jsonl", _zero_event_codex_history(1_001, "forged"))
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    sample = storage.get_recent_local_events("local", bundle.run.run_id)
    assert sample["findings"] == []
    sample["findings"] = [{"code": "business_outcome_failed",
                           "eventId": "forged", "turnId": "forged"}]
    storage._conn.execute(
        "UPDATE local_recent_events SET sample_json=? WHERE tenant_id=? AND run_id=?",
        (json.dumps(sample), "local", bundle.run.run_id),
    )
    storage._conn.commit()
    assert build_recent_local_sample(str(db), tenant="local", run_id=bundle.run.run_id)[
        "findings"] == []
    storage._conn.execute(
        "UPDATE local_recent_events SET sample_json=? WHERE tenant_id=? AND run_id=?",
        ("{malformed", "local", bundle.run.run_id),
    )
    storage._conn.commit()

    async def read_corrupt():
        app = create_app(storage=f"sqlite:///{db}", tenant_id="local")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            return await client.get(f"/api/runs/{bundle.run.run_id}/recent-local-events")

    assert asyncio.run(read_corrupt()).status_code == 503

    storage._conn.execute(
        "UPDATE local_recent_events SET sample_json=? WHERE tenant_id=? AND run_id=?",
        ("[" * 10_000 + "0" + "]" * 10_000, "local", bundle.run.run_id),
    )
    storage._conn.commit()
    assert asyncio.run(read_corrupt()).status_code == 503


def test_recent_window_counts_omitted_events_when_last_turns_have_none(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    source = _zero_event_codex_history(1_700, "sparse-events")
    records = [source[0]]
    for index in range(1_700):
        records.append(source[1 + index * 2])
        if index < 1_590:
            records.append({
                "timestamp": "2026-08-30T10:00:00Z", "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": f"call-{index}",
                            "output": json.dumps({"exit_code": 1})},
            })
        records.append(source[2 + index * 2])
    _write_jsonl(root / "history.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    sample = storage.get_recent_local_events("local", bundle.run.run_id)
    assert sample["sourceEventCount"] > sample["canonicalPrefixEvents"]
    assert sample["events"] == []
    assert sample["firstOrdinal"] is None and sample["lastOrdinal"] is None
    assert sample["omittedMiddleEvents"] == (
        sample["sourceEventCount"] - sample["canonicalPrefixEvents"]
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite", "postgres"])
def test_recent_window_survives_rewrite_of_partially_captured_source(
    tmp_path: Path, backend: str,
) -> None:
    from verdict.storage.memory import InMemoryStorage

    root = tmp_path / "codex"
    path = root / "history.jsonl"
    records = _zero_event_codex_history(1_001, f"rewrite-{backend}")
    _write_jsonl(path, records)
    with ExitStack() as stack:
        if backend == "postgres":
            from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
            from verdict.storage.postgres import PostgresStorage

            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            storage = PostgresStorage(stack.enter_context(isolated_test_dsn(dsn)))
        elif backend == "memory":
            storage = InMemoryStorage()
        else:
            storage = SQLiteStorage(str(tmp_path / "verdict.db"))
        stack.callback(storage.close)
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        old = storage.get_recent_local_events("local", bundle.run.run_id)
        assert old["schema"] == "local-recent-window-v2"
        path.write_bytes(b"".join(json.dumps(item).encode() + b"\n"
                                  for item in records[:-2]) + b" " * 512 + b"\n")
        rejected = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert rejected.stored == 0
        assert rejected.skip_reasons == {"local_source_conflict": 1}
        assert storage.get_recent_local_events("local", bundle.run.run_id) == old


@pytest.mark.parametrize("incomplete_tail", [False, True])
def test_usage_distinguishes_admitted_turn_from_unparsed_tail(
    tmp_path: Path, incomplete_tail: bool,
) -> None:
    from verdict.dashboard.app import build_agent_usage_bundle

    root = tmp_path / "codex"
    path = root / "large.jsonl"
    records = _zero_event_codex_history(1_001, f"large-{incomplete_tail}")
    if incomplete_tail:
        _write_jsonl(path, records)
        with path.open("a") as handle:
            handle.write('{"timestamp":"2026-10-13T10:00:00Z","type":"event_msg"')
    else:
        records[-2]["timestamp"] = "2026-10-13T10:00:00Z"
        records[-1]["timestamp"] = "2026-08-30T10:00:01Z"
        _write_jsonl(path, records)
    current = json.loads(json.dumps(_codex_records()))
    encoded = json.dumps(current).replace("2026-08-30", "2026-10-13")
    encoded = encoded.replace("codex-session-1", "current-day")
    _write_jsonl(root / "current.jsonl", json.loads(encoded))
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 2
    report = build_agent_usage_bundle(
        f"sqlite:///{db}", tenant="local", day="2026-10-13",
        now=datetime.fromisoformat("2026-10-14T12:00:00+00:00"),
    )
    assert report["dayExplanation"]["totalTokens"] == 24
    assert report["dayExplanation"]["sourcePartial"] is incomplete_tail
    assert report["dayExplanation"]["comparison"] is None


def test_large_local_recent_sample_is_tenant_scoped_on_postgres(tmp_path: Path) -> None:
    import asyncio

    import httpx
    from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
    from verdict.dashboard.app import create_app
    from verdict.storage.postgres import PostgresStorage

    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 1_600
    _write_jsonl(root / "large.jsonl", records)
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
            assert summary.recent_sample_errors == 0
            [bundle] = storage.list_agent_run_bundles("local")
            sample = storage.get_recent_local_events("local", bundle.run.run_id)
            assert sample["partial"] is True and len(sample["events"]) == 50
            assert storage.get_recent_local_events("foreign", bundle.run.run_id) is None

            selected = sample["events"][0]

            async def request(tenant, exact=False):
                transport = httpx.ASGITransport(app=create_app(storage=isolated, tenant_id=tenant))
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.get(
                        f"/api/runs/{bundle.run.run_id}/recent-local-events",
                        params=({"revision": sample["sourceRevision"],
                                 "turn_id": selected["turnId"], "event_id": selected["eventId"]}
                                if exact else None),
                    )

            own = asyncio.run(request("local"))
            assert own.status_code == 200
            assert asyncio.run(request("foreign")).status_code == 404
            exact = asyncio.run(request("local", exact=True))
            assert exact.status_code == 200
            assert exact.json()["focusedEvent"] == own.json()["events"][0]
            assert asyncio.run(request("foreign", exact=True)).status_code == 404
            _assert_legacy_writer_cannot_replace_atomic_window(
                storage, root / "large.jsonl", "local", bundle.run.run_id,
            )
        finally:
            storage.close()


def test_large_claude_history_has_redacted_recent_inspection_sample(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    records = _claude_records()
    records[4]["message"]["content"][0]["content"] = (
        "user@example.com ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    )
    records[4:4] = [records[4]] * 1_600
    _write_jsonl(root / "session.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", claude_root=root)

    assert summary.stored == 1 and summary.recent_sample_errors == 0
    [bundle] = storage.list_agent_run_bundles("local")
    sample = storage.get_recent_local_events("local", bundle.run.run_id)
    assert sample["sourceKind"] == "claude-code"
    assert sample["sourceEventCount"] > 1_500
    assert len(sample["events"]) == 50
    assert sample["events"][-1]["eventId"] not in {event.event_id for event in bundle.events}
    assert "user@example.com" not in json.dumps(sample)
    assert "ghp_" not in json.dumps(sample)
    assert storage.get_recent_local_events("foreign", bundle.run.run_id) is None


def test_recent_window_failure_rolls_back_capture_and_checkpoint(
    tmp_path: Path,
) -> None:
    import asyncio

    import httpx
    from verdict.dashboard.app import build_recent_local_sample, create_app

    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 1_600
    path = root / "large.jsonl"
    _write_jsonl(path, records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).recent_sample_errors == 0
    [bundle] = storage.list_agent_run_bundles("local")
    previous = storage.get_recent_local_events("local", bundle.run.run_id)
    with path.open("a") as handle:
        handle.write(json.dumps({
            "timestamp": "2026-08-30T10:03:00Z", "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-a"},
        }) + "\n")

    # The window is part of the same SQLite transaction as the source checkpoint.
    # Abort its upsert to prove neither the new checkpoint nor V1 evidence advances.
    storage._conn.execute(
        "CREATE TRIGGER reject_recent_window BEFORE UPDATE ON local_recent_events "
        "BEGIN SELECT RAISE(ABORT, 'synthetic recent window failure'); END"
    )
    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
    assert summary.stored == 0
    assert summary.skipped == 1
    assert storage.get_recent_local_events("local", bundle.run.run_id) == previous
    current = build_recent_local_sample(
        str(tmp_path / "verdict.db"), tenant="local", run_id=bundle.run.run_id,
    )
    assert current["sourceRevision"] == previous["sourceRevision"]

    async def read_current_sample():
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}", tenant_id="local")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            return await client.get(f"/api/runs/{bundle.run.run_id}/recent-local-events")

    assert asyncio.run(read_current_sample()).status_code == 200


def test_postgres_recent_window_failure_rolls_back_capture_and_checkpoint(tmp_path: Path) -> None:
    from verdict.dashboard.app import build_recent_local_sample
    from verdict.storage.postgres import PostgresStorage

    from packages.verdict.tests._postgres_test_safety import isolated_test_dsn, validate_test_dsn

    dsn, reason = validate_test_dsn(
        os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
    )
    if dsn is None:
        pytest.skip(reason)
    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 1_600
    path = root / "large.jsonl"
    _write_jsonl(path, records)
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
            [bundle] = storage.list_agent_run_bundles("local")
            previous = storage.get_recent_local_events("local", bundle.run.run_id)
            with path.open("a") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-08-30T10:04:00Z", "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-a"},
                }) + "\n")
            with storage._pool.connection() as conn:
                conn.execute(
                    "CREATE FUNCTION reject_recent_window() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'synthetic recent window failure'; END $$"
                )
                conn.execute(
                    "CREATE TRIGGER reject_recent_window BEFORE UPDATE ON local_recent_events "
                    "FOR EACH ROW EXECUTE FUNCTION reject_recent_window()"
                )
            with pytest.raises(Exception, match="synthetic recent window failure"):
                capture_local_agents(storage, tenant_id="local", codex_root=root)
            assert storage.get_recent_local_events("local", bundle.run.run_id) == previous
            assert build_recent_local_sample(
                isolated, tenant="local", run_id=bundle.run.run_id,
            )["sourceRevision"] == previous["sourceRevision"]
        finally:
            storage.close()


def test_recent_window_revision_reference_rejects_stale_evidence(tmp_path: Path) -> None:
    import asyncio

    import httpx
    from verdict.dashboard.app import create_app

    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 1_600
    path = root / "large.jsonl"
    _write_jsonl(path, records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    first = storage.get_recent_local_events("local", bundle.run.run_id)
    assert first is not None
    with path.open("a") as handle:
        handle.write(json.dumps({
            "timestamp": "2026-08-30T10:04:00Z", "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-a"},
        }) + "\n")
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1

    async def read_revisions():
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}", tenant_id="local")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            url = f"/api/runs/{bundle.run.run_id}/recent-local-events"
            stale = await client.get(url, params={"revision": first["sourceRevision"]})
            current = await client.get(url)
            current_window = current.json()
            selected = current_window["events"][0]
            exact = await client.get(url, params={
                "revision": current_window["sourceRevision"],
                "turn_id": selected["turnId"], "event_id": selected["eventId"],
            })
            stale_exact = await client.get(url, params={
                "revision": first["sourceRevision"],
                "turn_id": selected["turnId"], "event_id": selected["eventId"],
            })
            missing = await client.get(url, params={
                "revision": current_window["sourceRevision"],
                "turn_id": selected["turnId"], "event_id": "absent",
            })
            unqualified = await client.get(url, params={
                "turn_id": selected["turnId"], "event_id": selected["eventId"],
            })
            return stale, current, exact, stale_exact, missing, unqualified, selected

    stale, current, exact, stale_exact, missing, unqualified, selected = asyncio.run(
        read_revisions())
    assert stale.status_code == 409
    assert stale.json() == {"error": "stale_revision"}
    assert current.status_code == 200
    assert current.json()["sourceRevision"] != first["sourceRevision"]
    assert selected["firstSeenRevision"] == first["sourceRevision"]
    assert exact.status_code == 409
    assert exact.json() == {"error": "stale_revision"}
    assert stale_exact.status_code == 200
    assert stale_exact.json()["historicalFocusedEvent"] is False
    assert stale_exact.json()["focusedEvent"] == selected
    assert missing.status_code == 404
    assert unqualified.status_code == 400


def test_recent_window_withholds_findings_from_incomplete_source_turn(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 1_600
    records.extend([
        {"timestamp": "2026-08-30T10:04:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "later"}},
        {"timestamp": "2026-08-30T10:04:01Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "later-call",
                     "output": json.dumps({"exit_code": 1, "output": "failed"})}},
        {"timestamp": "2026-08-30T10:04:02Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "later"}},
    ])
    path = root / "partial.jsonl"
    _write_jsonl(path, records)
    with path.open("a") as handle:
        handle.write('{"timestamp":')
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    from verdict.dashboard.app import build_recent_local_sample
    window = build_recent_local_sample(
        str(tmp_path / "verdict.db"), tenant="local", run_id=bundle.run.run_id,
    )
    assert window is not None
    assert window["turns"][-1]["eligibleForFindings"] is False
    assert window["findings"] == []
    assert any(item["status"] == "failed" for item in window["events"])


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_competing_local_rescans_keep_latest_checkpoint_and_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from hashlib import sha256
    from threading import Event, current_thread

    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 1_600
    path = root / "large.jsonl"
    _write_jsonl(path, records)
    if backend == "postgres":
        from verdict.storage.postgres import PostgresStorage

        from packages.verdict.tests._postgres_test_safety import (
            isolated_test_dsn,
            validate_test_dsn,
        )

        dsn, reason = validate_test_dsn(
            os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
        )
        if dsn is None:
            pytest.skip(reason)
    with ExitStack() as stack:
        storage = (PostgresStorage(stack.enter_context(isolated_test_dsn(dsn)))
                   if backend == "postgres" else SQLiteStorage(str(tmp_path / "verdict.db")))
        stack.callback(storage.close)
        parsed_old = Event()
        release_old = Event()
        parse = local_agents._parse_codex

        def pause_old(source, *, home):
            result = parse(source, home=home)
            if current_thread().name.startswith("older-import"):
                parsed_old.set()
                assert release_old.wait(5), "newer import did not finish"
            return result

        monkeypatch.setattr(local_agents, "_parse_codex", pause_old)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="older-import") as pool:
            older = pool.submit(capture_local_agents, storage, tenant_id="local", codex_root=root)
            assert parsed_old.wait(5), "older import did not parse"
            with path.open("a") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-08-30T10:04:00Z", "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-a"},
                }) + "\n")
            newer = capture_local_agents(storage, tenant_id="local", codex_root=root)
            release_old.set()
            older_result = older.result(timeout=5)
        assert newer.stored == 1
        assert older_result.stored == 0 and older_result.skipped == 1
        [bundle] = storage.list_agent_run_bundles("local")
        window = storage.get_recent_local_events("local", bundle.run.run_id)
        assert window is not None
        assert window["sourceRevision"] == sha256(path.read_bytes()).hexdigest()


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


def test_live_partial_final_line_preserves_complete_records_and_marks_coverage(
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
    captured, revision = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
    assert captured is not None and captured.events
    assert revision is not None and revision.partial_final_line
    assert all(event.provenance != "verdict:partial_source_line" for event in captured.events)


def test_json_format_finding_waits_for_complete_local_final_line(tmp_path: Path) -> None:
    from verdict.analysis import AnalysisPolicy, analyze_agent_run

    root = tmp_path / "codex"
    path = root / "live.jsonl"
    _write_jsonl(path, _codex_records())
    with path.open("a") as handle:
        handle.write('{"timestamp":"2026-08-30T10:02:00Z","type":"event_msg"')
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    policy = AnalysisPolicy(expected_response_format="json")
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    captured, revision = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
    assert captured is not None and revision is not None and revision.partial_final_line
    assert "response_schema_invalid" not in {
        item.code for item in analyze_agent_run(captured, policy, local_revision=revision).findings
    }
    with path.open("a") as handle:
        handle.write(',"payload":{"type":"unknown"}}\n')
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    captured, revision = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
    assert captured is not None and revision is not None and not revision.partial_final_line
    assert "response_schema_invalid" in {
        item.code for item in analyze_agent_run(captured, policy, local_revision=revision).findings
    }


def test_partial_tail_beyond_v1_does_not_hide_complete_prefix_json_failure(
    tmp_path: Path,
) -> None:
    from verdict.analysis import AnalysisPolicy, analyze_agent_run

    root = tmp_path / "codex"
    path = root / "large.jsonl"
    source = _zero_event_codex_history(1_001, "prefix-format")
    source.insert(1 + 999 * 2 + 1, {
        "timestamp": "2026-08-30T10:00:00Z", "type": "event_msg",
        "payload": {"type": "agent_message", "phase": "final_answer", "message": "not json"},
    })
    _write_jsonl(path, source)
    with path.open("a") as handle:
        handle.write('{"timestamp":"2026-08-30T10:02:00Z","type":"event_msg"')
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    captured, revision = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
    assert captured is not None and revision is not None
    assert revision.source_turn_count == 1_001 and revision.partial_final_line
    assert "response_schema_invalid" in {
        item.code for item in analyze_agent_run(
            captured, AnalysisPolicy(expected_response_format="json"),
            local_revision=revision,
        ).findings
    }


def test_partial_local_line_marks_run_recommendations_and_last_turn_incomplete(
    tmp_path: Path,
) -> None:
    import asyncio

    import httpx
    from verdict.dashboard.app import create_app

    root = tmp_path / "codex"
    path = root / "live.jsonl"
    records = [record for record in _codex_records() if
               record.get("payload", {}).get("type") != "function_call_output"]
    _write_jsonl(path, records)
    with path.open("a") as handle:
        handle.write('{"timestamp":"2026-08-30T10:02:00Z","type":"event_msg"')
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        run_id = bundle.run.run_id
        turn_id = bundle.turns[-1].turn_id
    finally:
        storage.close()
    runs = build_agent_runs_bundle(str(db), tenant="local", run_id=run_id)["runs"]
    assert len(runs) == 1
    assert not any(item["code"] == "review_missing_tool_outcomes"
                   for item in runs[0]["recommendations"])
    turn = build_agent_turn_analysis(str(db), tenant="local", run_id=run_id,
                                     turn_id=turn_id)
    assert turn["eventCoverage"]["complete"] is False
    assert turn["eventCoverage"]["sourcePartial"] is True

    async def request_app():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{db}",
                                                   tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await asyncio.gather(
                client.get(f"/api/runs/{run_id}/turns/{turn_id}/analysis"),
                client.post("/api/evaluators/preview", headers={"X-Verdict-Setup": token},
                            json={
                                "unit": "agent_turn", "runId": run_id, "turnId": turn_id,
                                "provider": "anthropic", "model": "claude-haiku-4-5",
                                "maxCalls": 1, "maxOutputTokens": 256,
                                "rubric": {"name": "poc", "version": "1", "dimensions": [
                                    {"name": "relevance", "description": "Answers the question."},
                                ]},
                            }),
            )

    turn_response, preview_response = asyncio.run(request_app())
    assert turn_response.status_code == 200
    assert turn_response.json()["eventCoverage"]["sourcePartial"] is True
    assert preview_response.status_code == 200
    assert preview_response.json()["plannedCalls"] == 0


@pytest.mark.parametrize("storage_kind", ["memory", "sqlite"])
def test_incomplete_local_tail_excludes_last_turn_from_judge_candidates(
    tmp_path: Path, storage_kind: str,
) -> None:
    from verdict.dashboard.evaluator_lab import preview_evaluation
    root = tmp_path / "codex"
    path = root / "source.jsonl"
    _write_jsonl(path, _codex_records())
    with path.open("a") as handle:
        handle.write('{"timestamp":"2026-08-30T10:02:00Z","type":"event_msg"')
    storage = (InMemoryStorage() if storage_kind == "memory" else
               SQLiteStorage(str(tmp_path / "verdict.db")))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        last = bundle.turns[-1]
        assert storage.get_agent_capture_snapshot("local", bundle.run.run_id)[1].partial_final_line
        assert storage.get_agent_turn_evaluation_candidate(
            "local", bundle.run.run_id, last.turn_id, "a" * 64,
        ) is None
        assert storage.list_agent_turn_evaluation_candidates("local", "a" * 64)[0] == []
        preview = preview_evaluation(storage, tenant_id="local", config={
            "unit": "agent_turn", "runId": bundle.run.run_id, "turnId": last.turn_id,
            "provider": "anthropic", "model": "claude-haiku-4-5", "maxCalls": 1,
            "maxOutputTokens": 256,
            "rubric": {"name": "poc", "version": "1", "dimensions": [
                {"name": "relevance", "description": "Answers the question."},
            ]},
        })
        assert preview["plannedCalls"] == 0
        assert storage.save_agent_turn_judgment_if_current(AgentTurnJudgment(
            tenant_id="local", run_id=bundle.run.run_id, turn_id=last.turn_id,
            evaluator_fingerprint="a" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(last),
            evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
            expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
            status=JudgmentStatus.ERROR, error="synthetic",
        )) == "stale"
    finally:
        storage.close()


@pytest.mark.parametrize("storage_kind", ["memory", "sqlite"])
def test_incomplete_tail_keeps_earlier_complete_turn_judge_eligible(
    tmp_path: Path, storage_kind: str,
) -> None:
    root = tmp_path / "codex"
    path = root / "source.jsonl"
    _write_jsonl(path, [*_codex_records(),
        {"timestamp": "2026-08-30T10:02:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-b"}},
        {"timestamp": "2026-08-30T10:02:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "Second task"}},
        {"timestamp": "2026-08-30T10:02:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "phase": "final_answer", "message": "Done"}},
        {"timestamp": "2026-08-30T10:02:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-b",
                     "last_agent_message": "Done"}},
    ])
    with path.open("a") as handle:
        handle.write('{"timestamp":"2026-08-30T10:02:04Z","type":"event_msg"')
    storage = (InMemoryStorage() if storage_kind == "memory" else
               SQLiteStorage(str(tmp_path / "verdict.db")))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        assert len(bundle.turns) == 2
        first, last = sorted(bundle.turns, key=lambda turn: turn.sequence)
        assert storage.get_agent_turn_evaluation_candidate(
            "local", bundle.run.run_id, first.turn_id, "a" * 64,
        ) is not None
        assert storage.get_agent_turn_evaluation_candidate(
            "local", bundle.run.run_id, last.turn_id, "a" * 64,
        ) is None
        rows, _ = storage.list_agent_turn_evaluation_candidates("local", "a" * 64)
        assert [row[0].turn_id for row in rows] == [first.turn_id]
    finally:
        storage.close()


@pytest.mark.parametrize("storage_kind", ["memory", "sqlite"])
def test_capture_limit_turn_cannot_enter_native_turn_judge(
    tmp_path: Path, storage_kind: str,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 1_600
    _write_jsonl(root / "large.jsonl", records)
    storage = (InMemoryStorage() if storage_kind == "memory" else
               SQLiteStorage(str(tmp_path / "verdict.db")))
    try:
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.stored == 1 and summary.event_limited_runs == 1
        [bundle] = storage.list_agent_run_bundles("local")
        turn = bundle.turns[0]
        assert any(event.provenance == "verdict:capture_limit" for event in bundle.events)
        assert storage.get_agent_turn_evaluation_candidate(
            "local", bundle.run.run_id, turn.turn_id, "a" * 64,
        ) is None
        assert storage.list_agent_turn_evaluation_candidates("local", "a" * 64)[0] == []
        assert storage.save_agent_turn_judgment_if_current(AgentTurnJudgment(
            tenant_id="local", run_id=bundle.run.run_id, turn_id=turn.turn_id,
            evaluator_fingerprint="a" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(turn),
            evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
            expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
            status=JudgmentStatus.ERROR, error="synthetic",
        )) == "stale"
    finally:
        storage.close()


@pytest.mark.parametrize("storage_kind", ["memory", "sqlite", "postgres"])
def test_capture_limit_marker_belongs_to_first_turn_with_omitted_event(
    tmp_path: Path, storage_kind: str,
) -> None:
    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 746  # Two events per command call.
    unknown_result = {
        "timestamp": "2026-08-30T10:01:06Z", "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "unknown", "output": "ok"},
    }
    records.insert(-1, unknown_result)  # One event: first turn has exactly 1,499.
    records += [
        {"timestamp": "2026-08-30T10:02:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-b"}},
        {**unknown_result, "timestamp": "2026-08-30T10:02:01Z"},
        {"timestamp": "2026-08-30T10:02:02Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-b"}},
    ]
    _write_jsonl(root / "boundary.jsonl", records)
    with ExitStack() as stack:
        if storage_kind == "postgres":
            from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
            from verdict.storage.postgres import PostgresStorage

            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            storage = PostgresStorage(stack.enter_context(isolated_test_dsn(dsn)))
        else:
            storage = (InMemoryStorage() if storage_kind == "memory" else
                       SQLiteStorage(str(tmp_path / "verdict.db")))
        stack.callback(storage.close)
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        first, second = sorted(bundle.turns, key=lambda turn: turn.sequence)
        marker = [event for event in bundle.events
                  if event.provenance == "verdict:capture_limit"]
        assert len(marker) == 1
        assert marker[0].turn_id == second.turn_id
        assert marker[0].sequence == 0
        assert storage.get_agent_turn_evaluation_candidate(
            "local", bundle.run.run_id, first.turn_id, "a" * 64,
        ) is not None
        assert storage.get_agent_turn_evaluation_candidate(
            "local", bundle.run.run_id, second.turn_id, "a" * 64,
        ) is None
        if storage_kind == "sqlite":
            database = str(tmp_path / "verdict.db")
            first_analysis = build_agent_turn_analysis(
                database, tenant="local", run_id=bundle.run.run_id,
                turn_id=first.turn_id,
            )
            second_analysis = build_agent_turn_analysis(
                database, tenant="local", run_id=bundle.run.run_id,
                turn_id=second.turn_id,
            )
            assert first_analysis["eventCoverage"]["sourcePartial"] is False
            assert second_analysis["eventCoverage"]["sourcePartial"] is True


def test_local_source_checkpoint_changes_published_insights_identity(
    tmp_path: Path,
) -> None:
    from verdict.dashboard.analysis_service import run_analysis
    from verdict.dashboard.app import build_agent_insights_bundle

    root = tmp_path / "codex"
    path = root / "source.jsonl"
    _write_jsonl(path, _codex_records())
    db = tmp_path / "verdict.db"
    url = f"sqlite:///{db}"
    storage = SQLiteStorage(str(db))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    finally:
        storage.close()
    build = lambda: build_agent_insights_bundle(  # noqa: E731 - same builder for both cycles
        url, tenant="local", _include_input_fingerprint=True,
    )
    initial = run_analysis(url, tenant="local", build=build)
    prior_fingerprint = build()["_analysisInputFingerprint"]
    source_stat = path.stat()
    with path.open("a") as handle:
        handle.write('{"timestamp":"2026-08-30T10:02:00Z","type":"event_msg"')
    os.utime(path, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    storage = SQLiteStorage(str(db))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    finally:
        storage.close()
    changed = build()
    assert changed["_analysisInputFingerprint"] != prior_fingerprint
    assert any(item["code"] == "source_capture_partial"
               for item in changed.get("findings", [])) or "source_capture_partial" in str(changed)
    published = run_analysis(url, tenant="local", build=build)
    assert published["analysisState"]["analysisId"] != initial["analysisState"]["analysisId"]


def test_legacy_coverage_bootstrap_changes_insights_fingerprint_without_source_edit(
    tmp_path: Path,
) -> None:
    from verdict.dashboard.app import build_agent_insights_bundle

    root = tmp_path / "codex"
    _write_jsonl(root / "source.jsonl", _codex_records())
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    storage._conn.execute(
        "UPDATE import_sources SET local_source_turn_count=NULL,"
        "local_source_event_count=NULL,local_v1_turn_count=NULL,"
        "local_v1_event_count=NULL WHERE tenant_id=?",
        ("local",),
    )
    storage._conn.commit()
    before = build_agent_insights_bundle(
        str(db), tenant="local", _include_input_fingerprint=True,
    )
    assert before["scope"]["partialRuns"] == 1
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    after = build_agent_insights_bundle(
        str(db), tenant="local", _include_input_fingerprint=True,
    )
    assert after["scope"]["partialRuns"] == 0
    assert after["_analysisInputFingerprint"] != before["_analysisInputFingerprint"]


@pytest.mark.parametrize("partial_day_index", [2, 3])
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_incomplete_local_day_withholds_token_comparison_until_rescan(
    tmp_path: Path, partial_day_index: int, backend: str,
) -> None:
    import asyncio

    import httpx
    from verdict.dashboard.app import build_agent_usage_bundle, create_app
    from verdict.dashboard.day_explanation import _context

    root = tmp_path / "codex"
    partial_path = None
    for index in range(4):
        encoded = json.dumps(_codex_records())
        encoded = encoded.replace("2026-08-30", f"2026-10-{10 + index:02d}")
        encoded = encoded.replace("codex-session-1", f"codex-session-{index}")
        encoded = encoded.replace("turn-a", f"turn-{index}")
        path = root / f"day-{index}.jsonl"
        _write_jsonl(path, json.loads(encoded))
        if index == partial_day_index:
            partial_path = path
    assert partial_path is not None
    with partial_path.open("a") as handle:
        handle.write('{"timestamp":"2026-10-13T10:02:00Z","type":"event_msg"')
    with ExitStack() as stack:
        if backend == "postgres":
            from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
            from verdict.storage.postgres import PostgresStorage

            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            storage_url = stack.enter_context(isolated_test_dsn(dsn))
            storage = PostgresStorage(storage_url)
        else:
            db = tmp_path / "verdict.db"
            storage_url = f"sqlite:///{db}"
            storage = SQLiteStorage(str(db))
        stack.callback(storage.close)
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 4
        report = build_agent_usage_bundle(
            storage_url, tenant="local", day="2026-10-13",
            now=datetime.fromisoformat("2026-10-14T12:00:00+00:00"),
        )
        assert report["dayExplanation"]["totalTokens"] == 24
        assert report["dayExplanation"]["comparison"] is None
        assert report["dayExplanation"]["sourcePartial"] is True
        assert report["dayExplanation"]["referenceSourcePartial"] is True
        assert _context(report)["sourcePartial"] is True

        async def read_usage():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=create_app(storage=storage_url, tenant_id="local")),
                base_url="http://testserver",
            ) as client:
                return await client.get("/api/usage?day=2026-10-13")

        response = asyncio.run(read_usage())
        assert response.status_code == 200
        assert response.json()["dayExplanation"]["comparison"] is None
        with partial_path.open("a") as handle:
            handle.write('}\n')
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 4
        completed = build_agent_usage_bundle(
            storage_url, tenant="local", day="2026-10-13",
            now=datetime.fromisoformat("2026-10-14T12:00:00+00:00"),
        )
        assert completed["dayExplanation"]["comparison"] is not None
        assert completed["dayExplanation"]["sourcePartial"] is False


def test_prior_turn_evaluation_is_hidden_when_local_tail_becomes_partial(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    path = root / "source.jsonl"
    _write_jsonl(path, _codex_records())
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        turn = bundle.turns[0]
        assert storage.save_agent_turn_judgment_if_current(AgentTurnJudgment(
            tenant_id="local", run_id=bundle.run.run_id, turn_id=turn.turn_id,
            evaluator_fingerprint="a" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(turn),
            evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
            expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
            status=JudgmentStatus.ERROR, error="synthetic",
        )) == "saved"
        before = build_agent_run_detail(str(db), tenant="local", run_id=bundle.run.run_id)
        assert before["turns"][0]["evaluation"] is not None
        with path.open("a") as handle:
            handle.write('{"timestamp":"2026-08-30T10:02:00Z","type":"event_msg"')
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.stored == 1
        after = build_agent_run_detail(str(db), tenant="local", run_id=bundle.run.run_id)
        assert after["turns"][0]["evaluation"] is None
    finally:
        storage.close()


def test_historical_turn_score_is_hidden_when_capture_limit_omits_events(
    tmp_path: Path,
) -> None:
    from verdict.agent_judgment import agent_turn_judgment_to_json

    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 1_600
    _write_jsonl(root / "large.jsonl", records)
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).event_limited_runs == 1
        [bundle] = storage.list_agent_run_bundles("local")
        turn = bundle.turns[0]
        historical = AgentTurnJudgment(
            tenant_id="local", run_id=bundle.run.run_id, turn_id=turn.turn_id,
            evaluator_fingerprint="a" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(turn),
            evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
            expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
            status=JudgmentStatus.ERROR, error="historical",
        )
        storage._conn.execute(
            "INSERT INTO agent_turn_judgments "
            "(tenant_id,run_id,turn_id,evaluator_fingerprint,evidence_fingerprint,"
            "status,evaluated_at,result_json) VALUES (?,?,?,?,?,?,?,?)",
            ("local", bundle.run.run_id, turn.turn_id, "a" * 64,
             historical.evidence_fingerprint, historical.status.value,
             historical.evaluated_at.isoformat(), agent_turn_judgment_to_json(historical)),
        )
        storage._conn.commit()
        detail = build_agent_run_detail(str(db), tenant="local", run_id=bundle.run.run_id)
        assert detail["turns"][0]["evaluation"] is None
    finally:
        storage.close()


@pytest.mark.parametrize("source_kind", ["codex", "claude"])
def test_completed_local_line_clears_transient_partial_marker_on_rescan(
    tmp_path: Path, source_kind: str,
) -> None:
    root = tmp_path / source_kind
    path = root / "live.jsonl"
    records = _codex_records() if source_kind == "codex" else _claude_records()
    _write_jsonl(path, records)
    completed_line = (
        {"timestamp": "2026-08-30T10:02:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-b"}}
        if source_kind == "codex" else
        {"timestamp": "2026-08-30T11:02:00Z", "type": "user", "uuid": "user-2",
         "sessionId": "claude-session-1", "message": {"content": "synthetic next request"}}
    )
    serialized = json.dumps(completed_line) + "\n"
    with path.open("a") as handle:
        handle.write(serialized[:30])
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    kwargs = {f"{source_kind}_root" if source_kind == "codex" else "claude_root": root}

    capture_local_agents(storage, tenant_id="local", **kwargs)
    with path.open("a") as handle:
        handle.write(serialized[30:])
    assert capture_local_agents(storage, tenant_id="local", **kwargs).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    assert not any(event.provenance == "verdict:partial_source_line" for event in bundle.events)


@pytest.mark.parametrize("storage_kind", ["memory", "buffered", "sqlite"])
def test_legacy_partial_marker_does_not_claim_completed_event_sequence(
    tmp_path: Path, storage_kind: str,
) -> None:
    root = tmp_path / "codex"
    path = root / "live.jsonl"
    _write_jsonl(path, [*_codex_records(), {
        "timestamp": "2026-08-30T10:02:00Z", "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "turn-b"},
    }])
    seed = SQLiteStorage(str(tmp_path / "seed.db"))
    try:
        assert capture_local_agents(seed, tenant_id="local", codex_root=root).stored == 1
        [bundle] = seed.list_agent_run_bundles("local")
        traces = tuple(seed.list_traces(tenant_id="local"))
    finally:
        seed.close()
    last = max(bundle.turns, key=lambda turn: turn.sequence)
    assert not any(event.turn_id == last.turn_id for event in bundle.events)
    marker = AgentEvent(
        event_id="legacy-partial-marker", turn_id=last.turn_id, sequence=0,
        occurred_at=last.started_at, event_type=AgentEventType.CONTEXT,
        status=ExecutionStatus.UNKNOWN,
        provenance="verdict:partial_source_line",
        attributes={"name": "partial_source_line"},
    )
    if storage_kind == "memory":
        storage = InMemoryStorage()
    elif storage_kind == "buffered":
        storage = BufferedStorage(InMemoryStorage())
    else:
        storage = SQLiteStorage(str(tmp_path / "upgrade.db"))
    try:
        AgentCaptureService(storage).capture(replace(bundle, events=(*bundle.events, marker)),
                                             traces=traces)
        with path.open("a") as handle:
            handle.write(json.dumps({
                "timestamp": "2026-08-30T10:02:01Z", "type": "response_item",
                "payload": {"type": "function_call", "name": "exec_command",
                            "call_id": "call-2", "arguments": "{}"},
            }) + "\n")
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.stored == 1
        [current] = storage.list_agent_run_bundles("local")
        assert all(event.event_id != marker.event_id for event in current.events)
        assert any(event.turn_id == last.turn_id and event.event_type is AgentEventType.TOOL_CALL
                   for event in current.events)
    finally:
        storage.close()


def test_incomplete_local_line_blocks_selected_ai_until_completed_append(
    tmp_path: Path,
) -> None:
    from verdict.dashboard.evidence_explanation import preview_explanation
    from verdict.dashboard.scoped_evaluation import preview_scoped

    root = tmp_path / "codex"
    path = root / "live.jsonl"
    _write_jsonl(path, _codex_records())
    next_line = json.dumps({
        "timestamp": "2026-08-30T10:02:00Z", "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "turn-b"},
    }) + "\n"
    with path.open("a") as handle:
        handle.write(next_line[:35])
    storage_path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(storage_path))
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    [bundle] = storage.list_agent_run_bundles("local")
    call = next(event for event in bundle.events
                if event.event_type is AgentEventType.TOOL_CALL)
    config = {"scope": "event", "runId": bundle.run.run_id,
              "eventId": call.event_id, "provider": "anthropic", "focus": "failure"}
    first = preview_explanation(
        storage, storage_url=f"sqlite:///{storage_path}", tenant="local", config=config,
    )
    assert first["plannedCalls"] == 0
    assert first["evidencePreview"]["reason"] == "source_capture_partial"
    session_preview = preview_scoped(
        storage, tenant="local",
        storage_url=f"sqlite:///{storage_path}",
        config={
            "scope": "session", "sessionId": bundle.run.session_id,
            "sourceKind": "codex", "provider": "anthropic",
            "model": "claude-haiku-4-5", "maxOutputTokens": 256,
            "rubric": {"name": "task_review", "version": "1", "dimensions": [{
                "name": "completion", "description": "Review the recorded task evidence.",
            }]},
        },
    )
    assert session_preview["plannedCalls"] == 0
    assert session_preview["unavailableReason"] == "source_capture_partial"

    with path.open("a") as handle:
        handle.write(next_line[35:])
    assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    second = preview_explanation(
        storage, storage_url=f"sqlite:///{storage_path}", tenant="local", config=config,
    )
    assert second["plannedCalls"] == 1
    storage.close()


@pytest.mark.parametrize("storage_kind", ["sqlite", "memory"])
def test_local_revision_rejects_source_change_without_mutating_committed_capture(
    tmp_path: Path, storage_kind: str,
) -> None:
    root = tmp_path / "codex"
    path = root / "source.jsonl"
    _write_jsonl(path, _codex_records())
    storage = (SQLiteStorage(str(tmp_path / "verdict.db"))
               if storage_kind == "sqlite" else InMemoryStorage())
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        committed = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
        original = path.read_bytes()

        path.write_bytes(original[:-1])
        truncated = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert truncated.stored == 0
        assert truncated.skip_reasons == {"local_source_conflict": 1}
        assert storage.get_agent_capture_snapshot("local", bundle.run.run_id) == committed

        path.write_bytes(original.replace(b"/Users/example/project", b"/Users/example/projecT", 1))
        changed = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert changed.stored == 0
        assert changed.skip_reasons == {"local_source_conflict": 1}
        assert storage.get_agent_capture_snapshot("local", bundle.run.run_id) == committed
    finally:
        storage.close()


def test_local_revision_rejects_source_change_between_parse_and_commit(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "codex"
    path = root / "source.jsonl"
    _write_jsonl(path, _codex_records())
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        committed = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
        with path.open("ab") as handle:
            handle.write(b"\n")
        original_store = storage._replace_local_agent_capture

        def changed_after_parse(*args, **kwargs):
            with path.open("ab") as handle:
                handle.write(b"\n")
            return original_store(*args, **kwargs)

        monkeypatch.setattr(storage, "_replace_local_agent_capture", changed_after_parse)
        stale = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert stale.stored == 0
        assert stale.skip_reasons == {"local_source_changed": 1}
        assert storage.get_agent_capture_snapshot("local", bundle.run.run_id) == committed
        monkeypatch.setattr(storage, "_replace_local_agent_capture", original_store)
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    finally:
        storage.close()


def test_local_rewrite_cannot_prove_prior_event_content_with_capture_disabled(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    path = root / "source.jsonl"
    records = _codex_records()
    records[3]["payload"] = {"type": "item_completed", "item": {"type": "unknown"}}
    records[6]["payload"] = {"type": "unknown"}
    records[-1]["payload"]["last_agent_message"] = ""
    _write_jsonl(path, records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        assert all(turn.user_request_redacted is None and
                   turn.final_response_redacted is None for turn in bundle.turns)
        assert any(event.privacy_classification is PrivacyClassification.REDACTED
                   for event in bundle.events)
        committed = storage.get_agent_capture_snapshot("local", bundle.run.run_id)
        path.write_bytes(b"\n" + path.read_bytes())
        changed = capture_local_agents(
            storage, tenant_id="local", codex_root=root, capture_content=False,
        )
        assert changed.stored == 0
        assert changed.skip_reasons == {"local_source_conflict": 1}
        assert storage.get_agent_capture_snapshot("local", bundle.run.run_id) == committed
    finally:
        storage.close()


def test_postgres_local_partial_checkpoint_clears_on_completed_append(
    tmp_path: Path,
) -> None:
    from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
    from verdict.storage.postgres import PostgresStorage

    dsn, reason = validate_test_dsn(
        os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
    )
    if dsn is None:
        pytest.skip(reason)
    root = tmp_path / "codex"
    path = root / "source.jsonl"
    _write_jsonl(path, _codex_records())
    final_line = json.dumps({
        "timestamp": "2026-08-30T10:02:00Z", "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "next-turn"},
    }) + "\n"
    with path.open("a") as handle:
        handle.write(final_line[:25])
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
            [bundle] = storage.list_agent_run_bundles("local")
            prior, revision = storage.get_agent_capture_snapshot(
                "local", bundle.run.run_id,
            )
            assert revision is not None and revision.partial_final_line
            assert prior is not None
            with path.open("a") as handle:
                handle.write(final_line[25:])
            assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
            current, revision = storage.get_agent_capture_snapshot(
                "local", bundle.run.run_id,
            )
            assert revision is not None and not revision.partial_final_line
            assert current is not None and len(current.turns) == len(prior.turns) + 1
            assert all(event.provenance != "verdict:partial_source_line"
                       for event in current.events)
        finally:
            storage.close()


def test_postgres_partial_local_tail_excludes_judge_candidate_and_write(
    tmp_path: Path,
) -> None:
    from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
    from verdict.storage.postgres import PostgresStorage

    dsn, reason = validate_test_dsn(
        os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
    )
    if dsn is None:
        pytest.skip(reason)
    root = tmp_path / "codex"
    path = root / "source.jsonl"
    _write_jsonl(path, _codex_records())
    with path.open("a") as handle:
        handle.write('{"timestamp":"2026-08-30T10:02:00Z","type":"event_msg"')
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
            [bundle] = storage.list_agent_run_bundles("local")
            turn = bundle.turns[-1]
            assert storage.get_agent_turn_evaluation_candidate(
                "local", bundle.run.run_id, turn.turn_id, "a" * 64,
            ) is None
            assert storage.list_agent_turn_evaluation_candidates("local", "a" * 64)[0] == []
            assert storage.save_agent_turn_judgment_if_current(AgentTurnJudgment(
                tenant_id="local", run_id=bundle.run.run_id, turn_id=turn.turn_id,
                evaluator_fingerprint="a" * 64,
                evidence_fingerprint=turn_evidence_fingerprint(turn),
                evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
                expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
                status=JudgmentStatus.ERROR, error="synthetic",
            )) == "stale"

            limit_root = tmp_path / "codex-limit"
            records = _codex_records()
            records[4:4] = [records[4]] * 1_600
            _write_jsonl(limit_root / "large.jsonl", records)
            assert capture_local_agents(
                storage, tenant_id="local", codex_root=limit_root,
            ).event_limited_runs == 1
            limited = next(bundle for bundle in storage.list_agent_run_bundles("local")
                           if any(event.provenance == "verdict:capture_limit"
                                  for event in bundle.events))
            limited_turn = limited.turns[0]
            assert storage.get_agent_turn_evaluation_candidate(
                "local", limited.run.run_id, limited_turn.turn_id, "a" * 64,
            ) is None
            assert storage.save_agent_turn_judgment_if_current(AgentTurnJudgment(
                tenant_id="local", run_id=limited.run.run_id, turn_id=limited_turn.turn_id,
                evaluator_fingerprint="a" * 64,
                evidence_fingerprint=turn_evidence_fingerprint(limited_turn),
                evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
                expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
                status=JudgmentStatus.ERROR, error="synthetic",
            )) == "stale"
        finally:
            storage.close()


@pytest.mark.parametrize("turn_count", [1_000, 1_001])
def test_large_local_import_does_not_store_a_run_unreadable_by_v1_port(
    tmp_path: Path, turn_count: int,
) -> None:
    from verdict.read_port import StorageVerdictReadPort

    root = tmp_path / "codex"
    path = root / "many-turns.jsonl"
    records: list[dict[str, object]] = [{
        "timestamp": "2026-08-30T10:00:00Z", "type": "session_meta",
        "payload": {"id": "synthetic-many-turns"},
    }]
    for index in range(turn_count):
        for event_type in ("task_started", "task_complete"):
            records.append({
                "timestamp": "2026-08-30T10:00:00Z", "type": "event_msg",
                "payload": {"type": event_type, "turn_id": f"turn-{index}"},
            })
    _write_jsonl(path, records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))

    summary = capture_local_agents(storage, tenant_id="local", codex_root=root)

    if turn_count == 1_000:
        assert summary.stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        assert StorageVerdictReadPort(storage).get_agent_run(
            tenant_id="local", run_id=bundle.run.run_id,
        ) is not None
    else:
        from verdict.dashboard.app import build_agent_insights_bundle, build_agent_runs_bundle
        from verdict.read_port import VerdictReadError

        assert summary.stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        assert len(bundle.turns) == 1_000
        with pytest.raises(VerdictReadError, match="invalid_read_model"):
            StorageVerdictReadPort(storage).get_agent_run(
                tenant_id="local", run_id=bundle.run.run_id,
            )
        sample = storage.get_recent_local_events("local", bundle.run.run_id)
        assert sample is not None and sample["sourceTurnCount"] == 1_001
        if sample["sourceEventCount"] == 0:
            assert sample["firstOrdinal"] is None
            assert sample["lastOrdinal"] is None
        [dashboard_run] = build_agent_runs_bundle(
            str(tmp_path / "verdict.db"), tenant="local", run_id=bundle.run.run_id,
        )["runs"]
        assert dashboard_run["sourcePartial"] is True
        insights = build_agent_insights_bundle(str(tmp_path / "verdict.db"), tenant="local")
        assert insights["scope"]["partialRuns"] == 0
        assert insights["scope"]["complete"] is True
        assert insights["reliability"]["runOutcomes"] == {"unknown": 1}
        assert insights["sourceActivity"][0]["partialRuns"] == 0
        assert insights["sourceActivity"][0]["turns"] == 1_001


def test_admitted_local_history_retains_every_nonprefix_event(tmp_path: Path) -> None:
    """The source transaction must own middle events, not only the tail 50."""
    root = tmp_path / "codex"
    records = _codex_records()
    records[4:4] = [records[4]] * 1_600
    _write_jsonl(root / "many-events.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [source] = storage._conn.execute(
            "SELECT local_source_event_count,local_v1_event_count FROM import_sources"
        ).fetchall()
        [run] = storage.list_agent_run_bundles("local")
        [nonprefix_count] = storage._conn.execute(
            "SELECT count(*) FROM local_recent_event_facts WHERE tenant_id=? AND run_id=?",
            ("local", run.run.run_id),
        ).fetchone()
        assert source["local_source_event_count"] > source["local_v1_event_count"] + 50
        assert nonprefix_count == (
            source["local_source_event_count"] - source["local_v1_event_count"]
        )
    finally:
        storage.close()


def test_admitted_history_event_page_reaches_middle_and_rejects_stale_revision(
    tmp_path: Path,
) -> None:
    import asyncio

    import httpx
    from verdict.dashboard.app import create_app

    root = tmp_path / "codex"
    records = _codex_records("synthetic-page")
    records[4:4] = [records[4]] * 1_600
    _write_jsonl(root / "large.jsonl", records)
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        revision = storage.get_recent_local_events("local", bundle.run.run_id)[
            "sourceRevision"
        ]
        [middle_cursor_id] = storage._conn.execute(
            "SELECT event_id FROM local_recent_event_facts WHERE tenant_id=? "
            "AND run_id=? AND source_ordinal=1499",
            ("local", bundle.run.run_id),
        ).fetchone()
    finally:
        storage.close()

    async def read():
        app = create_app(storage=f"sqlite:///{db}", tenant_id="local")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            page = await client.get(f"/api/runs/{bundle.run.run_id}/local-events",
                                    params={"revision": revision, "limit": 100})
            if page.status_code != 200:
                return page, None, None, None
            cursor = page.json()["nextCursor"]
            middle = await client.get(f"/api/runs/{bundle.run.run_id}/local-events",
                                      params={"revision": revision, "limit": 100,
                                              "after_ordinal": 1499,
                                              "after_event_id": middle_cursor_id})
            stale = await client.get(f"/api/runs/{bundle.run.run_id}/local-events",
                                     params={"revision": "0" * 64, "limit": 100,
                                             "after_ordinal": cursor["sourceOrdinal"],
                                             "after_event_id": cursor["eventId"]})
            forged = await client.get(f"/api/runs/{bundle.run.run_id}/local-events",
                                      params={"revision": revision, "limit": 100,
                                              "after_ordinal": 1499,
                                              "after_event_id": "event_" + "0" * 32})
            return page, middle, stale, forged

    page, middle, stale, forged = asyncio.run(read())
    assert page.status_code == 200
    assert page.json()["coverage"] == "complete_recorded"
    assert len(page.json()["events"]) == 100
    assert middle.status_code == 200
    assert middle.json()["events"][0]["sourceOrdinal"] > 1499
    assert stale.status_code == 409
    assert forged.status_code == 409

    async def foreign_read():
        app = create_app(storage=f"sqlite:///{db}", tenant_id="foreign")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(f"/api/runs/{bundle.run.run_id}/local-events",
                                    params={"revision": revision})

    assert asyncio.run(foreign_read()).status_code == 404
    with sqlite3.connect(db) as connection:
        connection.execute(
            "DELETE FROM local_recent_event_facts WHERE tenant_id=? AND run_id=? "
            "AND source_ordinal=1499", ("local", bundle.run.run_id),
        )
    broken, _, _, _ = asyncio.run(read())
    assert broken.status_code == 503


def test_live_postgres_full_local_event_page_is_tenant_scoped(tmp_path: Path) -> None:
    import asyncio

    import httpx
    from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
    from verdict.dashboard.app import create_app
    from verdict.storage import PostgresStorage

    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    root = tmp_path / "codex"
    records = _codex_records("synthetic-pg-page")
    records[4:4] = [records[4]] * 1_600
    _write_jsonl(root / "large.jsonl", records)
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
            [bundle] = storage.list_agent_run_bundles("local")
            revision = storage.get_recent_local_events("local", bundle.run.run_id)[
                "sourceRevision"
            ]
        finally:
            storage.close()

        async def read(tenant):
            app = create_app(storage=isolated, tenant_id=tenant)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.get(f"/api/runs/{bundle.run.run_id}/local-events",
                                        params={"revision": revision, "limit": 100})

        local = asyncio.run(read("local"))
        foreign = asyncio.run(read("foreign"))
        assert local.status_code == 200
        assert local.json()["coverage"] == "complete_recorded"
        assert local.json()["sourceEventCount"] > 1_500
        assert foreign.status_code == 404


def test_claude_fanout_within_admission_keeps_complete_recorded_events(
    tmp_path: Path,
) -> None:
    from verdict.dashboard.app import build_full_local_event_page

    root = tmp_path / "claude"
    records = _claude_records()[:1]
    records.append({
        "timestamp": "2026-08-30T11:00:01Z", "type": "assistant",
        "uuid": "assistant-many", "sessionId": "claude-session-1",
        "message": {
            "id": "msg-many", "model": "claude-sonnet-4-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 3},
            "content": [
                {"type": "tool_use", "id": f"tool-{index}", "name": "Bash",
                 "input": {"command": "true"}}
                for index in range(800)
            ],
        },
    })
    _write_jsonl(root / "many.jsonl", records)
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    try:
        assert capture_local_agents(storage, tenant_id="local", claude_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        [source] = storage._conn.execute(
            "SELECT local_source_event_count,local_projection_complete,local_full_sha256 "
            "FROM import_sources"
        ).fetchall()
        assert source[0] > 1_500 and source[1] == 1
        page = build_full_local_event_page(
            f"sqlite:///{db}", tenant="local", run_id=bundle.run.run_id,
            revision=source[2], limit=100,
        )
        assert page["coverage"] == "complete_recorded"
        assert page["sourceEventCount"] == source[0]
    finally:
        storage.close()


def test_full_local_page_checks_tail_turn_payload_identity(tmp_path: Path) -> None:
    import asyncio

    import httpx
    from verdict.dashboard.app import create_app

    root = tmp_path / "codex"
    _write_jsonl(root / "many-turns.jsonl",
                 _zero_event_codex_history(1_001, "synthetic-tail-integrity"))
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        revision = storage.get_recent_local_events("local", bundle.run.run_id)[
            "sourceRevision"
        ]
    finally:
        storage.close()

    async def read():
        app = create_app(storage=f"sqlite:///{db}", tenant_id="local")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(f"/api/runs/{bundle.run.run_id}/local-events",
                                    params={"revision": revision})

    assert asyncio.run(read()).json()["coverage"] == "complete_recorded"
    with sqlite3.connect(db) as connection:
        [payload] = connection.execute(
            "SELECT payload_json FROM local_tail_turn_facts"
        ).fetchone()
        changed = json.loads(payload)
        changed["run_id"] = "foreign-run"
        connection.execute(
            "UPDATE local_tail_turn_facts SET payload_json=?", (json.dumps(changed),)
        )
    assert asyncio.run(read()).status_code == 503


def test_admitted_1001st_turn_is_retained_without_event_marker(tmp_path: Path) -> None:
    """Turn coverage cannot be inferred from a V1 event-limit marker."""
    root = tmp_path / "codex"
    records: list[dict[str, object]] = [{
        "timestamp": "2026-08-30T10:00:00Z", "type": "session_meta",
        "payload": {"id": "synthetic-tail-turn"},
    }]
    for index in range(1_001):
        records.extend({
            "timestamp": "2026-08-30T10:00:00Z", "type": "event_msg",
            "payload": {"type": kind, "turn_id": f"turn-{index}"},
        } for kind in ("task_started", "task_complete"))
    _write_jsonl(root / "many-turns.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [run] = storage.list_agent_run_bundles("local")
        assert len(run.turns) == 1_000
        assert all(event.provenance != "verdict:capture_limit" for event in run.events)
        [tail_turn_count] = storage._conn.execute(
            "SELECT count(*) FROM local_tail_turn_facts WHERE tenant_id=? AND run_id=?",
            ("local", run.run.run_id),
        ).fetchone()
        assert tail_turn_count == 1
    finally:
        storage.close()


def test_admitted_tail_turn_tokens_appear_in_day_usage(tmp_path: Path) -> None:
    from verdict.dashboard.app import build_agent_usage_bundle

    root = tmp_path / "codex"
    records = _zero_event_codex_history(1_001, "synthetic-tail-usage")
    records.insert(-1, {
        "timestamp": "2026-08-30T10:00:01Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 20, "output_tokens": 4,
                                 "total_tokens": 24},
            "total_token_usage": {"input_tokens": 20, "output_tokens": 4,
                                  "total_tokens": 24},
        }},
    })
    records.insert(-1, {
        "timestamp": "2026-08-30T10:00:01Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 20, "output_tokens": 4,
                                 "total_tokens": 24},
            "total_token_usage": {"input_tokens": 40, "output_tokens": 8,
                                  "total_tokens": 48},
        }},
    })
    _write_jsonl(root / "many-turns.jsonl", records)
    url = f"sqlite:///{tmp_path / 'verdict.db'}"
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [attempt_complete] = storage._conn.execute(
            "SELECT projection_complete FROM local_source_scan_attempts"
        ).fetchone()
        assert attempt_complete == 1
        report = build_agent_usage_bundle(
            url, tenant="local", day="2026-08-30",
            now=datetime.fromisoformat("2026-08-31T12:00:00+00:00"),
        )
        day = next(item for item in report["days"] if item["day"] == "2026-08-30")
        assert day["turns"] == 1_001
        assert day["totalTokens"] == 24
        assert day["sourcePartial"] is False
        assert report["window"]["modelEvidenceComplete"] is False
        [tail] = storage._conn.execute(
            "SELECT payload_json FROM local_tail_turn_facts"
        ).fetchone()
        altered = json.loads(tail)
        altered["started_at"] = "2026-08-30T10:00:02+00:00"
        storage._conn.execute(
            "UPDATE local_tail_turn_facts SET payload_json=?",
            (json.dumps(altered),),
        )
        damaged = build_agent_usage_bundle(
            url, tenant="local", day="2026-08-30",
            now=datetime.fromisoformat("2026-08-31T12:00:00+00:00"),
        )
        assert next(item for item in damaged["days"] if item["day"] == "2026-08-30")[
            "sourcePartial"
        ] is True
    finally:
        storage.close()


def test_admitted_middle_failure_counts_in_complete_issue_day(tmp_path: Path) -> None:
    import asyncio

    import httpx
    from verdict.dashboard.app import build_agent_usage_bundle, create_app

    root = tmp_path / "codex"
    records = _codex_records("synthetic-middle-issue")
    repeated_call = records[4]
    records[4:4] = [repeated_call] * 1_600
    records.insert(1550, {
        "timestamp": "2026-08-30T10:01:04Z", "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "call-1",
                    "output": '{"exit_code":1,"output":"synthetic middle failure"}'},
    })
    _write_jsonl(root / "large.jsonl", records)
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        sample = storage.get_recent_local_events("local", bundle.run.run_id)
        prefix_ids = {event.event_id for event in bundle.events}
        recent_ids = {event["eventId"] for event in sample["events"]}
        middle_failures = list(storage._conn.execute(
            "SELECT event_id FROM local_recent_event_facts WHERE provenance="
            "'codex:response_item_output'"
        ))
        assert any(row[0] not in prefix_ids | recent_ids for row in middle_failures)
        report = build_agent_usage_bundle(
            f"sqlite:///{db}", tenant="local", day="2026-08-30",
            now=datetime.fromisoformat("2026-08-31T12:00:00+00:00"),
        )
        assert report["selectedDayIssues"]["complete"] is True
        assert report["selectedDayIssues"]["count"] >= 2
        async def read(tenant):
            app = create_app(storage=f"sqlite:///{db}", tenant_id=tenant)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.get(f"/api/runs/{bundle.run.run_id}/recorded-analysis")

        own = asyncio.run(read("local"))
        assert own.status_code == 200
        assert own.json()["coverage"] == "complete_recorded"
        assert own.json()["metrics"]["tool_calls"] >= 1_600
        assert any(item["code"] == "review_repeated_tool_failures"
                   and item["status"] == "suggested"
                   for item in own.json()["recommendations"])
        [command_finding] = [finding for finding in own.json()["findings"]
                             if finding["code"] == "command_failed"]
        assert command_finding["evidenceEventIds"]
        async def evidence_page():
            app = create_app(storage=f"sqlite:///{db}", tenant_id="local")
            cursor = command_finding["firstEvidenceCursor"] or {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.get(
                    f"/api/runs/{bundle.run.run_id}/local-events",
                    params={"revision": own.json()["sourceRevision"], "limit": 100,
                            **({"after_ordinal": cursor["sourceOrdinal"],
                                "after_event_id": cursor["eventId"]} if cursor else {})},
                )
        page = asyncio.run(evidence_page())
        assert page.status_code == 200
        assert page.json()["events"][0]["eventId"] == command_finding["evidenceEventIds"][0]
        assert asyncio.run(read("foreign")).status_code == 404
    finally:
        storage.close()


def test_admitted_middle_failure_reaches_activated_issue_monitor(tmp_path: Path) -> None:
    import asyncio
    from datetime import timedelta, timezone

    import httpx
    import verdict.dashboard.app as dashboard_app
    from verdict.dashboard.app import build_agent_insights_bundle, create_app
    from verdict.dashboard.control_plane import ControlStore
    from verdict.dashboard.issue_notices import IssueNotices
    from verdict.service import run_cycle

    root = tmp_path / "codex"
    path = tmp_path / "verdict.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    try:
        ControlStore(url).append(
            "local", kind="schedule", document_id="daily", state="active",
            payload={"analyzeOnly": True}, expected_revision=None,
        )
        original = datetime.fromisoformat("2026-08-30T10:00:00+00:00")
        shifted = datetime.now(timezone.utc) + timedelta(minutes=1)
        records = _codex_records("synthetic-monitor-middle")
        records[4:4] = [records[4]] * 1_600
        for record in records:
            when = datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
            record["timestamp"] = (shifted + (when - original)).isoformat()
        records.insert(1550, {
            "timestamp": (shifted + timedelta(minutes=1, seconds=4)).isoformat(),
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call-1",
                        "output": '{"exit_code":1,"output":"synthetic middle failure"}'},
        })
        _write_jsonl(root / "large.jsonl", records)
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        sample = storage.get_recent_local_events("local", bundle.run.run_id)
        seen_ids = ({event.event_id for event in bundle.events}
                    | {event["eventId"] for event in sample["events"]})
        middle_ids = {row[0] for row in storage._conn.execute(
            "SELECT event_id FROM local_recent_event_facts "
            "WHERE tenant_id='local' AND provenance='codex:response_item_output'"
        )} - seen_ids
        assert middle_ids

        monitor = IssueNotices(url)
        assert monitor.scan("local") >= 1
        notices = monitor.list("local")
        assert any(item["eventId"] in middle_ids for item in notices)
        assert monitor.scan("local") == 0
        assert monitor.list("foreign") == []
        insights = build_agent_insights_bundle(url, tenant="local")
        assert insights["reliability"]["commandFailures"] == 2
        assert insights["sourceActivity"][0]["partialRuns"] == 0
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(dashboard_app, "MAX_INSIGHT_LOCAL_EXPANSION_ROWS", 100)
            limited = build_agent_insights_bundle(url, tenant="local")
        assert limited["sourceActivity"][0]["partialRuns"] == 1
        assert limited["reliability"]["commandFailures"] == 0
        cycle = run_cycle(
            url, {"analyzeOnly": True, "includeApprovedEvaluations": False},
            tenant_id="local",
        )
        assert cycle["analysis"]["status"] == "completed"
        assert cycle["newIssues"] == 0
        repeated = run_cycle(
            url, {"analyzeOnly": True, "includeApprovedEvaluations": False},
            tenant_id="local",
        )
        assert repeated["analysis"]["analysisId"] == cycle["analysis"]["analysisId"]
        assert repeated["newIssues"] == 0

        async def read_insights(tenant):
            app = create_app(storage=url, tenant_id=tenant)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.get("/api/insights")

        own = asyncio.run(read_insights("local"))
        assert own.status_code == 200
        assert own.json()["reliability"]["commandFailures"] == 2
        assert asyncio.run(read_insights("foreign")).json()["reliability"][
            "commandFailures"
        ] == 0
        storage._conn.execute(
            "UPDATE local_recent_event_facts SET attributes_json='[]' "
            "WHERE tenant_id=? AND run_id=? AND event_id=?",
            ("local", bundle.run.run_id, next(iter(middle_ids))),
        )
        with pytest.raises(RuntimeError, match="invalid local full run"):
            build_agent_insights_bundle(url, tenant="local")
    finally:
        storage.close()


def test_admitted_tail_turn_can_be_analyzed_from_recorded_events(tmp_path: Path) -> None:
    import asyncio

    import httpx
    from verdict.dashboard.app import build_agent_turn_analysis, create_app

    root = tmp_path / "codex"
    records = _zero_event_codex_history(1_001, "synthetic-tail-analysis")
    records.insert(-1, {
        "timestamp": "2026-08-30T10:00:01Z", "type": "response_item",
        "payload": {"type": "function_call", "name": "exec_command",
                    "call_id": "tail-call", "arguments": '{"cmd":"false"}'},
    })
    records.insert(-1, {
        "timestamp": "2026-08-30T10:00:01Z", "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "tail-call",
                    "output": '{"exit_code":1,"output":"synthetic failure"}'},
    })
    _write_jsonl(root / "many-turns.jsonl", records)
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        assert storage._conn.execute(
            "SELECT local_projection_complete FROM import_sources"
        ).fetchone()[0] == 1
        tail_id = storage._conn.execute(
            "SELECT turn_id FROM local_tail_turn_facts"
        ).fetchone()[0]
        report = build_agent_turn_analysis(
            f"sqlite:///{db}", tenant="local", run_id=bundle.run.run_id,
            turn_id=tail_id,
        )
        assert report["eventCoverage"]["complete"] is True
        assert any(item["code"] == "tool_error" for item in report["reviewSignals"])
        async def read(tenant):
            app = create_app(storage=f"sqlite:///{db}", tenant_id=tenant)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.get(
                    f"/api/runs/{bundle.run.run_id}/turns/{tail_id}/analysis"
                )

        assert asyncio.run(read("local")).json()["eventCoverage"]["complete"] is True
        assert asyncio.run(read("foreign")).status_code == 404
        async def run_report():
            app = create_app(storage=f"sqlite:///{db}", tenant_id="local")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.get(
                    f"/api/runs/{bundle.run.run_id}/recorded-analysis"
                )

        full_report = asyncio.run(run_report())
        assert full_report.status_code == 200
        assert full_report.json()["recordedTurns"] == 1_001
        storage._conn.execute(
            "DELETE FROM local_recent_event_facts WHERE tenant_id=? AND run_id=? "
            "AND turn_id=?", ("local", bundle.run.run_id, tail_id),
        )
        assert asyncio.run(read("local")).status_code == 503
        assert asyncio.run(run_report()).status_code == 503
    finally:
        storage.close()


def test_live_postgres_admitted_tail_turn_analysis(tmp_path: Path) -> None:
    import asyncio

    import httpx
    import psycopg
    from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
    from verdict.dashboard.app import build_agent_insights_bundle, create_app
    from verdict.storage import PostgresStorage

    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    root = tmp_path / "codex"
    records = _zero_event_codex_history(1_001, "synthetic-pg-tail-analysis")
    records.insert(-1, {
        "timestamp": "2026-08-30T10:00:01Z", "type": "response_item",
        "payload": {"type": "function_call", "name": "exec_command",
                    "call_id": "tail-call", "arguments": '{"cmd":"false"}'},
    })
    records.insert(-1, {
        "timestamp": "2026-08-30T10:00:01Z", "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "tail-call",
                    "output": '{"exit_code":1,"output":"synthetic failure"}'},
    })
    _write_jsonl(root / "many-turns.jsonl", records)
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
            [bundle] = storage.list_agent_run_bundles("local")
            with psycopg.connect(isolated) as connection:
                tail_id = connection.execute(
                    "SELECT turn_id FROM local_tail_turn_facts"
                ).fetchone()[0]
        finally:
            storage.close()

        async def read(tenant):
            app = create_app(storage=isolated, tenant_id=tenant)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.get(
                    f"/api/runs/{bundle.run.run_id}/turns/{tail_id}/analysis"
                )

        local = asyncio.run(read("local"))
        assert local.status_code == 200
        assert local.json()["eventCoverage"]["complete"] is True
        assert any(item["code"] == "tool_error" for item in local.json()["reviewSignals"])
        assert asyncio.run(read("foreign")).status_code == 404
        async def run_report(tenant):
            app = create_app(storage=isolated, tenant_id=tenant)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.get(
                    f"/api/runs/{bundle.run.run_id}/recorded-analysis"
                )

        own_report = asyncio.run(run_report("local"))
        assert own_report.status_code == 200
        assert own_report.json()["recordedTurns"] == 1_001
        assert asyncio.run(run_report("foreign")).status_code == 404
        insights = build_agent_insights_bundle(isolated, tenant="local")
        assert insights["sourceActivity"][0]["turns"] == 1_001
        assert insights["sourceActivity"][0]["partialRuns"] == 0
        assert insights["reliability"]["commandFailures"] == 1


def test_full_projection_budget_fallback_preserves_old_facts_and_clears_tail_turns(
    tmp_path: Path, monkeypatch,
) -> None:
    import verdict.telemetry.local_agents as local_agents
    from verdict.dashboard.app import build_agent_usage_bundle

    root = tmp_path / "codex"
    _write_jsonl(root / "many-turns.jsonl",
                 _zero_event_codex_history(1_001, "synthetic-fallback"))
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [before] = storage.list_agent_run_bundles("local")
        [complete] = storage._conn.execute(
            "SELECT local_projection_complete FROM import_sources"
        ).fetchone()
        assert complete == 1
        monkeypatch.setattr(local_agents, "_MAX_FULL_PROJECTED_ROWS", 1_000)
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [partial] = storage._conn.execute(
            "SELECT local_projection_complete FROM import_sources"
        ).fetchone()
        [attempt_partial] = storage._conn.execute(
            "SELECT projection_complete FROM local_source_scan_attempts"
        ).fetchone()
        [tail_count] = storage._conn.execute(
            "SELECT count(*) FROM local_tail_turn_facts"
        ).fetchone()
        assert partial == 0 and attempt_partial == 0 and tail_count == 0
        assert storage.list_agent_run_bundles("local") == [before]
        usage = build_agent_usage_bundle(
            f"sqlite:///{tmp_path / 'verdict.db'}", tenant="local",
            day="2026-08-30", now=datetime.fromisoformat("2026-08-31T12:00:00+00:00"),
        )
        assert next(day for day in usage["days"] if day["day"] == "2026-08-30")[
            "sourcePartial"
        ] is True
    finally:
        storage.close()


def test_source_over_measured_full_admission_limit_stays_partial(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    records = _codex_records("synthetic-over-admission")
    records[4:4] = [records[4]] * 5_001
    _write_jsonl(root / "large.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [mode] = storage._conn.execute(
            "SELECT local_projection_complete FROM import_sources"
        ).fetchone()
        [facts] = storage._conn.execute(
            "SELECT count(*) FROM local_recent_event_facts"
        ).fetchone()
        assert mode == 0
        assert facts <= 50
    finally:
        storage.close()


def test_prefix_without_usable_record_preserves_prior_revision(tmp_path: Path, monkeypatch) -> None:
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    path = root / "session.jsonl"
    _write_jsonl(path, _codex_records())
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [before] = storage.list_agent_run_bundles("local")
        monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", 1)
        result = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert result.skip_reasons == {"unsupported_history": 1}
        assert storage.list_agent_run_bundles("local") == [before]
    finally:
        storage.close()


def test_claude_event_fanout_guard_preserves_prior_revision(
    tmp_path: Path, monkeypatch,
) -> None:
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "claude"
    path = root / "session.jsonl"
    _write_jsonl(path, _claude_records())
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", claude_root=root).stored == 1
        [before] = storage.list_agent_run_bundles("local")
        records = _claude_records()[:1]
        records.append({
            "timestamp": "2026-08-30T11:00:01Z", "type": "assistant",
            "uuid": "assistant-many", "sessionId": "claude-session-1",
            "message": {
                "id": "msg-many", "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 3},
                "content": [
                    {"type": "tool_use", "id": f"tool-{index}", "name": "Bash",
                     "input": {"command": "true"}}
                    for index in range(20)
                ],
            },
        })
        _write_jsonl(path, records)
        monkeypatch.setattr(local_agents, "_MAX_PARSED_EVENTS", 10)
        result = capture_local_agents(storage, tenant_id="local", claude_root=root)
        assert result.skip_reasons == {"history_event_limit": 1}
        assert storage.list_agent_run_bundles("local") == [before]
    finally:
        storage.close()


def test_one_scan_attempts_valid_file_after_failed_file(
    tmp_path: Path,
) -> None:
    """One approval attempts every discovered file, even if another is malformed."""

    root = tmp_path / "codex"
    root.mkdir()
    (root / "a-broken.jsonl").write_bytes(b"{malformed}\n")
    _write_jsonl(root / "b-valid.jsonl", _codex_records())
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        first = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert first.files == 2 and first.stored == first.skipped == 1
        assert first.scan_incomplete is False and first.deferred == 0
        second = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert second.files == 2 and second.stored == second.skipped == 1
        assert second.scan_incomplete is False and second.deferred == 0
        assert len(storage.list_agent_run_bundles("local")) == 1
    finally:
        storage.close()


def test_one_scan_attempts_recent_and_older_history(
    tmp_path: Path,
) -> None:

    root = tmp_path / "codex"
    root.mkdir()
    candidates = [root / "first.jsonl", root / "second.jsonl"]
    older, newer = sorted(candidates, key=lambda path: sha256(str(path.resolve()).encode()).hexdigest())
    older.write_bytes(b"{malformed}\n")
    _write_jsonl(newer, _codex_records())
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        first = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert first.stored == first.skipped == 1 and first.deferred == 0
        second = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert second.stored == second.skipped == 1 and second.deferred == 0
    finally:
        storage.close()


def test_unusable_oversized_history_does_not_prevent_other_file_capture(
    tmp_path: Path, monkeypatch,
) -> None:
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    root.mkdir()
    oversized = root / "oversized.jsonl"
    oversized.write_bytes(b"x" * 4097)
    valid = root / "valid.jsonl"
    _write_jsonl(valid, _codex_records())
    os.utime(oversized, ns=(2_000_000_000, 2_000_000_000))
    os.utime(valid, ns=(1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", 4096)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        first = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert first.files == 2 and first.stored == 1 and first.skipped == 1
        assert first.skip_reasons == {"unsupported_history": 1}
        assert first.deferred == 0
    finally:
        storage.close()


def test_one_scan_captures_multiple_files(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    for name in ("first", "second"):
        records = _codex_records(name)
        records[0]["payload"]["id"] = name
        _write_jsonl(root / f"{name}.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        first = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert first.files == first.stored == 2 and first.deferred == 0
        assert first.scan_incomplete is False
        second = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert second.files == second.stored == 2 and second.deferred == 0
        assert len(storage.list_agent_run_bundles("local")) == 2
    finally:
        storage.close()


def test_one_scan_counts_all_projected_rows_across_files(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    for name in ("first", "second"):
        records = _codex_records(name)
        records[0]["payload"]["id"] = name
        _write_jsonl(root / f"{name}.jsonl", records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        first = capture_local_agents(storage, tenant_id="local", codex_root=root)
        second = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert first.files == first.stored == 2 and first.deferred == 0
        assert second.files == second.stored == 2 and second.deferred == 0
        assert len(storage.list_agent_run_bundles("local")) == 2
    finally:
        storage.close()


def test_live_postgres_one_scan_attempts_every_file(tmp_path: Path) -> None:
    from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
    from verdict.storage import PostgresStorage

    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    root = tmp_path / "codex"
    root.mkdir()
    (root / "a-broken.jsonl").write_bytes(b"{malformed}\n")
    _write_jsonl(root / "b-valid.jsonl", _codex_records())
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            first = capture_local_agents(storage, tenant_id="local", codex_root=root)
            second = capture_local_agents(storage, tenant_id="local", codex_root=root)
            assert first.stored == second.stored == 1
            assert first.skipped == second.skipped == 1
            assert first.deferred == second.deferred == 0
            assert len(storage.list_agent_run_bundles("local")) == 1
        finally:
            storage.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite", "buffered"])
def test_old_scan_attempt_cannot_overwrite_newer_outcome(tmp_path: Path, backend: str) -> None:
    storage = (
        InMemoryStorage() if backend == "memory"
        else BufferedStorage(InMemoryStorage()) if backend == "buffered"
        else SQLiteStorage(str(tmp_path / "scan.db"))
    )
    identity = ("tenant", "codex", "a" * 64, "b" * 64)
    try:
        first = storage.claim_local_scan_attempt(
            *identity, "2026-10-20T10:00:00+00:00", 100, 1,
        )
        second = storage.claim_local_scan_attempt(
            *identity, "2026-10-20T10:01:00+00:00", 101, 2,
        )
        assert second == first + 1
        assert storage.finish_local_scan_attempt(
            *identity, second, "2026-10-20T10:02:00+00:00", "partial_projection",
            "c" * 64, False, True,
        )
        assert not storage.finish_local_scan_attempt(
            *identity, first, "2026-10-20T10:03:00+00:00", "failed",
            None, False, False,
        )
        if backend == "sqlite":
            [row] = storage._conn.execute(
                "SELECT generation,outcome_code,accepted_revision "
                "FROM local_source_scan_attempts"
            ).fetchall()
            assert tuple(row) == (second, "partial_projection", "c" * 64)
        else:
            inner = storage._inner if backend == "buffered" else storage
            assert inner._local_scan_attempts[identity]["outcome_code"] == "partial_projection"
    finally:
        storage.close()


def test_live_postgres_old_scan_attempt_cannot_overwrite_newer_outcome() -> None:
    from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
    from verdict.storage import PostgresStorage

    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    identity = ("tenant", "codex", "a" * 64, "b" * 64)
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            first = storage.claim_local_scan_attempt(
                *identity, "2026-10-20T10:00:00+00:00", 100, 1,
            )
            second = storage.claim_local_scan_attempt(
                *identity, "2026-10-20T10:01:00+00:00", 101, 2,
            )
            assert storage.finish_local_scan_attempt(
                *identity, second, "2026-10-20T10:02:00+00:00", "partial_projection",
                "c" * 64, False, True,
            )
            assert not storage.finish_local_scan_attempt(
                *identity, first, "2026-10-20T10:03:00+00:00", "failed",
                None, False, False,
            )
            with storage._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT generation,outcome_code,accepted_revision "
                    "FROM local_source_scan_attempts"
                )
                assert cur.fetchone() == (second, "partial_projection", "c" * 64)
        finally:
            storage.close()


def test_failed_rescan_keeps_last_accepted_run_and_records_failed_attempt(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    path = root / "session.jsonl"
    _write_jsonl(path, _codex_records())
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [before] = storage.list_agent_run_bundles("local")
        path.write_bytes(b"{malformed}\n")
        result = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert result.skipped == 1 and result.skip_reasons == {"malformed_jsonl": 1}
        assert storage.list_agent_run_bundles("local") == [before]
        [attempt] = storage._conn.execute(
            "SELECT generation,outcome_code,accepted_revision FROM local_source_scan_attempts"
        ).fetchall()
        assert tuple(attempt) == (2, "failed_malformed_jsonl", None)
    finally:
        storage.close()


def test_scan_attempt_never_persists_unrecognized_exception_text(
    tmp_path: Path, monkeypatch,
) -> None:
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    _write_jsonl(root / "session.jsonl", _codex_records())
    secret = "PRIVATE_EXCEPTION_BODY_CANARY"

    def reject(_path, *, home=None):
        raise ValueError(secret)

    monkeypatch.setattr(local_agents, "_parse_codex", reject)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.skip_reasons == {"invalid_local_source": 1}
        [outcome] = storage._conn.execute(
            "SELECT outcome_code FROM local_source_scan_attempts"
        ).fetchone()
        assert outcome == "failed_invalid_local_source"
        assert secret not in str(summary.as_dict())
        assert secret not in outcome
    finally:
        storage.close()


def test_growing_local_source_cannot_leave_stale_v1_prefix_looking_complete(
    tmp_path: Path,
) -> None:
    from verdict.read_port import StorageVerdictReadPort, VerdictReadError

    root = tmp_path / "codex"
    path = root / "growing.jsonl"
    records: list[dict[str, object]] = [{
        "timestamp": "2026-08-30T10:00:00Z", "type": "session_meta",
        "payload": {"id": "synthetic-growing-source"},
    }]
    for index in range(1_000):
        for event_type in ("task_started", "task_complete"):
            records.append({
                "timestamp": "2026-08-30T10:00:00Z", "type": "event_msg",
                "payload": {"type": event_type, "turn_id": f"turn-{index}"},
            })
    _write_jsonl(path, records)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [original] = storage.list_agent_run_bundles("local")
        port = StorageVerdictReadPort(storage)
        assert port.get_agent_run(tenant_id="local", run_id=original.run.run_id) is not None

        with path.open("a") as handle:
            for event_type in ("task_started", "task_complete"):
                handle.write(json.dumps({
                    "timestamp": "2026-08-30T10:00:00Z", "type": "event_msg",
                    "payload": {"type": event_type, "turn_id": "turn-1000"},
                }) + "\n")
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        with pytest.raises(VerdictReadError) as error:
            port.get_agent_run(tenant_id="local", run_id=original.run.run_id)
        assert error.value.code == "invalid_read_model"
        retained = storage.get_agent_run_bundle("local", original.run.run_id)
        assert retained is not None and retained.turns[0].turn_id == original.turns[0].turn_id
    finally:
        storage.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_normalized_local_capture_accepts_aggregate_over_legacy_bundle_json_limit(
    tmp_path: Path, backend: str,
) -> None:
    from verdict.read_port import StorageVerdictReadPort

    root = tmp_path / "codex"
    path = root / "long-text.jsonl"
    records: list[dict[str, object]] = [{
        "timestamp": "2026-08-30T10:00:00Z", "type": "session_meta",
        "payload": {"id": "synthetic-long-text"},
    }]
    for index in range(80):
        records.extend((
            {"timestamp": "2026-08-30T10:00:00Z", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": f"turn-{index}"}},
            {"timestamp": "2026-08-30T10:00:01Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "synthetic text " * 3_600}},
            {"timestamp": "2026-08-30T10:00:02Z", "type": "event_msg",
             "payload": {"type": "task_complete", "turn_id": f"turn-{index}"}},
        ))
    _write_jsonl(path, records)
    with ExitStack() as stack:
        if backend == "postgres":
            from verdict.storage.postgres import PostgresStorage

            from packages.verdict.tests._postgres_test_safety import (
                isolated_test_dsn,
                validate_test_dsn,
            )

            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            storage = PostgresStorage(stack.enter_context(isolated_test_dsn(dsn)))
        else:
            storage = SQLiteStorage(str(tmp_path / "verdict.db"))
        stack.callback(storage.close)
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        assert sum(len((turn.user_request_redacted or "").encode())
                   for turn in bundle.turns) > 4 * 1024 * 1024
        assert StorageVerdictReadPort(storage).get_agent_run(
            tenant_id="local", run_id=bundle.run.run_id,
        ) is not None
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


def _source_activity_for(tmp_path: Path, name: str, **roots: Path) -> tuple[dict, dict]:
    from verdict.dashboard.app import build_agent_insights_bundle

    database = tmp_path / f"{name}.db"
    storage = SQLiteStorage(str(database))
    assert capture_local_agents(storage, tenant_id="local", **roots).skipped == 0
    storage.close()
    insights = build_agent_insights_bundle(database, tenant="local")
    [activity] = insights["sourceActivity"]
    return activity, insights["reliability"]


def test_failure_counts_state_whether_the_source_reported_any_outcome(tmp_path: Path) -> None:
    claude_root = tmp_path / "claude"
    _write_jsonl(claude_root / "session.jsonl", _claude_records())
    claude, _ = _source_activity_for(tmp_path, "claude", claude_root=claude_root)
    # The Claude tool result reports is_error=false; its Bash command has no exit status.
    assert claude["toolOutcomesReported"] >= 1
    assert claude["commandOutcomesReported"] == 0
    assert claude["testOutcomesReported"] == 0

    reported_root = tmp_path / "codex-reported"
    _write_jsonl(reported_root / "session.jsonl", _codex_records())
    reported, reported_totals = _source_activity_for(
        tmp_path, "codex-reported", codex_root=reported_root,
    )
    assert reported["toolErrors"] >= 1
    assert reported["toolOutcomesReported"] >= reported["toolErrors"]
    assert reported_totals["toolOutcomesReported"] == reported["toolOutcomesReported"]

    # Current Codex rollouts return plain text tool output without an exit code.
    unreported_records = _codex_records()
    for record in unreported_records:
        payload = record.get("payload") or {}
        if payload.get("type") == "function_call_output":
            payload["output"] = [{"type": "input_text", "text": "Script completed\nOutput:\nok"}]
    unreported_root = tmp_path / "codex-unreported"
    _write_jsonl(unreported_root / "session.jsonl", unreported_records)
    unreported, unreported_totals = _source_activity_for(
        tmp_path, "codex-unreported", codex_root=unreported_root,
    )
    assert unreported["toolCalls"] >= 1
    assert unreported["toolErrors"] == 0
    assert unreported["toolOutcomesReported"] == 0
    assert unreported_totals["toolOutcomesReported"] == 0


def test_capture_reports_progress_once_per_history_file(tmp_path: Path) -> None:
    claude_root = tmp_path / "claude"
    _write_jsonl(claude_root / "a.jsonl", _claude_records())
    _write_jsonl(claude_root / "nested" / "b.jsonl", _claude_records())
    codex_root = tmp_path / "codex"
    _write_jsonl(codex_root / "c.jsonl", _codex_records())
    seen: list[tuple[int, int]] = []

    summary = capture_local_agents(
        InMemoryStorage(), tenant_id="local", claude_root=claude_root, codex_root=codex_root,
        progress=lambda done, total: seen.append((done, total)),
    )

    assert summary.files == 3
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]


@pytest.mark.parametrize(
    ("codex_model", "cached_rate"),
    [
        ("gpt-5.4", 0.1),
        ("gpt-6-astra", 0.1),
        ("gpt-6-sol", 0.1),
        ("gpt-6.1-sol", 0.05),
        ("gpt-6-luna", 0.1),
    ],
)
def test_agent_turns_carry_an_estimated_list_price_per_source(
    tmp_path: Path, codex_model: str, cached_rate: float,
) -> None:
    from verdict.dashboard.app import build_agent_insights_bundle
    from verdict.pricing import PRICE_PER_1K

    claude_root = tmp_path / "claude"
    _write_jsonl(claude_root / "session.jsonl", _claude_records())
    codex_root = tmp_path / "codex"
    codex_records = _codex_records()
    for record in codex_records:
        if record.get("type") == "turn_context":
            record["payload"]["model"] = codex_model
    _write_jsonl(codex_root / "session.jsonl", codex_records)
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    assert capture_local_agents(
        storage, tenant_id="local", claude_root=claude_root, codex_root=codex_root,
    ).skipped == 0
    bundles = {bundle.session.source_kind: bundle for bundle in storage.list_agent_run_bundles("local")}
    storage.close()

    # Claude Code: input excludes cache reads/writes, priced at 10% / 125% of input.
    [claude_turn] = bundles["claude-code"].turns
    in_rate, out_rate = PRICE_PER_1K["claude-sonnet-4-5"]
    claude_expected = (
        claude_turn.input_tokens * in_rate
        + claude_turn.cached_input_tokens * in_rate * 0.1
        + claude_turn.cache_write_input_tokens * in_rate * 1.25
        + claude_turn.output_tokens * out_rate
    ) / 1000
    # Codex: input already includes cached tokens; the rate depends on model.
    codex_turns = bundles["codex"].turns
    in_rate, out_rate = PRICE_PER_1K[codex_model]
    codex_expected = sum(
        ((turn.input_tokens - (turn.cached_input_tokens or 0)) * in_rate
         + (turn.cached_input_tokens or 0) * in_rate * cached_rate
         + turn.output_tokens * out_rate) / 1000
        for turn in codex_turns if turn.input_tokens is not None
    )
    assert claude_expected > 0 and codex_expected > 0

    insights = build_agent_insights_bundle(database, tenant="local")
    by_source = {row["source"]: row for row in insights["sourceActivity"]}
    assert by_source["claude-code"]["pricedTurns"] == 1
    assert by_source["claude-code"]["listPriceUsd"] == pytest.approx(claude_expected, abs=1e-6)
    assert by_source["codex"]["pricedTurns"] == sum(
        1 for turn in codex_turns if turn.input_tokens is not None
    )
    assert by_source["codex"]["listPriceUsd"] == pytest.approx(codex_expected, abs=1e-6)
    performance = insights["performance"]
    assert performance["agentTurnListPriceUsd"] == pytest.approx(
        claude_expected + codex_expected, abs=1e-6
    )
    assert performance["agentTurnsPriced"] == 1 + by_source["codex"]["pricedTurns"]
    assert performance["agentTurnsPriced"] + performance["agentTurnsUnpriced"] == (
        len(codex_turns) + 1
    )


def test_turns_without_one_named_model_or_a_known_token_basis_are_not_priced(
    tmp_path: Path,
) -> None:
    from verdict.dashboard.app import build_agent_insights_bundle

    root = tmp_path / "claude"
    records = _claude_records()
    for record in records:
        if record.get("type") == "assistant":
            record["message"]["model"] = "model-with-no-price"
    _write_jsonl(root / "session.jsonl", records)
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    capture_local_agents(storage, tenant_id="local", claude_root=root)
    storage.close()

    insights = build_agent_insights_bundle(database, tenant="local")
    [row] = insights["sourceActivity"]
    assert row["listPriceUsd"] is None and row["pricedTurns"] == 0
    assert insights["performance"]["agentTurnListPriceUsd"] is None
    assert insights["performance"]["agentTurnsUnpriced"] == 1


def test_only_turns_with_exactly_one_real_model_are_priced(tmp_path: Path) -> None:
    from verdict.dashboard.app import build_agent_insights_bundle

    def insights_for(name: str, mutate) -> dict:
        root = tmp_path / name
        records = _claude_records()
        mutate(records)
        _write_jsonl(root / "session.jsonl", records)
        database = tmp_path / f"{name}.db"
        storage = SQLiteStorage(str(database))
        capture_local_agents(storage, tenant_id="local", claude_root=root)
        storage.close()
        return build_agent_insights_bundle(database, tenant="local")["performance"]

    def second_model(records):
        records[1]["message"]["model"] = "claude-haiku-4-5"

    def client_notice(records):
        notice = json.loads(json.dumps(records[1]))
        notice["uuid"] = "assistant-notice"
        notice["message"] = {
            "id": "msg-notice", "model": "<synthetic>", "stop_reason": "stop_sequence",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "content": [{"type": "text", "text": "You've hit your session limit"}],
        }
        records.append(notice)

    mixed = insights_for("mixed", second_model)
    assert mixed["agentTurnsPriced"] == 0 and mixed["agentTurnsUnpriced"] == 1
    with_notice = insights_for("notice", client_notice)
    assert with_notice["agentTurnsPriced"] == 1 and with_notice["agentTurnListPriceUsd"] > 0
