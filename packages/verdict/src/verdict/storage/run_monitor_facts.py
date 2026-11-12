"""Bounded, content-free Agent Run outcome reads shared by the SQL adapters."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from verdict.evidence import AgentEvent, AgentEventType, AgentRun, ExecutionStatus
from verdict.run_monitoring import MAX_RUN_MONITOR_RUNS, RunEventCount, RunMonitorFacts
from verdict.storage.turn_tool_evidence import _error_flag_sql

CAPTURE_LIMIT_PROVENANCE = "verdict:capture_limit"
_OUTCOME_EVENT_TYPES = (AgentEventType.TOOL_RESULT.value, AgentEventType.MODEL_CALL.value)


def _time(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _row(row, names: tuple[str, ...]) -> tuple:
    return tuple(row[name] for name in names) if hasattr(row, "keys") else tuple(row)


def read_run_monitor_facts(cursor, *, postgres: bool, tenant_id: str) -> list[RunMonitorFacts]:
    """Read every run's status and grouped outcome counts; never event content."""
    placeholder = "%s" if postgres else "?"
    runs = cursor.execute(
        "SELECT run_id,started_at,status,agent_name FROM agent_runs "
        f"WHERE tenant_id={placeholder} ORDER BY started_at,run_id LIMIT {placeholder}",
        (tenant_id, MAX_RUN_MONITOR_RUNS + 1),
    ).fetchall()
    if len(runs) > MAX_RUN_MONITOR_RUNS:
        raise ValueError("run comparison exceeds bounded run limit")
    types = ",".join(f"'{value}'" for value in _OUTCOME_EVENT_TYPES)
    counts: dict[str, list[RunEventCount]] = {}
    for row in cursor.execute(
        f"SELECT run_id,event_type,status,{_error_flag_sql(postgres)} AS is_error,"
        f"COUNT(*) AS n FROM agent_events WHERE tenant_id={placeholder} "
        f"AND event_type IN ({types}) GROUP BY run_id,event_type,status,is_error",
        (tenant_id,),
    ).fetchall():
        run_id, event_type, status, is_error, count = _row(
            row, ("run_id", "event_type", "status", "is_error", "n"),
        )
        counts.setdefault(run_id, []).append(RunEventCount(
            event_type, status, None if is_error is None else bool(is_error), int(count),
        ))
    activity = {}
    for row in cursor.execute(
        "SELECT run_id,MAX(occurred_at) AS last_at,"
        f"MAX(CASE WHEN provenance='{CAPTURE_LIMIT_PROVENANCE}' THEN 1 ELSE 0 END) AS limited "
        f"FROM agent_events WHERE tenant_id={placeholder} GROUP BY run_id",
        (tenant_id,),
    ).fetchall():
        run_id, last_at, limited = _row(row, ("run_id", "last_at", "limited"))
        activity[run_id] = (_time(last_at), bool(limited))
    facts = []
    for row in runs:
        run_id, started_at, status, agent_name = _row(
            row, ("run_id", "started_at", "status", "agent_name"),
        )
        last_at, limited = activity.get(run_id, (None, False))
        facts.append(RunMonitorFacts(
            run_id, _time(started_at), ExecutionStatus(status), agent_name or "",
            last_at, limited, tuple(sorted(
                counts.get(run_id, ()),
                key=lambda item: (item.event_type, item.status, str(item.is_error)),
            )),
        ))
    return facts


def run_monitor_facts_from_records(
    runs: Iterable[AgentRun], events: Iterable[tuple[str, AgentEvent]],
) -> list[RunMonitorFacts]:
    """The same projection over in-memory records, for adapter parity."""
    runs = sorted(runs, key=lambda run: (run.started_at, run.run_id))
    if len(runs) > MAX_RUN_MONITOR_RUNS:
        raise ValueError("run comparison exceeds bounded run limit")
    grouped: dict[str, dict[tuple[str, str, bool | None], int]] = {}
    activity: dict[str, tuple[datetime, bool]] = {}
    for run_id, event in events:
        last_at, limited = activity.get(run_id, (event.occurred_at, False))
        activity[run_id] = (
            max(last_at, event.occurred_at),
            limited or event.provenance == CAPTURE_LIMIT_PROVENANCE,
        )
        if event.event_type.value not in _OUTCOME_EVENT_TYPES:
            continue
        flag = event.attributes.get("is_error")
        key = (event.event_type.value, event.status.value, flag if isinstance(flag, bool) else None)
        bucket = grouped.setdefault(run_id, {})
        bucket[key] = bucket.get(key, 0) + 1
    return [
        RunMonitorFacts(
            run.run_id, run.started_at, run.status, run.agent_name,
            *activity.get(run.run_id, (None, False)),
            tuple(sorted(
                (RunEventCount(*key, count) for key, count in grouped.get(run.run_id, {}).items()),
                key=lambda item: (item.event_type, item.status, str(item.is_error)),
            )),
        )
        for run in runs
    ]
