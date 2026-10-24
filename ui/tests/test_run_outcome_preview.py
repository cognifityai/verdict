"""Read-only production Agent Run outcome coverage at the dashboard boundary."""

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import httpx
from verdict.dashboard.app import build_agent_usage_bundle, create_app
from verdict.dashboard.query import SQLiteSession
from verdict.dashboard.run_outcome_preview import build_run_outcome_preview
from verdict.dashboard.usage import build_usage_view
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    ExecutionStatus,
    SourceSession,
)
from verdict.storage import SQLiteStorage

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def _run(path, *, tenant, run_id, run_status="completed", result_status=None,
         is_error=None, agent_name="worker", source_kind="verdict_sdk"):
    store = SQLiteStorage(str(path))
    started = NOW + timedelta(minutes=len(run_id))
    source_id = f"source-{tenant}-{run_id}"
    terminal = run_status != "unknown"
    event = () if result_status is None else (
        AgentEvent(
            event_id=f"event-{tenant}-{run_id}", turn_id=f"turn-{tenant}-{run_id}",
            sequence=0, occurred_at=started, event_type=AgentEventType.TOOL_RESULT,
            status=ExecutionStatus(result_status), provenance="test-tool",
            attributes={"is_error": is_error} if is_error is not None else {},
        ),
    )
    try:
        store.replace_agent_run_bundle(AgentRunBundle(
            SourceSession(source_id, tenant, source_kind,
                          sha256(source_id.encode()).hexdigest(), started, started),
            AgentRun(run_id, source_id, tenant, started, ExecutionStatus(run_status),
                     started + timedelta(seconds=1) if terminal else None,
                     agent_name=agent_name, service_name="app", environment="prod"),
            (AgentTurn(f"turn-{tenant}-{run_id}", run_id, 0, started,
                       ExecutionStatus(run_status),
                       started + timedelta(seconds=1) if terminal else None),),
            event,
        ))
    finally:
        store.close()


def _append_tool_records(path, *, tenant, run_id, calls, results):
    store = SQLiteStorage(str(path))
    try:
        bundle = store.get_agent_run_bundle(tenant, run_id)
        assert bundle is not None
        events = list(bundle.events)
        for kind, identifiers in ((AgentEventType.TOOL_CALL, calls),
                                  (AgentEventType.TOOL_RESULT, results)):
            for call_id in identifiers:
                sequence = len(events)
                attributes = {} if call_id is None else {"call_id": call_id}
                if kind is AgentEventType.TOOL_RESULT:
                    attributes["is_error"] = False
                events.append(AgentEvent(
                    f"{run_id}-{kind.value}-{sequence}", bundle.turns[0].turn_id,
                    sequence, NOW, kind,
                    ExecutionStatus.COMPLETED if kind is AgentEventType.TOOL_RESULT
                    else ExecutionStatus.UNKNOWN,
                    "test-tool", attributes,
                ))
        store.replace_agent_run_bundle(AgentRunBundle(
            bundle.session, bundle.run, bundle.turns, tuple(events),
        ))
    finally:
        store.close()


def test_preview_requires_matched_call_outcomes_before_clean_recorded_coverage(tmp_path):
    path = tmp_path / "paired-actions.db"
    for run_id, calls, results in (
        ("paired", ["a", "b"], ["b", "a"]),
        ("sparse", ["a", "b"], ["a"]),
        ("duplicate", ["a"], ["a", "a"]),
        ("missing-id", [None], [None]),
        ("orphan", ["a"], ["b"]),
    ):
        _run(path, tenant="customer-a", run_id=run_id)
        _append_tool_records(path, tenant="customer-a", run_id=run_id,
                             calls=calls, results=results)
    _run(path, tenant="customer-b", run_id="foreign")
    _append_tool_records(path, tenant="customer-b", run_id="foreign",
                         calls=["a", "b"], results=["a"])
    _run(path, tenant="customer-a", run_id="failed-sparse",
         result_status="failed", is_error=True)
    _append_tool_records(path, tenant="customer-a", run_id="failed-sparse",
                         calls=["a", "b"], results=[])

    result = build_run_outcome_preview(f"sqlite:///{path}", tenant="customer-a")
    assert result["totals"] == {
        "recordedFailure": 1, "observedWithoutFailure": 1,
        "unclear": 4, "conflicts": 0,
    }
    assert result["groups"][0]["examples"]["observedWithoutFailure"] == ["paired"]
    assert "foreign" not in json.dumps(result)


def test_preview_classifies_reported_tool_outcomes_without_leaking_other_tenants(tmp_path):
    path = tmp_path / "runs.db"
    _run(path, tenant="customer-a", run_id="failed", result_status="failed", is_error=False)
    _run(path, tenant="customer-a", run_id="healthy", result_status="completed", is_error=False)
    _run(path, tenant="customer-a", run_id="unknown", result_status="unknown")
    _run(path, tenant="customer-a", run_id="unknown-nonerror",
         result_status="unknown", is_error=False)
    _run(path, tenant="customer-a", run_id="timeout", result_status="timed_out")
    _run(path, tenant="customer-a", run_id="cancelled", result_status="cancelled")
    _run(path, tenant="customer-a", run_id="imported", run_status="unknown")
    _run(path, tenant="customer-b", run_id="FOREIGN-PRIVATE-RUN", result_status="failed")

    async def request():
        app = create_app(storage=f"sqlite:///{path}", tenant_id="customer-a")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get("/api/agent-run-outcomes")

    response = asyncio.run(request())
    assert response.status_code == 200
    result = response.json()
    assert result["schema"] == "agent-run-outcome-preview-v1"
    assert result["status"] == "ready"
    assert result["scope"] == {"terminalRuns": 6, "sampledTerminalRuns": 6,
                               "truncated": False}
    assert result["totals"] == {"recordedFailure": 2,
                                "observedWithoutFailure": 1,
                                "unclear": 3, "conflicts": 1}
    assert result["groups"][0]["agentName"] == "worker"
    assert result["groups"][0]["comparison"]["earlier"]["terminalRuns"] == 3
    assert result["groups"][0]["comparison"]["newer"]["terminalRuns"] == 3
    assert result["groups"][0]["examples"]["recordedFailure"]
    assert "FOREIGN-PRIVATE-RUN" not in json.dumps(result)
    assert "imported" not in json.dumps(result["groups"])
    assert result["execution"]["totals"]["unfinalized"] == 1


def test_preview_shows_bounded_recorded_execution_statuses_even_without_terminal_runs(tmp_path):
    import sqlite3

    path = tmp_path / "run-statuses.db"
    for run_id, status in (
        ("done", "completed"), ("failed", "failed"),
        ("timed", "timed_out"), ("cancel", "cancelled"),
        ("unknown", "unknown"), ("unfinished", "completed"),
        ("odd", "unknown"),
    ):
        _run(path, tenant="customer-a", run_id=run_id, run_status=status)
    _run(path, tenant="customer-b", run_id="FOREIGN-PRIVATE-RUN",
         run_status="failed")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE agent_runs SET ended_at=NULL WHERE run_id='unfinished'")
        connection.execute(
            "UPDATE agent_runs SET ended_at=? WHERE run_id='odd'",
            ((NOW + timedelta(hours=1)).isoformat(),),
        )
        connection.execute(
            "UPDATE agent_runs SET started_at=? WHERE tenant_id='customer-a'",
            (NOW.isoformat(),),
        )

    result = build_run_outcome_preview(f"sqlite:///{path}", tenant="customer-a")
    assert result["execution"]["scope"] == {"sampledRuns": 7, "truncated": False}
    assert result["execution"]["totals"] == {
        "completed": 1, "failed": 1, "timed_out": 1,
        "cancelled": 1, "unfinalized": 3,
    }
    [group] = result["execution"]["groups"]
    assert group["agentName"] == "worker"
    assert group["counts"] == result["execution"]["totals"]
    assert group["examples"]["unfinalized"] == ["unknown", "unfinished", "odd"]
    assert "FOREIGN-PRIVATE-RUN" not in json.dumps(result)
    sampled = build_run_outcome_preview(f"sqlite:///{path}", tenant="customer-a",
                                        max_runs=2)
    assert sampled["execution"]["scope"] == {"sampledRuns": 2, "truncated": True}
    assert sum(sampled["execution"]["totals"].values()) == 2
    assert "FOREIGN-PRIVATE-RUN" not in json.dumps(sampled)

    history = tmp_path / "unfinished-history.db"
    _run(history, tenant="customer-a", run_id="history", run_status="unknown",
         source_kind="codex")
    only_unfinalized = build_run_outcome_preview(
        f"sqlite:///{history}", tenant="customer-a",
    )
    assert only_unfinalized["status"] == "no_terminal_runs"
    assert only_unfinalized["execution"]["totals"]["unfinalized"] == 1


def test_preview_compares_earlier_and_newer_exact_agent_runs_without_health_claim(tmp_path):
    path = tmp_path / "run-order.db"
    # Insert in reverse event-time order; the comparison is by recorded end
    # time, not ingestion order or the sampled query's descending presentation.
    for run_id, status in (
        ("dddd", "completed"), ("ccc", "unknown"),
        ("bb", "failed"), ("a", "completed"),
    ):
        _run(path, tenant="customer-a", run_id=run_id, result_status=status)
    _run(path, tenant="customer-b", run_id="foreign", result_status="failed")
    result = build_run_outcome_preview(f"sqlite:///{path}", tenant="customer-a")
    group = result["groups"][0]
    assert group["comparison"] == {
        "earlier": {"terminalRuns": 2, "recordedFailure": 1,
                    "observedWithoutFailure": 1, "unclear": 0},
        "newer": {"terminalRuns": 2, "recordedFailure": 0,
                  "observedWithoutFailure": 1, "unclear": 1},
    }
    assert "foreign" not in json.dumps(result)
    assert "alert" not in json.dumps(result).lower()


def test_preview_treats_capture_limit_as_unclear_unless_failure_is_recorded(tmp_path):
    path = tmp_path / "partial-events.db"
    _run(path, tenant="customer-a", run_id="partial-success",
         result_status="completed", is_error=False)
    _run(path, tenant="customer-a", run_id="partial-failure",
         result_status="failed", is_error=True)
    _run(path, tenant="customer-b", run_id="foreign-partial",
         result_status="completed", is_error=False)
    storage = SQLiteStorage(str(path))
    try:
        for tenant, run_id in (
            ("customer-a", "partial-success"),
            ("customer-a", "partial-failure"),
            ("customer-b", "foreign-partial"),
        ):
            bundle = storage.get_agent_run_bundle(tenant, run_id)
            assert bundle is not None
            marker = AgentEvent(
                event_id=f"marker-{run_id}", turn_id=bundle.turns[0].turn_id,
                sequence=1, occurred_at=NOW, event_type=AgentEventType.CONTEXT,
                status=ExecutionStatus.UNKNOWN, provenance="verdict:capture_limit",
                attributes={"name": "source_events_omitted", "source": "1", "available": False},
            )
            storage.replace_agent_run_bundle(AgentRunBundle(
                bundle.session, bundle.run, bundle.turns, (*bundle.events, marker),
            ))
    finally:
        storage.close()

    async def request():
        app = create_app(storage=f"sqlite:///{path}", tenant_id="customer-a")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get("/api/agent-run-outcomes")

    response = asyncio.run(request())
    assert response.status_code == 200
    result = response.json()
    assert result["totals"] == {
        "recordedFailure": 1, "observedWithoutFailure": 0,
        "unclear": 1, "conflicts": 0,
    }
    assert result["groups"][0]["examples"]["unclear"] == ["partial-success"]
    assert "foreign-partial" not in json.dumps(result)
    limited = build_run_outcome_preview(f"sqlite:///{path}", tenant="customer-a",
                                        max_events=3)
    assert limited["status"] == "event_limit"
    assert limited["totals"] is None


def test_preview_rejects_nonboolean_error_flags_as_outcome_evidence(tmp_path):
    import sqlite3

    path = tmp_path / "flag-types.db"
    for run_id, flag in (
        ("integer-zero", 0), ("integer-one", 1),
        ("text-false", "false"), ("text-true", "true"),
    ):
        _run(path, tenant="customer-a", run_id=run_id,
             result_status="unknown")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE agent_events SET attributes_json=? WHERE tenant_id=? AND run_id=?",
                (json.dumps({"is_error": flag}), "customer-a", run_id),
            )

    async def request():
        app = create_app(storage=f"sqlite:///{path}", tenant_id="customer-a")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get("/api/agent-run-outcomes")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["totals"] == {
        "recordedFailure": 0, "observedWithoutFailure": 0,
        "unclear": 4, "conflicts": 0,
    }


def test_preview_keeps_malformed_tool_attributes_unclear_and_available(tmp_path):
    import sqlite3

    path = tmp_path / "malformed-outcomes.db"
    for run_id, tenant, status, attributes in (
        ("malformed", "customer-a", "completed", "{broken"),
        ("array", "customer-a", "completed", "[]"),
        ("explicit-failure", "customer-a", "failed", "{broken"),
        ("valid", "customer-a", "completed", '{"is_error":false}'),
        ("foreign", "customer-b", "completed", "{broken"),
    ):
        _run(path, tenant=tenant, run_id=run_id, result_status=status)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE agent_events SET attributes_json=? WHERE tenant_id=? AND run_id=?",
                (attributes, tenant, run_id),
            )
    _run(path, tenant="customer-a", run_id="malformed-call")
    _append_tool_records(path, tenant="customer-a", run_id="malformed-call",
                         calls=["matched"], results=["matched"])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE agent_events SET attributes_json=? WHERE tenant_id=? "
            "AND run_id=? AND event_type='tool_call'",
            ("{broken", "customer-a", "malformed-call"),
        )

    async def request():
        app = create_app(storage=f"sqlite:///{path}", tenant_id="customer-a")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get("/api/agent-run-outcomes")

    response = asyncio.run(request())
    assert response.status_code == 200
    result = response.json()
    assert result["totals"] == {
        "recordedFailure": 1, "observedWithoutFailure": 1,
        "unclear": 3, "conflicts": 0,
    }
    assert "foreign" not in json.dumps(result)


def test_preview_obeys_dashboard_basic_auth_when_configured(tmp_path, monkeypatch):
    path = tmp_path / "protected.db"
    _run(path, tenant="customer-a", run_id="private", result_status="failed")
    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")

    async def request():
        app = create_app(storage=f"sqlite:///{path}", tenant_id="customer-a")
        token = base64.b64encode(b"reviewer:secret").decode()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return (await client.get("/api/agent-run-outcomes"),
                    await client.get("/api/agent-run-outcomes",
                                     headers={"Authorization": f"Basic {token}"}))

    denied, allowed = asyncio.run(request())
    assert denied.status_code == 401
    assert "private" not in denied.text
    assert allowed.status_code == 200
    assert allowed.json()["scope"]["terminalRuns"] == 1


def test_preview_bounds_terminal_runs_and_discloses_omissions(tmp_path):
    path = tmp_path / "bounded.db"
    for index in range(3):
        _run(path, tenant="customer-a", run_id=f"run-{index}",
             result_status="completed", is_error=False)
    result = build_run_outcome_preview(f"sqlite:///{path}", tenant="customer-a",
                                       max_runs=2)
    assert result["scope"] == {"terminalRuns": None, "sampledTerminalRuns": 2,
                               "truncated": True}
    assert result["totals"]["observedWithoutFailure"] == 2
    assert all(group["comparison"] is None for group in result["groups"])


def test_preview_preserves_exact_evidence_link_with_sensitive_run_id(tmp_path):
    path = tmp_path / "identifiers.db"
    run_id = "run-alice@example.com"
    _run(path, tenant="customer-a", run_id=run_id,
         result_status="failed", is_error=True)
    result = build_run_outcome_preview(f"sqlite:///{path}", tenant="customer-a")
    assert result["groups"][0]["examples"]["recordedFailure"] == [run_id]


def test_preview_uses_indexed_recent_terminal_scan(tmp_path):
    import sqlite3

    path = tmp_path / "indexed.db"
    _run(path, tenant="customer-a", run_id="recent", result_status="completed")
    with sqlite3.connect(path) as connection:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(agent_runs)")}
        assert "idx_agent_runs_tenant_ended" in indexes
        plan = " ".join(str(row[3]) for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT r.run_id,r.agent_name,r.service_name,"
            "r.environment,s.source_kind FROM agent_runs r JOIN import_sources s "
            "ON s.tenant_id=r.tenant_id AND s.source_session_id=r.source_session_id "
            "WHERE r.tenant_id=? AND r.status IN (?,?,?,?) "
            "AND r.ended_at IS NOT NULL "
            "ORDER BY r.ended_at DESC,r.run_id DESC LIMIT ?",
            ("customer-a", "completed", "failed", "timed_out", "cancelled", 1001),
        ))
        assert "idx_agent_runs_tenant_ended" in plan
        assert "TEMP B-TREE" not in plan


def test_preview_requires_recent_run_index_on_legacy_read_only_store(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    _run(path, tenant="customer-a", run_id="older", result_status="failed")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_agent_runs_tenant_ended")
    result = build_run_outcome_preview(f"sqlite:///{path}", tenant="customer-a")
    assert result["status"] == "migration_required"
    assert result["totals"] is None
    assert result["groups"] == []


def test_usage_day_joins_recorded_issues_by_event_date_and_tenant(tmp_path):
    import sqlite3

    path = tmp_path / "usage-issues.db"
    _run(path, tenant="customer-a", run_id="own-failure", result_status="failed")
    _run(path, tenant="customer-a", run_id="own-success", result_status="completed")
    _run(path, tenant="customer-b", run_id="FOREIGN-PRIVATE-RUN",
         result_status="failed")
    with sqlite3.connect(path) as connection:
        issue_day = (NOW + timedelta(days=1)).date().isoformat()
        connection.execute(
            "UPDATE agent_events SET occurred_at=? WHERE tenant_id=? AND run_id=?",
            ((NOW + timedelta(days=1)).isoformat(), "customer-a", "own-failure"),
        )
        connection.commit()
        connection.row_factory = sqlite3.Row
        result = build_usage_view(SQLiteSession(connection), tenant="customer-a",
                                  now=NOW + timedelta(days=1), day=issue_day,
                                  session_id=None)
    selected = next(item for item in result["days"] if item["day"] == issue_day)
    assert selected["recordedIssueEvents"] == 1
    turn_day = next(item for item in result["days"] if item["day"] == NOW.date().isoformat())
    assert turn_day["recordedIssueEvents"] == 0
    assert result["selectedDayIssues"]["complete"] is True
    assert result["selectedDayIssues"]["events"] == [{
        "code": "tool_error", "runId": "own-failure",
        "eventId": "event-customer-a-own-failure",
    }]
    assert "FOREIGN-PRIVATE-RUN" not in json.dumps(result)


def test_usage_withholds_issue_counts_when_event_scan_is_partial(tmp_path, monkeypatch):
    import sqlite3

    import verdict.dashboard.usage as usage

    path = tmp_path / "partial-issues.db"
    _run(path, tenant="customer-a", run_id="failure-1", result_status="failed")
    _run(path, tenant="customer-a", run_id="failure-2", result_status="failed")
    monkeypatch.setattr(usage, "MAX_ISSUE_EVENTS", 1)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        result = build_usage_view(SQLiteSession(connection), tenant="customer-a",
                                  now=NOW, day=NOW.date().isoformat(), session_id=None)
    assert result["selectedDayIssues"] == {"complete": False, "count": None,
                                           "events": [], "eventsTruncated": False}
    assert all(item["recordedIssueEvents"] is None for item in result["days"])


def test_usage_issue_links_preserve_exact_dashboard_identifiers(tmp_path):
    path = tmp_path / "usage-identity.db"
    run_id = "run-alice@example.com"
    _run(path, tenant="customer-a", run_id=run_id, result_status="failed")
    result = build_agent_usage_bundle(
        f"sqlite:///{path}", tenant="customer-a", now=NOW,
        day=NOW.date().isoformat())
    assert result["selectedDayIssues"]["events"][0]["runId"] == run_id


def test_usage_issue_link_resolves_exact_run_and_event_through_api(tmp_path):
    path = tmp_path / "exact-link.db"
    run_id = "run-alice@example.com"
    _run(path, tenant="customer-a", run_id=run_id, result_status="failed",
         agent_name="contact-bob@example.com")
    usage = build_agent_usage_bundle(
        f"sqlite:///{path}", tenant="customer-a", now=NOW,
        day=NOW.date().isoformat(),
    )
    link = usage["selectedDayIssues"]["events"][0]

    async def request():
        app = create_app(storage=f"sqlite:///{path}", tenant_id="customer-a")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return (
                await client.get("/api/runs", params={"view": "list", "selected_run_id": link["runId"]}),
                await client.get(f"/api/runs/{link['runId']}", params={"event_id": link["eventId"]}),
            )

    listed, detail = asyncio.run(request())
    assert listed.status_code == detail.status_code == 200
    assert listed.json()["selectedRun"]["runId"] == link["runId"]
    assert any(row["runId"] == link["runId"] for row in listed.json()["runs"])
    assert detail.json()["runId"] == link["runId"]
    assert detail.json()["focusEventId"] == link["eventId"]
    assert any(row["eventId"] == link["eventId"] for row in detail.json()["events"])
    assert detail.json()["events"][0]["issueCode"] == "tool_error"
    assert "contact-bob@example.com" not in listed.text
    assert "contact-bob@example.com" not in detail.text
    assert listed.json()["selectedRun"]["agentName"] == "<EMAIL>"


def test_usage_day_counts_recorded_tool_timeout_like_run_preview(tmp_path):
    path = tmp_path / "tool-timeout.db"
    _run(path, tenant="customer-a", run_id="timeout", result_status="timed_out")
    usage = build_agent_usage_bundle(
        f"sqlite:///{path}", tenant="customer-a", now=NOW,
        day=NOW.date().isoformat(),
    )
    preview = build_run_outcome_preview(f"sqlite:///{path}", tenant="customer-a")
    assert preview["totals"]["recordedFailure"] == 1
    assert usage["selectedDayIssues"]["count"] == 1
    assert usage["selectedDayIssues"]["events"][0]["code"] == "tool_error"


def test_usage_day_counts_recorded_command_and_test_timeouts(tmp_path):
    import sqlite3

    path = tmp_path / "execution-timeouts.db"
    for kind in ("command", "test_result"):
        _run(path, tenant="customer-a", run_id=kind, result_status="timed_out")
    with sqlite3.connect(path) as connection:
        for kind in ("command", "test_result"):
            connection.execute(
                "UPDATE agent_events SET event_type=? WHERE tenant_id=? AND run_id=?",
                (kind, "customer-a", kind),
            )
        connection.commit()
    usage = build_agent_usage_bundle(
        f"sqlite:///{path}", tenant="customer-a", now=NOW,
        day=NOW.date().isoformat(),
    )
    assert usage["selectedDayIssues"]["count"] == 2
    assert {item["code"] for item in usage["selectedDayIssues"]["events"]} == {
        "command_failed", "test_failed",
    }


def test_run_detail_exposes_shared_issue_codes_for_explicit_failure_flags(tmp_path):
    import sqlite3

    path = tmp_path / "explicit-flags.db"
    examples = (
        ("tool", "tool_result", '{"is_error":true}', "tool_error"),
        ("command", "command", '{"exit_code":7}', "command_failed"),
        ("test", "test_result", '{"failed":2}', "test_failed"),
    )
    for run_id, _, _, _ in examples:
        _run(path, tenant="customer-a", run_id=run_id,
             result_status="completed", is_error=False)
    with sqlite3.connect(path) as connection:
        for run_id, kind, attributes, _ in examples:
            connection.execute(
                "UPDATE agent_events SET event_type=?,attributes_json=? "
                "WHERE tenant_id=? AND run_id=?",
                (kind, attributes, "customer-a", run_id),
            )
        connection.commit()

    async def request():
        app = create_app(storage=f"sqlite:///{path}", tenant_id="customer-a")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return [await client.get(f"/api/runs/{run_id}")
                    for run_id, _, _, _ in examples]

    responses = asyncio.run(request())
    assert all(response.status_code == 200 for response in responses)
    assert [response.json()["events"][0]["issueCode"] for response in responses] == [
        code for _, _, _, code in examples
    ]


def test_usage_issue_scan_tolerates_invalid_legacy_attributes(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-attributes.db"
    _run(path, tenant="customer-a", run_id="failed-status", result_status="failed")
    _run(path, tenant="customer-a", run_id="string-flag",
         result_status="completed", is_error=False)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE agent_events SET attributes_json=? WHERE tenant_id=? AND run_id=?",
            ("not json", "customer-a", "failed-status"),
        )
        connection.execute(
            "UPDATE agent_events SET attributes_json=? WHERE tenant_id=? AND run_id=?",
            ('{"is_error":"true"}', "customer-a", "string-flag"),
        )
        connection.commit()
    result = build_agent_usage_bundle(
        f"sqlite:///{path}", tenant="customer-a", now=NOW,
        day=NOW.date().isoformat())
    assert result["selectedDayIssues"]["count"] == 1


def test_preview_refuses_to_classify_a_truncated_tool_result_sample(tmp_path):
    path = tmp_path / "events.db"
    for index in range(2):
        _run(path, tenant="customer-a", run_id=f"run-{index}",
             result_status="completed", is_error=False)
    result = build_run_outcome_preview(f"sqlite:///{path}", tenant="customer-a",
                                       max_events=1)
    assert result["status"] == "event_limit"
    assert result["scope"]["sampledTerminalRuns"] == 2
    assert result["totals"] is None
    assert result["groups"] == []
