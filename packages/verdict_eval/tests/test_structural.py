"""Tests for the structural Layer 2 module.

Critical regression to lock down: refusal detection must use word boundaries,
not substring matching. An internal validation test surfaced that the old "i won't"
substring matched "I won't recommend that" — a real false positive.
"""

from __future__ import annotations

import pytest

from verdict_eval.structural import (
    APOLOGY_STARTERS,
    HEDGE_PATTERNS,
    REFUSAL_SIGNATURES,
    StructuralChecker,
    _word_count,
    count_hedges,
    is_apology_start,
    is_refusal,
    is_valid_json,
)


# --------------------------------------------------------------------------- #
# Refusal detection — the regression cases that prompted this module
# --------------------------------------------------------------------------- #

def test_refusal_true_positive_basic() -> None:
    assert is_refusal("I can't help with that request.")
    assert is_refusal("I'm sorry, but I can't generate that for you.")
    assert is_refusal("I'm unable to comply with this prompt.")


def test_refusal_false_positive_substring() -> None:
    """Regression: internal validation caught these as false-positive refusals.
    They must NOT trigger the refusal detector with the new word-boundary
    signature patterns."""
    assert not is_refusal("I won't recommend that approach because it has known issues.")
    assert not is_refusal("I'm not able to verify that until you run it.")
    assert not is_refusal("You shouldn't worry about it.")
    assert not is_refusal("I cannot say for certain.")  # Generic uncertainty, not refusal


def test_refusal_handles_apostrophe_variants() -> None:
    assert is_refusal("I cant help with that, sorry.")
    assert is_refusal("I can't help with that.")
    assert is_refusal("I won't help with that, please find another assistant.")


# --------------------------------------------------------------------------- #
# Hedge counting
# --------------------------------------------------------------------------- #

def test_hedge_counting_basic() -> None:
    text = "I think this might work but I'm not sure. Perhaps we should test it."
    n = count_hedges(text)
    # i think, might, i'm not sure, perhaps
    assert n >= 3


def test_hedge_counting_word_boundaries() -> None:
    """Word boundaries: 'rethinking' should NOT match 'i think'."""
    assert count_hedges("Rethinking the whole problem") == 0
    assert count_hedges("I think it works") >= 1


def test_hedge_counting_no_hedges() -> None:
    assert count_hedges("This is definitely correct. The answer is 42.") == 0


# --------------------------------------------------------------------------- #
# Apology starters
# --------------------------------------------------------------------------- #

def test_apology_start_only_at_beginning() -> None:
    assert is_apology_start("I'm sorry, I made a mistake there.")
    assert is_apology_start("I apologize for the confusion.")
    # Mid-response "sorry" does NOT count as apology-start
    assert not is_apology_start("Let me clarify. I'm sorry if that was confusing.")


# --------------------------------------------------------------------------- #
# JSON validity
# --------------------------------------------------------------------------- #

def test_json_validity_basic() -> None:
    assert is_valid_json('{"a": 1, "b": [2, 3]}')
    assert not is_valid_json('{"a": 1, "b": }')  # broken


def test_json_validity_strips_fences() -> None:
    fenced = '```json\n{"key": "value"}\n```'
    assert is_valid_json(fenced)


def test_json_validity_empty() -> None:
    assert not is_valid_json("")
    assert not is_valid_json("   ")


# --------------------------------------------------------------------------- #
# StructuralChecker — window summary + drift
# --------------------------------------------------------------------------- #

def test_structural_summary_basic() -> None:
    responses = [
        "This is a definite answer with about twenty words in it for the test.",
        "Another definite response with similar length and structure for averaging.",
        "I think perhaps this might be the case. I'm not sure but it could work.",
    ]
    ck = StructuralChecker()
    sig = ck.summarize(responses, cluster_id="test", window_name="early")
    assert sig.n_responses == 3
    assert sig.mean_words > 0
    assert sig.hedge_density > 0      # Third response is hedge-heavy
    assert sig.refusal_rate == 0.0
    assert sig.apology_rate == 0.0


def test_structural_drift_hedge_spike() -> None:
    """A clear hedge-density spike should trigger structural drift."""
    baseline_responses = ["Definite answer here with no hedging at all."] * 12
    current_responses = [
        "I think perhaps this might work but I'm not sure. Maybe.",
    ] * 12
    ck = StructuralChecker(hedge_density_threshold=0.5)
    base = ck.summarize(baseline_responses, window_name="early")
    cur = ck.summarize(current_responses, window_name="late")
    drift = ck.compare(
        baseline=base, current=cur,
        baseline_responses=baseline_responses,
        current_responses=current_responses,
    )
    assert drift.triggered
    assert any("hedge_density" in s for s in drift.triggered_signals)


def test_structural_drift_refusal_spike() -> None:
    baseline_responses = ["Here is the answer with substantive content for testing."] * 12
    current_responses = ["I can't help with that. Please rephrase."] * 12
    ck = StructuralChecker(refusal_rate_threshold=0.05)
    base = ck.summarize(baseline_responses, window_name="early")
    cur = ck.summarize(current_responses, window_name="late")
    drift = ck.compare(
        baseline=base, current=cur,
        baseline_responses=baseline_responses,
        current_responses=current_responses,
    )
    assert drift.triggered
    assert any("refusal_rate" in s for s in drift.triggered_signals)


def test_structural_drift_length_jump() -> None:
    baseline_responses = ["short answer"] * 12
    current_responses = [(" " + "verbose").strip() + " " + "word " * 200] * 12
    ck = StructuralChecker(length_wasserstein_threshold=20.0)
    base = ck.summarize(baseline_responses, window_name="early")
    cur = ck.summarize(current_responses, window_name="late")
    drift = ck.compare(
        baseline=base, current=cur,
        baseline_responses=baseline_responses,
        current_responses=current_responses,
    )
    assert drift.triggered
    assert any("length_wasserstein" in s for s in drift.triggered_signals)


def test_structural_drift_no_change() -> None:
    same = ["A measured, definite response of moderate length and clear content."] * 12
    ck = StructuralChecker()
    base = ck.summarize(same, window_name="early")
    cur = ck.summarize(same, window_name="late")
    drift = ck.compare(
        baseline=base, current=cur,
        baseline_responses=same, current_responses=same,
    )
    assert not drift.triggered


# --------------------------------------------------------------------------- #
# Word counting — must work for CJK / no-whitespace scripts
# --------------------------------------------------------------------------- #

def test_word_count_latin_matches_split() -> None:
    assert _word_count("hello world foo") == 3
    assert _word_count("") == 0


def test_word_count_cjk_proportional_to_length() -> None:
    """CJK text has no spaces, so str.split() returns 1 for an entire sentence.
    _word_count must count ideographs/kana so length-drift stays measurable."""
    cjk = "今天天气很好啊朋友"          # 9 Han ideographs, no whitespace
    assert cjk.split() == [cjk]            # sanity: split() collapses to 1
    assert _word_count(cjk) > 1
    # 1 whitespace token (the whole run) + 9 ideographs.
    assert _word_count(cjk) == 1 + len(cjk)

    # Hiragana + Katakana also count.
    kana = "これはテストです"
    assert _word_count(kana) > 1


def test_word_count_mixed_script() -> None:
    """Whitespace tokens PLUS CJK characters. The CJK run is also one
    whitespace token, so it contributes both — fine, since the point is that
    CJK length is reflected at all."""
    mixed = "hello 世界 world"            # 3 split tokens + 2 Han chars
    assert _word_count(mixed) == 5
    # Strictly greater than the whitespace-only count, which is the bug fix.
    assert _word_count(mixed) > len(mixed.split())


def test_cjk_length_drift_is_detectable() -> None:
    """End-to-end: a CJK length jump must trigger length_wasserstein drift,
    which the old str.split() word count would have missed."""
    baseline_responses = ["短"] * 12                      # 1 char each
    current_responses = ["今天天气很好啊朋友们大家好呀" * 3] * 12   # long CJK
    ck = StructuralChecker(length_wasserstein_threshold=20.0)
    base = ck.summarize(baseline_responses, window_name="early")
    cur = ck.summarize(current_responses, window_name="late")
    assert cur.mean_words > base.mean_words
    drift = ck.compare(
        baseline=base, current=cur,
        baseline_responses=baseline_responses,
        current_responses=current_responses,
    )
    assert drift.triggered
    assert any("length_wasserstein" in s for s in drift.triggered_signals)


def test_structural_summary_json_validity() -> None:
    responses = [
        '{"valid": true}',
        '{"also": "valid"}',
        'not json at all, just text',
        '```json\n{"fenced": "still valid"}\n```',
    ]
    ck = StructuralChecker(expect_json=True)
    sig = ck.summarize(responses, window_name="api_responses")
    assert sig.json_validity_rate == 0.75      # 3 of 4 valid
