from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from verdict import shipper as shipper_module
from verdict.agent_transport import FileCaptureSink
from verdict.schema import Trace
from verdict.shipper import (
    CHECKPOINT_NAME,
    HttpCollectorClient,
    SegmentShipper,
    ShippingError,
    _DirectoryLock,
    main,
    read_shipping_status,
)


class AckingSender:
    def __init__(
        self,
        *,
        reject_trace: bool = False,
        transient: int = 0,
        destination: str = "1" * 64,
    ) -> None:
        self.reject_trace = reject_trace
        self.transient = transient
        self.destination = destination
        self.calls: list[tuple[str, str, bytes]] = []
        self.receipts: dict[str, tuple[bytes, bytes]] = {}

    @property
    def destination_fingerprint(self) -> str:
        return self.destination

    def send(self, *, batch_id: str, producer_id: str, body: bytes) -> bytes:
        self.calls.append((batch_id, producer_id, body))
        if self.transient:
            self.transient -= 1
            raise ShippingError("collector_unavailable", retryable=True)
        lines = body.split(b"\n")
        if lines[-1] == b"":
            lines.pop()
        results = []
        accepted = 0
        for index, line in enumerate(lines):
            rejected = self.reject_trace and b'"kind":"trace"' in line
            accepted += not rejected
            results.append(
                {
                    "index": index,
                    "recordDigest": hashlib.sha256(line).hexdigest(),
                    "status": "rejected" if rejected else "accepted",
                    "error": "unsupported_record_kind" if rejected else None,
                }
            )
        payload = json.dumps(
            {
                "schema": "verdict-ingest-ack-v1",
                "batchId": batch_id,
                "accepted": accepted,
                "rejected": len(lines) - accepted,
                "results": results,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        prior = self.receipts.setdefault(batch_id, (body, payload))
        if prior[0] != body:
            raise ShippingError("batch_conflict")
        return prior[1]


def _segment(root: Path, data: bytes, *, state: str = "open") -> Path:
    path = root / f"verdict-agent-producer1-000000.{state}"
    path.write_bytes(data)
    return path


def test_file_sink_exposes_only_sealed_segments_as_jsonl(tmp_path: Path) -> None:
    sink = FileCaptureSink(tmp_path, redaction_mode="redact", redaction_secret=None)
    sink.capture_trace(Trace(started_at=datetime.now(timezone.utc), provider="test"))
    [active] = tmp_path.glob("verdict-agent-*.open")
    assert not list(tmp_path.glob("verdict-agent-*.jsonl"))

    sink.close()

    assert not active.exists()
    assert len(list(tmp_path.glob("verdict-agent-*.jsonl"))) == 1


def test_active_segment_ships_incrementally_and_deletes_only_after_seal(
    tmp_path: Path,
) -> None:
    path = _segment(tmp_path, b'{"n":1}\n{"n":2}\n')
    sender = AckingSender()
    shipper = SegmentShipper(tmp_path, sender)

    first = shipper.ship_once()
    assert (first.accepted_records, first.pending_bytes, first.open_segments) == (2, 0, 1)
    assert path.exists()

    with path.open("ab") as handle:
        handle.write(b'{"n":3}\n')
    second = shipper.ship_once()
    assert second.accepted_records == 1
    assert [call[2] for call in sender.calls] == [b'{"n":1}\n{"n":2}\n', b'{"n":3}\n']

    sealed = path.with_suffix(".jsonl")
    path.replace(sealed)
    third = shipper.ship_once()
    assert third.deleted_segments == 1
    assert not sealed.exists()


def test_partial_tail_is_retained_and_never_sent(tmp_path: Path) -> None:
    path = _segment(tmp_path, b'{"complete":true}\n{"partial":')
    sender = AckingSender()

    summary = SegmentShipper(tmp_path, sender).ship_once()

    assert sender.calls[0][2] == b'{"complete":true}\n'
    assert summary.incomplete_segments == 1
    assert summary.pending_bytes == len(b'{"partial":')
    assert path.exists()


def test_crlf_record_digest_matches_the_exact_collector_line(tmp_path: Path) -> None:
    _segment(tmp_path, b'{"n":1}\r\n', state="jsonl")
    sender = AckingSender()

    summary = SegmentShipper(tmp_path, sender).ship_once()

    assert summary.accepted_records == 1
    assert summary.deleted_segments == 1


def test_rejected_record_quarantines_sealed_segment_without_deleting_it(
    tmp_path: Path,
) -> None:
    data = b'{"kind":"agent"}\n{"kind":"trace"}\n'
    path = _segment(tmp_path, data, state="jsonl")

    summary = SegmentShipper(tmp_path, AckingSender(reject_trace=True)).ship_once()

    rejected = path.with_suffix(".rejected")
    assert summary.accepted_records == 1
    assert summary.rejected_records == 1
    assert summary.quarantined_segments == 1
    assert rejected.read_bytes() == data


def test_rejection_remains_visible_while_segment_is_still_open(tmp_path: Path) -> None:
    path = _segment(tmp_path, b'{"kind":"trace"}\n')

    summary = SegmentShipper(tmp_path, AckingSender(reject_trace=True)).ship_once()

    assert path.exists()
    assert summary.rejected_records == 1
    assert summary.rejected_segments == 1
    assert read_shipping_status(tmp_path).rejected_segments == 1


def test_retry_is_bounded_and_reuses_exact_batch(tmp_path: Path) -> None:
    _segment(tmp_path, b'{"n":1}\n', state="jsonl")
    sender = AckingSender(transient=2)

    summary = SegmentShipper(tmp_path, sender, max_attempts=3).ship_once()

    assert summary.deleted_segments == 1
    assert len(sender.calls) == 3
    assert len({(call[0], call[2]) for call in sender.calls}) == 1


def test_record_limit_resumes_from_checkpoint_without_resending(tmp_path: Path) -> None:
    _segment(tmp_path, b"{}\n" * 501, state="jsonl")
    sender = AckingSender()

    first = SegmentShipper(tmp_path, sender, max_batches=1).ship_once()
    second = SegmentShipper(tmp_path, sender, max_batches=1).ship_once()

    assert first.accepted_records == 500
    assert first.deleted_segments == 0
    assert second.accepted_records == 1
    assert second.deleted_segments == 1
    assert [len(call[2].splitlines()) for call in sender.calls] == [500, 1]


def test_invalid_ack_does_not_advance_or_delete(tmp_path: Path) -> None:
    class InvalidSender:
        @property
        def destination_fingerprint(self) -> str:
            return "1" * 64

        def send(self, *, batch_id: str, producer_id: str, body: bytes) -> bytes:
            return b"{}"

    path = _segment(tmp_path, b'{"n":1}\n', state="jsonl")

    with pytest.raises(ShippingError, match="collector_invalid_acknowledgement"):
        SegmentShipper(tmp_path, InvalidSender()).ship_once()

    assert path.exists()
    state = json.loads((tmp_path / CHECKPOINT_NAME).read_bytes())
    assert state["segments"] == {}


def test_corrupt_checkpoint_fails_before_network_or_deletion(tmp_path: Path) -> None:
    path = _segment(tmp_path, b'{"n":1}\n', state="jsonl")
    (tmp_path / CHECKPOINT_NAME).write_text("not-json")
    sender = AckingSender()

    with pytest.raises(ShippingError, match="checkpoint_corrupt"):
        SegmentShipper(tmp_path, sender).ship_once()

    assert sender.calls == []
    assert path.exists()


def test_acknowledged_prefix_mutation_fails_closed(tmp_path: Path) -> None:
    path = _segment(tmp_path, b'{"n":1}\n')
    sender = AckingSender()
    SegmentShipper(tmp_path, sender).ship_once()
    path.write_bytes(b'{"n":2}\n')

    with pytest.raises(ShippingError, match="segment_changed"):
        SegmentShipper(tmp_path, sender).ship_once()

    assert len(sender.calls) == 1


def test_checkpoint_cannot_silently_switch_collectors(tmp_path: Path) -> None:
    _segment(tmp_path, b'{"n":1}\n')
    SegmentShipper(tmp_path, AckingSender()).ship_once()
    changed = AckingSender(destination="2" * 64)

    with pytest.raises(ShippingError, match="collector_destination_changed"):
        SegmentShipper(tmp_path, changed).ship_once()

    assert changed.calls == []


def test_checkpoint_write_failure_causes_safe_exact_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _segment(tmp_path, b'{"n":1}\n', state="jsonl")
    sender = AckingSender()
    save = shipper_module._save_state
    attempts = 0

    def fail_first(root, state):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("injected checkpoint failure")
        return save(root, state)

    monkeypatch.setattr(shipper_module, "_save_state", fail_first)
    with pytest.raises(ShippingError, match="spool_io_error"):
        SegmentShipper(tmp_path, sender).ship_once()
    assert path.exists()

    summary = SegmentShipper(tmp_path, sender).ship_once()
    assert summary.deleted_segments == 1
    assert len(sender.calls) == 2
    assert sender.calls[0] == sender.calls[1]


def test_delete_failure_keeps_acknowledged_file_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _segment(tmp_path, b'{"n":1}\n', state="jsonl")
    sender = AckingSender()
    unlink = Path.unlink

    def fail_segment(target: Path, *args, **kwargs):
        if target == path:
            raise OSError("injected delete failure")
        return unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_segment)
    with pytest.raises(ShippingError, match="spool_io_error"):
        SegmentShipper(tmp_path, sender).ship_once()
    assert path.exists()

    monkeypatch.setattr(Path, "unlink", unlink)
    summary = SegmentShipper(tmp_path, sender).ship_once()
    assert summary.deleted_segments == 1
    assert len(sender.calls) == 1


def test_replaced_sealed_path_is_not_deleted_after_acknowledgement(tmp_path: Path) -> None:
    path = _segment(tmp_path, b'{"n":1}\n', state="jsonl")

    class ReplacingSender(AckingSender):
        def send(self, *, batch_id: str, producer_id: str, body: bytes) -> bytes:
            response = super().send(batch_id=batch_id, producer_id=producer_id, body=body)
            path.unlink()
            path.write_bytes(b'{"n":2}\n')
            return response

    with pytest.raises(ShippingError, match="segment_changed"):
        SegmentShipper(tmp_path, ReplacingSender()).ship_once()

    assert path.read_bytes() == b'{"n":2}\n'


def test_symlinked_segment_is_never_read_or_deleted(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.jsonl"
    outside.write_bytes(b'{"n":1}\n')
    link = tmp_path / "verdict-agent-producer1-000000.jsonl"
    link.symlink_to(outside)
    sender = AckingSender()

    summary = SegmentShipper(tmp_path, sender).ship_once()

    assert sender.calls == []
    assert outside.read_bytes() == b'{"n":1}\n'
    assert link.is_symlink()
    assert summary.sealed_segments == 0


def test_second_shipper_cannot_share_one_checkpoint_owner(tmp_path: Path) -> None:
    _segment(tmp_path, b'{"n":1}\n')
    with _DirectoryLock(tmp_path):
        with pytest.raises(ShippingError, match="shipper_already_running"):
            SegmentShipper(tmp_path, AckingSender()).ship_once()
        assert read_shipping_status(tmp_path).pending_bytes == len(b'{"n":1}\n')


def test_http_client_requires_https_unless_explicitly_allowed() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        HttpCollectorClient("http://collector.test", "x" * 32)
    client = HttpCollectorClient(
        "http://127.0.0.1:8765",
        "x" * 32,
        allow_insecure_http=True,
    )
    assert client is not None


def test_status_reports_local_backlog_without_network_or_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _segment(tmp_path, b'{"n":1}\n{"partial":')
    modified = datetime(2026, 9, 9, tzinfo=timezone.utc).timestamp()
    os.utime(path, (modified, modified))

    summary = read_shipping_status(tmp_path)

    assert summary.pending_bytes == path.stat().st_size
    assert summary.open_segments == 1
    assert summary.incomplete_segments == 1
    assert summary.last_append_at == "2026-09-09T00:00:00+00:00"
    assert main(["--spool-directory", str(tmp_path), "--status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["pendingBytes"] == path.stat().st_size


def test_transport_failure_reports_local_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _segment(tmp_path, b'{"n":1}\n')

    def unavailable(self, *, batch_id: str, producer_id: str, body: bytes) -> bytes:
        raise ShippingError("collector_unavailable", retryable=True)

    monkeypatch.setattr(HttpCollectorClient, "send", unavailable)
    monkeypatch.setenv("TEST_VERDICT_COLLECTOR_KEY", "x" * 32)
    result = main(
        [
            "--spool-directory",
            str(tmp_path),
            "--collector-url",
            "http://127.0.0.1:9999",
            "--allow-insecure-http",
            "--api-key-env",
            "TEST_VERDICT_COLLECTOR_KEY",
            "--max-attempts",
            "1",
            "--once",
        ]
    )

    assert result == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "collector_unavailable"
    assert payload["pendingBytes"] == path.stat().st_size


def test_continuous_cli_owns_the_spool_until_shutdown(tmp_path: Path) -> None:
    environment = {**os.environ, "TEST_VERDICT_COLLECTOR_KEY": "x" * 32}
    base = [
        sys.executable,
        "-m",
        "verdict.shipper",
        "--spool-directory",
        str(tmp_path),
        "--collector-url",
        "http://127.0.0.1:9",
        "--allow-insecure-http",
        "--api-key-env",
        "TEST_VERDICT_COLLECTOR_KEY",
    ]
    owner = subprocess.Popen(base, env=environment)
    try:
        deadline = time.monotonic() + 5
        checkpoint = tmp_path / CHECKPOINT_NAME
        while not checkpoint.exists() and time.monotonic() < deadline:
            assert owner.poll() is None
            time.sleep(0.02)
        assert checkpoint.exists()
        contender = None
        while time.monotonic() < deadline:
            contender = subprocess.run(
                [*base, "--once"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if contender.returncode == 1 and "shipper_already_running" in contender.stderr:
                break
            time.sleep(0.02)
        assert contender is not None
        assert contender.returncode == 1
        assert "shipper_already_running" in contender.stderr
    finally:
        owner.terminate()
        assert owner.wait(timeout=5) == 0
