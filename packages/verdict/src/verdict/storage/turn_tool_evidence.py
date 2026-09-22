"""Bounded, content-free tool metadata reads for Agent Turn evaluation."""

from __future__ import annotations

from verdict.agent_judgment import (
    MAX_TOOL_EVIDENCE_EVENTS,
    TurnToolCounts,
    tool_counts_from_rows,
)


def tool_metadata_sql(*, postgres: bool) -> str:
    placeholder = "%s" if postgres else "?"
    if postgres:
        error_flag = (
            "CASE WHEN jsonb_typeof(attributes_json->'is_error')='boolean' "
            "THEN CASE WHEN attributes_json->>'is_error'='true' THEN 1 ELSE 0 END "
            "ELSE NULL END"
        )
    else:
        error_flag = (
            "CASE WHEN json_valid(attributes_json) THEN CASE "
            "WHEN json_type(attributes_json,'$.is_error')='true' THEN 1 "
            "WHEN json_type(attributes_json,'$.is_error')='false' THEN 0 "
            "ELSE NULL END ELSE NULL END"
        )
    return (
        f"SELECT event_type,status,{error_flag} AS is_error FROM agent_events "
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
