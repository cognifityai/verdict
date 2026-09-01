"""Immutable monitor policies and cohort manifests.

This module chooses membership before inspecting metric outcomes. Grouping is
an explicit versioned input; the default is no grouping.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum


class WindowMode(str, Enum):
    COUNT = "count"
    EXPLICIT = "explicit"


class MonitorStatus(str, Enum):
    ALERT = "alert"
    NO_ALERT = "no_alert"
    INSUFFICIENT = "insufficient"
    REFERENCE_STALE = "reference_stale"


def _aware(value: datetime | None, name: str) -> None:
    if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AnalysisUnitRecord:
    unit_id: str
    event_time: datetime
    metrics: Mapping[str, bool]
    group_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, str) or not self.unit_id:
            raise ValueError("unit_id is required")
        _aware(self.event_time, "event_time")
        if not isinstance(self.metrics, Mapping):
            raise ValueError("metrics must be a mapping")
        for name, value in self.metrics.items():
            if not isinstance(name, str) or not name:
                raise ValueError("metric names must be non-empty strings")
            if not isinstance(value, bool):
                raise ValueError(
                    "monitor metrics must be boolean; continuous values require "
                    "an explicitly versioned statistical contract"
                )


@dataclass(frozen=True, slots=True)
class MonitorPolicy:
    policy_id: str
    scope_key: str
    window_mode: WindowMode = WindowMode.COUNT
    reference_ratio: float = 0.8
    reference_start: datetime | None = None
    reference_end: datetime | None = None
    current_start: datetime | None = None
    current_end: datetime | None = None
    minimum_reference: int = 30
    minimum_current: int = 30
    prospective_target: int = 30
    p_threshold: float = 0.05
    minimum_effect: float = 0.1
    maximum_unseen_group_share: float = 0.2
    analysis_unit: str = "trace"
    grouping_mode: str = "none"
    sequential_method: str = "quadratic_alpha_spending_v1"

    def __post_init__(self) -> None:
        for name, maximum in (("policy_id", 256), ("scope_key", 512)):
            value = getattr(self, name)
            if (not isinstance(value, str) or not value or "\x00" in value
                    or len(value.encode("utf-8")) > maximum):
                raise ValueError(f"{name} must be bounded text")
        if not isinstance(self.window_mode, WindowMode):
            object.__setattr__(self, "window_mode", WindowMode(self.window_mode))
        if not 0.5 <= self.reference_ratio < 1:
            raise ValueError("reference_ratio must be between 0.5 and 1")
        for name in ("minimum_reference", "minimum_current", "prospective_target"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("p_threshold", "minimum_effect", "maximum_unseen_group_share"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")
        if self.analysis_unit not in {"trace", "turn", "run", "session"}:
            raise ValueError("analysis_unit is unsupported")
        if self.grouping_mode not in {"none", "provider_model", "cluster"}:
            raise ValueError("grouping_mode is unsupported")
        if self.sequential_method != "quadratic_alpha_spending_v1":
            raise ValueError("sequential_method is unsupported")
        ranges = (
            self.reference_start, self.reference_end, self.current_start, self.current_end,
        )
        for name, value in zip(
            ("reference_start", "reference_end", "current_start", "current_end"),
            ranges, strict=True,
        ):
            _aware(value, name)
        if self.window_mode is WindowMode.EXPLICIT:
            if any(value is None for value in ranges):
                raise ValueError("explicit windows require all four boundaries")
            assert all(value is not None for value in ranges)
            if not self.reference_start < self.reference_end <= self.current_start < self.current_end:
                raise ValueError("explicit windows must be ordered and non-overlapping")

    @property
    def fingerprint(self) -> str:
        payload = {
            key: value.isoformat() if isinstance(value, datetime)
            else value.value if isinstance(value, Enum) else value
            for item in fields(self)
            for key, value in ((item.name, getattr(self, item.name)),)
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class CohortManifest:
    snapshot_id: str
    policy_fingerprint: str
    cutoff: datetime
    reference_unit_ids: tuple[str, ...]
    current_unit_ids: tuple[str, ...]
    consumed_unit_ids: tuple[str, ...]
    late_unit_count: int = 0
    prospective_open: bool = False
    comparison_index: int = 0

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "policy_fingerprint"):
            value = getattr(self, name)
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} must be a SHA-256 digest")
        _aware(self.cutoff, "cutoff")
        reference = set(self.reference_unit_ids)
        current = set(self.current_unit_ids)
        consumed = set(self.consumed_unit_ids)
        if (len(reference) != len(self.reference_unit_ids)
                or len(current) != len(self.current_unit_ids)
                or len(consumed) != len(self.consumed_unit_ids)):
            raise ValueError("manifest unit identities must be unique")
        if reference & current or not reference | current <= consumed:
            raise ValueError("manifest cohorts must be non-overlapping and consumed")
        if (isinstance(self.late_unit_count, bool) or not isinstance(self.late_unit_count, int)
                or self.late_unit_count < 0):
            raise ValueError("late_unit_count must be non-negative")
        if not isinstance(self.prospective_open, bool):
            raise ValueError("prospective_open must be boolean")
        if (
            isinstance(self.comparison_index, bool)
            or not isinstance(self.comparison_index, int)
            or self.comparison_index < 0
        ):
            raise ValueError("comparison_index must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class MetricComparison:
    metric: str
    reference_n: int
    current_n: int
    reference_value: float
    current_value: float
    effect: float
    p_value: float
    p_adjusted: float
    alert: bool

    def __post_init__(self) -> None:
        if not self.metric or min(self.reference_n, self.current_n) < 0:
            raise ValueError("metric comparison identity and counts are invalid")
        values = (
            self.reference_value, self.current_value, self.effect,
            self.p_value, self.p_adjusted,
        )
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
            raise ValueError("metric comparison values must be finite")
        if not 0 <= self.p_value <= 1 or not 0 <= self.p_adjusted <= 1:
            raise ValueError("metric p-values must be between zero and one")
        if not isinstance(self.alert, bool):
            raise ValueError("metric alert must be boolean")


@dataclass(frozen=True, slots=True)
class MonitorComparison:
    status: MonitorStatus
    metrics: tuple[MetricComparison, ...]
    unseen_group_share: float
    alpha_threshold: float

    def __post_init__(self) -> None:
        if not isinstance(self.status, MonitorStatus):
            object.__setattr__(self, "status", MonitorStatus(self.status))
        if not 0 <= self.unseen_group_share <= 1:
            raise ValueError("unseen_group_share must be between zero and one")
        if not 0 < self.alpha_threshold <= 1:
            raise ValueError("alpha_threshold must be between zero and one")


def _ordered(units) -> list[AnalysisUnitRecord]:
    rows = list(units)
    if len({unit.unit_id for unit in rows}) != len(rows):
        raise ValueError("analysis units must have unique identities")
    return sorted(rows, key=lambda unit: (unit.event_time, unit.unit_id))


def _manifest(
    policy: MonitorPolicy,
    cutoff: datetime,
    reference: list[AnalysisUnitRecord],
    current: list[AnalysisUnitRecord],
    consumed: tuple[str, ...],
    late: int = 0,
    prospective_open: bool = False,
    comparison_index: int = 0,
) -> CohortManifest:
    identity = json.dumps(
        {
            "policy": policy.fingerprint,
            "cutoff": cutoff.isoformat(),
            "reference": [unit.unit_id for unit in reference],
            "current": [unit.unit_id for unit in current],
            "consumed": consumed,
            "prospective_open": prospective_open,
            "comparison_index": comparison_index,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return CohortManifest(
        hashlib.sha256(identity.encode()).hexdigest(), policy.fingerprint, cutoff,
        tuple(unit.unit_id for unit in reference), tuple(unit.unit_id for unit in current),
        consumed, late, prospective_open, comparison_index,
    )


def plan_historical_manifest(
    units, policy: MonitorPolicy, *, cutoff: datetime
) -> CohortManifest:
    """Freeze historical membership without reading any metric values."""
    _aware(cutoff, "cutoff")
    rows = [unit for unit in _ordered(units) if unit.event_time <= cutoff]
    if policy.window_mode is WindowMode.COUNT:
        boundary = int(len(rows) * policy.reference_ratio)
        reference, current = rows[:boundary], rows[boundary:]
    else:
        reference = [
            unit for unit in rows
            if policy.reference_start <= unit.event_time < policy.reference_end
        ]
        current = [
            unit for unit in rows
            if policy.current_start <= unit.event_time < policy.current_end
        ]
    consumed = tuple(unit.unit_id for unit in (*reference, *current))
    return _manifest(policy, cutoff, reference, current, consumed)


def plan_prospective_manifest(
    previous: CohortManifest, units, policy: MonitorPolicy
) -> CohortManifest:
    """Freeze the next non-overlapping current bucket against one reference."""
    if previous.policy_fingerprint != policy.fingerprint:
        raise ValueError("policy fingerprint changed; create a candidate policy")
    used = set(previous.consumed_unit_ids)
    rows = _ordered(units)
    unseen_rows = [unit for unit in rows if unit.unit_id not in used]
    late_units = [unit for unit in unseen_rows if unit.event_time < previous.cutoff]
    # A late-arriving unit is still evidence. Excluding it would selectively
    # discard slow/error-prone calls and bias the monitored failure rate.
    candidates = unseen_rows
    if previous.prospective_open:
        target_remaining = policy.prospective_target - len(previous.current_unit_ids)
        candidates = candidates[:target_remaining]
        current_ids = (*previous.current_unit_ids, *(unit.unit_id for unit in candidates))
        comparison_index = previous.comparison_index
    else:
        candidates = candidates[:policy.prospective_target]
        current_ids = tuple(unit.unit_id for unit in candidates)
        comparison_index = previous.comparison_index + 1
    prospective_open = len(current_ids) < policy.prospective_target
    cutoff = max((previous.cutoff, *(unit.event_time for unit in candidates)))
    consumed = (
        *previous.consumed_unit_ids,
        *(unit.unit_id for unit in candidates),
    )
    identity = json.dumps({
        "policy": policy.fingerprint, "cutoff": cutoff.isoformat(),
        "reference": previous.reference_unit_ids, "current": current_ids,
        "consumed": consumed, "prospective_open": prospective_open,
        "comparison_index": comparison_index,
    }, sort_keys=True, separators=(",", ":"))
    return CohortManifest(
        hashlib.sha256(identity.encode()).hexdigest(), policy.fingerprint, cutoff,
        previous.reference_unit_ids, current_ids, consumed,
        previous.late_unit_count + len(late_units) if previous.prospective_open else len(late_units),
        prospective_open, comparison_index,
    )


def compare_manifest(
    units, manifest: CohortManifest, policy: MonitorPolicy
) -> MonitorComparison:
    """Compare frozen binary metrics; never choose membership from outcomes."""
    if manifest.policy_fingerprint != policy.fingerprint:
        raise ValueError("manifest and policy do not match")
    alpha_threshold = _alpha_threshold(policy, manifest.comparison_index)
    if manifest.prospective_open:
        return MonitorComparison(
            MonitorStatus.INSUFFICIENT, (), 0.0, alpha_threshold
        )
    by_id = {unit.unit_id: unit for unit in _ordered(units)}
    reference = [by_id[item] for item in manifest.reference_unit_ids if item in by_id]
    current = [by_id[item] for item in manifest.current_unit_ids if item in by_id]
    if (len(reference) != len(manifest.reference_unit_ids)
            or len(current) != len(manifest.current_unit_ids)):
        raise ValueError("manifest evidence is missing")
    if len(reference) < policy.minimum_reference or len(current) < policy.minimum_current:
        return MonitorComparison(
            MonitorStatus.INSUFFICIENT, (), 0.0, alpha_threshold
        )
    reference_groups = {unit.group_id for unit in reference if unit.group_id is not None}
    grouped_current = [unit for unit in current if unit.group_id is not None]
    unseen = sum(unit.group_id not in reference_groups for unit in grouped_current)
    unseen_share = unseen / len(grouped_current) if grouped_current else 0.0
    if unseen_share > policy.maximum_unseen_group_share:
        return MonitorComparison(
            MonitorStatus.REFERENCE_STALE, (), unseen_share, alpha_threshold
        )
    metric_names = sorted(set().union(*(set(unit.metrics) for unit in (*reference, *current))))
    raw = []
    for name in metric_names:
        reference_values = [
            unit.metrics[name] for unit in reference
            if isinstance(unit.metrics.get(name), bool)
        ]
        current_values = [
            unit.metrics[name] for unit in current
            if isinstance(unit.metrics.get(name), bool)
        ]
        if (len(reference_values) < policy.minimum_reference
                or len(current_values) < policy.minimum_current):
            continue
        reference_true = sum(reference_values)
        current_true = sum(current_values)
        p_value = _fisher_two_sided(
            reference_true, len(reference_values) - reference_true,
            current_true, len(current_values) - current_true,
        )
        reference_rate = reference_true / len(reference_values)
        current_rate = current_true / len(current_values)
        raw.append((
            name, len(reference_values), len(current_values), reference_rate,
            current_rate, current_rate - reference_rate, p_value,
        ))
    adjusted = _benjamini_hochberg([item[-1] for item in raw])
    metrics = tuple(
        MetricComparison(
            name, reference_n, current_n, reference_value, current_value,
            effect, p_value, p_adjusted,
            p_adjusted <= alpha_threshold and abs(effect) >= policy.minimum_effect,
        )
        for (name, reference_n, current_n, reference_value, current_value, effect, p_value), p_adjusted
        in zip(raw, adjusted, strict=True)
    )
    if not metrics:
        return MonitorComparison(
            MonitorStatus.INSUFFICIENT, (), unseen_share, alpha_threshold
        )
    status = MonitorStatus.ALERT if any(metric.alert for metric in metrics) else MonitorStatus.NO_ALERT
    return MonitorComparison(status, metrics, unseen_share, alpha_threshold)


def _alpha_threshold(policy: MonitorPolicy, comparison_index: int) -> float:
    """Spend at most ``p_threshold`` across an unbounded sequence of looks.

    Historical previews use the nominal threshold but are never authoritative.
    Prospective look ``k`` uses alpha * 6 / (pi^2 * k^2); the series sums to
    alpha, while Benjamini-Hochberg still controls metrics within each look.
    """
    if comparison_index <= 0:
        return policy.p_threshold
    return policy.p_threshold * 6.0 / (math.pi**2 * comparison_index**2)


def trace_monitor_units(
    traces,
    *,
    grouping_mode: str = "none",
    cluster_assignments: Mapping[str, str] | None = None,
) -> tuple[AnalysisUnitRecord, ...]:
    """Project genuine LLM calls into deterministic trace-level monitor units."""
    from verdict.structural import is_refusal

    units = []
    for trace in traces:
        if trace.tags.get("verdict.workload") == "judge":
            continue
        metrics = {"provider_error": bool(trace.error)}
        if trace.response_redacted is not None:
            metrics.update({
                "response_empty": not bool(trace.response_redacted.strip()),
                "refusal_signature": is_refusal(trace.response_redacted),
            })
        if grouping_mode == "none":
            group_id = None
        elif grouping_mode == "provider_model":
            group_id = (
                f"{trace.provider or 'unknown'}:"
                f"{trace.request_model or trace.response_model or 'unknown'}"
            )
        elif grouping_mode == "cluster":
            group_id = (cluster_assignments or {}).get(trace.trace_id)
        else:
            raise ValueError("grouping_mode is unsupported")
        units.append(AnalysisUnitRecord(
            trace.trace_id, trace.started_at, metrics, group_id,
        ))
    return tuple(units)


def _fisher_two_sided(a: int, b: int, c: int, d: int) -> float:
    row1, col1, total = a + b, a + c, a + b + c + d
    lower, upper = max(0, row1 - (total - col1)), min(row1, col1)
    log_denominator = (
        math.lgamma(total + 1)
        - math.lgamma(row1 + 1)
        - math.lgamma(total - row1 + 1)
    )

    def log_probability(x: int) -> float:
        return (
            math.lgamma(col1 + 1)
            - math.lgamma(x + 1)
            - math.lgamma(col1 - x + 1)
            + math.lgamma(total - col1 + 1)
            - math.lgamma(row1 - x + 1)
            - math.lgamma(total - col1 - row1 + x + 1)
            - log_denominator
        )

    observed_log = log_probability(a)
    selected_log_sum = -math.inf
    for x in range(lower, upper + 1):
        current_log = log_probability(x)
        if current_log <= observed_log + 1e-12:
            if selected_log_sum == -math.inf:
                selected_log_sum = current_log
            else:
                high, low = max(selected_log_sum, current_log), min(
                    selected_log_sum, current_log
                )
                selected_log_sum = high + math.log1p(math.exp(low - high))
    return min(1.0, math.exp(selected_log_sum))


def _benjamini_hochberg(values: list[float]) -> list[float]:
    if not values:
        return []
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    adjusted = [1.0] * len(values)
    running = 1.0
    for rank, (index, value) in reversed(list(enumerate(ordered, start=1))):
        running = min(running, value * len(values) / rank)
        adjusted[index] = min(1.0, running)
    return adjusted


def monitor_policy_to_json(policy: MonitorPolicy) -> str:
    payload = {
        item.name: (
            value.isoformat() if isinstance(value, datetime)
            else value.value if isinstance(value, Enum) else value
        )
        for item in fields(policy)
        for value in (getattr(policy, item.name),)
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def monitor_policy_from_json(payload_json: str) -> MonitorPolicy:
    try:
        payload = json.loads(payload_json)
        for name in ("reference_start", "reference_end", "current_start", "current_end"):
            if payload.get(name) is not None:
                payload[name] = datetime.fromisoformat(payload[name])
        return MonitorPolicy(**payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid monitor policy JSON") from exc


def monitor_snapshot_to_json(
    manifest: CohortManifest, comparison: MonitorComparison
) -> str:
    payload = {
        "manifest": {
            "snapshot_id": manifest.snapshot_id,
            "policy_fingerprint": manifest.policy_fingerprint,
            "cutoff": manifest.cutoff.isoformat(),
            "reference_unit_ids": list(manifest.reference_unit_ids),
            "current_unit_ids": list(manifest.current_unit_ids),
            "consumed_unit_ids": list(manifest.consumed_unit_ids),
            "late_unit_count": manifest.late_unit_count,
            "prospective_open": manifest.prospective_open,
            "comparison_index": manifest.comparison_index,
        },
        "comparison": {
            "status": comparison.status.value,
            "unseen_group_share": comparison.unseen_group_share,
            "alpha_threshold": comparison.alpha_threshold,
            "metrics": [
                {item.name: getattr(metric, item.name) for item in fields(metric)}
                for metric in comparison.metrics
            ],
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def monitor_snapshot_from_json(
    payload_json: str,
) -> tuple[CohortManifest, MonitorComparison]:
    try:
        payload = json.loads(payload_json)
        manifest_data = payload["manifest"]
        comparison_data = payload["comparison"]
        manifest = CohortManifest(
            manifest_data["snapshot_id"], manifest_data["policy_fingerprint"],
            datetime.fromisoformat(manifest_data["cutoff"]),
            tuple(manifest_data["reference_unit_ids"]),
            tuple(manifest_data["current_unit_ids"]),
            tuple(manifest_data["consumed_unit_ids"]),
            manifest_data["late_unit_count"],
            manifest_data.get("prospective_open", False),
            manifest_data.get("comparison_index", 0),
        )
        comparison = MonitorComparison(
            MonitorStatus(comparison_data["status"]),
            tuple(MetricComparison(**metric) for metric in comparison_data["metrics"]),
            comparison_data["unseen_group_share"],
            comparison_data.get("alpha_threshold", 0.05),
        )
        return manifest, comparison
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid monitor snapshot JSON") from exc
