import json
from datetime import datetime, timedelta, timezone

from verdict.monitor_cli import main
from verdict.monitor_inputs import load_monitor_units
from verdict.monitoring import (
    MonitorPolicy,
    compare_manifest,
    plan_historical_manifest,
)
from verdict.schema import DimensionScore, Judgment, Trace, Verdict
from verdict.storage import SQLiteStorage


def test_monitor_cli_runs_one_idempotent_durable_cycle(tmp_path, capsys) -> None:
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    fingerprint = "a" * 64
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(10):
        trace_id = f"trace-{index}"
        storage.insert_trace(Trace(
            trace_id=trace_id, started_at=now + timedelta(days=index),
            provider="openai", request_model="model", response_redacted="ok",
        ))
        storage.insert_judgment(Judgment(
            trace_id=trace_id, evaluator_fingerprint=fingerprint,
            evaluator_provider="anthropic", judge_models=["judge"],
            expected_dimensions=["quality"],
            dimensions=[DimensionScore("quality", Verdict.PASS)],
        ))
    policy = MonitorPolicy(
        "policy", "__verdict_local__:application:trace", reference_ratio=0.8,
        minimum_reference=2, minimum_current=2, prospective_target=2,
        p_threshold=1.0, minimum_effect=0.5, grouping_mode="provider_model",
        evaluator_fingerprint=fingerprint, evaluator_dimensions=("quality",),
    )
    units = load_monitor_units(storage, policy, tenant_id="__verdict_local__")
    manifest = plan_historical_manifest(units, policy, cutoff=now + timedelta(days=10))
    storage.save_monitor_policy(policy)
    storage.save_monitor_snapshot(policy.policy_id, manifest,
                                  compare_manifest(units, manifest, policy))
    storage.activate_monitor_policy(
        policy.scope_key, policy.policy_id, expected_active_policy_id=None,
    )
    for index in range(10, 12):
        trace_id = f"trace-{index}"
        storage.insert_trace(Trace(
            trace_id=trace_id, started_at=now + timedelta(days=index),
            provider="openai", request_model="model", response_redacted="ok",
        ))
        storage.insert_judgment(Judgment(
            trace_id=trace_id, evaluator_fingerprint=fingerprint,
            evaluator_provider="anthropic", judge_models=["judge"],
            expected_dimensions=["quality"],
            dimensions=[DimensionScore("quality", Verdict.FAIL)],
        ))
    storage.close()

    first = main(["run", "--storage", f"sqlite:///{path}"])
    first_output = json.loads(capsys.readouterr().out)
    storage = SQLiteStorage(str(path))
    try:
        comparison = storage.get_latest_monitor_snapshot("policy")[1]
    finally:
        storage.close()
    second = main(["run", "--storage", f"sqlite:///{path}"])
    second_output = json.loads(capsys.readouterr().out)
    third = main(["run", "--storage", f"sqlite:///{path}"])
    third_output = json.loads(capsys.readouterr().out)

    assert first == second == third == 0
    assert first_output["status"] == "alert"
    assert first_output["current_units"] == 2
    assert second_output["status"] == "insufficient"
    assert second_output["current_units"] == 0
    assert third_output == second_output
    quality = next(item for item in comparison.metrics if item.metric == "judge.quality.pass")
    assert quality.group_id.startswith("provider_model:")
    [group] = comparison.groups
    assert (group.provider, group.model, group.label) == (
        "openai",
        "model",
        "openai / model",
    )
    assert quality.current_value == 0.0


def test_monitor_cli_reports_missing_policy_without_trace_content(tmp_path, capsys) -> None:
    path = tmp_path / "empty.db"
    SQLiteStorage(str(path)).close()

    assert main(["run", "--storage", f"sqlite:///{path}"]) == 2
    assert "no active monitor" in capsys.readouterr().err
