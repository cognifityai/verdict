from __future__ import annotations

import gzip
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import verdict.telemetry.files as telemetry_files
from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from verdict.schema import Trace
from verdict.storage.memory import InMemoryStorage
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.cli import main as import_main
from verdict.telemetry.files import iter_telemetry_file
from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.receiver import OtlpHttpReceiver
from verdict.telemetry.runner import ImportRunError, import_into_storage

ROOT = Path(__file__).parents[3]
SAMPLES = ROOT / "examples" / "telemetry"
UTC = timezone.utc


@pytest.mark.parametrize(
    ("filename", "file_format", "stored"),
    [
        ("otlp-genai.json", "otlp", 1),
        ("openinference-otlp.json", "otlp", 1),
        ("langfuse-observations.json", "langfuse", 1),
        ("langsmith-runs.jsonl", "langsmith", 1),
        ("datadog-spans.json", "datadog", 1),
        ("phoenix-traces.json", "phoenix", 1),
        ("opik-spans.json", "opik", 1),
        ("mlflow-traces.json", "mlflow", 1),
        ("voice-conversation.json", "voice", 2),
    ],
)
def test_published_sample_file_imports_through_sqlite_and_is_idempotent(
    tmp_path: Path, filename: str, file_format: str, stored: int
) -> None:
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    context = ImportContext(adapter="file", source_scope=filename, tenant_id="tenant-a")

    first = import_into_storage(
        iter_telemetry_file(SAMPLES / filename, file_format=file_format, context=context),
        storage,
    )
    second = import_into_storage(
        iter_telemetry_file(SAMPLES / filename, file_format=file_format, context=context),
        storage,
    )

    assert first.stored == stored
    assert first.skipped == 0
    assert second.stored == stored
    rows = storage.list_traces(tenant_id="tenant-a", limit=100)
    assert len(rows) == stored
    assert all(row.input_tokens is not None for row in rows)
    assert all(row.output_tokens is not None for row in rows)
    assert all(row.latency_ms is not None for row in rows)
    assert all(row.prompt_redacted for row in rows)
    assert all(row.response_redacted for row in rows)
    assert {row.tags["verdict.source"] for row in rows} == {file_format}
    storage.close()


def test_all_samples_share_one_database_without_source_id_collisions(tmp_path: Path) -> None:
    storage = SQLiteStorage(str(tmp_path / "all.db"))
    specifications = [
        ("otlp-genai.json", "otlp"),
        ("openinference-otlp.json", "otlp"),
        ("langfuse-observations.json", "langfuse"),
        ("langsmith-runs.jsonl", "langsmith"),
        ("datadog-spans.json", "datadog"),
        ("phoenix-traces.json", "phoenix"),
        ("opik-spans.json", "opik"),
        ("mlflow-traces.json", "mlflow"),
        ("voice-conversation.json", "voice"),
    ]
    for filename, file_format in specifications:
        context = ImportContext(adapter="file", source_scope=filename, tenant_id="tenant-a")
        import_into_storage(
            iter_telemetry_file(SAMPLES / filename, file_format=file_format, context=context),
            storage,
        )

    rows = storage.list_traces(tenant_id="tenant-a", limit=100)

    assert len(rows) == 10
    assert len({row.trace_id for row in rows}) == 10
    assert {row.tags["verdict.source"] for row in rows} == {
        "otlp",
        "langfuse",
        "langsmith",
        "datadog",
        "phoenix",
        "opik",
        "mlflow",
        "voice",
    }
    assert sum(row.input_tokens or 0 for row in rows) == 167
    assert sum(row.output_tokens or 0 for row in rows) == 63
    storage.close()


def test_same_source_record_is_isolated_across_tenants(tmp_path: Path) -> None:
    storage = SQLiteStorage(str(tmp_path / "tenants.db"))
    source = SAMPLES / "langfuse-observations.json"
    for tenant_id in ("tenant-a", "tenant-b"):
        import_into_storage(
            iter_telemetry_file(
                source,
                file_format="langfuse",
                context=ImportContext(
                    adapter="file",
                    source_scope="shared-project",
                    tenant_id=tenant_id,
                ),
            ),
            storage,
        )

    tenant_a = storage.list_traces(tenant_id="tenant-a", limit=10)
    tenant_b = storage.list_traces(tenant_id="tenant-b", limit=10)
    assert len(tenant_a) == len(tenant_b) == 1
    assert tenant_a[0].trace_id != tenant_b[0].trace_id
    storage.close()


def test_file_cli_imports_through_public_entry_point(tmp_path: Path, capsys) -> None:
    database = tmp_path / "cli.db"

    result = import_main(
        [
            "file",
            str(SAMPLES / "langfuse-observations.json"),
            "--format",
            "langfuse",
            "--storage",
            f"sqlite:///{database}",
            "--tenant-id",
            "tenant-cli",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "seen": 1,
        "skip_reasons": {},
        "skipped": 0,
        "stored": 1,
    }
    storage = SQLiteStorage(str(database))
    assert len(storage.list_traces(tenant_id="tenant-cli", limit=10)) == 1
    storage.close()


def test_api_cli_validates_credentials_before_opening_storage(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    database = tmp_path / "must-not-exist.db"

    result = import_main(
        [
            "langfuse",
            "--from",
            "2026-08-01T00:00:00Z",
            "--to",
            "2026-08-02T00:00:00Z",
            "--storage",
            f"sqlite:///{database}",
        ]
    )

    assert result == 2
    assert not database.exists()
    assert "LANGFUSE_PUBLIC_KEY" in capsys.readouterr().err


def test_cli_storage_errors_never_echo_credentials(capsys) -> None:
    result = import_main(
        [
            "file",
            str(SAMPLES / "langfuse-observations.json"),
            "--format",
            "langfuse",
            "--storage",
            "unsupported://user:canary-storage-password@example.invalid/database",
        ]
    )

    error = capsys.readouterr().err
    assert result == 2
    assert "cannot open configured storage (ValueError)" in error
    assert "canary-storage-password" not in error
    assert "example.invalid" not in error


def test_unknown_metadata_and_pii_do_not_reach_sqlite(tmp_path: Path) -> None:
    source = tmp_path / "langfuse.jsonl"
    source.write_text(
        json.dumps(
            {
                "id": "pii-1",
                "traceId": "trace-pii",
                "type": "GENERATION",
                "startTime": "2026-08-01T12:00:00Z",
                "endTime": "2026-08-01T12:00:01Z",
                "providedModelName": "gpt-4o-mini",
                "input": "Email alice@example.com",
                "output": "Called 415-555-1212",
                "usageDetails": {"input": 3, "output": 4},
                "metadata": {"api_key": "canary-secret-not-for-storage"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    storage = SQLiteStorage(str(tmp_path / "pii.db"))
    context = ImportContext(adapter="file", source_scope="pii", tenant_id="tenant-a")

    import_into_storage(
        iter_telemetry_file(source, file_format="langfuse", context=context), storage
    )
    row = storage.list_traces(limit=1)[0]

    persisted = repr(row)
    assert "alice@example.com" not in persisted
    assert "415-555-1212" not in persisted
    assert "canary-secret-not-for-storage" not in persisted
    assert "<EMAIL>" in (row.prompt_redacted or "")
    assert "<PHONE>" in (row.response_redacted or "")
    storage.close()


def test_runner_reports_partial_progress_when_storage_fails() -> None:
    class FailAfterOne(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def insert_trace(self, trace: Trace) -> None:
            self.calls += 1
            if self.calls == 2:
                raise OSError("database unavailable after acceptance")
            super().insert_trace(trace)

    storage = FailAfterOne()
    results = [
        MappingResult.mapped(
            Trace(trace_id=f"trace-{index}", started_at=datetime(2026, 8, 1, tzinfo=UTC))
        )
        for index in range(2)
    ]

    with pytest.raises(ImportRunError) as raised:
        import_into_storage(results, storage)

    assert raised.value.stage == "storage"
    assert raised.value.summary.seen == 2
    assert raised.value.summary.stored == 1
    assert storage.trace_exists("trace-0")
    assert not storage.trace_exists("trace-1")
    assert "database unavailable" not in str(raised.value)


def test_runner_reports_partial_progress_when_source_fails() -> None:
    def results():
        yield MappingResult.skipped("non_llm_span")
        raise OSError("source response canary")

    with pytest.raises(ImportRunError) as raised:
        import_into_storage(results(), InMemoryStorage())

    assert raised.value.stage == "source"
    assert raised.value.summary.seen == 1
    assert raised.value.summary.skipped == 1
    assert "source response canary" not in str(raised.value)


def _post(url: str, body: bytes, *, content_type: str, encoding: str | None = None):
    headers = {"Content-Type": content_type}
    if encoding:
        headers["Content-Encoding"] = encoding
    request = Request(url, data=body, headers=headers, method="POST")
    return urlopen(request, timeout=5)


def test_otlp_json_receiver_uses_real_http_and_sqlite(tmp_path: Path) -> None:
    storage = SQLiteStorage(str(tmp_path / "receiver.db"))
    context = ImportContext(adapter="otlp", source_scope="local-test", tenant_id="tenant-a")
    receiver = OtlpHttpReceiver(storage=storage, context=context, port=0)
    receiver.start()
    thread = threading.Thread(target=receiver.run_started_server, daemon=True)
    thread.start()
    body = (SAMPLES / "otlp-genai.json").read_bytes()

    with _post(
        f"http://127.0.0.1:{receiver.bound_port}/v1/traces",
        gzip.compress(body),
        content_type="application/json",
        encoding="gzip",
    ) as response:
        response_body = json.loads(response.read())

    receiver.shutdown()
    thread.join(timeout=5)
    rows = storage.list_traces(limit=10)
    assert response.status == 200
    assert response_body == {}
    assert len(rows) == 1
    assert rows[0].input_tokens == 18
    assert rows[0].output_tokens == 6
    assert rows[0].latency_ms == pytest.approx(750)
    assert rows[0].session_id == "demo-session-1"
    storage.close()


def test_otlp_protobuf_receiver_uses_real_http_and_sqlite(tmp_path: Path) -> None:
    storage = SQLiteStorage(str(tmp_path / "receiver-protobuf.db"))
    receiver = OtlpHttpReceiver(
        storage=storage,
        context=ImportContext(adapter="otlp", source_scope="protobuf-test"),
        port=0,
    )
    receiver.start()
    thread = threading.Thread(target=receiver.run_started_server, daemon=True)
    thread.start()
    request_message = ExportTraceServiceRequest()
    ParseDict(json.loads((SAMPLES / "otlp-genai.json").read_text()), request_message)

    with _post(
        f"http://127.0.0.1:{receiver.bound_port}/v1/traces",
        request_message.SerializeToString(),
        content_type="application/x-protobuf",
    ) as response:
        response_message = ExportTraceServiceResponse()
        response_message.ParseFromString(response.read())

    receiver.shutdown()
    thread.join(timeout=5)
    rows = storage.list_traces(limit=10)
    assert response.status == 200
    assert not response_message.HasField("partial_success")
    assert len(rows) == 1
    assert rows[0].input_tokens == 18
    assert rows[0].latency_ms == pytest.approx(750)
    storage.close()


def test_concurrent_duplicate_otlp_requests_remain_idempotent(tmp_path: Path) -> None:
    storage = SQLiteStorage(str(tmp_path / "receiver-concurrent.db"))
    receiver = OtlpHttpReceiver(
        storage=storage,
        context=ImportContext(adapter="otlp", source_scope="concurrent-test"),
        port=0,
    )
    receiver.start()
    thread = threading.Thread(target=receiver.run_started_server, daemon=True)
    thread.start()
    body = (SAMPLES / "otlp-genai.json").read_bytes()
    url = f"http://127.0.0.1:{receiver.bound_port}/v1/traces"

    def post_once(_: int) -> int:
        with _post(url, body, content_type="application/json") as response:
            response.read()
            return response.status

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(post_once, range(16)))

    receiver.shutdown()
    thread.join(timeout=5)
    assert statuses == [200] * 16
    assert len(storage.list_traces(limit=10)) == 1
    storage.close()


def test_otlp_receiver_rejects_unknown_encoding_without_writing(tmp_path: Path) -> None:
    storage = SQLiteStorage(str(tmp_path / "receiver.db"))
    receiver = OtlpHttpReceiver(
        storage=storage,
        context=ImportContext(adapter="otlp", source_scope="local-test"),
        port=0,
    )
    receiver.start()
    thread = threading.Thread(target=receiver.run_started_server, daemon=True)
    thread.start()

    with pytest.raises(HTTPError) as raised:
        _post(
            f"http://127.0.0.1:{receiver.bound_port}/v1/traces",
            b"not-brotli",
            content_type="application/json",
            encoding="br",
        )

    receiver.shutdown()
    thread.join(timeout=5)
    assert raised.value.code == 415
    assert storage.list_traces(limit=10) == []
    storage.close()


@pytest.mark.parametrize(
    ("body", "encoding"),
    [
        (b"x" * 1_025, None),
        (gzip.compress(b"x" * 2_000), "gzip"),
    ],
)
def test_otlp_receiver_rejects_wire_and_decompressed_size_limits(
    tmp_path: Path, body: bytes, encoding: str | None
) -> None:
    storage = SQLiteStorage(str(tmp_path / "receiver-limit.db"))
    receiver = OtlpHttpReceiver(
        storage=storage,
        context=ImportContext(adapter="otlp", source_scope="local-test"),
        port=0,
        max_request_bytes=1_024,
    )
    receiver.start()
    thread = threading.Thread(target=receiver.run_started_server, daemon=True)
    thread.start()

    with pytest.raises(HTTPError) as raised:
        _post(
            f"http://127.0.0.1:{receiver.bound_port}/v1/traces",
            body,
            content_type="application/json",
            encoding=encoding,
        )

    receiver.shutdown()
    thread.join(timeout=5)
    assert raised.value.code in {400, 413}
    assert storage.list_traces(limit=10) == []
    storage.close()


def test_unknown_and_malformed_files_fail_visibly(tmp_path: Path) -> None:
    unknown = tmp_path / "unknown.json"
    unknown.write_text('{"event":"not telemetry"}\n', encoding="utf-8")
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('{"broken":\n', encoding="utf-8")
    storage = InMemoryStorage()

    summary = import_into_storage(
        iter_telemetry_file(
            unknown,
            file_format="auto",
            context=ImportContext(adapter="file", source_scope="unknown"),
        ),
        storage,
    )

    assert summary.seen == summary.skipped == 1
    assert summary.skip_reasons == {"unsupported_record_format": 1}
    with pytest.raises(ValueError, match="invalid NDJSON at line 1"):
        list(
            iter_telemetry_file(
                malformed,
                file_format="auto",
                context=ImportContext(adapter="file", source_scope="malformed"),
            )
        )


def test_file_readers_enforce_limits_during_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = ImportContext(adapter="file", source_scope="bounded-files")
    monkeypatch.setattr(telemetry_files, "_MAX_NDJSON_LINE_BYTES", 1_024)
    monkeypatch.setattr(telemetry_files, "_MAX_JSON_BYTES", 1_024)
    record = {
        "id": "bounded-observation",
        "traceId": "bounded-trace",
        "type": "GENERATION",
        "startTime": "2026-08-01T12:00:00Z",
        "providedModelName": "gpt-4o-mini",
        "input": "question",
        "output": "answer",
    }
    encoded = json.dumps(record, separators=(",", ":")).encode()
    exact_line = tmp_path / "exact.jsonl"
    exact_line.write_bytes(encoded + b" " * (1_023 - len(encoded)) + b"\n")
    oversized_line = tmp_path / "oversized.jsonl"
    oversized_line.write_bytes(b"x" * 1_025)
    oversized_json = tmp_path / "oversized.json"
    oversized_json.write_bytes(b"{" + b" " * 1_024 + b"}")

    mapped = list(
        iter_telemetry_file(exact_line, file_format="langfuse", context=context)
    )

    assert len(mapped) == 1
    assert mapped[0].trace is not None
    with pytest.raises(ValueError, match="NDJSON line 1 exceeds"):
        list(iter_telemetry_file(oversized_line, file_format="auto", context=context))
    with pytest.raises(ValueError, match="JSON file exceeds"):
        list(iter_telemetry_file(oversized_json, file_format="auto", context=context))


def test_otlp_receiver_refuses_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        OtlpHttpReceiver(
            storage=InMemoryStorage(),
            context=ImportContext(adapter="otlp", source_scope="unsafe"),
            host="0.0.0.0",
        )
