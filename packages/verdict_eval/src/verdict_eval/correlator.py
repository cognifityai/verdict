"""Layer 5 — user-signal correlator.

Joins explicit user signals (thumbs up/down, regenerate, abandon, copy,
follow-up retry) with the judge's PASS/FAIL verdict per trace. Tells you
whether the judge actually predicts what users care about.

Why this layer matters: even a generally calibrated LLM judge can be
systematically miscalibrated for a specific workload. If users
consistently thumb-down responses the judge calls PASS, the judge is
under-fitting on something workload-specific (tone, length, domain
vocabulary). The correlator surfaces those disagreement cases and gives
you an agreement metric (Cohen's κ — same statistic we use for judge
alignment in `verify_judge_alignment.py`).

Storage contract: caller delivers `CorrelationPair` records. The correlator
doesn't fetch from storage itself; that's the SDK / pipeline's job. It accepts
at most one usable binary observation per trace; unusable signals and UNCLEAR
judge results are counted as coverage skips but do not consume that trace's
duplicate slot.

Output: `CorrelationReport` with:
  - n_pairs total
  - judge_pos_user_pos / judge_pos_user_neg / judge_neg_user_pos / judge_neg_user_neg
  - Cohen's κ (paradox-vulnerable; reported alongside Gwet's AC2)
  - Gwet's AC2 (paradox-corrected, more honest on skewed marginals)
  - Top disagreement examples (truncated text)

User signal interpretation:
  - thumbs_up / copy / accept → positive
  - thumbs_down / regenerate / abandon / retry → negative
  - no_signal / follow_up_question → skipped (no usable label)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Literal

from verdict.metrics import verdict_label

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

JudgeVerdict = Literal["PASS", "FAIL", "UNCLEAR"]
UserSignalKind = Literal[
    "thumbs_up", "thumbs_down",
    "copy", "regenerate", "retry", "abandon", "accept",
    "follow_up_question", "no_signal",
]


# These map directly to binary positive / negative
_POSITIVE_SIGNALS = {"thumbs_up", "copy", "accept"}
_NEGATIVE_SIGNALS = {"thumbs_down", "regenerate", "retry", "abandon"}
# Anything not in these sets is treated as "no usable label"


def user_signal_polarity(signal: UserSignalKind) -> Literal["POS", "NEG", "NA"]:
    if signal in _POSITIVE_SIGNALS:
        return "POS"
    if signal in _NEGATIVE_SIGNALS:
        return "NEG"
    return "NA"


def _judge_polarity(verdict: object) -> Literal["POS", "NEG", "NA"]:
    """Map only scored verdicts to binary polarity; keep UNCLEAR unscored."""
    label = verdict_label(verdict)
    if label == "PASS":
        return "POS"
    if label == "FAIL":
        return "NEG"
    return "NA"


@dataclass
class CorrelationPair:
    """One (trace_id, judge_verdict, user_signal) record.

    `prompt_preview` and `response_preview` are short truncated strings for
    inclusion in disagreement examples; full content stays in storage.
    """
    trace_id: str
    judge_verdict: JudgeVerdict
    user_signal: UserSignalKind
    prompt_preview: str = ""
    response_preview: str = ""


@dataclass
class CorrelationReport:
    n_pairs: int = 0
    n_skipped_no_label: int = 0

    # Confusion matrix (judge x user)
    judge_pos_user_pos: int = 0
    judge_pos_user_neg: int = 0
    judge_neg_user_pos: int = 0
    judge_neg_user_neg: int = 0

    # Agreement metrics
    raw_agreement: float = 0.0      # (TP + TN) / total
    cohens_kappa: float = 0.0
    gwet_ac2: float = 0.0
    judge_positive_rate: float = 0.0
    user_positive_rate: float = 0.0

    # Disagreement cases — most useful debugging output
    examples_judge_pass_user_neg: list[CorrelationPair] = field(default_factory=list)
    examples_judge_fail_user_pos: list[CorrelationPair] = field(default_factory=list)

    interpretation: str = ""

    # New fields stay after the complete 0.1.0a3 constructor so positional
    # callers retain their historical bindings.
    n_skipped_unclear_judge: int = 0
    n_skipped_duplicate_trace: int = 0
    n_skipped_conflicting_trace: int = 0
    minimum_pairs: int = 30
    status: str = "no_data"
    raw_agreement_ci_low: float | None = None
    raw_agreement_ci_high: float | None = None
    cohens_kappa_ci_low: float | None = None
    cohens_kappa_ci_high: float | None = None
    gwet_ac2_ci_low: float | None = None
    gwet_ac2_ci_high: float | None = None


# --------------------------------------------------------------------------- #
# Correlator
# --------------------------------------------------------------------------- #

@dataclass
class UserSignalCorrelator:
    """Compute a CorrelationReport from a list of CorrelationPairs.

    Parameters
    ----------
    max_examples_per_disagreement:
        How many disagreement examples to include in the report. Default 5.
    """
    max_examples_per_disagreement: int = 5
    minimum_pairs: int = 30
    bootstrap_samples: int = 1000

    def correlate(self, pairs: list[CorrelationPair]) -> CorrelationReport:
        if self.minimum_pairs < 1:
            raise ValueError("minimum_pairs must be at least 1")
        if self.bootstrap_samples < 1:
            raise ValueError("bootstrap_samples must be at least 1")
        report = CorrelationReport(minimum_pairs=self.minimum_pairs)
        observations: list[tuple[bool, bool]] = []
        usable_by_trace: dict[str, list[tuple[CorrelationPair, bool, bool]]] = {}
        for p in pairs:
            polarity = user_signal_polarity(p.user_signal)
            if polarity == "NA":
                report.n_skipped_no_label += 1
                continue
            judge_polarity = _judge_polarity(p.judge_verdict)
            if judge_polarity == "NA":
                report.n_skipped_unclear_judge += 1
                continue
            usable_by_trace.setdefault(p.trace_id, []).append(
                (p, judge_polarity == "POS", polarity == "POS")
            )

        # Resolve duplicates as a group so contradictory usable rows do not let
        # input order decide the confusion matrix. Exact duplicates collapse to
        # one observation; conflicting labels make the trace ineligible.
        for trace_id in sorted(usable_by_trace):
            candidates = usable_by_trace[trace_id]
            report.n_skipped_duplicate_trace += max(0, len(candidates) - 1)
            labels = {(judge_pos, user_pos) for _, judge_pos, user_pos in candidates}
            if len(labels) != 1:
                report.n_skipped_conflicting_trace += 1
                continue
            p, judge_pos, user_pos = min(
                candidates,
                key=lambda item: (
                    item[0].user_signal,
                    item[0].prompt_preview,
                    item[0].response_preview,
                ),
            )
            report.n_pairs += 1
            observations.append((judge_pos, user_pos))

            if judge_pos and user_pos:
                report.judge_pos_user_pos += 1
            elif judge_pos and not user_pos:
                report.judge_pos_user_neg += 1
                if len(report.examples_judge_pass_user_neg) < self.max_examples_per_disagreement:
                    report.examples_judge_pass_user_neg.append(p)
            elif (not judge_pos) and user_pos:
                report.judge_neg_user_pos += 1
                if len(report.examples_judge_fail_user_pos) < self.max_examples_per_disagreement:
                    report.examples_judge_fail_user_pos.append(p)
            else:
                report.judge_neg_user_neg += 1

        n = report.n_pairs
        if n == 0:
            report.interpretation = (
                "No usable label pairs. Need at least some explicit user "
                "signals (thumbs / regenerate / abandon) to compute agreement."
            )
            return report

        tp = report.judge_pos_user_pos
        tn = report.judge_neg_user_neg
        fp = report.judge_pos_user_neg     # judge said PASS, user disagreed
        fn = report.judge_neg_user_pos     # judge said FAIL, user accepted

        report.raw_agreement = (tp + tn) / n
        report.judge_positive_rate = (tp + fp) / n
        report.user_positive_rate = (tp + fn) / n

        # Cohen's κ — paradox-vulnerable when marginals are skewed
        p_judge_pos = report.judge_positive_rate
        p_user_pos = report.user_positive_rate
        p_e_cohen = p_judge_pos * p_user_pos + (1 - p_judge_pos) * (1 - p_user_pos)
        report.cohens_kappa = (
            (report.raw_agreement - p_e_cohen) / (1 - p_e_cohen)
            if (1 - p_e_cohen) > 1e-9 else 0.0
        )
        (
            report.cohens_kappa_ci_low,
            report.cohens_kappa_ci_high,
        ) = _bootstrap_metric_interval(
            observations,
            self.bootstrap_samples,
            _cohen_from_observations,
            report.cohens_kappa,
        )

        # Gwet's AC2 (binary) — π is mean marginal probability of "positive"
        # AC2(2|2) = (Pa - Pe) / (1 - Pe), Pe = 2 * π * (1 - π)
        pi = (p_judge_pos + p_user_pos) / 2.0
        p_e_gwet = 2.0 * pi * (1.0 - pi)
        report.gwet_ac2 = (
            (report.raw_agreement - p_e_gwet) / (1 - p_e_gwet)
            if (1 - p_e_gwet) > 1e-9 else 0.0
        )

        (
            report.raw_agreement_ci_low,
            report.raw_agreement_ci_high,
        ) = _wilson_interval(tp + tn, n)
        (
            report.gwet_ac2_ci_low,
            report.gwet_ac2_ci_high,
        ) = _bootstrap_gwet_interval(observations, self.bootstrap_samples)

        if n < self.minimum_pairs:
            report.status = "low_data"
            report.interpretation = (
                f"Low data: n={n} usable pairs is below the configured minimum "
                f"of {self.minimum_pairs}. Metrics and confidence intervals are "
                "descriptive only; do not label this judge calibrated from this run."
            )
        else:
            report.status = "ready"
            report.interpretation = _interpret(report)
        return report


def _wilson_interval(correct: int, total: int) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    z = 1.959963984540054
    observed = correct / total
    denominator = 1 + z * z / total
    center = (observed + z * z / (2 * total)) / denominator
    half_width = (
        z
        * math.sqrt(
            observed * (1 - observed) / total + z * z / (4 * total * total)
        )
        / denominator
    )
    low = 0.0 if correct == 0 else max(0.0, center - half_width)
    high = 1.0 if correct == total else min(1.0, center + half_width)
    return low, high


def _gwet_from_observations(observations: list[tuple[bool, bool]]) -> float:
    n = len(observations)
    if n == 0:
        return 0.0
    agreements = sum(judge == user for judge, user in observations)
    judge_positive = sum(judge for judge, _ in observations) / n
    user_positive = sum(user for _, user in observations) / n
    observed_agreement = agreements / n
    pi = (judge_positive + user_positive) / 2.0
    expected = 2.0 * pi * (1.0 - pi)
    return (
        (observed_agreement - expected) / (1 - expected)
        if (1 - expected) > 1e-9 else 0.0
    )


def _cohen_from_observations(observations: list[tuple[bool, bool]]) -> float:
    n = len(observations)
    if n == 0:
        return 0.0
    observed = sum(judge == user for judge, user in observations) / n
    judge_positive = sum(judge for judge, _ in observations) / n
    user_positive = sum(user for _, user in observations) / n
    expected = (
        judge_positive * user_positive
        + (1.0 - judge_positive) * (1.0 - user_positive)
    )
    return (observed - expected) / (1.0 - expected) if expected < 1.0 - 1e-9 else 0.0


def _bootstrap_metric_interval(
    observations: list[tuple[bool, bool]],
    samples: int,
    metric,
    point_estimate: float,
) -> tuple[float | None, float | None]:
    if not observations:
        return None, None
    rng = random.Random(0)  # nosec B311
    n = len(observations)
    estimates = sorted(
        metric([observations[rng.randrange(n)] for _ in range(n)])
        for _ in range(samples)
    )
    low_index = max(0, math.floor(0.025 * (samples - 1)))
    high_index = min(samples - 1, math.ceil(0.975 * (samples - 1)))
    return (
        min(point_estimate, estimates[low_index]),
        max(point_estimate, estimates[high_index]),
    )


def _bootstrap_gwet_interval(
    observations: list[tuple[bool, bool]], samples: int,
) -> tuple[float | None, float | None]:
    return _bootstrap_metric_interval(
        observations,
        samples,
        _gwet_from_observations,
        _gwet_from_observations(observations),
    )


def _interpret(report: CorrelationReport) -> str:
    """Plain-language summary of the calibration state."""
    kappa = report.gwet_ac2  # use AC2 — more honest on skewed marginals
    judge_positive = report.judge_pos_user_pos + report.judge_pos_user_neg
    judge_negative = report.judge_neg_user_pos + report.judge_neg_user_neg
    fp_rate = (
        report.judge_pos_user_neg / judge_positive if judge_positive else 0.0
    )
    fn_rate = (
        report.judge_neg_user_pos / judge_negative if judge_negative else 0.0
    )

    if kappa >= 0.80:
        agreement_word = "excellent"
    elif kappa >= 0.60:
        agreement_word = "substantial"
    elif kappa >= 0.40:
        agreement_word = "moderate"
    elif kappa >= 0.20:
        agreement_word = "fair"
    else:
        agreement_word = "poor"

    lines = [
        f"Judge-user agreement is {agreement_word} (Gwet's AC2 = {kappa:.2f}).",
    ]
    if fp_rate > 0.10:
        lines.append(
            f"Judge appears lenient: {fp_rate:.0%} of responses the judge called PASS "
            "were rejected by users (thumbs down / regenerate / abandon). "
            "Tighten rubric on whatever the judge is missing."
        )
    if fn_rate > 0.10:
        lines.append(
            f"Judge appears strict: {fn_rate:.0%} of responses the judge called FAIL "
            "were accepted by users (thumbs up / copy). "
            "Rubric may be too strict for this workload."
        )
    if fp_rate <= 0.10 and fn_rate <= 0.10:
        lines.append(
            "Disagreement is balanced and low in this sample; continue monitoring "
            "against independent human labels."
        )
    return " ".join(lines)
