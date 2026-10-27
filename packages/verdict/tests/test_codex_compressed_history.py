"""Compressed Codex history follows the same approved local capture path."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from pathlib import Path

import pytest
import verdict.telemetry.local_agents as local_agents
import zstandard
from fastapi.testclient import TestClient
from test_local_agent_capture import _codex_records
from verdict.dashboard.app import create_app
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.local_agents import capture_local_agents


def _write_zst(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = "".join(json.dumps(record) + "\n" for record in records).encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(raw))


def test_compressed_codex_history_is_previewed_captured_and_readable(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    _write_zst(root / "session.jsonl.zst", _codex_records())
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    try:
        with TestClient(create_app(storage=str(database), tenant_id="local")) as client:
            token = client.get("/api/setup/token").json()["setupToken"]
            preview = client.post(
                "/api/setup/preview", headers={"X-Verdict-Setup": token},
                json={"codexRoot": str(root)},
            )
        assert preview.status_code == 200
        assert preview.json()["codex"]["files"] == 1
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.files == summary.stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        assert bundle.turns
        with TestClient(create_app(storage=str(database), tenant_id="local")) as client:
            response = client.get(f"/api/runs/{bundle.run.run_id}")
        assert response.status_code == 200
        assert response.json()["runId"] == bundle.run.run_id
        repeated = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert repeated.stored == 1
        assert len(storage.list_agent_run_bundles("local")) == 1
    finally:
        storage.close()


def test_truncated_compressed_frame_is_rejected_even_with_complete_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl.zst"
    _write_zst(path, _codex_records())
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match="invalid_compressed_history"):
        local_agents._records(path)


def test_concatenated_compressed_frames_are_read(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl.zst"
    rows = _codex_records()
    compressor = zstandard.ZstdCompressor()
    path.write_bytes(b"".join(
        compressor.compress((json.dumps(row) + "\n").encode()) for row in rows
    ))
    decoded, revision = local_agents._records(path)
    assert decoded == rows
    assert revision.full_bytes == path.stat().st_size
    assert not revision.partial_final_line


def test_compressed_file_physical_and_decoded_limits_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "session.jsonl.zst"
    _write_zst(path, _codex_records())
    physical_size = path.stat().st_size
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_VERIFY_BYTES", physical_size - 1)
    with pytest.raises(ValueError, match="history_file_too_large"):
        local_agents._records(path)
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_VERIFY_BYTES", physical_size)
    records, _ = local_agents._records(path)
    assert records
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", 80)
    _records, revision = local_agents._records(path)
    assert revision.partial_final_line
    assert revision.complete_prefix_bytes == 0


def test_compressed_decoded_prefix_reports_partial_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "codex"
    path = root / "session.jsonl.zst"
    rows = _codex_records()
    _write_zst(path, rows)
    first_four = sum(len((json.dumps(row) + "\n").encode()) for row in rows[:4])
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", first_four)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.stored == summary.source_partial_files == 1
        assert summary.partial_reasons == {"bounded_source_prefix": 1}
        assert len(storage.list_agent_run_bundles("local")) == 1
        assert storage.local_scan_attempt_summary("local")[0][0] == (
            "partial_bounded_source_prefix"
        )
    finally:
        storage.close()


@pytest.mark.parametrize("compressed", [False, True])
def test_oversized_history_exposes_newest_source_records_without_counting_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compressed: bool,
) -> None:
    root = tmp_path / "codex"
    path = root / ("session.jsonl.zst" if compressed else "session.jsonl")
    rows = _codex_records()
    rows.extend({"timestamp": "2026-08-30T11:00:00Z", "type": "event_msg",
                 "payload": {"type": "agent_message", "phase": "final_answer",
                             "message": "Newest synthetic work completed"}} for _ in range(6))
    raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    path.parent.mkdir()
    path.write_bytes(zstandard.ZstdCompressor().compress(raw) if compressed else raw)
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", sum(
        len((json.dumps(row) + "\n").encode()) for row in rows[:8]
    ))
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    try:
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.stored == summary.source_partial_files == 1
        [bundle] = storage.list_agent_run_bundles("local")
        with TestClient(create_app(storage=str(database), tenant_id="local")) as client:
            response = client.get(f"/api/runs/{bundle.run.run_id}/recent-local-events")
        assert response.status_code == 200
        sample = response.json()
        assert sample["latestSample"]["status"] == "sampled"
        assert any("Newest synthetic work completed" in row.get("preview", "")
                   for row in sample["latestSample"]["records"])
        assert sample["events"] == []
        assert sample["findings"] == []
    finally:
        storage.close()


@pytest.mark.parametrize("compressed", [False, True])
def test_newest_sample_redacts_tail_content_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compressed: bool,
) -> None:
    secret = "sk-proj-" + "S" * 40
    root = tmp_path / "codex"
    path = root / ("session.jsonl.zst" if compressed else "session.jsonl")
    rows = _codex_records()
    rows.extend({"timestamp": "X" * 50 + secret, "type": "event_msg",
                 "payload": {"type": "agent_message", "phase": "final_answer",
                             "message": f"finished using {secret}"}} for _ in range(6))
    raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    path.parent.mkdir()
    path.write_bytes(zstandard.ZstdCompressor().compress(raw) if compressed else raw)
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", sum(
        len((json.dumps(row) + "\n").encode()) for row in rows[:8]
    ))
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        with TestClient(create_app(storage=str(database), tenant_id="local")) as client:
            response = client.get(f"/api/runs/{bundle.run.run_id}/recent-local-events")
        assert response.status_code == 200
        assert secret not in response.text
        assert "sk-proj-SSSSSS" not in response.text
        assert "finished using" in response.text
        assert secret not in database.read_bytes().decode("utf-8", errors="ignore")
    finally:
        storage.close()


def test_newest_compressed_sample_reports_decoded_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "codex"
    path = root / "session.jsonl.zst"
    _write_zst(path, _codex_records() + [
        {"type": "event_msg", "timestamp": "2026-08-30T11:00:00Z",
         "payload": {"type": "agent_message", "message": "later"}}
        for _ in range(10)
    ])
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", 900)
    monkeypatch.setattr(local_agents, "_LATEST_DECODED_BYTES", 1000)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        with TestClient(create_app(storage=str(tmp_path / "verdict.db"), tenant_id="local")) as client:
            response = client.get(f"/api/runs/{bundle.run.run_id}/recent-local-events")
        assert response.status_code == 200
        assert response.json()["latestSample"] == {
            "status": "unavailable_decoded_limit", "records": [],
        }
    finally:
        storage.close()


def test_newest_plain_sample_stops_when_active_file_grows_past_physical_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"type":"event_msg","timestamp":"2026-10-27T00:00:00Z"}\n')
    _, revision = local_agents._records(path)
    physical_limit = revision.full_bytes + 5
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_VERIFY_BYTES", physical_limit)
    original_open = Path.open
    bytes_read = 0

    class GrowingHandle:
        def __init__(self, handle):
            self.handle = handle
            self.appended = False

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            nonlocal bytes_read
            block = self.handle.read(size)
            bytes_read += len(block)
            if not self.appended:
                self.appended = True
                with original_open(path, "ab") as writer:
                    writer.write(b"x" * 100)
            return block

    def growing_open(self, mode="r", *args, **kwargs):
        handle = original_open(self, mode, *args, **kwargs)
        return GrowingHandle(handle) if self == path and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", growing_open)
    with pytest.raises(ValueError, match="history_file_too_large"):
        local_agents._latest_source_sample(path, revision, home=None, capture_content=True)
    assert bytes_read <= physical_limit + 1


@pytest.mark.parametrize("compressed", [False, True])
def test_metadata_only_capture_omits_newest_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compressed: bool,
) -> None:
    marker = "SYNTHETIC_PRIVATE_PROMPT_8675309"
    root = tmp_path / "codex"
    path = root / ("session.jsonl.zst" if compressed else "session.jsonl")
    rows = _codex_records()
    rows.extend({"timestamp": "2026-08-30T11:00:00Z", "type": "event_msg",
                 "payload": {"type": "agent_message", "message": marker}}
                for _ in range(6))
    raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    path.parent.mkdir()
    path.write_bytes(zstandard.ZstdCompressor().compress(raw) if compressed else raw)
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", sum(
        len((json.dumps(row) + "\n").encode()) for row in rows[:8]
    ))
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root,
                                    capture_content=False).stored == 1
        [bundle] = storage.list_agent_run_bundles("local")
        with TestClient(create_app(storage=str(database), tenant_id="local")) as client:
            response = client.get(f"/api/runs/{bundle.run.run_id}/recent-local-events")
        assert response.status_code == 200
        assert marker not in response.text
        assert marker not in database.read_bytes().decode("utf-8", errors="ignore")
        assert all(not row["preview"] for row in response.json()["latestSample"]["records"])
    finally:
        storage.close()


def test_plain_codex_history_wins_over_same_stem_archive(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    path = root / "session.jsonl.zst"
    _write_zst(path, _codex_records())
    plain = path.with_suffix("")
    plain.write_bytes(b"".join(
        (json.dumps(row) + "\n").encode() for row in _codex_records()
    ))
    assert list(local_agents.iter_local_history_paths(root, "codex")) == [plain]


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_active_codex_history_can_be_archived_without_duplicate_run(
    tmp_path: Path, backend: str,
) -> None:
    root = tmp_path / "codex"
    plain = root / "session.jsonl"
    plain.parent.mkdir()
    rows = _codex_records()
    plain.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in rows))
    with ExitStack() as stack:
        if backend == "postgres":
            from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
            from verdict.storage.postgres import PostgresStorage

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
            first = capture_local_agents(storage, tenant_id="local", codex_root=root)
            assert first.stored == 1
            _write_zst(plain.with_suffix(".jsonl.zst"), rows)
            plain.unlink()
            second = capture_local_agents(storage, tenant_id="local", codex_root=root)
            assert second.stored == 1
            assert second.skip_reasons == {}
            assert len(storage.list_agent_run_bundles("local")) == 1
        finally:
            storage.close()


def test_active_history_to_partial_archive_preserves_recorded_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "codex"
    plain = root / "session.jsonl"
    plain.parent.mkdir()
    original = b"".join((json.dumps(row) + "\n").encode() for row in _codex_records())
    plain.write_bytes(original)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
        archive = plain.with_suffix(".jsonl.zst")
        archive.write_bytes(zstandard.ZstdCompressor().compress(original + b"\n" * 256))
        plain.unlink()
        monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", len(original) + 16)
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.stored == summary.source_partial_files == 1
        assert summary.skip_reasons == {}
        assert len(storage.list_agent_run_bundles("local")) == 1
    finally:
        storage.close()


def test_compressed_history_discovery_excludes_symlinks_and_claude(tmp_path: Path) -> None:
    root = tmp_path / "history"
    path = root / "session.jsonl.zst"
    _write_zst(path, _codex_records())
    (root / "linked.jsonl.zst").symlink_to(path)
    assert list(local_agents.iter_local_history_paths(root, "codex")) == [path]
    assert list(local_agents.iter_local_history_paths(root, "claude-code")) == []


def test_archive_identity_does_not_follow_ignored_plain_sibling_symlink(tmp_path: Path) -> None:
    root = tmp_path / "history"
    archive = root / "session.jsonl.zst"
    _write_zst(archive, _codex_records())
    sibling = archive.with_suffix("")
    sibling.symlink_to(tmp_path / "outside.jsonl")
    assert list(local_agents.iter_local_history_paths(root, "codex")) == [archive]
    expected = local_agents.hashlib.sha256(str(sibling).encode()).hexdigest()
    assert local_agents._source_locator_hash(archive) == expected


def test_corrupt_archive_reports_skip_without_a_run(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    path = root / "session.jsonl.zst"
    path.parent.mkdir()
    path.write_bytes(b"not zstd")
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.skip_reasons == {"invalid_compressed_history": 1}
        assert storage.list_agent_run_bundles("local") == []
    finally:
        storage.close()


def test_compressed_archive_with_unfinished_jsonl_record_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    path = root / "session.jsonl.zst"
    _write_zst(path, _codex_records())
    raw = zstandard.ZstdDecompressor().decompress(path.read_bytes()) + b'{"type":'
    path.write_bytes(zstandard.ZstdCompressor().compress(raw))
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.skip_reasons == {"malformed_jsonl": 1}
        assert storage.list_agent_run_bundles("local") == []
    finally:
        storage.close()
