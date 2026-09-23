"""Bounded, content-free tool metadata reads for Agent Turn evaluation."""

from __future__ import annotations

from verdict.agent_judgment import (
    MAX_TOOL_EVIDENCE_EVENTS,
    TurnToolCounts,
    tool_counts_from_rows,
)


def _error_flag_sql(postgres: bool) -> str:
    if postgres:
        return (
            "CASE WHEN jsonb_typeof(attributes_json->'is_error')='boolean' "
            "THEN CASE WHEN attributes_json->>'is_error'='true' THEN 1 ELSE 0 END "
            "ELSE NULL END"
        )
    return (
        "CASE WHEN json_valid(attributes_json) THEN CASE "
        "WHEN json_type(attributes_json,'$.is_error')='true' THEN 1 "
        "WHEN json_type(attributes_json,'$.is_error')='false' THEN 0 "
        "ELSE NULL END ELSE NULL END"
    )


def tool_metadata_sql(*, postgres: bool) -> str:
    placeholder = "%s" if postgres else "?"
    return (
        f"SELECT event_type,status,{_error_flag_sql(postgres)} AS is_error FROM agent_events "
        f"WHERE tenant_id={placeholder} AND run_id={placeholder} AND turn_id={placeholder} "
        f"ORDER BY sequence,event_id LIMIT {placeholder}"
    )


def read_turn_tool_counts(cursor, *, postgres: bool, tenant_id: str,
                          run_id: str, turn_id: str) -> TurnToolCounts:
    cursor.execute(
        tool_metadata_sql(postgres=postgres),
        (tenant_id, run_id, turn_id, MAX_TOOL_EVIDENCE_EVENTS + 1),
    )
    rows = [
        (event_type, status, None if is_error is None else bool(is_error))
        for event_type, status, is_error in cursor.fetchall()
    ]
    return tool_counts_from_rows(rows)


def read_turn_tool_counts_batch(
    cursor, *, postgres: bool, tenant_id: str,
    turn_keys: list[tuple[str, str]],
    session: bool = False,
) -> dict[tuple[str, str], TurnToolCounts]:
    """One metadata-only statement, limited to N+1 events per Turn."""
    if not turn_keys:
        return {}
    if len(turn_keys) > 100 or len(set(turn_keys)) != len(turn_keys):
        raise ValueError("invalid bounded Turn evidence page")
    placeholder = "%s" if postgres and not session else "?"
    wanted = ",".join(f"({placeholder},{placeholder})" for _ in turn_keys)
    if postgres:
        event_join = (
            "CROSS JOIN LATERAL (SELECT event_type,status,attributes_json "
            f"FROM agent_events WHERE tenant_id={placeholder} AND run_id=w.run_id "
            f"AND turn_id=w.turn_id ORDER BY sequence,event_id LIMIT {placeholder}) e"
        )
    else:
        event_join = (
            "JOIN agent_events e ON e.rowid IN ("
            f"SELECT s.rowid FROM agent_events s WHERE s.tenant_id={placeholder} "
            "AND s.run_id=w.run_id AND s.turn_id=w.turn_id "
            f"ORDER BY s.sequence,s.event_id LIMIT {placeholder})"
        )
    result = cursor.execute(
        f"WITH wanted(run_id,turn_id) AS (VALUES {wanted}) "
        f"SELECT w.run_id,w.turn_id,e.event_type,e.status,"
        f"{_error_flag_sql(postgres)} AS is_error "
        f"FROM wanted w {event_join}",
        (*(value for key in turn_keys for value in key),
         tenant_id, MAX_TOOL_EVIDENCE_EVENTS + 1),
    )
    rows_by_turn: dict[tuple[str, str], list[tuple[str, str, bool | None]]] = {
        key: [] for key in turn_keys
    }
    for row in result:
        run_id, turn_id, event_type, status, is_error = (
            tuple(row[key] for key in ("run_id", "turn_id", "event_type", "status", "is_error"))
            if hasattr(row, "keys") else row
        )
        rows_by_turn[(run_id, turn_id)].append(
            (event_type, status, None if is_error is None else bool(is_error))
        )
    return {key: tool_counts_from_rows(rows) for key, rows in rows_by_turn.items()}
