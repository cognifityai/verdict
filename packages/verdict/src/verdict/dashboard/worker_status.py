"""Small durable heartbeat for the optional recurring Verdict worker."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from verdict.dashboard.storage_url import is_postgres_storage
from verdict.storage.sqlite import sqlite_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict_worker_heartbeats (
    tenant_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, worker_id)
)
"""


class WorkerHeartbeat:
    """Report recent worker presence without equating a saved schedule with execution."""

    def __init__(self, storage_url: str) -> None:
        self.storage_url = storage_url
        self.postgres = is_postgres_storage(storage_url)

    @contextmanager
    def _connection(self) -> Iterator:
        if self.postgres:
            import psycopg

            with psycopg.connect(self.storage_url) as connection:
                yield connection
        else:
            connection = sqlite3.connect(sqlite_path(self.storage_url), timeout=5)
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def touch(self, tenant: str, worker_id: str, *, now: datetime | None = None) -> None:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        placeholder = "%s" if self.postgres else "?"
        with self._connection() as connection:
            connection.execute(_SCHEMA)
            connection.execute(
                "INSERT INTO verdict_worker_heartbeats (tenant_id,worker_id,heartbeat_at) "
                f"VALUES ({placeholder},{placeholder},{placeholder}) "
                "ON CONFLICT (tenant_id,worker_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at",
                (tenant, worker_id, moment.isoformat()),
            )
            connection.execute(
                f"DELETE FROM verdict_worker_heartbeats WHERE heartbeat_at < {placeholder}",
                ((moment - timedelta(days=7)).isoformat(),),
            )

    def remove(self, tenant: str, worker_id: str) -> None:
        placeholder = "%s" if self.postgres else "?"
        with self._connection() as connection:
            connection.execute(_SCHEMA)
            connection.execute(
                f"DELETE FROM verdict_worker_heartbeats WHERE tenant_id={placeholder} AND worker_id={placeholder}",
                (tenant, worker_id),
            )

    def read(self, tenant: str, *, now: datetime | None = None) -> dict[str, str | None]:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        placeholder = "%s" if self.postgres else "?"
        with self._connection() as connection:
            connection.execute(_SCHEMA)
            row = connection.execute(
                "SELECT MAX(heartbeat_at) FROM verdict_worker_heartbeats "
                f"WHERE tenant_id={placeholder}", (tenant,),
            ).fetchone()
        if row is None or row[0] is None:
            return {"status": "unverified", "lastSeenAt": None}
        seen = datetime.fromisoformat(row[0]).astimezone(timezone.utc)
        status = "recent" if timedelta(0) <= moment - seen <= timedelta(seconds=90) else "stale"
        return {"status": status, "lastSeenAt": seen.isoformat()}


_CYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict_worker_cycle_status (
    tenant_id TEXT PRIMARY KEY,
    trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('worker','manual')),
    schedule_revision INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL CHECK(state IN ('started','completed','failed')),
    analysis_status TEXT,
    failure_code TEXT,
    last_success_at TEXT
)
"""
_DUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict_worker_due (
    tenant_id TEXT PRIMARY KEY,
    schedule_revision INTEGER NOT NULL CHECK(schedule_revision > 0),
    next_due_at TEXT NOT NULL
)
"""
_FAILURE_CODES = {
    "capture_or_cycle_error", "capture_incomplete", "analysis_error", "monitor_incomplete",
    "approved_evaluation_incomplete", "delivery_failed",
}


def cycle_failure_code(result: dict, schedule: dict, *, manual: bool) -> str | None:
    """Classify selected-cycle completion without treating HTTP success as check success."""
    capture = result.get("capture")
    if isinstance(capture, dict) and (
        capture.get("scan_incomplete") is True
        or capture.get("skipped", 0) != 0
        or capture.get("recent_sample_errors", 0) != 0
        or (isinstance(capture.get("codex_model_calls"), dict)
            and capture["codex_model_calls"].get("status") != "complete")
    ):
        return "capture_incomplete"
    if result.get("analysis", {}).get("status") != "completed":
        return "analysis_error"
    if schedule.get("runMonitor") is True:
        monitor = result.get("monitor") or {}
        if manual:
            if monitor.get("state") != "active" or not isinstance(monitor.get("snapshot"), dict):
                return "monitor_incomplete"
        elif monitor.get("status") not in {"alert", "no_alert", "insufficient", "reference_stale"}:
            return "monitor_incomplete"
    if schedule.get("includeApprovedEvaluations") is not False and result.get(
        "scheduledEvaluation", {}
    ).get("status") not in {"idle", "completed"}:
        return "approved_evaluation_incomplete"
    if any(item.get("status") == "failed" for item in result.get("notifications", [])):
        return "delivery_failed"
    return None


class WorkerCycleStatus(WorkerHeartbeat):
    """One typed latest-cycle record; an unfinished start is outcome unknown."""

    def begin(self, tenant: str, *, trigger: str, schedule_revision: int | None,
              now: datetime | None = None) -> None:
        if trigger not in {"worker", "manual"} or (
            schedule_revision is not None and (isinstance(schedule_revision, bool)
                                               or not isinstance(schedule_revision, int)
                                               or schedule_revision < 1)
        ):
            raise ValueError("invalid cycle identity")
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        placeholder = "%s" if self.postgres else "?"
        with self._connection() as connection:
            connection.execute(_CYCLE_SCHEMA)
            connection.execute(
                "INSERT INTO verdict_worker_cycle_status "
                "(tenant_id,trigger_kind,schedule_revision,started_at,state) "
                f"VALUES ({','.join([placeholder] * 4)},'started') "
                "ON CONFLICT (tenant_id) DO UPDATE SET "
                "trigger_kind=excluded.trigger_kind,"
                "schedule_revision=excluded.schedule_revision,"
                "started_at=excluded.started_at,finished_at=NULL,state='started',"
                "analysis_status=NULL,failure_code=NULL",
                (tenant, trigger, schedule_revision, timestamp),
            )

    def finish(self, tenant: str, *, state: str, analysis_status: str | None,
               failure_code: str | None, now: datetime | None = None) -> None:
        if state not in {"completed", "failed"} or (
            state == "completed" and failure_code is not None
        ) or (state == "failed" and failure_code not in _FAILURE_CODES):
            raise ValueError("invalid cycle outcome")
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        placeholder = "%s" if self.postgres else "?"
        with self._connection() as connection:
            connection.execute(_CYCLE_SCHEMA)
            updated = connection.execute(
                f"UPDATE verdict_worker_cycle_status SET finished_at={placeholder},"
                f"state={placeholder},analysis_status={placeholder},failure_code={placeholder},"
                f"last_success_at=CASE WHEN {placeholder}='completed' THEN {placeholder} "
                "ELSE last_success_at END "
                f"WHERE tenant_id={placeholder} AND state='started'",
                (timestamp, state, analysis_status, failure_code, state, timestamp, tenant),
            )
            if updated.rowcount != 1:
                raise ValueError("cycle start missing or already finished")

    def read(self, tenant: str) -> dict[str, object]:
        placeholder = "%s" if self.postgres else "?"
        with self._connection() as connection:
            connection.execute(_CYCLE_SCHEMA)
            row = connection.execute(
                "SELECT trigger_kind,schedule_revision,started_at,finished_at,state,"
                "analysis_status,failure_code,last_success_at "
                f"FROM verdict_worker_cycle_status WHERE tenant_id={placeholder}", (tenant,),
            ).fetchone()
        if row is None:
            return {"state": "never_run", "trigger": None, "scheduleRevision": None,
                    "startedAt": None, "finishedAt": None, "analysisStatus": None,
                    "failureCode": None, "lastSuccessAt": None}
        return dict(zip(
            ("trigger", "scheduleRevision", "startedAt", "finishedAt", "state",
             "analysisStatus", "failureCode", "lastSuccessAt"), row, strict=True,
        ))


class WorkerDue(WorkerHeartbeat):
    """Worker-only cadence; manual checks never move its due time."""

    @staticmethod
    def _revision(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("invalid worker schedule revision")

    def read(self, tenant: str) -> dict[str, object] | None:
        placeholder = "%s" if self.postgres else "?"
        with self._connection() as connection:
            connection.execute(_DUE_SCHEMA)
            row = connection.execute(
                "SELECT schedule_revision,next_due_at FROM verdict_worker_due "
                f"WHERE tenant_id={placeholder}", (tenant,),
            ).fetchone()
        return {"scheduleRevision": row[0], "nextDueAt": row[1]} if row else None

    def ready(self, tenant: str, revision: int, *, now: datetime | None = None) -> bool:
        self._revision(revision)
        current = self.read(tenant)
        if current is None or current["scheduleRevision"] != revision:
            return True
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return datetime.fromisoformat(str(current["nextDueAt"])) <= moment

    def begin(self, tenant: str, revision: int, *, now: datetime | None = None) -> None:
        self._revision(revision)
        next_due = ((now or datetime.now(timezone.utc)).astimezone(timezone.utc)
                    + timedelta(seconds=30)).isoformat()
        placeholder = "%s" if self.postgres else "?"
        with self._connection() as connection:
            connection.execute(_DUE_SCHEMA)
            connection.execute(
                "INSERT INTO verdict_worker_due (tenant_id,schedule_revision,next_due_at) "
                f"VALUES ({placeholder},{placeholder},{placeholder}) "
                "ON CONFLICT (tenant_id) DO UPDATE SET "
                "schedule_revision=excluded.schedule_revision,next_due_at=excluded.next_due_at",
                (tenant, revision, next_due),
            )

    def finish(self, tenant: str, revision: int, *, interval_hours: int,
               now: datetime | None = None) -> None:
        self._revision(revision)
        if (isinstance(interval_hours, bool) or not isinstance(interval_hours, int)
                or not 1 <= interval_hours <= 168):
            raise ValueError("invalid worker interval")
        next_due = ((now or datetime.now(timezone.utc)).astimezone(timezone.utc)
                    + timedelta(hours=interval_hours)).isoformat()
        placeholder = "%s" if self.postgres else "?"
        with self._connection() as connection:
            connection.execute(_DUE_SCHEMA)
            updated = connection.execute(
                f"UPDATE verdict_worker_due SET next_due_at={placeholder} "
                f"WHERE tenant_id={placeholder} AND schedule_revision={placeholder}",
                (next_due, tenant, revision),
            )
            if updated.rowcount != 1:
                raise ValueError("worker due start missing or revision changed")
