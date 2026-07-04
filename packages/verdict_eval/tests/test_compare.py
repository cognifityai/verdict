"""Bradley-Terry comparator tests."""

from __future__ import annotations

import random

from verdict_eval.compare import BradleyTerryComparator, PairwiseResult


def _games(model_a: str, model_b: str, n: int, p_a_wins: float, seed: int) -> list[PairwiseResult]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        winner = model_a if rng.random() < p_a_wins else model_b
        out.append(
            PairwiseResult(model_a=model_a, model_b=model_b, winner=winner)
        )
    return out


def test_two_models_clear_winner():
    games = _games("alpha", "beta", n=200, p_a_wins=0.85, seed=1)
    cmp = BradleyTerryComparator(bootstrap_iterations=100, anchor="beta")
    ratings = cmp.fit(games)
    by_model = {r.model: r for r in ratings}
    assert by_model["alpha"].rating > by_model["beta"].rating
    # Win-rate vs anchor (beta) should reflect ~85% empirical win rate
    assert by_model["alpha"].win_rate_vs_anchor is not None
    assert by_model["alpha"].win_rate_vs_anchor > 0.7
    # Bootstrap CIs should bracket the point estimate
    assert by_model["alpha"].rating_lo <= by_model["alpha"].rating <= by_model["alpha"].rating_hi


def test_three_way_transitivity():
    rng = random.Random(42)
    # alpha > beta > gamma
    games: list[PairwiseResult] = []
    for _ in range(150):
        w = "alpha" if rng.random() < 0.75 else "beta"
        games.append(PairwiseResult("alpha", "beta", w))
    for _ in range(150):
        w = "beta" if rng.random() < 0.75 else "gamma"
        games.append(PairwiseResult("beta", "gamma", w))
    for _ in range(150):
        w = "alpha" if rng.random() < 0.85 else "gamma"
        games.append(PairwiseResult("alpha", "gamma", w))
    cmp = BradleyTerryComparator(bootstrap_iterations=50)
    ratings = cmp.fit(games)
    by = {r.model: r.rating for r in ratings}
    assert by["alpha"] > by["beta"] > by["gamma"]


def test_drops_position_inconsistent_when_configured():
    g1 = PairwiseResult("a", "b", "a", position_consistent=False)
    g2 = PairwiseResult("a", "b", "a", position_consistent=True)
    cmp = BradleyTerryComparator(drop_position_inconsistent=True, bootstrap_iterations=10)
    ratings = cmp.fit([g1, g2])
    # Only one valid game → still produces ratings
    assert len(ratings) == 2


def test_clean_sweep_gives_large_positive_gap():
    """A beats B 10/10 (single class). The old zero-feature, weight-1.0 shortcut
    biased ratings toward 0, shrinking the gap to a near-tie. The symmetric
    1e-3 regularizer must leave a large, finite, positive A-over-B gap."""
    games = [PairwiseResult("A", "B", "A") for _ in range(10)]
    cmp = BradleyTerryComparator(bootstrap_iterations=10)
    ratings = cmp.fit(games)
    by = {r.model: r.rating for r in ratings}
    gap = by["A"] - by["B"]
    assert gap > 1.0, f"clean-sweep gap should be large, got {gap}"
    assert gap < 1e6, "gap must remain finite (L2 prior keeps it bounded)"


def test_ties_split_credit():
    games = [
        PairwiseResult("a", "b", "tie"),
        PairwiseResult("a", "b", "tie"),
        PairwiseResult("a", "b", "tie"),
    ]
    cmp = BradleyTerryComparator(bootstrap_iterations=10)
    ratings = cmp.fit(games)
    # All ties → ratings ~equal
    by = {r.model: r.rating for r in ratings}
    assert abs(by["a"] - by["b"]) < 0.1
