"""Probe, ProbeExpectation, ProbeResult, ProbeRun, ProbeSuite schemas.

Designed so a `ProbeSuite` is fully serializable (YAML/JSON) and a `ProbeRun`
is fully serializable too — so you can commit the suite to source control
and persist runs to disk for trend analysis.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

PROBE_METRIC_SCHEMA_VERSION = "2"
PROBE_JUDGE_METHOD_VERSION = "2"
LEGACY_PROBE_METRIC_SCHEMA_VERSION = "1"
LEGACY_PROBE_JUDGE_METHOD_VERSION = "1"

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

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, str) or not self.dimension.strip():
            raise ValueError("probe expectation dimension must be a non-empty string")
        if self.verdict not in ("PASS", "FAIL"):
            raise ValueError("probe expectation verdict must be PASS or FAIL")


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

    def __post_init__(self) -> None:
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ValueError("probe weight must be a finite number greater than zero")
        if not self.expectations:
            raise ValueError("probe must define at least one expectation")


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
    weight: float = 1.0
    # Backward defaults are deliberately v1: constructing from a historical
    # artifact that lacks these keys must never relabel it as current.
    metric_schema_version: str = LEGACY_PROBE_METRIC_SCHEMA_VERSION
    judge_method_version: str = LEGACY_PROBE_JUDGE_METHOD_VERSION


def _aggregation_weight(result: ProbeResult) -> float:
    """Return a safe weight for current and historical result artifacts."""
    return (
        result.weight
        if math.isfinite(result.weight) and result.weight > 0
        else 0.0
    )


@dataclass
class ProbeRun:
    """One execution of a full probe suite at a point in time."""
    suite_name: str
    suite_version: str
    target_model: str
    judge_model: str
    started_at: datetime
    # Keep these two fields in their historical positional slots. Public
    # callers may still construct ProbeRun with the pre-versioning signature.
    finished_at: datetime | None = None
    results: list[ProbeResult] = field(default_factory=list)
    # See ProbeResult: only ProbeRun.new stamps the current versions.
    metric_schema_version: str = LEGACY_PROBE_METRIC_SCHEMA_VERSION
    judge_method_version: str = LEGACY_PROBE_JUDGE_METHOD_VERSION

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        total_weight = sum(_aggregation_weight(result) for result in self.results)
        if total_weight <= 0:
            return 0.0
        return sum(
            _aggregation_weight(result)
            for result in self.results
            if result.overall_passed
        ) / total_weight

    def pass_rate_by_category(self) -> dict[str, float]:
        by_cat: dict[str, list[ProbeResult]] = {}
        for r in self.results:
            by_cat.setdefault(r.category, []).append(r)
        rates: dict[str, float] = {}
        for category, items in by_cat.items():
            total_weight = sum(_aggregation_weight(result) for result in items)
            rates[category] = (
                sum(
                    _aggregation_weight(result)
                    for result in items
                    if result.overall_passed
                )
                / total_weight
                if total_weight > 0
                else 0.0
            )
        return rates

    def pass_rate_by_dimension(self) -> dict[str, float]:
        """Return probe-weighted expectation agreement for each dimension."""
        passed_weight: dict[str, float] = {}
        total_weight: dict[str, float] = {}
        for result in self.results:
            for dimension in result.dimensions:
                name = str(dimension.get("name", ""))
                if not name:
                    continue
                weight = _aggregation_weight(result)
                total_weight[name] = total_weight.get(name, 0.0) + weight
                if dimension.get("passed") is True:
                    passed_weight[name] = passed_weight.get(name, 0.0) + weight
        return {
            name: passed_weight.get(name, 0.0) / weight
            for name, weight in total_weight.items()
            if weight > 0
        }

    @classmethod
    def new(cls, suite: ProbeSuite, target_model: str, judge_model: str) -> ProbeRun:
        return cls(
            suite_name=suite.name,
            suite_version=suite.version,
            target_model=target_model,
            judge_model=judge_model,
            started_at=datetime.now(timezone.utc),
            metric_schema_version=PROBE_METRIC_SCHEMA_VERSION,
            judge_method_version=PROBE_JUDGE_METHOD_VERSION,
        )
