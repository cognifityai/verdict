"""Static model pricing table and cost computation.

This is a small, dependency-free pricing table used to estimate the USD cost of
an LLM call from its token counts. It is intentionally a *static* snapshot of
public list prices (USD per 1,000 tokens, input/output) — provider prices change
over time, so this table MUST be reviewed and updated periodically. It is a best-
effort estimate, not a billing source of truth.

Known exact aliases resolve to an immutable release entry first. Other matching
is by substring of the model name (longest matching key wins), so a provider-
prefixed/suffixed model ID still resolves to its dated or versioned entry.
GPT-4.1 base prices are limited to the published base IDs and snapshots;
fine-tuned and custom derivatives do not inherit them.
"""

from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger("verdict.pricing")

# This is the last full-table audit date. Newly added model entries carry their
# own check date below; do not advance this date after checking only one family.
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
    "claude-opus-5-5": (0.004, 0.020),
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
    # GPT-6 standard short-context text rates checked 2026-10-01 against the
    # official model pricing pages. Long context and service tiers are excluded.
    "gpt-6-astra": (0.010, 0.050),
    "gpt-6.1-sol": (0.002, 0.010),
    "gpt-6-sol": (0.002, 0.010),
    "gpt-6-luna": (0.0001, 0.0005),
    "gpt-5.6-sol": (0.004, 0.020),
    "gpt-5.6-terra": (0.002, 0.012),
    "gpt-5.6-luna": (0.0002, 0.0012),
    "gpt-5.5-pro": (0.030, 0.180),
    "gpt-5.5": (0.005, 0.030),
    "gpt-5.4-pro": (0.030, 0.180),
    "gpt-5.4-mini": (0.00075, 0.0045),
    "gpt-5.4-nano": (0.0002, 0.00125),
    "gpt-5.4": (0.0025, 0.015),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1-nano": (0.0001, 0.0004),
    "gpt-4.1": (0.002, 0.008),
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

_GPT41_BASE_MODELS = frozenset({"gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"})
_GPT41_SNAPSHOT_DATE = "2025-04-14"
_GPT41_ALIASES = {
    f"{prefix}{model}{suffix}": model
    for model in _GPT41_BASE_MODELS
    for prefix in ("", "openai/", "openai:")
    for suffix in ("", f"-{_GPT41_SNAPSHOT_DATE}")
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

    Exact aliases resolve first. Other models use the longest matching
    PRICE_PER_1K substring (so "gpt-4o-mini" beats "gpt-4o"); GPT-4.1 prices
    require an exact approved alias. Returns None if the model is unknown or
    if *both* token counts are missing. A missing input/output count is treated
    as zero so a partially-known call still yields an estimate.

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
            best_key = _GPT41_ALIASES.get(normalized_model)
        if best_key is None:
            # Longest substring match wins for dated/versioned entries.
            for key in PRICE_PER_1K:
                if key in _GPT41_BASE_MODELS:
                    continue
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


# Cached-token rates relative to a model's base input rate, from provider
# pricing pages. Newer model entries were checked after the full-table audit.
# Anthropic bills cache reads at 10% of input and five-minute writes at 125%.
# OpenAI's cached-input discount depends on the family; the prefixes below are
# matched in order and an unlisted family uses 0.5, the least generous listed
# discount, so an unknown family is never under-priced.
# Anthropic cache reads are 10% of input except on the models listed here;
# cache writes are the five-minute rate. One-hour cache writes cost 2x input,
# but the normalized turn fields do not record which duration was used, so the
# estimate assumes five-minute writes.
_ANTHROPIC_CACHE_READ = 0.1
_ANTHROPIC_CACHE_READ_BY_MODEL: tuple[tuple[str, float], ...] = (
    ("claude-fable-5-1", 0.025),
    ("claude-mythos-5-1", 0.025),
    ("claude-opus-5-5", 0.05),
)
_ANTHROPIC_CACHE_WRITE = 1.25
_OPENAI_CACHE_READ_BY_FAMILY: tuple[tuple[str, float], ...] = (
    ("gpt-6.1-sol", 0.05),
    ("gpt-5", 0.1),
    ("gpt-6", 0.1),
    ("gpt-4.1", 0.25),
    ("o3", 0.25),
    ("o4", 0.25),
    ("gpt-4o", 0.5),
    ("o1", 0.5),
)
_OPENAI_CACHE_READ_DEFAULT = 0.5


def _provider_of(model: str) -> str | None:
    name = model.lower().rsplit("/", 1)[-1]
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    return None


def _cache_read_multiplier(provider: str, model: str) -> float:
    name = model.lower().rsplit("/", 1)[-1]
    if provider == "anthropic":
        for family, multiplier in _ANTHROPIC_CACHE_READ_BY_MODEL:
            if name == family or name.startswith(family + "-"):
                return multiplier
        return _ANTHROPIC_CACHE_READ
    for family, multiplier in _OPENAI_CACHE_READ_BY_FAMILY:
        if name == family or name.startswith((family + "-", family + ".")):
            return multiplier
    return _OPENAI_CACHE_READ_DEFAULT


def estimate_turn_cost_usd(
    model: str,
    *,
    input_tokens: int | None,
    cached_input_tokens: int | None,
    cache_write_input_tokens: int | None,
    output_tokens: int | None,
    input_includes_cached: bool,
) -> float | None:
    """Estimate the list price of one agent turn from its token components.

    ``input_includes_cached`` states the source's convention: OpenAI-style
    counts include cached tokens in ``input_tokens`` (Codex), Anthropic-style
    counts exclude them (Claude Code). Output tokens are priced at the output
    rate, which already covers reasoning tokens for both providers. Returns
    ``None`` when the model has no static price, the provider's cache rates are
    unknown, or the counts are inconsistent; never raises.
    """
    try:
        provider = _provider_of(model)
        if provider is None:
            return None
        counts = [
            0 if value is None else int(value)
            for value in (input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens)
        ]
        if any(value < 0 for value in counts):
            return None
        input_count, cached, cache_write, output = counts
        uncached = input_count - cached if input_includes_cached else input_count
        if uncached < 0:
            return None
        base = compute_cost_usd(model, uncached, output)
        if base is None:
            return None
        cached_cost = compute_cost_usd(model, cached, 0) or 0.0
        write_cost = compute_cost_usd(model, cache_write, 0) or 0.0
        return float(
            base
            + cached_cost * _cache_read_multiplier(provider, model)
            + write_cost * (_ANTHROPIC_CACHE_WRITE if provider == "anthropic" else 1.0)
        )
    except Exception:
        return None
