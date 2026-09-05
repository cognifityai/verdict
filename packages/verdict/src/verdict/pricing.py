"""Static model pricing table and cost computation.

This is a small, dependency-free pricing table used to estimate the USD cost of
an LLM call from its token counts. It is intentionally a *static* snapshot of
public list prices (USD per 1,000 tokens, input/output) — provider prices change
over time, so this table MUST be reviewed and updated periodically. It is a best-
effort estimate, not a billing source of truth.

Known exact aliases resolve to an immutable release entry first. Other matching
is by substring of the model name (longest matching key wins), so a provider-
prefixed/suffixed model ID still resolves to its dated or versioned entry.
"""

from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger("verdict.pricing")

# This is deliberately visible to callers and tests. Static pricing without an
# audit date looks authoritative long after it has become stale.
PRICING_LAST_VERIFIED = date(2026, 9, 5)
PRICING_REVIEW_AFTER = date(2026, 11, 15)
PRICING_SOURCE_URLS = (
    "https://platform.claude.com/docs/en/about-claude/pricing",
    "https://developers.openai.com/api/docs/pricing",
    "https://ai.google.dev/gemini-api/docs/pricing",
)
_warned_unknown_models: set[str] = set()
_UNKNOWN_WARNING_LIMIT = 100
_warned_stale = False

# model_substring -> (input_usd_per_1k, output_usd_per_1k)
#
# IMPORTANT: these are STATIC base text input/output rates. They do not model
# cached tokens, long-context tiers, audio, batch/priority modes, data
# residency, server tools, or negotiated discounts. Review periodically.
# Sources: Anthropic / OpenAI / Google public pricing pages.
PRICE_PER_1K: dict[str, tuple[float, float]] = {
    # Anthropic (USD per 1K tokens)
    "claude-fable-5": (0.010, 0.050),
    "claude-mythos-5": (0.010, 0.050),
    "claude-opus-5": (0.005, 0.025),
    "claude-sonnet-5": (0.002, 0.010),
    "claude-opus-4-8": (0.005, 0.025),
    "claude-opus-4-7": (0.005, 0.025),
    "claude-opus-4-6": (0.005, 0.025),
    "claude-opus-4-5": (0.005, 0.025),
    "claude-opus-4-1-20250805": (0.015, 0.075),
    "claude-opus-4-20250514": (0.015, 0.075),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-haiku-4-5": (0.001, 0.005),
    "claude-sonnet-4-5": (0.003, 0.015),
    "claude-3-5-haiku": (0.0008, 0.004),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-opus": (0.015, 0.075),
    "claude-3-haiku": (0.00025, 0.00125),
    "claude-3-sonnet": (0.003, 0.015),
    # OpenAI (USD per 1K tokens)
    "gpt-5.6-sol": (0.004, 0.020),
    "gpt-5.6-terra": (0.002, 0.012),
    "gpt-5.6-luna": (0.0002, 0.0012),
    "gpt-5.5-pro": (0.030, 0.180),
    "gpt-5.5": (0.005, 0.030),
    "gpt-5.4-pro": (0.030, 0.180),
    "gpt-5.4-mini": (0.00075, 0.0045),
    "gpt-5.4-nano": (0.0002, 0.00125),
    "gpt-5.4": (0.0025, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4-turbo": (0.01, 0.03),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    # Google Gemini (USD per 1K tokens)
    "gemini-3.5-flash": (0.0015, 0.009),
    "gemini-3.5-flash-lite": (0.0003, 0.0025),
    "gemini-3.1-flash-lite": (0.00025, 0.0015),
    "gemini-2.5-flash-lite": (0.0001, 0.0004),
    "gemini-2.5-flash": (0.0003, 0.0025),
    "gemini-2.5-pro": (0.00125, 0.01),
    "gemini-1.5-flash": (0.000075, 0.0003),
    "gemini-1.5-pro": (0.00125, 0.005),
}

# These retired/deprecated Claude API aliases previously resolved to the dated
# releases above. Keep alias recognition exact so a future "claude-opus-4-x"
# identifier cannot inherit a stale rate.
EXACT_MODEL_ALIASES: dict[str, str] = {
    "claude-opus-4-1": "claude-opus-4-1-20250805",
    "claude-opus-4": "claude-opus-4-20250514",
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
    global _warned_stale
    if date.today() > PRICING_REVIEW_AFTER and not _warned_stale:
        _warned_stale = True
        log.warning(
            "Static pricing was last verified on %s and is due for review; "
            "dashboard costs are estimates, not billing truth.",
            PRICING_LAST_VERIFIED.isoformat(),
        )
    if not model:
        return None

    try:
        normalized_model = model.lower()
        if (input_tokens is not None and input_tokens < 0) or (
            output_tokens is not None and output_tokens < 0
        ):
            return None
        # Google Cloud uses @ before the snapshot date; the API and Bedrock use
        # a hyphen. Normalize only that separator, then match immutable releases.
        lookup_model = normalized_model.replace("@", "-")
        model_leaf = normalized_model.rsplit("/", 1)[-1]
        best_key = EXACT_MODEL_ALIASES.get(model_leaf)
        if best_key is None:
            # Longest substring match wins for dated/versioned entries.
            for key in PRICE_PER_1K:
                if key in lookup_model and (
                    best_key is None or len(key) > len(best_key)
                ):
                    best_key = key
        if best_key is None:
            if (
                normalized_model not in _warned_unknown_models
                and len(_warned_unknown_models) < _UNKNOWN_WARNING_LIMIT
            ):
                _warned_unknown_models.add(normalized_model)
                log.warning(
                    "No static pricing entry for model %r; cost_usd will be unavailable. "
                    "Treat dashboard spend as incomplete and verify current provider pricing.",
                    model,
                )
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
