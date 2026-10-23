from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from verdict import collector as collector_module
from verdict.agent_transport import (
    FileCaptureSink,
    StorageCaptureSink,
    _span_from_payload,
    _span_to_payload,
)
from verdict.collector import (
    MAX_BATCH_BYTES,
    MAX_BATCH_RECORDS,
    BatchBoundaryError,
    CollectorService,
    PostgresReceiptStore,
    ReceiptConflict,
    ReceiptCorrupt,
    create_collector_app,
)
from verdict.collector import (
    main as collector_main,
)
from verdict.collector_receipts import (
    CollectorReceipt,
    _validate_acknowledgement,
    _validate_receipt,
)
from verdict.evidence import (
    AgentCaptureBatch,
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentTurn,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
)
from verdict.schema import SpanRecord, Trace
from verdict.shipper import SegmentShipper, ShippingError, read_shipping_status
from verdict.storage.buffered import BufferedStorage
from verdict.storage.memory import InMemoryStorage
from verdict.storage.sqlite import SQLiteStorage

TENANT = "collector-tenant"
SECRET = "collector-test-secret-32-characters"


def test_span_capture_accepts_published_payload_without_tenant_identity():
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    span = SpanRecord(span_id="legacy-span", name="step", trace_id="trace", started_at=now)
    published = _span_to_payload(span)
    published.pop("tenant_id")
    assert _span_from_payload(published).tenant_id is None
    assert _span_from_payload(_span_to_payload(SpanRecord(
        span_id="owned-span", started_at=now, tenant_id="tenant-a"))).tenant_id == "tenant-a"


def test_collector_accepts_published_span_wire_without_tenant_identity():
    service, storage, _receipts = _service()
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    legacy = _span_to_payload(SpanRecord(
        span_id="published-span", name="synthetic", started_at=now,
    ))
    legacy.pop("tenant_id")
    body = json.dumps({
        "schema": "verdict-capture-v1", "kind": "span", "record": legacy,
    }).encode() + b"\n"

    response = _decode(service.ingest("published-span-batch", "host-1", body))

    assert response["accepted"] == 1
    assert storage.list_spans(tenant_id=TENANT, span_id="published-span")[0].tenant_id == TENANT


def test_span_capture_retracts_foreign_tenant_trace_link():
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    storage = InMemoryStorage()
    storage.insert_trace(Trace(trace_id="trace-a", tenant_id="tenant-a", started_at=now))
    sink = StorageCaptureSink(storage)
    forged = sink.capture_span(SpanRecord(span_id="forged", trace_id="trace-a",
                                          tenant_id="tenant-b", started_at=now))
    assert forged.trace_id is None
    assert forged.attributes["verdict.link_status"] == "tenant_mismatch"


class _MemoryReceiptStore:
    def __init__(self) -> None:
        self.receipts: dict[tuple[str, str], CollectorReceipt] = {}

    def process(self, tenant_id, batch_id, producer_id, payload_sha256, processor):
        key = (tenant_id, batch_id)
        now = datetime.now(timezone.utc)
        receipt = self.receipts.get(key)
        if receipt is None:
            receipt = CollectorReceipt(
                tenant_id=tenant_id,
                batch_id=batch_id,
                producer_id=producer_id,
                payload_sha256=payload_sha256,
                state="pending",
                first_received_at=now,
                last_attempt_at=now,
                attempt_count=0,
            )
            self.receipts[key] = receipt
        if receipt.producer_id != producer_id or receipt.payload_sha256 != payload_sha256:
            raise ReceiptConflict("collector batch identity conflict")
        if receipt.state == "completed":
            assert receipt.response_body is not None
            return receipt.response_body
        self.receipts[key] = replace(
            receipt, last_attempt_at=now, attempt_count=receipt.attempt_count + 1
        )
        response = processor()
        _validate_acknowledgement(response, batch_id)
        completed_at = datetime.now(timezone.utc)
        self.receipts[key] = replace(
            self.receipts[key],
            state="completed",
            completed_at=completed_at,
            last_attempt_at=completed_at,
            response_body=response,
        )
        return response

    def get(self, tenant_id, batch_id):
        receipt = self.receipts.get((tenant_id, batch_id))
        if receipt is not None:
            _validate_receipt(receipt)
        return receipt

    def ready(self) -> bool:
        return True

    def close(self) -> None:
        return None


def _agent_record(
    tmp_path: Path,
    *,
    trace_id: str = "trace-1",
    status: ExecutionStatus = ExecutionStatus.UNKNOWN,
) -> bytes:
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    ended_at = now if status is not ExecutionStatus.UNKNOWN else None
    turn = AgentTurn(
        turn_id=f"turn-{trace_id}",
        run_id=f"run-{trace_id}",
        sequence=0,
        started_at=now,
        status=status,
        ended_at=ended_at,
        input_tokens=17,
        cached_input_tokens=11,
        output_tokens=5,
        total_tokens=22,
        token_usage_basis="codex_turn_delta",
    )
    event = AgentEvent(
        event_id=f"event-{trace_id}",
        turn_id=turn.turn_id,
        sequence=0,
        occurred_at=now,
        event_type=AgentEventType.MODEL_CALL,
        status=status,
        provenance="sdk:test",
        attributes={"provider": "openai", "request_model": "test-model"},
        privacy_classification=PrivacyClassification.METADATA,
        trace_id=trace_id,
    )
    batch = AgentCaptureBatch(
        session=SourceSession(
            source_session_id=f"source-{trace_id}",
            tenant_id="untrusted-payload-tenant",
            source_kind="verdict_sdk",
            source_locator_hash=sha256(f"locator-{trace_id}".encode()).hexdigest(),
            started_at=now,
            observed_at=now,
        ),
        run=AgentRun(
            run_id=turn.run_id,
            source_session_id=f"source-{trace_id}",
            tenant_id="untrusted-payload-tenant",
            started_at=now,
            status=status,
            ended_at=ended_at,
            agent_name="test-agent",
        ),
        turns=(turn,),
        events=(event,),
    )
    trace = Trace(
        trace_id=trace_id,
        tenant_id="untrusted-payload-tenant",
        provider="openai",
        request_model="test-model",
        response_model="test-model" if ended_at else "",
        started_at=now,
        ended_at=ended_at,
        prompt_redacted="question",
        response_redacted="answer" if ended_at else None,
    )
    spool = tmp_path / f"spool-{trace_id}-{uuid4().hex}"
    sink = FileCaptureSink(spool, redaction_mode="redact", redaction_secret=None)
    sink.capture_agent(batch, (trace,))
    sink.close()
    [path] = spool.glob("verdict-agent-*.jsonl")
    return path.read_bytes()


def _standalone_records(tmp_path: Path) -> bytes:
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    spool = tmp_path / "trace-spool"
    sink = FileCaptureSink(spool, redaction_mode="redact", redaction_secret=None)
    sink.capture_trace(Trace(trace_id="standalone", provider="openai", started_at=now))
    sink.capture_span(SpanRecord(span_id="span-1", name="work", started_at=now))
    sink.close()
    [path] = spool.glob("verdict-agent-*.jsonl")
    return path.read_bytes()


def _service(storage: InMemoryStorage | None = None):
    evidence = storage or InMemoryStorage()
    receipts = _MemoryReceiptStore()
    return CollectorService(evidence, receipts, tenant_id=TENANT), evidence, receipts


def _decode(response: bytes) -> dict[str, object]:
    return json.loads(response)


def test_agent_batch_uses_configured_tenant_and_replays_exactly(tmp_path: Path) -> None:
    service, storage, _receipts = _service()
    body = _agent_record(tmp_path)

    first = service.ingest("batch-1", "host-1", body)
    second = service.ingest("batch-1", "host-1", body)

    assert second == first
    assert _decode(first)["accepted"] == 1
    [bundle] = storage.list_agent_run_bundles(TENANT)
    assert bundle.session.tenant_id == TENANT
    assert bundle.run.tenant_id == TENANT
    assert bundle.turns[0].input_tokens == 17
    assert bundle.turns[0].cached_input_tokens == 11
    assert bundle.turns[0].output_tokens == 5
    assert bundle.turns[0].total_tokens == 22
    assert bundle.turns[0].token_usage_basis == "codex_turn_delta"
    [trace] = storage.list_traces(tenant_id=TENANT)
    assert trace.tenant_id == TENANT
    assert bundle.events[0].trace_id == trace.trace_id


def test_collector_reapplies_field_redaction_to_untrusted_agent_records(tmp_path: Path) -> None:
    service, storage, _receipts = _service()
    payload = json.loads(_agent_record(tmp_path))
    [model_event] = payload["record"]["batch"]["events"]
    canary = "opaque-collector-canary"
    payload["record"]["batch"]["events"].append(
        {
            **model_event,
            "event_id": "event-tool-result",
            "sequence": 1,
            "event_type": "tool_result",
            "attributes": {
                "tool_name": "lookup",
                "call_id": "call-1",
                "result": {
                    "password": canary,
                    "api_keys": [canary],
                    "passwords": {"primary": canary},
                    "AWSSECRETACCESSKEYS": [canary],
                    "XAPIKeys": [canary],
                    "input_tokens": 12345678,
                },
                "is_error": False,
            },
            "privacy_classification": "redacted",
            "trace_id": None,
        }
    )
    payload["record"]["batch"]["events"].append(
        {
            **model_event,
            "event_id": "event-context",
            "sequence": 2,
            "event_type": "context",
            "attributes": {
                "name": "OIDCIDTokens",
                "value": canary,
                "source": "remote",
            },
            "privacy_classification": "redacted",
            "trace_id": None,
        }
    )
    body = json.dumps(payload, separators=(",", ":")).encode() + b"\n"

    response = _decode(service.ingest("batch-secret", "host-1", body))

    assert response["accepted"] == 1
    [bundle] = storage.list_agent_run_bundles(TENANT)
    assert canary not in repr(bundle)
    assert bundle.events[1].attributes["result"]["password"] == "<SECRET>"
    assert bundle.events[1].attributes["result"]["api_keys"] == "<SECRET>"
    assert bundle.events[1].attributes["result"]["passwords"] == "<SECRET>"
    assert bundle.events[1].attributes["result"]["AWSSECRETACCESSKEYS"] == "<SECRET>"
    assert bundle.events[1].attributes["result"]["XAPIKeys"] == "<SECRET>"
    assert bundle.events[1].attributes["result"]["input_tokens"] == 12345678
    assert bundle.events[2].attributes["value"] == "<SECRET>"


def test_batch_identity_binds_body_and_producer(tmp_path: Path) -> None:
    service, storage, _receipts = _service()
    body = _agent_record(tmp_path)
    service.ingest("batch-1", "host-1", body)

    with pytest.raises(ReceiptConflict):
        service.ingest("batch-1", "host-2", body)
    with pytest.raises(ReceiptConflict):
        service.ingest("batch-1", "host-1", body + b"\n")
    assert len(storage.list_agent_run_bundles(TENANT)) == 1


def test_poison_records_are_terminal_without_blocking_valid_siblings(
    tmp_path: Path,
) -> None:
    service, storage, _receipts = _service()
    valid = _agent_record(tmp_path)
    retired_signal = json.dumps(
        {
            "schema": "verdict-capture-v1",
            "kind": "signal",
            "record": {"obsolete": True},
        }
    ).encode() + b"\n"
    body = (
        b'{"schema":"wrong"}\n'
        + valid
        + retired_signal
        + _standalone_records(tmp_path)
    )

    response = _decode(service.ingest("batch-mixed", "host-1", body))

    assert response["accepted"] == 3
    assert response["rejected"] == 2
    assert [item["error"] for item in response["results"]] == [
        "invalid_record",
        None,
        "unsupported_record_kind",
        None,
        None,
    ]
    assert len(storage.list_agent_run_bundles(TENANT)) == 1
    assert storage.get_trace("standalone").tenant_id == TENANT
    assert storage.list_spans(tenant_id=TENANT, span_id="span-1")[0].tenant_id == TENANT


def test_standalone_span_cannot_replace_a_foreign_tenant_span(tmp_path: Path) -> None:
    service, storage, _receipts = _service()
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    storage.insert_span(SpanRecord(
        span_id="span-1", name="foreign", tenant_id="tenant-b", started_at=now,
    ))

    response = _decode(service.ingest("batch-foreign-span", "host-1", _standalone_records(tmp_path)))

    assert response["accepted"] == 1
    assert response["rejected"] == 1
    assert response["results"][1]["error"] == "evidence_conflict"
    assert storage.list_spans(span_id="span-1")[0].name == "foreign"
    assert storage.list_spans(span_id="span-1")[0].tenant_id == "tenant-b"


def test_standalone_trace_cannot_replace_a_foreign_tenant_trace(tmp_path: Path) -> None:
    service, storage, _receipts = _service()
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    storage.insert_trace(Trace(
        trace_id="standalone", tenant_id="tenant-b", provider="foreign",
        started_at=now,
    ))

    response = _decode(service.ingest("batch-foreign-trace", "host-1", _standalone_records(tmp_path)))

    assert response["accepted"] == 1
    assert response["rejected"] == 1
    assert response["results"][0]["error"] == "evidence_conflict"
    assert storage.get_trace("standalone").provider == "foreign"
    assert storage.get_trace("standalone").tenant_id == "tenant-b"


def test_standalone_records_scope_untrusted_owners_and_keep_receipt_on_retry(tmp_path: Path) -> None:
    service, storage, _receipts = _service()
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    spool = tmp_path / "owned-standalone"
    sink = FileCaptureSink(spool, redaction_mode="redact", redaction_secret=None)
    sink.capture_trace(Trace(
        trace_id="owned-trace", tenant_id="forged-tenant", provider="synthetic",
        started_at=now,
    ))
    sink.capture_span(SpanRecord(
        span_id="owned-span", trace_id="owned-trace", tenant_id="forged-tenant",
        name="synthetic", started_at=now,
    ))
    sink.close()
    [segment] = spool.glob("verdict-agent-*.jsonl")
    body = segment.read_bytes()

    first = service.ingest("batch-standalone", "host-1", body)
    assert service.ingest("batch-standalone", "host-1", body) == first
    assert _decode(first)["accepted"] == 2
    assert storage.get_trace("owned-trace").tenant_id == TENANT
    [span] = storage.list_spans(tenant_id=TENANT, span_id="owned-span")
    assert span.trace_id == "owned-trace"
    assert span.tenant_id == TENANT
    assert storage.list_spans(tenant_id="forged-tenant", span_id="owned-span") == []


def test_standalone_span_retracts_foreign_trace_link(tmp_path: Path) -> None:
    service, storage, _receipts = _service()
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    storage.insert_trace(Trace(
        trace_id="foreign-trace", tenant_id="foreign", started_at=now,
    ))
    spool = tmp_path / "foreign-link"
    sink = FileCaptureSink(spool, redaction_mode="redact", redaction_secret=None)
    sink.capture_span(SpanRecord(
        span_id="unlinked-span", trace_id="foreign-trace", started_at=now,
    ))
    sink.close()
    [segment] = spool.glob("verdict-agent-*.jsonl")

    ack = _decode(service.ingest("batch-foreign-link", "host-1", segment.read_bytes()))

    assert ack["accepted"] == 1
    [span] = storage.list_spans(tenant_id=TENANT, span_id="unlinked-span")
    assert span.trace_id is None
    assert span.attributes["verdict.link_status"] == "tenant_mismatch"


@pytest.mark.parametrize("lookup_error", [OSError, ValueError])
def test_transient_explicit_trace_lookup_keeps_receipt_and_segment_retryable(
    tmp_path: Path, lookup_error: type[Exception],
) -> None:
    class FailingLookupOnce(InMemoryStorage):
        fail_lookup = True

        def trace_owner(self, trace_id: str):
            if self.fail_lookup:
                self.fail_lookup = False
                raise lookup_error("synthetic transient trace lookup failure")
            return super().trace_owner(trace_id)

    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    storage = FailingLookupOnce()
    storage.insert_trace(Trace(trace_id="linked-trace", tenant_id=TENANT, started_at=now))
    service, _storage, receipts = _service(storage)
    spool = tmp_path / "retry-spool"
    producer = FileCaptureSink(spool, redaction_mode="redact", redaction_secret=None)
    producer.capture_span(SpanRecord(
        span_id="linked-span", trace_id="linked-trace", name="work", started_at=now,
    ))
    producer.close()
    [segment] = spool.glob("verdict-agent-*.jsonl")
    app = create_collector_app(service, api_key=SECRET)

    with TestClient(app) as client:
        class LocalSender:
            destination_fingerprint = "a" * 64

            def __init__(self) -> None:
                self.batch_id: str | None = None
                self.statuses: list[int] = []

            def send(self, *, batch_id: str, producer_id: str, body: bytes) -> bytes:
                self.batch_id = batch_id
                response = client.post("/v1/ingest", content=body, headers={
                    "Authorization": f"Bearer {SECRET}",
                    "Content-Type": "application/x-ndjson",
                    "X-Verdict-Batch-ID": batch_id,
                    "X-Verdict-Producer-ID": producer_id,
                })
                self.statuses.append(response.status_code)
                if response.status_code != 200:
                    raise ShippingError(
                        f"collector_http_{response.status_code}",
                        retryable=response.status_code >= 500,
                    )
                return response.content

        sender = LocalSender()
        shipper = SegmentShipper(spool, sender, max_attempts=1)
        with pytest.raises(ShippingError, match="collector_http_503"):
            shipper.ship_once()
        assert sender.statuses == [503]
        assert segment.exists()
        assert storage.list_spans(tenant_id=TENANT, span_id="linked-span") == []
        assert receipts.get(TENANT, sender.batch_id).state == "pending"  # type: ignore[arg-type,union-attr]
        assert read_shipping_status(spool).pending_bytes > 0

        delivered = shipper.ship_once()

    assert sender.statuses == [503, 200]
    assert delivered.accepted_records == 1
    assert delivered.deleted_segments == 1
    assert not segment.exists()
    [span] = storage.list_spans(tenant_id=TENANT, span_id="linked-span")
    assert span.trace_id == "linked-trace"
    assert receipts.get(TENANT, sender.batch_id).state == "completed"  # type: ignore[arg-type,union-attr]


def test_mixed_span_batch_retries_prior_record_without_losing_explicit_link(
    tmp_path: Path,
) -> None:
    class FailingLookupOnce(InMemoryStorage):
        fail_lookup = True

        def trace_owner(self, trace_id: str):
            if self.fail_lookup:
                self.fail_lookup = False
                raise OSError("synthetic lookup outage")
            return super().trace_owner(trace_id)

    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    storage = FailingLookupOnce()
    storage.insert_trace(Trace(trace_id="existing", tenant_id=TENANT, started_at=now))
    service, _storage, receipts = _service(storage)
    producer = FileCaptureSink(
        tmp_path / "mixed-spool", redaction_mode="redact", redaction_secret=None,
    )
    producer.capture_span(SpanRecord(span_id="independent", name="first", started_at=now))
    producer.capture_span(SpanRecord(
        span_id="dependent", trace_id="existing", name="second", started_at=now,
    ))
    producer.close()
    [segment] = (tmp_path / "mixed-spool").glob("verdict-agent-*.jsonl")
    body = segment.read_bytes()

    with pytest.raises(RuntimeError, match="trace lookup unavailable"):
        service.ingest("batch-mixed-link", "host-1", body)
    assert receipts.get(TENANT, "batch-mixed-link").state == "pending"  # type: ignore[union-attr]
    assert len(storage.list_spans(tenant_id=TENANT, span_id="independent")) == 1
    assert storage.list_spans(tenant_id=TENANT, span_id="dependent") == []

    acknowledgement = _decode(service.ingest("batch-mixed-link", "host-1", body))
    assert acknowledgement["accepted"] == 2
    assert len(storage.list_spans(tenant_id=TENANT, span_id="independent")) == 1
    [linked] = storage.list_spans(tenant_id=TENANT, span_id="dependent")
    assert linked.trace_id == "existing"


def test_missing_explicit_trace_is_confirmed_unlinked_without_lookup_error() -> None:
    storage = InMemoryStorage()
    sink = StorageCaptureSink(storage)
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    unlinked = sink.capture_span(SpanRecord(
        span_id="missing-link", trace_id="absent", tenant_id=TENANT,
        started_at=now,
    ))
    independent = sink.capture_span(SpanRecord(
        span_id="independent", tenant_id=TENANT, started_at=now,
    ))
    assert unlinked.trace_id is None
    assert unlinked.attributes["verdict.link_status"] == "trace_not_found"
    assert independent.trace_id is None
    assert "verdict.link_status" not in independent.attributes


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_builtin_span_link_lookup_does_not_hydrate_trace_content(
    tmp_path: Path, adapter: str,
) -> None:
    class NoHydrationMemory(InMemoryStorage):
        def get_trace(self, trace_id: str):
            raise AssertionError("full Trace hydration is forbidden for link checks")

    class NoHydrationSQLite(SQLiteStorage):
        def get_trace(self, trace_id: str):
            raise AssertionError("full Trace hydration is forbidden for link checks")

    storage = (
        NoHydrationMemory() if adapter == "memory"
        else NoHydrationSQLite(str(tmp_path / "narrow-link.db"))
    )
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    storage.insert_trace(Trace(
        trace_id="large-linked-trace", tenant_id=TENANT, started_at=now,
        prompt_redacted="synthetic large prompt " * 10_000,
    ))
    selected_queries: list[str] = []
    if adapter == "sqlite":
        storage._conn.set_trace_callback(selected_queries.append)
    try:
        recorded = StorageCaptureSink(storage).capture_span(SpanRecord(
            span_id="narrow-span", trace_id="large-linked-trace",
            tenant_id=TENANT, started_at=now,
        ))
        assert recorded.trace_id == "large-linked-trace"
        assert storage.list_spans(tenant_id=TENANT, span_id="narrow-span")[0].trace_id == recorded.trace_id
        if adapter == "sqlite":
            assert any("SELECT tenant_id FROM traces WHERE trace_id" in sql for sql in selected_queries)
            assert not any("SELECT * FROM traces" in sql for sql in selected_queries)
    finally:
        storage.close()


def test_span_link_lookup_keeps_published_custom_storage_fallback() -> None:
    class PublishedStorageShape:
        def __init__(self) -> None:
            self.inner = InMemoryStorage()
            self.lookups = 0

        def get_trace(self, trace_id: str):
            self.lookups += 1
            return self.inner.get_trace(trace_id)

        def insert_span(self, span: SpanRecord) -> None:
            self.inner.insert_span(span)

    storage = PublishedStorageShape()
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    storage.inner.insert_trace(Trace(trace_id="custom-link", tenant_id=TENANT, started_at=now))
    recorded = StorageCaptureSink(storage).capture_span(SpanRecord(  # type: ignore[arg-type]
        span_id="custom-span", trace_id="custom-link", tenant_id=TENANT, started_at=now,
    ))
    assert recorded.trace_id == "custom-link"
    assert storage.lookups == 1
    assert storage.inner.list_spans(tenant_id=TENANT, span_id="custom-span")[0].trace_id == "custom-link"


def test_buffered_span_link_lookup_flushes_queued_trace_before_validation() -> None:
    inner = InMemoryStorage()
    buffered = BufferedStorage(inner, flush_interval=10.0, batch_size=100)
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    try:
        buffered.insert_trace(Trace(trace_id="queued-trace", tenant_id=TENANT, started_at=now))
        recorded = StorageCaptureSink(buffered).capture_span(SpanRecord(
            span_id="queued-span", trace_id="queued-trace", tenant_id=TENANT,
            started_at=now,
        ))
        buffered.flush()
        assert recorded.trace_id == "queued-trace"
        assert inner.list_spans(tenant_id=TENANT, span_id="queued-span")[0].trace_id == "queued-trace"
    finally:
        buffered.close()


def test_standalone_wire_content_is_redacted_again_at_collector(tmp_path: Path) -> None:
    service, storage, _receipts = _service()
    canary = "standalone-collector-secret-canary"
    records = [json.loads(line) for line in _standalone_records(tmp_path).splitlines()]
    records[0]["record"]["tags"] = {"nested": {"password": canary}}
    records[1]["record"]["attributes"] = {"nested": {"password": canary}}
    body = b"".join(json.dumps(item).encode() + b"\n" for item in records)

    response = _decode(service.ingest("batch-standalone-canary", "host-1", body))

    assert response["accepted"] == 2
    assert canary not in json.dumps(storage.get_trace("standalone").tags)
    [span] = storage.list_spans(tenant_id=TENANT, span_id="span-1")
    assert canary not in json.dumps(span.attributes)


def test_deep_json_is_a_terminal_invalid_record_without_blocking_its_sibling(
    tmp_path: Path,
) -> None:
    service, storage, receipts = _service()
    nested = b"[" * 10_000 + b"]" * 10_000

    response = _decode(
        service.ingest("batch-deep-json", "host-1", nested + b"\n" + _agent_record(tmp_path))
    )

    assert response["accepted"] == 1
    assert response["rejected"] == 1
    assert [item["error"] for item in response["results"]] == ["invalid_record", None]
    assert receipts.get(TENANT, "batch-deep-json").state == "completed"  # type: ignore[union-attr]
    assert len(storage.list_agent_run_bundles(TENANT)) == 1


def test_receipt_store_rejects_malformed_acknowledgements() -> None:
    valid_result = {
        "index": 0,
        "recordDigest": "a" * 64,
        "status": "accepted",
        "error": None,
    }
    base = {
        "schema": "verdict-ingest-ack-v1",
        "batchId": "batch-ack",
        "accepted": 1,
        "rejected": 0,
        "results": [valid_result],
    }
    corrupt = [b"not-json"]
    for field, value in (
        ("schema", "wrong"),
        ("batchId", "other-batch"),
        ("accepted", 2),
        ("results", [{**valid_result, "index": 1}]),
        ("results", [{**valid_result, "recordDigest": "bad"}]),
        ("results", [{**valid_result, "status": "unknown"}]),
    ):
        corrupt.append(
            json.dumps(
                {**base, field: value},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )

    for response in corrupt:
        receipts = _MemoryReceiptStore()
        with pytest.raises(ReceiptCorrupt):
            receipts.process(
                TENANT,
                "batch-ack",
                "host-1",
                "a" * 64,
                lambda response=response: response,
            )
        assert receipts.get(TENANT, "batch-ack").state == "pending"  # type: ignore[union-attr]


def test_batch_line_and_size_rules_are_bounded_before_receipt(tmp_path: Path) -> None:
    service, _storage, receipts = _service()
    record = _agent_record(tmp_path).rstrip(b"\n")

    mixed = _decode(service.ingest("batch-empty-line", "host-1", record + b"\n\n" + record))
    assert [item["status"] for item in mixed["results"]] == [
        "accepted",
        "rejected",
        "accepted",
    ]
    crlf = _decode(service.ingest("batch-crlf", "host-1", record + b"\r\n"))
    assert crlf["accepted"] == 1

    with pytest.raises(BatchBoundaryError, match="too_many_records"):
        service.ingest("batch-lines", "host-1", b"{}\n" * (MAX_BATCH_RECORDS + 1))
    with pytest.raises(BatchBoundaryError, match="batch_too_large"):
        service.ingest("batch-bytes", "host-1", b"x" * (MAX_BATCH_BYTES + 1))
    assert receipts.get(TENANT, "batch-lines") is None
    assert receipts.get(TENANT, "batch-bytes") is None


def test_unexpected_storage_failure_leaves_receipt_retryable(tmp_path: Path) -> None:
    class FailingOnceStorage(InMemoryStorage):
        fail = True

        def append_agent_capture(self, batch, traces=()):
            if self.fail:
                self.fail = False
                raise RuntimeError("temporary database failure")
            return super().append_agent_capture(batch, traces)

    storage = FailingOnceStorage()
    service, _storage, receipts = _service(storage)
    body = _agent_record(tmp_path)

    with pytest.raises(RuntimeError, match="temporary database failure"):
        service.ingest("batch-retry", "host-1", body)
    assert receipts.get(TENANT, "batch-retry").state == "pending"  # type: ignore[union-attr]

    result = _decode(service.ingest("batch-retry", "host-1", body))
    assert result["accepted"] == 1
    assert receipts.get(TENANT, "batch-retry").state == "completed"  # type: ignore[union-attr]


def test_unexpected_storage_recursion_leaves_receipt_retryable(tmp_path: Path) -> None:
    class RecursingOnceStorage(InMemoryStorage):
        fail = True

        def append_agent_capture(self, batch, traces=()):
            if self.fail:
                self.fail = False
                raise RecursionError("internal storage defect")
            return super().append_agent_capture(batch, traces)

    storage = RecursingOnceStorage()
    service, _storage, receipts = _service(storage)
    body = _agent_record(tmp_path)

    with pytest.raises(RecursionError, match="internal storage defect"):
        service.ingest("batch-recursion", "host-1", body)
    assert receipts.get(TENANT, "batch-recursion").state == "pending"  # type: ignore[union-attr]

    result = _decode(service.ingest("batch-recursion", "host-1", body))
    assert result["accepted"] == 1
    assert receipts.get(TENANT, "batch-recursion").state == "completed"  # type: ignore[union-attr]


def test_batch_deadline_leaves_receipt_retryable_without_applying_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, storage, receipts = _service()
    body = _agent_record(tmp_path)
    ticks = iter((0.0, 61.0))
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(TimeoutError, match="deadline"):
        service.ingest("batch-deadline", "host-1", body)

    assert receipts.get(TENANT, "batch-deadline").state == "pending"  # type: ignore[union-attr]
    assert storage.list_agent_run_bundles(TENANT) == []


def test_batch_deadline_after_final_record_leaves_receipt_retryable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, storage, receipts = _service()
    body = _agent_record(tmp_path)
    with monkeypatch.context() as clock:
        ticks = iter((0.0, 0.0, 61.0))
        clock.setattr(collector_module.time, "monotonic", lambda: next(ticks))

        with pytest.raises(TimeoutError, match="deadline"):
            service.ingest("batch-slow-final", "host-1", body)

    assert receipts.get(TENANT, "batch-slow-final").state == "pending"  # type: ignore[union-attr]
    assert len(storage.list_agent_run_bundles(TENANT)) == 1

    result = _decode(service.ingest("batch-slow-final", "host-1", body))
    assert result["accepted"] == 1
    assert len(storage.list_agent_run_bundles(TENANT)) == 1
    assert receipts.get(TENANT, "batch-slow-final").state == "completed"  # type: ignore[union-attr]


def test_http_boundary_auth_headers_and_status_lookup(tmp_path: Path) -> None:
    service, storage, receipts = _service()
    app = create_collector_app(service, api_key=SECRET)
    body = _agent_record(tmp_path)
    headers = {
        "Authorization": f"Bearer {SECRET}",
        "Content-Type": "application/x-ndjson",
        "X-Verdict-Batch-ID": "batch-http",
        "X-Verdict-Producer-ID": "host-http",
    }

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ready"}
        unauthorized = client.post(
            "/v1/ingest",
            content=body,
            headers={key: value for key, value in headers.items() if key != "Authorization"},
        )
        assert unauthorized.status_code == 401
        assert receipts.get(TENANT, "batch-http") is None

        accepted = client.post("/v1/ingest", content=body, headers=headers)
        assert accepted.status_code == 200
        assert accepted.content == service.ingest("batch-http", "host-http", body)
        assert (
            client.get(
                "/v1/ingest/batch-http",
                headers={"Authorization": f"Bearer {SECRET}"},
            ).content
            == accepted.content
        )

    assert len(storage.list_agent_run_bundles(TENANT)) == 1


def test_collector_routes_use_the_anyio3_run_sync_call_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Anyio3Thread:
        @staticmethod
        async def run_sync(function, *args):
            return function(*args)

    class Anyio3Surface:
        to_thread = Anyio3Thread()

    monkeypatch.setattr(collector_module, "anyio", Anyio3Surface())
    service, storage, _receipts = _service()
    app = create_collector_app(service, api_key=SECRET)
    body = _agent_record(tmp_path)
    headers = {
        "Authorization": f"Bearer {SECRET}",
        "Content-Type": "application/x-ndjson",
        "X-Verdict-Batch-ID": "batch-anyio3",
        "X-Verdict-Producer-ID": "host-anyio3",
    }

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/v1/ingest", content=body, headers=headers).status_code == 200
        assert (
            client.get(
                "/v1/ingest/batch-anyio3",
                headers={"Authorization": f"Bearer {SECRET}"},
            ).status_code
            == 200
        )

    assert len(storage.list_agent_run_bundles(TENANT)) == 1


def test_http_duplicate_authorization_is_rejected_without_writes(tmp_path: Path) -> None:
    service, storage, receipts = _service()
    app = create_collector_app(service, api_key=SECRET)
    body = _agent_record(tmp_path)
    headers = [
        ("Authorization", f"Bearer {SECRET}"),
        ("Authorization", f"Bearer {SECRET}"),
        ("Content-Type", "application/x-ndjson"),
        ("X-Verdict-Batch-ID", "batch-duplicate-header"),
        ("X-Verdict-Producer-ID", "host-http"),
    ]

    with TestClient(app) as client:
        response = client.post("/v1/ingest", content=body, headers=headers)

    assert response.status_code == 401
    assert receipts.get(TENANT, "batch-duplicate-header") is None
    assert storage.list_agent_run_bundles(TENANT) == []


@pytest.mark.parametrize(
    "duplicate",
    [
        "Content-Type",
        "Content-Encoding",
        "Content-Length",
        "X-Verdict-Batch-ID",
        "X-Verdict-Producer-ID",
    ],
)
def test_http_duplicate_boundary_header_is_rejected_without_writes(
    tmp_path: Path,
    duplicate: str,
) -> None:
    service, storage, receipts = _service()
    app = create_collector_app(service, api_key=SECRET)
    body = _agent_record(tmp_path)
    headers = [
        ("Authorization", f"Bearer {SECRET}"),
        ("Content-Type", "application/x-ndjson"),
        ("Content-Encoding", "identity"),
        ("Content-Length", str(len(body))),
        ("X-Verdict-Batch-ID", "batch-duplicate-boundary"),
        ("X-Verdict-Producer-ID", "host-http"),
    ]
    value = next(value for name, value in headers if name == duplicate)
    headers.append((duplicate, value))

    with TestClient(app) as client:
        response = client.post("/v1/ingest", content=body, headers=headers)

    assert response.status_code in {400, 415}
    assert receipts.get(TENANT, "batch-duplicate-boundary") is None
    assert storage.list_agent_run_bundles(TENANT) == []


@pytest.mark.parametrize(
    ("header", "value", "expected"),
    [
        ("Content-Type", "application/json", 415),
        ("Content-Encoding", "gzip", 415),
        ("X-Verdict-Batch-ID", "not allowed", 400),
        ("X-Verdict-Producer-ID", "", 400),
    ],
)
def test_http_rejects_invalid_boundaries_before_receipt(
    tmp_path: Path,
    header: str,
    value: str,
    expected: int,
) -> None:
    service, storage, receipts = _service()
    app = create_collector_app(service, api_key=SECRET)
    headers = {
        "Authorization": f"Bearer {SECRET}",
        "Content-Type": "application/x-ndjson",
        "X-Verdict-Batch-ID": "batch-boundary",
        "X-Verdict-Producer-ID": "host-http",
        header: value,
    }

    with TestClient(app) as client:
        response = client.post("/v1/ingest", content=_agent_record(tmp_path), headers=headers)

    assert response.status_code == expected
    assert receipts.get(TENANT, "batch-boundary") is None
    assert storage.list_agent_run_bundles(TENANT) == []


def test_http_streaming_limit_rejects_oversize_body_without_receipt() -> None:
    service, storage, receipts = _service()
    app = create_collector_app(service, api_key=SECRET)
    headers = {
        "Authorization": f"Bearer {SECRET}",
        "Content-Type": "application/x-ndjson",
        "Content-Length": "1",
        "X-Verdict-Batch-ID": "batch-oversize",
        "X-Verdict-Producer-ID": "host-http",
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/ingest",
            content=b"x" * (MAX_BATCH_BYTES + 1),
            headers=headers,
        )

    assert response.status_code == 413
    assert receipts.get(TENANT, "batch-oversize") is None
    assert storage.list_agent_run_bundles(TENANT) == []


def test_http_admission_fails_fast_when_worker_capacity_is_full(tmp_path: Path) -> None:
    class BlockingStorage(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def append_agent_capture(self, batch, traces=()):
            self.entered.set()
            assert self.release.wait(timeout=10)
            return super().append_agent_capture(batch, traces)

    storage = BlockingStorage()
    service, _storage, _receipts = _service(storage)
    app = create_collector_app(service, api_key=SECRET, max_in_flight=1)
    body = _agent_record(tmp_path)

    def headers(batch_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {SECRET}",
            "Content-Type": "application/x-ndjson",
            "X-Verdict-Batch-ID": batch_id,
            "X-Verdict-Producer-ID": "host-http",
        }

    first_result: list[int] = []
    with TestClient(app) as client:
        worker = threading.Thread(
            target=lambda: first_result.append(
                client.post("/v1/ingest", content=body, headers=headers("batch-first")).status_code
            )
        )
        try:
            worker.start()
            assert storage.entered.wait(timeout=10)
            second = client.post(
                "/v1/ingest",
                content=body,
                headers=headers("batch-second"),
            )
            assert second.status_code == 503
            assert second.json() == {"error": "busy"}
        finally:
            storage.release.set()
            worker.join(timeout=10)
    assert first_result == [200]


def test_shutdown_joins_admitted_worker_before_closing_storage(tmp_path: Path) -> None:
    class ClosingBlockingStorage(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.close_count = 0

        def append_agent_capture(self, batch, traces=()):
            self.entered.set()
            assert self.release.wait(timeout=10)
            return super().append_agent_capture(batch, traces)

        def close(self) -> None:
            self.close_count += 1

    storage = ClosingBlockingStorage()
    service = CollectorService(
        storage,
        _MemoryReceiptStore(),
        tenant_id=TENANT,
    )
    app = create_collector_app(service, api_key=SECRET, close_on_shutdown=True)
    result: list[int] = []
    worker: threading.Thread | None = None

    def release_after_shutdown_starts() -> None:
        assert storage.entered.wait(timeout=10)
        threading.Event().wait(0.1)
        assert storage.close_count == 0
        storage.release.set()

    releaser = threading.Thread(target=release_after_shutdown_starts)
    with TestClient(app) as client:
        worker = threading.Thread(
            target=lambda: result.append(
                client.post(
                    "/v1/ingest",
                    content=_agent_record(tmp_path),
                    headers={
                        "Authorization": f"Bearer {SECRET}",
                        "Content-Type": "application/x-ndjson",
                        "X-Verdict-Batch-ID": "batch-shutdown",
                        "X-Verdict-Producer-ID": "host-http",
                    },
                ).status_code
            )
        )
        worker.start()
        assert storage.entered.wait(timeout=10)
        releaser.start()
    worker.join(timeout=10)
    releaser.join(timeout=10)
    assert result == [200]
    assert storage.close_count == 1


@pytest.mark.parametrize(
    ("tenant_id", "secret", "max_in_flight"),
    [
        (TENANT, "short", 16),
        ("bad\x00tenant", SECRET, 16),
        (TENANT, SECRET, 0),
    ],
)
def test_cli_validates_configuration_before_opening_storage(
    monkeypatch,
    tenant_id: str,
    secret: str,
    max_in_flight: int,
) -> None:
    from verdict.storage import postgres

    monkeypatch.setenv("VERDICT_COLLECTOR_API_KEY", secret)
    monkeypatch.setattr(
        postgres,
        "PostgresStorage",
        lambda *_args, **_kwargs: pytest.fail("storage opened before validation"),
    )
    with pytest.raises(SystemExit) as exc:
        collector_main(
            [
                "--storage",
                "postgresql://localhost/verdict",
                "--tenant-id",
                tenant_id,
                "--max-in-flight",
                str(max_in_flight),
            ]
        )
    assert exc.value.code == 2


def test_receipt_store_closes_pool_when_schema_initialization_fails(monkeypatch) -> None:
    psycopg_pool = pytest.importorskip("psycopg_pool")

    created = []

    class FailingPool:
        def __init__(self, **_kwargs) -> None:
            self.closed = False
            created.append(self)

        def connection(self, **_kwargs):
            raise RuntimeError("schema initialization failed")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", FailingPool)

    with pytest.raises(RuntimeError, match="schema initialization failed"):
        PostgresReceiptStore("postgresql://localhost/verdict")
    assert len(created) == 1
    assert created[0].closed is True
