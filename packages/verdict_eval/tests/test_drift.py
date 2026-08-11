"""Drift detector tests — updated for non-parametric methodology.

Most important: stationary signal → no alerts (false-positive control);
clear regression → alert with Cliff's δ magnitude and Wasserstein > 0.
Also covers Benjamini-Hochberg, Cliff's δ, Wasserstein, PSI in isolation.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from verdict.schema import DriftDirection
from verdict_eval.drift import (
    DriftDetector,
    DriftWindow,
    _benjamini_hochberg,
    _cliffs_delta,
    _psi,
    _wasserstein,
    split_windows_by_time,
)


def _binom_scores(n: int, p: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [1.0 if rng.random() < p else 0.0 for _ in range(n)]


def test_stationary_signal_does_not_alert():
    """Same mean in current and baseline → no drift signal."""
    detector = DriftDetector(min_sample_size=30, p_threshold=0.01, effect_size_threshold=0.2)
    current = [DriftWindow("c1", "groundedness", _binom_scores(120, 0.8, seed=1))]
    baseline = [DriftWindow("c1", "groundedness", _binom_scores(900, 0.8, seed=2))]
    signals = detector.detect(current=current, baseline=baseline)
    assert signals == []


def test_real_regression_is_detected():
    """Baseline 85% pass-rate, current drops to 50% → must alert."""
    detector = DriftDetector(min_sample_size=30, p_threshold=0.01, effect_size_threshold=0.147)
    current = [DriftWindow("c1", "groundedness", _binom_scores(150, 0.5, seed=10))]
    baseline = [DriftWindow("c1", "groundedness", _binom_scores(1500, 0.85, seed=20))]
    signals = detector.detect(current=current, baseline=baseline)
    assert len(signals) == 1
    s = signals[0]
    assert s.direction == DriftDirection.REGRESSION
    assert s.effect_size_cliffs_delta < 0   # current < baseline
    assert s.effect_size_cohens_d < 0       # legacy d still reported
    assert s.wasserstein_distance > 0       # distributional shift confirmed
    assert s.p_value_adjusted < 0.01


def test_regression_signal_includes_current_failure_trace_ids():
    detector = DriftDetector(min_sample_size=30, p_threshold=0.01)
    baseline = [DriftWindow(
        "c1", "groundedness", [1.0] * 40,
        trace_ids=[f"base-{i}" for i in range(40)],
    )]
    current = [DriftWindow(
        "c1", "groundedness", [0.0] * 40,
        trace_ids=[f"current-{i}" for i in range(40)],
    )]

    signals = detector.detect(current=current, baseline=baseline)

    assert len(signals) == 1
    assert signals[0].example_trace_ids == [f"current-{i}" for i in range(5)]


def test_improvement_is_marked_improvement():
    detector = DriftDetector(min_sample_size=30, p_threshold=0.01, effect_size_threshold=0.2)
    current = [DriftWindow("c1", "relevance", _binom_scores(200, 0.9, seed=33))]
    baseline = [DriftWindow("c1", "relevance", _binom_scores(2000, 0.55, seed=44))]
    signals = detector.detect(current=current, baseline=baseline)
    assert len(signals) == 1
    assert signals[0].direction == DriftDirection.IMPROVEMENT


def test_too_few_samples_reports_why_detection_was_skipped(caplog):
    detector = DriftDetector(min_sample_size=30, p_threshold=0.01)
    current = [DriftWindow("c1", "groundedness", _binom_scores(5, 0.3, seed=1))]
    baseline = [DriftWindow("c1", "groundedness", _binom_scores(5, 0.9, seed=2))]
    assert detector.detect(current=current, baseline=baseline) == []
    assert detector.last_diagnostics
    assert "minimum 30" in detector.last_diagnostics[0]
    assert "minimum 30" in caplog.text


def test_unmatched_cluster_or_dimension_skipped():
    detector = DriftDetector(min_sample_size=30, p_threshold=0.01)
    current = [DriftWindow("c1", "groundedness", _binom_scores(100, 0.4, seed=1))]
    baseline = [DriftWindow("c2", "groundedness", _binom_scores(100, 0.9, seed=2))]
    assert detector.detect(current=current, baseline=baseline) == []


def test_tiny_effect_size_skipped_even_if_significant():
    """With huge samples, a tiny mean difference is statistically significant
    but practically meaningless. Cliff's δ threshold must filter it out."""
    detector = DriftDetector(min_sample_size=30, p_threshold=0.01, effect_size_threshold=0.474)
    # Means differ by ~0.02 → tiny Cliff's δ but n=10000 gives small p
    current = [DriftWindow("c1", "groundedness", _binom_scores(10_000, 0.80, seed=1))]
    baseline = [DriftWindow("c1", "groundedness", _binom_scores(10_000, 0.82, seed=2))]
    signals = detector.detect(current=current, baseline=baseline)
    assert signals == []


def test_cliffs_delta_basics():
    """Cliff's δ ∈ [-1, 1]; +1 when a > b always; -1 when a < b always; 0 when same."""
    a = np.array([1.0, 1.0, 1.0])
    b = np.array([0.0, 0.0, 0.0])
    assert _cliffs_delta(a, b) == 1.0
    assert _cliffs_delta(b, a) == -1.0
    # Equal distributions → roughly 0
    same = np.array([0.5, 0.5, 0.5])
    assert abs(_cliffs_delta(same, same)) < 1e-9


def test_cliffs_delta_partial_overlap():
    """Half PASS half FAIL vs all PASS → strongly negative."""
    a = np.array([0.0] * 5 + [1.0] * 5)  # 50% pass
    b = np.array([1.0] * 10)               # 100% pass
    d = _cliffs_delta(a, b)
    # P(a > b) = 0, P(a < b) = 0.5 (half of a's zeros < b's ones)
    assert d < -0.4
    assert d > -0.6


def test_wasserstein_nonneg_and_grows_with_drift():
    """Wasserstein distance is non-negative and grows as distributions diverge."""
    a = np.array([0.0] * 10 + [1.0] * 90)        # 90% pass
    b_close = np.array([0.0] * 15 + [1.0] * 85)  # 85% pass
    b_far = np.array([0.0] * 60 + [1.0] * 40)    # 40% pass
    w_close = _wasserstein(a, b_close)
    w_far = _wasserstein(a, b_far)
    assert w_close >= 0
    assert w_far >= w_close


def test_psi_thresholds():
    """PSI < 0.1 for similar; > 0.25 for significant shift."""
    same = np.random.RandomState(0).normal(0, 1, 1000)
    similar = np.random.RandomState(1).normal(0, 1, 1000)
    shifted = np.random.RandomState(2).normal(1.5, 1, 1000)
    assert _psi(similar, same) < 0.1
    assert _psi(shifted, same) > 0.25


def test_benjamini_hochberg_basic():
    # All p-values 1.0 → adjusted should also be 1.0
    assert all(p == 1.0 for p in _benjamini_hochberg([1.0, 1.0, 1.0]))
    # A single tiny p with many large ones: BH adjusts upward
    adj = _benjamini_hochberg([0.001, 0.5, 0.6, 0.7])
    assert adj[0] < 0.01
    assert adj[1] > 0.4


def test_benjamini_hochberg_preserves_order():
    p = [0.04, 0.001, 0.5]
    adj = _benjamini_hochberg(p)
    # Returned in same order as input
    assert len(adj) == 3
    assert adj[1] < adj[0]                # tiny p stays tiny


# --------------------------------------------------------------------------- #
# Unclear-rate drift — a dimension going UNCLEAR is a silent regression
# --------------------------------------------------------------------------- #

def test_unclear_rate_increase_emits_signal():
    """Pass-rate unchanged but unclear fraction jumps 0.05 → 0.40 → must emit
    an unclear_rate_increase signal even though the PASS/FAIL test sees no
    drift."""
    detector = DriftDetector(min_sample_size=30, p_threshold=0.01, effect_size_threshold=0.147)
    # Baseline: 95 scored (80% pass) + 5 unclear → unclear frac 0.05
    base_scores = _binom_scores(95, 0.8, seed=1)
    baseline = [DriftWindow("c1", "groundedness", base_scores, n_unclear=5)]
    # Current: 60 scored (same 80% pass) + 40 unclear → unclear frac 0.40
    cur_scores = _binom_scores(60, 0.8, seed=2)
    current = [DriftWindow("c1", "groundedness", cur_scores, n_unclear=40)]

    signals = detector.detect(current=current, baseline=baseline)
    unclear = [s for s in signals if s.statistic_name == "unclear_rate_increase"]
    assert len(unclear) == 1
    s = unclear[0]
    assert s.direction == DriftDirection.REGRESSION
    assert s.cluster_id == "c1"
    assert s.dimension == "groundedness"
    assert s.statistic_value > 0.3            # current unclear fraction


def test_stable_unclear_fraction_does_not_emit():
    """A stable unclear fraction (5% → 5%) must NOT emit an unclear signal."""
    detector = DriftDetector(min_sample_size=30, p_threshold=0.01, effect_size_threshold=0.147)
    baseline = [DriftWindow("c1", "groundedness", _binom_scores(95, 0.8, seed=1), n_unclear=5)]
    current = [DriftWindow("c1", "groundedness", _binom_scores(95, 0.8, seed=2), n_unclear=5)]
    signals = detector.detect(current=current, baseline=baseline)
    assert not any(s.statistic_name == "unclear_rate_increase" for s in signals)


def test_unclear_drift_detected_when_scored_window_too_small():
    """If a dimension goes mostly UNCLEAR the scored window shrinks below
    min_sample_size and the PASS/FAIL test can't run — but the unclear-rate
    signal must still fire on the TOTAL (scored + unclear) window size."""
    detector = DriftDetector(min_sample_size=30, p_threshold=0.01, effect_size_threshold=0.147)
    baseline = [DriftWindow("c1", "groundedness", _binom_scores(95, 0.8, seed=1), n_unclear=5)]
    # Only 5 scored (below min_sample_size) but 95 unclear → total 100, frac 0.95
    current = [DriftWindow("c1", "groundedness", _binom_scores(5, 0.8, seed=2), n_unclear=95)]
    signals = detector.detect(current=current, baseline=baseline)
    unclear = [s for s in signals if s.statistic_name == "unclear_rate_increase"]
    assert len(unclear) == 1
    assert unclear[0].sample_size_current == 100


# --------------------------------------------------------------------------- #
# Benjamini-Hochberg must run PER test type (exchangeability)
# --------------------------------------------------------------------------- #

def test_bh_applied_per_test_type():
    """When the detector mixes binary (Fisher) and continuous (Mann-Whitney)
    windows, BH must be run separately within each test family, so each
    window's adjusted p matches running BH on its own subgroup — not on the
    pooled family."""
    detector = DriftDetector(min_sample_size=30, p_threshold=0.99, effect_size_threshold=0.0)

    # Two binary windows (Fisher's exact) with clear regressions.
    bin_cur_1 = DriftWindow("cb1", "d", _binom_scores(200, 0.5, seed=11))
    bin_base_1 = DriftWindow("cb1", "d", _binom_scores(200, 0.85, seed=12))
    bin_cur_2 = DriftWindow("cb2", "d", _binom_scores(200, 0.55, seed=13))
    bin_base_2 = DriftWindow("cb2", "d", _binom_scores(200, 0.80, seed=14))

    # Two continuous windows (Mann-Whitney U).
    rng = np.random.RandomState(7)
    cont_cur_1 = DriftWindow("cc1", "d", list(rng.normal(0.3, 1.0, 200)))
    cont_base_1 = DriftWindow("cc1", "d", list(rng.normal(0.9, 1.0, 200)))
    cont_cur_2 = DriftWindow("cc2", "d", list(rng.normal(0.4, 1.0, 200)))
    cont_base_2 = DriftWindow("cc2", "d", list(rng.normal(0.85, 1.0, 200)))

    current = [bin_cur_1, bin_cur_2, cont_cur_1, cont_cur_2]
    baseline = [bin_base_1, bin_base_2, cont_base_1, cont_base_2]

    signals = detector.detect(current=current, baseline=baseline)
    by_key = {(s.cluster_id, s.statistic_name): s for s in signals}

    # Collect raw p-values grouped by test type from the emitted signals.
    fisher = {k: s for k, s in by_key.items() if s.statistic_name == "fisher_exact"}
    mw = {k: s for k, s in by_key.items() if s.statistic_name == "mann_whitney_u"}
    assert len(fisher) == 2
    assert len(mw) == 2

    # Recompute BH within each subgroup and confirm the adjusted p-values match.
    fisher_raw = sorted(s.p_value for s in fisher.values())
    fisher_adj_direct = _benjamini_hochberg(fisher_raw)
    mw_raw = sorted(s.p_value for s in mw.values())
    mw_adj_direct = _benjamini_hochberg(mw_raw)

    fisher_adj_emitted = sorted(s.p_value_adjusted for s in fisher.values())
    mw_adj_emitted = sorted(s.p_value_adjusted for s in mw.values())

    assert np.allclose(fisher_adj_emitted, fisher_adj_direct, atol=1e-9)
    assert np.allclose(mw_adj_emitted, mw_adj_direct, atol=1e-9)


def test_build_windows_populates_n_unclear():
    """build_windows_from_judgments must populate n_unclear so the unclear-rate
    detector can see UNCLEAR judgments."""
    from datetime import datetime, timezone

    from verdict.schema import DimensionScore, Judgment, Verdict
    from verdict_eval.drift import build_windows_from_judgments

    def _judgment(trace_id, verdict):
        return Judgment(
            trace_id=trace_id,
            dimensions=[DimensionScore(name="groundedness", verdict=verdict)],
            created_at=datetime.now(timezone.utc),
        )

    judgments = (
        [_judgment(f"t{i}", Verdict.PASS) for i in range(6)]
        + [_judgment(f"u{i}", Verdict.UNCLEAR) for i in range(4)]
    )
    cluster_map = {j.trace_id: "c1" for j in judgments}
    windows = build_windows_from_judgments(judgments, cluster_map)
    assert len(windows) == 1
    w = windows[0]
    assert w.n == 6
    assert w.n_unclear == 4
    assert abs(w.unclear_fraction - 0.4) < 1e-9
    assert w.trace_ids == [f"t{i}" for i in range(6)]
    assert w.unclear_trace_ids == [f"u{i}" for i in range(4)]


def test_split_windows_uses_trace_time_not_judgment_creation_time():
    """A historical trace judged today must remain in the baseline window."""
    from verdict.schema import DimensionScore, Judgment, Verdict

    now = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    judgments = [
        Judgment(
            trace_id="baseline-trace",
            created_at=now,
            dimensions=[DimensionScore(name="relevance", verdict=Verdict.PASS)],
        ),
        Judgment(
            trace_id="current-trace",
            created_at=now,
            dimensions=[DimensionScore(name="relevance", verdict=Verdict.FAIL)],
        ),
    ]
    clusters = {"baseline-trace": "c1", "current-trace": "c1"}
    trace_times = {
        "baseline-trace": now - timedelta(days=3),
        "current-trace": now - timedelta(hours=2),
    }

    current, baseline = split_windows_by_time(
        judgments,
        clusters,
        trace_times,
        current_hours=24,
        baseline_days=7,
        baseline_lag_hours=24,
        now=now,
    )

    assert current[0].trace_ids == ["current-trace"]
    assert baseline[0].trace_ids == ["baseline-trace"]


def test_split_windows_rejects_judgment_without_trace_time():
    from verdict.schema import Judgment

    judgment = Judgment(trace_id="missing")
    try:
        split_windows_by_time([judgment], {"missing": "c1"}, {})
    except ValueError as exc:
        assert "Trace.started_at" in str(exc)
    else:
        raise AssertionError("missing trace event time should fail loudly")


def test_split_windows_rejects_naive_trace_time():
    from verdict.schema import Judgment

    judgment = Judgment(trace_id="naive")
    with pytest.raises(ValueError, match="timezone"):
        split_windows_by_time(
            [judgment],
            {"naive": "c1"},
            {"naive": datetime(2026, 8, 11, 12)},
            now=datetime(2026, 8, 11, 13, tzinfo=timezone.utc),
        )


def test_split_windows_excludes_future_trace():
    from verdict.schema import DimensionScore, Judgment, Verdict

    now = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    judgment = Judgment(
        trace_id="future",
        dimensions=[DimensionScore(name="relevance", verdict=Verdict.PASS)],
    )

    current, baseline = split_windows_by_time(
        [judgment],
        {"future": "c1"},
        {"future": now + timedelta(minutes=1)},
        now=now,
    )

    assert current == []
    assert baseline == []
