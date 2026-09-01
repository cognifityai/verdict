import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from verdict.dashboard.app import create_app
from verdict.schema import Trace
from verdict.storage import SQLiteStorage

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _insert_traces(path, count, *, start=0, errors_from=10_000):
    storage = SQLiteStorage(str(path))
    for index in range(start, start + count):
        storage.insert_trace(Trace(
            trace_id=f"trace-{index:03d}", started_at=NOW + timedelta(days=index),
            ended_at=NOW + timedelta(days=index, seconds=1), provider="openai",
            request_model="model", response_redacted="ok",
            error="provider failed" if index >= errors_from else None,
        ))
    storage.close()


def test_monitor_preview_activation_and_prospective_run(tmp_path):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 50, errors_from=40)

    async def bootstrap():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = await client.post("/api/monitor/preview", headers=headers, json={
                "windowMode": "count", "referenceRatio": 0.8,
                "minimumReference": 10, "minimumCurrent": 5,
                "prospectiveTarget": 5, "minimumEffect": 0.2,
            })
            policy_id = preview.json()["policy"]["policy_id"]
            activate = await client.post("/api/monitor/activate", headers=headers, json={
                "policyId": policy_id, "expectedActivePolicyId": None,
            })
            state = await client.get("/api/monitor")
            return preview, activate, state

    preview, activate, state = asyncio.run(bootstrap())
    assert preview.status_code == 200
    assert preview.json()["snapshot"]["comparison"]["status"] == "alert"
    assert any(metric["metric"] == "provider_error" and metric["alert"]
               for metric in preview.json()["snapshot"]["comparison"]["metrics"])
    assert activate.status_code == 200
    assert activate.json()["snapshot"]["comparison"]["status"] == "insufficient"
    assert activate.json()["snapshot"]["manifest"]["current_unit_ids"] == []
    assert activate.json()["snapshot"]["manifest"]["comparison_index"] == 1
    assert state.json()["state"] == "active"
    assert state.json()["snapshot"]["comparison"]["status"] == "insufficient"

    _insert_traces(database, 3, start=50)

    async def run_cycle():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            current_token = (await client.get("/api/setup/token")).json()["setupToken"]
            response = await client.post(
                "/api/monitor/run", headers={"X-Verdict-Setup": current_token},
            )
            rejected = await client.post("/api/monitor/run")
            return response, rejected

    cycle, rejected = asyncio.run(run_cycle())
    assert cycle.status_code == 200
    assert cycle.json()["snapshot"]["comparison"]["status"] == "insufficient"
    assert len(cycle.json()["snapshot"]["manifest"]["current_unit_ids"]) == 3
    assert rejected.status_code == 403

    _insert_traces(database, 2, start=53)

    async def finish_cycle_after_restart():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            current_token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/run", headers={"X-Verdict-Setup": current_token},
            )

    completed = asyncio.run(finish_cycle_after_restart())
    assert completed.status_code == 200
    completed_snapshot = completed.json()["snapshot"]
    assert completed_snapshot["comparison"]["status"] == "no_alert"
    assert completed_snapshot["manifest"]["current_unit_ids"] == [
        "trace-050", "trace-051", "trace-052", "trace-053", "trace-054",
    ]


def test_monitor_rejects_outcome_seeking_or_invalid_window_parameters(tmp_path):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 5)

    async def invalid_preview():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/preview", headers={"X-Verdict-Setup": token},
                json={"windowMode": "count", "referenceRatio": 1.0},
            )

    assert asyncio.run(invalid_preview()).status_code == 400


def test_monitor_accepts_ordered_explicit_event_time_windows(tmp_path):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 50)

    async def explicit_preview():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/preview", headers={"X-Verdict-Setup": token},
                json={
                    "windowMode": "explicit",
                    "referenceStart": NOW.isoformat(),
                    "referenceEnd": (NOW + timedelta(days=20)).isoformat(),
                    "currentStart": (NOW + timedelta(days=30)).isoformat(),
                    "currentEnd": (NOW + timedelta(days=50)).isoformat(),
                    "minimumReference": 10,
                    "minimumCurrent": 10,
                },
            )

    response = asyncio.run(explicit_preview())
    assert response.status_code == 200
    manifest = response.json()["snapshot"]["manifest"]
    assert len(manifest["reference_unit_ids"]) == 20
    assert len(manifest["current_unit_ids"]) == 20
