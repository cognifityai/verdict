"""Real SQLite and HTTP checks for the local agent usage drilldown."""

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
import verdict.dashboard.app as dashboard_app
import verdict.dashboard.usage as usage_module
from verdict.dashboard.app import build_agent_session_bundle, build_agent_usage_bundle, create_app
from verdict.dashboard.day_explanation import _context, preview_day_explanation
from verdict.dashboard.query import PostgresSession, SQLiteSession
from verdict.dashboard.recommendations import run_recommendations
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
)
from verdict.storage import SQLiteStorage
from verdict.telemetry.local_agents import capture_local_agents


def _dt(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_usage_clock(monkeypatch):
    """Keep the October source fixtures inside the API's report window."""
    monkeypatch.setattr(dashboard_app, "_now_utc", lambda: _dt(14, 12))


def _bundle(tenant: str, run_id: str, turns, events=(), *, session_id=None):
    started = min(turn.started_at for turn in turns)
    return AgentRunBundle(
        SourceSession(f"source-{tenant}-{run_id}", tenant, "synthetic-codex", "a" * 64, started, started),
        AgentRun(run_id, f"source-{tenant}-{run_id}", tenant, started,
                 ExecutionStatus.UNKNOWN, session_id=session_id),
        tuple(turns), tuple(events),
    )


def _turn(run_id: str, turn_id: str, sequence: int, started: datetime, total,
          *, input_tokens=None, output_tokens=None, ended=None):
    return AgentTurn(
        turn_id, run_id, sequence, started, ExecutionStatus.COMPLETED,
        ended or started, input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=total,
        token_usage_basis="codex_turn_delta" if total is not None else None,
    )


def _model_event(turn_id: str, event_id: str, model: str, sequence=0):
    return AgentEvent(
        event_id, turn_id, sequence, _dt(8, 1), AgentEventType.CONTEXT,
        ExecutionStatus.COMPLETED, "test", {"name": "model_configuration", "source": model},
        PrivacyClassification.METADATA,
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_day_issue_reader_counts_overlapping_canonical_and_recent_identity_once(
    tmp_path, backend,
):
    with ExitStack() as stack:
        if backend == "postgres":
            import psycopg
            from verdict.storage.postgres import PostgresStorage

            from packages.verdict.tests._postgres_test_safety import (
                isolated_test_dsn,
                validate_test_dsn,
            )

            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            storage_url = stack.enter_context(isolated_test_dsn(dsn))
            storage = PostgresStorage(storage_url)
        else:
            path = tmp_path / "overlapping-issue.db"
            storage_url = f"sqlite:///{path}"
            storage = SQLiteStorage(str(path))
        stack.callback(storage.close)
        run_id = "overlapping-run"
        turn_id = "overlapping-turn"
        event_id = "same-event"
        storage.replace_agent_run_bundle(_bundle(
            "local", run_id,
            (_turn(run_id, turn_id, 0, _dt(12, 23), 100),),
            (AgentEvent(
                event_id, turn_id, 0, _dt(13, 1), AgentEventType.COMMAND,
                ExecutionStatus.FAILED, "test", {"exit_code": 1},
                PrivacyClassification.METADATA,
            ),),
        ))
        if backend == "postgres":
            connection = psycopg.connect(storage_url)
            placeholder = "%s"
        else:
            connection = sqlite3.connect(path)
            placeholder = "?"
        with connection:
            connection.execute(
                "INSERT INTO local_recent_event_facts "
                "(tenant_id,run_id,event_id,turn_id,source_ordinal,turn_sequence,"
                "sequence,occurred_at,event_type,status,provenance,attributes_json,"
                "privacy_classification,omission_reason,trace_id,first_seen_revision) "
                f"VALUES ({','.join([placeholder] * 16)})",
                ("local", run_id, event_id, turn_id, 1, 0, 0,
                 _dt(12, 23).isoformat(), "command", "failed", "test",
                 json.dumps({"exit_code": 1}), "metadata", None, None, "b" * 64),
            )
        connection.close()

        async def request(day):
            transport = httpx.ASGITransport(app=create_app(
                storage=storage_url, tenant_id="local",
            ))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(f"/api/usage?day=2026-10-{day:02d}")

        canonical = asyncio.run(request(13))
        assert canonical.status_code == 200
        assert canonical.json()["selectedDayIssues"]["events"] == [{
            "code": "command_failed", "runId": run_id, "eventId": event_id,
        }]
        assert canonical.json()["selectedDayIssues"]["count"] == 1
        earlier = asyncio.run(request(12))
        assert earlier.status_code == 200
        assert earlier.json()["selectedDayIssues"]["count"] == 0


def test_usage_groups_turns_by_utc_day_and_drills_into_session_without_cross_tenant_data(tmp_path):
    path = tmp_path / "usage.db"
    storage = SQLiteStorage(str(path))
    first = _turn("run-a", "a-1", 0, _dt(7, 23), 100,
                  input_tokens=80, output_tokens=20, ended=_dt(8, 0))
    first = replace(first, user_request_redacted="hidden-canary-example",
                    request_state="present")
    second = _turn("run-a", "a-2", 1, _dt(8, 1), 300,
                   input_tokens=250, output_tokens=50, ended=_dt(8, 2))
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (first, second),
        (_model_event("a-1", "model-1", "gpt-6-luna"),
         _model_event("a-2", "model-2", "gpt-6-luna")),
        session_id="session-a",
    ))
    storage.replace_agent_run_bundle(_bundle(
        "other", "run-other", (_turn("run-other", "secret", 0, _dt(8, 1), 999),),
        session_id="session-other",
    ))
    storage.close()

    report = build_agent_usage_bundle(
        path, tenant="local", now=_dt(8, 12), day="2026-10-08", session_id="session-a",
    )
    by_day = {row["day"]: row for row in report["days"]}
    assert by_day["2026-10-07"]["totalTokens"] == 100
    assert by_day["2026-10-08"]["totalTokens"] == 300
    assert by_day["2026-10-08"]["turns"] == 1
    assert by_day["2026-10-08"]["pricedTurns"] == 1
    assert report["selectedSession"]["totalTokens"] == 400
    assert report["selectedSession"]["turns"][0]["turnId"] == "a-2"
    assert report["selectedSession"]["topTurns"][0]["turnId"] == "a-2"
    assert report["daySources"][0]["sourceKind"] == "synthetic-codex"
    assert report["daySources"][0]["totalTokens"] == 300
    assert report["dayExplanation"]["day"] == "2026-10-08"
    assert report["dayExplanation"]["totalTokens"] == 300
    assert report["dayExplanation"]["comparison"] is None
    assert report["dayExplanation"]["topSessions"][0]["dayTokens"] == 300
    assert report["dayExplanation"]["topTurns"][0]["turnId"] == "a-2"
    assert "a-1" not in {turn["turnId"] for turn in report["dayExplanation"]["topTurns"]}
    assert "secret" not in str(report)
    assert "hidden-canary-example" not in str(report)

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/usage?day=2026-10-08&session_id=session-a")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["selectedSession"]["totalTokens"] == 400
    assert response.json()["dayExplanation"]["topSessions"][0]["dayTokens"] == 300


def test_day_explanation_compares_only_prior_active_utc_days(tmp_path):
    path = tmp_path / "day-explanation.db"
    storage = SQLiteStorage(str(path))
    for day, total in ((1, 100), (4, 200), (8, 300), (13, 900)):
        run_id = f"run-{day}"
        bundle = _bundle(
            "local", run_id, (_turn(run_id, f"turn-{day}", 0, _dt(day, 1), total),),
            session_id=f"session-{day}",
        )
        storage.replace_agent_run_bundle(replace(
            bundle, session=replace(bundle.session, source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest()),
        ))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(14, 1), day="2026-10-13")
    explanation = report["dayExplanation"]
    assert explanation["comparison"] == {
        "referenceDays": 3, "medianTokens": 200,
        "differenceTokens": 700, "differencePercent": 350,
    }
    assert explanation["topSessions"][0]["dayTokens"] == 900
    assert explanation["topTurns"][0]["totalTokens"] == 900


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_legacy_local_source_without_checkpoint_withholds_day_comparison(tmp_path, backend):
    with ExitStack() as stack:
        if backend == "postgres":
            from verdict.storage.postgres import PostgresStorage

            from packages.verdict.tests._postgres_test_safety import (
                isolated_test_dsn,
                validate_test_dsn,
            )

            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            storage_url = stack.enter_context(isolated_test_dsn(dsn))
            storage = PostgresStorage(storage_url)
        else:
            path = tmp_path / "legacy-local-day.db"
            storage_url = f"sqlite:///{path}"
            storage = SQLiteStorage(str(path))
        stack.callback(storage.close)
        for day, total in ((1, 100), (4, 200)):
            run_id = f"synthetic-run-{day}"
            bundle = _bundle("local", run_id, (_turn(run_id, f"turn-{day}", 0, _dt(day, 1), total),))
            storage.replace_agent_run_bundle(replace(
                bundle, session=replace(bundle.session,
                                        source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest()),
            ))
        run_id = "legacy-run-8-13"
        bundle = _bundle("local", run_id, (
            _turn(run_id, "turn-8", 0, _dt(8, 1), 300),
            _turn(run_id, "turn-13", 1, _dt(13, 1), 900),
        ), (AgentEvent(
            "recorded-failure", "turn-13", 0, _dt(13, 1), AgentEventType.TEST_RESULT,
            ExecutionStatus.FAILED, "test", {"failed": 1}, PrivacyClassification.METADATA,
        ), AgentEvent(
            "cross-date-failure", "turn-13", 1, _dt(12, 23), AgentEventType.TEST_RESULT,
            ExecutionStatus.FAILED, "test", {"failed": 1}, PrivacyClassification.METADATA,
        )))
        storage.replace_agent_run_bundle(replace(
            bundle, session=replace(bundle.session, source_kind="codex",
                                    source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest()),
        ))

        async def request(day):
            transport = httpx.ASGITransport(app=create_app(storage=storage_url, tenant_id="local"))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(f"/api/usage?day=2026-10-{day:02d}")

        selected = asyncio.run(request(13))
        assert selected.status_code == 200
        report = selected.json()
        assert report["dayExplanation"]["totalTokens"] == 900
        assert report["dayExplanation"]["sourcePartial"] is True
        assert report["dayExplanation"]["comparison"] is None
        assert report["selectedDayIssues"] == {
            "complete": False, "count": None, "observedCount": 1,
            "events": [{"code": "test_failed", "runId": run_id,
                        "eventId": "recorded-failure"}], "eventsTruncated": False,
            "sourcePartial": True,
        }
        assert next(item for item in report["days"] if item["day"] == "2026-10-13")["recordedIssueEvents"] == 1
        assert _context(report)["sourcePartial"] is True
        selected_session = build_agent_usage_bundle(
            storage_url, tenant="local", now=_dt(14, 12), day="2026-10-13",
            session_id=report["sessions"][0]["sessionId"], source_kind="codex",
        )["selectedSession"]
        assert selected_session["sourcePartial"] is True
        async def run_usage():
            transport = httpx.ASGITransport(app=create_app(storage=storage_url, tenant_id="local"))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(f"/api/runs?run_id={run_id}")

        run_response = asyncio.run(run_usage())
        assert run_response.status_code == 200
        assert run_response.json()["runs"][0]["sourceTokenUsage"]["state"] == "partial"
        preview = preview_day_explanation(
            storage_url, tenant="local",
            config={"day": "2026-10-13", "provider": "anthropic"},
        )
        assert preview["plannedCalls"] == 0
        earlier = asyncio.run(request(8))
        assert earlier.status_code == 200
        assert earlier.json()["dayExplanation"]["sourcePartial"] is True
        assert earlier.json()["selectedDayIssues"] == {
            "complete": False, "count": None, "observedCount": 0,
            "events": [], "eventsTruncated": False, "sourcePartial": True,
        }
        cross_date = asyncio.run(request(12))
        assert cross_date.status_code == 200
        assert cross_date.json()["selectedDayIssues"]["complete"] is False
        assert cross_date.json()["selectedDayIssues"]["observedCount"] == 1
        unrelated_recorded_day = asyncio.run(request(4))
        assert unrelated_recorded_day.status_code == 200
        assert unrelated_recorded_day.json()["dayExplanation"]["sourcePartial"] is True
        assert unrelated_recorded_day.json()["selectedDayIssues"]["complete"] is False
        storage.replace_agent_run_bundle(_bundle(
            "foreign", "foreign-complete", (_turn(
                "foreign-complete", "foreign-turn", 0, _dt(13, 1), 50,
            ),),
        ))

        async def foreign_request():
            transport = httpx.ASGITransport(app=create_app(
                storage=storage_url, tenant_id="foreign",
            ))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get("/api/usage?day=2026-10-13")

        foreign_report = asyncio.run(foreign_request())
        assert foreign_report.status_code == 200
        assert foreign_report.json()["selectedDayIssues"]["complete"] is True
        if backend == "postgres":
            import psycopg
            connection = psycopg.connect(storage_url)
        else:
            connection = sqlite3.connect(path)
        with connection:
            connection.execute(
                "UPDATE import_sources SET local_full_bytes=100,"
                "local_prefix_bytes=100,local_full_sha256=?,"
                "local_prefix_sha256=?,local_partial_line=FALSE "
                "WHERE tenant_id=? AND source_session_id=?"
                if backend == "sqlite" else
                "UPDATE import_sources SET local_full_bytes=100,"
                "local_prefix_bytes=100,local_full_sha256=%s,"
                "local_prefix_sha256=%s,local_partial_line=FALSE "
                "WHERE tenant_id=%s AND source_session_id=%s",
                ("a" * 64, "a" * 64, "local", "source-local-legacy-run-8-13"),
            )
        connection.close()
        old_checkpoint = asyncio.run(request(13))
        assert old_checkpoint.status_code == 200
        assert old_checkpoint.json()["selectedDayIssues"]["complete"] is False


def test_admitted_local_tail_preserves_mixed_source_token_day_coverage(tmp_path):
    root = tmp_path / "codex"
    root.mkdir()
    path = root / "oversized.jsonl"
    with path.open("w") as handle:
        handle.write(json.dumps({
            "timestamp": "2026-10-08T00:00:00Z", "type": "session_meta",
            "payload": {"id": "synthetic-oversized"},
        }) + "\n")
        for index in range(1_001):
            for event_type in ("task_started", "task_complete"):
                handle.write(json.dumps({
                    "timestamp": "2026-10-08T01:00:00Z", "type": "event_msg",
                    "payload": {"type": event_type, "turn_id": f"turn-{index}"},
                }) + "\n")
    db = tmp_path / "usage.db"
    storage = SQLiteStorage(str(db))
    try:
        storage.replace_agent_run_bundle(_bundle(
            "local", "measured", (_turn("measured", "measured-turn", 0, _dt(8, 1), 500),),
        ))
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert summary.stored == 1
    finally:
        storage.close()

    report = build_agent_usage_bundle(
        db, tenant="local", now=_dt(14, 12), day="2026-10-08",
    )
    selected = next(day for day in report["days"] if day["day"] == "2026-10-08")
    assert selected["totalTokens"] == 500
    assert selected["sourcePartial"] is False
    assert report["dayExplanation"]["sourcePartial"] is False
    assert report["dayExplanation"]["comparison"] is None


def test_admitted_tail_without_model_does_not_block_prior_token_comparison(tmp_path):
    root = tmp_path / "codex"
    root.mkdir()
    records = [{"timestamp": "2026-10-14T01:00:00Z", "type": "session_meta",
                "payload": {"id": "synthetic-tail-model-gap"}}]
    for index in range(1_001):
        records.extend({"timestamp": "2026-10-14T01:00:00Z", "type": "event_msg",
                        "payload": {"type": kind, "turn_id": f"turn-{index}"}}
                       for kind in ("task_started", "task_complete"))
    (root / "many-turns.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n"
    )
    db = tmp_path / "usage.db"
    storage = SQLiteStorage(str(db))
    try:
        for day, total in ((1, 100), (4, 200), (8, 300), (13, 900)):
            run_id = f"measured-{day}"
            bundle = _bundle("local", run_id,
                             (_turn(run_id, f"turn-{day}", 0, _dt(day, 1), total),))
            storage.replace_agent_run_bundle(replace(
                bundle, session=replace(bundle.session,
                                        source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest()),
            ))
        assert capture_local_agents(storage, tenant_id="local", codex_root=root).stored == 1
    finally:
        storage.close()
    report = build_agent_usage_bundle(db, tenant="local", now=_dt(14, 12),
                                      day="2026-10-13")
    assert report["window"]["modelEvidenceComplete"] is False
    assert report["dayExplanation"]["sourcePartial"] is False
    assert report["dayExplanation"]["comparison"]["differencePercent"] == 350


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_event_only_source_cap_preserves_complete_turn_token_comparison(tmp_path, backend):
    root = tmp_path / "codex"
    root.mkdir()
    history = root / "events.jsonl"
    records = [
        {"timestamp": "2026-09-01T01:00:00Z", "type": "session_meta",
         "payload": {"id": "synthetic-old-event-cap"}},
        {"timestamp": "2026-09-01T01:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1"}},
    ]
    records.extend({
        "timestamp": "2026-09-01T01:00:00Z", "type": "response_item",
        "payload": {"type": "function_call", "name": "read_file",
                    "call_id": f"call-{index}", "arguments": "{}"},
    } for index in range(1_501))
    records.append({"timestamp": "2026-09-01T02:00:00Z", "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-1"}})
    history.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    with ExitStack() as stack:
        if backend == "postgres":
            from verdict.storage.postgres import PostgresStorage

            from packages.verdict.tests._postgres_test_safety import (
                isolated_test_dsn,
                validate_test_dsn,
            )
            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            storage_url = stack.enter_context(isolated_test_dsn(dsn))
            storage = PostgresStorage(storage_url)
        else:
            path = tmp_path / "outside-window.db"
            storage_url = f"sqlite:///{path}"
            storage = SQLiteStorage(str(path))
        stack.callback(storage.close)
        for day, total in ((1, 100), (4, 200), (8, 300), (13, 900)):
            run_id = f"measured-{day}"
            bundle = _bundle(
                "local", run_id,
                (_turn(run_id, f"turn-{day}", 0, _dt(day, 1), total),),
            )
            storage.replace_agent_run_bundle(replace(
                bundle, session=replace(bundle.session,
                                        source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest()),
            ))
        captured = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert captured.stored == 1
        assert captured.event_limited_runs == 1
        async def request():
            transport = httpx.ASGITransport(app=create_app(storage=storage_url, tenant_id="local"))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get("/api/usage?day=2026-10-13")

        response = asyncio.run(request())
        assert response.status_code == 200
        report = response.json()
    assert report["dayExplanation"]["sourcePartial"] is False
    assert report["dayExplanation"]["comparison"]["differencePercent"] == 350
    assert next(day for day in report["days"] if day["day"] == "2026-10-13")[
        "issueSourcePartial"] is False
    assert report["selectedDayIssues"]["complete"] is True
    assert report["selectedDayIssues"]["count"] == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_out_of_window_omitted_turns_do_not_poison_current_token_day(tmp_path, backend):
    root = tmp_path / "codex"
    root.mkdir()
    history = root / "many-turns.jsonl"
    records = [{"timestamp": "2026-09-01T01:00:00Z", "type": "session_meta",
                "payload": {"id": "synthetic-old-turn-cap"}}]
    for index in range(1_001):
        records.extend({"timestamp": "2026-09-01T01:00:00Z", "type": "event_msg",
                        "payload": {"type": event_type, "turn_id": f"turn-{index}"}}
                       for event_type in ("task_started", "task_complete"))
    history.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    with ExitStack() as stack:
        if backend == "postgres":
            from verdict.storage.postgres import PostgresStorage

            from packages.verdict.tests._postgres_test_safety import (
                isolated_test_dsn,
                validate_test_dsn,
            )

            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            storage_url = stack.enter_context(isolated_test_dsn(dsn))
            storage = PostgresStorage(storage_url)
        else:
            path = tmp_path / "outside-window-turns.db"
            storage_url = f"sqlite:///{path}"
            storage = SQLiteStorage(str(path))
        stack.callback(storage.close)
        for day, total in ((1, 100), (4, 200), (8, 300), (13, 900)):
            run_id = f"measured-{day}"
            bundle = _bundle("local", run_id,
                             (_turn(run_id, f"turn-{day}", 0, _dt(day, 1), total),))
            storage.replace_agent_run_bundle(replace(
                bundle, session=replace(bundle.session,
                                        source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest()),
            ))
        captured = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert captured.stored == 1

        async def request():
            transport = httpx.ASGITransport(app=create_app(storage=storage_url, tenant_id="local"))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get("/api/usage?day=2026-10-13")

        response = asyncio.run(request())
        assert response.status_code == 200
        report = response.json()
        assert report["dayExplanation"]["sourcePartial"] is False
        assert report["dayExplanation"]["comparison"]["differencePercent"] == 350
        assert report["selectedDayIssues"]["complete"] is True


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_admitted_cross_date_event_keeps_complete_issue_total(tmp_path, backend):
    root = tmp_path / "codex"
    root.mkdir()
    history = root / "cross-date.jsonl"
    records = [
        {"timestamp": "2026-10-13T01:00:00Z", "type": "session_meta",
         "payload": {"id": "synthetic-cross-date"}},
        {"timestamp": "2026-10-13T01:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1"}},
    ]
    for index in range(1_501):
        records.append({
            "timestamp": "2026-10-12T23:00:00Z" if index == 1_500
            else "2026-10-13T01:00:00Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "read_file",
                        "call_id": f"call-{index}", "arguments": "{}"},
        })
    records.append({"timestamp": "2026-10-13T02:00:00Z", "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-1"}})
    history.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    with ExitStack() as stack:
        if backend == "postgres":
            from verdict.storage.postgres import PostgresStorage

            from packages.verdict.tests._postgres_test_safety import (
                isolated_test_dsn,
                validate_test_dsn,
            )
            dsn, reason = validate_test_dsn(
                os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
            )
            if dsn is None:
                pytest.skip(reason)
            storage_url = stack.enter_context(isolated_test_dsn(dsn))
            storage = PostgresStorage(storage_url)
        else:
            path = tmp_path / "cross-date.db"
            storage_url = f"sqlite:///{path}"
            storage = SQLiteStorage(str(path))
        stack.callback(storage.close)
        captured = capture_local_agents(storage, tenant_id="local", codex_root=root)
        assert captured.stored == 1
        assert captured.event_limited_runs == 1
        async def request():
            transport = httpx.ASGITransport(app=create_app(storage=storage_url, tenant_id="local"))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get("/api/usage?day=2026-10-12")

        response = asyncio.run(request())
        assert response.status_code == 200
        report = response.json()
        assert report["dayExplanation"]["sourcePartial"] is False
        assert next(day for day in report["days"] if day["day"] == "2026-10-12")[
            "issueSourcePartial"] is False
        assert report["selectedDayIssues"] == {
            "complete": True, "count": 0,
            "events": [], "eventsTruncated": False,
        }


def test_truncated_usage_window_skips_the_extra_local_coverage_scan(tmp_path, monkeypatch):
    path = tmp_path / "bounded-local-day.db"
    storage = SQLiteStorage(str(path))
    run_id = "many-turns"
    turns = tuple(_turn(run_id, f"turn-{index}", index, _dt(13, index + 1), 100)
                  for index in range(3))
    bundle = _bundle("local", run_id, turns)
    storage.replace_agent_run_bundle(replace(bundle, session=replace(bundle.session, source_kind="codex")))
    storage.close()
    monkeypatch.setattr(usage_module, "MAX_USAGE_TURNS", 2)
    extra_scans = []

    class ObservedSQLiteSession(SQLiteSession):
        def execute(self, query, params=()):
            if query.startswith("SELECT t.started_at FROM agent_turns t"):
                extra_scans.append(True)
            return super().execute(query, params)

    monkeypatch.setattr(dashboard_app, "_SQLiteSession", ObservedSQLiteSession)

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/usage?day=2026-10-13")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["window"]["complete"] is False
    assert response.json()["dayExplanation"]["comparison"] is None
    assert response.json()["dayExplanation"]["sourcePartial"] is True
    assert response.json()["selectedDayIssues"]["complete"] is False
    assert extra_scans == [], "a truncated window must not issue an unbounded second pass"


def test_postgres_usage_reads_source_coverage_in_the_same_snapshot(monkeypatch):
    from verdict.storage.postgres import PostgresStorage

    from packages.verdict.tests._postgres_test_safety import isolated_test_dsn, validate_test_dsn

    dsn, reason = validate_test_dsn(
        os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False,
    )
    if dsn is None:
        pytest.skip(reason)
    import psycopg

    with isolated_test_dsn(dsn) as storage_url:
        storage = PostgresStorage(storage_url)
        try:
            for day, total in ((1, 100), (4, 200), (8, 300), (13, 900)):
                run_id = f"snapshot-run-{day}"
                bundle = _bundle("local", run_id, (_turn(run_id, f"turn-{day}", 0, _dt(day, 1), total),))
                storage.replace_agent_run_bundle(replace(
                    bundle, session=replace(bundle.session,
                                            source_kind="codex" if day == 13 else "synthetic-codex",
                                            source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest()),
                ))
            with psycopg.connect(storage_url) as writer:
                writer.execute(
                    "UPDATE import_sources SET local_full_bytes=100,local_prefix_bytes=99,"
                    "local_full_sha256=%s,local_prefix_sha256=%s,local_partial_line=TRUE,"
                    "local_source_turn_count=1,local_v1_turn_count=1,"
                    "local_source_event_count=0,local_v1_event_count=0 "
                    "WHERE tenant_id='local' AND source_session_id='source-local-snapshot-run-13'",
                    ("a" * 64, "b" * 64),
                )
            moved = []

            class ObservedPostgresSession(PostgresSession):
                def execute(self, query, params=()):
                    if query.startswith("SELECT r.run_id,") and "FROM import_sources s" in query and not moved:
                        with psycopg.connect(storage_url) as writer:
                            writer.execute(
                                "UPDATE agent_turns SET total_tokens=1500 "
                                "WHERE tenant_id='local' AND run_id='snapshot-run-13'"
                            )
                            writer.execute(
                                "UPDATE import_sources SET local_full_bytes=101,local_prefix_bytes=101,"
                                "local_full_sha256=%s,local_prefix_sha256=%s,local_partial_line=FALSE "
                                "WHERE tenant_id='local' AND source_session_id='source-local-snapshot-run-13'",
                                ("c" * 64, "c" * 64),
                            )
                        moved.append(True)
                    return super().execute(query, params)

            monkeypatch.setattr(dashboard_app, "_PostgresSession", ObservedPostgresSession)

            async def request():
                transport = httpx.ASGITransport(app=create_app(storage=storage_url, tenant_id="local"))
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.get("/api/usage?day=2026-10-13")

            response = asyncio.run(request())
            assert response.status_code == 200
            report = response.json()
            assert moved == [True]
            assert report["dayExplanation"]["totalTokens"] == 900
            assert report["dayExplanation"]["sourcePartial"] is True
            assert report["dayExplanation"]["comparison"] is None
            later = asyncio.run(request()).json()
            assert later["dayExplanation"]["totalTokens"] == 1500
            assert later["dayExplanation"]["sourcePartial"] is False
            assert later["dayExplanation"]["comparison"] is not None
        finally:
            storage.close()


def test_day_explanation_withholds_comparison_for_unfinished_utc_day(tmp_path):
    path = tmp_path / "unfinished-day.db"
    storage = SQLiteStorage(str(path))
    for day, total in ((1, 100), (4, 200), (8, 300), (13, 900)):
        run_id = f"run-{day}"
        bundle = _bundle("local", run_id,
                         (_turn(run_id, f"turn-{day}", 0, _dt(day, 1), total),))
        storage.replace_agent_run_bundle(replace(
            bundle, session=replace(
                bundle.session,
                source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest(),
            ),
        ))
    storage.close()
    current = build_agent_usage_bundle(path, tenant="local", now=_dt(13, 12),
                                       day="2026-10-13")
    assert current["dayExplanation"]["totalTokens"] == 900
    assert current["dayExplanation"]["comparison"] is None
    ended = build_agent_usage_bundle(path, tenant="local", now=_dt(14, 1),
                                     day="2026-10-13")
    assert ended["dayExplanation"]["comparison"]["differencePercent"] == 350


@pytest.mark.parametrize("partial_day", [4, 13])
def test_day_explanation_withholds_change_when_a_compared_day_has_missing_turn_totals(
    tmp_path, partial_day,
):
    path = tmp_path / "partial-comparison.db"
    storage = SQLiteStorage(str(path))
    for day, total in ((1, 100), (4, 200), (8, 300), (13, 900)):
        run_id = f"run-{day}"
        turns = [_turn(run_id, f"turn-{day}", 0, _dt(day, 1), total)]
        if day == partial_day:
            turns.append(_turn(run_id, f"missing-{day}", 1, _dt(day, 2), None))
        bundle = _bundle("local", run_id, turns, session_id=f"session-{day}")
        storage.replace_agent_run_bundle(replace(
            bundle, session=replace(
                bundle.session,
                source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest(),
            ),
        ))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(14, 1), day="2026-10-13")
    assert report["dayExplanation"]["comparison"] is None
    selected = next(item for item in report["days"] if item["day"] == "2026-10-13")
    assert selected["totalTokens"] == 900
    assert selected["tokenKnownTurns"] == (1 if partial_day == 13 else selected["turns"])


def test_ambiguous_session_id_reports_source_choice_and_exact_source_selects(tmp_path):
    path = tmp_path / "ambiguous-session.db"
    storage = SQLiteStorage(str(path))
    codex = _bundle("local", "run-codex", (
        _turn("run-codex", "turn-codex", 0, _dt(8, 1), 100),
    ), session_id="shared-session")
    claude = _bundle("local", "run-claude", (
        _turn("run-claude", "turn-claude", 0, _dt(8, 2), 200),
    ), session_id="shared-session")
    storage.replace_agent_run_bundle(codex)
    storage.replace_agent_run_bundle(replace(
        claude, session=replace(claude.session, source_kind="synthetic-claude"),
    ))
    storage.close()

    async def request(source_kind=""):
        transport = httpx.ASGITransport(app=create_app(
            storage=f"sqlite:///{path}", tenant_id="local",
        ))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(
                "/api/usage?day=2026-10-08&session_id=shared-session" + source_kind,
            )

    ambiguous = asyncio.run(request())
    assert ambiguous.status_code == 200
    assert ambiguous.json()["selectedSession"] is None
    assert ambiguous.json()["selectionReason"] == "ambiguous_source"
    assert {item["sourceKind"] for item in ambiguous.json()["sessions"]} == {
        "synthetic-codex", "synthetic-claude",
    }
    context = _context(ambiguous.json())
    assert [item["session"] for item in context["topTurns"]] == ["Session 1", "Session 2"]
    selected = asyncio.run(request("&source_kind=synthetic-claude"))
    assert selected.status_code == 200
    assert selected.json()["selectedSession"]["sourceKind"] == "synthetic-claude"
    assert selected.json()["selectedSession"]["totalTokens"] == 200
    assert selected.json()["selectionReason"] is None


def test_session_report_combines_runs_and_isolates_source_and_tenant(tmp_path):
    path = tmp_path / "session-report.db"
    storage = SQLiteStorage(str(path))
    for run_id, total, source_kind, tenant in (
        ("run-one", 12, "synthetic-codex", "local"),
        ("run-two", 30, "synthetic-codex", "local"),
        ("other-source", 900, "synthetic-claude", "local"),
        ("other-tenant", 800, "synthetic-codex", "other"),
    ):
        turn = replace(_turn(run_id, f"{run_id}-turn", 0, _dt(8, 1), total),
                       user_request_redacted=f"task {run_id}",
                       final_response_redacted=f"result {run_id}",
                       request_state="present", response_state="present")
        bundle = _bundle(tenant, run_id, (turn,), session_id="shared")
        storage.replace_agent_run_bundle(replace(
            bundle, session=replace(bundle.session, source_kind=source_kind,
                                    source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest()),
        ))
    storage.close()
    report = build_agent_session_bundle(
        path, tenant="local", source_kind="synthetic-codex", session_id="shared",
    )
    assert report["coverage"] == "complete"
    assert report["runIds"] == ["run-one", "run-two"]
    assert report["usage"]["totalTokens"] == 42
    assert len(report["turns"]) == 2
    assert "other-source" not in str(report)
    assert "other-tenant" not in str(report)

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/session?source_kind=synthetic-codex&session_id=shared")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["runIds"] == report["runIds"]


def test_large_session_returns_bounded_partial_evidence_and_usage(tmp_path):
    path = tmp_path / "large-session.db"
    storage = SQLiteStorage(str(path))
    turn = _turn("run-a", "turn-a", 0, _dt(8, 1), 100,
                 input_tokens=80, output_tokens=20)
    events = tuple(AgentEvent(
        f"event-{index}", "turn-a", index, _dt(8, 1), AgentEventType.COMMAND,
        ExecutionStatus.COMPLETED, "test", {"command": "synthetic"},
        PrivacyClassification.REDACTED,
    ) for index in range(81))
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,), events, session_id="session-a",
    ))
    storage.close()
    report = build_agent_session_bundle(
        path, tenant="local", source_kind="synthetic-codex", session_id="session-a",
    )
    assert report["coverage"] == "partial"
    assert report["unavailableReason"] == "event_limit"
    assert report["eventCount"] == 81
    assert len(report["events"]) == 80
    assert len(report["turns"]) == 1
    assert report["usage"]["totalTokens"] == 100
    assert report["usage"]["listPriceUsd"] is None
    assert report["findings"] == []


def test_usage_missing_and_mixed_model_turns_are_not_priced_as_zero(tmp_path):
    path = tmp_path / "partial.db"
    storage = SQLiteStorage(str(path))
    known = _turn("run-a", "known", 0, _dt(8, 1), 10, input_tokens=8, output_tokens=2)
    unknown = _turn("run-a", "unknown", 1, _dt(8, 2), None)
    mixed = _turn("run-a", "mixed", 2, _dt(8, 3), 20, input_tokens=10, output_tokens=10)
    events = (
        _model_event("known", "m1", "gpt-6-luna"),
        _model_event("mixed", "m2", "gpt-6-luna"),
        _model_event("mixed", "m3", "gpt-6-sol", sequence=1),
    )
    storage.replace_agent_run_bundle(_bundle("local", "run-a", (known, unknown, mixed), events))
    storage.close()

    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    day = next(row for row in report["days"] if row["day"] == "2026-10-08")
    assert day["totalTokens"] == 30
    assert day["tokenKnownTurns"] == 2
    assert day["turns"] == 3
    assert day["pricedTurns"] == 1
    assert day["listPriceUsd"] is not None
    assert day["unattributedTokens"] == 20


def test_priced_components_without_reported_total_keep_model_tokens_unknown(tmp_path):
    path = tmp_path / "missing-total.db"
    storage = SQLiteStorage(str(path))
    turn = replace(_turn("run-a", "turn-a", 0, _dt(8, 1), 100,
                         input_tokens=80, output_tokens=20), total_tokens=None)
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,),
        (_model_event("turn-a", "model-a", "gpt-6-luna"),),
        session_id="session-a",
    ))
    storage.close()
    report = build_agent_usage_bundle(
        path, tenant="local", now=_dt(8, 12), day="2026-10-08",
        session_id="session-a", source_kind="synthetic-codex",
    )
    day = next(item for item in report["days"] if item["day"] == "2026-10-08")
    source = report["daySources"][0]
    model = report["dayModels"][0]
    session = report["selectedSession"]
    assert day["totalTokens"] is None
    assert source["totalTokens"] is None
    assert model["totalTokens"] is None
    assert session["totalTokens"] is None
    assert session["models"][0]["totalTokens"] is None
    assert day["listPriceUsd"] == source["listPriceUsd"] == model["listPriceUsd"] == session["listPriceUsd"]
    assert day["pricedTurns"] == source["pricedTurns"] == model["pricedTurns"] == 1


def test_model_event_scan_overflow_prices_only_fully_scanned_newer_turns(tmp_path, monkeypatch):
    path = tmp_path / "event-boundary.db"
    storage = SQLiteStorage(str(path))
    turns = tuple(_turn("run-a", f"turn-{index}", index, _dt(8, index + 1), 100,
                        input_tokens=80, output_tokens=20) for index in range(3))
    events = tuple(_model_event(turn.turn_id, f"model-{index}", "gpt-6-luna")
                   for index, turn in enumerate(turns))
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", turns, events, session_id="session-a",
    ))
    storage.close()
    monkeypatch.setattr(usage_module, "MAX_MODEL_EVENTS", 2)
    report = build_agent_usage_bundle(
        path, tenant="local", now=_dt(8, 12), day="2026-10-08",
        session_id="session-a", source_kind="synthetic-codex",
    )
    assert report["window"]["modelEvidenceComplete"] is False
    viewed = {item["turnId"]: item for item in report["selectedSession"]["turns"]}
    assert viewed["turn-2"]["listPriceUsd"] is not None
    assert viewed["turn-1"]["listPriceUsd"] is not None
    assert viewed["turn-0"]["listPriceUsd"] is None
    assert viewed["turn-2"]["modelEvidenceComplete"] is True
    assert viewed["turn-0"]["modelEvidenceComplete"] is False
    assert report["dayModels"][0]["pricedTurns"] == 2

    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", turns,
        (*events, _model_event("turn-1", "model-extra", "gpt-6-luna", sequence=1)),
        session_id="session-a",
    ))
    storage.close()
    boundary = build_agent_usage_bundle(
        path, tenant="local", now=_dt(8, 12), day="2026-10-08",
        session_id="session-a", source_kind="synthetic-codex",
    )
    viewed = {item["turnId"]: item for item in boundary["selectedSession"]["turns"]}
    assert viewed["turn-2"]["listPriceUsd"] is not None
    assert viewed["turn-1"]["listPriceUsd"] is None
    assert viewed["turn-1"]["modelEvidenceComplete"] is False


def test_usage_does_not_merge_equal_session_ids_from_different_sources(tmp_path):
    path = tmp_path / "source-session.db"
    storage = SQLiteStorage(str(path))
    local = _bundle("local", "run-a", (_turn("run-a", "a", 0, _dt(8, 1), 10),),
                    session_id="shared")
    other = _bundle("local", "run-b", (_turn("run-b", "b", 0, _dt(8, 2), 90),),
                    session_id="shared")
    storage.replace_agent_run_bundle(local)
    storage.replace_agent_run_bundle(replace(
        other, session=replace(other.session, source_kind="synthetic-claude"),
    ))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    assert len(report["sessions"]) == 2
    assert {item["sourceKind"]: item["totalTokens"] for item in report["sessions"]} == {
        "synthetic-codex": 10, "synthetic-claude": 90,
    }
    assert build_agent_usage_bundle(
        path, tenant="local", now=_dt(8, 12), day="2026-10-08", session_id="shared",
    )["selectedSession"] is None
    selected = build_agent_usage_bundle(
        path, tenant="local", now=_dt(8, 12), day="2026-10-08",
        session_id="shared", source_kind="synthetic-codex",
    )["selectedSession"]
    assert selected["totalTokens"] == 10


def test_usage_duplicate_model_evidence_does_not_duplicate_tokens_or_price(tmp_path):
    path = tmp_path / "duplicate.db"
    storage = SQLiteStorage(str(path))
    turn = _turn("run-a", "turn-a", 0, _dt(8, 1), 100,
                 input_tokens=80, output_tokens=20)
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,),
        (_model_event("turn-a", "event-a", "gpt-6-luna"),
         _model_event("turn-a", "event-b", "gpt-6-luna", sequence=1)),
    ))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    day = next(row for row in report["days"] if row["day"] == "2026-10-08")
    assert day["totalTokens"] == 100
    assert day["pricedTurns"] == 1
    assert report["dayModels"] == [{
        "model": "gpt-6-luna", "turns": 1, "tokenKnownTurns": 1,
        "totalTokens": 100, "pricedTurns": 1,
        "listPriceUsd": day["listPriceUsd"],
    }]


def test_run_report_prices_only_supported_models_and_links_evidence(tmp_path):
    path = tmp_path / "report.db"
    storage = SQLiteStorage(str(path))
    first = _turn("run-a", "a-1", 0, _dt(8, 1), 100,
                  input_tokens=80, output_tokens=20)
    second = _turn("run-a", "a-2", 1, _dt(8, 2), 300,
                   input_tokens=250, output_tokens=50)
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (first, second),
        (_model_event("a-1", "m1", "gpt-6-luna"),
         _model_event("a-2", "m2", "gpt-6-luna")),
    ))
    storage.close()

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs?run_id=run-a")

    response = asyncio.run(request())
    assert response.status_code == 200
    run = response.json()["runs"][0]
    assert run["sourceTokenUsage"]["pricedTurns"] == 2
    assert run["sourceTokenUsage"]["listPriceUsd"] is not None
    assert run["recommendations"] == []


def test_run_report_suggests_investigation_only_for_unresolved_recorded_failure(tmp_path):
    path = tmp_path / "failure-report.db"
    storage = SQLiteStorage(str(path))
    turns = (_turn("run-a", "a-1", 0, _dt(8, 1), None),)
    failure = AgentEvent(
        "failed-test", "a-1", 0, _dt(8, 1), AgentEventType.TEST_RESULT,
        ExecutionStatus.FAILED, "synthetic", {"failed": 1},
        PrivacyClassification.METADATA,
    )
    storage.replace_agent_run_bundle(_bundle("local", "run-a", turns, (failure,)))
    storage.close()

    async def read():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return (await client.get("/api/runs?run_id=run-a")).json()["runs"][0]

    report = asyncio.run(read())
    suggestion = next(item for item in report["recommendations"] if item["code"] == "review_last_failed_test")
    assert suggestion["status"] == "suggested"
    assert suggestion["evidenceEventIds"] == ["failed-test"]
    assert "rerun" in suggestion["action"].lower()

    storage = SQLiteStorage(str(path))
    passed = AgentEvent(
        "passed-test", "a-1", 1, _dt(8, 2), AgentEventType.TEST_RESULT,
        ExecutionStatus.COMPLETED, "synthetic", {"failed": 0},
        PrivacyClassification.METADATA,
    )
    storage.replace_agent_run_bundle(_bundle("local", "run-a", turns, (failure, passed)))
    storage.close()
    report = asyncio.run(read())
    assert not any(item["code"] == "review_last_failed_test" for item in report["recommendations"])


def test_token_advice_links_observed_growth_and_repeated_captured_result(tmp_path):
    path = tmp_path / "token-advice.db"
    storage = SQLiteStorage(str(path))
    turns = tuple(replace(
        _turn("run-a", f"turn-{index}", index, _dt(8, index + 1), inputs + 100,
              input_tokens=inputs, output_tokens=100),
        cached_input_tokens=cached,
    ) for index, (inputs, cached) in enumerate(((12000, 1000), (18000, 9000), (25000, 20000))))
    events = tuple(AgentEvent(
        f"result-{index}", f"turn-{index}", 0, _dt(8, index + 1),
        AgentEventType.TOOL_RESULT, ExecutionStatus.COMPLETED, "test",
        {"call_id": f"call-{index}", "tool_name": "read", "result": "A" * 2500},
        PrivacyClassification.REDACTED,
    ) for index in range(2))
    storage.replace_agent_run_bundle(_bundle("local", "run-a", turns, events))
    storage.close()

    async def read():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs?run_id=run-a")

    response = asyncio.run(read())
    assert response.status_code == 200
    recommendations = {item["code"]: item for item in response.json()["runs"][0]["recommendations"]}
    assert recommendations["review_input_growth"]["evidenceTurnIds"] == ["turn-0", "turn-2"]
    assert "cache" in recommendations["review_input_growth"]["tradeoff"].lower()
    assert recommendations["review_repeated_large_result"]["evidenceEventIds"] == ["result-0", "result-1"]
    assert all(item["status"] == "suggested" and "%" not in item["action"]
               for item in recommendations.values())


def test_token_advice_does_not_label_one_expensive_turn_or_small_repeated_output_as_waste():
    turns = (
        _turn("run-a", "first", 0, _dt(8, 1), 30000, input_tokens=29000, output_tokens=1000),
        _turn("run-a", "second", 1, _dt(8, 2), 20000, input_tokens=19000, output_tokens=1000),
    )
    events = tuple(AgentEvent(
        f"small-{index}", turn.turn_id, 0, _dt(8, index + 1),
        AgentEventType.TOOL_RESULT, ExecutionStatus.COMPLETED, "test",
        {"result": "same short output", "call_id": f"call-{index}"},
        PrivacyClassification.REDACTED,
    ) for index, turn in enumerate(turns))
    assert run_recommendations(_bundle("local", "run-a", turns, events)) == []


def test_usage_keeps_unknown_model_tokens_visible_without_a_price(tmp_path):
    path = tmp_path / "custom.db"
    storage = SQLiteStorage(str(path))
    turn = _turn("run-a", "turn-a", 0, _dt(8, 1), 50,
                 input_tokens=40, output_tokens=10)
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,),
        (_model_event("turn-a", "custom-model", "my-custom-model"),),
    ))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    assert report["dayModels"][0]["model"] == "my-custom-model"
    assert report["dayModels"][0]["totalTokens"] == 50
    assert report["dayModels"][0]["listPriceUsd"] is None


def test_unknown_model_call_with_known_configuration_withholds_whole_turn_price(tmp_path):
    path = tmp_path / "mixed-evidence.db"
    storage = SQLiteStorage(str(path))
    turn = _turn("run-a", "turn-a", 0, _dt(8, 1), 100,
                 input_tokens=80, output_tokens=20)
    missing_model = AgentEvent(
        "call-without-model", "turn-a", 1, _dt(8, 1), AgentEventType.MODEL_CALL,
        ExecutionStatus.COMPLETED, "test", {"request_model": None},
        PrivacyClassification.METADATA,
    )
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,),
        (_model_event("turn-a", "known-model", "gpt-6-luna"), missing_model),
    ))
    storage.close()

    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    day = next(row for row in report["days"] if row["day"] == "2026-10-08")
    assert day["totalTokens"] == 100
    assert day["pricedTurns"] == 0
    assert day["unattributedTokens"] == 100
    assert report["dayModels"] == []

    async def read():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            runs = await client.get("/api/runs?run_id=run-a")
            session = await client.get("/api/session?source_kind=synthetic-codex&session_id=source-local-run-a")
            return runs, session

    runs, session = asyncio.run(read())
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["sourceTokenUsage"]["pricedTurns"] == 0
    assert session.status_code == 200
    assert session.json()["turns"][0]["listPriceUsd"] is None


def test_model_evidence_attribution_cases_share_sql_and_bundle_rules(tmp_path):
    path = tmp_path / "attribution-cases.db"
    storage = SQLiteStorage(str(path))
    cases = {
        "known": ("gpt-6-luna",),
        "repeated": ("gpt-6-luna", "gpt-6-luna"),
        "two-models": ("gpt-6-luna", "gpt-6-sol"),
        "missing": ("gpt-6-luna", None),
        "redacted": ("gpt-6-luna", "<redacted>"),
        "unknown-only": (None,),
        "zero-notice": ("gpt-6-luna", None),
    }
    turns = tuple(_turn("run-a", name, index, _dt(8, index + 1), 100,
                        input_tokens=80, output_tokens=20)
                  for index, name in enumerate(cases))
    events = []
    for name, models in cases.items():
        for index, model in enumerate(models):
            events.append(AgentEvent(
                f"{name}-{index}", name, index, _dt(8, 1), AgentEventType.MODEL_CALL,
                ExecutionStatus.COMPLETED, "test", {
                    "request_model": model,
                    **({"input_tokens": 0, "output_tokens": 0}
                       if name == "zero-notice" and model is None else {}),
                },
                PrivacyClassification.METADATA,
            ))
    events.append(AgentEvent(
        "unrelated-context", "known", 1, _dt(8, 1), AgentEventType.CONTEXT,
        ExecutionStatus.COMPLETED, "test", {"name": "other"}, PrivacyClassification.METADATA,
    ))
    storage.replace_agent_run_bundle(_bundle("local", "run-a", turns, tuple(events)))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    day = next(row for row in report["days"] if row["day"] == "2026-10-08")
    assert day["totalTokens"] == 700
    assert day["pricedTurns"] == 3
    assert day["unattributedTokens"] == 400
    assert report["dayModels"][0]["model"] == "gpt-6-luna"
    assert report["dayModels"][0]["totalTokens"] == 300

    async def read():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs?run_id=run-a")

    response = asyncio.run(read())
    assert response.status_code == 200
    assert response.json()["runs"][0]["sourceTokenUsage"]["pricedTurns"] == 3


def test_usage_scan_limits_mark_partial_and_price_only_complete_newer_turns(tmp_path, monkeypatch):
    path = tmp_path / "bounded.db"
    storage = SQLiteStorage(str(path))
    turns = tuple(_turn("run-a", f"turn-{index}", index, _dt(8, index + 1), 10,
                        input_tokens=8, output_tokens=2) for index in range(3))
    events = tuple(_model_event(turn.turn_id, f"event-{index}", "gpt-6-luna")
                   for index, turn in enumerate(turns))
    storage.replace_agent_run_bundle(_bundle("local", "run-a", turns, events))
    storage.close()
    monkeypatch.setattr(usage_module, "MAX_USAGE_TURNS", 2)
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12))
    assert report["window"]["turnsComplete"] is False
    assert report["window"]["scannedTurns"] == 2
    monkeypatch.setattr(usage_module, "MAX_USAGE_TURNS", 20_000)
    monkeypatch.setattr(usage_module, "MAX_MODEL_EVENTS", 1)
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12))
    day = next(row for row in report["days"] if row["day"] == "2026-10-08")
    assert report["window"]["modelEvidenceComplete"] is False
    assert day["totalTokens"] == 30
    assert day["pricedTurns"] == 1
    assert day["listPriceUsd"] is not None


def test_usage_invalid_day_and_missing_session_do_not_expose_other_data(tmp_path):
    path = tmp_path / "invalid.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (_turn("run-a", "turn-a", 0, _dt(8, 1), 10),),
    ))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12),
                                      day="2026-10-08", session_id="other-session")
    assert report["selectedSession"] is None

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/usage?day=2026-02-30")

    assert asyncio.run(request()).status_code == 400


def test_usage_respects_dashboard_basic_auth(tmp_path, monkeypatch):
    path = tmp_path / "protected.db"
    canary = "synthetic-auth-boundary-canary"
    storage = SQLiteStorage(str(path))
    turn = replace(
        _turn("run-a", "turn-a", 0, _dt(8, 1), 10),
        user_request_redacted=canary,
        request_state="present",
    )
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,), session_id="x",
    ))
    storage.close()
    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")

    async def request():
        transport = httpx.ASGITransport(app=create_app(
            storage=f"sqlite:///{path}", tenant_id="local",
        ))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            routes = (
                "/api/usage",
                "/api/session?source_kind=synthetic-codex&session_id=x",
                "/api/experiments",
                "/api/scoped/scheduled",
                "/api/runs/run-a/turns/turn-a/analysis",
            )
            return [(
                route,
                await client.get(route),
                await client.get(route, auth=("reviewer", "secret")),
            ) for route in routes]

    responses = asyncio.run(request())
    for route, denied, allowed in responses:
        assert denied.status_code == 401, route
        assert canary not in denied.text, route
        assert allowed.status_code == 200, route
    assert canary in responses[1][2].text

    async def compare_request():
        transport = httpx.ASGITransport(app=create_app(
            storage=f"sqlite:///{path}", tenant_id="local",
        ))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            route = "/api/compare/conversations/matched"
            return (
                await client.post(route, json={}),
                await client.post(route, json={}, auth=("reviewer", "secret")),
            )

    denied, allowed = asyncio.run(compare_request())
    assert denied.status_code == 401
    assert canary not in denied.text
    assert allowed.status_code == 403  # This write path also needs the setup token.
