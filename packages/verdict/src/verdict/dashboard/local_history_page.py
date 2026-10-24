"""One bounded proof of complete recorded local-history positions."""

from __future__ import annotations

import json

from verdict.dashboard.query import QuerySession
from verdict.evidence import AgentRunBundle
from verdict.normalized_evidence import agent_event_from_row, agent_turn_from_row

MAX_FULL_LOCAL_EVENTS = 5_000


def verified_local_positions(
    session: QuerySession, tenant: str, run_id: str,
) -> tuple[str, list[tuple[int, str, str, int, int, bool]]]:
    """Return source revision and ordered positions only when all facts resolve."""
    if not session.table_exists("local_tail_turn_facts"):
        raise ValueError("partial_projection")
    source = session.execute(
        "SELECT s.local_full_sha256,s.local_source_turn_count,"
        "s.local_source_event_count,s.local_v1_turn_count,s.local_v1_event_count,"
        "s.local_partial_line,s.local_projection_complete FROM agent_runs r "
        "JOIN import_sources s ON s.tenant_id=r.tenant_id "
        "AND s.source_session_id=r.source_session_id "
        "WHERE r.tenant_id=? AND r.run_id=? AND s.source_kind IN ('codex','claude-code')",
        (tenant, run_id),
    ).fetchone()
    if source is None:
        raise KeyError(run_id)
    if (not source["local_projection_complete"] or source["local_partial_line"]
            or not source["local_full_sha256"]):
        raise ValueError("partial_projection")
    event_count = source["local_source_event_count"]
    turn_count = source["local_source_turn_count"]
    if (type(event_count) is not int or not 0 <= event_count <= MAX_FULL_LOCAL_EVENTS
            or type(turn_count) is not int or not 1 <= turn_count <= MAX_FULL_LOCAL_EVENTS):
        raise RuntimeError("invalid local history coverage")
    turns = list(session.execute(
        "SELECT sequence,turn_id,started_at,0 AS tail,NULL AS payload_json FROM agent_turns "
        "WHERE tenant_id=? AND run_id=? "
        "UNION ALL SELECT sequence,turn_id,started_at,1 AS tail,payload_json "
        "FROM local_tail_turn_facts "
        "WHERE tenant_id=? AND run_id=? ORDER BY sequence",
        (tenant, run_id, tenant, run_id),
    ))
    if (len(turns) != turn_count
            or sum(not turn["tail"] for turn in turns) != source["local_v1_turn_count"]
            or [turn["sequence"] for turn in turns] != list(range(turn_count))
            or len({turn["turn_id"] for turn in turns}) != turn_count):
        raise RuntimeError("invalid local history turns")
    for turn in turns:
        if turn["tail"]:
            try:
                payload = json.loads(turn["payload_json"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("invalid local tail turn") from exc
            if (not isinstance(payload, dict) or payload.get("run_id") != run_id
                    or payload.get("turn_id") != turn["turn_id"]
                    or payload.get("sequence") != turn["sequence"]
                    or payload.get("started_at") != str(turn["started_at"])):
                raise RuntimeError("invalid local tail turn")
    owners = {turn["sequence"]: turn["turn_id"] for turn in turns}
    prefix = list(session.execute(
        "SELECT e.event_id,e.turn_id,t.sequence AS turn_sequence,e.sequence "
        "FROM agent_events e JOIN agent_turns t ON t.tenant_id=e.tenant_id "
        "AND t.run_id=e.run_id AND t.turn_id=e.turn_id "
        "WHERE e.tenant_id=? AND e.run_id=? AND e.provenance NOT IN "
        "('verdict:capture_limit','verdict:partial_source_line') "
        "ORDER BY t.sequence,e.sequence,e.event_id",
        (tenant, run_id),
    ))
    facts = list(session.execute(
        "SELECT source_ordinal,event_id,turn_id,turn_sequence,sequence "
        "FROM local_recent_event_facts WHERE tenant_id=? AND run_id=? "
        "ORDER BY source_ordinal",
        (tenant, run_id),
    ))
    positions = [
        (ordinal, row["event_id"], row["turn_id"], row["turn_sequence"],
         row["sequence"], False)
        for ordinal, row in enumerate(prefix)
    ] + [
        (row["source_ordinal"], row["event_id"], row["turn_id"],
         row["turn_sequence"], row["sequence"], True)
        for row in facts
    ]
    positions.sort(key=lambda row: row[0])
    if (len(prefix) != source["local_v1_event_count"]
            or len(positions) != event_count
            or [row[0] for row in positions] != list(range(event_count))
            or len({row[1] for row in positions}) != event_count
            or any(owners.get(row[3]) != row[2] for row in positions)):
        raise RuntimeError("invalid local history events")
    return source["local_full_sha256"], positions


def complete_local_bundle(
    session: QuerySession, tenant: str, prefix: AgentRunBundle,
    positions: list[tuple[int, str, str, int, int, bool]],
) -> AgentRunBundle:
    """Reconstruct one admitted local run after the recorded-position proof."""
    run_id = prefix.run.run_id
    try:
        tail_turns = tuple(agent_turn_from_row(json.loads(row["payload_json"]))
                           for row in session.execute(
                               "SELECT payload_json FROM local_tail_turn_facts "
                               "WHERE tenant_id=? AND run_id=? ORDER BY sequence",
                               (tenant, run_id),
                           ))
        retained = tuple(agent_event_from_row(row) for row in session.execute(
            "SELECT * FROM local_recent_event_facts "
            "WHERE tenant_id=? AND run_id=? ORDER BY source_ordinal",
            (tenant, run_id),
        ))
        events = tuple(event for event in prefix.events if event.provenance not in {
            "verdict:capture_limit", "verdict:partial_source_line",
        }) + retained
        by_id = {event.event_id: event for event in events}
        if (len(events) != len(positions) or len(by_id) != len(positions)
                or len(prefix.turns) + len(tail_turns) > MAX_FULL_LOCAL_EVENTS):
            raise RuntimeError("invalid local full run")
        return AgentRunBundle(
            prefix.session, prefix.run, prefix.turns + tail_turns,
            tuple(by_id[position[1]] for position in positions),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid local full run") from exc
