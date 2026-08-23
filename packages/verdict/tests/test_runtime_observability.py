from __future__ import annotations

import pytest
from verdict.client import (
    VerdictClient,
    clear_context,
    get_context_workload,
    set_context,
    workload_context,
)
from verdict.instrumentors.base import apply_routing_context, safe_persist_trace
from verdict.schema import Trace
from verdict.storage.buffered import BufferedStorage
from verdict.storage.memory import InMemoryStorage


def test_real_persistence_boundary_records_bounded_capture_overhead() -> None:
    client = VerdictClient(storage=InMemoryStorage())

    safe_persist_trace(client, Trace(provider="custom-provider"))

    snapshot = client.runtime_metrics.snapshot(client.storage)
    assert snapshot["capture"]["attempts"] == 1
    assert snapshot["capture"]["failures"] == 0
    assert snapshot["capture"]["routing_context_skips"] == 0
    assert snapshot["capture"]["overhead_ms"]["samples"] == 1
    assert snapshot["capture"]["overhead_ms"]["p50"] >= 0
    assert snapshot["buffer"] == {"enabled": False}


def test_persistence_failure_is_counted_without_exposing_exception_text() -> None:
    class FailingStorage(InMemoryStorage):
        def insert_trace(self, trace: Trace) -> None:
            raise RuntimeError("canary-database-password")

    client = VerdictClient(storage=FailingStorage())

    safe_persist_trace(client, Trace(provider="custom-provider"))

    snapshot = client.runtime_metrics.snapshot(client.storage)
    assert snapshot["capture"]["attempts"] == 1
    assert snapshot["capture"]["failures"] == 1
    assert "canary-database-password" not in str(snapshot)


def test_buffer_snapshot_distinguishes_enabled_empty_and_failed_writes() -> None:
    class FailingStorage(InMemoryStorage):
        def insert_trace(self, trace: Trace) -> None:
            raise RuntimeError("write failed")

    buffered = BufferedStorage(FailingStorage(), flush_interval=0.01)
    try:
        buffered.insert_trace(Trace(trace_id="failed"))
        buffered.flush()

        snapshot = buffered.telemetry_snapshot()
        assert snapshot["enabled"] is True
        assert snapshot["queue_depth"] == 0
        assert snapshot["accepted"] == 1
        assert snapshot["written"] == 0
        assert snapshot["write_errors"] == 1
    finally:
        buffered.close()


def test_workload_context_is_explicit_and_unknown_values_remain_data_driven() -> None:
    client = VerdictClient(storage=InMemoryStorage())
    first = Trace()
    second = Trace()
    try:
        set_context(workload="judge")
        apply_routing_context(client, first)
        set_context(workload="customer-specialist")
        apply_routing_context(client, second)
    finally:
        clear_context()

    assert first.tags["verdict.workload"] == "judge"
    assert second.tags["verdict.workload"] == "customer-specialist"

    clean = Trace()
    apply_routing_context(client, clean)
    assert "verdict.workload" not in clean.tags


def test_workload_context_restores_previous_value_after_failure() -> None:
    set_context(workload="agent")
    try:
        with pytest.raises(RuntimeError):
            with workload_context("judge"):
                assert get_context_workload() == "judge"
                raise RuntimeError("judge failed")

        assert get_context_workload() == "agent"
    finally:
        clear_context()


@pytest.mark.parametrize("value", [" space", "bad/value", "x" * 65])
def test_workload_context_rejects_unbounded_or_unsafe_labels(value: str) -> None:
    with pytest.raises(ValueError, match="workload must be"):
        with workload_context(value):
            pass
