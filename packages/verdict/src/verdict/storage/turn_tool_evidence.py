"""Bounded reads of the extra evidence an Agent Turn judge may see.

Tool evidence is content-free metadata. Next-message evidence is the next
Turn's request, which capture already redacted and bounded.
"""

from __future__ import annotations

from verdict.agent_judgment import (
    MAX_TOOL_EVIDENCE_EVENTS,
    NEXT_MESSAGE_MODE,
    TOOL_EVIDENCE_MODE,
    NextUserMessage,
    TurnEvidence,
    TurnToolCounts,
    tool_counts_from_rows,
)
from verdict.evidence import EvidenceState


def _error_flag_sql(postgres: bool) -> str:
    if postgres:
        return (
            "CASE WHEN jsonb_typeof(attributes_json->'is_error')='boolean' "
            "THEN CASE WHEN attributes_json->>'is_error'='true' THEN 1 ELSE 0 END "
            "ELSE NULL END"
        )
    return (
        "CASE WHEN typeof(attributes_json)='text' AND json_valid(attributes_json) THEN CASE "
        "WHEN json_type(attributes_json,'$.is_error')='true' THEN 1 "
        "WHEN json_type(attributes_json,'$.is_error')='false' THEN 0 "
        "ELSE NULL END ELSE NULL END"
    )


def tool_origin_code_sql(*, postgres: bool, prefix: str = "") -> str:
    """Classify one attributes object without returning its raw origin value."""
    column = f"{prefix}attributes_json"
    if postgres:
        return (
            f"CASE WHEN jsonb_typeof({column})<>'object' THEN 'unusable' "
            f"WHEN NOT jsonb_exists({column},'tool_origin') THEN 'not_captured' "
            f"WHEN jsonb_typeof({column}->'tool_origin')='string' "
            f"AND {column}->>'tool_origin' IN ('mcp','application','provider_hosted') "
            f"THEN {column}->>'tool_origin' ELSE 'unusable' END"
        )
    origin_count = (
        f"(SELECT COUNT(*) FROM json_each({column}) WHERE key='tool_origin')"
    )
    return (
        f"CASE WHEN typeof({column})<>'text' THEN 'unusable' "
        f"WHEN NOT json_valid({column}) THEN 'unusable' "
        f"WHEN json_type({column})<>'object' THEN 'unusable' "
        f"WHEN {origin_count}=0 THEN 'not_captured' "
        f"WHEN {origin_count}=1 "
        f"AND json_type({column},'$.tool_origin')='text' "
        f"AND json_extract({column},'$.tool_origin') "
        "IN ('mcp','application','provider_hosted') "
        f"THEN json_extract({column},'$.tool_origin') ELSE 'unusable' END"
    )


def tool_origin_aggregate_sql(*, postgres: bool, prefix: str = "") -> str:
    """Return the five shared call-origin aggregate expressions."""
    event_type = f"{prefix}event_type"
    origin = tool_origin_code_sql(postgres=postgres, prefix=prefix)
    aliases = (
        ("mcp", "mcp_calls"),
        ("application", "application_calls"),
        ("provider_hosted", "provider_hosted_calls"),
        ("not_captured", "origin_not_captured_calls"),
        ("unusable", "origin_unusable_calls"),
    )
    return ",".join(
        f"SUM(CASE WHEN {event_type}='tool_call' AND ({origin})='{code}' "
        f"THEN 1 ELSE 0 END) AS {alias}"
        for code, alias in aliases
    )


def tool_metadata_sql(*, postgres: bool) -> str:
    placeholder = "%s" if postgres else "?"
    return (
        f"SELECT event_type,status,{_error_flag_sql(postgres)} AS is_error,"
        f"{tool_origin_code_sql(postgres=postgres)} AS tool_origin FROM agent_events "
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
        (event_type, status, None if is_error is None else bool(is_error), tool_origin)
        for event_type, status, is_error, tool_origin in cursor.fetchall()
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
        f"{_error_flag_sql(postgres)} AS is_error,"
        f"{tool_origin_code_sql(postgres=postgres, prefix='e.')} AS tool_origin "
        f"FROM wanted w {event_join}",
        (*(value for key in turn_keys for value in key),
         tenant_id, MAX_TOOL_EVIDENCE_EVENTS + 1),
    )
    rows_by_turn: dict[tuple[str, str], list[tuple[str, str, bool | None, str]]] = {
        key: [] for key in turn_keys
    }
    for row in result:
        run_id, turn_id, event_type, status, is_error, tool_origin = (
            tuple(row[key] for key in (
                "run_id", "turn_id", "event_type", "status", "is_error", "tool_origin"
            ))
            if hasattr(row, "keys") else row
        )
        rows_by_turn[(run_id, turn_id)].append(
            (event_type, status, None if is_error is None else bool(is_error), tool_origin)
        )
    return {key: tool_counts_from_rows(rows) for key, rows in rows_by_turn.items()}


def _next_turn_joins(postgres: bool) -> str:
    # Mirrors ``next_user_messages``: ``n`` is the lowest later sequence in the
    # same run, so a sequence gap cannot pair a Turn with an unrelated request.
    # Only when there is none, ``m`` is the first Turn of the session's next
    # top-level run that has a Turn. Sub-agent runs never pair. Run IDs break
    # start-time ties in byte order, as in Python and SQLite, whatever the
    # Postgres database collation; the row-value comparison lets the session
    # index seek to the next run instead of scanning the session.
    run_id = 'run_id COLLATE "C"' if postgres else "run_id"
    return (
        "JOIN agent_runs r ON r.tenant_id=t.tenant_id AND r.run_id=t.run_id "
        "LEFT JOIN agent_turns n ON n.tenant_id=t.tenant_id AND n.run_id=t.run_id "
        "AND n.sequence=(SELECT MIN(x.sequence) FROM agent_turns x "
        "WHERE x.tenant_id=t.tenant_id AND x.run_id=t.run_id AND x.sequence>t.sequence) "
        "LEFT JOIN agent_turns m ON n.turn_id IS NULL AND r.parent_run_id IS NULL "
        "AND r.session_id IS NOT NULL AND m.tenant_id=t.tenant_id "
        "AND m.run_id=(SELECT nr.run_id FROM agent_runs nr "
        "WHERE nr.tenant_id=r.tenant_id AND nr.session_id=r.session_id "
        "AND nr.parent_run_id IS NULL "
        f"AND (nr.started_at,nr.{run_id})>(r.started_at,r.{run_id}) "
        "AND EXISTS (SELECT 1 FROM agent_turns y "
        "WHERE y.tenant_id=nr.tenant_id AND y.run_id=nr.run_id) "
        f"ORDER BY nr.started_at,nr.{run_id} LIMIT 1) "
        "AND m.sequence=(SELECT MIN(z.sequence) FROM agent_turns z "
        "WHERE z.tenant_id=m.tenant_id AND z.run_id=m.run_id)"
    )


def _next_message(text, state, truncated) -> NextUserMessage:
    if state is None:
        return NextUserMessage()
    return NextUserMessage(
        text=text, state=EvidenceState(state), truncated=bool(truncated), has_next=True,
    )


def read_next_user_messages_batch(
    cursor, *, postgres: bool, tenant_id: str,
    turn_keys: list[tuple[str, str]],
    session: bool = False,
) -> dict[tuple[str, str], NextUserMessage]:
    """One statement returning the user's next request for each Turn."""
    if not turn_keys:
        return {}
    if len(turn_keys) > 100 or len(set(turn_keys)) != len(turn_keys):
        raise ValueError("invalid bounded Turn evidence page")
    placeholder = "%s" if postgres and not session else "?"
    wanted = ",".join(f"({placeholder},{placeholder})" for _ in turn_keys)
    columns = ("run_id", "turn_id", "parent_run_id", "n_id", "n_text", "n_state",
               "n_truncated", "m_text", "m_state", "m_truncated")
    result = cursor.execute(
        f"WITH wanted(run_id,turn_id) AS (VALUES {wanted}) "
        "SELECT w.run_id,w.turn_id,r.parent_run_id,n.turn_id AS n_id,"
        "n.user_request_redacted AS n_text,n.request_state AS n_state,"
        "n.request_truncated AS n_truncated,m.user_request_redacted AS m_text,"
        "m.request_state AS m_state,m.request_truncated AS m_truncated "
        f"FROM wanted w JOIN agent_turns t ON t.tenant_id={placeholder} "
        "AND t.run_id=w.run_id AND t.turn_id=w.turn_id " + _next_turn_joins(postgres),
        (*(value for key in turn_keys for value in key), tenant_id),
    )
    messages = {key: NextUserMessage() for key in turn_keys}
    for row in result:
        values = dict(zip(columns, (
            tuple(row[key] for key in columns) if hasattr(row, "keys") else row
        ), strict=True))
        key = (values["run_id"], values["turn_id"])
        if values["parent_run_id"] is not None:
            messages[key] = NextUserMessage(sub_agent=True)
        elif values["n_id"] is not None:
            messages[key] = _next_message(
                values["n_text"], values["n_state"], values["n_truncated"],
            )
        else:
            messages[key] = _next_message(
                values["m_text"], values["m_state"], values["m_truncated"],
            )
    return messages


def read_turn_evidence(cursor, mode: str | None, *, postgres: bool, tenant_id: str,
                       run_id: str, turn_id: str) -> TurnEvidence | None:
    if mode is None:
        return None
    if mode == TOOL_EVIDENCE_MODE:
        return read_turn_tool_counts(
            cursor, postgres=postgres, tenant_id=tenant_id, run_id=run_id, turn_id=turn_id,
        )
    if mode == NEXT_MESSAGE_MODE:
        return read_next_user_messages_batch(
            cursor, postgres=postgres, tenant_id=tenant_id, turn_keys=[(run_id, turn_id)],
        )[(run_id, turn_id)]
    raise ValueError("unsupported Turn evidence mode")


def read_turn_evidence_batch(
    cursor, mode: str | None, *, postgres: bool, tenant_id: str,
    turn_keys: list[tuple[str, str]], session: bool = False,
) -> dict[tuple[str, str], TurnEvidence]:
    if mode is None:
        return {}
    if mode == TOOL_EVIDENCE_MODE:
        return read_turn_tool_counts_batch(
            cursor, postgres=postgres, tenant_id=tenant_id, turn_keys=turn_keys,
            session=session,
        )
    if mode == NEXT_MESSAGE_MODE:
        return read_next_user_messages_batch(
            cursor, postgres=postgres, tenant_id=tenant_id, turn_keys=turn_keys,
            session=session,
        )
    raise ValueError("unsupported Turn evidence mode")
