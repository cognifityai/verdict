"""Static model pricing table and cost computation.

This is a small, dependency-free pricing table used to estimate the USD cost of
an LLM call from its token counts. It is intentionally a *static* snapshot of
public list prices (USD per 1,000 tokens, input/output) — provider prices change
over time, so this table MUST be reviewed and updated periodically. It is a best-
effort estimate, not a billing source of truth.

Matching is by substring of the model name (longest matching key wins), so a
versioned/suffixed model id like "claude-3-5-sonnet-20241022" still resolves to
the "claude-3-5-sonnet" entry.
"""

from __future__ import annotations

# model_substring -> (input_usd_per_1k, output_usd_per_1k)
#
# IMPORTANT: these are STATIC public list prices captured at authoring time and
# WILL drift as providers change pricing. Review and update periodically.
# Sources: Anthropic / OpenAI / Google public pricing pages.
PRICE_PER_1K: dict[str, tuple[float, float]] = {
    # Anthropic (USD per 1K tokens)
    "claude-haiku-4-5": (0.001, 0.005),
    "claude-sonnet-4-5": (0.003, 0.015),
    "claude-3-5-haiku": (0.0008, 0.004),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-opus": (0.015, 0.075),
    "claude-opus": (0.015, 0.075),
    "claude-3-haiku": (0.00025, 0.00125),
    "claude-3-sonnet": (0.003, 0.015),
    # OpenAI (USD per 1K tokens)
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4-turbo": (0.01, 0.03),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    # Google Gemini (USD per 1K tokens)
    "gemini-2.5-flash-lite": (0.0001, 0.0004),
    "gemini-2.5-flash": (0.0003, 0.0025),
    "gemini-2.5-pro": (0.00125, 0.01),
    "gemini-1.5-flash": (0.000075, 0.0003),
    "gemini-1.5-pro": (0.00125, 0.005),
}


def compute_cost_usd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    """Estimate the USD cost of a call from its model name and token counts.

    Matches ``model`` against PRICE_PER_1K by substring; the longest matching
    key wins (so "gpt-4o-mini" beats "gpt-4o"). Returns None if the model is
    unknown or if *both* token counts are missing. A missing input/output count
    is treated as zero so a partially-known call still yields an estimate.

    Never raises — returns None on any unexpected input.
    """
    if not model:
        return None

    try:
        # Longest substring match wins.
        best_key: str | None = None
        for key in PRICE_PER_1K:
            if key in model and (best_key is None or len(key) > len(best_key)):
                best_key = key
        if best_key is None:
            return None

        if input_tokens is None and output_tokens is None:
            return None

        in_rate, out_rate = PRICE_PER_1K[best_key]
        in_tok = input_tokens or 0
        out_tok = output_tokens or 0
        cost = (in_tok / 1000.0) * in_rate + (out_tok / 1000.0) * out_rate
        return float(cost)
    except Exception:
        return None
