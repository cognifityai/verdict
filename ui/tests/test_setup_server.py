import asyncio
import json
import sqlite3

import httpx
from verdict.dashboard.app import create_app
from verdict.storage import SQLiteStorage


def _write_codex(path):
    path.parent.mkdir(parents=True)
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


def test_setup_uses_canonical_historical_file_import(tmp_path):
    database = tmp_path / "verdict.db"
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
    assert evaluator_preview.json()["eligible"] == 1
    assert evaluator_preview.json()["plannedCalls"] == 1
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
