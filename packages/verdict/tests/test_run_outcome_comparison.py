"""Historical, statistically tested comparison of recorded Agent Run outcomes."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import verdict
from verdict.capture import AgentCaptureService
from verdict.dashboard.app import create_app
from verdict.evidence import ExecutionStatus
from verdict.run_monitoring import (
    RUN_SETTLE_SECONDS,
    RunEventCount,
    RunMonitorFacts,
    run_monitor_units,
)
from verdict.storage import BufferedStorage, InMemoryStorage, SQLiteStorage

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
SETTLE = timedelta(seconds=RUN_SETTLE_SECONDS)


@pytest.fixture(autouse=True)
def _reset_verdict():
    verdict.shutdown()
    yield
    verdict.shutdown()


def _facts(run_id: str, started: datetime, status=ExecutionStatus.COMPLETED, *,
           last=None, limited=False, agent="worker", events=()) -> RunMonitorFacts:
    return RunMonitorFacts(run_id, started, status, agent, last, limited, tuple(events))


def _metrics(*facts, now=NOW, grouping="none"):
    return {unit.unit_id: unit.metrics
            for unit in run_monitor_units(facts, now=now, grouping_mode=grouping)}


def test_metrics_need_reported_outcomes() -> None:
    before = NOW - timedelta(days=1)
    assert _metrics(
        _facts("failed", before, ExecutionStatus.FAILED),
        _facts("timed-out", before, ExecutionStatus.TIMED_OUT),
        _facts("cancelled", before, ExecutionStatus.CANCELLED),
        _facts("tools", before, events=(
            RunEventCount("tool_result", "completed", False, 3),
            RunEventCount("tool_result", "unknown", True, 1),
            RunEventCount("model_call", "failed", None, 1),
            RunEventCount("model_call", "completed", None, 4),
        )),
        _facts("unreported", before, ExecutionStatus.UNKNOWN, last=before, events=(
            RunEventCount("tool_result", "unknown", None, 5),
            RunEventCount("model_call", "unknown", None, 2),
        )),
    ) == {
        "failed": {"run_failed": True},
        "timed-out": {"run_failed": True},
        "cancelled": {"run_failed": False},
        "tools": {"run_failed": False, "run_tool_error": True, "run_model_call_error": True},
        # Nothing was reported, so nothing is measured; absence is not success.
        "unreported": {},
    }


def test_capture_limited_runs_measure_no_event_metric() -> None:
    before = NOW - timedelta(days=1)
    events = (RunEventCount("tool_result", "completed", False, 1),)
    assert _metrics(_facts("limited", before, ExecutionStatus.FAILED, limited=True,
                           events=events)) == {"limited": {"run_failed": True}}


def test_open_runs_hold_back_everything_that_started_after_them() -> None:
    """Adversarial: short failed runs settle first; they must not fill a cohort alone."""
    long_open = _facts("long-open", NOW - timedelta(hours=2), ExecutionStatus.UNKNOWN,
                       last=NOW - timedelta(minutes=1))
    quick_failure = _facts("quick-failure", NOW - timedelta(hours=1), ExecutionStatus.FAILED)
    older = _facts("older", NOW - timedelta(hours=3), ExecutionStatus.COMPLETED)

    assert set(_metrics(long_open, quick_failure, older)) == {"older"}
    # Once the long run is quiet for the settle period, all three are admitted.
    later = NOW + SETTLE
    assert set(_metrics(long_open, quick_failure, older, now=later)) == {
        "older", "long-open", "quick-failure",
    }


def test_inactive_local_runs_settle_without_a_terminal_status() -> None:
    quiet = _facts("quiet", NOW - timedelta(hours=1), ExecutionStatus.UNKNOWN,
                   last=NOW - SETTLE)
    assert set(_metrics(quiet)) == {"quiet"}
    active = _facts("active", NOW - timedelta(hours=1), ExecutionStatus.UNKNOWN,
                    last=NOW - SETTLE + timedelta(seconds=1))
    assert _metrics(active) == {}


def test_agent_grouping_is_by_recorded_agent_name() -> None:
    before = NOW - timedelta(days=1)
    units = run_monitor_units(
        [_facts("a", before, agent="researcher"), _facts("b", before, agent="reviewer"),
         _facts("c", before, agent="researcher"), _facts("d", before, agent="")],
        now=NOW, grouping_mode="agent",
    )
    labels = {unit.unit_id: unit.group_label for unit in units}
    assert labels == {"a": "researcher", "b": "reviewer", "c": "researcher",
                      "d": "unnamed agent"}
    assert len({unit.group_id for unit in units}) == 3


@pytest.mark.parametrize("grouping", ["provider_model", "cluster"])
def test_trace_groupings_are_rejected(grouping) -> None:
    with pytest.raises(ValueError, match="agent grouping"):
        run_monitor_units([], now=NOW, grouping_mode=grouping)


def test_naive_evaluation_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run_monitor_units([], now=NOW.replace(tzinfo=None))


def _record_runs(storage, *, healthy: int, failing: int) -> None:
    """Record runs through the SDK: a raised tool error and a raised run failure."""
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    for index in range(healthy + failing):
        name = "researcher" if index % 2 else "reviewer"
        try:
            with verdict.agent_run(name=name, external_id=f"run-{index:03d}") as run:
                with run.turn(user_input="find the order") as turn:
                    if index >= healthy:
                        with pytest.raises(RuntimeError):
                            with turn.tool("lookup"):
                                raise RuntimeError("lookup failed")
                        raise RuntimeError("agent gave up")
                    with turn.tool("lookup") as tool:
                        tool.set_output({"found": True})
                    turn.set_output("found")
        except RuntimeError:
            pass


def _storage(kind: str, stack: ExitStack, tmp_path):
    if kind == "memory":
        return InMemoryStorage()
    if kind == "buffered":
        return BufferedStorage(SQLiteStorage(str(tmp_path / "buffered.db")))
    if kind == "postgres":
        from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
        from verdict.storage import PostgresStorage

        dsn, reason = validate_test_dsn(
            os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
        )
        if dsn is None:
            pytest.skip(reason)
        return PostgresStorage(stack.enter_context(isolated_test_dsn(dsn)))
    return SQLiteStorage(str(tmp_path / "runs.db"))


@pytest.mark.parametrize("kind", ["memory", "sqlite", "buffered", "postgres"])
def test_adapters_read_the_same_run_facts(tmp_path, kind) -> None:
    with ExitStack() as stack:
        reference = InMemoryStorage()
        _record_runs(reference, healthy=3, failing=2)
        storage = _storage(kind, stack, tmp_path)
        try:
            for bundle in reference.list_agent_run_bundles("tenant-a", limit=100):
                AgentCaptureService(storage).capture(bundle, traces=())
            expected = reference.load_run_monitor_facts("tenant-a")
            facts = storage.load_run_monitor_facts("tenant-a")
            assert facts == expected
            assert storage.load_run_monitor_facts("other-tenant") == []
            assert sorted(
                (fact.status.value, [(c.event_type, c.status, c.is_error, c.count)
                                     for c in fact.event_counts])
                for fact in facts
            ) == sorted(
                [("completed", [("tool_result", "completed", False, 1)])] * 3
                + [("failed", [("tool_result", "failed", True, 1)])] * 2
            )
        finally:
            storage.close()


def test_dashboard_compares_runs_and_saves_nothing(tmp_path) -> None:
    database = tmp_path / "runs.db"
    _record_runs(SQLiteStorage(str(database)), healthy=40, failing=40)
    verdict.shutdown()  # closes the recording client's storage

    async def preview(payload):
        app = create_app(storage=f"sqlite:///{database}", tenant_id="tenant-a")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            response = await client.post(
                "/api/monitor/preview", headers={"X-Verdict-Setup": token}, json=payload,
            )
            return response, await client.get("/api/monitor")

    response, state = asyncio.run(preview({
        "analysisUnit": "run", "windowMode": "count", "referenceRatio": 0.5,
        "groupingMode": "none", "minimumReference": 30, "minimumCurrent": 30,
    }))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "historical_run"
    assert body["policyState"] == "not_saved"
    assert body["activationAllowed"] is False
    metrics = {item["metric"]: item for item in body["snapshot"]["comparison"]["metrics"]}
    assert (metrics["run_failed"]["reference_value"],
            metrics["run_failed"]["current_value"], metrics["run_failed"]["alert"]) == (
        0.0, 1.0, True,
    )
    assert metrics["run_tool_error"]["alert"] is True
    assert state.json()["candidate"] is None
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM monitor_policies").fetchone()[0] == 0
    finally:
        connection.close()

    grouped, _ = asyncio.run(preview({
        "analysisUnit": "run", "windowMode": "count", "referenceRatio": 0.5,
        "groupingMode": "agent", "minimumReference": 10, "minimumCurrent": 10,
    }))
    assert grouped.status_code == 200, grouped.text
    groups = {item["label"] for item in grouped.json()["snapshot"]["comparison"]["groups"]}
    assert groups == {"researcher", "reviewer"}

    rejected, _ = asyncio.run(preview({
        "analysisUnit": "run", "windowMode": "count", "groupingMode": "provider_model",
    }))
    assert rejected.status_code == 400
    assert rejected.json()["error"] == (
        "Run comparisons support no grouping or grouping by agent name."
    )
    judged, _ = asyncio.run(preview({
        "analysisUnit": "run", "windowMode": "count", "evaluatorFingerprint": "f" * 64,
    }))
    assert judged.status_code == 400
    assert judged.json()["error"] == (
        "Run comparisons use recorded run outcomes only; choose no evaluator."
    )
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM monitor_policies").fetchone()[0] == 0
    finally:
        connection.close()
