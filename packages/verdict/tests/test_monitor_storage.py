from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from verdict.schema import DriftRun, MonitorMember, MonitorResult, MonitorSeries
from verdict.storage import BufferedStorage
from verdict.storage.memory import InMemoryStorage
from verdict.storage.sqlite import SQLiteStorage

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _series(series_id: str, state: str, *, parent: str | None = None) -> MonitorSeries:
    return MonitorSeries(
        series_id=series_id,
        scope_key="scope",
        scope_json='{"granularity":"session","tenant_id":null,"workload":"production"}',
        state=state,
        generation=0,
        parent_series_id=parent,
        registry_json='{"clusters":[],"version":"1"}',
        boundary_time=NOW,
        boundary_trace_id="history-2",
        target_units=2,
        late_arrival_count=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _member(series_id: str, trace_id: str, role: str = "baseline") -> MonitorMember:
    return MonitorMember(
        series_id=series_id,
        trace_id=trace_id,
        role=role,
        bucket_index=1 if role == "current" else 0,
        cluster_id="cluster-1",
        unit_id=f"session-{trace_id}",
        event_time=NOW + timedelta(minutes=int(trace_id[-1])),
    )


def _result(series_id: str, *, status: str = "candidate") -> MonitorResult:
    return MonitorResult(
        series_id=series_id,
        cluster_id="cluster-1",
        bucket_index=1,
        run_id="run-1",
        status=status,
        direction_key="response_words:+",
        completed_at=NOW + timedelta(hours=1),
    )


@pytest.fixture(params=["memory", "sqlite", "buffered"])
def monitor_storage(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "memory":
        storage = InMemoryStorage()
    elif request.param == "sqlite":
        storage = SQLiteStorage(str(tmp_path / "monitor.db"))
    else:
        storage = BufferedStorage(InMemoryStorage(), flush_interval=0.01)
    yield storage
    storage.close()


def test_monitor_storage_cycle_is_atomic_and_activation_is_compare_and_swap(
    monitor_storage,
) -> None:
    active = _series("active", "active")
    baseline = [_member("active", "history-1"), _member("active", "history-2")]
    monitor_storage.create_monitor_series(active, baseline)
    assert monitor_storage.get_active_monitor_series("scope") == active

    current = _member("active", "current-3", "current")
    updated = monitor_storage.commit_monitor_cycle(
        series_id="active",
        expected_generation=0,
        members=[current],
        results=[_result("active")],
        snapshots=[],
        late_arrival_delta=0,
    )
    assert updated.generation == 1
    assert monitor_storage.list_monitor_results("active") == [_result("active")]

    rejected = _member("active", "current-4", "current")
    with pytest.raises(ValueError, match="result identity conflict"):
        monitor_storage.commit_monitor_cycle(
            series_id="active",
            expected_generation=1,
            members=[rejected],
            results=[_result("active", status="confirmed")],
            snapshots=[],
            late_arrival_delta=0,
        )
    assert rejected not in monitor_storage.list_monitor_members("active")
    assert monitor_storage.get_monitor_series("active").generation == 1

    candidate = _series("candidate", "candidate", parent="active")
    monitor_storage.create_monitor_series(candidate, [])
    owned = DriftRun(run_id="owned-run", evaluator_fingerprint="owner-a", signal_count=0)
    monitor_storage.replace_drift_run(owned, [])
    conflicting = DriftRun(run_id="owned-run", evaluator_fingerprint="owner-b", signal_count=0)
    with pytest.raises(ValueError, match="another evaluator"):
        monitor_storage.activate_monitor_series(
            "candidate",
            expected_active_series_id="active",
            snapshot=(conflicting, []),
        )
    assert monitor_storage.get_monitor_series("active").state == "active"
    assert monitor_storage.get_monitor_series("candidate").state == "candidate"

    activated = monitor_storage.activate_monitor_series(
        "candidate", expected_active_series_id="active"
    )
    assert activated.state == "active"
    assert monitor_storage.get_monitor_series("active").state == "retired"
    with pytest.raises(ValueError, match="compare-and-swap"):
        monitor_storage.activate_monitor_series("active", expected_active_series_id="active")
