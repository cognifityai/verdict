"""Bounded, read-only coverage preview for reported Agent Run tool outcomes."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from verdict.analysis import recorded_tool_pairing_complete
from verdict.dashboard.agent_evidence_queries import available
from verdict.dashboard.query import PostgresSession, QuerySession, SQLiteSession
from verdict.dashboard.storage_url import is_postgres_storage
from verdict.redaction import redact_structure
from verdict.storage.sqlite import sqlite_path

_TERMINAL = ("completed", "failed", "timed_out", "cancelled")
_EXECUTION_STATES = (*_TERMINAL, "unfinalized")
_EXAMPLES = 3
_RUN_BATCH = 400


def _classification(events: list[dict]) -> tuple[str, bool]:
    """Classify recorded tool outcomes without treating missing results as clean."""
    failed = False
    successful = False
    unclear = False
    conflict = False
    calls: list[str | None] = []
    results: list[str | None] = []
    for event in events:
        if event["provenance"] == "verdict:capture_limit":
            unclear = True
        if event["event_type"] == "tool_call":
            calls.append(event["call_id"])
            continue
        if event["event_type"] != "tool_result":
            continue
        results.append(event["call_id"])
        status = event["status"]
        error = event["is_error"]
        if not event["attributes_valid"]:
            unclear = True
        if (status == "completed" and error == 1) or (
            status in {"failed", "timed_out"} and error == 0
        ):
            conflict = True
        if status in {"failed", "timed_out"} or error == 1:
            failed = True
        elif status == "cancelled":
            unclear = True
        elif status == "completed":
            successful = True
        else:
            unclear = True
    if failed:
        return "recordedFailure", conflict
    if calls and not recorded_tool_pairing_complete(calls, results):
        unclear = True
    if successful and not unclear:
        return "observedWithoutFailure", conflict
    return "unclear", conflict


def _period_counts(states: list[str]) -> dict[str, int]:
    return {
        "terminalRuns": len(states),
        "recordedFailure": states.count("recordedFailure"),
        "observedWithoutFailure": states.count("observedWithoutFailure"),
        "unclear": states.count("unclear"),
    }


def _execution_preview(session: QuerySession, tenant: str, max_runs: int) -> dict:
    """Report bounded run status coverage, independently of tool-result evidence."""
    rows = list(session.execute(
        "SELECT r.run_id,r.status,r.ended_at,r.agent_name,r.service_name,"
        "r.environment,s.source_kind FROM agent_runs r JOIN import_sources s "
        "ON s.tenant_id=r.tenant_id AND s.source_session_id=r.source_session_id "
        "WHERE r.tenant_id=? ORDER BY r.started_at DESC,r.run_id DESC LIMIT ?",
        (tenant, max_runs + 1),
    ))
    truncated = len(rows) > max_runs
    counts = {state: 0 for state in _EXECUTION_STATES}
    groups: dict[tuple[str, str, str, str], dict] = {}
    for row in rows[:max_runs]:
        state = (row["status"] if row["ended_at"] is not None
                 and row["status"] in _TERMINAL else "unfinalized")
        counts[state] += 1
        identity = tuple(str(row[key] or "") for key in
                         ("source_kind", "service_name", "environment", "agent_name"))
        group = groups.setdefault(identity, {
            "sourceKind": identity[0], "serviceName": identity[1],
            "environment": identity[2], "agentName": identity[3],
            "counts": {name: 0 for name in _EXECUTION_STATES},
            "examples": {name: [] for name in _EXECUTION_STATES},
        })
        group["counts"][state] += 1
        if len(group["examples"][state]) < _EXAMPLES:
            group["examples"][state].append(str(row["run_id"]))
    ordered = sorted(groups.values(), key=lambda item: (
        -sum(item["counts"].values()), item["sourceKind"], item["serviceName"],
        item["environment"], item["agentName"],
    ))
    return {
        "scope": {"sampledRuns": min(len(rows), max_runs), "truncated": truncated},
        "totals": counts, "groups": ordered[:20],
        "groupsTruncated": len(ordered) > 20,
    }


def _project(session: QuerySession, tenant: str, *, max_runs: int,
             max_events: int, postgres: bool) -> dict:
    empty = {
        "schema": "agent-run-outcome-preview-v1",
        "status": "no_terminal_runs",
        "scope": {"terminalRuns": 0, "sampledTerminalRuns": 0, "truncated": False},
        "totals": {"recordedFailure": 0, "observedWithoutFailure": 0,
                   "unclear": 0, "conflicts": 0},
        "groups": [],
        "execution": {"scope": {"sampledRuns": 0, "truncated": False},
                      "totals": {state: 0 for state in _EXECUTION_STATES},
                      "groups": [], "groupsTruncated": False},
    }
    if not available(session):
        return empty
    if postgres:
        indexed = session.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_index i "
            "JOIN pg_class c ON c.oid=i.indexrelid "
            "WHERE i.indrelid=to_regclass('agent_runs') "
            "AND c.relname='idx_agent_runs_tenant_ended') AS present"
        ).fetchone()["present"]
    else:
        indexed = session.execute(
            "SELECT 1 AS present FROM sqlite_master WHERE type='index' "
            "AND tbl_name='agent_runs' AND name='idx_agent_runs_tenant_ended'"
        ).fetchone()
    if not indexed:
        return {**empty, "status": "migration_required", "totals": None}
    execution = _execution_preview(session, tenant, max_runs)
    terminal = ",".join("?" for _ in _TERMINAL)
    recent = list(session.execute(
        "SELECT r.run_id,r.agent_name,r.service_name,r.environment,s.source_kind "
        "FROM agent_runs r JOIN import_sources s "
        "ON s.tenant_id=r.tenant_id AND s.source_session_id=r.source_session_id "
        f"WHERE r.tenant_id=? AND r.status IN ({terminal}) "
        "AND r.ended_at IS NOT NULL "
        "ORDER BY r.ended_at DESC,r.run_id DESC LIMIT ?",
        (tenant, *_TERMINAL, max_runs + 1),
    ))
    if not recent:
        return {**empty, "execution": execution}
    truncated = len(recent) > max_runs
    rows = recent[:max_runs]
    result = dict(empty)
    result["execution"] = execution
    result["status"] = "ready"
    result["scope"] = {"terminalRuns": None if truncated else len(rows),
                       "sampledTerminalRuns": len(rows),
                       "truncated": truncated}

    # Read only outcome flags, bounded call IDs, and capture-limit markers.
    # Event bodies never cross this read model boundary.
    if postgres:
        valid_sql = (
            "CASE WHEN jsonb_typeof(e.attributes_json)='object' THEN 1 ELSE 0 END"
        )
        flag_sql = (
            "CASE WHEN jsonb_typeof(e.attributes_json->'is_error')='boolean' "
            "THEN CASE WHEN e.attributes_json->>'is_error'='true' THEN 1 ELSE 0 END "
            "ELSE NULL END"
        )
        call_id_sql = (
            "CASE WHEN jsonb_typeof(e.attributes_json->'call_id')='string' "
            "THEN e.attributes_json->>'call_id' ELSE NULL END"
        )
    else:
        valid_sql = (
            "CASE WHEN json_valid(e.attributes_json) THEN "
            "CASE WHEN json_type(e.attributes_json)='object' THEN 1 ELSE 0 END "
            "ELSE 0 END"
        )
        flag_type = "json_type(e.attributes_json,'$.is_error')"
        flag_sql = (
            f"CASE WHEN {valid_sql}=1 THEN CASE WHEN {flag_type}='true' THEN 1 "
            f"WHEN {flag_type}='false' THEN 0 END END"
        )
        call_id_sql = (
            f"CASE WHEN {valid_sql}=1 THEN CASE WHEN "
            "json_type(e.attributes_json,'$.call_id')='text' "
            "THEN json_extract(e.attributes_json,'$.call_id') END END"
        )
    by_run: dict[str, list[dict]] = {str(row["run_id"]): [] for row in rows}
    seen_events = 0
    for offset in range(0, len(rows), _RUN_BATCH):
        selected = tuple(str(row["run_id"]) for row in rows[offset:offset + _RUN_BATCH])
        placeholders = ",".join("?" for _ in selected)
        events = list(session.execute(
            "SELECT e.run_id,e.event_type,e.provenance,e.status,"
            + valid_sql + " AS attributes_valid,"
            + flag_sql + " AS is_error," + call_id_sql + " AS call_id "
            "FROM agent_events e WHERE e.tenant_id=? "
            "AND (e.event_type IN ('tool_call','tool_result') "
            "OR e.provenance='verdict:capture_limit') "
            f"AND e.run_id IN ({placeholders}) "
            "ORDER BY e.run_id,e.event_id LIMIT ?",
            (tenant, *selected, max_events - seen_events + 1),
        ))
        seen_events += len(events)
        if seen_events > max_events:
            result["status"] = "event_limit"
            result["totals"] = None
            result["groups"] = []
            return result
        for event in events:
            by_run[str(event["run_id"])].append(event)

    totals = {"recordedFailure": 0, "observedWithoutFailure": 0,
              "unclear": 0, "conflicts": 0}
    groups: dict[tuple[str, str, str, str], dict] = {}
    group_states: dict[tuple[str, str, str, str], list[str]] = {}
    for row in rows:
        run_id = str(row["run_id"])
        identity = tuple(str(row[key] or "") for key in
                         ("source_kind", "service_name", "environment", "agent_name"))
        if identity not in groups:
            groups[identity] = {
                "sourceKind": identity[0], "serviceName": identity[1],
                "environment": identity[2], "agentName": identity[3],
                "terminalRuns": 0,
                "recordedFailure": 0, "observedWithoutFailure": 0,
                "unclear": 0, "conflicts": 0,
                "examples": {"recordedFailure": [], "observedWithoutFailure": [],
                             "unclear": []},
                "comparison": None,
            }
            group_states[identity] = []
        group = groups[identity]
        state, conflict = _classification(by_run[run_id])
        group_states[identity].append(state)
        group["terminalRuns"] += 1
        group[state] += 1
        totals[state] += 1
        if conflict:
            group["conflicts"] += 1
            totals["conflicts"] += 1
        examples = group["examples"][state]
        if len(examples) < _EXAMPLES:
            examples.append(run_id)
    if not truncated:
        for identity, group in groups.items():
            # The indexed scan is newest first by ended_at and run_id. Reverse
            # its stable order within each exact group for an earlier/newer
            # descriptive split; missing outcomes remain explicit counts.
            chronology = group_states[identity][::-1]
            if len(chronology) >= 2:
                midpoint = len(chronology) // 2
                group["comparison"] = {
                    "earlier": _period_counts(chronology[:midpoint]),
                    "newer": _period_counts(chronology[midpoint:]),
                }
    result["totals"] = totals
    result["groups"] = sorted(groups.values(),
                              key=lambda item: (-item["terminalRuns"],
                                                item["sourceKind"], item["agentName"]))[:20]
    result["groupsTruncated"] = len(groups) > 20
    return result


def build_run_outcome_preview(storage: str, *, tenant: str, max_runs: int = 1000,
                              max_events: int = 10_000) -> dict:
    """Read tenant-scoped metadata from one consistent snapshot without writes."""
    if (not isinstance(tenant, str) or not tenant or len(tenant.encode("utf-8")) > 256
            or not 1 <= max_runs <= 1000 or not 1 <= max_events <= 10_000):
        raise ValueError("invalid run outcome preview")
    postgres = is_postgres_storage(storage)
    if postgres:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(storage, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                result = _project(PostgresSession(connection), tenant,
                                  max_runs=max_runs, max_events=max_events,
                                  postgres=True)
    else:
        path = sqlite_path(storage)
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            result = _project(SQLiteSession(connection), tenant,
                              max_runs=max_runs, max_events=max_events,
                              postgres=False)
            connection.commit()
    # Run IDs are existing dashboard-scoped navigation keys (also returned by
    # /api/runs). Redacting one changes its identity and breaks investigation.
    # Redact descriptive fields, then restore only the bounded exact IDs.
    examples = [group["examples"] for group in result["groups"]]
    execution_examples = [group["examples"] for group in result["execution"]["groups"]]
    display = dict(result)
    display["groups"] = [
        {**group, "examples": {state: [] for state in group["examples"]}}
        for group in result["groups"]
    ]
    display["execution"] = {
        **result["execution"],
        "groups": [
            {**group, "examples": {state: [] for state in group["examples"]}}
            for group in result["execution"]["groups"]
        ],
    }
    redacted = redact_structure(display)
    if not isinstance(redacted, dict):
        raise ValueError("run outcome preview exceeds redaction budget")
    for group, exact in zip(redacted["groups"], examples, strict=True):
        group["examples"] = exact
    for group, exact in zip(redacted["execution"]["groups"],
                            execution_examples, strict=True):
        group["examples"] = exact
    return redacted
