"""Tests for the user-signal correlator (Layer 5)."""

from __future__ import annotations

from dataclasses import fields

from verdict_eval.correlator import (
    CorrelationPair,
    CorrelationReport,
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


def test_report_appends_new_fields_after_published_constructor() -> None:
    published_fields = [
        "n_pairs", "n_skipped_no_label", "judge_pos_user_pos",
        "judge_pos_user_neg", "judge_neg_user_pos", "judge_neg_user_neg",
        "raw_agreement", "cohens_kappa", "gwet_ac2", "judge_positive_rate",
        "user_positive_rate", "examples_judge_pass_user_neg",
        "examples_judge_fail_user_pos", "interpretation",
    ]

    assert [field.name for field in fields(CorrelationReport)[:14]] == published_fields


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
    assert r.status == "ready"
    assert r.raw_agreement_ci_low is not None
    assert r.raw_agreement_ci_high == 1.0
    assert r.gwet_ac2_ci_low == r.gwet_ac2_ci_high == 1.0


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


def test_unclear_judge_verdicts_are_not_collapsed_into_failures() -> None:
    pairs = [
        CorrelationPair(
            trace_id=f"unclear-{index}",
            judge_verdict="UNCLEAR",
            user_signal="thumbs_up",
        )
        for index in range(40)
    ]

    report = UserSignalCorrelator().correlate(pairs)

    assert report.n_pairs == 0
    assert report.n_skipped_unclear_judge == 40
    assert report.judge_neg_user_pos == 0
    assert report.raw_agreement == 0.0
    assert report.status == "no_data"

    # The shared verdict contract normalizes missing/malformed stored values to
    # UNCLEAR too; neither may become a synthetic FAIL disagreement.
    for invalid_verdict in (None, "not-a-verdict"):
        malformed_report = UserSignalCorrelator().correlate([
            CorrelationPair(
                trace_id=f"invalid-{invalid_verdict}",
                judge_verdict=invalid_verdict,  # type: ignore[arg-type]
                user_signal="thumbs_up",
            )
        ])
        assert malformed_report.n_pairs == 0
        assert malformed_report.n_skipped_unclear_judge == 1
        assert malformed_report.judge_neg_user_pos == 0


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
    r = UserSignalCorrelator(
        max_examples_per_disagreement=3, minimum_pairs=20
    ).correlate(pairs)
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
    assert -1 <= r.gwet_ac2_ci_low <= r.gwet_ac2_ci_high <= 1


def test_low_sample_report_is_explicitly_not_calibrated() -> None:
    pairs = [
        CorrelationPair(
            trace_id=f"t-{index}",
            judge_verdict="PASS",
            user_signal="thumbs_up",
        )
        for index in range(5)
    ]

    report = UserSignalCorrelator(minimum_pairs=30).correlate(pairs)

    assert report.n_pairs == 5
    assert report.status == "low_data"
    assert report.raw_agreement_ci_low is not None
    assert report.gwet_ac2_ci_low is not None
    assert "do not label this judge calibrated" in report.interpretation


def test_degenerate_all_negative_data_has_bounded_uncertainty() -> None:
    pairs = [
        CorrelationPair(
            trace_id=f"t-{index}",
            judge_verdict="FAIL",
            user_signal="thumbs_down",
        )
        for index in range(30)
    ]

    report = UserSignalCorrelator().correlate(pairs)

    assert report.status == "ready"
    assert report.raw_agreement == 1.0
    assert 0 <= report.raw_agreement_ci_low <= report.raw_agreement_ci_high <= 1
    assert -1 <= report.gwet_ac2_ci_low <= report.gwet_ac2_ci_high <= 1


def test_duplicate_trace_signals_do_not_pseudoreplicate_confidence_sample() -> None:
    pairs = [
        CorrelationPair(
            trace_id="same-trace",
            judge_verdict="PASS",
            user_signal="thumbs_down",
        ),
        CorrelationPair(
            trace_id="same-trace",
            judge_verdict="PASS",
            user_signal="regenerate",
        ),
        CorrelationPair(
            trace_id="other-trace",
            judge_verdict="PASS",
            user_signal="thumbs_up",
        ),
    ]

    report = UserSignalCorrelator().correlate(pairs)

    assert report.n_pairs == 2
    assert report.n_skipped_duplicate_trace == 1
    assert report.judge_pos_user_neg == 1
    assert report.judge_pos_user_pos == 1


def test_contradictory_usable_duplicates_are_excluded_order_independently() -> None:
    positive = CorrelationPair("same-trace", "PASS", "thumbs_up")
    negative = CorrelationPair("same-trace", "PASS", "thumbs_down")

    first = UserSignalCorrelator().correlate([positive, negative])
    second = UserSignalCorrelator().correlate([negative, positive])

    assert first == second
    assert first.n_pairs == 0
    assert first.n_skipped_conflicting_trace == 1
    assert first.n_skipped_duplicate_trace == 1
    assert first.status == "no_data"


def test_interpretation_uses_conditional_false_positive_denominator() -> None:
    pairs = [CorrelationPair("only-pass", "PASS", "thumbs_down")]
    pairs.extend(
        CorrelationPair(f"negative-{index}", "FAIL", "thumbs_down")
        for index in range(99)
    )

    report = UserSignalCorrelator(minimum_pairs=1).correlate(pairs)

    assert "appears lenient" in report.interpretation
    assert "100% of responses the judge called PASS" in report.interpretation


def test_cohens_kappa_has_deterministic_bounded_bootstrap_interval() -> None:
    pairs = (
        [CorrelationPair(f"pp-{i}", "PASS", "thumbs_up") for i in range(35)]
        + [CorrelationPair(f"pn-{i}", "PASS", "thumbs_down") for i in range(5)]
        + [CorrelationPair(f"np-{i}", "FAIL", "thumbs_up") for i in range(10)]
        + [CorrelationPair(f"nn-{i}", "FAIL", "thumbs_down") for i in range(50)]
    )

    first = UserSignalCorrelator(bootstrap_samples=200).correlate(pairs)
    second = UserSignalCorrelator(bootstrap_samples=200).correlate(pairs)

    assert first.cohens_kappa_ci_low == second.cohens_kappa_ci_low
    assert first.cohens_kappa_ci_high == second.cohens_kappa_ci_high
    assert -1 <= first.cohens_kappa_ci_low <= first.cohens_kappa
    assert first.cohens_kappa <= first.cohens_kappa_ci_high <= 1


def test_unusable_row_does_not_consume_a_trace_duplicate_slot() -> None:
    """Correlation must not depend on whether an unusable row appears first."""
    unusable_cases = [
        (
            CorrelationPair("same-trace", "PASS", "no_signal"),
            "n_skipped_no_label",
        ),
        (
            CorrelationPair("same-trace", "UNCLEAR", "thumbs_up"),
            "n_skipped_unclear_judge",
        ),
    ]
    for unusable, skip_field in unusable_cases:
        usable = CorrelationPair("same-trace", "PASS", "thumbs_up")
        reports = [
            UserSignalCorrelator().correlate(order)
            for order in ([unusable, usable], [usable, unusable])
        ]

        for report in reports:
            assert report.n_pairs == 1
            assert getattr(report, skip_field) == 1
            assert report.n_skipped_duplicate_trace == 0
            assert report.judge_pos_user_pos == 1


def test_empty_pairs() -> None:
    r = UserSignalCorrelator().correlate([])
    assert r.n_pairs == 0
    assert r.status == "no_data"
    assert "No usable label pairs" in r.interpretation
