import asyncio
import json

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


def test_setup_preview_then_approved_local_capture(tmp_path):
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    claude.mkdir()
    _write_codex(codex / "session.jsonl")
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
            capture = await client.post(
                "/api/setup/capture", headers={"X-Verdict-Setup": token},
                json={"claudeRoot": str(claude), "codexRoot": str(codex)},
            )
            rejected = await client.post(
                "/api/setup/capture", headers={"X-Verdict-Setup": "wrong"}, json={},
            )
            dashboard = await client.get("/api/data")
            runs = await client.get("/api/runs?limit=1")
            return preview, capture, rejected, dashboard, runs

    preview, capture, rejected, dashboard, runs = asyncio.run(setup())
    assert preview.status_code == 200
    assert preview.json()["codex"]["files"] == 1
    assert capture.status_code == 200
    assert capture.json()["summary"]["stored"] == 1
    assert "SECRET_CANARY" not in capture.text
    assert rejected.status_code == 403
    assert dashboard.json()["meta"]["totalTraces"] == 0
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
            return await client.post(
                "/api/setup/capture",
                headers={"X-Verdict-Setup": token},
                json={"codexRoot": str(codex), "captureContent": False},
            )

    response = asyncio.run(setup())
    assert response.status_code == 200
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
