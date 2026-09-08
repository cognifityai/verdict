from __future__ import annotations

import os
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import verdict
from verdict import agent_transport
from verdict.agent_transport import MAX_RECORD_BYTES, CaptureQuotaExceeded, FileCaptureSink
from verdict.client import _resolve_storage
from verdict.evidence import AgentEventType
from verdict.instrumentors.base import apply_routing_context, safe_persist_trace
from verdict.schema import Trace
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.cli import main as import_main


@pytest.fixture(autouse=True)
def _reset_verdict() -> None:
    verdict.shutdown()
    yield
    verdict.shutdown()


def _capture_file_run(spool: Path) -> None:
    client = verdict.init(
        transport="file",
        spool_directory=spool,
        tenant_id="tenant-file",
        service_name="worker",
        instrumentors=[],
    )
    with verdict.agent_run(name="file-agent", external_id="run-1") as run:
        with run.turn(user_input="hello file@example.com") as turn:
            now = datetime.now(timezone.utc)
            trace = Trace(
                provider="openai",
                request_model="test-model",
                response_model="test-model",
                started_at=now,
                ended_at=now,
                prompt_redacted="question",
                response_redacted="answer",
            )
            apply_routing_context(client, trace)
            safe_persist_trace(client, trace)
            turn.record_command(command="true", exit_code=0)
            turn.set_output("done")
    verdict.shutdown()


def test_file_transport_replays_idempotently_into_sqlite(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    database = tmp_path / "verdict.db"
    _capture_file_run(spool)
    files = list(spool.glob("verdict-agent-*.jsonl"))
    assert files
    assert all(path.stat().st_size <= 8 * 1024 * 1024 for path in files)

    args = ["agent-file", str(spool), "--storage", f"sqlite:///{database}"]
    assert import_main(args) == 0
    assert import_main(args) == 0

    storage = SQLiteStorage(str(database))
    try:
        bundles = storage.list_agent_run_bundles("tenant-file")
        assert len(bundles) == 1
        assert len(bundles[0].turns) == 1
        assert bundles[0].turns[0].user_request_redacted == "hello <EMAIL>"
        assert [event.event_type for event in bundles[0].events] == [
            AgentEventType.MODEL_CALL,
            AgentEventType.COMMAND,
        ]
        model_event = bundles[0].events[0]
        assert model_event.trace_id is not None
        assert storage.get_trace(model_event.trace_id) is not None
    finally:
        storage.close()


def test_file_transport_does_not_open_default_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _capture_file_run(tmp_path / "spool")
    assert not (tmp_path / "verdict.db").exists()


def test_import_tolerates_only_incomplete_final_crash_fragment(
    tmp_path: Path,
    capsys,
) -> None:
    spool = tmp_path / "spool"
    database = tmp_path / "verdict.db"
    _capture_file_run(spool)
    capture_file = next(spool.glob("verdict-agent-*.jsonl"))
    with capture_file.open("ab") as handle:
        handle.write(b'{"schema":"verdict-agent-capture-v1"')

    assert import_main(["agent-file", str(spool), "--storage", f"sqlite:///{database}"]) == 0
    assert "incomplete" in capsys.readouterr().out

    bad = spool / "verdict-agent-bad-000000.jsonl"
    bad.write_bytes(b"not-json\n")
    assert import_main(["agent-file", str(spool), "--storage", f"sqlite:///{database}"]) == 2
    assert "malformed" in capsys.readouterr().err


def test_file_transport_quota_failure_does_not_change_agent_result(
    tmp_path: Path,
) -> None:
    client = verdict.init(
        transport="file",
        spool_directory=tmp_path / "spool",
        tenant_id="tenant-file",
        instrumentors=[],
    )
    assert client._capture_sink is not None
    client._capture_sink.directory_bytes = 1  # type: ignore[attr-defined]
    marker = object()
    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="hello") as turn:
            turn.set_output("done")
            result = marker
    assert result is marker


def test_file_transport_quota_drops_are_counted_and_warned_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = verdict.init(
        transport="file",
        spool_directory=tmp_path / "spool",
        tenant_id="tenant-file",
        instrumentors=[],
    )
    assert client._capture_sink is not None
    client._capture_sink.directory_bytes = 1  # type: ignore[attr-defined]

    caplog.set_level("WARNING", logger="verdict.agent")
    for ordinal in range(2):
        with verdict.agent_run(name="agent", external_id=f"run-{ordinal}") as run:
            with run.turn(user_input="hello") as turn:
                turn.set_output("done")

    snapshot = client.runtime_metrics.snapshot(client.storage)
    assert snapshot["capture"]["dropped_records"] == 8
    records = [record for record in caplog.records if record.name == "verdict.agent"]
    assert len(records) == 1
    assert "CaptureQuotaExceeded" in records[0].getMessage()


def test_file_transport_rejects_incompatible_storage_options(tmp_path: Path) -> None:
    storage = _resolve_storage("memory://")
    with pytest.raises(ValueError, match="file transport"):
        verdict.init(
            transport="file",
            spool_directory=tmp_path,
            storage=storage,
            instrumentors=[],
        )


@pytest.mark.parametrize(("segment_bytes", "directory_bytes"), [(0, 10_000_000), (5_000_000, 0)])
def test_file_transport_rejects_zero_byte_limits(
    tmp_path: Path,
    segment_bytes: int,
    directory_bytes: int,
) -> None:
    with pytest.raises(ValueError, match="byte limits"):
        FileCaptureSink(
            tmp_path,
            redaction_mode="redact",
            redaction_secret=None,
            segment_bytes=segment_bytes,
            directory_bytes=directory_bytes,
        )


def test_file_transport_rotates_segments_and_enforces_its_byte_quota(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    sink = FileCaptureSink(
        tmp_path,
        redaction_mode="redact",
        redaction_secret=None,
        segment_bytes=MAX_RECORD_BYTES + 1,
        directory_bytes=5_000_000,
    )
    now = datetime.now(timezone.utc)
    trace = Trace(
        started_at=now,
        ended_at=now,
        provider="test",
        response_redacted="x" * 2_200_000,
    )

    sink.capture_trace(trace)
    sink.capture_trace(Trace(**{**trace.__dict__, "trace_id": "second"}))
    with pytest.raises(CaptureQuotaExceeded, match="quota"):
        sink.capture_trace(Trace(**{**trace.__dict__, "trace_id": "third"}))
    sink.close()

    files = sorted(tmp_path.glob("verdict-agent-*.jsonl"))
    assert len(files) == 2
    assert all(path.stat().st_size <= MAX_RECORD_BYTES for path in files)
    assert tmp_path.stat().st_mode & 0o077 == 0
    assert all(path.stat().st_mode & 0o077 == 0 for path in files)


def test_file_transport_completes_short_writes(tmp_path: Path) -> None:
    class ShortWriter:
        def __init__(self) -> None:
            self.parts: list[bytes] = []
            self.calls = 0

        def write(self, value: bytes) -> int:
            raw = bytes(value)
            size = max(1, len(raw) // 2)
            self.parts.append(raw[:size])
            self.calls += 1
            return size

        def close(self) -> None:
            pass

    sink = FileCaptureSink(tmp_path, redaction_mode="redact", redaction_secret=None)
    assert sink._file is not None
    sink._file.close()
    writer = ShortWriter()
    sink._file = writer  # type: ignore[assignment]
    sink._size = 0
    sink._directory_size = 0

    sink.capture_trace(Trace(started_at=datetime.now(timezone.utc), provider="test"))

    record = b"".join(writer.parts)
    assert writer.calls > 1
    assert record.endswith(b"\n")
    kind, batch, traces = agent_transport.decode_capture_record(record[:-1])
    assert kind == "trace"
    assert batch is None
    assert len(traces) == 1


def test_partial_write_failure_abandons_segment_before_later_records(tmp_path: Path) -> None:
    class PartialThenError:
        def __init__(self, target: object) -> None:
            self.target = target
            self.calls = 0

        def write(self, value: bytes) -> int:
            self.calls += 1
            if self.calls > 1:
                raise OSError("injected write failure")
            raw = bytes(value)
            return self.target.write(raw[: max(1, len(raw) // 2)])  # type: ignore[attr-defined]

        def close(self) -> None:
            self.target.close()  # type: ignore[attr-defined]

    sink = FileCaptureSink(tmp_path, redaction_mode="redact", redaction_secret=None)
    assert sink._file is not None
    failing = PartialThenError(sink._file)
    sink._file = failing  # type: ignore[assignment]
    first = Trace(trace_id="first", started_at=datetime.now(timezone.utc), provider="test")
    second = Trace(trace_id="second", started_at=datetime.now(timezone.utc), provider="test")

    with pytest.raises(OSError, match="write failure"):
        sink.capture_trace(first)
    sink.capture_trace(second)
    sink.close()

    incomplete = 0

    def mark_incomplete() -> None:
        nonlocal incomplete
        incomplete += 1

    records = list(agent_transport.iter_capture_records(tmp_path, on_incomplete=mark_incomplete))
    assert incomplete == 1
    assert [record[2][0].trace_id for record in records] == ["second"]


def test_file_transport_switches_to_a_new_process_owned_file_after_fork(
    tmp_path: Path, monkeypatch
) -> None:
    process = [100]
    monkeypatch.setattr(agent_transport.os, "getpid", lambda: process[0])
    sink = FileCaptureSink(
        tmp_path,
        redaction_mode="redact",
        redaction_secret=None,
    )
    trace = Trace(started_at=datetime.now(timezone.utc), provider="test")

    sink.capture_trace(trace)
    inherited_lock = sink._lock
    inherited_lock.acquire()
    process[0] = 101
    try:
        sink.capture_trace(Trace(**{**trace.__dict__, "trace_id": "after-fork"}))
    finally:
        inherited_lock.release()
    sink.close()

    files = sorted(tmp_path.glob("verdict-agent-*.jsonl"))
    assert len(files) == 2
    assert sink._lock is not inherited_lock
    assert files[0].name.split("-")[2] != files[1].name.split("-")[2]


def test_file_transport_survives_process_exit_without_shutdown(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    script = """
import os
import sys
import verdict

verdict.init(
    transport="file",
    spool_directory=sys.argv[1],
    tenant_id="tenant-file",
    instrumentors=[],
)
for ordinal in range(10):
    with verdict.agent_run(name="agent", external_id=f"run-{ordinal}") as run:
        with run.turn(user_input="hello") as turn:
            turn.set_output("done")
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(spool)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    records = list(agent_transport.iter_capture_records(spool))
    assert len(records) == 40


def test_public_agent_sdk_example_writes_importable_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    example = Path(__file__).parents[3] / "examples" / "agent_sdk.py"

    runpy.run_path(str(example), run_name="__main__")

    database = tmp_path / "example.db"
    summary = import_main(
        [
            "agent-file",
            str(tmp_path / "verdict-capture"),
            "--storage",
            f"sqlite:///{database}",
        ]
    )
    assert summary == 0
    storage = SQLiteStorage(str(database))
    try:
        [bundle] = storage.list_agent_run_bundles("__verdict_local__")
        assert bundle.run.agent_name == "example-agent"
        assert [event.event_type for event in bundle.events] == [
            AgentEventType.TOOL_CALL,
            AgentEventType.TOOL_RESULT,
            AgentEventType.TEST_RESULT,
            AgentEventType.OUTCOME,
        ]
    finally:
        storage.close()
