"""Drift detector — proper non-parametric methodology.

The canonical answer to "your prompts are different every day so how do you
detect drift?": don't compare today's responses to yesterday's identical
prompts — compare today's *judgment scores* on prompts WITHIN A CLUSTER to
last week's judgment scores on prompts in the same cluster. The cluster
controls for prompt-distribution drift; the judgment score is the quality
signal.

Statistical methodology (June 2026 refresh):

- **Significance test — chosen by data type:**
  - For binary PASS/FAIL windows (the common case here) we use **Fisher's exact
    test** (Fisher 1922) on the 2×2 pass/fail × current/baseline table. It is
    exact, non-parametric, and the textbook test for comparing two proportions —
    a better fit than Mann-Whitney U, which on heavily-tied binary data is only
    a weaker proxy for the same comparison.
  - For non-binary (ordinal / continuous) scores we fall back to **Mann-Whitney
    U** (Mann & Whitney 1947), the right non-parametric two-sample test there.
  `statistic_name` on the emitted signal records which test was used.

- **Cliff's δ** (Cliff 1996) — non-parametric effect size, the PRIMARY gate.
  Replaces Cohen's d because our data is binary PASS/FAIL, which violates the
  normality assumption Cohen's d requires. Bounded in [-1, +1]:
  P(X1 > X2) - P(X1 < X2). On binary data this equals the pass-rate difference.

- **Cohen's d** (Cohen 1969) — kept for backward compatibility / familiarity.
  Reported alongside Cliff's δ but the threshold gating uses |Cliff's δ|.

- **Wasserstein-1 distance** (Earth Mover's Distance) — distributional drift
  signal. NOTE: on binary 0/1 data this reduces exactly to |pass-rate difference|,
  so it carries no information beyond Cliff's δ there; it becomes a genuinely
  independent signal only on continuous metrics. Reported, does not gate.

- **Population Stability Index (PSI)** — distributional stability metric. Binned
  by category for discrete/binary data (one bin per distinct value) and by linear
  edges for continuous data, so it stays meaningful on PASS/FAIL instead of
  degenerating to two populated bins. Reported, does not gate.

- **Benjamini-Hochberg** (1995) — False Discovery Rate control across multiple
  simultaneous tests. Still the standard.

A DriftSignal is emitted when:
  BH-adjusted p < p_threshold  AND  |Cliff's δ| > effect_size_threshold

Wasserstein and PSI are reported on every signal as additional evidence; they
don't gate emission but they inform the recommended-action heuristic.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import numpy as np
from scipy.stats import fisher_exact, mannwhitneyu, wasserstein_distance

from verdict.schema import DriftDirection, DriftSignal, Judgment, Verdict

log = logging.getLogger("verdict_eval.drift")


@dataclass
class DriftWindow:
    """A snapshot of judgments for one (cluster, dimension) pair.

    `scores` is a list of 0/1 floats — 1 for PASS, 0 for FAIL. UNCLEAR
    judgments are excluded from the statistical test, but their COUNT is
    tracked in `n_unclear` so the detector can flag a dimension drifting
    toward unevaluable (e.g. groundedness going UNCLEAR because retrieved
    context stopped being supplied) instead of letting it silently shrink
    the window below min_sample_size.
    """

    cluster_id: str
    dimension: str
    scores: list[float]
    n_unclear: int = 0

    @property
    def n(self) -> int:
        return len(self.scores)

    @property
    def n_total(self) -> int:
        """Scored + unclear judgments — the full window size."""
        return len(self.scores) + self.n_unclear

    @property
    def unclear_fraction(self) -> float:
        """Fraction of all judgments in this window that were UNCLEAR."""
        total = self.n_total
        return self.n_unclear / total if total else 0.0

    @property
    def mean(self) -> float:
        return float(np.mean(self.scores)) if self.scores else 0.0


@dataclass
class DriftDetector:
    """Mann-Whitney U drift detector with Cliff's δ effect-size gate and
    Benjamini-Hochberg multiple-comparison correction.

    Parameters
    ----------
    min_sample_size:
        Minimum samples in *each* of current + baseline windows before the
        test is even attempted.
    p_threshold:
        BH-adjusted p threshold for emitting a DriftSignal.
    effect_size_threshold:
        Cliff's δ threshold. If |δ| < threshold the change is "statistically
        significant but practically tiny" and we don't alert. Defaults to
        0.147 (small effect by Romano et al. 2006 thresholds for Cliff's δ):
        small=0.147, medium=0.33, large=0.474.
    """

    min_sample_size: int = 30
    p_threshold: float = 0.01
    effect_size_threshold: float = 0.147     # Cliff's δ "small" threshold

    def detect(
        self,
        *,
        current: Iterable[DriftWindow],
        baseline: Iterable[DriftWindow],
    ) -> list[DriftSignal]:
        """Compare matched (cluster, dimension) windows. Return drift signals."""
        baseline_map = {(w.cluster_id, w.dimension): w for w in baseline}
        # All matched (current, baseline) pairs, regardless of scored count —
        # the unclear-rate check needs pairs where the *scored* window is too
        # small for the pass/fail test but the *total* (scored + unclear) is
        # large enough to be meaningful.
        matched: list[tuple[DriftWindow, DriftWindow]] = []
        for cur in current:
            base = baseline_map.get((cur.cluster_id, cur.dimension))
            if base is None:
                continue
            matched.append((cur, base))

        # Pairs eligible for the PASS/FAIL drift test (need enough *scored*
        # judgments in each window).
        pairs = [
            (cur, base)
            for cur, base in matched
            if cur.n >= self.min_sample_size and base.n >= self.min_sample_size
        ]

        unclear_signals = self._detect_unclear_drift(matched)

        if not pairs:
            return unclear_signals

        # Run all statistics per pair
        raw_results: list[dict] = []
        for cur, base in pairs:
            cur_arr = np.asarray(cur.scores, dtype=np.float64)
            base_arr = np.asarray(base.scores, dtype=np.float64)

            # Pick the right significance test for the data type. Binary
            # PASS/FAIL → Fisher's exact (exact two-proportion test); anything
            # ordinal/continuous → Mann-Whitney U.
            is_binary = set(np.unique(np.concatenate([cur_arr, base_arr]))).issubset({0.0, 1.0})
            try:
                if is_binary:
                    pass_cur = int(cur_arr.sum())
                    pass_base = int(base_arr.sum())
                    table = [
                        [pass_cur, len(cur_arr) - pass_cur],
                        [pass_base, len(base_arr) - pass_base],
                    ]
                    odds, p = fisher_exact(table, alternative="two-sided")
                    stat_name = "fisher_exact"
                    stat_value = float(odds) if math.isfinite(odds) else 0.0
                else:
                    u_stat, p = mannwhitneyu(cur_arr, base_arr, alternative="two-sided")
                    stat_name = "mann_whitney_u"
                    stat_value = float(u_stat)
            except ValueError:
                continue

            cliffs = _cliffs_delta(cur_arr, base_arr)
            cohen = _cohens_d(cur_arr, base_arr)
            wass = _wasserstein(cur_arr, base_arr)
            psi = _psi(cur_arr, base_arr)

            mean_cur = float(cur_arr.mean())
            mean_base = float(base_arr.mean())
            if mean_cur < mean_base:
                direction = DriftDirection.REGRESSION
            elif mean_cur > mean_base:
                direction = DriftDirection.IMPROVEMENT
            else:
                direction = DriftDirection.CHANGE

            raw_results.append({
                "cur": cur,
                "base": base,
                "stat_name": stat_name,
                "stat_value": stat_value,
                "p": float(p),
                "cliffs": cliffs,
                "cohen": cohen,
                "wass": wass,
                "psi": psi,
                "direction": direction,
            })

        if not raw_results:
            return unclear_signals

        # Benjamini-Hochberg adjustment. BH assumes the p-values in a family
        # come from exchangeable tests; pooling Fisher's-exact and Mann-Whitney
        # p-values into one family violates that. So we group by the test that
        # produced each p-value and run BH separately within each group, then
        # recombine into the original order.
        adjusted = [1.0] * len(raw_results)
        groups: dict[str, list[int]] = {}
        for i, r in enumerate(raw_results):
            groups.setdefault(r["stat_name"], []).append(i)
        for idxs in groups.values():
            group_adj = _benjamini_hochberg([raw_results[i]["p"] for i in idxs])
            for i, p_adj in zip(idxs, group_adj, strict=True):
                adjusted[i] = p_adj

        signals: list[DriftSignal] = []
        for r, p_adj in zip(raw_results, adjusted, strict=True):
            # Gate on adjusted p AND Cliff's δ (NOT Cohen's d)
            if p_adj > self.p_threshold:
                continue
            if abs(r["cliffs"]) < self.effect_size_threshold:
                continue
            signals.append(
                DriftSignal(
                    cluster_id=r["cur"].cluster_id,
                    dimension=r["cur"].dimension,
                    direction=r["direction"],
                    statistic_name=r["stat_name"],
                    statistic_value=r["stat_value"],
                    p_value=r["p"],
                    p_value_adjusted=p_adj,
                    effect_size_cliffs_delta=r["cliffs"],
                    effect_size_cohens_d=r["cohen"],
                    wasserstein_distance=r["wass"],
                    psi=r["psi"],
                    sample_size_current=r["cur"].n,
                    sample_size_baseline=r["base"].n,
                    contributing_layers=["judge_rubric"],
                    recommended_action=_recommend(r["direction"], r["cliffs"], r["wass"]),
                )
            )
        return signals + unclear_signals

    def _detect_unclear_drift(
        self, matched: list[tuple[DriftWindow, DriftWindow]]
    ) -> list[DriftSignal]:
        """Emit a DriftSignal when a (cluster, dimension)'s UNCLEAR fraction
        rises materially between baseline and current.

        A dimension drifting toward unevaluable (e.g. groundedness going
        UNCLEAR because retrieved context stopped being supplied) is a real
        regression: the pass/fail test silently loses those samples, so the
        window shrinks and the regression disappears. We surface it as a
        separate signal (statistic_name="unclear_rate_increase",
        direction=REGRESSION) gated on:

          - (current_unclear_frac - baseline_unclear_frac) >= 0.15, AND
          - both windows have >= min_sample_size TOTAL judgments
            (scored + unclear).

        This is independent of, and additional to, the PASS/FAIL drift logic.
        """
        signals: list[DriftSignal] = []
        for cur, base in matched:
            if cur.n_total < self.min_sample_size or base.n_total < self.min_sample_size:
                continue
            cur_frac = cur.unclear_fraction
            base_frac = base.unclear_fraction
            if (cur_frac - base_frac) < 0.15:
                continue
            signals.append(
                DriftSignal(
                    cluster_id=cur.cluster_id,
                    dimension=cur.dimension,
                    direction=DriftDirection.REGRESSION,
                    statistic_name="unclear_rate_increase",
                    statistic_value=cur_frac,
                    p_value=1.0,
                    p_value_adjusted=1.0,
                    sample_size_current=cur.n_total,
                    sample_size_baseline=base.n_total,
                    contributing_layers=["judge_rubric"],
                    recommended_action=(
                        f"unclear rate rose {base_frac:.0%}→{cur_frac:.0%}; the judge is "
                        "increasingly unable to evaluate this dimension (e.g. missing "
                        "retrieved context) — investigate as a silent regression."
                    ),
                )
            )
        return signals


# ---------------------------------------------------------------------------
# Helpers to build DriftWindows from raw Judgments
# ---------------------------------------------------------------------------


def build_windows_from_judgments(
    judgments: Iterable[Judgment],
    cluster_id_for_trace: dict[str, str],
) -> list[DriftWindow]:
    """Group judgments by (cluster_id, dimension) and turn PASS/FAIL into 1/0.

    UNCLEAR verdicts are excluded from the score arrays (the stats can't use
    them), but they're counted so we can warn when a dimension is going
    *unjudgeable* — e.g. groundedness drifting to UNCLEAR because retrieved
    context stopped being supplied. Without this, such a regression silently
    shrinks the window below min_sample_size and the dimension just stops being
    tested instead of being flagged.
    """
    buckets: dict[tuple[str, str], list[float]] = {}
    unclear: dict[tuple[str, str], int] = {}
    for j in judgments:
        cluster = cluster_id_for_trace.get(j.trace_id)
        if cluster is None:
            continue
        for dim in j.dimensions:
            key = (cluster, dim.name)
            if dim.verdict == Verdict.PASS:
                v = 1.0
            elif dim.verdict == Verdict.FAIL:
                v = 0.0
            else:
                unclear[key] = unclear.get(key, 0) + 1
                continue
            buckets.setdefault(key, []).append(v)

    # Diagnostic: flag (cluster, dimension) pairs dominated by UNCLEAR.
    for key, n_unclear in unclear.items():
        n_scored = len(buckets.get(key, []))
        if n_unclear > n_scored:
            log.warning(
                "Drift window %s is mostly UNCLEAR (%d unclear vs %d scored); "
                "the judge may be unable to evaluate this dimension (e.g. missing "
                "retrieved context) — possible silent regression.",
                key, n_unclear, n_scored,
            )

    # Build windows for every (cluster, dimension) key seen as scored OR
    # unclear, so a dimension that has gone *mostly* UNCLEAR still produces a
    # window (with an empty/short score list and a populated n_unclear) for
    # the detector to inspect.
    all_keys = set(buckets) | set(unclear)
    return [
        DriftWindow(
            cluster_id=c,
            dimension=d,
            scores=buckets.get((c, d), []),
            n_unclear=unclear.get((c, d), 0),
        )
        for (c, d) in all_keys
    ]


def split_windows_by_time(
    judgments: list[Judgment],
    cluster_id_for_trace: dict[str, str],
    *,
    current_hours: int = 24,
    baseline_days: int = 7,
    baseline_lag_hours: int = 24,
    now: datetime | None = None,
) -> tuple[list[DriftWindow], list[DriftWindow]]:
    """Split a flat list of judgments into (current, baseline) windows."""
    now_ts = now or datetime.now(timezone.utc)
    cur_start = now_ts - timedelta(hours=current_hours)
    base_end = now_ts - timedelta(hours=baseline_lag_hours)
    base_start = base_end - timedelta(days=baseline_days)

    current_js, baseline_js = [], []
    for j in judgments:
        ts = j.created_at
        if ts >= cur_start:
            current_js.append(j)
        elif base_start <= ts < base_end:
            baseline_js.append(j)
    return (
        build_windows_from_judgments(current_js, cluster_id_for_trace),
        build_windows_from_judgments(baseline_js, cluster_id_for_trace),
    )


# ---------------------------------------------------------------------------
# Statistical helpers — properly non-parametric
# ---------------------------------------------------------------------------


def _cliffs_delta(a: np.ndarray, b: np.ndarray) -> float:
    """Cliff's δ — non-parametric effect size in [-1, 1].

    δ = P(X1 > X2) - P(X1 < X2) where X1 ~ a, X2 ~ b.

    Romano et al. 2006 magnitude thresholds:
        |δ| < 0.147        negligible
        0.147 ≤ |δ| < 0.33 small
        0.33  ≤ |δ| < 0.474 medium
        |δ| ≥ 0.474        large

    This is the right effect size to pair with Mann-Whitney U on
    non-parametric (e.g. PASS/FAIL binary) data.
    """
    if len(a) == 0 or len(b) == 0:
        return 0.0
    # Vectorized: count pairwise comparisons via broadcasting
    a_col = a[:, None]
    b_row = b[None, :]
    greater = (a_col > b_row).sum()
    lesser = (a_col < b_row).sum()
    n = len(a) * len(b)
    return float((greater - lesser) / n)


def _wasserstein(a: np.ndarray, b: np.ndarray) -> float:
    """Wasserstein-1 (Earth Mover's Distance). Higher = more drift."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    try:
        return float(wasserstein_distance(a, b))
    except Exception:
        return 0.0


def _psi(current: np.ndarray, baseline: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index.

    Standard industry interpretation:
        PSI < 0.1       no significant population change
        0.1 ≤ PSI < 0.25 moderate population change
        PSI ≥ 0.25      significant population change

    Computed by binning baseline and current into the same bins and computing:
        PSI = Σ (cur_pct - base_pct) * ln(cur_pct / base_pct)

    For discrete data with few distinct values (binary PASS/FAIL is the common
    case here) we use one bin per category instead of linear edges — otherwise
    np.linspace would put every observation in the first/last of 10 bins, leaving
    8 always-empty bins and reducing PSI to a noisy two-bin artifact. For
    continuous data we keep linear baseline-driven edges.

    Small constant added to avoid log(0) on empty bins.
    """
    if len(current) < 2 or len(baseline) < 2:
        return 0.0

    combined = np.concatenate([current, baseline])
    uniques = np.unique(combined)
    eps = 1e-6

    if uniques.size <= bins:
        # Discrete / few-valued: one bin per distinct category.
        if uniques.size < 2:
            # Both windows are a single constant value; no distributional change.
            return 0.0
        base_counts = np.array([(baseline == v).sum() for v in uniques], dtype=np.float64)
        cur_counts = np.array([(current == v).sum() for v in uniques], dtype=np.float64)
    else:
        # Continuous: baseline-driven linear edges.
        base_min, base_max = float(baseline.min()), float(baseline.max())
        if base_min == base_max:
            return 0.0
        edges = np.linspace(base_min, base_max, bins + 1)
        base_counts, _ = np.histogram(baseline, bins=edges)
        cur_counts, _ = np.histogram(current, bins=edges)
        base_counts = base_counts.astype(np.float64)
        cur_counts = cur_counts.astype(np.float64)

    base_pct = base_counts / max(base_counts.sum(), 1) + eps
    cur_pct = cur_counts / max(cur_counts.sum(), 1) + eps
    psi = float(np.sum((cur_pct - base_pct) * np.log(cur_pct / base_pct)))
    return abs(psi)


def _cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    """Cohen's d — kept for backward compatibility / reference only.

    Assumes normality which our binary PASS/FAIL data violates. Use Cliff's δ
    as the primary effect size; this is reported alongside for users familiar
    with d's magnitude scale (0.2 small, 0.5 medium, 0.8 large).
    """
    if len(a) < 2 or len(b) < 2:
        return 0.0
    var_a = a.var(ddof=1)
    var_b = b.var(ddof=1)
    pooled = math.sqrt(((len(a) - 1) * var_a + (len(b) - 1) * var_b) / (len(a) + len(b) - 2))
    if pooled == 0:
        return 0.0
    return float((a.mean() - b.mean()) / pooled)


def _benjamini_hochberg(p_values: list[float]) -> list[float]:
    """Benjamini-Hochberg FDR control. Returns adjusted p-values, original order."""
    if not p_values:
        return []
    n = len(p_values)
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    adjusted = [0.0] * n
    prev = 1.0
    for rank, (orig_idx, p) in enumerate(reversed(indexed), start=1):
        true_rank = n - rank + 1
        adj = min(prev, p * n / true_rank)
        adj = min(adj, 1.0)
        adjusted[orig_idx] = adj
        prev = adj
    return adjusted


def _recommend(direction: DriftDirection, cliffs_delta: float, wasserstein: float) -> str:
    """Recommend action based on Cliff's δ magnitude (Romano et al. 2006 thresholds)."""
    mag = abs(cliffs_delta)
    if direction == DriftDirection.REGRESSION:
        if mag >= 0.474:
            return "investigate immediately; consider rolling back model or prompt (Cliff's δ large)"
        if mag >= 0.33:
            return "investigate within day; sample affected responses (Cliff's δ medium)"
        return "monitor closely; early-warning regression signal (Cliff's δ small)"
    if direction == DriftDirection.IMPROVEMENT:
        return f"improvement detected (Cliff's δ={cliffs_delta:.3f}); consider refreshing baseline if sustained"
    return f"behavior changed (Wasserstein={wasserstein:.3f}) but direction unclear; sample responses for review"
