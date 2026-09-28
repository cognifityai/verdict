"""Dependency-free statistical primitives shared across Verdict packages."""

from __future__ import annotations

import math
from collections.abc import Sequence


def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Return the two-sided Fisher exact-test p-value for a 2x2 table."""
    if any(value < 0 for value in (a, b, c, d)):
        raise ValueError("Fisher table counts must be non-negative")
    row1, col1, total = a + b, a + c, a + b + c + d
    if total == 0:
        return 1.0
    lower, upper = max(0, row1 - (total - col1)), min(row1, col1)
    log_denominator = math.lgamma(total + 1) - math.lgamma(row1 + 1) - math.lgamma(total - row1 + 1)

    def log_probability(x: int) -> float:
        return (
            math.lgamma(col1 + 1)
            - math.lgamma(x + 1)
            - math.lgamma(col1 - x + 1)
            + math.lgamma(total - col1 + 1)
            - math.lgamma(row1 - x + 1)
            - math.lgamma(total - col1 - row1 + x + 1)
            - log_denominator
        )

    observed_log = log_probability(a)
    selected_log_sum = -math.inf
    for x in range(lower, upper + 1):
        current_log = log_probability(x)
        if current_log <= observed_log + 1e-12:
            if selected_log_sum == -math.inf:
                selected_log_sum = current_log
            else:
                high = max(selected_log_sum, current_log)
                low = min(selected_log_sum, current_log)
                selected_log_sum = high + math.log1p(math.exp(low - high))
    return min(1.0, math.exp(selected_log_sum))


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """Return Benjamini-Hochberg adjusted p-values in input order."""
    if not p_values:
        return []
    ordered = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [1.0] * len(p_values)
    running = 1.0
    for rank, (index, value) in reversed(list(enumerate(ordered, start=1))):
        running = min(running, value * len(p_values) / rank)
        adjusted[index] = min(1.0, running)
    return adjusted


def wilson_interval(
    successes: float,
    total: int,
) -> tuple[float | None, float | None]:
    """Return a two-sided 95% Wilson score interval for a binomial rate."""
    if total <= 0:
        return None, None
    z = 1.959963984540054
    observed = successes / total
    denominator = 1 + z * z / total
    center = (observed + z * z / (2 * total)) / denominator
    half_width = (
        z * math.sqrt(observed * (1 - observed) / total + z * z / (4 * total * total)) / denominator
    )
    low = 0.0 if successes == 0 else max(0.0, center - half_width)
    high = 1.0 if successes == total else min(1.0, center + half_width)
    return low, high


def gwet_ac1(
    labels_a: Sequence[int],
    labels_b: Sequence[int],
    n_categories: int = 3,
) -> float:
    """Return unweighted Gwet AC1 for two nominal label sequences."""
    if len(labels_a) != len(labels_b) or not labels_a:
        return 0.0
    total = len(labels_a)
    observed = sum(left == right for left, right in zip(labels_a, labels_b, strict=True)) / total
    if n_categories < 2:
        return 1.0 if observed == 1.0 else 0.0
    expected = 0.0
    for category in range(n_categories):
        left_rate = sum(label == category for label in labels_a) / total
        right_rate = sum(label == category for label in labels_b) / total
        mean_rate = (left_rate + right_rate) / 2.0
        expected += mean_rate * (1.0 - mean_rate)
    expected /= n_categories - 1
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1.0 - expected)


def mann_whitney_rank(reference, current):
    """Asymptotic two-sided U with ties/continuity; signed current rank effect.

    Version: mann_whitney_asymptotic_v1. This tests distributions/ranks, not
    medians. Minimum sample eligibility is enforced by the Monitor owner.
    """
    from collections import Counter
    a, b = list(reference), list(current)
    if not a or not b or len(a) + len(b) > 100_000:
        raise ValueError("rank samples must be nonempty and bounded")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in a + b):
        raise ValueError("rank samples must contain finite numbers")
    counts = Counter(a + b)
    ranks, prior = {}, 0
    for value, count in sorted(counts.items()):
        ranks[value] = prior + (count + 1) / 2
        prior += count
    n, m, total = len(a), len(b), len(a) + len(b)
    u = sum(ranks[v] for v in b) - m * (m + 1) / 2
    effect = 2 * u / (n * m) - 1
    variance = n * m / 12 * (total + 1 - sum(c**3 - c for c in counts.values()) / (total * (total - 1)))
    if variance <= 0:
        return 1.0, 0.0
    z = (abs(u - n * m / 2) - .5) / math.sqrt(variance)
    return min(1.0, math.erfc(z / math.sqrt(2))), effect
