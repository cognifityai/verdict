import math

import pytest
from verdict.statistics import (
    benjamini_hochberg,
    fisher_exact_two_sided,
    gwet_ac1,
    wilson_interval,
)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], []),
        ([1.0, 1.0, 1.0], [1.0, 1.0, 1.0]),
        ([0.01, 0.04, 0.03, 0.002], [0.02, 0.04, 0.04, 0.008]),
        ([0.0, 0.0, 1.0], [0.0, 0.0, 1.0]),
        ([0.2, 0.2, 0.2], [0.2, 0.2, 0.2]),
    ],
)
def test_benjamini_hochberg_preserves_known_results(values, expected) -> None:
    assert benjamini_hochberg(values) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ((1, 9, 11, 3), 0.0027594561852200836),
        ((0, 5, 5, 0), 0.007936507936507938),
        ((8, 2, 1, 5), 0.034965034965034975),
        ((0, 0, 0, 1), 1.0),
    ],
)
def test_fisher_exact_two_sided_matches_known_tables(table, expected) -> None:
    assert fisher_exact_two_sided(*table) == pytest.approx(expected, abs=1e-14)


def test_fisher_exact_two_sided_is_symmetric_and_finite_for_large_counts() -> None:
    value = fisher_exact_two_sided(8_000, 72_000, 1_500, 18_500)
    assert math.isfinite(value)
    assert 0.0 <= value <= 1.0
    assert fisher_exact_two_sided(1_500, 18_500, 8_000, 72_000) == pytest.approx(value)
    assert fisher_exact_two_sided(72_000, 8_000, 18_500, 1_500) == pytest.approx(value)


@pytest.mark.parametrize(
    ("successes", "total", "expected"),
    [
        (0, 0, (None, None)),
        (0, 10, (0.0, 0.2775327998628892)),
        (10, 10, (0.7224672001371107, 1.0)),
        (5, 10, (0.236593090512564, 0.7634069094874361)),
    ],
)
def test_wilson_interval_has_one_explicit_endpoint_policy(
    successes,
    total,
    expected,
) -> None:
    assert wilson_interval(successes, total) == pytest.approx(expected)


def test_gwet_ac1_handles_nominal_skew_and_degenerate_inputs() -> None:
    assert gwet_ac1([], [], 2) == 0.0
    assert gwet_ac1([0], [0, 1], 2) == 0.0
    assert gwet_ac1([0, 0, 0], [0, 0, 0], 2) == 1.0
    assert gwet_ac1([0, 0, 0, 1], [0, 0, 1, 1], 2) == pytest.approx(0.5294117647058824)
    assert gwet_ac1([0, 1], [0, 1], 1) == 1.0
    assert gwet_ac1([0, 0], [0, 1], 1) == 0.0
