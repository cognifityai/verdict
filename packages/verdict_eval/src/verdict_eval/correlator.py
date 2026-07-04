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

Storage contract: caller delivers `CorrelationPair` records — one per
trace where both a judge verdict and a user signal exist. The correlator
doesn't fetch from storage itself; that's the SDK / pipeline's job.

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

from dataclasses import dataclass, field
from typing import Literal


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

JudgeVerdict = Literal["PASS", "FAIL"]
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


def _judge_is_pass(verdict: object) -> bool:
    """Case-insensitive PASS check that also tolerates a Verdict enum.

    The contract is an uppercase "PASS"/"FAIL" string, but the Verdict enum
    stores lowercase values ("pass"), so a caller that passes a DimensionScore
    verdict straight through would otherwise have every comparison read as FAIL.
    Mirrors verdict_eval.judge.verdict_is_pass without the import cost.
    """
    return str(getattr(verdict, "value", verdict)).upper() == "PASS"


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

    # Confusion matrix (judge × user)
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

    def correlate(self, pairs: list[CorrelationPair]) -> CorrelationReport:
        report = CorrelationReport()
        for p in pairs:
            polarity = user_signal_polarity(p.user_signal)
            if polarity == "NA":
                report.n_skipped_no_label += 1
                continue
            report.n_pairs += 1
            judge_pos = _judge_is_pass(p.judge_verdict)
            user_pos = polarity == "POS"

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

        # Gwet's AC2 (binary) — π is mean marginal probability of "positive"
        # AC2(2|2) = (Pa - Pe) / (1 - Pe), Pe = 2 * π * (1 - π)
        pi = (p_judge_pos + p_user_pos) / 2.0
        p_e_gwet = 2.0 * pi * (1.0 - pi)
        report.gwet_ac2 = (
            (report.raw_agreement - p_e_gwet) / (1 - p_e_gwet)
            if (1 - p_e_gwet) > 1e-9 else 0.0
        )

        report.interpretation = _interpret(report)
        return report


def _interpret(report: CorrelationReport) -> str:
    """Plain-language summary of the calibration state."""
    kappa = report.gwet_ac2  # use AC2 — more honest on skewed marginals
    fp_rate = report.judge_pos_user_neg / max(report.n_pairs, 1)
    fn_rate = report.judge_neg_user_pos / max(report.n_pairs, 1)

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
        f"Judge–user agreement is {agreement_word} (Gwet's AC2 = {kappa:.2f}).",
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
        lines.append("Disagreement is balanced and low — judge is well-calibrated for this workload.")
    return " ".join(lines)
