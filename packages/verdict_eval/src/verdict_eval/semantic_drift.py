"""Semantic drift detector — embedding-based distribution monitoring.

This is the embedding-based layer of the eval pipeline. Where the judge-based
drift detector in `drift.py`
operates on PASS/FAIL scores (catching quality regressions the judge can detect),
the semantic drift detector operates on **response embeddings** (catching
distributional shifts in what the model is actually producing, even when the
judge doesn't flag them).

Two complementary signals from the same observation:

- Judge-based drift catches "the model is producing lower-quality responses"
- Semantic drift catches "the model is producing structurally different
  responses" — different topics, different lengths, different formats — even
  if the judge can't articulate why it's bad.

Methodology:

1. Embed current and baseline responses via the configured embedder.
2. L2-normalize each embedding row.
3. Compute centroid cosine distance, a two-sample permutation p-value, and
   diagnostic per-axis Wasserstein and quantile-binned PSI values.
4. Mark the comparison triggered only when both the significance and centroid
   effect-size gates pass. ``analyze()`` retains all computed comparisons;
   ``detect()`` preserves the signal-only public contract.

References:
- "Drift detection in NLP using semantic embeddings" — Feldhans et al. 2021
- Evidently AI's `EmbeddingsDriftMetric` for the implementation pattern
- Chroma Research's "context rot" detection methodology
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import wasserstein_distance


@dataclass
class SemanticDriftSignal:
    """Statistics from one semantic-drift comparison.

    ``detect()`` returns this object only for a triggered comparison, preserving
    the original public contract. ``analyze()`` returns it for every comparison
    with enough data so reporting callers can show the exact same statistics
    without embedding or calculating them a second time.
    """

    cluster_id: str = ""
    direction: str = "change"     # "change", "shift" — semantic drift doesn't have an inherent good/bad direction
    centroid_distance: float = 0.0          # cosine distance between current and baseline centroids (the EFFECT SIZE)
    p_value: float = 1.0                    # permutation-test p-value (the SIGNIFICANCE)
    n_permutations: int = 0                 # how many permutations backed the p-value
    mean_wasserstein: float = 0.0           # mean Wasserstein-1 across embedding dimensions (diagnostic)
    max_wasserstein: float = 0.0            # max Wasserstein-1 across embedding dimensions (diagnostic)
    cluster_psi: float = 0.0                # PSI on dominant-variance axis (diagnostic)
    sample_size_current: int = 0
    sample_size_baseline: int = 0
    contributing_layer: str = "semantic_embedding"
    recommended_action: str = ""
    # Appended to preserve the positional order of the published signal fields.
    triggered: bool = True


@dataclass
class SemanticDriftDetector:
    """Embedding-based drift detector. Operates on response embeddings, NOT
    on judge scores. Complementary signal to the judge-based DriftDetector.

    **Methodology (v2).** The old detector triggered whenever an absolute
    centroid distance / Wasserstein / PSI crossed a hand-picked threshold.
    That fires on *any* topic shift, because a fixed cutoff can't know how
    much variation is natural in the data. We now use the same pattern as
    the judge-based drift detector — a significance gate AND an effect-size
    gate:

      - **Significance** — a two-sample permutation test. Pool the current
        and baseline embeddings, repeatedly reshuffle them into two groups of
        the original sizes, and measure how often a random regrouping
        produces a centroid distance at least as large as the observed one.
        A small p-value means the observed shift is bigger than the data's
        natural within-sample variation. This self-calibrates — no magic
        threshold.
      - **Effect size** — the centroid cosine distance must also exceed a
        small floor, so that in large samples a statistically-significant but
        trivially-small shift doesn't raise an alarm.

    Drift is reported only when BOTH gates pass. Wasserstein and PSI are kept
    as reported diagnostics, not as independent triggers (single-axis PSI in
    particular was noisy).

    **Important interpretation.** Semantic drift is *informational*, not an
    alarm on its own — it says the response distribution moved, not that
    quality dropped. A topic change produces semantic drift with no quality
    regression. To decide whether a shift matters, cross-reference it with the
    judge-based and structural signals (a higher layer can gate on
    co-occurrence). The `recommended_action` says this explicitly.

    Parameters
    ----------
    embedder:
        Anything implementing `.embed(texts: list[str]) -> np.ndarray`.
    centroid_distance_threshold:
        Effect-size floor — the centroid cosine distance must exceed this for
        a shift to be reported even if it's statistically significant.
        Default 0.10.
    significance_level:
        Permutation-test p-value cutoff. Default 0.05.
    n_permutations:
        Number of permutations backing the test. Default 200.
    random_seed:
        Seed for the permutation RNG, for reproducible p-values.
    wasserstein_threshold:
        Retained for backward compatibility; no longer gates the trigger.
    min_sample_size:
        Minimum responses in each of current/baseline before testing.
    """

    embedder: object
    centroid_distance_threshold: float = 0.10
    significance_level: float = 0.05
    n_permutations: int = 200
    random_seed: int = 0
    wasserstein_threshold: float = 0.05      # deprecated as a trigger; diagnostic only
    min_sample_size: int = 30

    def detect(
        self,
        *,
        cluster_id: str,
        current_responses: list[str],
        baseline_responses: list[str],
    ) -> SemanticDriftSignal | None:
        """Return a SemanticDriftSignal when the current window's response
        distribution differs from baseline by more than natural variation
        (permutation p < significance_level) AND by a meaningful amount
        (centroid distance >= effect-size floor). Otherwise None."""
        analysis = self.analyze(
            cluster_id=cluster_id,
            current_responses=current_responses,
            baseline_responses=baseline_responses,
        )
        return analysis if analysis is not None and analysis.triggered else None

    def analyze(
        self,
        *,
        cluster_id: str,
        current_responses: list[str],
        baseline_responses: list[str],
    ) -> SemanticDriftSignal | None:
        """Return normalized statistics for every comparison with enough data.

        Unlike :meth:`detect`, this method retains non-triggered comparisons and
        marks them with ``triggered=False``. Embedding, normalization, effect
        size, significance, and diagnostics are computed exactly once here.
        """
        if len(current_responses) < self.min_sample_size:
            return None
        if len(baseline_responses) < self.min_sample_size:
            return None

        # Embed both sets
        cur_emb = self.embedder.embed(current_responses)
        base_emb = self.embedder.embed(baseline_responses)
        if cur_emb.size == 0 or base_emb.size == 0:
            return None
        if cur_emb.shape[0] < 2 or base_emb.shape[0] < 2:
            return None

        # L2-normalize every embedding row (guarding zero-norm rows) BEFORE
        # computing centroids, the permutation test, Wasserstein, and PSI.
        # The centroid statistic is a cosine distance, which is meant to be
        # scale-invariant; but a centroid of *un*-normalized vectors is
        # norm-confounded — a row with a larger magnitude pulls the centroid
        # direction more than a smaller one, so the same angular distribution
        # can yield different centroids depending on per-row norms. Normalizing
        # up front makes the statistic purely directional, scale-invariant, and
        # reproducible. NOTE: this changes the numeric outputs slightly versus
        # the old un-normalized path, so the distance / p-value thresholds may
        # need re-validation. If a caller already passes pre-normalized rows,
        # this is a no-op and the existing contracts still hold.
        cur_emb = _l2_normalize_rows(cur_emb)
        base_emb = _l2_normalize_rows(base_emb)

        # Effect size: centroid cosine distance
        cosine_dist = _cosine_distance(cur_emb.mean(axis=0), base_emb.mean(axis=0))

        # Significance: two-sample permutation test on the same statistic
        rng = np.random.default_rng(self.random_seed)
        p_value = _permutation_p_value(
            cur_emb, base_emb, observed=cosine_dist,
            n_permutations=self.n_permutations, rng=rng,
        )

        # Diagnostics (reported, not gating)
        n_dim = cur_emb.shape[1]
        wasserstein_per_dim = np.zeros(n_dim, dtype=np.float64)
        for d in range(n_dim):
            try:
                wasserstein_per_dim[d] = wasserstein_distance(cur_emb[:, d], base_emb[:, d])
            except Exception:
                wasserstein_per_dim[d] = 0.0
        mean_wass = float(wasserstein_per_dim.mean())
        max_wass = float(wasserstein_per_dim.max())
        var_per_dim = base_emb.var(axis=0)
        dominant = int(np.argmax(var_per_dim))
        cluster_psi = _psi_1d(cur_emb[:, dominant], base_emb[:, dominant])

        # Gate: significant AND meaningfully large
        triggered = (
            p_value < self.significance_level
            and cosine_dist >= self.centroid_distance_threshold
        )
        return SemanticDriftSignal(
            cluster_id=cluster_id,
            triggered=triggered,
            direction="shift",
            centroid_distance=cosine_dist,
            p_value=p_value,
            n_permutations=self.n_permutations,
            mean_wasserstein=mean_wass,
            max_wasserstein=max_wass,
            cluster_psi=cluster_psi,
            sample_size_current=len(current_responses),
            sample_size_baseline=len(baseline_responses),
            recommended_action=(
                _recommend(cosine_dist, p_value) if triggered else ""
            ),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _l2_normalize_rows(emb: np.ndarray) -> np.ndarray:
    """L2-normalize each row to unit length. Zero-norm rows are left as-is
    (returned as zeros) to avoid dividing by zero."""
    emb = np.asarray(emb, dtype=np.float64)
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    safe = np.where(norms == 0.0, 1.0, norms)
    return emb / safe


def _cosine_distance(u: np.ndarray, v: np.ndarray) -> float:
    """1 - cosine_similarity. Range [0, 2]; 0 = identical direction."""
    nu, nv = np.linalg.norm(u), np.linalg.norm(v)
    if nu == 0 or nv == 0:
        return 0.0
    return float(1.0 - np.dot(u, v) / (nu * nv))


def _permutation_p_value(
    cur_emb: np.ndarray,
    base_emb: np.ndarray,
    *,
    observed: float,
    n_permutations: int,
    rng: np.random.Generator,
) -> float:
    """Two-sample permutation test on centroid cosine distance.

    Null hypothesis: current and baseline embeddings are drawn from the same
    distribution. We pool them, repeatedly reshuffle into two groups of the
    original sizes, and compute the between-group centroid distance for each
    shuffle. The p-value is the fraction of shuffles whose distance is >= the
    observed one — i.e. how often random regrouping reproduces a shift this
    large. Small p means the observed shift exceeds natural within-data
    variation.

    Uses the unbiased (count + 1) / (n + 1) estimator (Davison & Hinkley 1997),
    which keeps the p-value strictly positive.
    """
    pooled = np.vstack([cur_emb, base_emb])
    n_cur = cur_emb.shape[0]
    n_total = pooled.shape[0]
    count = 0
    for _ in range(n_permutations):
        idx = rng.permutation(n_total)
        a = pooled[idx[:n_cur]].mean(axis=0)
        b = pooled[idx[n_cur:]].mean(axis=0)
        if _cosine_distance(a, b) >= observed:
            count += 1
    return (count + 1) / (n_permutations + 1)


def _psi_1d(current: np.ndarray, baseline: np.ndarray, bins: int = 10) -> float:
    """1D PSI on continuous values. Uses baseline-driven quantile bins so
    skewed distributions don't break the calculation."""
    if len(current) < 2 or len(baseline) < 2:
        return 0.0
    # Quantile-based bin edges from baseline
    try:
        edges = np.quantile(baseline, np.linspace(0, 1, bins + 1))
    except Exception:
        return 0.0
    edges = np.unique(edges)
    if len(edges) < 3:
        return 0.0
    base_hist, _ = np.histogram(baseline, bins=edges)
    cur_hist, _ = np.histogram(current, bins=edges)
    eps = 1e-6
    base_pct = base_hist / max(base_hist.sum(), 1) + eps
    cur_pct = cur_hist / max(cur_hist.sum(), 1) + eps
    # PSI is a magnitude; wrap in abs() to match the sibling _psi in drift.py
    # (floating-point rounding can otherwise leave a tiny negative value).
    return abs(float(np.sum((cur_pct - base_pct) * np.log(cur_pct / base_pct))))


def _recommend(centroid_dist: float, p_value: float) -> str:
    """Severity scales with effect size; the message makes clear that semantic
    drift is informational on its own and must be cross-referenced with quality
    signals before being treated as a regression."""
    if centroid_dist >= 0.30:
        severity = "large"
    elif centroid_dist >= 0.15:
        severity = "moderate"
    else:
        severity = "small"
    return (
        f"{severity} semantic shift (effect={centroid_dist:.2f}, p={p_value:.3f}): "
        "the response distribution changed beyond natural variation. This is "
        "informational, not a quality alarm by itself — cross-reference the "
        "judge and structural signals to tell an expected topic change apart "
        "from a real regression."
    )
