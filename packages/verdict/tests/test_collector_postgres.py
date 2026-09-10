"""Live PostgreSQL collector tests; never run against an unapproved database."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
import verdict
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from fastapi.testclient import TestClient
from test_collector import TENANT, _agent_record
from verdict.collector import (
    CollectorService,
    PostgresReceiptStore,
    ReceiptBusy,
    ReceiptConflict,
    ReceiptCorrupt,
    create_collector_app,
)
from verdict.dashboard.app import create_app as create_dashboard_app
from verdict.evidence import ExecutionStatus
from verdict.instrumentors.base import apply_routing_context, safe_persist_trace
from verdict.schema import Trace
from verdict.storage.postgres import PostgresStorage

DSN, POSTGRES_SKIP_REASON = validate_test_dsn(
    os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
    allow_any_database=os.environ.get("VERDICT_TEST_POSTGRES_ALLOW_ANY_DB") == "1",
)
if os.environ.get("VERDICT_REQUIRE_POSTGRES_TESTS") == "1" and DSN is None:
    raise RuntimeError(
        "VERDICT_REQUIRE_POSTGRES_TESTS=1 but live PostgreSQL tests are unsafe: "
        f"{POSTGRES_SKIP_REASON}"
    )

pytestmark = [
    pytest.mark.skipif(DSN is None, reason=POSTGRES_SKIP_REASON),
    pytest.mark.filterwarnings("error::DeprecationWarning:psycopg_pool.*"),
]


@contextmanager
def _collector_pair():
    with isolated_test_dsn(DSN) as scoped_dsn:
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=2)
        receipts = PostgresReceiptStore(scoped_dsn, min_pool=1, max_pool=2)
        try:
            yield scoped_dsn, storage, receipts
        finally:
            receipts.close()
            storage.close()


def test_live_postgres_replay_is_byte_identical_after_receipt_restart(
    tmp_path: Path,
) -> None:
    body = _agent_record(tmp_path)
    with _collector_pair() as (dsn, storage, receipts):
        service = CollectorService(storage, receipts, tenant_id=TENANT)
        first = service.ingest("batch-restart", "host-1", body)
        receipts.close()

        reopened = PostgresReceiptStore(dsn, min_pool=1, max_pool=2)
        try:
            replay = CollectorService(storage, reopened, tenant_id=TENANT).ingest(
                "batch-restart", "host-1", body
            )
            assert replay == first
            assert len(storage.list_agent_run_bundles(TENANT)) == 1
        finally:
            reopened.close()


def test_live_postgres_concurrent_delivery_has_one_receipt_owner(
    tmp_path: Path,
) -> None:
    class BlockingStorage:
        def __init__(self, storage: PostgresStorage) -> None:
            self.storage = storage
            self.entered = threading.Event()
            self.release = threading.Event()

        def append_agent_capture(self, batch, traces=()):
            self.entered.set()
            assert self.release.wait(timeout=10)
            self.storage.append_agent_capture(batch, traces)

    body = _agent_record(tmp_path)
    with _collector_pair() as (dsn, storage, first_receipts):
        second_receipts = PostgresReceiptStore(dsn, min_pool=1, max_pool=2)
        blocking = BlockingStorage(storage)
        first = CollectorService(blocking, first_receipts, tenant_id=TENANT)
        second = CollectorService(storage, second_receipts, tenant_id=TENANT)
        result: list[bytes] = []
        worker = threading.Thread(
            target=lambda: result.append(first.ingest("batch-race", "host-1", body))
        )
        try:
            worker.start()
            assert blocking.entered.wait(timeout=10)
            with pytest.raises(ReceiptBusy):
                second.ingest("batch-race", "host-1", body)
            blocking.release.set()
            worker.join(timeout=10)
            assert not worker.is_alive()
            assert second.ingest("batch-race", "host-1", body) == result[0]
            assert len(storage.list_agent_run_bundles(TENANT)) == 1
        finally:
            blocking.release.set()
            worker.join(timeout=10)
            second_receipts.close()


def test_live_postgres_concurrent_different_body_cannot_take_over_batch(
    tmp_path: Path,
) -> None:
    class BlockingStorage:
        def __init__(self, storage: PostgresStorage) -> None:
            self.storage = storage
            self.entered = threading.Event()
            self.release = threading.Event()

        def append_agent_capture(self, batch, traces=()):
            self.entered.set()
            assert self.release.wait(timeout=10)
            self.storage.append_agent_capture(batch, traces)

    first_body = _agent_record(tmp_path, trace_id="first-body")
    second_body = _agent_record(tmp_path, trace_id="second-body")
    with _collector_pair() as (dsn, storage, first_receipts):
        second_receipts = PostgresReceiptStore(dsn, min_pool=1, max_pool=2)
        blocking = BlockingStorage(storage)
        first = CollectorService(blocking, first_receipts, tenant_id=TENANT)
        second = CollectorService(storage, second_receipts, tenant_id=TENANT)
        worker = threading.Thread(
            target=lambda: first.ingest("batch-body-race", "host-1", first_body)
        )
        try:
            worker.start()
            assert blocking.entered.wait(timeout=10)
            with pytest.raises(ReceiptBusy):
                second.ingest("batch-body-race", "host-1", second_body)
            blocking.release.set()
            worker.join(timeout=10)
            assert not worker.is_alive()
            with pytest.raises(ReceiptConflict):
                second.ingest("batch-body-race", "host-1", second_body)
            assert storage.get_agent_run_bundle(TENANT, "run-first-body") is not None
            assert storage.get_agent_run_bundle(TENANT, "run-second-body") is None
        finally:
            blocking.release.set()
            worker.join(timeout=10)
            second_receipts.close()


def test_live_postgres_corrupt_completed_ack_fails_closed(
    tmp_path: Path,
) -> None:
    import psycopg

    body = _agent_record(tmp_path)
    with _collector_pair() as (dsn, storage, receipts):
        service = CollectorService(storage, receipts, tenant_id=TENANT)
        service.ingest("batch-corrupt-ack", "host-1", body)
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE collector_receipts SET response_body=%s WHERE tenant_id=%s AND batch_id=%s",
                (b"not-json", TENANT, "batch-corrupt-ack"),
            )

        with pytest.raises(ReceiptCorrupt):
            service.get_receipt("batch-corrupt-ack")
        app = create_collector_app(service, api_key="collector-test-secret-value")
        with TestClient(app) as client:
            response = client.get(
                "/v1/ingest/batch-corrupt-ack",
                headers={"Authorization": "Bearer collector-test-secret-value"},
            )
        assert response.status_code == 503
        assert response.json() == {"error": "unavailable"}


def test_live_postgres_retry_after_partial_application_converges(
    tmp_path: Path,
) -> None:
    class FailBeforeSecondRecord:
        def __init__(self, storage: PostgresStorage) -> None:
            self.storage = storage
            self.calls = 0

        def append_agent_capture(self, batch, traces=()):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("injected process failure")
            self.storage.append_agent_capture(batch, traces)

    body = _agent_record(tmp_path, trace_id="first") + _agent_record(tmp_path, trace_id="second")
    with _collector_pair() as (dsn, storage, receipts):
        failing = CollectorService(FailBeforeSecondRecord(storage), receipts, tenant_id=TENANT)
        with pytest.raises(RuntimeError, match="injected process failure"):
            failing.ingest("batch-partial", "host-1", body)
        assert receipts.get(TENANT, "batch-partial").state == "pending"  # type: ignore[union-attr]
        receipts.close()

        reopened = PostgresReceiptStore(dsn, min_pool=1, max_pool=2)
        try:
            response = CollectorService(storage, reopened, tenant_id=TENANT).ingest(
                "batch-partial", "host-1", body
            )
            assert b'"accepted":2' in response
            assert len(storage.list_agent_run_bundles(TENANT)) == 2
        finally:
            reopened.close()


def test_live_postgres_cross_tenant_trace_collision_fails_without_mutation(
    tmp_path: Path,
) -> None:
    body = _agent_record(tmp_path, trace_id="shared-trace-id")
    with _collector_pair() as (_dsn, storage, receipts):
        tenant_a = CollectorService(storage, receipts, tenant_id="tenant-a")
        tenant_b = CollectorService(storage, receipts, tenant_id="tenant-b")

        assert b'"accepted":1' in tenant_a.ingest("batch-a", "host-a", body)
        rejected = tenant_b.ingest("batch-b", "host-b", body)

        assert b'"evidence_conflict"' in rejected
        assert storage.list_agent_run_bundles("tenant-b") == []
        [trace] = storage.list_traces(tenant_id="tenant-a")
        assert trace.trace_id == "shared-trace-id"
        assert trace.tenant_id == "tenant-a"


def test_live_postgres_out_of_order_lifecycle_revisions_converge(tmp_path: Path) -> None:
    with _collector_pair() as (_dsn, storage, receipts):
        service = CollectorService(storage, receipts, tenant_id=TENANT)
        for trace_id, statuses in (
            ("forward", (ExecutionStatus.UNKNOWN, ExecutionStatus.COMPLETED)),
            ("reverse", (ExecutionStatus.COMPLETED, ExecutionStatus.UNKNOWN)),
        ):
            for index, status in enumerate(statuses):
                service.ingest(
                    f"batch-{trace_id}-{index}",
                    "host-1",
                    _agent_record(tmp_path, trace_id=trace_id, status=status),
                )

        bundles = {bundle.run.run_id: bundle for bundle in storage.list_agent_run_bundles(TENANT)}
        for trace_id in ("forward", "reverse"):
            bundle = bundles[f"run-{trace_id}"]
            assert bundle.run.status is ExecutionStatus.COMPLETED
            assert bundle.turns[0].status is ExecutionStatus.COMPLETED
            assert bundle.events[0].status is ExecutionStatus.COMPLETED
            trace = storage.get_trace(trace_id)
            assert trace is not None
            assert trace.response_redacted == "answer"


def test_live_postgres_terminal_conflict_rejects_only_that_record(
    tmp_path: Path,
) -> None:
    with _collector_pair() as (_dsn, storage, receipts):
        service = CollectorService(storage, receipts, tenant_id=TENANT)
        service.ingest(
            "batch-original",
            "host-1",
            _agent_record(
                tmp_path,
                trace_id="conflict",
                status=ExecutionStatus.COMPLETED,
            ),
        )
        body = _agent_record(
            tmp_path,
            trace_id="conflict",
            status=ExecutionStatus.FAILED,
        ) + _agent_record(tmp_path, trace_id="sibling")

        response = service.ingest("batch-conflict", "host-1", body)

        assert b'"accepted":1' in response
        assert b'"rejected":1' in response
        assert b'"evidence_conflict"' in response
        assert storage.get_agent_run_bundle(TENANT, "run-sibling") is not None
        original = storage.get_agent_run_bundle(TENANT, "run-conflict")
        assert original is not None
        assert original.run.status is ExecutionStatus.COMPLETED


def test_live_postgres_sdk_file_to_http_to_dashboard_journey(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spool = tmp_path / "sdk-spool"
    now = datetime.now(timezone.utc)
    verdict.shutdown()
    sdk = verdict.init(
        transport="file",
        spool_directory=spool,
        tenant_id="payload-tenant",
        service_name="remote-agent",
        instrumentors=[],
    )
    with verdict.agent_run(name="support-agent", external_id="remote-run") as run:
        with run.turn(user_input="find order") as turn:
            with turn.tool("lookup_order", arguments={"id": "123"}) as tool:
                tool.set_output({"found": True})
            trace = Trace(
                trace_id="remote-model-call",
                provider="openai",
                request_model="test-model",
                response_model="test-model",
                started_at=now,
                ended_at=now,
                prompt_redacted="find order",
                response_redacted="order found",
            )
            apply_routing_context(sdk, trace)
            safe_persist_trace(sdk, trace)
            turn.set_output("order found")
    verdict.shutdown()
    body = b"".join(item.read_bytes() for item in sorted(spool.glob("verdict-agent-*.jsonl")))

    with _collector_pair() as (dsn, storage, receipts):
        service = CollectorService(storage, receipts, tenant_id="__verdict_local__")
        collector = create_collector_app(service, api_key="collector-test-secret-value")
        headers = {
            "Authorization": "Bearer collector-test-secret-value",
            "Content-Type": "application/x-ndjson",
            "X-Verdict-Batch-ID": "sdk-batch",
            "X-Verdict-Producer-ID": "sdk-host",
        }
        with TestClient(collector) as client:
            response = client.post("/v1/ingest", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["rejected"] == 0

        [bundle] = storage.list_agent_run_bundles("__verdict_local__")
        assert storage.get_trace("remote-model-call") is not None
        monkeypatch.delenv("VERDICT_USER", raising=False)
        monkeypatch.delenv("VERDICT_PASS", raising=False)
        dashboard = create_dashboard_app(storage=dsn)
        with TestClient(dashboard) as client:
            detail = client.get(f"/api/runs/{bundle.run.run_id}").json()
        assert detail["agentName"] == "support-agent"
        assert any(event["type"] == "tool_call" for event in detail["events"])
        assert any(event["traceId"] == "remote-model-call" for event in detail["events"])
