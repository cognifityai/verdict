"""Project recorded Agent Run outcomes into historical monitor units.

Run units reuse the trace monitor's cohort, Fisher, Benjamini-Hochberg and
minimum-effect machinery. They are historical only: a run's outcome can still
change while it is open, so prospective run policies are not supported.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from verdict.analysis import outcome_reported_values, recorded_event_issue
from verdict.evidence import AgentEventType, ExecutionStatus
from verdict.monitoring import AnalysisUnitRecord

# A run without a terminal status is settled once it has recorded nothing for
# this long. Local Claude Code and Codex histories never record a terminal run
# status, so their runs settle only through inactivity.
RUN_SETTLE_SECONDS = 30 * 60

MAX_RUN_MONITOR_RUNS = 100_000
_FAILED = frozenset({ExecutionStatus.FAILED.value, ExecutionStatus.TIMED_OUT.value})


@dataclass(frozen=True, slots=True)
class RunEventCount:
    """Content-free count of one run's events with the same outcome fields."""

    event_type: str
    status: str
    is_error: bool | None
    count: int


@dataclass(frozen=True, slots=True)
class RunMonitorFacts:
    """Bounded, content-free outcome facts for one Agent Run."""

    run_id: str
    started_at: datetime
    status: ExecutionStatus
    agent_name: str
    last_event_at: datetime | None
    capture_limited: bool
    event_counts: tuple[RunEventCount, ...]


def _settled(facts: RunMonitorFacts, now: datetime) -> bool:
    if facts.status is not ExecutionStatus.UNKNOWN:
        return True
    last_activity = max(
        value for value in (facts.started_at, facts.last_event_at) if value is not None
    )
    return last_activity <= now - timedelta(seconds=RUN_SETTLE_SECONDS)


def _run_metrics(facts: RunMonitorFacts) -> dict[str, bool]:
    metrics: dict[str, bool] = {}
    if facts.status is not ExecutionStatus.UNKNOWN:
        metrics["run_failed"] = facts.status.value in _FAILED
    # Omitted events can hide a failure, so a capture-limited run measures no
    # event metric instead of reporting a clean run.
    if facts.capture_limited:
        return metrics
    tool_results = [
        item for item in facts.event_counts
        if item.event_type == AgentEventType.TOOL_RESULT.value
        and outcome_reported_values(item.status, {"is_error": item.is_error})
    ]
    if tool_results:
        metrics["run_tool_error"] = any(
            recorded_event_issue(item.event_type, item.status, {"is_error": item.is_error})
            == "tool_error"
            for item in tool_results
        )
    model_calls = [
        item for item in facts.event_counts
        if item.event_type == AgentEventType.MODEL_CALL.value
        and item.status != ExecutionStatus.UNKNOWN.value
    ]
    if model_calls:
        metrics["run_model_call_error"] = any(item.status in _FAILED for item in model_calls)
    return metrics


def _agent_group(agent_name: str) -> tuple[str, str]:
    label = agent_name or "unnamed agent"
    digest = hashlib.sha256(agent_name.encode("utf-8")).hexdigest()
    return f"agent:{digest}", label


def run_monitor_units(
    facts: Iterable[RunMonitorFacts],
    *,
    now: datetime,
    grouping_mode: str = "none",
) -> tuple[AnalysisUnitRecord, ...]:
    """Return settled runs that started before the earliest unsettled run.

    Admitting only runs older than every open run keeps cohorts from filling
    with short runs, which settle first and fail more often, while longer runs
    that started at the same time are still open.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if grouping_mode not in {"none", "agent"}:
        raise ValueError("run monitors support no grouping or agent grouping")
    facts = list(facts)
    watermark = min(
        (item.started_at for item in facts if not _settled(item, now)),
        default=None,
    )
    units = []
    for item in facts:
        if not _settled(item, now) or (watermark is not None and item.started_at >= watermark):
            continue
        group_id = group_label = None
        if grouping_mode == "agent":
            group_id, group_label = _agent_group(item.agent_name)
        units.append(AnalysisUnitRecord(
            item.run_id,
            item.started_at,
            _run_metrics(item),
            group_id,
            None,
            group_label,
        ))
    return tuple(units)


def load_run_monitor_units(
    storage, *, tenant_id: str, now: datetime, grouping_mode: str = "none",
) -> tuple[AnalysisUnitRecord, ...]:
    """Read bounded run facts and project the settled runs into monitor units."""
    reader = getattr(storage, "load_run_monitor_facts", None)
    if not callable(reader):
        raise ValueError("run comparison is unavailable for this storage")
    return run_monitor_units(reader(tenant_id), now=now, grouping_mode=grouping_mode)
