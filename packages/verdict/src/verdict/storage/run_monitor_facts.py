"""Bounded, content-free Agent Run outcome reads shared by the SQL adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from verdict.evidence import AgentEvent, AgentEventType, AgentRun, AgentTurn, ExecutionStatus
from verdict.local_source_revision import (
    LocalSourceRevision,
    local_scope_partial,
    revision_from_row,
)
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


_REVISION_COLUMNS = (
    "local_full_bytes", "local_full_sha256", "local_prefix_bytes", "local_prefix_sha256",
    "local_partial_line", "local_source_turn_count", "local_source_event_count",
    "local_v1_turn_count", "local_v1_event_count", "local_min_turn_at", "local_max_turn_at",
)


def _latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _source_partial(source_kind: str | None, revision: LocalSourceRevision | None) -> bool:
    # Whole-run scope: the run view's rule for an incomplete or unverified local source.
    return local_scope_partial(source_kind or "", revision, -1, None)


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
    turn_activity = {}
    for row in cursor.execute(
        "SELECT run_id,MAX(started_at) AS started,MAX(ended_at) AS ended "
        f"FROM agent_turns WHERE tenant_id={placeholder} GROUP BY run_id",
        (tenant_id,),
    ).fetchall():
        run_id, started, ended = _row(row, ("run_id", "started", "ended"))
        turn_activity[run_id] = _latest(_time(started), None if ended is None else _time(ended))
    partial_sources = set()
    columns = ("run_id", "source_kind", *_REVISION_COLUMNS)
    for row in cursor.execute(
        "SELECT r.run_id,s.source_kind," + ",".join(f"s.{name}" for name in _REVISION_COLUMNS) +
        " FROM agent_runs r LEFT JOIN import_sources s ON s.tenant_id=r.tenant_id "
        f"AND s.source_session_id=r.source_session_id WHERE r.tenant_id={placeholder}",
        (tenant_id,),
    ).fetchall():
        values = dict(zip(columns, _row(row, columns), strict=True))
        if _source_partial(values["source_kind"], revision_from_row(values)):
            partial_sources.add(values["run_id"])
    facts = []
    for row in runs:
        run_id, started_at, status, agent_name = _row(
            row, ("run_id", "started_at", "status", "agent_name"),
        )
        last_at, limited = activity.get(run_id, (None, False))
        facts.append(RunMonitorFacts(
            run_id, _time(started_at), ExecutionStatus(status), agent_name or "",
            _latest(last_at, turn_activity.get(run_id)), limited or run_id in partial_sources,
            tuple(sorted(
                counts.get(run_id, ()),
                key=lambda item: (item.event_type, item.status, str(item.is_error)),
            )),
        ))
    return facts


def run_monitor_facts_from_records(
    runs: Iterable[AgentRun], events: Iterable[tuple[str, AgentEvent]],
    turns: Iterable[AgentTurn],
    sources: Mapping[str, tuple[str, LocalSourceRevision | None]],
) -> list[RunMonitorFacts]:
    """The same projection over in-memory records, for adapter parity.

    ``sources`` maps a source session ID to its source kind and local revision.
    """
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
    turn_activity: dict[str, datetime] = {}
    for turn in turns:
        turn_activity[turn.run_id] = _latest(
            turn_activity.get(turn.run_id), turn.started_at, turn.ended_at,
        )
    facts = []
    for run in runs:
        last_at, limited = activity.get(run.run_id, (None, False))
        source_kind, revision = sources.get(run.source_session_id, (None, None))
        facts.append(RunMonitorFacts(
            run.run_id, run.started_at, run.status, run.agent_name,
            _latest(last_at, turn_activity.get(run.run_id)),
            limited or _source_partial(source_kind, revision),
            tuple(sorted(
                (RunEventCount(*key, count) for key, count in grouped.get(run.run_id, {}).items()),
                key=lambda item: (item.event_type, item.status, str(item.is_error)),
            )),
        ))
    return facts
