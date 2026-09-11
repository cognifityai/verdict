"""Differential checks for statistics shared by core and evaluator paths."""

from __future__ import annotations

import random

import pytest
from scipy.stats import fisher_exact
from verdict.statistics import fisher_exact_two_sided


def test_shared_fisher_matches_scipy_across_edge_and_random_tables() -> None:
    tables = [
        (0, 0, 0, 0),
        (0, 10, 0, 10),
        (10, 0, 0, 10),
        (1, 9, 11, 3),
        (8_000, 72_000, 1_500, 18_500),
    ]
    rng = random.Random(0)
    tables.extend(tuple(rng.randrange(0, 250) for _ in range(4)) for _ in range(500))

    for table in tables:
        expected = float(fisher_exact([[table[0], table[1]], [table[2], table[3]]]).pvalue)
        assert fisher_exact_two_sided(*table) == pytest.approx(expected, abs=1e-12)


def test_shared_fisher_preserves_alert_threshold_decisions() -> None:
    rng = random.Random(1)
    for _ in range(2_000):
        table = tuple(rng.randrange(0, 100) for _ in range(4))
        expected = float(fisher_exact([[table[0], table[1]], [table[2], table[3]]]).pvalue)
        actual = fisher_exact_two_sided(*table)
        for threshold in (0.001, 0.01, 0.05):
            assert (actual < threshold) == (expected < threshold)
