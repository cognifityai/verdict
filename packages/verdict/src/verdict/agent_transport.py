"""Bounded local transport for SDK capture records."""

from __future__ import annotations

import json
import math
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
from verdict.redaction import RedactionMode, sanitize_span, sanitize_trace
from verdict.schema import (
    Operation,
    SpanRecord,
    Trace,
    UserSignalRecord,
    populate_trace_analysis_fields,
)
from verdict.storage.base import Storage

CAPTURE_SCHEMA = "verdict-capture-v1"
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
_SPAN_TRANSPORT_FIELDS = frozenset(
    {
        "span_id",
        "name",
        "trace_id",
        "parent_name",
        "started_at",
        "ended_at",
        "duration_ms",
        "attributes",
        "error",
    }
)
_SIGNAL_TRANSPORT_FIELDS = frozenset({"signal_id", "trace_id", "kind", "created_at"})
CaptureKind = Literal["trace", "agent", "span", "signal"]


def _require_transport_text(
    value: object,
    *,
    field: str,
    maximum: int,
    optional: bool = False,
    allow_empty: bool = False,
) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or (not value and not allow_empty) or "\x00" in value:
        raise ValueError(f"capture {field} must be bounded text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError(f"capture {field} must be bounded text") from exc
    if size > maximum:
        raise ValueError(f"capture {field} must be bounded text")


class CaptureQuotaExceeded(RuntimeError):
    """The local capture directory reached its configured hard bound."""


class CaptureSink(Protocol):
    def capture_trace(self, trace: Trace) -> None: ...

    def capture_agent(
        self,
        batch: AgentCaptureBatch,
        traces: tuple[Trace, ...] = (),
    ) -> None: ...

    def capture_span(self, span: SpanRecord) -> SpanRecord: ...

    def capture_user_signal(self, signal: UserSignalRecord) -> None: ...

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

    def capture_span(self, span: SpanRecord) -> SpanRecord:
        prepared = deepcopy(span)
        if prepared.trace_id is not None:
            try:
                trace_exists = getattr(self.storage, "trace_exists", None)
                if callable(trace_exists):
                    link_exists = bool(trace_exists(prepared.trace_id))
                else:
                    link_exists = self.storage.get_trace(prepared.trace_id) is not None
            except Exception:
                link_exists = False
                prepared.attributes.setdefault("verdict.link_status", "trace_lookup_failed")
            if not link_exists:
                prepared.trace_id = None
                prepared.attributes.setdefault("verdict.link_status", "trace_not_found")
        self.storage.insert_span(prepared)
        return prepared

    def capture_user_signal(self, signal: UserSignalRecord) -> None:
        self.storage.insert_user_signal(signal)

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


def _span_to_payload(span: SpanRecord) -> dict[str, Any]:
    payload = asdict(span)
    payload["started_at"] = span.started_at.isoformat()
    payload["ended_at"] = span.ended_at.isoformat() if span.ended_at else None
    return payload


def _span_from_payload(payload: object) -> SpanRecord:
    if not isinstance(payload, dict) or set(payload) != _SPAN_TRANSPORT_FIELDS:
        raise ValueError("capture Span has an invalid shape")
    values = dict(payload)
    try:
        started_raw = values.pop("started_at")
        ended_raw = values.pop("ended_at")
        started_at = datetime.fromisoformat(started_raw)
        ended_at = datetime.fromisoformat(ended_raw) if ended_raw is not None else None
        span = SpanRecord(started_at=started_at, ended_at=ended_at, **values)
    except (TypeError, ValueError) as exc:
        raise ValueError("capture Span has invalid typed fields") from exc
    if span.started_at.tzinfo is None or (
        span.ended_at is not None and span.ended_at.tzinfo is None
    ):
        raise ValueError("capture Span timestamps must be timezone-aware")
    _require_transport_text(span.span_id, field="span_id", maximum=256)
    _require_transport_text(span.name, field="span name", maximum=4096, allow_empty=True)
    _require_transport_text(span.trace_id, field="span trace_id", maximum=256, optional=True)
    _require_transport_text(
        span.parent_name,
        field="span parent name",
        maximum=4096,
        optional=True,
        allow_empty=True,
    )
    _require_transport_text(
        span.error,
        field="span error",
        maximum=4096,
        optional=True,
        allow_empty=True,
    )
    if not isinstance(span.attributes, dict):
        raise ValueError("capture Span attributes must be an object")
    if span.duration_ms is not None and (
        isinstance(span.duration_ms, bool)
        or not isinstance(span.duration_ms, (int, float))
        or not math.isfinite(float(span.duration_ms))
        or span.duration_ms < 0
    ):
        raise ValueError("capture Span duration must be a non-negative number")
    return span


def _signal_to_payload(signal: UserSignalRecord) -> dict[str, Any]:
    payload = asdict(signal)
    payload["created_at"] = signal.created_at.isoformat()
    return payload


def _signal_from_payload(payload: object) -> UserSignalRecord:
    if not isinstance(payload, dict) or set(payload) != _SIGNAL_TRANSPORT_FIELDS:
        raise ValueError("capture user signal has an invalid shape")
    values = dict(payload)
    try:
        created_at = datetime.fromisoformat(values.pop("created_at"))
        signal = UserSignalRecord(created_at=created_at, **values)
    except (TypeError, ValueError) as exc:
        raise ValueError("capture user signal has invalid typed fields") from exc
    from verdict.signals import VALID_SIGNAL_KINDS

    for name in ("signal_id", "trace_id", "kind"):
        _require_transport_text(
            getattr(signal, name),
            field=f"user signal {name}",
            maximum=256,
        )
    if signal.kind not in VALID_SIGNAL_KINDS or signal.created_at.tzinfo is None:
        raise ValueError("capture user signal has invalid typed fields")
    return signal


@dataclass(frozen=True)
class CaptureRecord:
    kind: CaptureKind
    trace: Trace | None = None
    batch: AgentCaptureBatch | None = None
    traces: tuple[Trace, ...] = ()
    span: SpanRecord | None = None
    user_signal: UserSignalRecord | None = None


def _record_bytes(kind: CaptureKind, record: object) -> bytes:
    envelope: dict[str, Any] = {
        "schema": CAPTURE_SCHEMA,
        "kind": kind,
        "record": record,
    }
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError("agent capture record exceeds the local transport limit")
    return encoded + b"\n"


def decode_capture_record(raw: bytes) -> CaptureRecord:
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("agent capture record exceeds the local transport limit")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("agent capture record is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "kind", "record"}:
        raise ValueError("capture record has an invalid shape")
    kind = payload["kind"]
    record = payload["record"]
    if payload["schema"] != CAPTURE_SCHEMA or kind not in {
        "trace",
        "agent",
        "span",
        "signal",
    }:
        raise ValueError("capture record has an unsupported schema")
    if kind == "trace":
        return CaptureRecord(kind="trace", trace=_trace_from_payload(record))
    if kind == "span":
        return CaptureRecord(kind="span", span=_span_from_payload(record))
    if kind == "signal":
        return CaptureRecord(kind="signal", user_signal=_signal_from_payload(record))
    if not isinstance(record, dict) or set(record) != {"batch", "traces"}:
        raise ValueError("agent evidence capture record is invalid")
    traces_payload = record["traces"]
    if not isinstance(traces_payload, list) or len(traces_payload) > 1:
        raise ValueError("agent capture record has an invalid Trace list")
    traces = tuple(_trace_from_payload(item) for item in traces_payload)
    if not isinstance(record["batch"], dict):
        raise ValueError("agent evidence capture record is invalid")
    batch = agent_capture_batch_from_json(
        json.dumps(record["batch"], ensure_ascii=False, separators=(",", ":"))
    )
    prepare_agent_capture_batch(batch, traces)
    return CaptureRecord(kind="agent", batch=batch, traces=traces)


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
        self._path: Path | None = None
        self._size = 0
        self._directory_size = 0
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self._reset_process_file()

    def _reset_process_file(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None
        self._path = None
        self._pid = os.getpid()
        self._producer_id = uuid4().hex
        self._segment = 0
        self._directory_size = sum(
            path.stat().st_size
            for path in self.directory.glob("verdict-agent-*.jsonl")
            if path.is_file()
        )

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
            self._path = path
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
            if self._file is None:
                self._open_segment()
            if self._size and self._size + len(record) > self.segment_bytes:
                assert self._file is not None
                self._file.close()
                self._file = None
                self._path = None
                self._open_segment()
            assert self._file is not None
            written = 0
            view = memoryview(record)
            try:
                while written < len(record):
                    count = self._file.write(view[written:])
                    if (
                        not isinstance(count, int)
                        or isinstance(count, bool)
                        or count <= 0
                        or count > len(record) - written
                    ):
                        raise OSError("agent capture file did not accept the complete record")
                    written += count
            except BaseException:
                self._size += written
                self._directory_size += written
                failed_path = self._path
                try:
                    self._file.close()
                except BaseException:
                    pass
                self._file = None
                self._path = None
                if self._size == 0 and failed_path is not None:
                    try:
                        failed_path.unlink()
                    except FileNotFoundError:
                        pass
                raise
            self._size += written
            self._directory_size += written

    def capture_trace(self, trace: Trace) -> None:
        prepared = sanitize_trace(
            deepcopy(trace), mode=self.redaction_mode, secret=self.redaction_secret
        )
        populate_trace_analysis_fields(prepared)
        self._append(_record_bytes("trace", _trace_to_payload(prepared)))

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
        self._append(
            _record_bytes(
                "agent",
                {
                    "batch": json.loads(agent_capture_batch_to_json(prepared_batch)),
                    "traces": [_trace_to_payload(trace) for trace in prepared_traces],
                },
            )
        )

    def capture_span(self, span: SpanRecord) -> SpanRecord:
        prepared = sanitize_span(
            deepcopy(span),
            mode=self.redaction_mode,
            secret=self.redaction_secret,
        )
        self._append(_record_bytes("span", _span_to_payload(prepared)))
        return prepared

    def capture_user_signal(self, signal: UserSignalRecord) -> None:
        self._append(_record_bytes("signal", _signal_to_payload(signal)))

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
                self._path = None


def iter_capture_records(
    path: str | Path,
    *,
    on_incomplete: Callable[[], None] | None = None,
) -> Iterator[CaptureRecord]:
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

    sink = StorageCaptureSink(storage)
    for record in iter_capture_records(path, on_incomplete=mark_incomplete):
        seen += 1
        if record.kind == "trace":
            assert record.trace is not None
            sink.capture_trace(record.trace)
        elif record.kind == "agent":
            assert record.batch is not None
            sink.capture_agent(record.batch, record.traces)
        elif record.kind == "span":
            assert record.span is not None
            sink.capture_span(record.span)
        else:
            assert record.user_signal is not None
            sink.capture_user_signal(record.user_signal)
        stored += 1
    return CaptureImportSummary(seen=seen, stored=stored, incomplete=incomplete)
