"""Turn-level list-price estimates from token components."""

from __future__ import annotations

import pytest
from verdict.pricing import PRICE_PER_1K, estimate_turn_cost_usd


def test_claude_turn_prices_cache_reads_and_writes_at_anthropic_rates() -> None:
    assert PRICE_PER_1K["claude-sonnet-4-5"] == (0.003, 0.015)
    # 1,000 uncached input, 10,000 cache reads at 10%, 2,000 cache writes at
    # 125%, 500 output: 0.003 + 0.003 + 0.0075 + 0.0075.
    cost = estimate_turn_cost_usd(
        "claude-sonnet-4-5", input_tokens=1_000, cached_input_tokens=10_000,
        cache_write_input_tokens=2_000, output_tokens=500, input_includes_cached=False,
    )
    assert cost == pytest.approx(0.021)


def test_openai_turn_subtracts_cached_tokens_from_input_before_pricing() -> None:
    assert PRICE_PER_1K["gpt-4.1"] == (0.002, 0.008)
    # 4,000 input of which 3,000 cached (25% rate for gpt-4.1), 1,000 output:
    # 0.002 + 0.0015 + 0.008.
    cost = estimate_turn_cost_usd(
        "gpt-4.1", input_tokens=4_000, cached_input_tokens=3_000,
        cache_write_input_tokens=None, output_tokens=1_000, input_includes_cached=True,
    )
    assert cost == pytest.approx(0.0115)


def test_openai_family_cache_rates() -> None:
    def cached_only(model: str) -> float:
        return estimate_turn_cost_usd(
            model, input_tokens=1_000, cached_input_tokens=1_000,
            cache_write_input_tokens=None, output_tokens=0, input_includes_cached=True,
        )

    in_rate = lambda model: PRICE_PER_1K[model][0]  # noqa: E731
    assert cached_only("gpt-5.4") == pytest.approx(in_rate("gpt-5.4") * 0.1)
    assert cached_only("gpt-4.1") == pytest.approx(in_rate("gpt-4.1") * 0.25)


def test_inconsistent_or_unknown_inputs_return_none() -> None:
    common = dict(cache_write_input_tokens=None, output_tokens=10)
    # Cached tokens exceeding the input that supposedly includes them.
    assert estimate_turn_cost_usd("gpt-4.1", input_tokens=100, cached_input_tokens=200,
                                  input_includes_cached=True, **common) is None
    assert estimate_turn_cost_usd("gpt-4.1", input_tokens=-1, cached_input_tokens=0,
                                  input_includes_cached=True, **common) is None
    assert estimate_turn_cost_usd("not-a-real-model", input_tokens=100, cached_input_tokens=0,
                                  input_includes_cached=False, **common) is None
    assert estimate_turn_cost_usd("mistral-large", input_tokens=100, cached_input_tokens=0,
                                  input_includes_cached=False, **common) is None
    assert estimate_turn_cost_usd("gpt-4.1", input_tokens="ten", cached_input_tokens=0,
                                  input_includes_cached=True, **common) is None


def test_missing_components_count_as_zero() -> None:
    assert estimate_turn_cost_usd(
        "claude-sonnet-4-5", input_tokens=1_000, cached_input_tokens=None,
        cache_write_input_tokens=None, output_tokens=None, input_includes_cached=False,
    ) == pytest.approx(0.003)
