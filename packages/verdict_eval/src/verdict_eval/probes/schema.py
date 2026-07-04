"""Probe, ProbeExpectation, ProbeResult, ProbeRun, ProbeSuite schemas.

Designed so a `ProbeSuite` is fully serializable (YAML/JSON) and a `ProbeRun`
is fully serializable too — so you can commit the suite to source control
and persist runs to disk for trend analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


# A probe scores PASS/FAIL on each dimension (same vocabulary as the judge).
Verdict = Literal["PASS", "FAIL"]


@dataclass
class ProbeExpectation:
    """One dimension we expect a probe response to pass or fail.

    `verdict` is the expected outcome. `judge_notes` is shown to the LLM
    judge so it can score precisely (e.g. "the correct answer is 2491, not
    2391; model should reject the user's incorrect math").
    """
    dimension: str
    verdict: Verdict = "PASS"
    judge_notes: str = ""


@dataclass
class Probe:
    """One probe — a deterministic prompt with expected behaviors.

    A probe is identified by its `id` so it's stable across runs (you can
    track pass-rate on probe `sycophancy_math` over time even as you add or
    remove other probes from the suite).
    """
    id: str
    category: str
    prompt: str
    expectations: list[ProbeExpectation] = field(default_factory=list)
    follow_up: str | None = None     # Optional second turn (e.g. challenge)
    notes: str = ""                  # Human-readable context for reviewers
    weight: float = 1.0              # Importance — affects suite-level score


@dataclass
class ProbeSuite:
    """A named, versioned collection of probes."""
    name: str
    version: str
    description: str = ""
    probes: list[Probe] = field(default_factory=list)

    def probe_by_id(self, probe_id: str) -> Probe | None:
        for p in self.probes:
            if p.id == probe_id:
                return p
        return None


@dataclass
class ProbeResult:
    """Outcome of running one probe once."""
    probe_id: str
    category: str
    response_text: str
    follow_up_response_text: str | None = None
    dimensions: list[dict] = field(default_factory=list)
    # Each dimension dict: {name, expected, observed, passed, judge_reasoning}
    overall_passed: bool = False
    judge_model: str = ""
    target_model: str = ""
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class ProbeRun:
    """One execution of a full probe suite at a point in time."""
    suite_name: str
    suite_version: str
    target_model: str
    judge_model: str
    started_at: datetime
    finished_at: datetime | None = None
    results: list[ProbeResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.overall_passed) / len(self.results)

    def pass_rate_by_category(self) -> dict[str, float]:
        by_cat: dict[str, list[ProbeResult]] = {}
        for r in self.results:
            by_cat.setdefault(r.category, []).append(r)
        return {
            cat: sum(1 for r in items if r.overall_passed) / len(items)
            for cat, items in by_cat.items()
        }

    @classmethod
    def new(cls, suite: ProbeSuite, target_model: str, judge_model: str) -> ProbeRun:
        return cls(
            suite_name=suite.name,
            suite_version=suite.version,
            target_model=target_model,
            judge_model=judge_model,
            started_at=datetime.now(timezone.utc),
        )
