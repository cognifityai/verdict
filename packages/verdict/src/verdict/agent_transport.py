"""Bounded local capture transport for the framework-neutral Agent SDK."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol
from uuid import uuid4

from verdict.capture import AgentCaptureService
from verdict.evidence import (
    AgentCaptureBatch,
    agent_capture_batch_from_json,
    agent_capture_batch_to_json,
)
from verdict.normalized_evidence import prepare_agent_capture_batch
from verdict.redaction import RedactionMode, sanitize_trace
from verdict.schema import Operation, Trace, populate_trace_analysis_fields
from verdict.storage.base import Storage

CAPTURE_SCHEMA = "verdict-agent-capture-v1"
DEFAULT_SEGMENT_BYTES = 8 * 1024 * 1024
DEFAULT_DIRECTORY_BYTES = 128 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
_TRACE_TRANSPORT_FIELDS = frozenset(
    {
        "trace_id",
        "parent_span_id",
        "started_at",
        "ended_at",
        "provider",
        "operation",
        "request_model",
        "response_model",
        "input_tokens",
        "output_tokens",
        "temperature",
        "max_tokens",
        "finish_reason",
        "error",
        "latency_ms",
        "prompt_redacted",
        "response_redacted",
        "raw_messages",
        "tenant_id",
        "session_id",
        "user_id_hash",
        "cluster_id",
        "tags",
        "cost_usd",
    }
)


class CaptureQuotaExceeded(RuntimeError):
    """The local capture directory reached its configured hard bound."""


class AgentCaptureSink(Protocol):
    def capture_trace(self, trace: Trace) -> None: ...

    def capture_agent(
        self,
        batch: AgentCaptureBatch,
        traces: tuple[Trace, ...] = (),
    ) -> None: ...

    def close(self) -> None: ...


class StorageCaptureSink:
    """Use Verdict's canonical storage transaction boundary directly."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._agent_capture = AgentCaptureService(storage)

    def capture_trace(self, trace: Trace) -> None:
        self.storage.insert_trace(trace)

    def capture_agent(
        self,
        batch: AgentCaptureBatch,
        traces: tuple[Trace, ...] = (),
    ) -> None:
        self._agent_capture.append(batch, traces=traces)

    def close(self) -> None:
        pass


def _trace_to_payload(trace: Trace) -> dict[str, Any]:
    payload = {key: value for key, value in asdict(trace).items() if key in _TRACE_TRANSPORT_FIELDS}
    payload["started_at"] = trace.started_at.isoformat()
    payload["ended_at"] = trace.ended_at.isoformat() if trace.ended_at else None
    payload["operation"] = trace.operation.value
    return payload


def _trace_from_payload(payload: object) -> Trace:
    if not isinstance(payload, dict) or set(payload) != _TRACE_TRANSPORT_FIELDS:
        raise ValueError("agent capture Trace has an invalid shape")
    values = dict(payload)
    try:
        started_at = datetime.fromisoformat(values.pop("started_at"))
        ended_raw = values.pop("ended_at")
        ended_at = datetime.fromisoformat(ended_raw) if ended_raw is not None else None
        operation = Operation(values.pop("operation"))
        trace = Trace(
            started_at=started_at,
            ended_at=ended_at,
            operation=operation,
            **values,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("agent capture Trace has invalid typed fields") from exc
    if trace.started_at.tzinfo is None or (
        trace.ended_at is not None and trace.ended_at.tzinfo is None
    ):
        raise ValueError("agent capture Trace timestamps must be timezone-aware")
    return trace


def _record_bytes(
    kind: Literal["trace", "agent"],
    *,
    batch: AgentCaptureBatch | None,
    traces: tuple[Trace, ...],
) -> bytes:
    payload: dict[str, Any] = {
        "schema": CAPTURE_SCHEMA,
        "kind": kind,
        "batch": (json.loads(agent_capture_batch_to_json(batch)) if batch is not None else None),
        "traces": [_trace_to_payload(trace) for trace in traces],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError("agent capture record exceeds the local transport limit")
    return encoded + b"\n"


def decode_capture_record(
    raw: bytes,
) -> tuple[Literal["trace", "agent"], AgentCaptureBatch | None, tuple[Trace, ...]]:
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("agent capture record exceeds the local transport limit")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("agent capture record is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "kind",
        "batch",
        "traces",
    }:
        raise ValueError("agent capture record has an invalid shape")
    if payload["schema"] != CAPTURE_SCHEMA or payload["kind"] not in {"trace", "agent"}:
        raise ValueError("agent capture record has an unsupported schema")
    traces_payload = payload["traces"]
    if not isinstance(traces_payload, list) or len(traces_payload) > 1:
        raise ValueError("agent capture record has an invalid Trace list")
    traces = tuple(_trace_from_payload(item) for item in traces_payload)
    if payload["kind"] == "trace":
        if payload["batch"] is not None or len(traces) != 1:
            raise ValueError("standalone Trace capture record is invalid")
        return "trace", None, traces
    if not isinstance(payload["batch"], dict):
        raise ValueError("agent evidence capture record is invalid")
    batch = agent_capture_batch_from_json(
        json.dumps(payload["batch"], ensure_ascii=False, separators=(",", ":"))
    )
    prepare_agent_capture_batch(batch, traces)
    return "agent", batch, traces


class FileCaptureSink:
    """Append redacted records to bounded process-owned JSONL segments."""

    def __init__(
        self,
        directory: str | Path,
        *,
        redaction_mode: RedactionMode,
        redaction_secret: str | None,
        segment_bytes: int | None = None,
        directory_bytes: int | None = None,
    ) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.segment_bytes = DEFAULT_SEGMENT_BYTES if segment_bytes is None else segment_bytes
        self.directory_bytes = (
            DEFAULT_DIRECTORY_BYTES if directory_bytes is None else directory_bytes
        )
        if not MAX_RECORD_BYTES < self.segment_bytes <= self.directory_bytes:
            raise ValueError("file transport byte limits are invalid")
        self.redaction_mode = redaction_mode
        self.redaction_secret = redaction_secret
        self._lock = threading.Lock()
        self._pid = -1
        self._producer_id = ""
        self._segment = 0
        self._file: BinaryIO | None = None
        self._size = 0
        self._directory_size = 0
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self._reset_process_file()

    def _reset_process_file(self) -> None:
        if self._file is not None:
            self._file.close()
        self._pid = os.getpid()
        self._producer_id = uuid4().hex
        self._segment = 0
        self._directory_size = sum(
            path.stat().st_size
            for path in self.directory.glob("verdict-agent-*.jsonl")
            if path.is_file()
        )
        self._open_segment()

    def _open_segment(self) -> None:
        while True:
            path = self.directory / (f"verdict-agent-{self._producer_id}-{self._segment:06d}.jsonl")
            self._segment += 1
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                continue
            self._file = os.fdopen(descriptor, "ab", buffering=0)
            self._size = 0
            return

    def _append(self, record: bytes) -> None:
        if self._pid != os.getpid():
            # A fork can inherit a lock held by a thread that no longer exists.
            self._lock = threading.Lock()
        with self._lock:
            if self._pid != os.getpid():
                self._reset_process_file()
            if self._directory_size + len(record) > self.directory_bytes:
                raise CaptureQuotaExceeded("agent capture directory quota exceeded")
            if self._size and self._size + len(record) > self.segment_bytes:
                assert self._file is not None
                self._file.close()
                self._file = None
                self._open_segment()
            assert self._file is not None
            self._file.write(record)
            self._size += len(record)
            self._directory_size += len(record)

    def capture_trace(self, trace: Trace) -> None:
        prepared = sanitize_trace(
            deepcopy(trace), mode=self.redaction_mode, secret=self.redaction_secret
        )
        populate_trace_analysis_fields(prepared)
        self._append(_record_bytes("trace", batch=None, traces=(prepared,)))

    def capture_agent(
        self,
        batch: AgentCaptureBatch,
        traces: tuple[Trace, ...] = (),
    ) -> None:
        prepared_batch, prepared_traces, _links = prepare_agent_capture_batch(
            batch,
            traces,
            mode=self.redaction_mode,
            secret=self.redaction_secret,
        )
        self._append(_record_bytes("agent", batch=prepared_batch, traces=prepared_traces))

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


def iter_capture_records(
    path: str | Path,
    *,
    on_incomplete: Callable[[], None] | None = None,
) -> Iterator[tuple[Literal["trace", "agent"], AgentCaptureBatch | None, tuple[Trace, ...]]]:
    root = Path(path).expanduser()
    files = (
        sorted(item for item in root.glob("verdict-agent-*.jsonl") if item.is_file())
        if root.is_dir()
        else [root]
    )
    if not files:
        raise ValueError("agent capture path contains no Verdict JSONL files")
    for capture_file in files:
        with capture_file.open("rb") as handle:
            while True:
                raw = handle.readline(MAX_RECORD_BYTES + 2)
                if not raw:
                    break
                if len(raw) > MAX_RECORD_BYTES + 1:
                    raise ValueError("agent capture record exceeds the local transport limit")
                if not raw.endswith(b"\n"):
                    if on_incomplete is not None:
                        on_incomplete()
                    break
                yield decode_capture_record(raw[:-1])


@dataclass(frozen=True)
class CaptureImportSummary:
    seen: int
    stored: int
    incomplete: int


def import_capture_records(path: str | Path, storage: Storage) -> CaptureImportSummary:
    """Replay local records through the canonical idempotent storage boundary."""
    seen = 0
    stored = 0
    incomplete = 0

    def mark_incomplete() -> None:
        nonlocal incomplete
        incomplete += 1

    for kind, batch, traces in iter_capture_records(path, on_incomplete=mark_incomplete):
        seen += 1
        if kind == "trace":
            storage.insert_trace(traces[0])
        else:
            assert batch is not None
            storage.append_agent_capture(batch, traces)
        stored += 1
    return CaptureImportSummary(seen=seen, stored=stored, incomplete=incomplete)
