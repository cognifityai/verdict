"""Count-based historical analysis over independent trace sessions."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from statistics import fmean, median

import numpy as np
from scipy.stats import mannwhitneyu, wilcoxon
from verdict.schema import Trace

from verdict_eval.clustering import HashingEmbedder
from verdict_eval.drift import _benjamini_hochberg
from verdict_eval.stable_clustering import ClusterRegistry, StableIntentClusterer
from verdict_eval.structural import _word_count, is_refusal


class AnalysisStatus(str, Enum):
    DRIFT_DETECTED = "drift_detected"
    NO_DRIFT_DETECTED = "no_drift_detected"
    LOW_POWER = "low_power"
    NOT_EVALUABLE = "not_evaluable"
    COLLECTING_BASELINE = "collecting_baseline"


@dataclass(frozen=True, slots=True)
class ScopeKey:
    tenant_id: str | None
    workload: str
    granularity: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "tenant_id": self.tenant_id,
            "workload": self.workload,
            "granularity": self.granularity,
        }


@dataclass(frozen=True, slots=True)
class HistoryUnit:
    unit_id: str
    event_time: datetime
    traces: tuple[Trace, ...]


@dataclass(frozen=True, slots=True)
class HistoryPlan:
    baseline: tuple[HistoryUnit, ...]
    current: tuple[HistoryUnit, ...]
    excluded_middle: HistoryUnit | None = None


@dataclass(frozen=True, slots=True)
class MetricResult:
    cluster_id: str
    metric: str
    status: AnalysisStatus
    baseline_n: int
    current_n: int
    baseline_value: float
    current_value: float
    effect_size: float
    confidence_low: float
    confidence_high: float
    p_value: float
    p_value_adjusted: float

    def to_dict(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "metric": self.metric,
            "status": self.status.value,
            "baseline_n": self.baseline_n,
            "current_n": self.current_n,
            "baseline_value": self.baseline_value,
            "current_value": self.current_value,
            "effect_size": self.effect_size,
            "confidence_low": self.confidence_low,
            "confidence_high": self.confidence_high,
            "p_value": self.p_value,
            "p_value_adjusted": self.p_value_adjusted,
        }


@dataclass(frozen=True, slots=True)
class ScopeReport:
    scope: ScopeKey
    status: AnalysisStatus
    baseline_units: int
    current_units: int
    baseline_traces: int
    current_traces: int
    excluded_middle_units: int
    tested_hypotheses: int
    results: tuple[MetricResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_dict(),
            "status": self.status.value,
            "baseline_units": self.baseline_units,
            "current_units": self.current_units,
            "baseline_traces": self.baseline_traces,
            "current_traces": self.current_traces,
            "excluded_middle_units": self.excluded_middle_units,
            "tested_hypotheses": self.tested_hypotheses,
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True, slots=True)
class MatchedReport:
    status: AnalysisStatus
    matched_pairs: int
    results: tuple[MetricResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "comparison": "controlled_comparison",
            "status": self.status.value,
            "matched_pairs": self.matched_pairs,
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True, slots=True)
class BootstrapBundle:
    report: ScopeReport
    plan: HistoryPlan
    assignments: dict[str, str]
    registry_json: str


def scope_for_trace(trace: Trace) -> ScopeKey:
    return ScopeKey(
        tenant_id=trace.tenant_id,
        workload=trace.tags.get("verdict.workload", "application"),
        granularity=trace.tags.get("capture.granularity", "llm-call"),
    )


def plan_history(traces: Iterable[Trace]) -> HistoryPlan:
    """Split complete independent units into equal event-time cohorts."""
    grouped: dict[str, list[Trace]] = defaultdict(list)
    for trace in traces:
        unit_id = trace.session_id or trace.trace_id
        grouped[unit_id].append(trace)

    units: list[HistoryUnit] = []
    for unit_id, rows in grouped.items():
        ordered = tuple(sorted(rows, key=lambda row: (_event_time(row), row.trace_id)))
        units.append(HistoryUnit(unit_id, _event_time(ordered[0]), ordered))
    units.sort(key=lambda unit: (unit.event_time, unit.unit_id))

    half = len(units) // 2
    if half == 0:
        return HistoryPlan(tuple(), tuple(), units[0] if units else None)
    middle = units[half] if len(units) % 2 else None
    current_start = half + (1 if middle else 0)
    return HistoryPlan(tuple(units[:half]), tuple(units[current_start:]), middle)


def analyze_traces(
    traces: Iterable[Trace],
    *,
    effect_threshold: float = 0.147,
    p_threshold: float = 0.05,
) -> tuple[ScopeReport, ...]:
    """Analyze every workload/granularity scope without combining identities."""
    return tuple(
        bundle.report
        for bundle in build_bootstrap_bundles(
            traces,
            effect_threshold=effect_threshold,
            p_threshold=p_threshold,
        )
    )


def build_bootstrap_bundles(
    traces: Iterable[Trace],
    *,
    effect_threshold: float = 0.147,
    p_threshold: float = 0.05,
) -> tuple[BootstrapBundle, ...]:
    scopes: dict[ScopeKey, list[Trace]] = defaultdict(list)
    for trace in traces:
        if trace.tags.get("verdict.workload") == "judge":
            continue
        if trace.started_at.tzinfo is None or not (trace.prompt_redacted or "").strip():
            continue
        scopes[scope_for_trace(trace)].append(trace)
    return tuple(
        _analyze_scope(scope, rows, effect_threshold=effect_threshold, p_threshold=p_threshold)
        for scope, rows in sorted(
            scopes.items(),
            key=lambda item: (
                item[0].tenant_id or "",
                item[0].workload,
                item[0].granularity,
            ),
        )
    )


def analyze_matched(
    traces: Iterable[Trace],
    *,
    baseline_model: str,
    current_model: str,
    p_threshold: float = 0.05,
) -> MatchedReport:
    """Compare the same explicit intent keys under two declared models."""
    by_variant: dict[str, dict[str, Trace]] = {
        baseline_model: {},
        current_model: {},
    }
    for trace in traces:
        pair_id = trace.tags.get("verdict.intent_key")
        model = trace.request_model or trace.response_model
        if not pair_id or model not in by_variant or trace.tags.get("verdict.workload") == "judge":
            continue
        previous = by_variant[model].get(pair_id)
        if previous is None or (_event_time(trace), trace.trace_id) > (
            _event_time(previous),
            previous.trace_id,
        ):
            by_variant[model][pair_id] = trace

    pair_ids = sorted(set(by_variant[baseline_model]) & set(by_variant[current_model]))
    if len(pair_ids) < 2:
        return MatchedReport(AnalysisStatus.COLLECTING_BASELINE, len(pair_ids), ())

    paired: dict[str, tuple[list[float], list[float]]] = {}
    for pair_id in pair_ids:
        baseline_metrics = _summarize_unit([by_variant[baseline_model][pair_id]])
        current_metrics = _summarize_unit([by_variant[current_model][pair_id]])
        for metric in set(baseline_metrics) & set(current_metrics):
            baseline, current = paired.setdefault(metric, ([], []))
            baseline.append(baseline_metrics[metric])
            current.append(current_metrics[metric])

    candidates: list[tuple[str, list[float], list[float], float, float, float, float]] = []
    for metric, (baseline, current) in sorted(paired.items()):
        differences = np.asarray(current) - np.asarray(baseline)
        if np.all(differences == 0):
            p_value = 1.0
        else:
            p_value = float(wilcoxon(differences, alternative="two-sided", method="auto").pvalue)
        low, high = _paired_interval(
            differences, seed_key=f"{baseline_model}|{current_model}|{metric}"
        )
        candidates.append(
            (metric, baseline, current, float(median(differences)), low, high, p_value)
        )

    results: list[MetricResult] = []
    adjusted = _benjamini_hochberg([item[-1] for item in candidates])
    for item, p_adjusted in zip(candidates, adjusted, strict=True):
        metric, baseline, current, effect, low, high, p_value = item
        if p_adjusted < p_threshold and (low > 0 or high < 0):
            status = AnalysisStatus.DRIFT_DETECTED
        elif low == high == 0:
            status = AnalysisStatus.NO_DRIFT_DETECTED
        else:
            status = AnalysisStatus.LOW_POWER
        results.append(
            MetricResult(
                cluster_id="matched",
                metric=metric,
                status=status,
                baseline_n=len(baseline),
                current_n=len(current),
                baseline_value=float(median(baseline)),
                current_value=float(median(current)),
                effect_size=effect,
                confidence_low=low,
                confidence_high=high,
                p_value=p_value,
                p_value_adjusted=p_adjusted,
            )
        )
    if any(result.status is AnalysisStatus.DRIFT_DETECTED for result in results):
        overall = AnalysisStatus.DRIFT_DETECTED
    elif all(result.status is AnalysisStatus.NO_DRIFT_DETECTED for result in results):
        overall = AnalysisStatus.NO_DRIFT_DETECTED
    else:
        overall = AnalysisStatus.LOW_POWER
    return MatchedReport(overall, len(pair_ids), tuple(results))


def _analyze_scope(
    scope: ScopeKey,
    traces: list[Trace],
    *,
    effect_threshold: float,
    p_threshold: float,
) -> BootstrapBundle:
    plan = plan_history(traces)
    if not plan.baseline or not plan.current:
        return BootstrapBundle(
            _report(scope, plan, AnalysisStatus.COLLECTING_BASELINE, ()),
            plan,
            {},
            ClusterRegistry(version="count-hashing-v1").to_json(),
        )

    assignments, registry_json = _cluster_assignments(plan)
    results, overall = compare_cohorts(
        plan.baseline,
        plan.current,
        assignments,
        effect_threshold=effect_threshold,
        p_threshold=p_threshold,
    )
    return BootstrapBundle(
        _report(scope, plan, overall, results),
        plan,
        assignments,
        registry_json,
    )


def compare_cohorts(
    baseline_units: tuple[HistoryUnit, ...],
    current_units: tuple[HistoryUnit, ...],
    assignments: dict[str, str],
    *,
    effect_threshold: float = 0.147,
    p_threshold: float = 0.05,
) -> tuple[tuple[MetricResult, ...], AnalysisStatus]:
    baseline_values = _metric_values(baseline_units, assignments)
    current_values = _metric_values(current_units, assignments)
    candidates: list[tuple[str, str, list[float], list[float], float, float, float, float]] = []
    for cluster_id, metric in sorted(set(baseline_values) & set(current_values)):
        baseline = baseline_values[(cluster_id, metric)]
        current = current_values[(cluster_id, metric)]
        if len(baseline) < 2 or len(current) < 2:
            continue
        test = mannwhitneyu(current, baseline, alternative="two-sided", method="auto")
        effect = (2.0 * float(test.statistic) / (len(current) * len(baseline))) - 1.0
        low, high = _bootstrap_effect_interval(
            baseline,
            current,
            seed_key="|".join(
                [
                    cluster_id,
                    metric,
                    *(unit.unit_id for unit in baseline_units),
                    "current",
                    *(unit.unit_id for unit in current_units),
                ]
            ),
        )
        candidates.append(
            (
                cluster_id,
                metric,
                baseline,
                current,
                effect,
                low,
                high,
                float(test.pvalue),
            )
        )

    adjusted = _benjamini_hochberg([item[-1] for item in candidates])
    results: list[MetricResult] = []
    for item, p_adjusted in zip(candidates, adjusted, strict=True):
        cluster_id, metric, baseline, current, effect, low, high, p_value = item
        if p_adjusted < p_threshold and abs(effect) >= effect_threshold:
            status = AnalysisStatus.DRIFT_DETECTED
        elif -effect_threshold < low and high < effect_threshold:
            status = AnalysisStatus.NO_DRIFT_DETECTED
        else:
            status = AnalysisStatus.LOW_POWER
        results.append(
            MetricResult(
                cluster_id=cluster_id,
                metric=metric,
                status=status,
                baseline_n=len(baseline),
                current_n=len(current),
                baseline_value=float(median(baseline)),
                current_value=float(median(current)),
                effect_size=effect,
                confidence_low=low,
                confidence_high=high,
                p_value=p_value,
                p_value_adjusted=p_adjusted,
            )
        )

    if any(result.status is AnalysisStatus.DRIFT_DETECTED for result in results):
        overall = AnalysisStatus.DRIFT_DETECTED
    elif not results:
        overall = AnalysisStatus.NOT_EVALUABLE
    elif all(result.status is AnalysisStatus.NO_DRIFT_DETECTED for result in results):
        overall = AnalysisStatus.NO_DRIFT_DETECTED
    else:
        overall = AnalysisStatus.LOW_POWER
    return tuple(results), overall


def _report(
    scope: ScopeKey,
    plan: HistoryPlan,
    status: AnalysisStatus,
    results: tuple[MetricResult, ...],
) -> ScopeReport:
    return ScopeReport(
        scope=scope,
        status=status,
        baseline_units=len(plan.baseline),
        current_units=len(plan.current),
        baseline_traces=sum(len(unit.traces) for unit in plan.baseline),
        current_traces=sum(len(unit.traces) for unit in plan.current),
        excluded_middle_units=int(plan.excluded_middle is not None),
        tested_hypotheses=len(results),
        results=results,
    )


def _event_time(trace: Trace) -> datetime:
    if trace.started_at.tzinfo is None:
        raise ValueError(f"trace {trace.trace_id!r} has no event-time offset")
    return trace.started_at.astimezone(UTC)


def _cluster_assignments(plan: HistoryPlan) -> tuple[dict[str, str], str]:
    baseline = [trace for unit in plan.baseline for trace in unit.traces]
    current = [trace for unit in plan.current for trace in unit.traces]
    registry = ClusterRegistry(version="count-hashing-v1")
    clusterer = StableIntentClusterer(
        HashingEmbedder(), threshold=0.5, freeze_after=200, registry=registry
    )
    baseline_ids = clusterer.assign([trace.prompt_redacted or "" for trace in baseline])
    baseline_cluster_ids = set(baseline_ids)
    frozen = ClusterRegistry.from_json(clusterer.registry.to_json())
    current_clusterer = StableIntentClusterer(
        HashingEmbedder(), threshold=0.5, freeze_after=1, registry=frozen
    )
    current_ids = current_clusterer.assign([trace.prompt_redacted or "" for trace in current])
    assignments = dict(zip((trace.trace_id for trace in baseline), baseline_ids, strict=True))
    assignments.update(
        {
            trace.trace_id: cluster_id
            for trace, cluster_id in zip(current, current_ids, strict=True)
            if cluster_id in baseline_cluster_ids
        }
    )
    return assignments, clusterer.registry.to_json()


def _metric_values(
    units: tuple[HistoryUnit, ...],
    assignments: dict[str, str],
) -> dict[tuple[str, str], list[float]]:
    unit_metrics: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for unit in units:
        by_cluster: dict[str, list[Trace]] = defaultdict(list)
        for trace in unit.traces:
            cluster_id = assignments.get(trace.trace_id)
            if cluster_id:
                by_cluster[cluster_id].append(trace)
        for cluster_id, traces in by_cluster.items():
            for metric, value in _summarize_unit(traces).items():
                unit_metrics[(cluster_id, metric, unit.unit_id)].append(value)

    values: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (cluster_id, metric, _), rows in unit_metrics.items():
        values[(cluster_id, metric)].append(fmean(rows))
    return values


def _summarize_unit(traces: list[Trace]) -> dict[str, float]:
    values: dict[str, float] = {"error_rate": fmean(float(bool(trace.error)) for trace in traces)}
    responses = [
        trace.response_redacted for trace in traces if (trace.response_redacted or "").strip()
    ]
    if responses:
        values["response_words"] = fmean(_word_count(response or "") for response in responses)
        values["refusal_rate"] = fmean(float(is_refusal(response or "")) for response in responses)
    latencies = [trace.latency_ms for trace in traces if trace.latency_ms is not None]
    if latencies:
        values["latency_ms"] = fmean(latencies)
    output_tokens = [trace.output_tokens for trace in traces if trace.output_tokens is not None]
    if output_tokens:
        values["output_tokens"] = fmean(output_tokens)
    tool_calls = [
        int(value)
        for trace in traces
        if (value := trace.tags.get("capture.tool_calls", "")).isdecimal()
    ]
    if tool_calls:
        values["tool_calls"] = fmean(tool_calls)
    return values


def _bootstrap_effect_interval(
    baseline: list[float],
    current: list[float],
    *,
    seed_key: str,
    repetitions: int = 300,
) -> tuple[float, float]:
    seed = int(hashlib.sha256(seed_key.encode()).hexdigest()[:16], 16)
    rng = np.random.default_rng(seed)
    base = np.asarray(baseline, dtype=float)
    cur = np.asarray(current, dtype=float)
    effects = np.empty(repetitions, dtype=float)
    for index in range(repetitions):
        base_sample = rng.choice(base, len(base), replace=True)
        cur_sample = rng.choice(cur, len(cur), replace=True)
        u = mannwhitneyu(cur_sample, base_sample, alternative="two-sided", method="auto").statistic
        effects[index] = (2.0 * float(u) / (len(cur) * len(base))) - 1.0
    return float(np.quantile(effects, 0.025)), float(np.quantile(effects, 0.975))


def _paired_interval(
    differences: np.ndarray,
    *,
    seed_key: str,
    repetitions: int = 500,
) -> tuple[float, float]:
    seed = int(hashlib.sha256(seed_key.encode()).hexdigest()[:16], 16)
    rng = np.random.default_rng(seed)
    effects = np.empty(repetitions, dtype=float)
    for index in range(repetitions):
        effects[index] = float(median(rng.choice(differences, len(differences), replace=True)))
    return float(np.quantile(effects, 0.025)), float(np.quantile(effects, 0.975))
