"""Tests for the permutation-test-based SemanticDriftDetector.

The v2 detector gates on BOTH a permutation p-value (significance) and a
centroid-distance effect-size floor. These tests pin that behavior:
  - same distribution -> no drift (shift within natural variation)
  - genuinely different distribution -> drift, low p, effect over floor
  - significant-but-tiny shift -> blocked by the effect floor
  - same seed -> same p-value (deterministic)
"""

from __future__ import annotations

import numpy as np
from verdict_eval.semantic_drift import (
    SemanticDriftDetector,
    _cosine_distance,
    _l2_normalize_rows,
    _permutation_p_value,
    _psi_1d,
)


class _ArrayEmbedder:
    """Returns pre-built embedding arrays in call order. detect() calls
    embed(current) then embed(baseline), so pass [cur_array, base_array]."""

    def __init__(self, arrays: list[np.ndarray]) -> None:
        self._arrays = list(arrays)
        self._i = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        a = self._arrays[self._i]
        self._i += 1
        return a


def _cluster(axis: int, n: int, dim: int, noise: float, seed: int,
             weight: float = 1.0) -> np.ndarray:
    """n points pointing mostly along `axis`, with gaussian noise."""
    rng = np.random.default_rng(seed)
    base = np.zeros((n, dim), dtype=np.float64)
    base[:, axis] = weight
    return base + rng.normal(0.0, noise, size=(n, dim))


def _detector(cur: np.ndarray, base: np.ndarray, **kw) -> SemanticDriftDetector:
    defaults = dict(
        embedder=_ArrayEmbedder([cur, base]),
        centroid_distance_threshold=0.10,
        significance_level=0.05,
        n_permutations=200,
        random_seed=0,
        min_sample_size=20,
    )
    defaults.update(kw)
    return SemanticDriftDetector(**defaults)


def test_no_drift_same_distribution() -> None:
    cur = _cluster(axis=0, n=40, dim=8, noise=0.05, seed=1)
    base = _cluster(axis=0, n=40, dim=8, noise=0.05, seed=2)
    det = _detector(cur, base)
    sig = det.detect(cluster_id="x",
                     current_responses=["a"] * 40,
                     baseline_responses=["b"] * 40)
    assert sig is None, "identical distributions should not trigger drift"


def test_drift_on_genuinely_different_distribution() -> None:
    cur = _cluster(axis=1, n=40, dim=8, noise=0.05, seed=1)   # points along axis 1
    base = _cluster(axis=0, n=40, dim=8, noise=0.05, seed=2)  # points along axis 0
    det = _detector(cur, base)
    sig = det.detect(cluster_id="x",
                     current_responses=["a"] * 40,
                     baseline_responses=["b"] * 40)
    assert sig is not None, "orthogonal distributions should trigger drift"
    assert sig.p_value < 0.05
    assert sig.centroid_distance >= 0.10
    assert sig.n_permutations == 200


def test_significant_but_tiny_effect_blocked_by_floor() -> None:
    """A small but consistent angular shift can be statistically significant,
    but should NOT trigger because it's below the effect-size floor."""
    base = _cluster(axis=0, n=60, dim=8, noise=0.02, seed=2)
    cur = base.copy()
    cur[:, 1] += 0.25     # small consistent push toward axis 1 -> cosine dist ~0.03
    det = _detector(cur, base, centroid_distance_threshold=0.10)
    observed = _cosine_distance(cur.mean(axis=0), base.mean(axis=0))
    assert observed < 0.10, f"setup sanity: effect should be small, got {observed}"
    sig = det.detect(cluster_id="x",
                     current_responses=["a"] * 60,
                     baseline_responses=["b"] * 60)
    assert sig is None, "tiny effect should be blocked by the effect-size floor"


def test_permutation_p_value_is_deterministic() -> None:
    cur = _cluster(axis=1, n=30, dim=8, noise=0.05, seed=1)
    base = _cluster(axis=0, n=30, dim=8, noise=0.05, seed=2)
    observed = _cosine_distance(cur.mean(axis=0), base.mean(axis=0))
    p1 = _permutation_p_value(cur, base, observed=observed, n_permutations=200,
                              rng=np.random.default_rng(0))
    p2 = _permutation_p_value(cur, base, observed=observed, n_permutations=200,
                              rng=np.random.default_rng(0))
    assert p1 == p2
    assert 0.0 < p1 <= 1.0


def test_below_min_sample_size_returns_none() -> None:
    cur = _cluster(axis=1, n=5, dim=8, noise=0.05, seed=1)
    base = _cluster(axis=0, n=5, dim=8, noise=0.05, seed=2)
    det = _detector(cur, base, min_sample_size=20)
    sig = det.detect(cluster_id="x",
                     current_responses=["a"] * 5,
                     baseline_responses=["b"] * 5)
    assert sig is None


def test_psi_1d_is_non_negative() -> None:
    """PSI is a magnitude — it must never be negative regardless of which way
    the distribution shifted (the old signed return could go negative)."""
    rng = np.random.default_rng(0)
    base = rng.normal(0.0, 1.0, 500)
    # Shift in either direction; both must give PSI >= 0.
    shifted_up = rng.normal(1.5, 1.0, 500)
    shifted_down = rng.normal(-1.5, 1.0, 500)
    assert _psi_1d(shifted_up, base) >= 0.0
    assert _psi_1d(shifted_down, base) >= 0.0
    assert _psi_1d(base, base) >= 0.0


def test_l2_normalize_rows_unit_length_and_zero_guard() -> None:
    x = np.array([[3.0, 4.0], [0.0, 0.0], [1.0, 0.0]])
    out = _l2_normalize_rows(x)
    assert abs(np.linalg.norm(out[0]) - 1.0) < 1e-9
    assert np.allclose(out[1], 0.0)          # zero row guarded, stays zero
    assert abs(np.linalg.norm(out[2]) - 1.0) < 1e-9


def test_normalization_preserves_same_distribution_contract() -> None:
    """Pre-normalized same-distribution arrays must still yield no drift after
    the in-detector normalization (idempotent on unit vectors)."""
    cur = _l2_normalize_rows(_cluster(axis=0, n=40, dim=8, noise=0.05, seed=1))
    base = _l2_normalize_rows(_cluster(axis=0, n=40, dim=8, noise=0.05, seed=2))
    det = _detector(cur, base)
    sig = det.detect(cluster_id="x",
                     current_responses=["a"] * 40,
                     baseline_responses=["b"] * 40)
    assert sig is None
