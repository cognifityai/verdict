"""Shared PASS/FAIL/UNCLEAR denominator semantics."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


def verdict_label(value: object) -> str:
    """Normalize enum and string verdicts to PASS, FAIL, or UNCLEAR.

    Unknown, missing, and malformed values are deliberately UNCLEAR. This is
    the only normalization contract used by core, eval, inspect, probes, and
    dashboard aggregation.
    """
    label = str(getattr(value, "value", value)).strip().upper()
    return label if label in {"PASS", "FAIL", "UNCLEAR"} else "UNCLEAR"


@dataclass(frozen=True)
class ScoreCounts:
    passed: int = 0
    failed: int = 0
    unclear: int = 0
    missing: int = 0
    errors: int = 0

    @property
    def evaluable(self) -> int:
        return self.passed + self.failed

    @property
    def observed(self) -> int:
        return self.evaluable + self.unclear

    @property
    def pass_rate(self) -> float | None:
        return self.passed / self.evaluable if self.evaluable else None

    @property
    def evaluability_rate(self) -> float | None:
        total = self.observed + self.missing + self.errors
        return self.evaluable / total if total else None


def count_scores(
    verdicts: Iterable[object],
    *,
    missing: int = 0,
    errors: int = 0,
) -> ScoreCounts:
    passed = failed = unclear = 0
    for verdict in verdicts:
        label = verdict_label(verdict)
        if label == "PASS":
            passed += 1
        elif label == "FAIL":
            failed += 1
        else:
            unclear += 1
    return ScoreCounts(
        passed=passed,
        failed=failed,
        unclear=unclear,
        missing=missing,
        errors=errors,
    )
