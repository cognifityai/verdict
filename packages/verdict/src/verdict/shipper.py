"""Acknowledgement-driven shipping for Verdict's bounded local capture segments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol
from uuid import uuid4

from verdict.agent_transport import (
    MAX_RECORD_BYTES,
    capture_segment_identity,
    capture_segment_paths,
)
from verdict.collector import MAX_BATCH_BYTES, MAX_BATCH_RECORDS
from verdict.collector_receipts import (
    MAX_ACK_BYTES,
    ReceiptCorrupt,
    parse_acknowledgement,
    validate_transport_id,
)

CHECKPOINT_SCHEMA = "verdict-shipper-state-v1"
CHECKPOINT_NAME = ".verdict-shipper-state.json"
LOCK_NAME = ".verdict-shipper.lock"
MAX_CHECKPOINT_BYTES = 1024 * 1024
MAX_SEGMENTS = 4096
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_REQUEST_SECONDS = 15.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MAX_BATCHES = 128
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class ShippingError(RuntimeError):
    """A bounded transport failure that leaves source evidence in place."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class BatchSender(Protocol):
    @property
    def destination_fingerprint(self) -> str: ...

    def send(
        self,
        *,
        batch_id: str,
        producer_id: str,
        body: bytes,
    ) -> bytes: ...


@dataclass(frozen=True)
class _Progress:
    offset: int = 0
    rejected: bool = False
    prefix_sha256: str = _EMPTY_SHA256


@dataclass
class _State:
    segments: dict[str, _Progress]
    destination_fingerprint: str | None = None
    last_receipt_at: str | None = None
    last_batch_id: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class _Chunk:
    body: bytes
    end_offset: int
    record_digests: tuple[str, ...]
    incomplete: bool


@dataclass(frozen=True)
class ShippingSummary:
    accepted_records: int
    rejected_records: int
    deleted_segments: int
    quarantined_segments: int
    open_segments: int
    sealed_segments: int
    rejected_segments: int
    backlog_bytes: int
    pending_bytes: int
    incomplete_segments: int
    last_append_at: str | None
    last_receipt_at: str | None
    last_error: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "acceptedRecords": self.accepted_records,
            "rejectedRecords": self.rejected_records,
            "deletedSegments": self.deleted_segments,
            "quarantinedSegments": self.quarantined_segments,
            "openSegments": self.open_segments,
            "sealedSegments": self.sealed_segments,
            "rejectedSegments": self.rejected_segments,
            "backlogBytes": self.backlog_bytes,
            "pendingBytes": self.pending_bytes,
            "incompleteSegments": self.incomplete_segments,
            "lastAppendAt": self.last_append_at,
            "lastReceiptAt": self.last_receipt_at,
            "lastError": self.last_error,
        }


class HttpCollectorClient:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_REQUEST_SECONDS,
        allow_insecure_http: bool = False,
    ) -> None:
        if not endpoint.isascii() or any(ord(character) < 33 for character in endpoint):
            raise ValueError("collector URL must be bounded printable ASCII")
        parsed = urllib.parse.urlsplit(endpoint)
        try:
            _port = parsed.port
        except ValueError as exc:
            raise ValueError("collector URL has an invalid port") from exc
        if (
            parsed.scheme not in ({"https", "http"} if allow_insecure_http else {"https"})
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("collector URL must be an HTTPS origin or path")
        if not 0 < timeout_seconds <= 120:
            raise ValueError("collector request timeout is invalid")
        if (
            not isinstance(api_key, str)
            or not 16 <= len(api_key.encode("ascii", errors="ignore")) <= 512
            or any(ord(char) < 33 or ord(char) > 126 for char in api_key)
        ):
            raise ValueError("collector API key must be 16-512 printable ASCII bytes")
        self._url = endpoint.rstrip("/") + "/v1/ingest"
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._destination_fingerprint = hashlib.sha256(self._url.encode()).hexdigest()
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    @property
    def destination_fingerprint(self) -> str:
        return self._destination_fingerprint

    def send(self, *, batch_id: str, producer_id: str, body: bytes) -> bytes:
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/x-ndjson; charset=utf-8",
                "X-Verdict-Batch-ID": batch_id,
                "X-Verdict-Producer-ID": producer_id,
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:  # nosec B310
                payload = response.read(MAX_ACK_BYTES + 1)
                status_code = response.status
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {408, 425, 429} or exc.code >= 500
            exc.close()
            raise ShippingError(f"collector_http_{exc.code}", retryable=retryable) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise ShippingError("collector_unavailable", retryable=True) from exc
        if status_code != 200:
            retryable = status_code in {202, 408, 425, 429} or status_code >= 500
            raise ShippingError(f"collector_http_{status_code}", retryable=retryable)
        if len(payload) > MAX_ACK_BYTES:
            raise ShippingError("collector_invalid_response")
        return payload


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _DirectoryLock:
    def __init__(self, root: Path) -> None:
        self._path = root / LOCK_NAME
        self._handle: BinaryIO | None = None

    def __enter__(self) -> _DirectoryLock:
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - supported server platforms are POSIX
            raise RuntimeError("verdict-shipper requires POSIX file locking") from exc
        descriptor = os.open(
            self._path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ShippingError("shipper_lock_invalid")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ShippingError("shipper_already_running") from exc
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def _segment_key(producer_id: str, sequence: int) -> str:
    return f"{producer_id}:{sequence:012d}"


def _validate_progress_key(key: str) -> None:
    try:
        producer_id, sequence = key.rsplit(":", 1)
    except ValueError as exc:
        raise ShippingError("checkpoint_corrupt") from exc
    try:
        validate_transport_id(producer_id, field="producer_id")
    except ValueError as exc:
        raise ShippingError("checkpoint_corrupt") from exc
    if len(sequence) != 12 or not sequence.isascii() or not sequence.isdigit():
        raise ShippingError("checkpoint_corrupt")


def _load_state(root: Path) -> _State:
    path = root / CHECKPOINT_NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return _State(segments={})
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ShippingError("checkpoint_corrupt")
        raw = handle.read(MAX_CHECKPOINT_BYTES + 1)
    if not raw or len(raw) > MAX_CHECKPOINT_BYTES:
        raise ShippingError("checkpoint_corrupt")
    try:
        payload = json.loads(raw)
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShippingError("checkpoint_corrupt") from exc
    fields = {
        "schema",
        "segments",
        "destinationFingerprint",
        "lastReceiptAt",
        "lastBatchId",
        "lastError",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != fields
        or payload["schema"] != CHECKPOINT_SCHEMA
    ):
        raise ShippingError("checkpoint_corrupt")
    raw_segments = payload["segments"]
    if not isinstance(raw_segments, dict) or len(raw_segments) > MAX_SEGMENTS:
        raise ShippingError("checkpoint_corrupt")
    segments: dict[str, _Progress] = {}
    for key, value in raw_segments.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, dict)
            or set(value) != {"offset", "rejected", "prefixSha256"}
        ):
            raise ShippingError("checkpoint_corrupt")
        _validate_progress_key(key)
        offset, rejected, prefix_sha256 = (
            value["offset"],
            value["rejected"],
            value["prefixSha256"],
        )
        if (
            type(offset) is not int
            or not 0 <= offset <= 2**63 - 1
            or type(rejected) is not bool
            or not isinstance(prefix_sha256, str)
            or len(prefix_sha256) != 64
            or any(character not in "0123456789abcdef" for character in prefix_sha256)
        ):
            raise ShippingError("checkpoint_corrupt")
        if offset == 0 and prefix_sha256 != _EMPTY_SHA256:
            raise ShippingError("checkpoint_corrupt")
        segments[key] = _Progress(offset, rejected, prefix_sha256)
    last_batch = payload["lastBatchId"]
    if last_batch is not None:
        try:
            validate_transport_id(last_batch, field="batch_id")
        except ValueError as exc:
            raise ShippingError("checkpoint_corrupt") from exc
    last_receipt = payload["lastReceiptAt"]
    if last_receipt is not None:
        try:
            parsed = datetime.fromisoformat(last_receipt)
        except (TypeError, ValueError) as exc:
            raise ShippingError("checkpoint_corrupt") from exc
        if parsed.tzinfo is None:
            raise ShippingError("checkpoint_corrupt")
    last_error = payload["lastError"]
    if last_error is not None and (not isinstance(last_error, str) or len(last_error) > 128):
        raise ShippingError("checkpoint_corrupt")
    destination = payload["destinationFingerprint"]
    if destination is not None and (
        not isinstance(destination, str)
        or len(destination) != 64
        or any(character not in "0123456789abcdef" for character in destination)
    ):
        raise ShippingError("checkpoint_corrupt")
    return _State(
        segments=segments,
        destination_fingerprint=destination,
        last_receipt_at=last_receipt,
        last_batch_id=last_batch,
        last_error=last_error,
    )


def _state_bytes(state: _State) -> bytes:
    payload = {
        "schema": CHECKPOINT_SCHEMA,
        "destinationFingerprint": state.destination_fingerprint,
        "segments": {
            key: {
                "offset": value.offset,
                "rejected": value.rejected,
                "prefixSha256": value.prefix_sha256,
            }
            for key, value in sorted(state.segments.items())
        },
        "lastReceiptAt": state.last_receipt_at,
        "lastBatchId": state.last_batch_id,
        "lastError": state.last_error,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise ShippingError("checkpoint_too_large")
    return encoded


def _save_state(root: Path, state: _State) -> None:
    target = root / CHECKPOINT_NAME
    temporary = root / f"{CHECKPOINT_NAME}.{os.getpid()}.{uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        encoded = _state_bytes(state)
        with os.fdopen(descriptor, "wb", buffering=0) as handle:
            written = 0
            while written < len(encoded):
                count = handle.write(encoded[written:])
                if not isinstance(count, int) or count <= 0:
                    raise OSError("shipper checkpoint write was incomplete")
                written += count
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _read_chunk(path: Path, offset: int) -> _Chunk:
    if offset < 0 or path.is_symlink():
        raise ShippingError("segment_changed")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or offset > metadata.st_size:
            raise ShippingError("segment_changed")
        if offset:
            handle.seek(offset - 1)
            if handle.read(1) != b"\n":
                raise ShippingError("checkpoint_misaligned")
        handle.seek(offset)
        records: list[bytes] = []
        digests: list[str] = []
        body_size = 0
        incomplete = False
        while len(records) < MAX_BATCH_RECORDS:
            start = handle.tell()
            raw = handle.readline(MAX_RECORD_BYTES + 2)
            if not raw:
                break
            if not raw.endswith(b"\n"):
                if len(raw) > MAX_RECORD_BYTES + 1:
                    raise ShippingError("segment_record_too_large")
                incomplete = True
                handle.seek(start)
                break
            if len(raw) - 1 > MAX_RECORD_BYTES:
                raise ShippingError("segment_record_too_large")
            if records and body_size + len(raw) > MAX_BATCH_BYTES:
                handle.seek(start)
                break
            records.append(raw)
            body_size += len(raw)
            digests.append(hashlib.sha256(raw[:-1]).hexdigest())
        return _Chunk(b"".join(records), handle.tell(), tuple(digests), incomplete)


def _prefix_hasher(path: Path, progress: _Progress) -> tuple[Any, tuple[int, int]]:
    digest = hashlib.sha256()
    remaining = progress.offset
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ShippingError("segment_changed")
        while remaining:
            block = handle.read(min(remaining, 1024 * 1024))
            if not block:
                raise ShippingError("segment_changed")
            digest.update(block)
            remaining -= len(block)
    if digest.hexdigest() != progress.prefix_sha256:
        raise ShippingError("segment_changed")
    return digest, (metadata.st_dev, metadata.st_ino)


def _require_same_file(path: Path, expected: tuple[int, int]) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != expected:
        raise ShippingError("segment_changed")


def _batch_id(producer_id: str, sequence: int, start: int, end: int, body: bytes) -> str:
    producer_digest = hashlib.sha256(producer_id.encode("ascii")).hexdigest()[:16]
    body_digest = hashlib.sha256(body).hexdigest()[:24]
    value = f"ship.{producer_digest}.{sequence}.{start}.{end}.{body_digest}"
    validate_transport_id(value, field="batch_id")
    return value


def _spool_root(spool_directory: str | Path) -> Path:
    root = Path(spool_directory).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("shipper spool directory must be an existing directory")
    return root


def _shipping_summary(
    root: Path,
    state: _State,
    *,
    accepted_records: int = 0,
    rejected_records: int = 0,
    deleted_segments: int = 0,
    quarantined_segments: int = 0,
) -> ShippingSummary:
    counts = {"open": 0, "jsonl": 0, "rejected": 0}
    backlog = pending = incomplete = rejected_segments = 0
    last_append: datetime | None = None
    for path in capture_segment_paths(root, maximum=MAX_SEGMENTS):
        identity = capture_segment_identity(path)
        assert identity is not None
        lifecycle = identity[2]
        counts[lifecycle] += 1
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ShippingError("segment_changed")
            size = metadata.st_size
            incomplete_tail = False
            if size:
                handle.seek(-1, os.SEEK_END)
                incomplete_tail = handle.read(1) != b"\n"
        modified = datetime.fromtimestamp(metadata.st_mtime, timezone.utc)
        if lifecycle != "rejected" and (last_append is None or modified > last_append):
            last_append = modified
        if lifecycle != "rejected":
            progress = state.segments.get(_segment_key(identity[0], identity[1]), _Progress())
            if progress.offset > size:
                raise ShippingError("segment_changed")
            rejected_segments += progress.rejected
            backlog += size
            pending += size - progress.offset
            incomplete += incomplete_tail
    return ShippingSummary(
        accepted_records=accepted_records,
        rejected_records=rejected_records,
        deleted_segments=deleted_segments,
        quarantined_segments=quarantined_segments,
        open_segments=counts["open"],
        sealed_segments=counts["jsonl"],
        rejected_segments=counts["rejected"] + rejected_segments,
        backlog_bytes=backlog,
        pending_bytes=pending,
        incomplete_segments=incomplete,
        last_append_at=last_append.isoformat() if last_append is not None else None,
        last_receipt_at=state.last_receipt_at,
        last_error=state.last_error,
    )


def read_shipping_status(spool_directory: str | Path) -> ShippingSummary:
    """Read bounded local delivery health without contacting a collector."""
    try:
        root = _spool_root(spool_directory)
        return _shipping_summary(root, _load_state(root))
    except ShippingError:
        raise
    except OSError as exc:
        raise ShippingError("spool_io_error", retryable=True) from exc
    except ValueError as exc:
        raise ShippingError("spool_invalid") from exc


class SegmentShipper:
    def __init__(
        self,
        spool_directory: str | Path,
        sender: BatchSender,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_batches: int = DEFAULT_MAX_BATCHES,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.root = _spool_root(spool_directory)
        if not 1 <= max_attempts <= 20 or not 1 <= max_batches <= 4096:
            raise ValueError("shipper work limits are invalid")
        self.sender = sender
        self.destination_fingerprint = sender.destination_fingerprint
        if len(self.destination_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.destination_fingerprint
        ):
            raise ValueError("shipper destination fingerprint is invalid")
        self.max_attempts = max_attempts
        self.max_batches = max_batches
        self.stop_event = stop_event or threading.Event()

    def _send(self, *, batch_id: str, producer_id: str, body: bytes) -> bytes:
        failure: ShippingError | None = None
        for attempt in range(self.max_attempts):
            try:
                return self.sender.send(batch_id=batch_id, producer_id=producer_id, body=body)
            except ShippingError as exc:
                failure = exc
                if not exc.retryable or attempt + 1 == self.max_attempts:
                    break
                if self.stop_event.wait(min(2**attempt, 8)):
                    break
        assert failure is not None
        raise failure

    def ship_once(self) -> ShippingSummary:
        with _DirectoryLock(self.root):
            return self._ship_once_locked()

    def _ship_once_locked(self) -> ShippingSummary:
        try:
            return self._ship_cycle()
        except ShippingError:
            raise
        except OSError as exc:
            raise ShippingError("spool_io_error", retryable=True) from exc
        except ValueError as exc:
            raise ShippingError("spool_invalid") from exc

    def _ship_cycle(self) -> ShippingSummary:
        accepted = rejected = deleted = quarantined = batches = 0
        state = _load_state(self.root)
        try:
            if (
                state.destination_fingerprint is not None
                and state.destination_fingerprint != self.destination_fingerprint
            ):
                raise ShippingError("collector_destination_changed")
            paths = capture_segment_paths(self.root, maximum=MAX_SEGMENTS)
            active_identities: set[str] = set()
            for path in paths:
                identity = capture_segment_identity(path)
                assert identity is not None
                if identity[2] == "rejected":
                    continue
                key = _segment_key(identity[0], identity[1])
                if key in active_identities:
                    raise ShippingError("segment_identity_conflict")
                active_identities.add(key)
            visible_keys = {
                _segment_key(identity[0], identity[1])
                for path in paths
                if (identity := capture_segment_identity(path)) is not None
                and identity[2] != "rejected"
            }
            state.segments = {
                key: progress for key, progress in state.segments.items() if key in visible_keys
            }
            for path in paths:
                identity = capture_segment_identity(path)
                assert identity is not None
                producer_id, sequence, lifecycle = identity
                if lifecycle == "rejected":
                    continue
                key = _segment_key(producer_id, sequence)
                progress = state.segments.get(key, _Progress())
                prefix_hasher, source_identity = _prefix_hasher(path, progress)
                while batches < self.max_batches and not self.stop_event.is_set():
                    chunk = _read_chunk(path, progress.offset)
                    if not chunk.body:
                        break
                    batch_id = _batch_id(
                        producer_id,
                        sequence,
                        progress.offset,
                        chunk.end_offset,
                        chunk.body,
                    )
                    if state.destination_fingerprint is None:
                        state.destination_fingerprint = self.destination_fingerprint
                        _save_state(self.root, state)
                    response = self._send(
                        batch_id=batch_id,
                        producer_id=producer_id,
                        body=chunk.body,
                    )
                    try:
                        acknowledgement = parse_acknowledgement(response, batch_id)
                    except ReceiptCorrupt as exc:
                        raise ShippingError("collector_invalid_acknowledgement") from exc
                    results = acknowledgement["results"]
                    if tuple(item["recordDigest"] for item in results) != chunk.record_digests:
                        raise ShippingError("collector_digest_mismatch")
                    batch_accepted = int(acknowledgement["accepted"])
                    batch_rejected = int(acknowledgement["rejected"])
                    accepted += batch_accepted
                    rejected += batch_rejected
                    state.last_batch_id = batch_id
                    state.last_receipt_at = datetime.now(timezone.utc).isoformat()
                    state.last_error = None
                    prefix_hasher.update(chunk.body)
                    progress = _Progress(
                        offset=chunk.end_offset,
                        rejected=progress.rejected or batch_rejected > 0,
                        prefix_sha256=prefix_hasher.hexdigest(),
                    )
                    state.segments[key] = progress
                    _save_state(self.root, state)
                    batches += 1
                if lifecycle == "jsonl" and path.exists():
                    size = path.stat().st_size
                    final = _read_chunk(path, progress.offset)
                    if not final.body and not final.incomplete and progress.offset == size:
                        _require_same_file(path, source_identity)
                        if progress.rejected:
                            target = path.with_suffix(".rejected")
                            if target.exists():
                                raise ShippingError("segment_identity_conflict")
                            path.rename(target)
                            quarantined += 1
                        else:
                            path.unlink()
                            deleted += 1
                        state.segments.pop(key, None)
                        _save_state(self.root, state)
                if batches >= self.max_batches:
                    break
            state.last_error = None
            _save_state(self.root, state)
        except ShippingError as exc:
            state.last_error = exc.code
            _save_state(self.root, state)
            raise
        return _shipping_summary(
            self.root,
            state,
            accepted_records=accepted,
            rejected_records=rejected,
            deleted_segments=deleted,
            quarantined_segments=quarantined,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ship Verdict Agent capture segments.")
    parser.add_argument("--spool-directory", required=True)
    parser.add_argument("--collector-url")
    parser.add_argument("--api-key-env", default="VERDICT_COLLECTOR_API_KEY")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--max-batches", type=int, default=DEFAULT_MAX_BATCHES)
    args = parser.parse_args(argv)
    if not 0.1 <= args.poll_seconds <= 3600:
        parser.error("--poll-seconds must be between 0.1 and 3600")
    if args.status:
        try:
            summary = read_shipping_status(args.spool_directory)
        except (ShippingError, ValueError) as exc:
            code = exc.code if isinstance(exc, ShippingError) else str(exc)
            print(json.dumps({"status": "error", "error": code}), file=sys.stderr)
            return 1
        status_payload = {"status": "ok", **summary.as_dict()}
        print(json.dumps(status_payload, sort_keys=True) if args.json else status_payload)
        return 0
    if args.collector_url is None:
        parser.error("--collector-url is required unless --status is used")
    api_key = os.environ.get(args.api_key_env)
    if api_key is None:
        parser.error(f"collector API key environment variable {args.api_key_env!r} is missing")
    try:
        sender = HttpCollectorClient(
            args.collector_url,
            api_key,
            timeout_seconds=args.request_timeout,
            allow_insecure_http=args.allow_insecure_http,
        )
        stop = threading.Event()
        shipper = SegmentShipper(
            args.spool_directory,
            sender,
            max_attempts=args.max_attempts,
            max_batches=args.max_batches,
            stop_event=stop,
        )
    except ValueError as exc:
        parser.error(str(exc))

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)

    def report_failure(exc: ShippingError) -> None:
        payload: dict[str, object] = {"status": "error", "error": exc.code}
        try:
            payload.update(read_shipping_status(args.spool_directory).as_dict())
        except ShippingError:
            pass
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)

    def run_cycles(*, lock_held: bool) -> int:
        while not stop.is_set():
            try:
                summary = shipper._ship_once_locked() if lock_held else shipper.ship_once()
            except ShippingError as exc:
                report_failure(exc)
                if args.once or not exc.retryable:
                    return 1
            else:
                payload = {"status": "ok", **summary.as_dict()}
                print(json.dumps(payload, sort_keys=True) if args.json else payload)
                if args.once:
                    return 0
            stop.wait(args.poll_seconds)
        return 0

    if args.once:
        return run_cycles(lock_held=False)
    try:
        with _DirectoryLock(shipper.root):
            return run_cycles(lock_held=True)
    except ShippingError as exc:
        report_failure(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
