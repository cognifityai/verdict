"""Unit tests for the static pricing table and cost computation.

No provider SDKs are imported — pure arithmetic against PRICE_PER_1K.
"""

from __future__ import annotations

import math

from verdict.pricing import PRICE_PER_1K, compute_cost_usd


def test_known_model_exact_match():
    # gpt-4o: input 0.0025/1k, output 0.01/1k
    cost = compute_cost_usd("gpt-4o", 1000, 1000)
    assert cost is not None
    assert math.isclose(cost, 0.0025 + 0.01, rel_tol=1e-9)


def test_substring_match_versioned_model():
    # A versioned/suffixed id still resolves to the base entry.
    in_rate, out_rate = PRICE_PER_1K["claude-3-5-sonnet"]
    cost = compute_cost_usd("claude-3-5-sonnet-20241022", 2000, 500)
    expected = (2000 / 1000.0) * in_rate + (500 / 1000.0) * out_rate
    assert cost is not None
    assert math.isclose(cost, expected, rel_tol=1e-9)


def test_current_anthropic_haiku_45_pricing():
    in_rate, out_rate = PRICE_PER_1K["claude-haiku-4-5"]
    cost = compute_cost_usd("claude-haiku-4-5-20251001", 1000, 1000)
    assert cost is not None
    assert math.isclose(cost, in_rate + out_rate, rel_tol=1e-9)


def test_current_google_gemini_25_flash_pricing():
    in_rate, out_rate = PRICE_PER_1K["gemini-2.5-flash"]
    cost = compute_cost_usd("gemini-2.5-flash", 1000, 1000)
    assert cost is not None
    assert math.isclose(cost, in_rate + out_rate, rel_tol=1e-9)


def test_longest_substring_match_wins():
    # "gpt-4o-mini" must beat the shorter "gpt-4o" prefix.
    in_rate, out_rate = PRICE_PER_1K["gpt-4o-mini"]
    cost = compute_cost_usd("gpt-4o-mini-2024-07-18", 1000, 1000)
    expected = in_rate + out_rate
    assert cost is not None
    assert math.isclose(cost, expected, rel_tol=1e-9)
    # Sanity: it did NOT use gpt-4o's pricing.
    gpt4o_in, gpt4o_out = PRICE_PER_1K["gpt-4o"]
    assert not math.isclose(cost, gpt4o_in + gpt4o_out, rel_tol=1e-9)


def test_unknown_model_returns_none():
    assert compute_cost_usd("some-random-model", 100, 100) is None


def test_none_tokens_returns_none():
    assert compute_cost_usd("gpt-4o", None, None) is None


def test_empty_model_returns_none():
    assert compute_cost_usd("", 100, 100) is None


def test_partial_tokens_treated_as_zero():
    # Only output tokens known -> input treated as 0.
    in_rate, out_rate = PRICE_PER_1K["gpt-4o-mini"]
    cost = compute_cost_usd("gpt-4o-mini", None, 1000)
    assert cost is not None
    assert math.isclose(cost, out_rate, rel_tol=1e-9)


if __name__ == "__main__":
    test_known_model_exact_match()
    test_substring_match_versioned_model()
    test_current_anthropic_haiku_45_pricing()
    test_current_google_gemini_25_flash_pricing()
    test_longest_substring_match_wins()
    test_unknown_model_returns_none()
    test_none_tokens_returns_none()
    test_empty_model_returns_none()
    test_partial_tokens_treated_as_zero()
    print("test_pricing OK")
