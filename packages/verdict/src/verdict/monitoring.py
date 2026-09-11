"""Immutable monitor policies and cohort manifests.

This module chooses membership before inspecting metric outcomes. Grouping is
an explicit versioned input; the default is no grouping.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum

from verdict.statistics import benjamini_hochberg, fisher_exact_two_sided


class WindowMode(str, Enum):
    COUNT = "count"
    EXPLICIT = "explicit"


class MonitorStatus(str, Enum):
    ALERT = "alert"
    NO_ALERT = "no_alert"
    INSUFFICIENT = "insufficient"
    REFERENCE_STALE = "reference_stale"


MAX_MONITOR_GROUPS = 250
MAX_MONITOR_GROUP_METRICS = 4_000
MAX_MONITOR_SNAPSHOT_BYTES = 4_194_304
EVIDENCE_FINALIZATION_VERSION = 1


class MonitorRebootstrapRequired(ValueError):
    """The stored monitor cannot safely continue with its frozen evidence."""


class MonitorStateConflict(ValueError):
    """The monitor head or policy authority changed before a successor write."""


class MonitorEvaluatorPending(ValueError):
    """The selected historical evaluator has eligible unfinished work."""


def _aware(value: datetime | None, name: str) -> None:
    if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AnalysisUnitRecord:
    unit_id: str
    event_time: datetime
    metrics: Mapping[str, bool]
    group_id: str | None = None
    metric_states: Mapping[str, str] | None = None
    group_label: str | None = None
    group_provider: str | None = None
    group_model: str | None = None
    evaluator_state: str = "not_requested"
    evaluator_evidence_digest: str | None = None

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
        if self.metric_states is None:
            object.__setattr__(self, "metric_states", {})
        elif not isinstance(self.metric_states, Mapping):
            raise ValueError("metric_states must be a mapping")
        for name, value in self.metric_states.items():
            if not isinstance(name, str) or not name:
                raise ValueError("metric state names must be non-empty strings")
            if value not in {"pass", "fail", "unclear", "missing", "error"}:
                raise ValueError("metric state is unsupported")
        _validate_group_id(self.group_id)
        for name in ("group_label", "group_provider", "group_model"):
            _validate_optional_text(getattr(self, name), name, maximum=512)
        if self.group_id is None and any(
            getattr(self, name) is not None
            for name in ("group_label", "group_provider", "group_model")
        ):
            raise ValueError("group metadata requires a group identity")
        if self.evaluator_state not in {
            "not_requested", "not_evaluable", "pending", "error", "completed",
        }:
            raise ValueError("evaluator state is unsupported")
        digest = self.evaluator_evidence_digest
        if self.evaluator_state == "not_requested":
            if digest is not None:
                raise ValueError("judge evidence digest requires an evaluator")
        elif (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("evaluator evidence digest must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class FrozenPendingEvaluatorUnit:
    unit_id: str
    group_id: str | None
    prior_state: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, str) or not self.unit_id:
            raise ValueError("pending evaluator unit identity is required")
        _validate_group_id(self.group_id)
        if self.prior_state not in {"missing", "error"}:
            raise ValueError("pending evaluator state must be missing or error")
        if (
            len(self.evidence_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.evidence_digest)
        ):
            raise ValueError("pending evaluator evidence digest is invalid")


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
    evaluator_fingerprint: str | None = None
    evaluator_dimensions: tuple[str, ...] = ()
    cluster_registry_version_id: str | None = None

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
        if self.cluster_registry_version_id is not None:
            version_id = self.cluster_registry_version_id
            if (
                self.grouping_mode != "cluster"
                or not isinstance(version_id, str)
                or not version_id
                or "\x00" in version_id
                or len(version_id.encode("utf-8")) > 64
            ):
                raise ValueError("cluster registry version is invalid")
        if self.sequential_method != "quadratic_alpha_spending_v1":
            raise ValueError("sequential_method is unsupported")
        if self.evaluator_fingerprint is None:
            if self.evaluator_dimensions:
                raise ValueError("evaluator dimensions require an evaluator fingerprint")
        else:
            fingerprint = self.evaluator_fingerprint
            if (
                not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in fingerprint)
            ):
                raise ValueError("evaluator_fingerprint must be a SHA-256 digest")
            if not isinstance(self.evaluator_dimensions, tuple):
                object.__setattr__(self, "evaluator_dimensions", tuple(self.evaluator_dimensions))
            if not 1 <= len(self.evaluator_dimensions) <= 12:
                raise ValueError("evaluator_dimensions must contain 1-12 names")
            if len(set(self.evaluator_dimensions)) != len(self.evaluator_dimensions):
                raise ValueError("evaluator_dimensions must be unique")
            for dimension in self.evaluator_dimensions:
                if (
                    not isinstance(dimension, str)
                    or not dimension
                    or "\x00" in dimension
                    or len(dimension.encode("utf-8")) > 80
                ):
                    raise ValueError("evaluator dimension must be bounded text")
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
            if not (
                (
                    self.evaluator_fingerprint is None
                    and item.name in {"evaluator_fingerprint", "evaluator_dimensions"}
                )
                or (
                    self.cluster_registry_version_id is None
                    and item.name == "cluster_registry_version_id"
                )
            )
            for key, value in ((item.name, getattr(self, item.name)),)
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenGroupCount:
    group_id: str
    unit_count: int
    label: str | None = None
    provider: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        _validate_group_id(self.group_id)
        if (
            isinstance(self.unit_count, bool)
            or not isinstance(self.unit_count, int)
            or self.unit_count < 1
        ):
            raise ValueError("frozen group count must be positive")
        for name in ("label", "provider", "model"):
            _validate_optional_text(getattr(self, name), name, maximum=512)


@dataclass(frozen=True, slots=True)
class FrozenMetricCounts:
    group_id: str | None
    metric: str
    true_count: int
    false_count: int
    state_pass: int = 0
    state_fail: int = 0
    state_unclear: int = 0
    state_missing: int = 0
    state_error: int = 0

    def __post_init__(self) -> None:
        _validate_group_id(self.group_id)
        if (
            not isinstance(self.metric, str)
            or not self.metric
            or "\x00" in self.metric
            or len(self.metric.encode("utf-8")) > 160
        ):
            raise ValueError("frozen metric identity is required")
        for item in fields(self):
            if item.name in {"group_id", "metric"}:
                continue
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("frozen metric counts must be non-negative integers")

    @property
    def reports_coverage(self) -> bool:
        return any(
            (
                self.state_pass,
                self.state_fail,
                self.state_unclear,
                self.state_missing,
                self.state_error,
            )
        )


@dataclass(frozen=True, slots=True)
class FrozenCohortSummary:
    unit_count: int
    unassigned_unit_count: int
    groups: tuple[FrozenGroupCount, ...]
    metrics: tuple[FrozenMetricCounts, ...]

    def __post_init__(self) -> None:
        for name in ("unit_count", "unassigned_unit_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("frozen cohort counts must be non-negative integers")
        if self.unassigned_unit_count > self.unit_count:
            raise ValueError("unassigned count exceeds cohort size")
        if not isinstance(self.groups, tuple):
            object.__setattr__(self, "groups", tuple(self.groups))
        if not isinstance(self.metrics, tuple):
            object.__setattr__(self, "metrics", tuple(self.metrics))
        if len(self.groups) > MAX_MONITOR_GROUPS:
            raise ValueError(f"monitor grouping exceeds {MAX_MONITOR_GROUPS} groups")
        if len(self.metrics) > MAX_MONITOR_GROUP_METRICS:
            raise ValueError("monitor grouping produces too many metric cells")
        if len({item.group_id for item in self.groups}) != len(self.groups):
            raise ValueError("frozen cohort group identities must be unique")
        if len({(item.group_id, item.metric) for item in self.metrics}) != len(self.metrics):
            raise ValueError("frozen cohort metric identities must be unique")
        if sum(item.unit_count for item in self.groups) + self.unassigned_unit_count not in {
            0,
            self.unit_count,
        }:
            raise ValueError("frozen cohort group counts do not cover the cohort")

    @property
    def evidence_digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                _summary_payload(self),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
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
    reference_summary: FrozenCohortSummary | None = None
    current_summary: FrozenCohortSummary | None = None
    pending_evaluator_units: tuple[FrozenPendingEvaluatorUnit, ...] = ()
    evidence_finalization_version: int = 0
    prospective_start_at: datetime | None = None

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
        if (self.reference_summary is None) != (self.current_summary is None):
            raise ValueError("monitor summaries must be present together")
        if self.reference_summary is not None:
            if self.reference_summary.unit_count != len(self.reference_unit_ids):
                raise ValueError("reference summary does not match membership")
            if self.current_summary.unit_count != len(self.current_unit_ids):
                raise ValueError("current summary does not match membership")
        pending_ids = tuple(item.unit_id for item in self.pending_evaluator_units)
        if len(set(pending_ids)) != len(pending_ids) or not set(pending_ids) <= current:
            raise ValueError("pending evaluator units must be unique current members")
        if self.pending_evaluator_units and (
            self.evidence_finalization_version != EVIDENCE_FINALIZATION_VERSION
        ):
            raise ValueError("pending evaluator units require versioned finalization")
        if self.evidence_finalization_version not in {0, EVIDENCE_FINALIZATION_VERSION}:
            raise ValueError("unsupported evidence finalization version")
        if self.prospective_start_at is not None:
            _aware(self.prospective_start_at, "prospective_start_at")


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
    group_id: str | None = None

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
        _validate_group_id(self.group_id)


@dataclass(frozen=True, slots=True)
class MetricEvidenceCoverage:
    metric: str
    reference_evaluable: int
    reference_unclear: int
    reference_missing: int
    reference_error: int
    current_evaluable: int
    current_unclear: int
    current_missing: int
    current_error: int
    group_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.metric, str) or not self.metric:
            raise ValueError("metric coverage identity is required")
        for item in fields(self):
            if item.name in {"metric", "group_id"}:
                continue
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("metric coverage counts must be non-negative integers")
        _validate_group_id(self.group_id)


@dataclass(frozen=True, slots=True)
class MonitorGroupCoverage:
    group_id: str
    reference_units: int
    current_units: int
    label: str | None = None
    provider: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        _validate_group_id(self.group_id)
        for name in ("reference_units", "current_units"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("monitor group counts must be non-negative integers")
        for name in ("label", "provider", "model"):
            _validate_optional_text(getattr(self, name), name, maximum=512)


@dataclass(frozen=True, slots=True)
class MonitorComparison:
    status: MonitorStatus
    metrics: tuple[MetricComparison, ...]
    unseen_group_share: float
    alpha_threshold: float
    metric_coverage: tuple[MetricEvidenceCoverage, ...] = ()
    unassigned_group_share: float = 0.0
    groups: tuple[MonitorGroupCoverage, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, MonitorStatus):
            object.__setattr__(self, "status", MonitorStatus(self.status))
        if not 0 <= self.unseen_group_share <= 1:
            raise ValueError("unseen_group_share must be between zero and one")
        if not 0 < self.alpha_threshold <= 1:
            raise ValueError("alpha_threshold must be between zero and one")
        if not 0 <= self.unassigned_group_share <= self.unseen_group_share:
            raise ValueError("unassigned_group_share must be within unseen_group_share")
        if not isinstance(self.metric_coverage, tuple):
            object.__setattr__(self, "metric_coverage", tuple(self.metric_coverage))
        if not isinstance(self.groups, tuple):
            object.__setattr__(self, "groups", tuple(self.groups))
        if len({(item.group_id, item.metric) for item in self.metrics}) != len(self.metrics):
            raise ValueError("metric comparison identities must be unique")
        if len({(item.group_id, item.metric) for item in self.metric_coverage}) != len(
            self.metric_coverage
        ):
            raise ValueError("metric coverage identities must be unique")
        if len(self.groups) > MAX_MONITOR_GROUPS or len(
            {item.group_id for item in self.groups}
        ) != len(self.groups):
            raise ValueError("monitor group coverage identities must be bounded and unique")


def _validate_group_id(group_id: str | None) -> None:
    if group_id is None:
        return
    if (
        not isinstance(group_id, str)
        or not group_id
        or "\x00" in group_id
        or len(group_id.encode("utf-8")) > 256
    ):
        raise ValueError("metric group identity must be bounded text")


def _validate_optional_text(value: str | None, name: str, *, maximum: int) -> None:
    if value is None:
        return
    try:
        valid = (
            isinstance(value, str)
            and bool(value)
            and "\x00" not in value
            and len(value.encode("utf-8")) <= maximum
        )
    except UnicodeError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be bounded text")


def _ordered(units) -> list[AnalysisUnitRecord]:
    rows = list(units)
    if len({unit.unit_id for unit in rows}) != len(rows):
        raise ValueError("analysis units must have unique identities")
    return sorted(rows, key=lambda unit: (unit.event_time, unit.unit_id))


def _summary_payload(summary: FrozenCohortSummary) -> dict[str, object]:
    return {
        "unit_count": summary.unit_count,
        "unassigned_unit_count": summary.unassigned_unit_count,
        "groups": [
            {
                item.name: getattr(group, item.name)
                for item in fields(group)
                if getattr(group, item.name) is not None
            }
            for group in summary.groups
        ],
        "metrics": [
            {
                item.name: getattr(metric, item.name)
                for item in fields(metric)
                if not (item.name == "group_id" and getattr(metric, item.name) is None)
            }
            for metric in summary.metrics
        ],
    }


def _serialized_summary(summary: FrozenCohortSummary) -> dict[str, object]:
    payload = _summary_payload(summary)
    payload["evidence_digest"] = summary.evidence_digest
    return payload


def _summary_from_payload(payload: object) -> FrozenCohortSummary:
    if not isinstance(payload, dict):
        raise ValueError("invalid frozen cohort summary")
    digest = payload.get("evidence_digest")
    summary = FrozenCohortSummary(
        payload["unit_count"],
        payload["unassigned_unit_count"],
        tuple(FrozenGroupCount(**item) for item in payload["groups"]),
        tuple(
            FrozenMetricCounts(**{"group_id": None, **dict(item)}) for item in payload["metrics"]
        ),
    )
    if digest != summary.evidence_digest:
        raise ValueError("frozen cohort evidence digest changed")
    return summary


def _freeze_cohort(
    units: list[AnalysisUnitRecord],
    *,
    grouped: bool,
) -> FrozenCohortSummary:
    selected: dict[str | None, list[AnalysisUnitRecord]] = {}
    group_metadata: dict[str, AnalysisUnitRecord] = {}
    unassigned = 0
    for unit in units:
        if grouped and unit.group_id is None:
            unassigned += 1
            continue
        group_id = unit.group_id if grouped else None
        selected.setdefault(group_id, []).append(unit)
        if group_id is not None:
            group_metadata.setdefault(group_id, unit)
    if grouped and len(group_metadata) > MAX_MONITOR_GROUPS:
        raise ValueError(f"monitor grouping exceeds {MAX_MONITOR_GROUPS} groups")

    groups = tuple(
        FrozenGroupCount(
            group_id,
            len(selected[group_id]),
            source.group_label or group_id,
            source.group_provider,
            source.group_model,
        )
        for group_id, source in sorted(group_metadata.items())
    )
    metric_counts = []
    for group_id, rows in sorted(
        selected.items(),
        key=lambda item: "" if item[0] is None else item[0],
    ):
        names = sorted(
            set().union(*(set(unit.metrics) | set(unit.metric_states or {}) for unit in rows))
        )
        for name in names:
            values = [
                unit.metrics[name] for unit in rows if isinstance(unit.metrics.get(name), bool)
            ]
            state_counts = Counter(
                unit.metric_states[name] for unit in rows if name in (unit.metric_states or {})
            )
            metric_counts.append(
                FrozenMetricCounts(
                    group_id,
                    name,
                    sum(values),
                    len(values) - sum(values),
                    state_counts["pass"],
                    state_counts["fail"],
                    state_counts["unclear"],
                    state_counts["missing"],
                    state_counts["error"],
                )
            )
    return FrozenCohortSummary(
        len(units),
        unassigned,
        groups,
        tuple(metric_counts),
    )


def _merge_summaries(
    first: FrozenCohortSummary,
    second: FrozenCohortSummary,
) -> FrozenCohortSummary:
    groups: dict[str, FrozenGroupCount] = {item.group_id: item for item in first.groups}
    for item in second.groups:
        previous = groups.get(item.group_id)
        if previous is None:
            groups[item.group_id] = item
        else:
            if (previous.provider, previous.model) != (item.provider, item.model):
                raise ValueError("monitor group identity metadata changed")
            groups[item.group_id] = FrozenGroupCount(
                item.group_id,
                previous.unit_count + item.unit_count,
                previous.label or item.label,
                previous.provider,
                previous.model,
            )
    metrics: dict[tuple[str | None, str], FrozenMetricCounts] = {
        (item.group_id, item.metric): item for item in first.metrics
    }
    for item in second.metrics:
        key = (item.group_id, item.metric)
        previous = metrics.get(key)
        if previous is None:
            metrics[key] = item
        else:
            metrics[key] = FrozenMetricCounts(
                item.group_id,
                item.metric,
                previous.true_count + item.true_count,
                previous.false_count + item.false_count,
                previous.state_pass + item.state_pass,
                previous.state_fail + item.state_fail,
                previous.state_unclear + item.state_unclear,
                previous.state_missing + item.state_missing,
                previous.state_error + item.state_error,
            )
    return FrozenCohortSummary(
        first.unit_count + second.unit_count,
        first.unassigned_unit_count + second.unassigned_unit_count,
        tuple(sorted(groups.values(), key=lambda item: item.group_id)),
        tuple(sorted(metrics.values(), key=lambda item: (item.group_id or "", item.metric))),
    )


def _pending_evaluator_units(
    units: list[AnalysisUnitRecord], *, tested_group_ids: set[str] | None,
) -> tuple[FrozenPendingEvaluatorUnit, ...]:
    pending = []
    for unit in units:
        if (
            tested_group_ids is not None
            and unit.group_id not in tested_group_ids
        ) or unit.evaluator_state not in {
            "pending", "error",
        }:
            continue
        assert unit.evaluator_evidence_digest is not None
        pending.append(FrozenPendingEvaluatorUnit(
            unit.unit_id,
            unit.group_id if tested_group_ids is not None else None,
            "error" if unit.evaluator_state == "error" else "missing",
            unit.evaluator_evidence_digest,
        ))
    return tuple(pending)


def _replace_evaluator_state(
    metrics: dict[tuple[str | None, str], FrozenMetricCounts],
    pending: FrozenPendingEvaluatorUnit,
    states: Mapping[str, str],
) -> None:
    for metric, state in states.items():
        key = (pending.group_id, metric)
        previous = metrics.get(key)
        if previous is None:
            raise MonitorRebootstrapRequired(
                "pending evaluator summary changed; re-bootstrap the monitor"
            )
        values = {item.name: getattr(previous, item.name) for item in fields(previous)}
        prior_name = f"state_{pending.prior_state}"
        if values[prior_name] < 1:
            raise MonitorRebootstrapRequired(
                "pending evaluator summary changed; re-bootstrap the monitor"
            )
        values[prior_name] -= 1
        values[f"state_{state}"] += 1
        if state == "pass":
            values["true_count"] += 1
        elif state == "fail":
            values["false_count"] += 1
        metrics[key] = FrozenMetricCounts(**values)


def _advance_pending_evaluator_units(
    summary: FrozenCohortSummary,
    pending: tuple[FrozenPendingEvaluatorUnit, ...],
    units_by_id: Mapping[str, AnalysisUnitRecord],
    dimensions: tuple[str, ...],
) -> tuple[FrozenCohortSummary, tuple[FrozenPendingEvaluatorUnit, ...]]:
    remaining = []
    metrics = {(item.group_id, item.metric): item for item in summary.metrics}
    changed = False
    for item in pending:
        unit = units_by_id.get(item.unit_id)
        if unit is None or unit.evaluator_evidence_digest != item.evidence_digest:
            raise MonitorRebootstrapRequired(
                "pending evaluator evidence is unavailable or changed; re-bootstrap the monitor"
            )
        if unit.evaluator_state == "completed":
            states = {
                f"judge.{dimension}.pass": unit.metric_states[
                    f"judge.{dimension}.pass"
                ]
                for dimension in dimensions
            }
            _replace_evaluator_state(metrics, item, states)
            changed = True
            continue
        if unit.evaluator_state == "not_evaluable":
            raise MonitorRebootstrapRequired(
                "pending evaluator evidence is no longer evaluable; re-bootstrap the monitor"
            )
        if unit.evaluator_state == "error" and item.prior_state == "missing":
            states = {f"judge.{dimension}.pass": "error" for dimension in dimensions}
            _replace_evaluator_state(metrics, item, states)
            changed = True
            item = FrozenPendingEvaluatorUnit(
                item.unit_id, item.group_id, "error", item.evidence_digest,
            )
        remaining.append(item)
    if changed:
        summary = FrozenCohortSummary(
            summary.unit_count,
            summary.unassigned_unit_count,
            summary.groups,
            tuple(sorted(
                metrics.values(), key=lambda item: (item.group_id or "", item.metric),
            )),
        )
    return summary, tuple(remaining)


def _manifest(
    policy: MonitorPolicy,
    cutoff: datetime,
    reference: list[AnalysisUnitRecord],
    current: list[AnalysisUnitRecord],
    consumed: tuple[str, ...],
    late: int = 0,
    prospective_open: bool = False,
    comparison_index: int = 0,
    reference_summary: FrozenCohortSummary | None = None,
    current_summary: FrozenCohortSummary | None = None,
    reference_unit_ids: tuple[str, ...] | None = None,
    current_unit_ids: tuple[str, ...] | None = None,
    pending_evaluator_units: tuple[FrozenPendingEvaluatorUnit, ...] = (),
    evidence_finalization_version: int = EVIDENCE_FINALIZATION_VERSION,
    prospective_start_at: datetime | None = None,
) -> CohortManifest:
    if reference_summary is None:
        reference_summary = _freeze_cohort(
            reference,
            grouped=policy.grouping_mode != "none",
        )
    if current_summary is None:
        current_summary = _freeze_cohort(
            current,
            grouped=policy.grouping_mode != "none",
        )
    if (
        len({item.group_id for item in (*reference_summary.groups, *current_summary.groups)})
        > MAX_MONITOR_GROUPS
    ):
        raise ValueError(f"monitor grouping exceeds {MAX_MONITOR_GROUPS} groups")
    if (
        len(
            {
                (item.group_id, item.metric)
                for item in (*reference_summary.metrics, *current_summary.metrics)
            }
        )
        > MAX_MONITOR_GROUP_METRICS
    ):
        raise ValueError("monitor grouping produces too many metric cells")
    frozen_reference_ids = (
        tuple(unit.unit_id for unit in reference)
        if reference_unit_ids is None
        else reference_unit_ids
    )
    frozen_current_ids = (
        tuple(unit.unit_id for unit in current) if current_unit_ids is None else current_unit_ids
    )
    identity_payload = {
        "policy": policy.fingerprint,
        "cutoff": cutoff.isoformat(),
        "reference": frozen_reference_ids,
        "current": frozen_current_ids,
        "consumed": consumed,
        "late_unit_count": late,
        "prospective_open": prospective_open,
        "comparison_index": comparison_index,
        "reference_evidence": reference_summary.evidence_digest,
        "current_evidence": current_summary.evidence_digest,
        "pending_evaluator_units": [
            {
                "unit_id": item.unit_id,
                "group_id": item.group_id,
                "prior_state": item.prior_state,
                "evidence_digest": item.evidence_digest,
            }
            for item in pending_evaluator_units
        ],
        "evidence_finalization_version": evidence_finalization_version,
    }
    if prospective_start_at is not None:
        identity_payload["prospective_start_at"] = prospective_start_at.isoformat()
    identity = json.dumps(
        identity_payload,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CohortManifest(
        hashlib.sha256(identity.encode()).hexdigest(),
        policy.fingerprint,
        cutoff,
        frozen_reference_ids,
        frozen_current_ids,
        consumed,
        late,
        prospective_open,
        comparison_index,
        reference_summary,
        current_summary,
        pending_evaluator_units,
        evidence_finalization_version,
        prospective_start_at,
    )


def plan_historical_manifest(units, policy: MonitorPolicy, *, cutoff: datetime) -> CohortManifest:
    """Choose historical membership first, then freeze its normalized facts."""
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
    reference_group_ids = None
    shared_group_ids = None
    if policy.grouping_mode != "none":
        reference_group_ids = {
            unit.group_id for unit in reference if unit.group_id is not None
        }
        shared_group_ids = (
            reference_group_ids
            & {unit.group_id for unit in current if unit.group_id is not None}
        )
    pending = (
        *_pending_evaluator_units(
            reference, tested_group_ids=reference_group_ids,
        ),
        *_pending_evaluator_units(
            current, tested_group_ids=shared_group_ids,
        ),
    )
    if policy.evaluator_fingerprint is not None and pending:
        noun = "trace" if len(pending) == 1 else "traces"
        raise MonitorEvaluatorPending(
            f"Run the selected evaluator for {len(pending)} eligible {noun}, "
            "then preview this monitor again."
        )
    return _manifest(policy, cutoff, reference, current, consumed)


def monitor_requires_rebootstrap(
    policy: MonitorPolicy,
    manifest: CohortManifest,
    *,
    active: bool = False,
) -> bool:
    """Return whether a legacy policy lacks immutable execution evidence."""
    return (
        manifest.reference_summary is None
        or manifest.current_summary is None
        or (policy.grouping_mode == "cluster" and policy.cluster_registry_version_id is None)
        or (
            policy.evaluator_fingerprint is not None
            and manifest.evidence_finalization_version != EVIDENCE_FINALIZATION_VERSION
        )
        or (
            (active or manifest.comparison_index > 0)
            and manifest.prospective_start_at is None
        )
    )


def plan_prospective_manifest(
    previous: CohortManifest,
    units,
    policy: MonitorPolicy,
    *,
    prospective_start_at: datetime | None = None,
) -> CohortManifest:
    """Freeze the next non-overlapping current bucket against one reference."""
    if previous.policy_fingerprint != policy.fingerprint:
        raise ValueError("policy fingerprint changed; create a candidate policy")
    if previous.reference_summary is None or previous.current_summary is None:
        raise ValueError("monitor policy requires re-bootstrap")
    if previous.comparison_index > 0 and previous.prospective_start_at is None:
        raise ValueError("monitor policy requires re-bootstrap")
    if previous.prospective_start_at is not None:
        if (
            prospective_start_at is not None
            and prospective_start_at != previous.prospective_start_at
        ):
            raise ValueError("prospective start cannot change")
        start_at = previous.prospective_start_at
    else:
        start_at = prospective_start_at or previous.cutoff
    _aware(start_at, "prospective_start_at")
    used = set(previous.consumed_unit_ids)
    rows = _ordered(units)
    units_by_id = {unit.unit_id: unit for unit in rows}
    unseen_rows = [
        unit for unit in rows if unit.unit_id not in used and unit.event_time >= start_at
    ]
    tested_group_ids = (
        {item.group_id for item in previous.reference_summary.groups}
        if policy.grouping_mode != "none"
        else None
    )
    # A post-activation event remains evidence even when it arrives late.
    # Pre-activation history is excluded by the immutable start boundary above.
    candidates = unseen_rows
    if previous.prospective_open:
        current_summary, pending = _advance_pending_evaluator_units(
            previous.current_summary,
            previous.pending_evaluator_units,
            units_by_id,
            policy.evaluator_dimensions,
        )
        target_remaining = policy.prospective_target - len(previous.current_unit_ids)
        candidates = candidates[:target_remaining]
        current_ids = (*previous.current_unit_ids, *(unit.unit_id for unit in candidates))
        comparison_index = previous.comparison_index
        current_summary = _merge_summaries(
            current_summary,
            _freeze_cohort(candidates, grouped=policy.grouping_mode != "none"),
        )
        pending = (*pending, *_pending_evaluator_units(
            candidates, tested_group_ids=tested_group_ids,
        ))
    else:
        candidates = candidates[:policy.prospective_target]
        current_ids = tuple(unit.unit_id for unit in candidates)
        comparison_index = previous.comparison_index + 1
        current_summary = _freeze_cohort(
            candidates,
            grouped=policy.grouping_mode != "none",
        )
        pending = _pending_evaluator_units(
            candidates, tested_group_ids=tested_group_ids,
        )
    admitted_late = sum(unit.event_time < previous.cutoff for unit in candidates)
    prospective_open = len(current_ids) < policy.prospective_target or bool(pending)
    cutoff = max((previous.cutoff, start_at, *(unit.event_time for unit in candidates)))
    consumed = (
        *previous.consumed_unit_ids,
        *(unit.unit_id for unit in candidates),
    )
    return _manifest(
        policy,
        cutoff,
        [],
        [],
        consumed,
        previous.late_unit_count + admitted_late
        if previous.prospective_open
        else admitted_late,
        prospective_open,
        comparison_index,
        previous.reference_summary,
        current_summary,
        previous.reference_unit_ids,
        current_ids,
        tuple(pending),
        prospective_start_at=start_at,
    )


def compare_manifest(units, manifest: CohortManifest, policy: MonitorPolicy) -> MonitorComparison:
    """Compare immutable cohort summaries; never reload approved outcomes."""
    if manifest.policy_fingerprint != policy.fingerprint:
        raise ValueError("manifest and policy do not match")
    if manifest.reference_summary is None or manifest.current_summary is None:
        raise ValueError("monitor policy requires re-bootstrap")
    alpha_threshold = _alpha_threshold(policy, manifest.comparison_index)
    reference = manifest.reference_summary
    current = manifest.current_summary
    reference_groups = {item.group_id: item for item in reference.groups}
    current_groups = {item.group_id: item for item in current.groups}
    group_ids = sorted(set(reference_groups) | set(current_groups))
    group_coverage = tuple(
        MonitorGroupCoverage(
            group_id,
            reference_groups.get(group_id).unit_count if group_id in reference_groups else 0,
            current_groups.get(group_id).unit_count if group_id in current_groups else 0,
            (reference_groups.get(group_id) or current_groups[group_id]).label,
            (reference_groups.get(group_id) or current_groups[group_id]).provider,
            (reference_groups.get(group_id) or current_groups[group_id]).model,
        )
        for group_id in group_ids
    )
    grouped = policy.grouping_mode != "none"
    reference_metrics = {(item.group_id, item.metric): item for item in reference.metrics}
    current_metrics = {(item.group_id, item.metric): item for item in current.metrics}
    coverage_keys = sorted(
        {
            key
            for source in (reference_metrics, current_metrics)
            for key, counts in source.items()
            if counts.reports_coverage
        },
        key=lambda item: (item[0] or "", item[1]),
    )
    metric_coverage = tuple(
        _coverage_from_frozen_counts(
            group_id,
            metric,
            reference_metrics.get((group_id, metric)),
            current_metrics.get((group_id, metric)),
        )
        for group_id, metric in coverage_keys
    )
    if manifest.prospective_open:
        return MonitorComparison(
            MonitorStatus.INSUFFICIENT,
            (),
            0.0,
            alpha_threshold,
            metric_coverage,
            groups=group_coverage,
        )
    if (
        reference.unit_count < policy.minimum_reference
        or current.unit_count < policy.minimum_current
    ):
        return MonitorComparison(
            MonitorStatus.INSUFFICIENT,
            (),
            0.0,
            alpha_threshold,
            metric_coverage,
            groups=group_coverage,
        )
    reference_group_ids = set(reference_groups)
    unseen = (
        current.unassigned_unit_count
        + sum(
            item.unit_count for item in current.groups if item.group_id not in reference_group_ids
        )
        if grouped
        else 0
    )
    unseen_share = unseen / current.unit_count if current.unit_count else 0.0
    unassigned_share = (
        current.unassigned_unit_count / current.unit_count
        if grouped and current.unit_count
        else 0.0
    )
    if unseen_share > policy.maximum_unseen_group_share:
        return MonitorComparison(
            MonitorStatus.REFERENCE_STALE,
            (),
            unseen_share,
            alpha_threshold,
            metric_coverage,
            unassigned_share,
            group_coverage,
        )
    comparison_groups = sorted(reference_group_ids & set(current_groups)) if grouped else [None]
    raw = []
    for group_id in comparison_groups:
        metric_names = sorted(
            {
                metric
                for source in (reference_metrics, current_metrics)
                for candidate_group, metric in source
                if candidate_group == group_id
            }
        )
        for name in metric_names:
            reference_counts = reference_metrics.get((group_id, name))
            current_counts = current_metrics.get((group_id, name))
            reference_true = reference_counts.true_count if reference_counts else 0
            reference_false = reference_counts.false_count if reference_counts else 0
            current_true = current_counts.true_count if current_counts else 0
            current_false = current_counts.false_count if current_counts else 0
            reference_n = reference_true + reference_false
            current_n = current_true + current_false
            if reference_n < policy.minimum_reference or current_n < policy.minimum_current:
                continue
            p_value = fisher_exact_two_sided(
                reference_true,
                reference_false,
                current_true,
                current_false,
            )
            reference_rate = reference_true / reference_n
            current_rate = current_true / current_n
            raw.append(
                (
                    group_id,
                    name,
                    reference_n,
                    current_n,
                    reference_rate,
                    current_rate,
                    current_rate - reference_rate,
                    p_value,
                )
            )
    adjusted = benjamini_hochberg([item[-1] for item in raw])
    metrics = tuple(
        MetricComparison(
            name, reference_n, current_n, reference_value, current_value,
            effect, p_value, p_adjusted,
            p_adjusted <= alpha_threshold and abs(effect) >= policy.minimum_effect,
            group_id,
        )
        for (
            group_id, name, reference_n, current_n, reference_value,
            current_value, effect, p_value,
        ), p_adjusted
        in zip(raw, adjusted, strict=True)
    )
    if not metrics:
        return MonitorComparison(
            MonitorStatus.INSUFFICIENT,
            (),
            unseen_share,
            alpha_threshold,
            metric_coverage,
            unassigned_share,
            group_coverage,
        )
    status = MonitorStatus.ALERT if any(metric.alert for metric in metrics) else MonitorStatus.NO_ALERT
    return MonitorComparison(
        status, metrics, unseen_share, alpha_threshold, metric_coverage,
        unassigned_share,
        group_coverage,
    )


def _coverage_from_frozen_counts(
    group_id: str | None,
    metric: str,
    reference: FrozenMetricCounts | None,
    current: FrozenMetricCounts | None,
) -> MetricEvidenceCoverage:
    reference = reference or FrozenMetricCounts(group_id, metric, 0, 0)
    current = current or FrozenMetricCounts(group_id, metric, 0, 0)
    return MetricEvidenceCoverage(
        metric,
        reference.state_pass + reference.state_fail,
        reference.state_unclear,
        reference.state_missing,
        reference.state_error,
        current.state_pass + current.state_fail,
        current.state_unclear,
        current.state_missing,
        current.state_error,
        group_id,
    )


def _alpha_threshold(policy: MonitorPolicy, comparison_index: int) -> float:
    """Spend at most ``p_threshold`` across an unbounded sequence of looks.

    Historical previews use the nominal threshold but are never authoritative.
    Prospective look ``k`` uses alpha * 6 / (pi^2 * k^2); the series sums to
    alpha, while Benjamini-Hochberg still controls metrics within each look.
    """
    if comparison_index <= 0:
        return policy.p_threshold
    return policy.p_threshold * 6.0 / (math.pi**2 * comparison_index**2)


def _bounded_group_text(value: object, *, maximum: int = 240) -> str:
    text = str(value) if value not in (None, "") else "unknown"
    text = text.encode("utf-8", "replace").decode("utf-8").replace("\x00", "�")
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text
    suffix = f"…{hashlib.sha256(encoded).hexdigest()[:12]}"
    budget = maximum - len(suffix.encode("utf-8"))
    prefix = encoded[:budget].decode("utf-8", "ignore")
    return prefix + suffix


def _provider_model_group(provider: object, model: object) -> tuple[str, str, str, str]:
    raw_provider = None if provider is None else str(provider)
    raw_model = None if model is None else str(model)
    identity = hashlib.sha256(
        json.dumps(
            [raw_provider, raw_model],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    display_provider = _bounded_group_text(raw_provider)
    display_model = _bounded_group_text(raw_model)
    return (
        f"provider_model:{identity}",
        f"{display_provider} / {display_model}",
        display_provider,
        display_model,
    )


def trace_monitor_units(
    traces,
    *,
    grouping_mode: str = "none",
    cluster_assignments: Mapping[str, str] | None = None,
    judgments_by_trace: Mapping[str, object] | None = None,
    evaluator_dimensions: tuple[str, ...] = (),
    cluster_labels: Mapping[str, str] | None = None,
) -> tuple[AnalysisUnitRecord, ...]:
    """Project genuine LLM calls and one frozen evaluator into monitor units."""
    from verdict.schema import JudgmentStatus
    from verdict.structural import is_refusal
    from verdict.trace_facts import trace_evidence_reason, trace_judge_evidence_digest

    units = []
    for trace in traces:
        if (
            trace.ended_at is None
            or trace.tags.get("verdict.workload") in {"judge", "paired_replay"}
        ):
            continue
        metrics = {"provider_error": bool(trace.error)}
        if trace.response_redacted is not None:
            metrics.update({
                "response_empty": not bool(trace.response_redacted.strip()),
                "refusal_signature": is_refusal(trace.response_redacted),
            })
        metric_states = {}
        evaluator_state = "not_requested"
        evaluator_evidence_digest = None
        if evaluator_dimensions:
            judgment = (judgments_by_trace or {}).get(trace.trace_id)
            evaluator_evidence_digest = trace_judge_evidence_digest(
                error=trace.error,
                prompt=trace.prompt_redacted,
                response=trace.response_redacted,
            )
            evidence_reason = trace_evidence_reason(
                error=trace.error,
                prompt=trace.prompt_redacted,
                response=trace.response_redacted,
            )
            if evidence_reason is not None:
                evaluator_state = "not_evaluable"
                states = judgment_metric_states(None, evaluator_dimensions)
            elif getattr(judgment, "status", None) is JudgmentStatus.COMPLETED:
                evaluator_state = "completed"
                states = judgment_metric_states(judgment, evaluator_dimensions)
            else:
                evaluator_state = "error" if judgment is not None else "pending"
                states = judgment_metric_states(judgment, evaluator_dimensions)
            for dimension, state in states.items():
                metric = f"judge.{dimension}.pass"
                metric_states[metric] = state
                if state in {"pass", "fail"}:
                    metrics[metric] = state == "pass"
        if grouping_mode == "none":
            group_id = None
            group_label = group_provider = group_model = None
        elif grouping_mode == "provider_model":
            group_id, group_label, group_provider, group_model = _provider_model_group(
                trace.provider,
                trace.request_model or trace.response_model,
            )
        elif grouping_mode == "cluster":
            group_id = (cluster_assignments or {}).get(trace.trace_id)
            group_label = (
                (cluster_labels or {}).get(group_id, group_id) if group_id is not None else None
            )
            group_provider = group_model = None
        else:
            raise ValueError("grouping_mode is unsupported")
        units.append(
            AnalysisUnitRecord(
                trace.trace_id,
                trace.started_at,
                metrics,
                group_id,
                metric_states,
                group_label,
                group_provider,
                group_model,
                evaluator_state,
                evaluator_evidence_digest,
            )
        )
    return tuple(units)


def judgment_metric_states(
    judgment: object | None,
    expected_dimensions: tuple[str, ...],
) -> dict[str, str]:
    """Classify one evaluator result without coercing absent evidence to FAIL."""
    from verdict.metrics import verdict_label
    from verdict.schema import JudgmentStatus

    if judgment is None:
        return {dimension: "missing" for dimension in expected_dimensions}
    if getattr(judgment, "status", None) is not JudgmentStatus.COMPLETED:
        return {dimension: "error" for dimension in expected_dimensions}
    by_name: dict[str, list[object]] = {}
    for score in getattr(judgment, "dimensions", ()):
        by_name.setdefault(getattr(score, "name", ""), []).append(score)
    states = {}
    for dimension in expected_dimensions:
        matches = by_name.get(dimension, [])
        if not matches:
            states[dimension] = "missing"
        elif len(matches) != 1:
            states[dimension] = "unclear"
        else:
            states[dimension] = verdict_label(getattr(matches[0], "verdict", None)).lower()
    return states


def monitor_policy_to_json(policy: MonitorPolicy) -> str:
    payload = {
        item.name: (
            value.isoformat() if isinstance(value, datetime)
            else value.value if isinstance(value, Enum) else value
        )
        for item in fields(policy)
        for value in (getattr(policy, item.name),)
        if not (item.name == "cluster_registry_version_id" and value is None)
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def monitor_policy_from_json(payload_json: str) -> MonitorPolicy:
    try:
        payload = json.loads(payload_json)
        for name in ("reference_start", "reference_end", "current_start", "current_end"):
            if payload.get(name) is not None:
                payload[name] = datetime.fromisoformat(payload[name])
        if "evaluator_dimensions" in payload:
            payload["evaluator_dimensions"] = tuple(payload["evaluator_dimensions"])
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
            "pending_evaluator_units": [
                {
                    "unit_id": item.unit_id,
                    "group_id": item.group_id,
                    "prior_state": item.prior_state,
                    "evidence_digest": item.evidence_digest,
                }
                for item in manifest.pending_evaluator_units
            ],
            "evidence_finalization_version": manifest.evidence_finalization_version,
        },
        "comparison": {
            "status": comparison.status.value,
            "unseen_group_share": comparison.unseen_group_share,
            "unassigned_group_share": comparison.unassigned_group_share,
            "alpha_threshold": comparison.alpha_threshold,
            "metrics": [
                {
                    item.name: getattr(metric, item.name)
                    for item in fields(metric)
                    if not (
                        item.name == "group_id" and getattr(metric, item.name) is None
                    )
                }
                for metric in comparison.metrics
            ],
            "metric_coverage": [
                {
                    item.name: getattr(coverage, item.name)
                    for item in fields(coverage)
                    if not (
                        item.name == "group_id" and getattr(coverage, item.name) is None
                    )
                }
                for coverage in comparison.metric_coverage
            ],
            "groups": [
                {
                    item.name: getattr(group, item.name)
                    for item in fields(group)
                    if getattr(group, item.name) is not None
                }
                for group in comparison.groups
            ],
        },
    }
    if manifest.reference_summary is not None:
        payload["manifest"]["reference_summary"] = _serialized_summary(manifest.reference_summary)
        payload["manifest"]["current_summary"] = _serialized_summary(manifest.current_summary)
    if manifest.prospective_start_at is not None:
        payload["manifest"]["prospective_start_at"] = manifest.prospective_start_at.isoformat()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if not 2 <= len(encoded.encode("utf-8")) <= MAX_MONITOR_SNAPSHOT_BYTES:
        raise ValueError("monitor snapshot exceeds the 4 MiB storage contract")
    return encoded


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
            (
                _summary_from_payload(manifest_data["reference_summary"])
                if "reference_summary" in manifest_data
                else None
            ),
            (
                _summary_from_payload(manifest_data["current_summary"])
                if "current_summary" in manifest_data
                else None
            ),
            tuple(
                FrozenPendingEvaluatorUnit(**item)
                for item in manifest_data.get("pending_evaluator_units", [])
            ),
            manifest_data.get("evidence_finalization_version", 0),
            (
                datetime.fromisoformat(manifest_data["prospective_start_at"])
                if manifest_data.get("prospective_start_at") is not None
                else None
            ),
        )
        comparison = MonitorComparison(
            MonitorStatus(comparison_data["status"]),
            tuple(MetricComparison(**metric) for metric in comparison_data["metrics"]),
            comparison_data["unseen_group_share"],
            comparison_data.get("alpha_threshold", 0.05),
            tuple(
                MetricEvidenceCoverage(**coverage)
                for coverage in comparison_data.get("metric_coverage", [])
            ),
            comparison_data.get("unassigned_group_share", 0.0),
            tuple(MonitorGroupCoverage(**group) for group in comparison_data.get("groups", [])),
        )
        return manifest, comparison
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid monitor snapshot JSON") from exc
