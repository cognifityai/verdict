"""Structural checks — Layer 2 of the 5-layer eval pipeline.

These are the cheap, deterministic signals you can compute without LLM
judges or embeddings. They catch things the semantic-drift detector and
the judge can both miss:

  - **Hedge density**: how often the response uses uncertainty markers
    ("I think", "perhaps", "might"). Spikes signal context rot or
    sycophantic capitulation.
  - **Refusal rate**: fraction of responses that decline to answer.
    Uses signature-based regex (not substring matching) to avoid false
    positives — `i won't` matches `I won't help` but not `I won't recommend`.
  - **Apology rate**: response starts with apology, often correlated with
    the model thinking it made a mistake or is reversing a position.
  - **Response-length drift**: distribution shift in tokens/words via
    Wasserstein-1. Long-tail spikes can signal verbose hallucination;
    short-tail drops can signal clipped / refused responses.
  - **Latency drift**: distribution shift in response latency. Useful for
    catching backend degradation upstream of quality drops.
  - **JSON-validity rate**: when the response is expected to be JSON, what
    fraction actually parses. A drop is a hard regression.
  - **Token-efficiency drift**: output_tokens / input_tokens ratio.
    Big jumps signal verbosity drift or context-window inefficiency.

These compose into a StructuralChecker that produces a StructuralSignal —
the same shape as DriftSignal and SemanticDriftSignal so storage and
downstream consumers treat them uniformly.

Why this matters: deterministic checks catch regressions that are often cheaper
and clearer to detect structurally than with an LLM judge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import numpy as np

# --------------------------------------------------------------------------- #
# Pattern libraries
# --------------------------------------------------------------------------- #

# Hedge markers — word-boundary regex so "i think" doesn't match "rethinking".
# Case-insensitive at apply time (we lowercase the text first).
HEDGE_PATTERNS: tuple[str, ...] = (
    r"\bi think\b", r"\bi believe\b", r"\bperhaps\b", r"\bmaybe\b",
    r"\bmight\b", r"\bcould\b", r"\bpossibly\b", r"\bseems like\b",
    r"\bappears (?:to|that)\b", r"\bi'?m not sure\b", r"\bnot certain\b",
    r"\bpotentially\b", r"\bit depends\b", r"\bwould say\b",
    r"\bin some sense\b",
)

# Refusal signatures — require the surrounding context, not just the verb.
# "i won't" alone false-positives on "I won't recommend that approach."
# Each pattern is a *signature* of an actual refusal.
REFUSAL_SIGNATURES: tuple[str, ...] = (
    r"\bi can'?t (?:help|assist|do that|do this|provide|create|generate|write)\b",
    r"\bi won'?t (?:help|assist|provide|create|write|do|generate)\b",
    r"\bi'?m (?:not able|unable) to (?:help|assist|do|provide|comply|fulfill|generate)\b",
    r"\bi (?:must|have to) decline\b",
    r"\bi (?:cannot|can'?t) comply\b",
    r"\bnot (?:something|able) i (?:can|will) (?:help|do|assist)\b",
    r"\bi'?m sorry,? but (?:i can'?t|i won'?t|i'?m unable|i can not)\b",
)

# Apology starters — anchored to start-of-text to avoid mid-response apologies
# (those are often conversational rather than refusal-signaling).
APOLOGY_STARTERS: tuple[str, ...] = (
    r"^\s*i apologi[sz]e\b",
    r"^\s*i'?m sorry\b",
    r"^\s*sorry,?\s",
    r"^\s*my apologies\b",
)


# --------------------------------------------------------------------------- #
# Pattern application
# --------------------------------------------------------------------------- #

# CJK / Japanese-kana ranges. These scripts don't separate words with
# whitespace, so str.split() returns ~1 token for an entire CJK paragraph and
# length-drift detection on word counts goes blind. We approximate a token per
# ideograph / kana character instead.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs (Han)
    (0x3040, 0x309F),    # Hiragana
    (0x30A0, 0x30FF),    # Katakana
)


def _is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _word_count(text: str) -> int:
    """Approximate word count that works for whitespace-delimited AND
    non-whitespace (CJK/kana) text.

    = number of whitespace-separated tokens PLUS number of CJK ideograph /
    kana characters. For Latin text the CJK count is 0 so this matches
    str.split(); for CJK text the whitespace token count is ~1 so the
    per-character count dominates and length drift becomes measurable.
    """
    whitespace_tokens = len(text.split())
    cjk_chars = sum(1 for ch in text if _is_cjk_char(ch))
    return whitespace_tokens + cjk_chars


def count_hedges(text: str) -> int:
    """Total hedge-marker occurrences in `text` (case-insensitive)."""
    lower = text.lower()
    return sum(len(re.findall(p, lower)) for p in HEDGE_PATTERNS)


def is_refusal(text: str) -> bool:
    """True if `text` matches any refusal signature.

    The signatures are constructed to require enough context that normal
    conversational uses of "I can't" or "I won't" don't trigger.
    """
    lower = text.lower()
    return any(re.search(p, lower) for p in REFUSAL_SIGNATURES)


def is_apology_start(text: str) -> bool:
    """True if the response begins with an apology."""
    lower = text.lower()
    return any(re.search(p, lower) for p in APOLOGY_STARTERS)


def is_valid_json(text: str) -> bool:
    """True if `text` parses as JSON. Trims fenced code blocks first since
    LLMs frequently wrap JSON in ```json fences."""
    s = text.strip()
    if s.startswith("```"):
        # Strip ```json ... ``` fence
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"```\s*$", "", s)
    if not s:
        return False
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Window-level signal
# --------------------------------------------------------------------------- #

@dataclass
class StructuralSignal:
    """Aggregate structural metrics over a window of responses.

    Same conceptual shape as DriftSignal / SemanticDriftSignal so storage
    and downstream rules treat all three uniformly.
    """
    cluster_id: str = ""
    window_name: str = ""
    n_responses: int = 0
    mean_words: float = 0.0
    median_words: float = 0.0
    p95_words: float = 0.0
    hedge_density: float = 0.0
    refusal_rate: float = 0.0
    apology_rate: float = 0.0
    json_validity_rate: float | None = None     # None if JSON wasn't expected
    mean_latency_ms: float | None = None
    contributing_layer: str = "structural"
    flagged_examples: list[str] = field(default_factory=list)


@dataclass
class StructuralDriftSignal:
    """Drift between two StructuralSignals — same window in two epochs, or
    early-vs-late within one conversation.

    Triggers on any of:
      - hedge_density swing > hedge_density_threshold
      - refusal_rate swing > refusal_rate_threshold
      - response-length distribution Wasserstein > length_wasserstein_threshold
      - JSON-validity drop > json_drop_threshold (when both signals have JSON expected)
      - latency Wasserstein > latency_wasserstein_threshold
    """
    cluster_id: str
    baseline_window: str
    current_window: str
    hedge_density_delta: float
    refusal_rate_delta: float
    apology_rate_delta: float
    length_wasserstein: float
    latency_wasserstein: float
    json_validity_delta: float | None
    triggered: bool
    triggered_signals: list[str]
    recommended_action: str
    contributing_layer: str = "structural"


# --------------------------------------------------------------------------- #
# Checker
# --------------------------------------------------------------------------- #

@dataclass
class StructuralChecker:
    """Compute structural signals + structural drift signals.

    Parameters
    ----------
    expect_json:
        If True, the checker scores JSON-validity rate. Default False.
    hedge_density_threshold:
        Per-response hedge-marker count delta to trigger drift. Default 0.5.
    refusal_rate_threshold:
        Refusal-rate delta (absolute) to trigger drift. Default 0.05.
    length_wasserstein_threshold:
        Wasserstein-1 on the word-count distribution. Default 20 words.
    latency_wasserstein_threshold:
        Wasserstein-1 on latency_ms. Default 500 ms.
    json_drop_threshold:
        JSON-validity rate drop. Default 0.10 (10 percentage points).
    """
    expect_json: bool = False
    hedge_density_threshold: float = 0.5
    refusal_rate_threshold: float = 0.05
    length_wasserstein_threshold: float = 20.0
    latency_wasserstein_threshold: float = 500.0
    json_drop_threshold: float = 0.10

    def summarize(
        self,
        responses: list[str],
        *,
        cluster_id: str = "",
        window_name: str = "",
        latencies_ms: list[float] | None = None,
    ) -> StructuralSignal:
        """Compute the aggregate structural metrics over a window."""
        if not responses:
            return StructuralSignal(cluster_id=cluster_id, window_name=window_name)
        word_counts = np.array([_word_count(r) for r in responses], dtype=np.float64)
        hedges = np.array([count_hedges(r) for r in responses], dtype=np.float64)
        refusals = sum(1 for r in responses if is_refusal(r))
        apologies = sum(1 for r in responses if is_apology_start(r))

        json_rate: float | None = None
        if self.expect_json:
            valid = sum(1 for r in responses if is_valid_json(r))
            json_rate = valid / len(responses)

        mean_latency: float | None = None
        if latencies_ms:
            mean_latency = float(np.mean(latencies_ms))

        # Flagged examples — first few responses that look problematic
        flagged: list[str] = []
        for r in responses:
            if is_refusal(r) and len(flagged) < 3:
                flagged.append(r[:200])

        return StructuralSignal(
            cluster_id=cluster_id,
            window_name=window_name,
            n_responses=len(responses),
            mean_words=float(word_counts.mean()),
            median_words=float(np.median(word_counts)),
            p95_words=float(np.percentile(word_counts, 95)),
            hedge_density=float(hedges.mean()),
            refusal_rate=refusals / len(responses),
            apology_rate=apologies / len(responses),
            json_validity_rate=json_rate,
            mean_latency_ms=mean_latency,
            flagged_examples=flagged,
        )

    def compare(
        self,
        *,
        baseline: StructuralSignal,
        current: StructuralSignal,
        baseline_responses: list[str] | None = None,
        current_responses: list[str] | None = None,
        baseline_latencies_ms: list[float] | None = None,
        current_latencies_ms: list[float] | None = None,
    ) -> StructuralDriftSignal:
        """Compute the drift between two StructuralSignals."""
        from scipy.stats import wasserstein_distance

        hedge_delta = current.hedge_density - baseline.hedge_density
        refusal_delta = current.refusal_rate - baseline.refusal_rate
        apology_delta = current.apology_rate - baseline.apology_rate

        # Length Wasserstein — needs raw word-count arrays
        length_w = 0.0
        if baseline_responses and current_responses:
            base_w = np.array([_word_count(r) for r in baseline_responses])
            cur_w = np.array([_word_count(r) for r in current_responses])
            if len(base_w) and len(cur_w):
                length_w = float(wasserstein_distance(base_w, cur_w))

        # Latency Wasserstein
        latency_w = 0.0
        if baseline_latencies_ms and current_latencies_ms:
            latency_w = float(wasserstein_distance(baseline_latencies_ms, current_latencies_ms))

        json_delta: float | None = None
        if baseline.json_validity_rate is not None and current.json_validity_rate is not None:
            json_delta = current.json_validity_rate - baseline.json_validity_rate

        triggered_signals: list[str] = []
        if abs(hedge_delta) > self.hedge_density_threshold:
            triggered_signals.append(f"hedge_density Δ={hedge_delta:+.2f}")
        if abs(refusal_delta) > self.refusal_rate_threshold:
            triggered_signals.append(f"refusal_rate Δ={refusal_delta:+.2%}")
        if length_w > self.length_wasserstein_threshold:
            triggered_signals.append(f"length_wasserstein={length_w:.1f}")
        if latency_w > self.latency_wasserstein_threshold:
            triggered_signals.append(f"latency_wasserstein={latency_w:.1f}ms")
        if json_delta is not None and json_delta < -self.json_drop_threshold:
            triggered_signals.append(f"json_validity Δ={json_delta:+.2%}")

        triggered = bool(triggered_signals)
        action = _recommend(triggered_signals)
        return StructuralDriftSignal(
            cluster_id=baseline.cluster_id or current.cluster_id,
            baseline_window=baseline.window_name,
            current_window=current.window_name,
            hedge_density_delta=hedge_delta,
            refusal_rate_delta=refusal_delta,
            apology_rate_delta=apology_delta,
            length_wasserstein=length_w,
            latency_wasserstein=latency_w,
            json_validity_delta=json_delta,
            triggered=triggered,
            triggered_signals=triggered_signals,
            recommended_action=action,
        )


def _recommend(triggered_signals: list[str]) -> str:
    if not triggered_signals:
        return "no structural drift detected"
    return "investigate: " + "; ".join(triggered_signals)
