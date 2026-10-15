"""Bounded normalized Agent Run queries shared by dashboard views."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from verdict.dashboard.query import QuerySession
from verdict.evidence import AgentRunBundle
from verdict.normalized_evidence import bundle_from_normalized_rows

_TABLES = ("import_sources", "agent_runs", "agent_turns", "agent_events")


def session_run_ids(
    session: QuerySession, tenant_id: str, source_kind: str, session_id: str,
    *, limit: int,
) -> tuple[tuple[str, ...], int]:
    """Resolve the exact source-qualified session, including runs with no turns."""
    if not available(session):
        return (), 0
    rows = list(session.execute(
        """SELECT r.run_id FROM agent_runs r JOIN import_sources s
           ON s.tenant_id=r.tenant_id AND s.source_session_id=r.source_session_id
           WHERE r.tenant_id=? AND s.source_kind=?
             AND COALESCE(NULLIF(r.session_id,''),r.source_session_id)=?
           ORDER BY r.started_at,r.run_id LIMIT ?""",
        (tenant_id, source_kind, session_id, limit + 1),
    ))
    return tuple(str(row["run_id"]) for row in rows[:limit]), len(rows)


def available(session: QuerySession) -> bool:
    return all(session.table_exists(table) for table in _TABLES)


def count_runs(session: QuerySession, tenant_id: str) -> int:
    if not available(session):
        return 0
    row = session.execute(
        "SELECT COUNT(*) AS count FROM agent_runs WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    return int(row["count"] if row else 0)


def list_run_rows(
    session: QuerySession, tenant_id: str, *, limit: int, offset: int = 0,
    run_ids: tuple[str, ...] | None = None,
) -> list[dict]:
    """Read page metadata and child counts without loading event content."""
    where = "r.tenant_id=?"
    params: tuple = (tenant_id,)
    if run_ids is not None:
        placeholders = ",".join("?" for _ in run_ids)
        where += f" AND r.run_id IN ({placeholders})"  # nosec B608
        params += run_ids
    else:
        params += (limit, offset)
    pagination = "" if run_ids is not None else " LIMIT ? OFFSET ?"
    rows = session.execute(
        """SELECT r.run_id,r.started_at,r.status,r.agent_name,s.source_kind,
                  (SELECT COUNT(*) FROM agent_turns t
                   WHERE t.tenant_id=r.tenant_id AND t.run_id=r.run_id) AS turn_count,
                  (SELECT COUNT(*) FROM agent_events e
                   WHERE e.tenant_id=r.tenant_id AND e.run_id=r.run_id) AS event_count
           FROM agent_runs r JOIN import_sources s
             ON s.tenant_id=r.tenant_id AND s.source_session_id=r.source_session_id
           WHERE """ + where + " ORDER BY r.started_at DESC,r.run_id DESC" + pagination,  # nosec B608
        params,
    )
    return [
        {
            "runId": str(row["run_id"]), "sourceKind": row["source_kind"],
            "startedAt": row["started_at"], "status": row["status"],
            "agentName": row["agent_name"] or None,
            "turnCount": int(row["turn_count"]), "eventCount": int(row["event_count"]),
        }
        for row in rows
    ]


def load_bundles(
    session: QuerySession,
    tenant_id: str,
    *,
    limit: int,
    run_ids: tuple[str, ...] | None = None,
    oldest_first: bool = False,
    offset: int = 0,
) -> list[AgentRunBundle]:
    """Load a bounded run slice in four queries, independent of run count."""
    if not available(session):
        return []
    direction = "ASC" if oldest_first else "DESC"
    if run_ids is None:
        run_rows = list(
            session.execute(
                """SELECT r.*,
                      s.source_kind,
                      s.source_locator_hash,
                      s.started_at AS source_started_at,
                      s.observed_at AS source_observed_at,
                      s.ended_at AS source_ended_at
               FROM agent_runs r
               JOIN import_sources s
                 ON s.tenant_id=r.tenant_id
                AND s.source_session_id=r.source_session_id
               WHERE r.tenant_id=?
               ORDER BY r.started_at """
                + direction
                + ",r.run_id "
                + direction
                + " LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            )
        )
    else:
        placeholders = ",".join("?" for _ in run_ids)
        run_rows = list(
            session.execute(
                """SELECT r.*,
                      s.source_kind,
                      s.source_locator_hash,
                      s.started_at AS source_started_at,
                      s.observed_at AS source_observed_at,
                      s.ended_at AS source_ended_at
               FROM agent_runs r
               JOIN import_sources s
                 ON s.tenant_id=r.tenant_id
                AND s.source_session_id=r.source_session_id
               WHERE r.tenant_id=? AND r.run_id IN ("""
                + placeholders
                + ") ORDER BY r.started_at "
                + direction
                + ",r.run_id "
                + direction,  # nosec B608 -- placeholders are generated, never user text
                (tenant_id, *run_ids),
            )
        )
    selected = tuple(str(row["run_id"]) for row in run_rows)
    if not selected:
        return []
    placeholders = ",".join("?" for _ in selected)
    child_params = (tenant_id, *selected)
    turn_rows = list(
        session.execute(
            "SELECT * FROM agent_turns WHERE tenant_id=? AND run_id IN ("
            + placeholders
            + ") ORDER BY run_id,sequence,turn_id",  # nosec B608
            child_params,
        )
    )
    event_rows = list(
        session.execute(
            "SELECT * FROM agent_events WHERE tenant_id=? AND run_id IN ("
            + placeholders
            + ") ORDER BY run_id,turn_id,sequence,event_id",  # nosec B608
            child_params,
        )
    )
    turns_by_run: dict[str, list[Mapping[str, object]]] = {}
    for row in turn_rows:
        turns_by_run.setdefault(str(row["run_id"]), []).append(row)
    events_by_run: dict[str, list[Mapping[str, object]]] = {}
    for row in event_rows:
        events_by_run.setdefault(str(row["run_id"]), []).append(row)
    bundles = []
    for row in run_rows:
        run_id = str(row["run_id"])
        source = {
            "tenant_id": row["tenant_id"],
            "source_session_id": row["source_session_id"],
            "source_kind": row["source_kind"],
            "source_locator_hash": row["source_locator_hash"],
            "started_at": row["source_started_at"],
            "observed_at": row["source_observed_at"],
            "ended_at": row["source_ended_at"],
        }
        bundles.append(
            bundle_from_normalized_rows(
                source,
                row,
                turns_by_run.get(run_id, ()),
                events_by_run.get(run_id, ()),
            )
        )
    return bundles


def iter_bundles(
    session: QuerySession,
    tenant_id: str,
    *,
    limit: int,
    page_size: int = 100,
) -> Iterable[AgentRunBundle]:
    """Yield a large scan without materializing every selected run at once."""
    for offset in range(0, limit, page_size):
        page = load_bundles(
            session,
            tenant_id,
            limit=min(page_size, limit - offset),
            oldest_first=True,
            offset=offset,
        )
        yield from page
        if len(page) < page_size:
            return


def load_run_page(
    session: QuerySession,
    tenant_id: str,
    run_id: str,
    *,
    event_limit: int,
    event_offset: int,
    turn_limit: int,
    turn_offset: int,
    event_id: str | None,
) -> dict[str, object] | None:
    """Read one run and bounded child pages without constructing a whole bundle."""
    if not available(session):
        return None
    run = session.execute(
        """SELECT r.*,s.source_kind
           FROM agent_runs r JOIN import_sources s
             ON s.tenant_id=r.tenant_id
            AND s.source_session_id=r.source_session_id
           WHERE r.tenant_id=? AND r.run_id=?""",
        (tenant_id, run_id),
    ).fetchone()
    if run is None:
        return None
    event_count = session.execute(
        "SELECT COUNT(*) AS count FROM agent_events WHERE tenant_id=? AND run_id=?",
        (tenant_id, run_id),
    ).fetchone()
    turn_count = session.execute(
        "SELECT COUNT(*) AS count FROM agent_turns WHERE tenant_id=? AND run_id=?",
        (tenant_id, run_id),
    ).fetchone()
    producer_count = session.execute(
        "SELECT COUNT(DISTINCT producer_id) AS count FROM agent_events "
        "WHERE tenant_id=? AND run_id=? AND producer_id<>''",
        (tenant_id, run_id),
    ).fetchone()
    resolved_offset = event_offset
    order = "occurred_at,producer_id,COALESCE(producer_sequence,sequence),event_id"
    if event_id is not None:
        focused = session.execute(
            "SELECT ordinal FROM (SELECT event_id,ROW_NUMBER() OVER (ORDER BY "
            + order
            + ")-1 AS ordinal FROM agent_events WHERE tenant_id=? AND run_id=?) page "
            "WHERE event_id=?",  # nosec B608 -- order is a fixed schema expression
            (tenant_id, run_id, event_id),
        ).fetchone()
        if focused is None:
            raise KeyError(event_id)
        resolved_offset = (int(focused["ordinal"]) // event_limit) * event_limit
    events = list(session.execute(
        "SELECT * FROM agent_events WHERE tenant_id=? AND run_id=? ORDER BY "
        + order
        + " LIMIT ? OFFSET ?",  # nosec B608 -- order is a fixed schema expression
        (tenant_id, run_id, event_limit, resolved_offset),
    ))
    turns = list(session.execute(
        """SELECT * FROM agent_turns WHERE tenant_id=? AND run_id=?
           ORDER BY sequence,turn_id LIMIT ? OFFSET ?""",
        (tenant_id, run_id, turn_limit, turn_offset),
    ))
    return {
        "run": run,
        "events": events,
        "turns": turns,
        "eventCount": int(event_count["count"] if event_count else 0),
        "turnCount": int(turn_count["count"] if turn_count else 0),
        "producerCount": int(producer_count["count"] if producer_count else 0),
        "eventOffset": resolved_offset,
    }


def source_metadata(session: QuerySession, tenant_id: str) -> dict[str, object]:
    if not available(session):
        return {
            "available": 0,
            "sources": [],
            "sourcesTruncated": False,
            "lastCapturedAt": None,
        }
    available_runs = count_runs(session, tenant_id)
    rows = session.execute(
        """SELECT s.source_kind,COUNT(*) AS count
           FROM agent_runs r JOIN import_sources s
             ON s.tenant_id=r.tenant_id
            AND s.source_session_id=r.source_session_id
           WHERE r.tenant_id=?
           GROUP BY s.source_kind ORDER BY s.source_kind LIMIT 16""",
        (tenant_id,),
    )
    sources = [{"sourceKind": row["source_kind"], "runs": int(row["count"])} for row in rows]
    source_count = session.execute(
        """SELECT COUNT(DISTINCT s.source_kind) AS count
           FROM agent_runs r JOIN import_sources s
             ON s.tenant_id=r.tenant_id
            AND s.source_session_id=r.source_session_id
           WHERE r.tenant_id=?""",
        (tenant_id,),
    ).fetchone()
    latest = session.execute(
        "SELECT MAX(started_at) AS newest_started_at FROM agent_runs WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    newest = latest["newest_started_at"] if latest else None
    if hasattr(newest, "isoformat"):
        newest = newest.isoformat()
    return {
        "available": available_runs,
        "sources": sources,
        "sourcesTruncated": int(source_count["count"] if source_count else 0) > len(sources),
        "lastCapturedAt": newest,
    }
