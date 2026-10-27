import asyncio
import json
import os
import sqlite3

import httpx
import pytest
import zstandard
from verdict.dashboard.app import create_app
from verdict.storage import SQLiteStorage


def _write_codex(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"timestamp": "2026-08-30T10:00:00Z", "type": "session_meta", "payload": {
            "id": "session", "originator": "Codex Desktop", "cli_version": "1.0"}},
        {"timestamp": "2026-08-30T10:01:00Z", "type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn"}},
        {"timestamp": "2026-08-30T10:01:01Z", "type": "event_msg", "payload": {
            "type": "user_message", "message": "SECRET_CANARY request"}},
        {"timestamp": "2026-08-30T10:01:02Z", "type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "turn", "last_agent_message": "done"}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in records))


def _write_codex_diagnostic(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE logs (
                   id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER,
                   target TEXT, feedback_log_body TEXT, process_uuid TEXT
               )"""
        )
        connection.execute(
            """INSERT INTO logs VALUES (
                   1, 1788000000, 0, 'codex_core::session::turn', ?, 'process-a'
               )""",
            (
                "run{thread.id=thread-a turn.id=turn-a model=gpt-5.6-sol}: "
                "post sampling token usage turn_id=turn-a "
                "secret=DIAGNOSTIC_BODY_CANARY",
            ),
        )


def test_setup_capture_reports_bounded_partial_source_and_exposes_captured_run(
    tmp_path, monkeypatch,
):
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    history = root / "session.jsonl"
    _write_codex(history)
    complete_bytes = history.stat().st_size
    with history.open("ab") as handle:
        handle.write(b"\n" * 256)
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_FILE_BYTES", complete_bytes + 16)
    database = tmp_path / "verdict.db"

    async def exercise():
        app = create_app(storage=f"sqlite:///{database}")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            preview = await client.post("/api/setup/preview", headers={"X-Verdict-Setup": token},
                                        json={"codexRoot": str(root)})
            started, job = await _capture_and_wait(
                client, {"X-Verdict-Setup": token}, {"codexRoot": str(root)},
            )
            runs = await client.get("/api/runs")
            attempts = await client.get("/api/setup/capture/attempts")
            return preview, started, job, runs, attempts

    preview, started, job, runs, attempts = asyncio.run(exercise())
    assert preview.status_code == 200 and started.status_code == 202
    assert job["state"] == "completed"
    assert job["summary"]["source_complete_files"] == 0
    assert job["summary"]["source_partial_files"] == 1
    assert job["summary"]["partial_reasons"] == {"bounded_source_prefix": 1}
    assert job["summary"]["skipped"] == 0
    assert runs.status_code == 200 and len(runs.json()["runs"]) == 1
    assert attempts.json()["partialFiles"] == 1
    assert attempts.json()["sourcePartialFiles"] == 1
    assert attempts.json()["partialReasons"] == {"bounded_source_prefix": 1}


def test_setup_capture_reports_over_verification_bound_as_skipped(tmp_path, monkeypatch):
    import verdict.telemetry.local_agents as local_agents

    root = tmp_path / "codex"
    history = root / "session.jsonl"
    _write_codex(history)
    monkeypatch.setattr(local_agents, "_MAX_LOCAL_VERIFY_BYTES", history.stat().st_size - 1)

    async def exercise():
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = await client.post("/api/setup/preview", headers=headers,
                                        json={"codexRoot": str(root)})
            started, job = await _capture_and_wait(client, headers, {"codexRoot": str(root)})
            attempts = await client.get("/api/setup/capture/attempts")
            runs = await client.get("/api/runs")
            return preview, started, job, attempts, runs

    preview, started, job, attempts, runs = asyncio.run(exercise())
    assert preview.status_code == 200 and started.status_code == 202
    assert job["state"] == "failed" and job["error"] == "capture_incomplete"
    assert job["summary"]["skip_reasons"] == {"history_file_too_large": 1}
    assert attempts.json()["failureReasons"] == {"history_file_too_large": 1}
    assert runs.json()["summary"]["available"] == 0


async def _capture_and_wait(client, headers, payload, *, timeout_s: float = 30.0):
    """POST a capture, then poll its status until the job reaches a terminal state."""
    import time as _time

    started = await client.post("/api/setup/capture", headers=headers, json=payload)
    if started.status_code != 202:
        return started, None
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        status = await client.get("/api/setup/capture/status")
        job = status.json()["job"]
        if job["state"] in {"completed", "failed"}:
            return started, job
        await asyncio.sleep(0.02)
    raise AssertionError("capture job did not finish")


def test_setup_preview_and_background_capture_include_compressed_codex(tmp_path):
    root = tmp_path / "codex"
    plain = root / "session.jsonl"
    _write_codex(plain)
    plain.with_suffix(".jsonl.zst").write_bytes(
        zstandard.ZstdCompressor().compress(plain.read_bytes())
    )
    plain.unlink()

    async def exercise():
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = await client.post(
                "/api/setup/preview", headers=headers, json={"codexRoot": str(root)},
            )
            started, job = await _capture_and_wait(
                client, headers, {"codexRoot": str(root)},
            )
            runs = await client.get("/api/runs")
            return preview, started, job, runs

    preview, started, job, runs = asyncio.run(exercise())
    assert preview.json()["codex"]["files"] == 1
    assert started.status_code == 202
    assert job["state"] == "completed" and job["filesTotal"] == job["filesDone"] == 1
    assert runs.status_code == 200 and len(runs.json()["runs"]) == 1


def test_setup_preview_then_approved_local_capture(tmp_path):
    codex = tmp_path / "codex" / "sessions"
    claude = tmp_path / "claude"
    claude.mkdir()
    _write_codex(codex / "session.jsonl")
    _write_codex_diagnostic(codex.parent / "logs_2.sqlite")
    database = tmp_path / "verdict.db"

    async def setup():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            config = await client.get("/api/setup/token")
            token = config.json()["setupToken"]
            preview = await client.post(
                "/api/setup/preview", headers={"X-Verdict-Setup": token},
                json={"claudeRoot": str(claude), "codexRoot": str(codex)},
            )
            before = await client.get("/api/setup/capture/status")
            started, job = await _capture_and_wait(
                client, {"X-Verdict-Setup": token},
                {"claudeRoot": str(claude), "codexRoot": str(codex)},
            )
            rejected = await client.post(
                "/api/setup/capture", headers={"X-Verdict-Setup": "wrong"}, json={},
            )
            status = await client.get("/api/setup/capture/status")
            dashboard = await client.get("/api/data")
            runs = await client.get("/api/runs?limit=1")
            return preview, before, started, job, rejected, status, dashboard, runs

    preview, before, started, job, rejected, status, dashboard, runs = asyncio.run(setup())
    assert before.json() == {"job": None}
    assert started.status_code == 202
    assert started.json()["job"]["state"] in {"running", "analyzing", "completed"}
    assert job["state"] == "completed" and job["error"] is None
    assert job["filesDone"] == job["filesTotal"] == 1
    assert job["finishedAt"] is not None and job["analysis"]["status"] == "completed"
    capture = status  # the terminal job is readable until the next capture starts
    assert preview.status_code == 200
    assert preview.json()["codex"]["files"] == 1
    assert preview.json()["codex"]["modelCallDiagnostics"] == {
        "path": str(codex.parent / "logs_2.sqlite"),
        "exists": True,
    }
    assert capture.status_code == 200
    assert capture.json()["job"]["summary"]["stored"] == 1
    assert capture.json()["job"]["summary"]["codex_model_calls"]["stored"] == 1
    assert "SECRET_CANARY" not in capture.text
    assert "DIAGNOSTIC_BODY_CANARY" not in capture.text
    assert rejected.status_code == 403
    assert dashboard.json()["meta"]["totalTraces"] == 1
    assert dashboard.json()["meta"]["totalAgentRuns"] == 1
    assert dashboard.json()["meta"]["agentRunSources"] == [
        {"sourceKind": "codex", "runs": 1}
    ]
    assert dashboard.json()["meta"]["agentRunSourcesTruncated"] is False
    assert dashboard.json()["meta"]["lastAgentCaptureAt"]
    assert runs.json()["summary"]["available"] == 1
    storage = SQLiteStorage(str(database))
    [bundle] = storage.list_agent_run_bundles("__verdict_local__")
    storage.close()
    assert bundle.turns[0].request_state.value == "present"
    assert bundle.turns[0].user_request_redacted == "SECRET_CANARY request"


def test_setup_capture_attempts_all_discovered_files_in_one_approval(tmp_path, monkeypatch):
    import verdict.telemetry.local_agents as local_agents

    codex = tmp_path / "codex" / "sessions"
    _write_codex(codex / "first.jsonl")
    second = (codex / "first.jsonl").read_text().replace('"id": "session"', '"id": "second-session"')
    (codex / "second.jsonl").write_text(second)
    # The old aggregate budgets stopped after one file. They are not allowed
    # to turn one approval into a permanently deferred backlog.
    monkeypatch.setattr(local_agents, "_MAX_SCAN_FILES", 1, raising=False)
    monkeypatch.setattr(local_agents, "_MAX_SCAN_RAW_BYTES", 1, raising=False)

    async def run():
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = await client.post(
                "/api/setup/preview", headers=headers, json={"codexRoot": str(codex)},
            )
            started, job = await _capture_and_wait(
                client, headers, {"codexRoot": str(codex)},
            )
            return preview, started, job

    preview, started, job = asyncio.run(run())
    assert preview.status_code == 200
    assert started.status_code == 202
    assert job["summary"]["files"] == job["summary"]["stored"] == 2
    assert job["filesDone"] == job["filesTotal"] == 2
    assert "deferred" not in job["summary"]
    assert job["analysis"]["status"] == "completed"
    assert job["state"] == "completed"
    assert job["error"] is None


def test_setup_attempt_status_survives_restart_without_source_paths(tmp_path):
    codex = tmp_path / "codex" / "sessions"
    _write_codex(codex / "good.jsonl")
    (codex / "bad.jsonl").write_text("{PRIVATE_PATH_CANARY}\n")
    database = tmp_path / "verdict.db"

    async def run():
        app = create_app(storage=f"sqlite:///{database}")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            await client.post("/api/setup/preview", headers=headers,
                              json={"codexRoot": str(codex)})
            _, job = await _capture_and_wait(client, headers,
                                             {"codexRoot": str(codex)})
            assert job["state"] == "failed"
        restarted = create_app(storage=f"sqlite:///{database}")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://testserver"
        ) as client:
            own = await client.get("/api/setup/capture/attempts")
            resumed_status = await client.get("/api/setup/capture/status")
        foreign = create_app(storage=f"sqlite:///{database}", tenant_id="foreign")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=foreign), base_url="http://testserver"
        ) as client:
            other = await client.get("/api/setup/capture/attempts")
        return own, resumed_status, other

    own, resumed_status, other = asyncio.run(run())
    assert own.status_code == 200
    assert own.json()["trackedFiles"] == 2
    assert own.json()["failedFiles"] == 1
    assert own.json()["failureReasons"] == {"malformed_jsonl": 1}
    assert resumed_status.json()["job"] is None
    assert resumed_status.json()["attempts"] == own.json()
    assert "PRIVATE_PATH_CANARY" not in own.text and str(codex) not in own.text
    assert other.status_code == 200 and other.json()["trackedFiles"] == 0


def test_setup_attempt_status_reads_disposable_postgres_without_cross_tenant_rows():
    from verdict.storage import PostgresStorage

    from packages.verdict.tests._postgres_test_safety import (
        isolated_test_dsn,
        validate_test_dsn,
    )

    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        storage = PostgresStorage(isolated)
        try:
            generation = storage.claim_local_scan_attempt(
                "local", "codex", "a" * 64, "b" * 64,
                "2026-10-20T10:00:00+00:00", 100, 1,
            )
            assert storage.finish_local_scan_attempt(
                "local", "codex", "a" * 64, "b" * 64, generation,
                "2026-10-20T10:00:01+00:00", "failed_malformed_jsonl",
                None, False, False,
            )
        finally:
            storage.close()

        async def read(tenant):
            app = create_app(storage=isolated, tenant_id=tenant)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.get("/api/setup/capture/attempts")

        own = asyncio.run(read("local"))
        other = asyncio.run(read("foreign"))
        assert own.status_code == 200
        assert own.json()["failureReasons"] == {"malformed_jsonl": 1}
        assert own.json()["failedFiles"] == own.json()["trackedFiles"] == 1
        assert other.status_code == 200 and other.json()["trackedFiles"] == 0


def test_setup_capture_reports_sessions_with_bounded_event_detail(tmp_path, monkeypatch):
    import verdict.dashboard.setup_routes as setup_routes

    codex = tmp_path / "codex" / "sessions"
    path = codex / "long.jsonl"
    _write_codex(path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[3:3] = [
        {"timestamp": "2026-08-30T10:01:02Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "synthetic_tool", "call_id": f"call-{index}",
            "arguments": "{}",
        }}
        for index in range(1_600)
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))

    async def run():
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = await client.post(
                "/api/setup/preview", headers=headers,
                json={"codexRoot": str(codex)},
            )
            started, job = await _capture_and_wait(
                client, headers, {"codexRoot": str(codex), "captureContent": False},
            )
            real_capture = setup_routes.capture_local_agents

            def failed_capture(storage, **kwargs):
                raise OSError("synthetic rescan failure")

            monkeypatch.setattr(setup_routes, "capture_local_agents", failed_capture)
            await client.post("/api/setup/preview", headers=headers, json={"codexRoot": str(codex)})
            failed_start, failed_job = await _capture_and_wait(
                client, headers, {"codexRoot": str(codex), "captureContent": False},
            )
            after_failure = (await client.get("/api/setup/capture/status")).json()

            monkeypatch.setattr(setup_routes, "capture_local_agents", real_capture)
            clean_codex = tmp_path / "clean" / "sessions"
            _write_codex(clean_codex / "short.jsonl")
            await client.post("/api/setup/preview", headers=headers, json={"codexRoot": str(clean_codex)})
            clean_start, clean_job = await _capture_and_wait(
                client, headers, {"codexRoot": str(clean_codex), "captureContent": False},
            )
            after_clean = (await client.get("/api/setup/capture/status")).json()
            monkeypatch.setattr(
                setup_routes.SetupRoutes, "_run_analysis",
                lambda self: {"analysisState": {"status": "error", "error": "synthetic"}},
            )
            await client.post("/api/setup/preview", headers=headers, json={"codexRoot": str(codex)})
            analysis_start, analysis_job = await _capture_and_wait(
                client, headers, {"codexRoot": str(codex), "captureContent": False},
            )
            after_analysis_failure = (await client.get("/api/setup/capture/status")).json()
            monkeypatch.setattr(setup_routes, "capture_local_agents", failed_capture)
            await client.post("/api/setup/preview", headers=headers, json={"codexRoot": str(codex)})
            later_failed_start, later_failed_job = await _capture_and_wait(
                client, headers, {"codexRoot": str(codex), "captureContent": False},
            )
            after_later_failure = (await client.get("/api/setup/capture/status")).json()
            return (preview, started, job, failed_start, failed_job, after_failure,
                    clean_start, clean_job, after_clean, analysis_start, analysis_job,
                    after_analysis_failure, later_failed_start, later_failed_job, after_later_failure)

    (preview, started, job, failed_start, failed_job, after_failure,
     clean_start, clean_job, after_clean, analysis_start, analysis_job,
     after_analysis_failure, later_failed_start, later_failed_job,
     after_later_failure) = asyncio.run(run())
    assert preview.status_code == 200 and started.status_code == 202
    assert job["state"] == "completed"
    assert job["summary"]["stored"] == 1
    assert job["summary"]["event_limited_runs"] == 1
    assert failed_start.status_code == 202 and failed_job["state"] == "failed"
    assert after_failure["job"]["jobId"] == failed_job["jobId"]
    assert after_failure["lastImportedSummary"]["event_limited_runs"] == 1
    assert clean_start.status_code == 202 and clean_job["state"] == "completed"
    assert after_clean["lastImportedSummary"] == clean_job["summary"]
    assert "event_limited_runs" not in after_clean["lastImportedSummary"]
    assert analysis_start.status_code == 202 and analysis_job["state"] == "failed"
    assert analysis_job["error"] == "analysis_failed"
    assert after_analysis_failure["job"]["summary"]["event_limited_runs"] == 1
    assert after_analysis_failure["lastImportedSummary"]["event_limited_runs"] == 1
    assert later_failed_start.status_code == 202 and later_failed_job["state"] == "failed"
    assert after_later_failure["job"]["summary"] is None
    assert after_later_failure["lastImportedSummary"]["event_limited_runs"] == 1


def test_setup_capture_can_explicitly_disable_content(tmp_path):
    codex = tmp_path / "codex"
    _write_codex(codex / "session.jsonl")

    async def setup():
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            await client.post(
                "/api/setup/preview",
                headers={"X-Verdict-Setup": token},
                json={"codexRoot": str(codex)},
            )
            started, job = await _capture_and_wait(
                client, {"X-Verdict-Setup": token},
                {"codexRoot": str(codex), "captureContent": False},
            )
            return started, job

    response, job = asyncio.run(setup())
    assert response.status_code == 202 and job["state"] == "completed"
    [bundle] = SQLiteStorage(str(tmp_path / "verdict.db")).list_agent_run_bundles(
        "__verdict_local__"
    )
    assert bundle.turns[0].request_state.value == "not_captured"
    assert bundle.turns[0].user_request_redacted is None


def test_setup_rejects_unapproved_source_and_unbounded_payload(tmp_path):
    database = tmp_path / "verdict.db"

    async def setup():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            missing_approval = await client.post(
                "/api/setup/capture", headers={"X-Verdict-Setup": token}, json={},
            )
            oversized = await client.post(
                "/api/setup/preview", headers={"X-Verdict-Setup": token},
                json={"codexRoot": "x" * 5000},
            )
            return missing_approval, oversized

    missing_approval, oversized = asyncio.run(setup())
    assert missing_approval.status_code == 400
    assert oversized.status_code == 400


def test_setup_preview_reports_limit_only_when_more_files_exist(tmp_path, monkeypatch):
    from verdict.dashboard import setup_routes

    monkeypatch.setattr(setup_routes, "_MAX_FILES_PREVIEW", 2)
    source = tmp_path / "codex"
    for index in range(3):
        _write_codex(source / str(index) / "session.jsonl")

    async def setup():
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/setup/preview", headers={"X-Verdict-Setup": token},
                json={"codexRoot": str(source)},
            )

    response = asyncio.run(setup())
    assert response.status_code == 200
    assert response.json()["codex"]["files"] == 2
    assert response.json()["codex"]["fileLimitReached"] is True


@pytest.mark.parametrize("with_recorded_end", [False, True])
def test_setup_uses_canonical_historical_file_import(tmp_path, with_recorded_end):
    database = tmp_path / "verdict.db"
    export = tmp_path / "voice.json"
    assistant_turn = {"role": "assistant", "content": "done", "status": "completed",
                      "timestamp": "2026-07-01T00:00:01Z"}
    if with_recorded_end:
        assistant_turn["ended_at"] = "2026-07-01T00:00:02Z"
    export.write_text(json.dumps({
        "conversation_id": "conversation",
        "turns": [
            {"role": "user", "content": "help", "timestamp": "2026-07-01T00:00:00Z"},
            assistant_turn,
        ],
    }))

    async def setup():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            await client.post(
                "/api/setup/import/preview", headers={"X-Verdict-Setup": token},
                json={"path": str(export), "format": "voice"},
            )
            imported = await client.post(
                "/api/setup/import", headers={"X-Verdict-Setup": token},
                json={"path": str(export), "format": "voice"},
            )
            evaluator_preview = await client.post(
                "/api/evaluators/preview",
                headers={"X-Verdict-Setup": token},
                json={
                    "provider": "anthropic", "model": "claude-haiku-4-5",
                    "maxCalls": "all", "maxOutputTokens": 256,
                    "rubric": {
                        "name": "poc", "version": "1",
                        "dimensions": [{
                            "name": "relevance",
                            "description": "Directly answers the request.",
                        }],
                    },
                },
            )
            dashboard = await client.get("/api/data")
            return imported, evaluator_preview, dashboard

    response, evaluator_preview, dashboard = asyncio.run(setup())
    assert response.status_code == 200
    assert response.json()["summary"]["stored"] == 1
    assert dashboard.json()["meta"]["totalTraces"] == 1
    assert dashboard.json()["meta"]["totalAgentRuns"] == 0
    assert evaluator_preview.status_code == 200
    assert evaluator_preview.json()["availableTraces"] == 1
    assert evaluator_preview.json()["eligible"] == int(with_recorded_end)
    assert evaluator_preview.json()["plannedCalls"] == int(with_recorded_end)
    storage = SQLiteStorage(str(database))
    traces = storage.list_traces(limit=10)
    assert len(traces) == 1
    assert traces[0].tenant_id == "__verdict_local__"
    storage.close()


def test_configured_tenant_owns_setup_import_evaluator_and_dashboard_reads(tmp_path):
    database = tmp_path / "configured-tenant.db"
    export = tmp_path / "voice.json"
    export.write_text(json.dumps({
        "conversation_id": "conversation",
        "turns": [
            {"role": "user", "content": "help", "timestamp": "2026-07-01T00:00:00Z"},
            {"role": "assistant", "content": "done", "status": "completed",
             "timestamp": "2026-07-01T00:00:01Z"},
        ],
    }))

    async def setup():
        app = create_app(storage=f"sqlite:///{database}", tenant_id="customer-a")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            config = await client.get("/api/config")
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            await client.post(
                "/api/setup/import/preview", headers=headers,
                json={"path": str(export), "format": "voice"},
            )
            imported = await client.post(
                "/api/setup/import", headers=headers,
                json={"path": str(export), "format": "voice"},
            )
            evaluator = await client.post(
                "/api/evaluators/preview",
                headers=headers,
                json={
                    "provider": "anthropic", "model": "claude-haiku-4-5",
                    "maxCalls": "all", "maxOutputTokens": 256,
                    "rubric": {
                        "name": "poc", "version": "1",
                        "dimensions": [{
                            "name": "relevance",
                            "description": "Directly answers the request.",
                        }],
                    },
                },
            )
            dashboard = await client.get("/api/data")
            return config, imported, evaluator, dashboard

    config, imported, evaluator, dashboard = asyncio.run(setup())

    assert config.json()["tenantId"] == "customer-a"
    assert imported.status_code == 200
    assert evaluator.status_code == 200
    assert evaluator.json()["availableTraces"] == 1
    assert dashboard.json()["meta"]["totalTraces"] == 1
    storage = SQLiteStorage(str(database))
    [trace] = storage.list_traces(tenant_id="customer-a", limit=10)
    assert trace.tenant_id == "customer-a"
    assert storage.list_traces(tenant_id="__verdict_local__", limit=10) == []
    storage.close()


def test_evaluator_preview_explains_when_every_dimension_requires_context(tmp_path):
    async def preview(unit):
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/evaluators/preview",
                headers={"X-Verdict-Setup": token},
                json={
                    "unit": unit,
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5",
                    "maxCalls": "all",
                    "maxOutputTokens": 256,
                    "rubric": {
                        "name": "groundedness",
                        "version": "1",
                        "dimensions": [
                            {
                                "name": "groundedness",
                                "description": "Supported by retrieved context.",
                                "requiresContext": True,
                            }
                        ],
                    },
                },
            )

    for unit, explanation in (
        ("trace", "Verdict traces do not include retrieved context."),
        ("agent_turn", "Verdict does not have retrieved context for this evaluation unit."),
    ):
        response = asyncio.run(preview(unit))
        assert response.status_code == 400
        assert response.json() == {
            "error": "No rubric dimensions can be evaluated because " + explanation
        }


def test_setup_imports_a_bounded_historical_directory(tmp_path):
    database = tmp_path / "verdict.db"
    exports = tmp_path / "exports"
    exports.mkdir()
    for index in range(2):
        (exports / f"voice-{index}.json").write_text(json.dumps({
            "conversation_id": f"conversation-{index}",
            "turns": [
                {"role": "user", "content": "help", "timestamp": "2026-07-01T00:00:00Z"},
                {"role": "assistant", "content": "done", "status": "completed",
                 "timestamp": "2026-07-01T00:00:01Z"},
            ],
        }))

    async def setup():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{database}"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            await client.post(
                "/api/setup/import/preview", headers={"X-Verdict-Setup": token},
                json={"path": str(exports), "format": "voice"},
            )
            return await client.post(
                "/api/setup/import", headers={"X-Verdict-Setup": token},
                json={"path": str(exports), "format": "voice"},
            )

    response = asyncio.run(setup())
    assert response.status_code == 200
    assert response.json()["summary"] == {
        "files": 2, "seen": 2, "stored": 2, "skipped": 0, "skipReasons": {},
    }
    storage = SQLiteStorage(str(database))
    assert len(storage.list_traces(limit=10)) == 2
    storage.close()


def test_dashboard_rejects_untrusted_host_before_exposing_setup_token(tmp_path):
    async def request():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://attacker.example"
        ) as client:
            return await client.get("/api/setup/token")

    response = asyncio.run(request())
    assert response.status_code == 400
    assert "setupToken" not in response.text


def test_setup_capture_requires_preview_of_the_exact_paths(tmp_path):
    codex = tmp_path / "codex"
    _write_codex(codex / "session.jsonl")

    async def request():
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            without_preview = await client.post(
                "/api/setup/capture", headers=headers,
                json={"codexRoot": str(codex)},
            )
            await client.post(
                "/api/setup/preview", headers=headers,
                json={"codexRoot": str(codex)},
            )
            changed_path = await client.post(
                "/api/setup/capture", headers=headers,
                json={"claudeRoot": str(codex)},
            )
            return without_preview, changed_path

    without_preview, changed_path = asyncio.run(request())
    assert without_preview.status_code == 409
    assert changed_path.status_code == 409


def test_capture_thread_start_failure_preserves_approval_and_allows_retry(tmp_path, monkeypatch):
    import threading

    import verdict.dashboard.setup_routes as setup_routes

    codex = tmp_path / "codex"
    _write_codex(codex / "session.jsonl")
    real_start = threading.Thread.start

    def cannot_start(self):
        raise RuntimeError(f"thread failed near {tmp_path}/private")

    async def run():
        app = create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            payload = {"codexRoot": str(codex)}
            await client.post("/api/setup/preview", headers=headers, json=payload)
            monkeypatch.setattr(setup_routes.threading.Thread, "start", cannot_start)
            failed = await client.post("/api/setup/capture", headers=headers, json=payload)
            status = await client.get("/api/setup/capture/status")
            monkeypatch.setattr(setup_routes.threading.Thread, "start", real_start)
            retry, job = await _capture_and_wait(client, headers, payload)
            return failed, status, retry, job

    failed, status, retry, job = asyncio.run(run())
    assert failed.status_code == 503
    assert "private" not in failed.text and str(tmp_path) not in failed.text
    assert status.json()["job"] is None
    assert retry.status_code == 202
    assert job["state"] == "completed"


def test_capture_status_reports_progress_failure_and_single_flight(tmp_path, monkeypatch):
    import threading

    import verdict.dashboard.setup_routes as setup_routes

    codex = tmp_path / "codex" / "sessions"
    _write_codex(codex / "session.jsonl")
    database = tmp_path / "verdict.db"
    release = threading.Event()
    seen_progress: list[tuple[int, int]] = []
    real_capture = setup_routes.capture_local_agents

    def slow_capture(storage, **kwargs):
        progress = kwargs.get("progress")

        def record(done, total):
            seen_progress.append((done, total))
            progress(done, total)

        release.wait(timeout=10)
        return real_capture(storage, **{**kwargs, "progress": record})

    monkeypatch.setattr(setup_routes, "capture_local_agents", slow_capture)

    async def run():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            await client.post("/api/setup/preview", headers=headers, json={"codexRoot": str(codex)})
            first = await client.post("/api/setup/capture", headers=headers, json={"codexRoot": str(codex)})
            running = await client.get("/api/setup/capture/status")
            # A second start while the job runs is refused, and so is a start
            # after a fresh preview, because one capture runs at a time.
            await client.post("/api/setup/preview", headers=headers, json={"codexRoot": str(codex)})
            second = await client.post("/api/setup/capture", headers=headers, json={"codexRoot": str(codex)})
            release.set()
            deadline = 200
            while deadline:
                job = (await client.get("/api/setup/capture/status")).json()["job"]
                if job["state"] in {"completed", "failed"}:
                    break
                deadline -= 1
                await asyncio.sleep(0.02)
            return first, running, second, job

    first, running, second, job = asyncio.run(run())
    assert first.status_code == 202
    assert running.json()["job"]["state"] == "running"
    assert second.status_code == 409 and "already running" in second.json()["error"]
    assert job["state"] == "completed"
    assert seen_progress[0] == (0, 1) and seen_progress[-1] == (1, 1)
    assert job["filesDone"] == 1 and job["filesTotal"] == 1

    # A capture that raises ends in a failed state with a category, not a message.
    def broken_capture(storage, **kwargs):
        raise OSError(f"disk error at {tmp_path}/secret-path")

    monkeypatch.setattr(setup_routes, "capture_local_agents", broken_capture)

    async def fail():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            await client.post("/api/setup/preview", headers=headers, json={"codexRoot": str(codex)})
            started = await client.post("/api/setup/capture", headers=headers, json={"codexRoot": str(codex)})
            for _ in range(200):
                status = await client.get("/api/setup/capture/status")
                if status.json()["job"]["state"] in {"completed", "failed"}:
                    return started, status
                await asyncio.sleep(0.02)
            raise AssertionError("job did not finish")

    started, status = asyncio.run(fail())
    assert started.status_code == 202
    assert status.json()["job"]["state"] == "failed"
    assert status.json()["job"]["error"] == "capture_failed"
    assert "secret-path" not in status.text and str(tmp_path) not in status.text


def test_setup_capture_cancel_preserves_committed_files_and_allows_retry(tmp_path, monkeypatch):
    import threading

    import verdict.telemetry.local_agents as local_agents

    codex = tmp_path / "codex" / "sessions"
    for name in ("a", "b"):
        path = codex / f"{name}.jsonl"
        _write_codex(path)
        path.write_text(path.read_text().replace('"id": "session"', f'"id": "session-{name}"'))
    database = tmp_path / "verdict.db"
    entered_second = threading.Event()
    release_second = threading.Event()
    real_parser = local_agents._parse_codex
    parsed = 0

    def pause_second(path, *, home):
        nonlocal parsed
        parsed += 1
        if parsed == 2:
            entered_second.set()
            assert release_second.wait(10)
        return real_parser(path, home=home)

    monkeypatch.setattr(local_agents, "_parse_codex", pause_second)

    async def run():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            payload = {"codexRoot": str(codex)}
            await client.post("/api/setup/preview", headers=headers, json=payload)
            started = await client.post("/api/setup/capture", headers=headers, json=payload)
            assert started.status_code == 202
            assert await asyncio.to_thread(entered_second.wait, 10)
            job_id = started.json()["job"]["jobId"]
            denied = await client.post(f"/api/setup/capture/{job_id}/cancel")
            requested = await client.post(
                f"/api/setup/capture/{job_id}/cancel", headers=headers,
            )
            repeated = await client.post(
                f"/api/setup/capture/{job_id}/cancel", headers=headers,
            )
            release_second.set()
            for _ in range(300):
                job = (await client.get("/api/setup/capture/status")).json()["job"]
                if job["state"] == "cancelled":
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("capture did not cancel")
            attempts = await client.get("/api/setup/capture/attempts")
            committed = SQLiteStorage(str(database))
            try:
                committed_count = len(committed.list_agent_run_bundles("__verdict_local__"))
            finally:
                committed.close()
            await client.post("/api/setup/preview", headers=headers, json=payload)
            retry, complete = await _capture_and_wait(client, headers, payload)
            late = await client.post(
                f"/api/setup/capture/{job_id}/cancel", headers=headers,
            )
            return denied, requested, repeated, job, attempts, committed_count, retry, complete, late

    try:
        denied, requested, repeated, job, attempts, committed_count, retry, complete, late = asyncio.run(run())
    finally:
        release_second.set()
    assert denied.status_code == 403
    assert requested.status_code == 202 and repeated.status_code == 202
    assert job["finishedAt"] is not None
    assert job["filesDone"] <= job["filesTotal"] == 2
    assert committed_count == 1
    assert attempts.json()["cancelledFiles"] == 1
    assert attempts.json()["failedFiles"] == 0
    assert retry.status_code == 202 and complete["state"] == "completed"
    assert late.status_code == 404
    storage = SQLiteStorage(str(database))
    try:
        assert len(storage.list_agent_run_bundles("__verdict_local__")) == 2
    finally:
        storage.close()


def test_capture_status_identifies_the_job_and_reports_analysis_errors(tmp_path, monkeypatch):
    import verdict.dashboard.setup_routes as setup_routes

    codex = tmp_path / "codex" / "sessions"
    _write_codex(codex / "session.jsonl")
    database = tmp_path / "verdict.db"
    # The analysis service reports a builder failure as a returned error status.
    monkeypatch.setattr(
        setup_routes.SetupRoutes, "_run_analysis",
        lambda self: {"analysisState": {"status": "error", "error": "io_error"}},
    )

    async def run():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            await client.post("/api/setup/preview", headers=headers, json={"codexRoot": str(codex)})
            started, job = await _capture_and_wait(client, headers, {"codexRoot": str(codex)})
            by_id = await client.get(f"/api/setup/capture/status?job={job['jobId']}")
            other = await client.get("/api/setup/capture/status?job=not-this-job")
            return started, job, by_id, other

    started, job, by_id, other = asyncio.run(run())
    assert started.status_code == 202 and started.json()["job"]["jobId"] == job["jobId"]
    assert len(job["jobId"]) == 16
    # The files were imported and the summary is kept even though analysis failed.
    assert job["state"] == "failed" and job["error"] == "analysis_failed"
    assert job["summary"]["stored"] == 1 and job["analysis"]["status"] == "error"
    assert by_id.status_code == 200 and by_id.json()["job"]["jobId"] == job["jobId"]
    assert other.status_code == 404 and other.json()["job"]["jobId"] == job["jobId"]
def test_capture_worker_interruption_sets_terminal_state(tmp_path, monkeypatch):
    from verdict.dashboard import setup_routes

    routes = setup_routes.SetupRoutes(f"sqlite:///{tmp_path / 'capture.db'}", "synthetic-token")
    job = setup_routes.CaptureJob("synthetic-job", "2026-10-01T00:00:00Z")

    def interrupted(_storage, **_kwargs):
        raise SystemExit("synthetic interruption")

    monkeypatch.setattr(setup_routes, "capture_local_agents", interrupted)
    with pytest.raises(SystemExit):
        routes._run_capture_job(job, None, None, True)

    assert job.state == "failed"
    assert job.error == "capture_failed"
    assert job.finished_at is not None
