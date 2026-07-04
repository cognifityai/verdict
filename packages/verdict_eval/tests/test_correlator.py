"""Tests for the user-signal correlator (Layer 5)."""

from __future__ import annotations

from verdict_eval.correlator import (
    CorrelationPair,
    UserSignalCorrelator,
    user_signal_polarity,
)


def test_signal_polarity() -> None:
    assert user_signal_polarity("thumbs_up") == "POS"
    assert user_signal_polarity("copy") == "POS"
    assert user_signal_polarity("thumbs_down") == "NEG"
    assert user_signal_polarity("regenerate") == "NEG"
    assert user_signal_polarity("abandon") == "NEG"
    assert user_signal_polarity("no_signal") == "NA"
    assert user_signal_polarity("follow_up_question") == "NA"


def test_perfect_agreement() -> None:
    pairs = (
        [CorrelationPair(trace_id=f"p-{i}", judge_verdict="PASS",
                         user_signal="thumbs_up") for i in range(50)]
        + [CorrelationPair(trace_id=f"n-{i}", judge_verdict="FAIL",
                           user_signal="thumbs_down") for i in range(50)]
    )
    r = UserSignalCorrelator().correlate(pairs)
    assert r.n_pairs == 100
    assert r.raw_agreement == 1.0
    assert r.cohens_kappa > 0.95
    assert r.gwet_ac2 > 0.95


def test_perfect_disagreement() -> None:
    pairs = (
        [CorrelationPair(trace_id=f"p-{i}", judge_verdict="PASS",
                         user_signal="thumbs_down") for i in range(50)]
        + [CorrelationPair(trace_id=f"n-{i}", judge_verdict="FAIL",
                           user_signal="thumbs_up") for i in range(50)]
    )
    r = UserSignalCorrelator().correlate(pairs)
    assert r.raw_agreement == 0.0
    assert r.cohens_kappa < -0.5
    assert r.judge_pos_user_neg == 50
    assert r.judge_neg_user_pos == 50


def test_skips_no_label_pairs() -> None:
    pairs = [
        CorrelationPair(trace_id="a", judge_verdict="PASS", user_signal="follow_up_question"),
        CorrelationPair(trace_id="b", judge_verdict="PASS", user_signal="no_signal"),
        CorrelationPair(trace_id="c", judge_verdict="PASS", user_signal="thumbs_up"),
    ]
    r = UserSignalCorrelator().correlate(pairs)
    assert r.n_pairs == 1
    assert r.n_skipped_no_label == 2


def test_lenient_judge_surfaces_examples() -> None:
    """Judge calls everything PASS; users mostly disagree."""
    pairs = [
        CorrelationPair(trace_id=f"t-{i}", judge_verdict="PASS",
                        user_signal="regenerate",
                        prompt_preview=f"prompt {i}",
                        response_preview=f"bad response {i}")
        for i in range(20)
    ] + [
        CorrelationPair(trace_id=f"ok-{i}", judge_verdict="PASS",
                        user_signal="thumbs_up")
        for i in range(5)
    ]
    r = UserSignalCorrelator(max_examples_per_disagreement=3).correlate(pairs)
    assert r.judge_pos_user_neg == 20
    assert r.judge_pos_user_pos == 5
    assert len(r.examples_judge_pass_user_neg) == 3
    assert r.gwet_ac2 < 0.5
    assert "appears lenient" in r.interpretation


def test_gwet_ac2_on_skewed_marginals() -> None:
    """When most pairs are PASS / thumbs_up, Cohen's κ tanks but AC2 stays
    high — the canonical kappa-paradox case. This proves the correlator
    reports both."""
    pairs = (
        [CorrelationPair(trace_id=f"p-{i}", judge_verdict="PASS",
                         user_signal="thumbs_up") for i in range(95)]
        + [CorrelationPair(trace_id=f"d-{i}", judge_verdict="PASS",
                           user_signal="thumbs_down") for i in range(5)]
    )
    r = UserSignalCorrelator().correlate(pairs)
    # Skewed marginals make Cohen's κ near zero (paradox) even at 95% agreement
    assert r.raw_agreement == 0.95
    # AC2 should be much higher than κ here
    assert r.gwet_ac2 > r.cohens_kappa


def test_empty_pairs() -> None:
    r = UserSignalCorrelator().correlate([])
    assert r.n_pairs == 0
    assert "No usable label pairs" in r.interpretation
