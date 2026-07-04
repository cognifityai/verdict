"""Regression test for the Verdict enum case-mismatch bug.

The Verdict enum stores lowercase values (Verdict.PASS == "pass"). Several
judge consumers naively compared `d.verdict == "PASS"` (uppercase), which is
always False — this silently zeroed out judge pass-rates in the internal validation run.
verdict_is_pass() is the canonical fix; this test locks the behavior down.
"""

from __future__ import annotations

from verdict.schema import Verdict

from verdict_eval.judge import verdict_is_pass, verdict_label


def test_enum_pass_is_pass() -> None:
    assert verdict_is_pass(Verdict.PASS) is True


def test_enum_fail_is_not_pass() -> None:
    assert verdict_is_pass(Verdict.FAIL) is False


def test_enum_unclear_is_not_pass() -> None:
    assert verdict_is_pass(Verdict.UNCLEAR) is False


def test_raw_string_uppercase() -> None:
    assert verdict_is_pass("PASS") is True
    assert verdict_is_pass("FAIL") is False


def test_raw_string_lowercase() -> None:
    assert verdict_is_pass("pass") is True
    assert verdict_is_pass("fail") is False


def test_verdict_label_three_way() -> None:
    """verdict_label normalizes to PASS/FAIL/UNCLEAR so callers can exclude
    UNCLEAR from pass-rate denominators."""
    assert verdict_label(Verdict.PASS) == "PASS"
    assert verdict_label(Verdict.FAIL) == "FAIL"
    assert verdict_label(Verdict.UNCLEAR) == "UNCLEAR"
    assert verdict_label("pass") == "PASS"
    assert verdict_label("UNCLEAR") == "UNCLEAR"


def test_unclear_excluded_from_pass_rate() -> None:
    """A dimension the judge can't evaluate (all UNCLEAR) yields no applicable
    judgments, so pass rate is None — not a misleading 0%."""
    labels = [verdict_label(v) for v in (Verdict.UNCLEAR, Verdict.UNCLEAR)]
    applicable = sum(1 for l in labels if l in ("PASS", "FAIL"))
    passes = sum(1 for l in labels if l == "PASS")
    rate = (passes / applicable) if applicable else None
    assert rate is None


def test_the_original_bug_would_have_been_caught() -> None:
    """The exact comparison that was broken in production. If someone reverts
    to `Verdict.PASS == "PASS"`, this documents why that's wrong."""
    # The naive comparison is False — this is the bug:
    assert (Verdict.PASS == "PASS") is False
    # The enum's underlying value is lowercase:
    assert Verdict.PASS.value == "pass"
    # The helper handles it correctly:
    assert verdict_is_pass(Verdict.PASS) is True
