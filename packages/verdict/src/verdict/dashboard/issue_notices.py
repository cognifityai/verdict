"""Durable, content-free notices for explicit Agent Event and Run failures."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from verdict.analysis import recorded_event_issue
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.storage_url import is_postgres_storage
from verdict.storage.sqlite import sqlite_path

CHECK_VERSION = 1
EVENT_TYPES = ("tool_result", "command", "test_result", "outcome")
MAX_RECHECKS_PER_NOTICE = 8
MAX_CANDIDATE_SCAN_ROWS = 200
MAX_RECHECK_CANDIDATES = 10
MAX_CANDIDATE_ATTRIBUTE_BYTES = 16_384
EVENT_EVIDENCE_SQL = (
    "(SELECT tenant_id,run_id,event_id,occurred_at,event_type,status,attributes_json "
    "FROM agent_events UNION ALL "
    "SELECT recent.tenant_id,recent.run_id,recent.event_id,recent.occurred_at,"
    "recent.event_type,recent.status,recent.attributes_json "
    "FROM local_recent_event_facts recent WHERE NOT EXISTS ("
    "SELECT 1 FROM agent_events canonical WHERE canonical.tenant_id=recent.tenant_id "
    "AND canonical.run_id=recent.run_id AND canonical.event_id=recent.event_id))"
)


def _attributes(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _positive_recheck(event_type: str, status: str, attributes: dict) -> bool:
    """Accept only an explicit, noncontradictory positive result."""
    if status != "completed" or recorded_event_issue(event_type, status, attributes):
        return False
    if event_type == "tool_result":
        return attributes.get("is_error") is False
    if event_type == "command":
        return type(attributes.get("exit_code")) is int and attributes["exit_code"] == 0
    if event_type == "test_result":
        return (type(attributes.get("exit_code")) is int
                and attributes["exit_code"] == 0
                and type(attributes.get("failed")) is int
                and attributes["failed"] == 0)
    if event_type == "outcome":
        return attributes.get("value") is True
    return False


def _same_recorded_check(event_type: str, failed: dict, passed: dict) -> bool:
    if event_type not in {"command", "test_result"}:
        return False
    first = failed.get("check_id_hash")
    second = passed.get("check_id_hash")
    return (isinstance(first, str) and len(first) == 64
            and all(character in "0123456789abcdef" for character in first)
            and first == second)


def _instant(value: object) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        raise ValueError("event time is unavailable")
    return moment.astimezone(timezone.utc)


class IssueNotices:
    """Project typed failures after schedule activation; never infer recovery."""

    def __init__(self, storage_url: str) -> None:
        self.storage_url = storage_url
        self.postgres = is_postgres_storage(storage_url)

    def _connect(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(self.storage_url, row_factory=dict_row)
        connection = sqlite3.connect(sqlite_path(self.storage_url))
        connection.row_factory = sqlite3.Row
        return connection

    def _prepare(self, connection) -> None:
        if self.postgres:
            # Two first-time workers may create these additive tables together;
            # PostgreSQL's CREATE IF NOT EXISTS does not serialize that race.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                               ("verdict-agent-issue-notice-schema-v1",))
        time_type = "TIMESTAMPTZ" if self.postgres else "TEXT"
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS agent_issue_notices (
                tenant_id TEXT NOT NULL,
                notice_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                check_code TEXT NOT NULL,
                check_version INTEGER NOT NULL,
                event_at {time_type} NOT NULL,
                created_at {time_type} NOT NULL,
                updated_at {time_type} NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('open','acknowledged','dismissed')),
                PRIMARY KEY (tenant_id,notice_id),
                UNIQUE (tenant_id,run_id,event_id,check_code,check_version)
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_issue_notices_tenant_created
            ON agent_issue_notices(tenant_id,created_at DESC,notice_id DESC)
        """)
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS agent_run_issue_notices (
                tenant_id TEXT NOT NULL,
                notice_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                check_code TEXT NOT NULL,
                check_version INTEGER NOT NULL,
                event_at {time_type} NOT NULL,
                created_at {time_type} NOT NULL,
                updated_at {time_type} NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('open','acknowledged','dismissed')),
                PRIMARY KEY (tenant_id,notice_id),
                UNIQUE (tenant_id,run_id,check_code,check_version)
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_run_issue_notices_tenant_created
            ON agent_run_issue_notices(tenant_id,created_at DESC,notice_id DESC)
        """)
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS agent_issue_rechecks (
                tenant_id TEXT NOT NULL,
                notice_id TEXT NOT NULL,
                recheck_run_id TEXT NOT NULL,
                recheck_event_id TEXT NOT NULL,
                event_at {time_type} NOT NULL,
                created_at {time_type} NOT NULL,
                PRIMARY KEY (tenant_id,notice_id,recheck_run_id,recheck_event_id)
            )
        """)

    @staticmethod
    def _row(row) -> dict[str, object]:
        def time(value):
            return value.isoformat() if isinstance(value, datetime) else value

        return {
            "noticeId": row["notice_id"], "runId": row["run_id"],
            "eventId": row["event_id"], "checkCode": row["check_code"],
            "evidenceKind": row["evidence_kind"],
            "eventAt": time(row["event_at"]), "createdAt": time(row["created_at"]),
            "updatedAt": time(row["updated_at"]), "state": row["state"],
        }

    def _activation_at(self, connection, tenant: str):
        placeholder = "%s" if self.postgres else "?"
        row = connection.execute(
            "SELECT MIN(created_at) AS activated_at FROM product_control_documents "
            f"WHERE tenant_id={placeholder} AND kind='schedule' "
            "AND document_id='daily' AND state='active'", (tenant,),
        ).fetchone()
        return row["activated_at"] if row else None

    def _add_recent_references(self, connection, tenant: str,
                               notices: list[dict[str, object]]) -> None:
        targets = [item for notice in notices
                   for item in (notice, *notice.get("rechecks", []))
                   if item.get("eventId") is not None]
        ids = sorted({str(item["eventId"]) for item in targets})
        if not ids:
            return
        slot = "%s" if self.postgres else "?"
        rows = connection.execute(
            "SELECT recent.run_id,recent.event_id,recent.turn_id,"
            "recent.first_seen_revision FROM local_recent_event_facts recent "
            f"WHERE recent.tenant_id={slot} "
            f"AND recent.event_id IN ({','.join([slot] * len(ids))}) "
            "AND NOT EXISTS (SELECT 1 FROM agent_events canonical "
            "WHERE canonical.tenant_id=recent.tenant_id "
            "AND canonical.run_id=recent.run_id "
            "AND canonical.event_id=recent.event_id)",
            (tenant, *ids),
        ).fetchall()
        by_event = {(row["run_id"], row["event_id"]): row for row in rows}
        for item in targets:
            row = by_event.get((item["runId"], item["eventId"]))
            if row is not None:
                item["recentRevision"] = row["first_seen_revision"]
                item["recentTurnId"] = row["turn_id"]

    def scan(self, tenant: str, *, page_size: int = 200) -> int:
        """Idempotently scan every post-activation event, including late imports."""
        if not 1 <= page_size <= 1000:
            raise ValueError("issue scan page size must be 1-1000")
        # Ensures the additive control table exists for an empty/legacy store.
        current = ControlStore(self.storage_url).list_current(tenant)
        if not any(item["kind"] == "schedule" and item["documentId"] == "daily"
                   and item["state"] == "active" for item in current):
            return 0
        placeholder = "%s" if self.postgres else "?"
        added = 0
        cursor = None
        with closing(self._connect()) as connection:
            with connection:
                self._prepare(connection)
                activated_at = self._activation_at(connection, tenant)
                if activated_at is None:
                    return 0
                while True:
                    after = ""
                    params: list[object] = [tenant, activated_at, *EVENT_TYPES]
                    if cursor is not None:
                        after = (f" AND (occurred_at,run_id,event_id) > "
                                 f"({placeholder},{placeholder},{placeholder})")
                        params.extend(cursor)
                    params.append(page_size)
                    rows = connection.execute(
                        "SELECT run_id,event_id,occurred_at,event_type,status,attributes_json "
                        f"FROM {EVENT_EVIDENCE_SQL} evidence WHERE tenant_id={placeholder} "
                        f"AND occurred_at>={placeholder} "
                        f"AND event_type IN ({','.join([placeholder] * len(EVENT_TYPES))})"
                        + after + " ORDER BY occurred_at,run_id,event_id "
                        f"LIMIT {placeholder}", tuple(params),
                    ).fetchall()
                    for row in rows:
                        attributes = _attributes(row["attributes_json"])
                        code = recorded_event_issue(row["event_type"], row["status"], attributes)
                        if code is None:
                            continue
                        identity = json.dumps(
                            [tenant, row["run_id"], row["event_id"], code, CHECK_VERSION],
                            separators=(",", ":"), ensure_ascii=False,
                        )
                        notice_id = hashlib.sha256(identity.encode()).hexdigest()
                        now = datetime.now(timezone.utc)
                        timestamp = now if self.postgres else now.isoformat()
                        inserted = connection.execute(
                            "INSERT INTO agent_issue_notices "
                            "(tenant_id,notice_id,run_id,event_id,check_code,check_version,"
                            "event_at,created_at,updated_at,state) VALUES ("
                            + ",".join([placeholder] * 10) + ") "
                            "ON CONFLICT (tenant_id,notice_id) DO NOTHING",
                            (tenant, notice_id, row["run_id"], row["event_id"], code,
                             CHECK_VERSION, row["occurred_at"], timestamp, timestamp, "open"),
                        )
                        added += inserted.rowcount
                    # Release write locks between pages. A newly arrived event
                    # behind this cursor is picked up by the next full scan.
                    connection.commit()
                    if len(rows) < page_size:
                        break
                    last = rows[-1]
                    cursor = (last["occurred_at"], last["run_id"], last["event_id"])
                cursor = None
                while True:
                    after = ""
                    params = [tenant, activated_at]
                    if cursor is not None:
                        after = (f" AND (ended_at,run_id) > "
                                 f"({placeholder},{placeholder})")
                        params.extend(cursor)
                    params.append(page_size)
                    runs = connection.execute(
                        "SELECT run_id,ended_at,status FROM agent_runs "
                        f"WHERE tenant_id={placeholder} AND ended_at>={placeholder} "
                        "AND status IN ('failed','timed_out') AND ended_at IS NOT NULL"
                        + after + " ORDER BY ended_at,run_id "
                        f"LIMIT {placeholder}", tuple(params),
                    ).fetchall()
                    for run in runs:
                        code = "run_failed" if run["status"] == "failed" else "run_timed_out"
                        identity = json.dumps(
                            [tenant, "run", run["run_id"], code, CHECK_VERSION],
                            separators=(",", ":"), ensure_ascii=False,
                        )
                        notice_id = hashlib.sha256(identity.encode()).hexdigest()
                        now = datetime.now(timezone.utc)
                        timestamp = now if self.postgres else now.isoformat()
                        inserted = connection.execute(
                            "INSERT INTO agent_run_issue_notices "
                            "(tenant_id,notice_id,run_id,check_code,check_version,"
                            "event_at,created_at,updated_at,state) VALUES ("
                            + ",".join([placeholder] * 9) + ") "
                            "ON CONFLICT (tenant_id,notice_id) DO NOTHING",
                            (tenant, notice_id, run["run_id"], code, CHECK_VERSION,
                             run["ended_at"], timestamp, timestamp, "open"),
                        )
                        added += inserted.rowcount
                    connection.commit()
                    if len(runs) < page_size:
                        break
                    last = runs[-1]
                    cursor = (last["ended_at"], last["run_id"])
        return added

    def list(self, tenant: str, *, limit: int = 50, offset: int = 0,
             state: str | None = None,
             notice_id: str | None = None) -> list[dict[str, object]]:
        if not 1 <= limit <= 100 or offset < 0 or state not in {None, "open", "reviewed"}:
            raise ValueError("invalid issue page")
        if notice_id is not None and (not isinstance(notice_id, str)
                                      or not 1 <= len(notice_id.encode("utf-8")) <= 256
                                      or "\x00" in notice_id):
            raise ValueError("invalid notice identity")
        placeholder = "%s" if self.postgres else "?"
        state_clause = (" AND state='open'" if state == "open" else
                        " AND state<>'open'" if state == "reviewed" else "")
        notice_clause = f" AND notice_id={placeholder}" if notice_id is not None else ""
        scope_params = (tenant, notice_id) if notice_id is not None else (tenant,)
        with closing(self._connect()) as connection:
            with connection:
                self._prepare(connection)
                rows = connection.execute(
                    "SELECT * FROM ("
                    "SELECT notice_id,run_id,event_id,check_code,event_at,"
                    "created_at,updated_at,state,'event' AS evidence_kind "
                    "FROM agent_issue_notices "
                    f"WHERE tenant_id={placeholder}" + notice_clause + state_clause + " UNION ALL "
                    "SELECT notice_id,run_id,NULL AS event_id,check_code,event_at,"
                    "created_at,updated_at,state,'run' AS evidence_kind "
                    "FROM agent_run_issue_notices "
                    f"WHERE tenant_id={placeholder}" + notice_clause + state_clause + ") notices "
                    "ORDER BY created_at DESC,notice_id DESC "
                    f"LIMIT {placeholder} OFFSET {placeholder}",
                    (*scope_params, *scope_params, limit, offset),
                ).fetchall()
                notices = [self._row(row) for row in rows]
                if not notices:
                    return notices
                ids = [item["noticeId"] for item in notices
                       if item["evidenceKind"] == "event"]
                for item in notices:
                    item["rechecks"] = []
                if not ids:
                    self._add_recent_references(connection, tenant, notices)
                    return notices
                slots = ",".join([placeholder] * len(ids))
                rechecks = connection.execute(
                    "SELECT a.notice_id,a.recheck_run_id,a.recheck_event_id,"
                    "a.event_at,a.created_at,n.run_id AS failed_run_id,"
                    "n.event_id AS failed_event_id,n.check_code,"
                    "f.occurred_at AS failed_at,f.event_type AS failed_type,"
                    "f.status AS failed_status,f.attributes_json AS failed_attributes,"
                    "r.occurred_at AS recorded_at,r.event_type AS recheck_type,"
                    "r.status AS recheck_status,r.attributes_json AS recheck_attributes "
                    "FROM agent_issue_rechecks a "
                    "JOIN agent_issue_notices n ON n.tenant_id=a.tenant_id "
                    "AND n.notice_id=a.notice_id "
                    f"LEFT JOIN {EVENT_EVIDENCE_SQL} f ON f.tenant_id=n.tenant_id "
                    "AND f.run_id=n.run_id AND f.event_id=n.event_id "
                    f"LEFT JOIN {EVENT_EVIDENCE_SQL} r ON r.tenant_id=a.tenant_id "
                    "AND r.run_id=a.recheck_run_id AND r.event_id=a.recheck_event_id "
                    f"WHERE a.tenant_id={placeholder} AND a.notice_id IN ({slots}) "
                    "ORDER BY a.created_at DESC,a.recheck_event_id DESC",
                    (tenant, *ids),
                ).fetchall()
                by_id = {item["noticeId"]: item for item in notices}
                for row in rechecks:
                    if row["failed_at"] is None or row["recorded_at"] is None:
                        continue
                    if (row["failed_type"] != row["recheck_type"]
                            or recorded_event_issue(row["failed_type"],
                                                       row["failed_status"],
                                                       _attributes(row["failed_attributes"]))
                            != row["check_code"]
                            or not _positive_recheck(row["recheck_type"],
                                                     row["recheck_status"],
                                                     _attributes(row["recheck_attributes"]))):
                        continue
                    try:
                        if _instant(row["recorded_at"]) <= _instant(row["failed_at"]):
                            continue
                    except ValueError:
                        continue
                    by_id[row["notice_id"]]["rechecks"].append({
                        "runId": row["recheck_run_id"],
                        "eventId": row["recheck_event_id"],
                        "eventAt": self._row_time(row["event_at"]),
                        "sameCheckId": _same_recorded_check(
                            row["failed_type"],
                            _attributes(row["failed_attributes"]),
                            _attributes(row["recheck_attributes"])),
                    })
                self._add_recent_references(connection, tenant, notices)
                return notices

    def get(self, tenant: str, notice_id: str) -> dict[str, object] | None:
        page = self.list(tenant, limit=1, notice_id=notice_id)
        return page[0] if page else None

    @staticmethod
    def _row_time(value: object) -> str:
        return value.isoformat() if isinstance(value, datetime) else str(value)

    def find_recheck_candidates(self, tenant: str, notice_id: str) -> dict[str, object]:
        """Search one bounded raw event page; a candidate is never a repair claim."""
        if (not isinstance(tenant, str) or not tenant or len(tenant.encode("utf-8")) > 256
                or not isinstance(notice_id, str) or len(notice_id) != 64
                or any(c not in "0123456789abcdef" for c in notice_id)):
            raise ValueError("candidate evidence unavailable")
        slot = "%s" if self.postgres else "?"
        attribute_bytes = ("octet_length(attributes_json::text)" if self.postgres
                           else "length(CAST(attributes_json AS BLOB))")
        bounded_attributes = (
            f"CASE WHEN {attribute_bytes}<={MAX_CANDIDATE_ATTRIBUTE_BYTES} "
            "THEN attributes_json ELSE NULL END AS attributes_json"
        )
        with closing(self._connect()) as connection:
            with connection:
                self._prepare(connection)
                notice = connection.execute(
                    "SELECT run_id,event_id,check_code FROM agent_issue_notices "
                    f"WHERE tenant_id={slot} AND notice_id={slot}",
                    (tenant, notice_id),
                ).fetchone()
                if notice is None:
                    raise ValueError("candidate evidence unavailable")
                failed = connection.execute(
                    f"SELECT occurred_at,event_type,status,{bounded_attributes} "
                    f"FROM {EVENT_EVIDENCE_SQL} evidence WHERE tenant_id={slot} AND run_id={slot} "
                    f"AND event_id={slot}",
                    (tenant, notice["run_id"], notice["event_id"]),
                ).fetchone()
                if failed is None:
                    raise ValueError("candidate evidence unavailable")
                if failed["attributes_json"] is None:
                    return {"searchable": False, "scanned": 0, "partial": True,
                            "candidates": []}
                failed_attributes = _attributes(failed["attributes_json"])
                event_type = failed["event_type"]
                searchable = (event_type in {"command", "test_result"}
                              and recorded_event_issue(event_type, failed["status"],
                                                       failed_attributes) == notice["check_code"]
                              and _same_recorded_check(event_type, failed_attributes,
                                                       failed_attributes))
                if not searchable:
                    return {"searchable": False, "scanned": 0, "partial": False,
                            "candidates": []}
                # The tenant/time index bounds *raw* rows before JSON parsing
                # or check-ID filtering. Further matches may remain outside this scan.
                rows = connection.execute(
                    f"SELECT run_id,event_id,occurred_at,event_type,status,{bounded_attributes} "
                    f"FROM {EVENT_EVIDENCE_SQL} evidence WHERE tenant_id={slot} AND occurred_at>{slot} "
                    "ORDER BY occurred_at,run_id,event_id "
                    f"LIMIT {slot}",
                    (tenant, failed["occurred_at"], MAX_CANDIDATE_SCAN_ROWS + 1),
                ).fetchall()
                scanned = rows[:MAX_CANDIDATE_SCAN_ROWS]
                candidates = []
                oversized = False
                for row in scanned:
                    if row["attributes_json"] is None:
                        oversized = True
                        continue
                    attributes = _attributes(row["attributes_json"])
                    if (row["event_type"] != event_type
                            or not _positive_recheck(event_type, row["status"],
                                                     attributes)
                            or not _same_recorded_check(event_type, failed_attributes,
                                                        attributes)):
                        continue
                    candidates.append({
                        "runId": row["run_id"], "eventId": row["event_id"],
                        "eventAt": self._row_time(row["occurred_at"]),
                    })
                candidate_limit_hit = len(candidates) > MAX_RECHECK_CANDIDATES
                candidates = candidates[:MAX_RECHECK_CANDIDATES]
                self._add_recent_references(connection, tenant, candidates)
                return {
                    "searchable": True,
                    "scanned": len(scanned),
                    "partial": oversized or len(rows) > MAX_CANDIDATE_SCAN_ROWS
                               or candidate_limit_hit,
                    "candidates": candidates,
                }

    def attach_recheck(self, tenant: str, notice_id: str, run_id: str,
                       event_id: str) -> dict[str, str]:
        """Attach a later recorded pass; never change the notice review state."""
        if (len(notice_id) != 64 or any(c not in "0123456789abcdef" for c in notice_id)
                or any(not isinstance(value, str) or not value
                       or len(value.encode("utf-8")) > 256
                       for value in (tenant, run_id, event_id))):
            raise ValueError("invalid recheck identity")
        slot = "%s" if self.postgres else "?"
        with closing(self._connect()) as connection:
            with connection:
                if not self.postgres:
                    # Serialize the per-notice bound with other SQLite writers.
                    connection.execute("BEGIN IMMEDIATE")
                self._prepare(connection)
                notice = connection.execute(
                    "SELECT * FROM agent_issue_notices "
                    f"WHERE tenant_id={slot} AND notice_id={slot}"
                    + (" FOR UPDATE" if self.postgres else ""),
                    (tenant, notice_id),
                ).fetchone()
                if notice is None:
                    raise ValueError("recheck evidence unavailable")
                def event(selected_run: str, selected_event: str):
                    return connection.execute(
                        "SELECT occurred_at,event_type,status,attributes_json "
                        f"FROM {EVENT_EVIDENCE_SQL} evidence WHERE tenant_id={slot} "
                        f"AND run_id={slot} AND event_id={slot}",
                        (tenant, selected_run, selected_event),
                    ).fetchone()

                failed = event(notice["run_id"], notice["event_id"])
                passed = event(run_id, event_id)
                if (failed is None or passed is None
                        or failed["event_type"] != passed["event_type"]
                        or recorded_event_issue(failed["event_type"], failed["status"],
                                                 _attributes(failed["attributes_json"]))
                        != notice["check_code"]
                        or not _positive_recheck(passed["event_type"], passed["status"],
                                                 _attributes(passed["attributes_json"]))
                        or _instant(passed["occurred_at"]) <= _instant(failed["occurred_at"])):
                    raise ValueError("recheck evidence unavailable")
                # A retained link is useful only while its exact evidence is
                # still present and positive. Reclaim invalid links so the
                # bounded attachment slots cannot become permanently stuck.
                attached = connection.execute(
                    "SELECT recheck_run_id,recheck_event_id FROM agent_issue_rechecks "
                    f"WHERE tenant_id={slot} AND notice_id={slot}",
                    (tenant, notice_id),
                ).fetchall()
                for prior in attached:
                    recorded = event(prior["recheck_run_id"], prior["recheck_event_id"])
                    valid = (recorded is not None
                             and recorded["event_type"] == failed["event_type"]
                             and _positive_recheck(recorded["event_type"],
                                                   recorded["status"],
                                                   _attributes(recorded["attributes_json"])))
                    if valid:
                        try:
                            valid = _instant(recorded["occurred_at"]) > _instant(
                                failed["occurred_at"])
                        except ValueError:
                            valid = False
                    if not valid:
                        connection.execute(
                            "DELETE FROM agent_issue_rechecks "
                            f"WHERE tenant_id={slot} AND notice_id={slot} "
                            f"AND recheck_run_id={slot} AND recheck_event_id={slot}",
                            (tenant, notice_id, prior["recheck_run_id"],
                             prior["recheck_event_id"]),
                        )
                existing = connection.execute(
                    "SELECT event_at FROM agent_issue_rechecks "
                    f"WHERE tenant_id={slot} AND notice_id={slot} "
                    f"AND recheck_run_id={slot} AND recheck_event_id={slot}",
                    (tenant, notice_id, run_id, event_id),
                ).fetchone()
                if existing is None:
                    count = connection.execute(
                        "SELECT COUNT(*) AS n FROM agent_issue_rechecks "
                        f"WHERE tenant_id={slot} AND notice_id={slot}",
                        (tenant, notice_id),
                    ).fetchone()["n"]
                    if count >= MAX_RECHECKS_PER_NOTICE:
                        raise ValueError("recheck evidence limit reached")
                    now = datetime.now(timezone.utc)
                    stored_at = passed["occurred_at"] if self.postgres else self._row_time(
                        passed["occurred_at"])
                    connection.execute(
                        "INSERT INTO agent_issue_rechecks "
                        "(tenant_id,notice_id,recheck_run_id,recheck_event_id,event_at,created_at) "
                        f"VALUES ({','.join([slot] * 6)}) "
                        "ON CONFLICT (tenant_id,notice_id,recheck_run_id,recheck_event_id) "
                        "DO NOTHING",
                        (tenant, notice_id, run_id, event_id, stored_at,
                         now if self.postgres else now.isoformat()),
                    )
                result = {"runId": run_id, "eventId": event_id,
                        "eventAt": self._row_time(
                            existing["event_at"] if existing is not None else
                            passed["occurred_at"]),
                        "sameCheckId": _same_recorded_check(
                            failed["event_type"],
                            _attributes(failed["attributes_json"]),
                            _attributes(passed["attributes_json"]))}
                self._add_recent_references(connection, tenant, [result])
                return result

    def count_open(self, tenant: str) -> int:
        placeholder = "%s" if self.postgres else "?"
        with closing(self._connect()) as connection:
            with connection:
                self._prepare(connection)
                counts = [connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table} "
                    f"WHERE tenant_id={placeholder} AND state='open'", (tenant,),
                ).fetchone()["count"] for table in
                    ("agent_issue_notices", "agent_run_issue_notices")]
                return sum(int(count) for count in counts)

    def transition(self, tenant: str, notice_id: str, state: str) -> bool:
        if state not in {"acknowledged", "dismissed"} or len(notice_id) != 64 or any(
            char not in "0123456789abcdef" for char in notice_id
        ):
            raise ValueError("invalid issue transition")
        placeholder = "%s" if self.postgres else "?"
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection:
            with connection:
                self._prepare(connection)
                changed = 0
                for table in ("agent_issue_notices", "agent_run_issue_notices"):
                    result = connection.execute(
                        f"UPDATE {table} SET state={placeholder},updated_at={placeholder} "
                        f"WHERE tenant_id={placeholder} AND notice_id={placeholder} "
                        "AND state='open'",
                        (state, now if self.postgres else now.isoformat(), tenant,
                         notice_id),
                    )
                    changed += result.rowcount
                return changed == 1
