"""Unit tests for the static pricing table and cost computation.

No provider SDKs are imported — pure arithmetic against PRICE_PER_1K.
"""

from __future__ import annotations

import logging
import math
from datetime import date

import verdict.pricing as pricing
from verdict.pricing import (
    PRICE_PER_1K,
    PRICING_LAST_VERIFIED,
    PRICING_REVIEW_AFTER,
    compute_cost_usd,
)


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


def test_model_lookup_is_case_insensitive():
    assert compute_cost_usd("GPT-4O-MINI", 1000, 1000) == compute_cost_usd(
        "gpt-4o-mini", 1000, 1000,
    )


def test_negative_token_counts_return_none():
    assert compute_cost_usd("gpt-4o-mini", -1, 100) is None


def test_unknown_model_warns_that_cost_is_unavailable(caplog):
    with caplog.at_level(logging.WARNING, logger="verdict.pricing"):
        assert compute_cost_usd("never-seen-model-for-warning-test", 100, 100) is None
    assert "cost_usd will be unavailable" in caplog.text


def test_pricing_table_has_a_visible_verification_date():
    assert PRICING_LAST_VERIFIED.isoformat() >= "2026-08-01"
    assert PRICING_REVIEW_AFTER >= PRICING_LAST_VERIFIED


def test_stale_pricing_snapshot_warns(monkeypatch, caplog):
    class FutureDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 1)

    monkeypatch.setattr(pricing, "date", FutureDate)
    monkeypatch.setattr(pricing, "_warned_stale", False)
    with caplog.at_level(logging.WARNING, logger="verdict.pricing"):
        assert compute_cost_usd("gpt-4o-mini", 100, 100) is not None
    assert "due for review" in caplog.text


def test_current_model_entries_use_base_text_rates():
    assert PRICE_PER_1K["claude-opus-5"] == (0.005, 0.025)
    assert PRICE_PER_1K["gpt-5.6-sol"] == (0.005, 0.030)
    assert PRICE_PER_1K["gpt-5.6-terra"] == (0.0025, 0.015)
    assert PRICE_PER_1K["gpt-5.6-luna"] == (0.001, 0.006)
    assert PRICE_PER_1K["gpt-5.5-pro"] == (0.030, 0.180)
    assert PRICE_PER_1K["gpt-5.4"] == (0.0025, 0.015)
    assert PRICE_PER_1K["gemini-3.5-flash"] == (0.0015, 0.009)


def test_none_tokens_returns_none():
    assert compute_cost_usd("gpt-4o", None, None) is None


def test_empty_model_returns_none():
    assert compute_cost_usd("", 100, 100) is None


def test_partial_tokens_treated_as_zero():
    # Only output tokens known -> input treated as 0.
    _in_rate, out_rate = PRICE_PER_1K["gpt-4o-mini"]
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
