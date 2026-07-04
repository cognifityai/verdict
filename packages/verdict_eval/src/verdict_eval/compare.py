"""Cross-LLM Bradley-Terry pairwise comparator.

Implementation follows the Chatbot Arena / Arena-Hard-Auto methodology:
- Pairwise (not absolute) scoring.
- Position-swap with reject-on-inconsistency to kill position bias.
- Optional length normalization (recorded so caller can filter).
- Bradley-Terry fit via scikit-learn logistic regression (one parameter
  per model). Ties → 0.5 win each (Davidson extension).
- Bootstrap CIs over the pairwise data.

This module computes Bradley-Terry ratings from a list of `PairwiseResult`
records. Sampling, judging, and persistence are handled by caller workflows.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import numpy as np
from sklearn.linear_model import LogisticRegression


@dataclass
class PairwiseResult:
    """One judged head-to-head comparison.

    `winner` is the model id ('A', 'B', or 'tie'). 'A' and 'B' are placeholders
    — the actual model identifiers are stored in `model_a`, `model_b`.
    `position_consistent` is True only if a position swap produced the same
    verdict; if False, caller should treat as a tie (per Wang 2023).
    """

    model_a: str
    model_b: str
    winner: str         # model_a / model_b / "tie"
    cluster_id: str = ""
    judge_model: str = ""
    position_consistent: bool = True
    judge_text_length: int = 0


@dataclass
class ModelRating:
    model: str
    rating: float       # log-odds; higher is better
    rating_lo: float    # bootstrap lower bound (2.5%)
    rating_hi: float    # bootstrap upper bound (97.5%)
    win_rate_vs_anchor: float | None = None


@dataclass
class BradleyTerryComparator:
    """Fits a Bradley-Terry model over PairwiseResults.

    Usage
    -----
        cmp = BradleyTerryComparator()
        ratings = cmp.fit(results)   # list[ModelRating]
    """

    bootstrap_iterations: int = 1000
    seed: int = 42
    anchor: str | None = None         # if set, win-rate is reported vs this model
    drop_position_inconsistent: bool = True

    def fit(self, results: list[PairwiseResult]) -> list[ModelRating]:
        if self.drop_position_inconsistent:
            results = [r for r in results if r.position_consistent]
        if not results:
            return []
        models = sorted({r.model_a for r in results} | {r.model_b for r in results})
        if len(models) < 2:
            return [ModelRating(models[0], 0.0, 0.0, 0.0) for _ in models]

        ratings = self._fit_once(results, models)

        # Bootstrap
        rng = random.Random(self.seed)
        boots: dict[str, list[float]] = {m: [] for m in models}
        N = len(results)
        for _ in range(self.bootstrap_iterations):
            sample = [results[rng.randrange(N)] for _ in range(N)]
            try:
                rs = self._fit_once(sample, models)
                for m, r in rs.items():
                    boots[m].append(r)
            except Exception:
                continue

        out: list[ModelRating] = []
        for m in models:
            arr = np.asarray(boots.get(m, []), dtype=np.float64)
            if arr.size:
                lo, hi = float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5))
            else:
                lo = hi = ratings[m]
            out.append(ModelRating(model=m, rating=ratings[m], rating_lo=lo, rating_hi=hi))

        if self.anchor and self.anchor in ratings:
            anchor_rating = ratings[self.anchor]
            for r in out:
                # P(model beats anchor) under BT
                diff = r.rating - anchor_rating
                r.win_rate_vs_anchor = 1.0 / (1.0 + math.exp(-diff))

        return out

    def _fit_once(
        self,
        results: list[PairwiseResult],
        models: list[str],
    ) -> dict[str, float]:
        idx = {m: i for i, m in enumerate(models)}
        n = len(models)
        # Build design matrix: rows are pairwise samples, columns are model effects.
        # A tie (Davidson extension) contributes half a win + half a loss. We carry
        # that as sample_weight rather than two full-weight duplicate rows — the
        # latter gives every tie 2× the influence of a decisive result, which also
        # skews the bootstrap (a tie would resample as two rows). With weights, each
        # result — decisive or tied — contributes total weight 1.0.
        X_rows: list[np.ndarray] = []
        y: list[float] = []
        w: list[float] = []
        for r in results:
            x = np.zeros(n, dtype=np.float64)
            x[idx[r.model_a]] = 1
            x[idx[r.model_b]] = -1
            if r.winner == r.model_a:
                X_rows.append(x); y.append(1.0); w.append(1.0)
            elif r.winner == r.model_b:
                X_rows.append(x); y.append(0.0); w.append(1.0)
            else:  # tie → half win, half loss, total weight 1.0
                X_rows.append(x); y.append(1.0); w.append(0.5)
                X_rows.append(x); y.append(0.0); w.append(0.5)
        X = np.vstack(X_rows)
        y_arr = np.asarray(y, dtype=np.float64)
        w_arr = np.asarray(w, dtype=np.float64)
        # Use Logistic Regression w/o intercept; coefficients are log-odds ratings.
        # Slight L2 regularization for stability when comparing 2 models with few games.
        model = LogisticRegression(
            fit_intercept=False,
            C=1e3,                     # very weak regularization
            solver="lbfgs",
            max_iter=2000,
        )
        # If y is entirely one class (a clean sweep — e.g. A beats B 10/10),
        # sklearn refuses to fit. The old fix appended a *zero-feature* row
        # (all model effects 0) with the opposite label and full weight 1.0.
        # That row carries no information about which models are involved, yet
        # at weight 1.0 it acts like a strong symmetric prior pulling every
        # rating toward 0 — exactly the clean-sweep cases where we most want a
        # large, finite rating gap get shrunk toward a near-tie.
        #
        # Instead, append ONE real opposing-outcome row built from an actual
        # model pair (reuse the first result's contrast vector), flipped to the
        # other label, with a SMALL weight. sklearn now sees 2 classes, but the
        # dominant outcome still drives the fit; the size of the rating gap is
        # governed by the L2 prior (C), not by this regularizing row.
        if len(set(y_arr.tolist())) < 2:
            contrast = X[0].copy()                       # a real model-pair contrast
            X = np.vstack([X, contrast])
            y_arr = np.concatenate([y_arr, np.asarray([1.0 - y_arr[0]])])
            w_arr = np.concatenate([w_arr, np.asarray([1e-3])])
        model.fit(X, y_arr, sample_weight=w_arr)
        coefs = model.coef_[0]
        # Center to keep ratings comparable across fits
        coefs = coefs - coefs.mean()
        return {m: float(coefs[idx[m]]) for m in models}
