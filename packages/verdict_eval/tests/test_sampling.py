"""Stratified judge sampler — coverage, capping, top-up, determinism."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from verdict_eval.sampling import (
    StratifiedJudgeSampler,
    WindowSpec,
    uniform_coverage_estimate,
)


@dataclass
class FakeTrace:
    trace_id: str
    cluster_id: str | None
    started_at: datetime


NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
# lag (48h) > current (24h) so there is a real gap between the two windows
# (between NOW-48h and NOW-24h) where a trace belongs to neither.
WINDOW = WindowSpec(now=NOW, current_hours=24, baseline_days=7, baseline_lag_hours=48)


def _current_ts(h: float = 1) -> datetime:
    return NOW - timedelta(hours=h)        # inside current window (< 24h old)


def _baseline_ts(d: float = 3) -> datetime:
    return NOW - timedelta(hours=48) - timedelta(days=d)   # inside baseline (> 48h old)


def _make(cluster: str, n: int, ts: datetime, prefix: str) -> list[FakeTrace]:
    return [FakeTrace(f"{prefix}-{cluster}-{i}", cluster, ts) for i in range(n)]


def test_window_label_boundaries():
    assert WINDOW.label(_current_ts(1)) == "current"
    assert WINDOW.label(_baseline_ts(3)) == "baseline"
    # In the lag gap between windows → None
    assert WINDOW.label(NOW - timedelta(hours=36)) is None
    # Far past → None
    assert WINDOW.label(NOW - timedelta(days=30)) is None
    assert WINDOW.label(NOW + timedelta(seconds=1)) is None


def test_window_rejects_naive_trace_timestamp():
    with pytest.raises(ValueError, match="timezone"):
        WINDOW.label(datetime(2026, 6, 29, 11))


def test_high_volume_cluster_is_capped_at_target():
    # 500 current traces in one cluster, target 40 → judge exactly 40, no more.
    traces = _make("c1", 500, _current_ts(), "cur")
    plan = StratifiedJudgeSampler(target_per_cell=40).plan(traces, window=WINDOW)
    assert plan.total_to_judge == 40
    cell = next(c for c in plan.cells if c.window == "current")
    assert cell.to_judge == 40 and not cell.under_covered


def test_low_volume_cluster_is_undercovered_not_silent():
    # Only 12 traces but target 40 → judge all 12, mark under-covered.
    traces = _make("c2", 12, _current_ts(), "cur")
    plan = StratifiedJudgeSampler(target_per_cell=40).plan(traces, window=WINDOW)
    assert plan.total_to_judge == 12
    assert len(plan.under_covered_cells) == 1
    assert plan.under_covered_cells[0].reached == 12


def test_each_cluster_reaches_target_where_traffic_allows():
    # Two clusters, both with plenty of traffic in both windows.
    traces = (
        _make("a", 100, _current_ts(), "cur") + _make("a", 100, _baseline_ts(), "base")
        + _make("b", 100, _current_ts(), "cur") + _make("b", 100, _baseline_ts(), "base")
    )
    plan = StratifiedJudgeSampler(target_per_cell=30).plan(traces, window=WINDOW)
    # 2 clusters x 2 windows = 4 cells, each judged to 30.
    assert len(plan.cells) == 4
    assert all(c.to_judge == 30 for c in plan.cells)
    assert plan.total_to_judge == 120
    assert not plan.under_covered_cells


def test_topup_does_not_rejudge_existing():
    traces = _make("c", 100, _current_ts(), "cur")
    # 25 already judged → should top up by 15 to reach 40, not judge 40 more.
    already = {f"cur-c-{i}" for i in range(25)}
    plan = StratifiedJudgeSampler(target_per_cell=40).plan(
        traces, window=WINDOW, already_judged_trace_ids=already)
    cell = plan.cells[0]
    assert cell.existing == 25
    assert cell.to_judge == 15
    assert cell.reached == 40
    # None of the newly selected ids are already-judged.
    assert not (set(plan.selected_trace_ids) & already)


def test_already_covered_cell_judges_nothing():
    traces = _make("c", 100, _current_ts(), "cur")
    already = {f"cur-c-{i}" for i in range(40)}
    plan = StratifiedJudgeSampler(target_per_cell=40).plan(
        traces, window=WINDOW, already_judged_trace_ids=already)
    assert plan.total_to_judge == 0


def test_traces_outside_windows_excluded():
    traces = _make("c", 50, NOW - timedelta(hours=36), "gap")  # in the lag gap
    plan = StratifiedJudgeSampler(target_per_cell=40).plan(traces, window=WINDOW)
    assert plan.total_to_judge == 0
    assert plan.cells == []


def test_unclustered_traces_excluded():
    traces = [FakeTrace(f"x-{i}", None, _current_ts()) for i in range(50)]
    plan = StratifiedJudgeSampler(target_per_cell=40).plan(traces, window=WINDOW)
    assert plan.total_to_judge == 0


def test_deterministic_with_seed():
    traces = _make("c", 200, _current_ts(), "cur")
    p1 = StratifiedJudgeSampler(target_per_cell=40, seed=5).plan(traces, window=WINDOW)
    p2 = StratifiedJudgeSampler(target_per_cell=40, seed=5).plan(traces, window=WINDOW)
    assert p1.selected_trace_ids == p2.selected_trace_ids


def test_uniform_contrast_shows_starvation():
    # One big cluster (1000) + one small (20). At rate 0.1: big reaches floor
    # (100 >= 30), small does not (2 < 30). Stratified would cover both.
    traces = _make("big", 1000, _current_ts(), "cur") + _make("small", 20, _current_ts(), "cur")
    est = uniform_coverage_estimate(traces, window=WINDOW, sample_rate=0.1, min_sample_size=30)
    assert est["cells_total"] == 2
    assert est["cells_reaching_floor"] == 1   # only the big cluster
    strat = StratifiedJudgeSampler(target_per_cell=30).plan(traces, window=WINDOW)
    # Stratified reaches the floor for big and judges all 20 of small (capped by
    # availability), spending far less than uniform on the big cluster.
    big_cell = next(c for c in strat.cells if c.cluster_id == "big")
    assert big_cell.to_judge == 30   # not 100
