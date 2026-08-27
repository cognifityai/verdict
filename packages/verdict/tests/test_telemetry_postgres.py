"""Live PostgreSQL contract for the telemetry importer.

CI supplies ``VERDICT_TEST_POSTGRES_DSN``. Local runs skip when it is absent.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from verdict.storage.postgres import PostgresStorage
from verdict.telemetry.files import iter_telemetry_file
from verdict.telemetry.model import ImportContext
from verdict.telemetry.runner import import_into_storage

DSN = os.environ.get("VERDICT_TEST_POSTGRES_DSN")
ROOT = Path(__file__).parents[3]

pytestmark = pytest.mark.skipif(not DSN, reason="VERDICT_TEST_POSTGRES_DSN not set")


def test_live_postgres_imports_existing_telemetry_through_storage_port() -> None:
    tenant = f"telemetry-{uuid4().hex}"
    storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
    trace_id: str | None = None
    try:
        summary = import_into_storage(
            iter_telemetry_file(
                ROOT / "examples" / "telemetry" / "langfuse-observations.json",
                file_format="langfuse",
                context=ImportContext(
                    adapter="file",
                    source_scope="postgres-contract",
                    tenant_id=tenant,
                ),
            ),
            storage,
        )

        rows = storage.list_traces(tenant_id=tenant, limit=10)
        assert summary.stored == 1
        assert summary.skipped == 0
        assert len(rows) == 1
        [trace] = rows
        trace_id = trace.trace_id
        assert trace.input_tokens == 20
        assert trace.output_tokens == 7
        assert trace.latency_ms == pytest.approx(600)
        assert trace.prompt_redacted == "I need a refund"
        assert trace.response_redacted == "I started your refund."
    finally:
        if trace_id is not None:
            storage.delete_trace(trace_id)
        storage.close()
