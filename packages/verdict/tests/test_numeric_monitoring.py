import random

import pytest
from scipy.stats import mannwhitneyu
from verdict.statistics import mann_whitney_rank


def test_mann_whitney_matches_scipy_ties_and_non_ties():
    rng = random.Random(7301)
    for tied in (False, True):
        for _ in range(50):
            base = [rng.randint(0, 9) if tied else rng.random() for _ in range(30)]
            current = [rng.randint(0, 9) if tied else rng.random() for _ in range(35)]
            p, effect = mann_whitney_rank(base, current)
            expected = mannwhitneyu(
                current, base, alternative="two-sided", method="asymptotic", use_continuity=True
            )
            assert p == pytest.approx(expected.pvalue, abs=1e-12)
            assert effect == pytest.approx(2 * expected.statistic / (30 * 35) - 1)
    assert mann_whitney_rank([3] * 30, [3] * 30) == (1.0, 0.0)


def test_rank_effect_does_not_infer_direction_from_median():
    p, effect = mann_whitney_rank([0] * 14 + [5] * 16, [3] * 16 + [10] * 14)
    assert p < 0.005 and effect > 0.4


def test_numeric_input_rejects_bool_missing_nonfinite():
    for bad in (True, None, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            mann_whitney_rank([bad], [1])
