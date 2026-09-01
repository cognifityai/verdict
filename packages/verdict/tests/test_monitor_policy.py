from datetime import datetime, timedelta, timezone

import pytest
from verdict.monitoring import (
    AnalysisUnitRecord,
    MonitorPolicy,
    MonitorStatus,
    WindowMode,
    compare_manifest,
    monitor_policy_from_json,
    monitor_policy_to_json,
    monitor_snapshot_from_json,
    monitor_snapshot_to_json,
    plan_historical_manifest,
    plan_prospective_manifest,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _units(count: int, *, failures_from: int = 10_000, group: str = "known"):
    return tuple(
        AnalysisUnitRecord(
            f"u-{index:03d}", NOW + timedelta(days=index),
            {"failed": index >= failures_from}, group,
        )
        for index in range(count)
    )


def test_count_policy_freezes_event_time_ordered_80_20_membership() -> None:
    policy = MonitorPolicy("p", "scope", window_mode=WindowMode.COUNT,
                           reference_ratio=0.8, minimum_reference=10, minimum_current=10)

    manifest = plan_historical_manifest(reversed(_units(100)), policy, cutoff=NOW + timedelta(days=200))

    assert len(manifest.reference_unit_ids) == 80
    assert len(manifest.current_unit_ids) == 20
    assert manifest.reference_unit_ids[0] == "u-000"
    assert manifest.current_unit_ids[0] == "u-080"
    assert not set(manifest.reference_unit_ids) & set(manifest.current_unit_ids)
    assert manifest == plan_historical_manifest(_units(100), policy,
                                                 cutoff=NOW + timedelta(days=200))


def test_explicit_ranges_are_user_owned_and_non_overlapping() -> None:
    policy = MonitorPolicy(
        "p", "scope", window_mode=WindowMode.EXPLICIT,
        reference_start=NOW, reference_end=NOW + timedelta(days=40),
        current_start=NOW + timedelta(days=60), current_end=NOW + timedelta(days=80),
        minimum_reference=2, minimum_current=2,
    )

    manifest = plan_historical_manifest(_units(100), policy,
                                         cutoff=NOW + timedelta(days=90))

    assert manifest.reference_unit_ids == tuple(f"u-{i:03d}" for i in range(40))
    assert manifest.current_unit_ids == tuple(f"u-{i:03d}" for i in range(60, 80))


def test_invalid_ranges_are_rejected_and_cutoff_excludes_future_units() -> None:
    with pytest.raises(ValueError, match="non-overlapping"):
        MonitorPolicy(
            "p", "scope", window_mode=WindowMode.EXPLICIT,
            reference_start=NOW, reference_end=NOW + timedelta(days=40),
            current_start=NOW + timedelta(days=39), current_end=NOW + timedelta(days=80),
        )
    manifest = plan_historical_manifest(_units(3), MonitorPolicy("p", "s"), cutoff=NOW)
    assert manifest.consumed_unit_ids == ("u-000",)


def test_binary_comparison_detects_large_change_and_reports_adjusted_p() -> None:
    units = _units(80, failures_from=35)
    policy = MonitorPolicy("p", "scope", reference_ratio=0.5,
                           minimum_reference=20, minimum_current=20,
                           minimum_effect=0.2)
    manifest = plan_historical_manifest(units, policy, cutoff=NOW + timedelta(days=100))

    result = compare_manifest(units, manifest, policy)

    assert result.status is MonitorStatus.ALERT
    [metric] = result.metrics
    assert metric.metric == "failed"
    assert metric.reference_value == 0.125
    assert metric.current_value == 1.0
    assert metric.p_adjusted <= 0.05


def test_small_or_underpowered_cohort_is_not_called_no_drift() -> None:
    units = _units(12)
    policy = MonitorPolicy("p", "scope", reference_ratio=0.5,
                           minimum_reference=10, minimum_current=10)
    manifest = plan_historical_manifest(units, policy, cutoff=NOW + timedelta(days=20))

    result = compare_manifest(units, manifest, policy)

    assert result.status is MonitorStatus.INSUFFICIENT
    assert result.metrics == ()


def test_each_metric_uses_its_own_eligible_denominator() -> None:
    units = tuple(
        AnalysisUnitRecord(
            f"u-{index:03d}", NOW + timedelta(days=index),
            ({"failed": index >= 15, "refused": index >= 10}
             if index != 19 else {"failed": True}),
        )
        for index in range(20)
    )
    policy = MonitorPolicy("p", "scope", reference_ratio=0.5,
                           minimum_reference=5, minimum_current=5)
    manifest = plan_historical_manifest(units, policy, cutoff=NOW + timedelta(days=30))

    result = compare_manifest(units, manifest, policy)

    assert result.status is MonitorStatus.ALERT
    by_name = {metric.metric: metric for metric in result.metrics}
    assert by_name["failed"].reference_n == by_name["failed"].current_n == 10
    assert by_name["refused"].reference_n == 10
    assert by_name["refused"].current_n == 9
    assert by_name["refused"].current_value == 1.0
    assert by_name["refused"].p_value == pytest.approx(1 / 92378)


def test_metric_below_its_own_minimum_is_omitted_without_hiding_eligible_metrics() -> None:
    units = tuple(
        AnalysisUnitRecord(
            f"u-{index:03d}", NOW + timedelta(days=index),
            {"failed": False, **({"sparse": False} if index < 3 else {})},
        )
        for index in range(20)
    )
    policy = MonitorPolicy("p", "scope", reference_ratio=0.5,
                           minimum_reference=5, minimum_current=5)
    manifest = plan_historical_manifest(units, policy, cutoff=NOW + timedelta(days=30))

    result = compare_manifest(units, manifest, policy)

    assert result.status is MonitorStatus.NO_ALERT
    assert [metric.metric for metric in result.metrics] == ["failed"]


def test_unseen_group_share_suspends_comparison_as_reference_stale() -> None:
    baseline = _units(20)
    current = tuple(
        AnalysisUnitRecord(f"new-{i}", NOW + timedelta(days=20 + i), {"failed": False}, "new")
        for i in range(10)
    )
    units = baseline + current
    policy = MonitorPolicy("p", "scope", reference_ratio=2 / 3,
                           minimum_reference=10, minimum_current=5,
                           maximum_unseen_group_share=0.2)
    manifest = plan_historical_manifest(units, policy, cutoff=NOW + timedelta(days=40))

    result = compare_manifest(units, manifest, policy)

    assert result.status is MonitorStatus.REFERENCE_STALE
    assert result.unseen_group_share == 1.0
    assert result.metrics == ()


def test_prospective_cohorts_never_reuse_units_and_count_late_arrivals() -> None:
    policy = MonitorPolicy("p", "scope", reference_ratio=0.8,
                           minimum_reference=5, minimum_current=3,
                           prospective_target=3)
    bootstrap = plan_historical_manifest(_units(10), policy, cutoff=NOW + timedelta(days=10))
    new = (
        AnalysisUnitRecord("late", NOW + timedelta(days=5), {"failed": False}),
        *_units(4, failures_from=100),
    )
    new = tuple(
        unit if unit.unit_id == "late" else AnalysisUnitRecord(
            f"future-{i}", NOW + timedelta(days=11 + i), unit.metrics,
        )
        for i, unit in enumerate(new)
    )

    first = plan_prospective_manifest(bootstrap, new, policy)
    second = plan_prospective_manifest(first, new, policy)

    assert first.reference_unit_ids == bootstrap.reference_unit_ids
    assert first.current_unit_ids == ("late", "future-1", "future-2")
    assert first.late_unit_count == 1
    assert second.current_unit_ids == ("future-3", "future-4")
    assert second.prospective_open is True
    assert not set(first.current_unit_ids) & set(second.current_unit_ids)


def test_underfilled_prospective_bucket_accumulates_before_one_comparison() -> None:
    policy = MonitorPolicy("p", "scope", reference_ratio=0.8,
                           minimum_reference=5, minimum_current=5,
                           prospective_target=10)
    baseline = _units(10)
    bootstrap = plan_historical_manifest(
        baseline, policy, cutoff=NOW + timedelta(days=10),
    )
    first_rows = baseline + tuple(
        AnalysisUnitRecord(f"new-{index}", NOW + timedelta(days=11 + index),
                           {"failed": False})
        for index in range(3)
    )

    first = plan_prospective_manifest(bootstrap, first_rows, policy)
    first_result = compare_manifest(first_rows, first, policy)
    completed_rows = first_rows + tuple(
        AnalysisUnitRecord(f"new-{index}", NOW + timedelta(days=11 + index),
                           {"failed": False})
        for index in range(3, 10)
    )
    completed = plan_prospective_manifest(first, completed_rows, policy)
    completed_result = compare_manifest(completed_rows, completed, policy)

    assert first.current_unit_ids == ("new-0", "new-1", "new-2")
    assert first.prospective_open is True
    assert first_result.status is MonitorStatus.INSUFFICIENT
    assert completed.current_unit_ids == tuple(f"new-{index}" for index in range(10))
    assert completed.prospective_open is False
    assert completed_result.status is MonitorStatus.NO_ALERT

    unchanged = plan_prospective_manifest(first, first_rows, policy)
    assert unchanged == first


def test_units_sharing_the_event_time_frontier_are_not_discarded_as_late() -> None:
    policy = MonitorPolicy("p", "scope", reference_ratio=0.8,
                           minimum_reference=5, minimum_current=2,
                           prospective_target=2)
    baseline = _units(10)
    bootstrap = plan_historical_manifest(
        baseline, policy, cutoff=NOW + timedelta(days=10),
    )
    shared_time = NOW + timedelta(days=11)
    new = baseline + tuple(
        AnalysisUnitRecord(f"same-{index}", shared_time, {"failed": False})
        for index in range(4)
    )

    first = plan_prospective_manifest(bootstrap, new, policy)
    second = plan_prospective_manifest(first, new, policy)

    assert first.current_unit_ids == ("same-0", "same-1")
    assert second.current_unit_ids == ("same-2", "same-3")
    assert second.late_unit_count == 0


def test_policy_and_snapshot_canonical_round_trip() -> None:
    units = _units(20, failures_from=15)
    policy = MonitorPolicy("p", "scope", reference_ratio=0.5,
                           minimum_reference=5, minimum_current=5)
    manifest = plan_historical_manifest(units, policy, cutoff=NOW + timedelta(days=30))
    comparison = compare_manifest(units, manifest, policy)

    assert monitor_policy_from_json(monitor_policy_to_json(policy)) == policy
    assert monitor_snapshot_from_json(
        monitor_snapshot_to_json(manifest, comparison)
    ) == (manifest, comparison)


def test_trace_projection_is_ungrouped_by_default_and_grouping_is_explicit() -> None:
    from verdict.monitoring import trace_monitor_units
    from verdict.schema import Trace

    traces = [
        Trace(trace_id="a", started_at=NOW, provider="anthropic", request_model="a"),
        Trace(trace_id="b", started_at=NOW, provider="openai", request_model="b"),
    ]
    assert [unit.group_id for unit in trace_monitor_units(traces)] == [None, None]
    assert [unit.group_id for unit in trace_monitor_units(
        traces, grouping_mode="provider_model"
    )] == ["anthropic:a", "openai:b"]


def test_continuous_metric_cannot_be_silently_accepted_then_ignored() -> None:
    with pytest.raises(ValueError, match="must be boolean"):
        AnalysisUnitRecord("latency", NOW, {"latency_ms": 123.0})


def test_prospective_alpha_spending_is_summable_across_repeated_looks() -> None:
    policy = MonitorPolicy(
        "p", "scope", reference_ratio=0.5,
        minimum_reference=2, minimum_current=2, prospective_target=2,
    )
    rows = _units(4)
    manifest = plan_historical_manifest(rows, policy, cutoff=NOW + timedelta(days=4))
    spent = 0.0
    for look in range(1, 501):
        additions = tuple(
            AnalysisUnitRecord(
                f"look-{look}-{index}",
                NOW + timedelta(days=4 + look, seconds=index),
                {"failed": False},
            )
            for index in range(2)
        )
        rows += additions
        manifest = plan_prospective_manifest(manifest, rows, policy)
        comparison = compare_manifest(rows, manifest, policy)
        assert manifest.comparison_index == look
        spent += comparison.alpha_threshold
    assert spent < policy.p_threshold


def test_large_fisher_table_is_accurate_and_bounded() -> None:
    import time

    from verdict.monitoring import _fisher_two_sided

    started = time.perf_counter()
    result = _fisher_two_sided(8_000, 72_000, 1_500, 18_500)
    elapsed = time.perf_counter() - started

    assert result == pytest.approx(2.0463505882235967e-28, rel=1e-8)
    assert elapsed < 1.0


def test_repeated_null_monitoring_respects_the_campaign_false_alert_budget() -> None:
    import random

    generator = random.Random(20260831)
    campaigns_with_alert = 0
    campaigns = 200
    for campaign in range(campaigns):
        rows = tuple(
            AnalysisUnitRecord(
                f"baseline-{index}", NOW + timedelta(seconds=index),
                {"failed": generator.random() < 0.1},
            )
            for index in range(200)
        )
        policy = MonitorPolicy(
            f"null-{campaign}", "scope", reference_ratio=0.8,
            minimum_reference=30, minimum_current=30,
            prospective_target=50, minimum_effect=0,
        )
        manifest = plan_historical_manifest(
            rows, policy, cutoff=NOW + timedelta(seconds=200)
        )
        alerted = False
        for look in range(1, 21):
            additions = tuple(
                AnalysisUnitRecord(
                    f"current-{look}-{index}",
                    NOW + timedelta(seconds=201 + (look - 1) * 50 + index),
                    {"failed": generator.random() < 0.1},
                )
                for index in range(50)
            )
            rows += additions
            manifest = plan_prospective_manifest(manifest, rows, policy)
            alerted |= compare_manifest(rows, manifest, policy).status is MonitorStatus.ALERT
        campaigns_with_alert += alerted

    # Fixed-seed null calibration: 5/200 campaigns alert at least once across
    # 20 looks. The 8% guard is deliberately looser than the observed 2.5% and
    # catches removal of the repeated-look correction without overfitting it.
    assert campaigns_with_alert / campaigns <= 0.08
