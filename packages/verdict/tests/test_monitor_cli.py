import json
from datetime import datetime, timedelta, timezone

from verdict.monitor_cli import main
from verdict.monitoring import (
    MonitorPolicy,
    compare_manifest,
    plan_historical_manifest,
    trace_monitor_units,
)
from verdict.schema import Trace
from verdict.storage import SQLiteStorage


def test_monitor_cli_runs_one_idempotent_durable_cycle(tmp_path, capsys) -> None:
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(10):
        storage.insert_trace(Trace(
            trace_id=f"trace-{index}", started_at=now + timedelta(days=index),
            response_redacted="ok",
        ))
    policy = MonitorPolicy(
        "policy", "__verdict_local__:application:trace", reference_ratio=0.8,
        minimum_reference=2, minimum_current=2, prospective_target=2,
    )
    units = trace_monitor_units(storage.list_traces(limit=100))
    manifest = plan_historical_manifest(units, policy, cutoff=now + timedelta(days=10))
    storage.save_monitor_policy(policy)
    storage.save_monitor_snapshot(policy.policy_id, manifest,
                                  compare_manifest(units, manifest, policy))
    storage.activate_monitor_policy(
        policy.scope_key, policy.policy_id, expected_active_policy_id=None,
    )
    storage.close()

    first = main(["run", "--storage", f"sqlite:///{path}"])
    first_output = json.loads(capsys.readouterr().out)
    second = main(["run", "--storage", f"sqlite:///{path}"])
    second_output = json.loads(capsys.readouterr().out)

    assert first == second == 0
    assert first_output["status"] == "insufficient"
    assert first_output["current_units"] == 0
    assert second_output == first_output


def test_monitor_cli_reports_missing_policy_without_trace_content(tmp_path, capsys) -> None:
    path = tmp_path / "empty.db"
    SQLiteStorage(str(path)).close()

    assert main(["run", "--storage", f"sqlite:///{path}"]) == 2
    assert "no active monitor" in capsys.readouterr().err
