import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from verdict.dashboard.app import create_app
from verdict.schema import (
    ClusterIdentity,
    ClusterRegistryCluster,
    ClusterRegistryEvent,
    ClusterRegistryVersion,
    DimensionScore,
    Judgment,
    JudgmentStatus,
    Trace,
    TraceClusterAssignment,
    Verdict,
    cluster_candidate_digest,
)
from verdict.storage import SQLiteStorage

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _insert_traces(path, count, *, start=0, errors_from=10_000, tenant="__verdict_local__"):
    storage = SQLiteStorage(str(path))
    for index in range(start, start + count):
        storage.insert_trace(Trace(
            trace_id=f"trace-{index:03d}", started_at=NOW + timedelta(days=index),
            ended_at=NOW + timedelta(days=index, seconds=1), provider="openai",
            request_model="model", response_redacted="ok",
            tenant_id=tenant,
            error="provider failed" if index >= errors_from else None,
        ))
    storage.close()


def test_monitor_preview_includes_default_tenantless_sdk_traces(tmp_path):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 20, tenant=None)
    _insert_traces(database, 1, start=100, tenant="other")

    async def preview():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/preview",
                headers={"X-Verdict-Setup": token},
                json={
                    "windowMode": "count",
                    "referenceRatio": 0.5,
                    "minimumReference": 5,
                    "minimumCurrent": 5,
                },
            )

    response = asyncio.run(preview())

    assert response.status_code == 200
    manifest = response.json()["snapshot"]["manifest"]
    assert len(manifest["reference_unit_ids"]) == 10
    assert len(manifest["current_unit_ids"]) == 10


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
    assert state.json()["approvedHistoricalSnapshot"] == preview.json()["snapshot"]

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
    assert completed.json()["approvedHistoricalSnapshot"] == preview.json()["snapshot"]


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


def test_cluster_grouping_without_active_registry_is_a_bounded_request_error(tmp_path):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 5)

    async def preview_without_registry():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/preview",
                headers={"X-Verdict-Setup": token},
                json={"groupingMode": "cluster"},
            )

    response = asyncio.run(preview_without_registry())
    assert response.status_code == 400
    assert response.json() == {"error": "invalid monitor request"}


def test_cluster_monitor_pins_the_reviewed_registry_version(tmp_path):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 20)
    tenant = "__verdict_local__"
    storage = SQLiteStorage(str(database))

    def insert_version(version_id, cluster_id, *, parent=None):
        identity = ClusterIdentity(
            tenant_id=tenant, cluster_id=cluster_id, kind="explicit",
            explicit_key=cluster_id, display_name=cluster_id,
        )
        trace_ids = [f"trace-{index:03d}" for index in range(20)]
        storage.insert_cluster_preview(
            ClusterRegistryVersion(
                tenant_id=tenant, version_id=version_id,
                parent_version_id=parent, strategy="explicit", cutoff=NOW,
            ),
            [identity],
            [ClusterRegistryCluster(
                tenant_id=tenant, version_id=version_id,
                cluster_id=cluster_id, kind="explicit", member_count=20,
            )],
            [
                TraceClusterAssignment(
                    tenant_id=tenant, version_id=version_id, trace_id=trace_id,
                    origin="fit", status="assigned", cluster_id=cluster_id,
                    cluster_kind="explicit",
                )
                for trace_id in trace_ids
            ],
        )
        storage.insert_cluster_registry_event(ClusterRegistryEvent(
            tenant_id=tenant, action="validated", to_version_id=version_id,
            actor="test", details_json='{"passed":true}',
        ))
        return trace_ids

    first_ids = insert_version("registry-1", "cluster-old")
    storage.activate_cluster_registry(
        tenant, "registry-1", expected_generation=0, actor="test",
        action="activated", expected_candidate_digest=cluster_candidate_digest(first_ids),
    )
    storage.close()

    async def preview():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/preview",
                headers={"X-Verdict-Setup": token},
                json={
                    "groupingMode": "cluster", "referenceRatio": 0.5,
                    "minimumReference": 5, "minimumCurrent": 5,
                },
            )

    response = asyncio.run(preview())

    assert response.status_code == 200
    body = response.json()
    assert body["policy"]["cluster_registry_version_id"] == "registry-1"
    assert {
        metric["group_id"]
        for metric in body["snapshot"]["comparison"]["metrics"]
    } == {"cluster-old"}


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


def test_monitor_compares_existing_judgments_without_mixing_evaluators_or_tenants(
    tmp_path,
):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 20, tenant=None)
    selected = "a" * 64
    storage = SQLiteStorage(str(database))
    for index in range(20):
        trace_id = f"trace-{index:03d}"
        if index != 19:
            storage.insert_judgment(Judgment(
                judgment_id=f"selected-{index:03d}", trace_id=trace_id,
                evaluator_provider="anthropic", judge_models=["judge"],
                evaluator_fingerprint=selected, expected_dimensions=["quality"],
                rubric_name="quality", rubric_version="1",
                dimensions=([] if index == 18 else [DimensionScore(
                    "quality",
                    Verdict.PASS if index < 10 else (
                        Verdict.UNCLEAR if index in {16, 17} else Verdict.FAIL
                    ),
                )]),
                status=(JudgmentStatus.ERROR if index == 18 else JudgmentStatus.COMPLETED),
                error=("judge unavailable" if index == 18 else None),
            ))
        storage.insert_judgment(Judgment(
            judgment_id=f"other-{index:03d}", trace_id=trace_id,
            evaluator_provider="anthropic", judge_models=["other"],
            evaluator_fingerprint="b" * 64, expected_dimensions=["quality"],
            rubric_name="other", rubric_version="1",
            dimensions=[DimensionScore(
                "quality", Verdict.FAIL if index < 10 else Verdict.PASS,
            )],
        ))
    for index in range(20):
        storage.insert_trace(Trace(
            trace_id=f"foreign-{index:03d}", tenant_id="foreign",
            started_at=NOW + timedelta(days=100 + index),
            ended_at=NOW + timedelta(days=100 + index, seconds=1),
            error="foreign failure",
        ))
    storage.close()

    async def preview():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/preview",
                headers={"X-Verdict-Setup": token},
                json={
                    "windowMode": "count", "referenceRatio": 0.5,
                    "minimumReference": 10, "minimumCurrent": 5,
                    "minimumEffect": 0.5,
                    "evaluatorFingerprint": selected,
                },
            )

    response = asyncio.run(preview())

    assert response.status_code == 200
    body = response.json()
    assert len(body["snapshot"]["manifest"]["reference_unit_ids"]) == 10
    assert len(body["snapshot"]["manifest"]["current_unit_ids"]) == 10
    metric = next(
        item for item in body["snapshot"]["comparison"]["metrics"]
        if item["metric"] == "judge.quality.pass"
    )
    assert metric["reference_value"] == 1.0
    assert metric["current_value"] == 0.0
    assert metric["reference_n"] == 10
    assert metric["current_n"] == 6
    assert metric["alert"] is True
    assert body["snapshot"]["comparison"]["metric_coverage"] == [{
        "metric": "judge.quality.pass",
        "reference_evaluable": 10, "reference_unclear": 0,
        "reference_missing": 0, "reference_error": 0,
        "current_evaluable": 6, "current_unclear": 2,
        "current_missing": 1, "current_error": 1,
    }]
    assert body["snapshot"]["comparison"]["status"] == "alert"
    assert body["policy"]["evaluator_fingerprint"] == selected
    assert body["policy"]["evaluator_dimensions"] == ["quality"]


def test_monitor_rejects_an_unknown_evaluator_fingerprint(tmp_path):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 20)

    async def preview():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/preview",
                headers={"X-Verdict-Setup": token},
                json={"evaluatorFingerprint": "f" * 64},
            )

    response = asyncio.run(preview())

    assert response.status_code == 400
    assert response.json() == {"error": "invalid monitor request"}
