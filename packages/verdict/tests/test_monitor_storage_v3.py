from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from verdict.monitoring import (
    AnalysisUnitRecord,
    MonitorPolicy,
    MonitorStatus,
    compare_manifest,
    plan_historical_manifest,
    plan_prospective_manifest,
)
from verdict.storage import BufferedStorage, InMemoryStorage, SQLiteStorage

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "sqlite", "buffered"])
def storage(request, tmp_path):
    if request.param == "memory":
        value = InMemoryStorage()
    elif request.param == "buffered":
        value = BufferedStorage(InMemoryStorage())
    else:
        value = SQLiteStorage(str(tmp_path / "monitor.db"))
    try:
        yield value
    finally:
        value.close()


def _policy(policy_id="p1", effect=0.1):
    return MonitorPolicy(policy_id, "tenant:app", reference_ratio=0.5,
                         minimum_reference=2, minimum_current=2,
                         minimum_effect=effect)


def _snapshot(policy):
    units = tuple(
        AnalysisUnitRecord(f"u-{index}", NOW + timedelta(minutes=index),
                           {"failed": index >= 3})
        for index in range(6)
    )
    manifest = plan_historical_manifest(units, policy, cutoff=NOW + timedelta(hours=1))
    return manifest, compare_manifest(units, manifest, policy)


def test_candidate_activation_is_atomic_and_optimistic(storage) -> None:
    first, second = _policy(), _policy("p2")
    storage.save_monitor_policy(first)
    storage.save_monitor_policy(second)

    assert storage.get_active_monitor_policy("tenant:app") is None
    assert storage.activate_monitor_policy(
        "tenant:app", "p1", expected_active_policy_id=None
    ) == first
    assert storage.get_active_monitor_policy("tenant:app") == first
    assert storage.activate_monitor_policy(
        "tenant:app", "p2", expected_active_policy_id="p1"
    ) == second
    assert storage.get_monitor_policy("p1") == (first, "retired")
    assert storage.get_monitor_policy("p2") == (second, "active")
    with pytest.raises(ValueError, match="active policy changed"):
        storage.activate_monitor_policy(
            "tenant:app", "p1", expected_active_policy_id="missing"
        )


def test_policy_identity_is_immutable(storage) -> None:
    storage.save_monitor_policy(_policy())
    storage.save_monitor_policy(_policy())

    with pytest.raises(ValueError, match="different definition"):
        storage.save_monitor_policy(_policy(effect=0.4))


def test_monitor_snapshot_is_idempotent_and_bound_to_policy(storage) -> None:
    policy = _policy()
    storage.save_monitor_policy(policy)
    manifest, comparison = _snapshot(policy)

    storage.save_monitor_snapshot(policy.policy_id, manifest, comparison)
    storage.save_monitor_snapshot(policy.policy_id, manifest, comparison)

    assert storage.get_latest_monitor_snapshot(policy.policy_id) == (manifest, comparison)
    with pytest.raises(ValueError, match="unknown policy"):
        storage.save_monitor_snapshot("missing", manifest, comparison)


def test_snapshot_content_cannot_change_under_same_identity(storage) -> None:
    policy = _policy()
    storage.save_monitor_policy(policy)
    manifest, comparison = _snapshot(policy)
    storage.save_monitor_snapshot(policy.policy_id, manifest, comparison)

    changed = replace(comparison, unseen_group_share=0.5)
    with pytest.raises(ValueError, match="different content"):
        storage.save_monitor_snapshot(policy.policy_id, manifest, changed)


def test_snapshot_cannot_be_attached_to_a_different_policy(storage) -> None:
    first, second = _policy(), _policy("p2", effect=0.4)
    storage.save_monitor_policy(first)
    storage.save_monitor_policy(second)
    manifest, comparison = _snapshot(first)

    with pytest.raises(ValueError, match="does not match policy"):
        storage.save_monitor_snapshot(second.policy_id, manifest, comparison)


def test_latest_snapshot_is_the_last_saved_when_event_cutoff_does_not_advance(storage) -> None:
    policy = _policy()
    storage.save_monitor_policy(policy)
    manifest, comparison = _snapshot(policy)
    storage.save_monitor_snapshot(policy.policy_id, manifest, comparison)

    collecting = plan_prospective_manifest(manifest, (), policy)
    collecting_result = compare_manifest((), collecting, policy)
    assert collecting.cutoff == manifest.cutoff
    storage.save_monitor_snapshot(policy.policy_id, collecting, collecting_result)

    assert storage.get_latest_monitor_snapshot(policy.policy_id) == (
        collecting, collecting_result,
    )
    assert storage.get_initial_monitor_snapshot(policy.policy_id) == (
        manifest, comparison,
    )


def test_prior_alert_remains_queryable_after_later_insufficient_snapshot(storage) -> None:
    policy = _policy()
    storage.save_monitor_policy(policy)
    manifest, comparison = _snapshot(policy)
    alert = replace(comparison, status=MonitorStatus.ALERT)
    storage.save_monitor_snapshot(policy.policy_id, manifest, alert)
    collecting = plan_prospective_manifest(manifest, (), policy)
    storage.save_monitor_snapshot(
        policy.policy_id, collecting, compare_manifest((), collecting, policy)
    )

    assert storage.get_latest_monitor_snapshot(policy.policy_id)[0] == collecting
    assert storage.get_latest_monitor_alert(policy.policy_id) == (manifest, alert)


def test_sqlite_snapshot_size_violation_is_loud_and_preserves_previous_row(
    tmp_path, monkeypatch,
) -> None:
    storage = SQLiteStorage(str(tmp_path / "bounded.db"))
    policy = _policy()
    storage.save_monitor_policy(policy)
    manifest, comparison = _snapshot(policy)
    storage.save_monitor_snapshot(policy.policy_id, manifest, comparison)
    monkeypatch.setattr(
        "verdict.storage.sqlite.monitor_snapshot_to_json",
        lambda *_args: "x" * (4_194_304 + 1),
    )

    with pytest.raises(ValueError, match="4 MiB"):
        storage.save_monitor_snapshot(policy.policy_id, manifest, comparison)

    assert storage.get_latest_monitor_snapshot(policy.policy_id) == (
        manifest, comparison,
    )
    storage.close()
