"""Real storage path for durable recorded-event notices."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier

import httpx
import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.dashboard.app import create_app
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.issue_notices import IssueNotices
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    SourceSession,
)
from verdict.storage import SQLiteStorage
from verdict.storage.postgres import PostgresStorage


def test_recent_fact_recheck_candidates_and_attachment_keep_exact_links(tmp_path):
    path = tmp_path / "recent-recheck.db"
    url = f"sqlite:///{path}"
    tenant = "tenant-a"
    storage = SQLiteStorage(str(path))
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    same_check = "a" * 64
    with sqlite3.connect(path) as connection:
        for run_id, event_id, status, exit_code, second in (
            ("failed-run", "failed-fact", "failed", 1, 0),
            ("passed-run", "passed-fact", "completed", 0, 2),
        ):
            connection.execute(
                "INSERT INTO local_recent_event_facts "
                "(tenant_id,run_id,event_id,turn_id,source_ordinal,turn_sequence,"
                "sequence,occurred_at,event_type,status,provenance,attributes_json,"
                "privacy_classification,omission_reason,trace_id,first_seen_revision) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant, run_id, event_id, f"turn-{run_id}", 1, 1, 0,
                 (now + timedelta(seconds=second)).isoformat(), "command", status,
                 "codex:response_item_output",
                 json.dumps({"exit_code": exit_code, "check_id_hash": same_check}),
                 "omitted", None, None, "b" * 64),
            )
    storage.close()
    notices = IssueNotices(url)
    assert notices.scan(tenant) == 1
    [notice] = notices.list(tenant)
    assert notice["recentRevision"] == "b" * 64
    candidates = notices.find_recheck_candidates(tenant, notice["noticeId"])
    assert candidates["candidates"] == [{
        "runId": "passed-run", "eventId": "passed-fact",
        "eventAt": (now + timedelta(seconds=2)).isoformat(),
        "recentRevision": "b" * 64, "recentTurnId": "turn-passed-run",
    }]
    attached = notices.attach_recheck(
        tenant, notice["noticeId"], "passed-run", "passed-fact",
    )
    assert attached["recentRevision"] == "b" * 64
    assert attached["recentTurnId"] == "turn-passed-run"
    assert notices.list(tenant)[0]["rechecks"][0]["recentRevision"] == "b" * 64

    async def api_paths():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(storage=url, tenant_id=tenant)),
            base_url="http://testserver",
        ) as client:
            found = await client.get(
                f"/api/control/issues/{notice['noticeId']}/candidate-rechecks",
            )
            detail = await client.get(f"/api/control/issues/{notice['noticeId']}")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(storage=url, tenant_id="tenant-b")),
            base_url="http://testserver",
        ) as client:
            foreign = await client.get(
                f"/api/control/issues/{notice['noticeId']}/candidate-rechecks",
            )
        return found, detail, foreign

    found, detail, foreign = asyncio.run(api_paths())
    assert found.status_code == 200
    assert found.json()["candidates"][0]["recentTurnId"] == "turn-passed-run"
    assert detail.status_code == 200
    assert detail.json()["rechecks"][0]["recentRevision"] == "b" * 64
    assert foreign.status_code == 404


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_overlapping_event_identity_is_one_issue_and_one_recheck(tmp_path, backend):
    with ExitStack() as stack:
        if backend == "postgres":
            dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                            allow_any_database=False)
            if dsn is None:
                pytest.skip(reason)
            import psycopg

            url = stack.enter_context(isolated_test_dsn(dsn))
            storage = PostgresStorage(url)
            def connect():
                return psycopg.connect(url)
            slot = "%s"
        else:
            path = tmp_path / "overlap-recheck.db"
            url = f"sqlite:///{path}"
            storage = SQLiteStorage(str(path))
            def connect():
                return sqlite3.connect(path)
            slot = "?"
        stack.callback(storage.close)
        tenant = "tenant-a"
        when = datetime.now(timezone.utc) + timedelta(minutes=1)
        digest = sha256(b"overlap-check").hexdigest()
        ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                                 state="active", payload={"analyzeOnly": True},
                                 expected_revision=None)
        for run_id, event_id, status, attributes, moment in (
            ("failed", "failed-event", ExecutionStatus.FAILED,
             {"failed": 1, "check_id_hash": digest}, when),
            ("passed", "passed-event", ExecutionStatus.COMPLETED,
             {"failed": 0, "exit_code": 0, "check_id_hash": digest},
             when + timedelta(seconds=2)),
        ):
            bundle = _bundle(tenant, run_id, event_id, moment)
            storage.replace_agent_run_bundle(replace(bundle, events=(replace(
                bundle.events[0], status=status, attributes=attributes),)))
            with connect() as connection:
                connection.execute(
                    "INSERT INTO local_recent_event_facts "
                    "(tenant_id,run_id,event_id,turn_id,source_ordinal,turn_sequence,"
                    "sequence,occurred_at,event_type,status,provenance,attributes_json,"
                    "privacy_classification,omission_reason,trace_id,first_seen_revision) "
                    f"VALUES ({','.join([slot] * 16)})",
                    (tenant, run_id, event_id, f"turn-{run_id}", 1, 0, 0,
                     moment.isoformat(), "test_result", status.value, "test:result",
                     json.dumps(attributes), "metadata", None, None, "b" * 64),
                )

        notices = IssueNotices(url)
        assert notices.scan(tenant, page_size=1) == 1
        [notice] = notices.list(tenant)

        async def api_path():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(storage=url, tenant_id=tenant)),
                base_url="http://testserver",
            ) as client:
                token = (await client.get("/api/setup/token")).json()["setupToken"]
                route = f"/api/control/issues/{notice['noticeId']}"
                candidates = await client.get(route + "/candidate-rechecks")
                attached = await client.post(
                    route + "/rechecks", json={"runId": "passed", "eventId": "passed-event"},
                    headers={"X-Verdict-Setup": token},
                )
                detail = await client.get(route)
                return candidates, attached, detail

        candidates, attached, detail = asyncio.run(api_path())
        assert candidates.status_code == attached.status_code == detail.status_code == 200
        assert [(row["runId"], row["eventId"]) for row in
                candidates.json()["candidates"]] == [("passed", "passed-event")]
        assert candidates.json()["scanned"] == 1
        assert "recentRevision" not in candidates.json()["candidates"][0]
        assert "recentRevision" not in attached.json()["recheck"]
        assert [(row["runId"], row["eventId"]) for row in
                detail.json()["rechecks"]] == [("passed", "passed-event")]
        assert "recentRevision" not in notice
        assert "recentRevision" not in detail.json()["rechecks"][0]

        # A retained copy with different facts cannot override canonical evidence.
        with connect() as connection:
            connection.execute(
                "UPDATE local_recent_event_facts SET status='failed',attributes_json="
                f"{slot} WHERE tenant_id={slot} AND run_id={slot} AND event_id={slot}",
                (json.dumps({"failed": 1, "check_id_hash": digest}),
                 tenant, "passed", "passed-event"),
            )
        assert notices.scan(tenant, page_size=1) == 0
        candidates, attached, detail = asyncio.run(api_path())
        assert candidates.status_code == attached.status_code == detail.status_code == 200
        assert len(candidates.json()["candidates"]) == 1
        assert len(detail.json()["rechecks"]) == 1

        # A deleted canonical row reveals the retained fact even when another
        # tenant has the same run/event identifiers.
        foreign = _bundle("tenant-b", "passed", "passed-event",
                          when + timedelta(seconds=2))
        storage.replace_agent_run_bundle(foreign)
        with connect() as connection:
            connection.execute(
                "DELETE FROM agent_events WHERE tenant_id=" + slot
                + " AND run_id=" + slot + " AND event_id=" + slot,
                (tenant, "passed", "passed-event"),
            )
        assert notices.scan(tenant, page_size=1) == 1
        assert notices.get(tenant, notice["noticeId"])["rechecks"] == []
        assert notices.find_recheck_candidates(tenant, notice["noticeId"])["candidates"] == []
        assert any(item.get("recentRevision") == "b" * 64
                   for item in notices.list(tenant) if item["eventId"] == "passed-event")


def _bundle(tenant: str, run_id: str, event_id: str, when: datetime) -> AgentRunBundle:
    source = SourceSession(f"source-{run_id}", tenant, "test-agent",
                           sha256(run_id.encode()).hexdigest(), when, when)
    run = AgentRun(run_id, source.source_session_id, tenant, when,
                   ExecutionStatus.COMPLETED, ended_at=when)
    turn = AgentTurn(f"turn-{run_id}", run_id, 0, when, ExecutionStatus.COMPLETED,
                     when, "run checks", "checks failed", EvidenceState.PRESENT,
                     EvidenceState.PRESENT)
    event = AgentEvent(event_id, turn.turn_id, 0, when, AgentEventType.TEST_RESULT,
                       ExecutionStatus.FAILED, "test:result", {"failed": 1})
    return AgentRunBundle(source, run, (turn,), (event,))


def test_terminal_run_status_notice_is_distinct_from_event_and_tenant_scoped(tmp_path):
    path = tmp_path / "run-status.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    tenant = "tenant-a"
    before = datetime.now(timezone.utc) - timedelta(minutes=2)
    after = datetime.now(timezone.utc) + timedelta(minutes=2)
    for run_id, ended, status in (
        ("preplan", before, ExecutionStatus.FAILED),
        ("failed", after, ExecutionStatus.FAILED),
        ("timedout", after, ExecutionStatus.TIMED_OUT),
        ("completed", after, ExecutionStatus.COMPLETED),
        ("cancelled", after, ExecutionStatus.CANCELLED),
        ("unknown", after, ExecutionStatus.UNKNOWN),
    ):
        bundle = _bundle(tenant, run_id, f"event-{run_id}", ended)
        event = replace(bundle.events[0], status=ExecutionStatus.COMPLETED,
                        attributes={"failed": 0, "exit_code": 0})
        storage.replace_agent_run_bundle(replace(
            bundle, run=replace(bundle.run, status=status), events=(event,),
        ))
        if run_id == "preplan":
            ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                                     state="active", payload={"analyzeOnly": True},
                                     expected_revision=None)
    failed = _bundle(tenant, "both", "failed-event", after)
    storage.replace_agent_run_bundle(replace(
        failed, run=replace(failed.run, status=ExecutionStatus.FAILED),
    ))
    foreign = _bundle("tenant-b", "foreign", "foreign-event", after)
    storage.replace_agent_run_bundle(replace(
        foreign, run=replace(foreign.run, status=ExecutionStatus.FAILED),
    ))
    storage.close()

    notices = IssueNotices(url)
    assert notices.scan(tenant, page_size=1) == 4
    assert notices.scan(tenant, page_size=1) == 0
    page = notices.list(tenant, limit=10)
    assert {(item["runId"], item["checkCode"], item["evidenceKind"])
            for item in page} == {
        ("failed", "run_failed", "run"),
        ("timedout", "run_timed_out", "run"),
        ("both", "run_failed", "run"),
        ("both", "test_failed", "event"),
    }
    assert all(item["eventId"] is None for item in page
               if item["evidenceKind"] == "run")
    assert notices.count_open(tenant) == 4
    assert notices.list("tenant-b") == []
    run_notice = next(item for item in page if item["runId"] == "failed")
    assert notices.transition(tenant, run_notice["noticeId"], "acknowledged")
    assert notices.count_open(tenant) == 3
    assert notices.transition("tenant-b", run_notice["noticeId"], "dismissed") is False
    assert notices.list(tenant, state="reviewed")[0]["noticeId"] == run_notice["noticeId"]


def test_terminal_run_notice_requires_end_time_and_late_import(tmp_path):
    path = tmp_path / "late-run.db"
    url = f"sqlite:///{path}"
    tenant = "tenant-a"
    now = datetime.now(timezone.utc)
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage = SQLiteStorage(str(path))
    later = _bundle(tenant, "later", "pass-later", now + timedelta(minutes=2))
    earlier = _bundle(tenant, "earlier", "pass-earlier", now + timedelta(minutes=1))
    no_end = _bundle(tenant, "no-end", "pass-no-end", now + timedelta(minutes=3))
    for bundle in (later, earlier, no_end):
        event = replace(bundle.events[0], status=ExecutionStatus.COMPLETED,
                        attributes={"failed": 0, "exit_code": 0})
        storage.replace_agent_run_bundle(replace(
            bundle, run=replace(bundle.run, status=ExecutionStatus.FAILED),
            events=(event,),
        ))
    storage.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE agent_runs SET ended_at=NULL WHERE run_id='no-end'")
    notices = IssueNotices(url)
    assert notices.scan(tenant, page_size=1) == 2
    assert {item["runId"] for item in notices.list(tenant)} == {"later", "earlier"}
    assert notices.scan(tenant, page_size=1) == 0


def test_terminal_run_notice_uses_real_api_without_event_selection(tmp_path):
    path = tmp_path / "run-api.db"
    url = f"sqlite:///{path}"
    tenant = "__verdict_local__"
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage = SQLiteStorage(str(path))
    bundle = _bundle(tenant, "run-failed", "event-pass",
                     datetime.now(timezone.utc) + timedelta(minutes=1))
    storage.replace_agent_run_bundle(replace(
        bundle, run=replace(bundle.run, status=ExecutionStatus.FAILED),
        events=(replace(bundle.events[0], status=ExecutionStatus.COMPLETED,
                        attributes={"failed": 0, "exit_code": 0}),),
    ))
    storage.close()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(
                app=create_app(storage=url)), base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            cycle = await client.post("/api/control/actions/run-schedule",
                                      json={"analyzeOnly": True},
                                      headers={"X-Verdict-Setup": token})
            listed = await client.get("/api/control/issues")
            notice = listed.json()["issues"][0]
            run = await client.get("/api/runs/run-failed")
            invalid_recheck = await client.post(
                f"/api/control/issues/{notice['noticeId']}/rechecks",
                json={"runId": "run-failed", "eventId": "event-pass"},
                headers={"X-Verdict-Setup": token},
            )
            return cycle, notice, run, invalid_recheck

    cycle, notice, run, invalid_recheck = asyncio.run(scenario())
    assert cycle.status_code == 200
    assert notice["checkCode"] == "run_failed"
    assert notice["evidenceKind"] == "run" and notice["eventId"] is None
    assert run.status_code == 200
    assert invalid_recheck.status_code == 400


def test_post_activation_event_notice_is_durable_deduplicated_and_tenant_scoped(tmp_path):
    url = f"sqlite:///{tmp_path / 'issues.db'}"
    storage = SQLiteStorage(str(tmp_path / "issues.db"))
    store = IssueNotices(url)
    before = datetime.now(timezone.utc) - timedelta(minutes=2)
    after = datetime.now(timezone.utc) + timedelta(minutes=2)
    storage.replace_agent_run_bundle(_bundle("tenant-a", "old", "same-event", before))
    ControlStore(url).append("tenant-a", kind="schedule", document_id="daily",
                             state="active", payload={"intervalHours": 24,
                                                      "analyzeOnly": True},
                             expected_revision=None)
    storage.replace_agent_run_bundle(_bundle("tenant-a", "new", "same-event", after))
    storage.replace_agent_run_bundle(_bundle("tenant-b", "new", "same-event", after))

    assert store.scan("tenant-a") == 1
    assert store.scan("tenant-a") == 0
    [notice] = store.list("tenant-a")
    assert notice["runId"] == "new" and notice["eventId"] == "same-event"
    assert notice["checkCode"] == "test_failed" and notice["state"] == "open"
    assert store.list("tenant-b") == []
    assert store.transition("tenant-a", notice["noticeId"], "acknowledged") is True
    assert store.count_open("tenant-a") == 0
    assert store.transition("tenant-a", notice["noticeId"], "dismissed") is False
    assert store.scan("tenant-a") == 0
    assert store.list("tenant-a")[0]["state"] == "acknowledged"
    assert store.transition("tenant-b", notice["noticeId"], "dismissed") is False

    storage.replace_agent_run_bundle(_bundle("tenant-a", "newer", "same-event", after))
    assert store.scan("tenant-a") == 1
    assert len(store.list("tenant-a")) == 2
    ControlStore(url).append("tenant-a", kind="schedule", document_id="daily",
                             state="disabled", payload={"intervalHours": 24,
                                                        "analyzeOnly": True},
                             expected_revision=1)
    storage.replace_agent_run_bundle(_bundle("tenant-a", "while-disabled", "same-event", after))
    assert store.scan("tenant-a") == 0
    ControlStore(url).append("tenant-a", kind="schedule", document_id="daily",
                             state="active", payload={"intervalHours": 24,
                                                      "analyzeOnly": True},
                             expected_revision=2)
    assert store.scan("tenant-a") == 1
    storage.close()


def test_tool_timeout_generates_the_same_recorded_issue_as_run_analysis(tmp_path):
    url = f"sqlite:///{tmp_path / 'timeout.db'}"
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    bundle = _bundle("tenant-a", "timeout", "tool-timeout", when)
    event = replace(bundle.events[0], event_type=AgentEventType.TOOL_RESULT,
                    status=ExecutionStatus.TIMED_OUT, attributes={"is_error": False})
    storage = SQLiteStorage(str(tmp_path / "timeout.db"))
    ControlStore(url).append("tenant-a", kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage.replace_agent_run_bundle(replace(bundle, events=(event,)))
    storage.close()
    notices = IssueNotices(url)
    assert notices.scan("tenant-a") == 1
    assert notices.list("tenant-a")[0]["checkCode"] == "tool_error"


def test_later_recorded_pass_is_attached_without_resolving_notice(tmp_path):
    url = f"sqlite:///{tmp_path / 'recheck.db'}"
    tenant = "tenant-a"
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    storage = SQLiteStorage(str(tmp_path / "recheck.db"))
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage.replace_agent_run_bundle(_bundle(tenant, "failed-run", "failed-event", base))
    passed = _bundle(tenant, "passed-run", "passed-event", base + timedelta(seconds=2))
    passed_event = replace(passed.events[0], status=ExecutionStatus.COMPLETED,
                           attributes={"failed": 0, "exit_code": 0})
    storage.replace_agent_run_bundle(replace(passed, events=(passed_event,)))
    storage.close()
    notices = IssueNotices(url)
    assert notices.scan(tenant) == 1
    [notice] = notices.list(tenant)

    result = notices.attach_recheck(tenant, notice["noticeId"],
                                    "passed-run", "passed-event")
    assert result["runId"] == "passed-run"
    assert result["eventId"] == "passed-event"
    assert notices.attach_recheck(tenant, notice["noticeId"],
                                  "passed-run", "passed-event") == result
    [updated] = notices.list(tenant)
    assert updated["state"] == "open"
    assert updated["rechecks"] == [result]
    assert notices.count_open(tenant) == 1
    assert notices.list("tenant-b") == []
    with pytest.raises(ValueError):
        notices.attach_recheck("tenant-b", notice["noticeId"],
                                "passed-run", "passed-event")


def test_recheck_reports_only_matching_valid_application_check_digest(tmp_path):
    path = tmp_path / "check-identity.db"
    url = f"sqlite:///{path}"
    tenant = "tenant-a"
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage = SQLiteStorage(str(path))
    first_digest = sha256(b"check-one").hexdigest()
    other_digest = sha256(b"check-two").hexdigest()
    failed = _bundle(tenant, "failed", "failed-event", when)
    storage.replace_agent_run_bundle(replace(failed, events=(replace(
        failed.events[0], attributes={"failed": 1, "check_id_hash": first_digest}),)))
    for index, digest in enumerate((first_digest, other_digest, None), start=1):
        passed = _bundle(tenant, f"pass-{index}", f"event-{index}",
                         when + timedelta(seconds=index))
        attributes = {"failed": 0, "exit_code": 0}
        if digest is not None:
            attributes["check_id_hash"] = digest
        storage.replace_agent_run_bundle(replace(passed, events=(replace(
            passed.events[0], status=ExecutionStatus.COMPLETED,
            attributes=attributes),)))
    storage.close()
    notices = IssueNotices(url)
    assert notices.scan(tenant) == 1
    [notice] = notices.list(tenant)
    for index in range(1, 4):
        notices.attach_recheck(tenant, notice["noticeId"],
                               f"pass-{index}", f"event-{index}")
    attached = {item["runId"]: item for item in notices.list(tenant)[0]["rechecks"]}
    assert attached["pass-1"]["sameCheckId"] is True
    assert attached["pass-2"]["sameCheckId"] is False
    assert attached["pass-3"]["sameCheckId"] is False
    assert notices.count_open(tenant) == 1
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE agent_events SET attributes_json=? "
                           "WHERE tenant_id=? AND run_id=?",
                           ('{"failed":0,"exit_code":0,"check_id_hash":"<EMAIL>"}',
                            tenant, "pass-1"))
    assert next(item for item in notices.list(tenant)[0]["rechecks"]
                if item["runId"] == "pass-1")["sameCheckId"] is False


def test_same_check_failure_after_attached_pass_creates_a_new_open_notice(tmp_path):
    path = tmp_path / "recheck-relapse.db"
    url = f"sqlite:///{path}"
    tenant = "tenant-a"
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    digest = sha256(b"stable-check").hexdigest()
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage = SQLiteStorage(str(path))
    for index, (status, failed_count) in enumerate((
        (ExecutionStatus.FAILED, 1),
        (ExecutionStatus.COMPLETED, 0),
        (ExecutionStatus.FAILED, 1),
    )):
        bundle = _bundle(tenant, f"attempt-{index}", f"event-{index}",
                         when + timedelta(seconds=index))
        event = replace(bundle.events[0], status=status,
                        attributes={"exit_code": failed_count,
                                    "failed": failed_count,
                                    "check_id_hash": digest})
        storage.replace_agent_run_bundle(replace(bundle, events=(event,)))
    storage.close()

    notices = IssueNotices(url)
    assert notices.scan(tenant) == 2
    first = next(item for item in notices.list(tenant) if item["runId"] == "attempt-0")
    notices.attach_recheck(tenant, first["noticeId"], "attempt-1", "event-1")
    assert notices.scan(tenant) == 0
    current = {item["runId"]: item for item in notices.list(tenant)}
    assert set(current) == {"attempt-0", "attempt-2"}
    assert current["attempt-0"]["state"] == "open"
    assert current["attempt-0"]["rechecks"][0]["sameCheckId"] is True
    assert current["attempt-2"]["state"] == "open"
    assert current["attempt-2"]["rechecks"] == []
    assert notices.count_open(tenant) == 2


def test_candidate_search_finds_only_later_same_check_pass_without_changing_notice(tmp_path):
    path = tmp_path / "candidate-search.db"
    url = f"sqlite:///{path}"
    tenant = "tenant-a"
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    digest = sha256(b"candidate-check").hexdigest()
    other = sha256(b"other-check").hexdigest()
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage = SQLiteStorage(str(path))

    def put(run_id, offset, *, failed=0, check=digest, kind=AgentEventType.TEST_RESULT,
            selected_tenant=tenant):
        bundle = _bundle(selected_tenant, run_id, f"event-{run_id}",
                         base + timedelta(seconds=offset))
        event = replace(bundle.events[0], event_type=kind,
                        status=ExecutionStatus.FAILED if failed else ExecutionStatus.COMPLETED,
                        attributes={**({"failed": failed} if kind is AgentEventType.TEST_RESULT else {}),
                                    "exit_code": failed, "check_id_hash": check})
        storage.replace_agent_run_bundle(replace(bundle, events=(event,)))

    put("failure-first", 0, failed=1)
    put("same-time", 0)
    put("wrong-hash", 1, check=other)
    put("wrong-type", 2, kind=AgentEventType.COMMAND)
    put("foreign", 3, selected_tenant="tenant-b")
    put("pass", 4)
    put("failure-second", 5, failed=1)
    storage.close()

    notices = IssueNotices(url)
    assert notices.scan(tenant) == 2
    by_run = {item["runId"]: item for item in notices.list(tenant)}
    first = notices.find_recheck_candidates(tenant, by_run["failure-first"]["noticeId"])
    second = notices.find_recheck_candidates(tenant, by_run["failure-second"]["noticeId"])
    assert first["candidates"] == [{
        "runId": "pass", "eventId": "event-pass",
        "eventAt": (base + timedelta(seconds=4)).isoformat(),
    }]
    assert first["partial"] is False
    assert second["candidates"] == []
    assert notices.count_open(tenant) == 2
    assert notices.list(tenant)[0]["rechecks"] == []
    with pytest.raises(ValueError, match="candidate evidence unavailable"):
        notices.find_recheck_candidates("tenant-b", by_run["failure-first"]["noticeId"])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE agent_events SET attributes_json=? "
            "WHERE tenant_id=? AND run_id='pass'",
            ('{"failed":1,"exit_code":1,"check_id_hash":"' + digest + '"}', tenant),
        )
    with pytest.raises(ValueError, match="recheck evidence unavailable"):
        notices.attach_recheck(tenant, by_run["failure-first"]["noticeId"],
                               "pass", "event-pass")
    assert notices.list(tenant)[0]["rechecks"] == []


def test_candidate_search_bounds_raw_rows_before_check_filter(tmp_path, monkeypatch):
    import verdict.dashboard.issue_notices as issue_notices

    path = tmp_path / "candidate-limit.db"
    url = f"sqlite:///{path}"
    tenant = "tenant-a"
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    digest = sha256(b"bounded-check").hexdigest()
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage = SQLiteStorage(str(path))
    for index, check in enumerate((digest, "", "", digest)):
        bundle = _bundle(tenant, f"run-{index}", f"event-{index}",
                         base + timedelta(seconds=index))
        event = replace(bundle.events[0],
                        status=ExecutionStatus.FAILED if index == 0 else ExecutionStatus.COMPLETED,
                        attributes={"failed": 1 if index == 0 else 0,
                                    "exit_code": 1 if index == 0 else 0,
                                    **({"check_id_hash": check} if check else {})})
        storage.replace_agent_run_bundle(replace(bundle, events=(event,)))
    storage.close()
    notices = IssueNotices(url)
    assert notices.scan(tenant) == 1
    monkeypatch.setattr(issue_notices, "MAX_CANDIDATE_SCAN_ROWS", 2)
    result = notices.find_recheck_candidates(tenant, notices.list(tenant)[0]["noticeId"])
    assert result["scanned"] == 2
    assert result["partial"] is True
    assert result["candidates"] == []


def test_candidate_search_does_not_materialize_oversized_event_attributes(tmp_path):
    path = tmp_path / "candidate-oversized.db"
    url = f"sqlite:///{path}"
    tenant = "tenant-a"
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    digest = sha256(b"oversized-check").hexdigest()
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage = SQLiteStorage(str(path))
    for index in range(3):
        bundle = _bundle(tenant, f"run-{index}", f"event-{index}",
                         base + timedelta(seconds=index))
        failed = index == 0
        event = replace(bundle.events[0],
                        status=ExecutionStatus.FAILED if failed else ExecutionStatus.COMPLETED,
                        attributes={"failed": int(failed), "exit_code": int(failed),
                                    "check_id_hash": digest})
        storage.replace_agent_run_bundle(replace(bundle, events=(event,)))
    storage.close()
    with sqlite3.connect(path) as connection:
        # Simulate a legacy or externally modified row beyond the current
        # SQLite table constraint; the read path must still stay bounded.
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE agent_events SET attributes_json=? WHERE tenant_id=? AND run_id='run-1'",
            ('{"failed":0,"exit_code":0,"check_id_hash":"' + digest
             + '","untrusted":"' + ('x' * 500_000) + '"}', tenant),
        )
    notices = IssueNotices(url)
    assert notices.scan(tenant) == 1
    result = notices.find_recheck_candidates(tenant, notices.list(tenant)[0]["noticeId"])
    assert result["partial"] is True
    assert result["candidates"] == [{
        "runId": "run-2", "eventId": "event-2",
        "eventAt": (base + timedelta(seconds=2)).isoformat(),
    }]


def test_live_postgres_candidate_search_matches_sqlite_contract():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            tenant = "candidate-pg"
            base = datetime.now(timezone.utc) + timedelta(minutes=1)
            digest = sha256(b"postgres-candidate").hexdigest()
            ControlStore(isolated).append(
                tenant, kind="schedule", document_id="daily", state="active",
                payload={"analyzeOnly": True}, expected_revision=None,
            )
            for index in range(3):
                bundle = _bundle(tenant, f"run-{index}", f"event-{index}",
                                 base + timedelta(seconds=index))
                failed = index == 0
                event = replace(bundle.events[0],
                                status=ExecutionStatus.FAILED if failed else ExecutionStatus.COMPLETED,
                                attributes={"failed": int(failed), "exit_code": int(failed),
                                            "check_id_hash": digest if index != 1
                                            else sha256(b"other").hexdigest()})
                storage.replace_agent_run_bundle(replace(bundle, events=(event,)))
            notices = IssueNotices(isolated)
            assert notices.scan(tenant) == 1
            [notice] = notices.list(tenant)
            found = notices.find_recheck_candidates(tenant, notice["noticeId"])
            assert len(found["candidates"]) == 1
            [candidate] = found["candidates"]
            assert candidate["runId"] == "run-2"
            assert candidate["eventId"] == "event-2"
            assert datetime.fromisoformat(candidate["eventAt"]) == base + timedelta(seconds=2)
            assert found["partial"] is False
            assert notice["state"] == notices.list(tenant)[0]["state"] == "open"
            attached = notices.attach_recheck(tenant, notice["noticeId"], "run-2", "event-2")
            assert notices.get(tenant, notice["noticeId"])["rechecks"] == [attached]
            assert notices.get("foreign-tenant", notice["noticeId"]) is None
            import psycopg

            with psycopg.connect(isolated) as connection:
                connection.execute(
                    "UPDATE agent_events SET attributes_json=%s::jsonb "
                    "WHERE tenant_id=%s AND run_id='run-1'",
                    (json.dumps({"failed": 0, "exit_code": 0,
                                 "check_id_hash": digest,
                                 "untrusted": "x" * 500_000}), tenant),
                )
            bounded = notices.find_recheck_candidates(tenant, notice["noticeId"])
            assert bounded["partial"] is True
            assert bounded["candidates"] == found["candidates"]
        finally:
            storage.close()


def test_recheck_requires_later_explicit_positive_event(tmp_path):
    url = f"sqlite:///{tmp_path / 'recheck-invalid.db'}"
    tenant = "tenant-a"
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    storage = SQLiteStorage(str(tmp_path / "recheck-invalid.db"))
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage.replace_agent_run_bundle(_bundle(tenant, "failed-run", "failed-event", base))
    for run_id, when, status, attributes in (
        ("earlier", base, ExecutionStatus.COMPLETED, {"failed": 0, "exit_code": 0}),
        ("unknown", base + timedelta(seconds=1), ExecutionStatus.COMPLETED, {}),
        ("conflict", base + timedelta(seconds=2), ExecutionStatus.FAILED,
         {"failed": 0, "exit_code": 0}),
        ("still-failed", base + timedelta(seconds=3), ExecutionStatus.COMPLETED,
         {"failed": 1, "exit_code": 0}),
    ):
        bundle = _bundle(tenant, run_id, f"{run_id}-event", when)
        storage.replace_agent_run_bundle(replace(
            bundle, events=(replace(bundle.events[0], status=status,
                                    attributes=attributes),),
        ))
    storage.close()
    notices = IssueNotices(url)
    assert notices.scan(tenant) == 3
    notice = next(item for item in notices.list(tenant) if item["runId"] == "failed-run")
    for run_id in ("earlier", "unknown", "conflict", "still-failed", "failed-run"):
        event_id = "failed-event" if run_id == "failed-run" else f"{run_id}-event"
        with pytest.raises(ValueError):
            notices.attach_recheck(tenant, notice["noticeId"], run_id, event_id)
    assert notices.list(tenant)[0]["rechecks"] == []


def test_stale_rechecks_release_bounded_slots_for_a_new_pass(tmp_path):
    path = tmp_path / "stale-rechecks.db"
    url = f"sqlite:///{path}"
    tenant = "tenant-a"
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    storage = SQLiteStorage(str(path))
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage.replace_agent_run_bundle(_bundle(tenant, "failed", "failed-event", base))
    for index in range(9):
        run_id = f"pass-{index}"
        bundle = _bundle(tenant, run_id, f"event-{index}",
                         base + timedelta(seconds=index + 1))
        storage.replace_agent_run_bundle(replace(bundle, events=(replace(
            bundle.events[0], status=ExecutionStatus.COMPLETED,
            attributes={"failed": 0, "exit_code": 0},
        ),)))
    storage.close()
    notices = IssueNotices(url)
    assert notices.scan(tenant) == 1
    notice = notices.list(tenant)[0]
    for index in range(8):
        notices.attach_recheck(tenant, notice["noticeId"],
                               f"pass-{index}", f"event-{index}")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE agent_events SET attributes_json=? "
                           "WHERE tenant_id=? AND run_id LIKE 'pass-%' AND run_id!='pass-8'",
                           ('{"failed":1,"exit_code":0}', tenant))
    assert notices.list(tenant)[0]["rechecks"] == []
    newest = notices.attach_recheck(tenant, notice["noticeId"], "pass-8", "event-8")
    assert notices.list(tenant)[0]["rechecks"] == [newest]


def test_malformed_historical_boolean_counts_do_not_create_failure_notices(tmp_path):
    path = tmp_path / "boolean-counts.db"
    url = f"sqlite:///{path}"
    tenant = "tenant-a"
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    storage = SQLiteStorage(str(path))
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    for run_id, event_type, attributes in (
        ("command", AgentEventType.COMMAND, {"exit_code": 0}),
        ("test", AgentEventType.TEST_RESULT, {"failed": 0}),
    ):
        bundle = _bundle(tenant, run_id, f"{run_id}-event", when)
        event = replace(bundle.events[0], event_type=event_type,
                        status=ExecutionStatus.COMPLETED, attributes=attributes)
        storage.replace_agent_run_bundle(replace(bundle, events=(event,)))
    storage.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE agent_events SET attributes_json=? WHERE event_id=?",
                           ('{"exit_code":true}', "command-event"))
        connection.execute("UPDATE agent_events SET attributes_json=? WHERE event_id=?",
                           ('{"failed":true}', "test-event"))

    notices = IssueNotices(url)
    assert notices.scan(tenant) == 0
    assert notices.list(tenant) == []
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE agent_events SET status='failed' WHERE event_id=?",
                           ("command-event",))
    assert notices.scan(tenant) == 1
    assert [item["checkCode"] for item in notices.list(tenant)] == ["command_failed"]


def test_scheduled_cycle_continues_past_malformed_historical_event_attributes(tmp_path):
    path = tmp_path / "malformed-cycle.db"
    url = f"sqlite:///{path}"
    tenant = "__verdict_local__"
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    storage = SQLiteStorage(str(path))
    for index, (name, status) in enumerate((
        ("bad-completed", ExecutionStatus.COMPLETED),
        ("bad-failed", ExecutionStatus.FAILED),
        ("nonobject-completed", ExecutionStatus.COMPLETED),
        ("nonobject-timeout", ExecutionStatus.TIMED_OUT),
        ("later-valid", ExecutionStatus.FAILED),
    )):
        bundle = _bundle(tenant, name, f"event-{name}", when + timedelta(seconds=index))
        event = replace(bundle.events[0], status=status,
                        attributes={"failed": 1} if name == "later-valid" else {})
        storage.replace_agent_run_bundle(replace(bundle, events=(event,)))
    storage.replace_agent_run_bundle(_bundle("foreign", "foreign", "event-foreign", when))
    storage.close()
    with sqlite3.connect(path) as connection:
        for name, raw in (
            ("bad-completed", "{broken"), ("bad-failed", "{broken"),
            ("nonobject-completed", "[]"), ("nonobject-timeout", "null"),
        ):
            connection.execute("UPDATE agent_events SET attributes_json=? WHERE event_id=?",
                               (raw, f"event-{name}"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(
                app=create_app(storage=url)), base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            cycle = await client.post("/api/control/actions/run-schedule",
                                      json={"analyzeOnly": True},
                                      headers={"X-Verdict-Setup": token})
            inbox = await client.get("/api/control/issues")
            readiness = await client.get("/api/readiness")
            return cycle, inbox, readiness

    cycle, inbox, readiness = asyncio.run(scenario())
    assert cycle.status_code == 200, cycle.text
    assert cycle.json()["cycleFailureCode"] == "analysis_error"
    assert inbox.status_code == 200
    assert readiness.status_code == 200
    assert readiness.json()["lastCycle"]["state"] == "failed"
    assert {(item["runId"], item["checkCode"], item["evidenceKind"])
            for item in inbox.json()["issues"]} == {
        ("bad-failed", "test_failed", "event"),
        ("nonobject-timeout", "test_failed", "event"),
        ("later-valid", "test_failed", "event"),
    }
    assert IssueNotices(url).scan(tenant, page_size=1) == 0


def test_notice_scan_preserves_out_of_order_new_event_and_does_not_use_failure_text(tmp_path):
    url = f"sqlite:///{tmp_path / 'late.db'}"
    storage = SQLiteStorage(str(tmp_path / "late.db"))
    ControlStore(url).append("tenant-a", kind="schedule", document_id="daily",
                             state="active", payload={"intervalHours": 24,
                                                      "analyzeOnly": True},
                             expected_revision=None)
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    newer = _bundle("tenant-a", "newer", "event", base + timedelta(minutes=2))
    storage.replace_agent_run_bundle(newer)
    store = IssueNotices(url)
    assert store.scan("tenant-a") == 1
    older = _bundle("tenant-a", "older", "event", base)
    storage.replace_agent_run_bundle(older)
    assert store.scan("tenant-a") == 1
    assert {item["runId"] for item in store.list("tenant-a")} == {"older", "newer"}
    storage.replace_agent_run_bundle(older)
    assert store.scan("tenant-a") == 0
    storage.close()


def test_saved_schedule_cycle_exposes_event_and_review_transition_through_api(tmp_path):
    url = f"sqlite:///{tmp_path / 'api.db'}"
    storage = SQLiteStorage(str(tmp_path / "api.db"))
    tenant = "__verdict_local__"
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"intervalHours": 24,
                                                      "analyzeOnly": True},
                             expected_revision=None)
    storage.replace_agent_run_bundle(_bundle(
        tenant, "observed-run", "failed-test", datetime.now(timezone.utc) + timedelta(seconds=1),
    ))
    storage.close()

    async def scenario():
        transport = httpx.ASGITransport(app=create_app(storage=url))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            before = await client.get("/api/control/issues")
            cycle = await client.post("/api/control/actions/run-schedule",
                                      json={"analyzeOnly": True},
                                      headers={"X-Verdict-Setup": token})
            issues = await client.get("/api/control/issues")
            notice = issues.json()["issues"][0]
            detail = await client.get("/api/runs/observed-run",
                                      params={"event_id": notice["eventId"]})
            unauthorized = await client.post(
                f"/api/control/issues/{notice['noticeId']}/acknowledge",
            )
            changed = await client.post(
                f"/api/control/issues/{notice['noticeId']}/acknowledge",
                headers={"X-Verdict-Setup": token},
            )
            repeated = await client.post(
                f"/api/control/issues/{notice['noticeId']}/dismiss",
                headers={"X-Verdict-Setup": token},
            )
            after = await client.get("/api/control/issues", params={"state": "reviewed"})
            readiness = await client.get("/api/readiness")
            return before, cycle, issues, detail, unauthorized, changed, repeated, after, readiness

    before, cycle, issues, detail, unauthorized, changed, repeated, after, readiness = asyncio.run(scenario())
    assert before.status_code == 200 and before.json()["issues"] == []
    assert cycle.status_code == 200 and cycle.json()["newIssues"] == 1
    assert issues.status_code == 200 and len(issues.json()["issues"]) == 1
    assert detail.status_code == 200
    assert detail.json()["focusEventId"] == "failed-test"
    assert unauthorized.status_code == 403
    assert changed.status_code == 200 and changed.json()["state"] == "acknowledged"
    assert repeated.status_code == 409
    assert after.json()["issues"][0]["state"] == "acknowledged"
    assert readiness.json()["openIssues"] == 0


def test_recheck_attachment_round_trips_through_authorized_issue_api(tmp_path):
    url = f"sqlite:///{tmp_path / 'recheck-api.db'}"
    tenant = "__verdict_local__"
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    storage = SQLiteStorage(str(tmp_path / "recheck-api.db"))
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    check_digest = sha256(b"synthetic-api-check").hexdigest()
    failed = _bundle(tenant, "failed", "failed-event", when)
    storage.replace_agent_run_bundle(replace(failed, events=(replace(
        failed.events[0], attributes={"failed": 1,
                                      "check_id_hash": check_digest}),)))
    passed = _bundle(tenant, "passed", "passed-event", when + timedelta(minutes=1))
    storage.replace_agent_run_bundle(replace(passed, events=(replace(
        passed.events[0], status=ExecutionStatus.COMPLETED,
        attributes={"failed": 0, "exit_code": 0, "check_id_hash": check_digest},
    ),)))
    storage.close()
    assert IssueNotices(url).scan(tenant) == 1

    async def scenario():
        transport = httpx.ASGITransport(app=create_app(storage=url))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            notice = (await client.get("/api/control/issues")).json()["issues"][0]
            route = f"/api/control/issues/{notice['noticeId']}/rechecks"
            candidates = await client.get(
                f"/api/control/issues/{notice['noticeId']}/candidate-rechecks")
            payload = {"runId": "passed", "eventId": "passed-event"}
            unauthorized = await client.post(route, json=payload)
            invalid = await client.post(route, json={"runId": "failed",
                                                     "eventId": "failed-event"},
                                        headers={"X-Verdict-Setup": token})
            malformed_unicode = await client.post(
                route, content='{"runId":"\\ud800","eventId":"x"}',
                headers={"X-Verdict-Setup": token, "Content-Type": "application/json"},
            )
            attached = await client.post(route, json=payload,
                                         headers={"X-Verdict-Setup": token})
            listed = await client.get("/api/control/issues")
            exact = await client.get(f"/api/control/issues/{notice['noticeId']}")
            missing = await client.get("/api/control/issues/not-a-notice")
            detail = await client.get("/api/runs/passed")
            session = await client.get("/api/session", params={
                "session_id": "source-passed", "source_kind": "test-agent"})
            turn = await client.get("/api/runs/passed/turns/turn-passed/analysis")
            return candidates, unauthorized, invalid, malformed_unicode, attached, listed, exact, missing, detail, session, turn

    candidates, unauthorized, invalid, malformed_unicode, attached, listed, exact, missing, detail, session, turn = asyncio.run(scenario())
    assert candidates.status_code == 200
    assert candidates.json()["candidates"] == [{
        "runId": "passed", "eventId": "passed-event",
        "eventAt": (when + timedelta(minutes=1)).isoformat(),
    }]
    assert check_digest not in candidates.text
    assert unauthorized.status_code == 403
    assert invalid.status_code == 400
    assert malformed_unicode.status_code == 400
    assert attached.status_code == 200
    assert attached.json()["recheck"]["eventId"] == "passed-event"
    assert attached.json()["recheck"]["sameCheckId"] is True
    assert listed.json()["issues"][0]["rechecks"] == [attached.json()["recheck"]]
    assert exact.status_code == 200
    assert exact.json() == listed.json()["issues"][0]
    assert missing.status_code == 404
    assert check_digest not in listed.text
    assert detail.status_code == 200
    assert check_digest not in detail.text
    assert session.status_code == 200 and check_digest not in session.text
    assert turn.status_code == 200 and check_digest not in turn.text
    assert listed.json()["issues"][0]["state"] == "open"


def test_open_issue_page_is_not_hidden_behind_reviewed_history(tmp_path):
    url = f"sqlite:///{tmp_path / 'issue-pages.db'}"
    tenant = "__verdict_local__"
    storage = SQLiteStorage(str(tmp_path / "issue-pages.db"))
    ControlStore(url).append(tenant, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    for index in range(52):
        storage.replace_agent_run_bundle(_bundle(
            tenant, f"run-{index:02}", f"event-{index:02}",
            base + timedelta(seconds=index),
        ))
    storage.close()
    notices = IssueNotices(url)
    assert notices.scan(tenant) == 52
    for notice in notices.list(tenant, limit=100):
        if notice["runId"] != "run-00":
            assert notices.transition(tenant, notice["noticeId"], "acknowledged")

    async def scenario():
        transport = httpx.ASGITransport(app=create_app(storage=url))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return (await client.get("/api/control/issues"),
                    await client.get("/api/control/issues", params={"state": "reviewed"}),
                    await client.get("/api/control/issues", params={"state": "reviewed", "offset": 50}),
                    await client.get("/api/control/issues", params={"state": "bogus"}))

    open_page, reviewed, reviewed_next, invalid = asyncio.run(scenario())
    assert [item["runId"] for item in open_page.json()["issues"]] == ["run-00"]
    assert open_page.json()["nextOffset"] is None
    assert len(reviewed.json()["issues"]) == 50
    assert reviewed.json()["nextOffset"] == 50
    assert len(reviewed_next.json()["issues"]) == 1
    assert invalid.status_code == 400


def test_live_postgres_issue_notice_parity():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            tenant = "issue-test-tenant"
            ControlStore(isolated).append(tenant, kind="schedule", document_id="daily",
                                          state="active", payload={"intervalHours": 24,
                                                                   "analyzeOnly": True},
                                          expected_revision=None)
            storage.replace_agent_run_bundle(_bundle(
                tenant, "postgres-run", "postgres-event",
                datetime.now(timezone.utc) + timedelta(seconds=1),
            ))
            notices = IssueNotices(isolated)
            assert notices.scan(tenant, page_size=1) == 1
            assert notices.scan(tenant, page_size=1) == 0
            [notice] = notices.list(tenant)
            assert notice["checkCode"] == "test_failed"
            assert notices.count_open(tenant) == 1
            assert notices.transition(tenant, notice["noticeId"], "dismissed")
            assert notices.count_open(tenant) == 0
            assert notices.list("different-tenant") == []
        finally:
            storage.close()


def test_live_postgres_nonobject_historical_attributes_keep_explicit_status():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    import psycopg

    with isolated_test_dsn(dsn) as isolated:
        tenant = "issue-attributes-pg"
        ControlStore(isolated).append(tenant, kind="schedule", document_id="daily",
                                      state="active", payload={"analyzeOnly": True},
                                      expected_revision=None)
        storage = PostgresStorage(isolated)
        try:
            when = datetime.now(timezone.utc) + timedelta(minutes=1)
            for index, (run_id, status) in enumerate((
                ("failed", ExecutionStatus.FAILED),
                ("completed", ExecutionStatus.COMPLETED),
                ("later", ExecutionStatus.FAILED),
            )):
                bundle = _bundle(tenant, run_id, f"event-{run_id}",
                                 when + timedelta(seconds=index))
                storage.replace_agent_run_bundle(replace(
                    bundle, events=(replace(bundle.events[0], status=status,
                                            attributes={"failed": 1} if run_id == "later" else {}),),
                ))
            with psycopg.connect(isolated) as connection:
                connection.execute("UPDATE agent_events SET attributes_json='[]'::jsonb "
                                   "WHERE tenant_id=%s AND event_id='event-failed'", (tenant,))
                connection.execute("UPDATE agent_events SET attributes_json='null'::jsonb "
                                   "WHERE tenant_id=%s AND event_id='event-completed'", (tenant,))
            notices = IssueNotices(isolated)
            assert notices.scan(tenant, page_size=1) == 2
            assert {(item["runId"], item["checkCode"])
                    for item in notices.list(tenant)} == {
                ("failed", "test_failed"), ("later", "test_failed"),
            }
            assert notices.scan(tenant, page_size=1) == 0
        finally:
            storage.close()


def test_live_postgres_terminal_run_notice_and_two_scanners():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            tenant = "run-status-pg"
            ControlStore(isolated).append(tenant, kind="schedule", document_id="daily",
                                          state="active", payload={"analyzeOnly": True},
                                          expected_revision=None)
            when = datetime.now(timezone.utc) + timedelta(minutes=1)
            for run_id, status in (("failed", ExecutionStatus.FAILED),
                                   ("timeout", ExecutionStatus.TIMED_OUT)):
                bundle = _bundle(tenant, run_id, f"pass-{run_id}", when)
                storage.replace_agent_run_bundle(replace(
                    bundle, run=replace(bundle.run, status=status),
                    events=(replace(bundle.events[0],
                                    status=ExecutionStatus.COMPLETED,
                                    attributes={"failed": 0, "exit_code": 0}),),
                ))
            notices = IssueNotices(isolated)
            start = Barrier(2)

            def scan():
                start.wait(timeout=5)
                return notices.scan(tenant, page_size=1)

            with ThreadPoolExecutor(max_workers=2) as workers:
                added = list(workers.map(lambda _: scan(), range(2)))
            assert sum(added) == 2
            assert {item["checkCode"] for item in notices.list(tenant)} == {
                "run_failed", "run_timed_out",
            }
            assert notices.count_open(tenant) == 2
            assert notices.list("foreign") == []
        finally:
            storage.close()


def test_live_postgres_malformed_boolean_exit_code_does_not_create_notice():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    import psycopg

    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            tenant = "issue-boolean-tenant"
            ControlStore(isolated).append(tenant, kind="schedule", document_id="daily",
                                          state="active", payload={"analyzeOnly": True},
                                          expected_revision=None)
            bundle = _bundle(tenant, "boolean-command", "command-event",
                             datetime.now(timezone.utc) + timedelta(minutes=1))
            command = replace(bundle.events[0], event_type=AgentEventType.COMMAND,
                              status=ExecutionStatus.COMPLETED, attributes={"exit_code": 0})
            storage.replace_agent_run_bundle(replace(bundle, events=(command,)))
            with psycopg.connect(isolated) as connection:
                connection.execute(
                    "UPDATE agent_events SET attributes_json=%s::jsonb WHERE event_id=%s",
                    ('{"exit_code":true}', "command-event"),
                )
            notices = IssueNotices(isolated)
            assert notices.scan(tenant) == 0
            with psycopg.connect(isolated) as connection:
                connection.execute("UPDATE agent_events SET status='failed' WHERE event_id=%s",
                                   ("command-event",))
            assert notices.scan(tenant) == 1
            assert notices.list(tenant)[0]["checkCode"] == "command_failed"
        finally:
            storage.close()


def test_live_postgres_recheck_attachment_and_deleted_evidence():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    import psycopg

    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            tenant = "recheck-pg-tenant"
            when = datetime.now(timezone.utc) + timedelta(minutes=1)
            ControlStore(isolated).append(tenant, kind="schedule", document_id="daily",
                                          state="active", payload={"analyzeOnly": True},
                                          expected_revision=None)
            failed = _bundle(tenant, "failed", "failed-event", when)
            check_digest = sha256(b"synthetic-check").hexdigest()
            storage.replace_agent_run_bundle(replace(failed, events=(replace(
                failed.events[0], attributes={"failed": 1,
                                              "check_id_hash": check_digest}),)))
            for index in range(9):
                passed = _bundle(tenant, f"pass-{index}", f"event-{index}",
                                 when + timedelta(seconds=index + 1))
                storage.replace_agent_run_bundle(replace(passed, events=(replace(
                    passed.events[0], status=ExecutionStatus.COMPLETED,
                    attributes={"failed": 0, "exit_code": 0,
                                **({"check_id_hash": check_digest} if index == 8 else {})},
                ),)))
            notices = IssueNotices(isolated)
            assert notices.scan(tenant) == 1
            [notice] = notices.list(tenant)
            for index in range(8):
                notices.attach_recheck(tenant, notice["noticeId"],
                                       f"pass-{index}", f"event-{index}")
            assert len(notices.list(tenant)[0]["rechecks"]) == 8
            assert notices.count_open(tenant) == 1
            with psycopg.connect(isolated) as connection:
                connection.execute(
                    "UPDATE agent_events SET attributes_json=%s::jsonb "
                    "WHERE tenant_id=%s AND run_id LIKE 'pass-%%' AND run_id!='pass-8'",
                    ('{"failed":1,"exit_code":0}', tenant),
                )
            assert notices.list(tenant)[0]["rechecks"] == []
            attached = notices.attach_recheck(tenant, notice["noticeId"],
                                               "pass-8", "event-8")
            assert attached["sameCheckId"] is True
            assert notices.list(tenant)[0]["rechecks"] == [attached]
            assert notices.count_open(tenant) == 1
        finally:
            storage.close()


def test_live_postgres_concurrent_rechecks_respect_notice_limit():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)

    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            tenant = "recheck-race-tenant"
            when = datetime.now(timezone.utc) + timedelta(minutes=1)
            ControlStore(isolated).append(tenant, kind="schedule", document_id="daily",
                                          state="active", payload={"analyzeOnly": True},
                                          expected_revision=None)
            storage.replace_agent_run_bundle(_bundle(tenant, "failed", "failed-event", when))
            for index in range(9):
                passed = _bundle(tenant, f"pass-{index}", f"event-{index}",
                                 when + timedelta(seconds=index + 1))
                storage.replace_agent_run_bundle(replace(passed, events=(replace(
                    passed.events[0], status=ExecutionStatus.COMPLETED,
                    attributes={"failed": 0, "exit_code": 0},
                ),)))
            notices = IssueNotices(isolated)
            assert notices.scan(tenant) == 1
            notice_id = notices.list(tenant)[0]["noticeId"]
            start = Barrier(9)

            def attach(index):
                start.wait(timeout=5)
                try:
                    notices.attach_recheck(tenant, notice_id,
                                           f"pass-{index}", f"event-{index}")
                    return True
                except ValueError as exc:
                    assert str(exc) == "recheck evidence limit reached"
                    return False

            with ThreadPoolExecutor(max_workers=9) as workers:
                results = list(workers.map(attach, range(9)))
            assert results.count(True) == 8
            assert results.count(False) == 1
            assert len(notices.list(tenant)[0]["rechecks"]) == 8
        finally:
            storage.close()
