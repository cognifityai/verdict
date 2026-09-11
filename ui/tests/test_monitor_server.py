import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
from verdict.dashboard.app import create_app
from verdict.monitoring import CohortManifest, MonitorComparison, MonitorPolicy, MonitorStatus
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
from verdict_eval.cluster_registry import ClusterRegistryService
from verdict_eval.clustering_strategies import FitConfig

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _insert_traces(
    path, count, *, start=0, errors_from=10_000,
    tenant="__verdict_local__", at=None,
):
    storage = SQLiteStorage(str(path))
    for index in range(start, start + count):
        started_at = (
            at + timedelta(seconds=index - start)
            if at is not None
            else NOW + timedelta(days=index)
        )
        storage.insert_trace(Trace(
            trace_id=f"trace-{index:03d}", started_at=started_at,
            ended_at=started_at + timedelta(seconds=1), provider="openai",
            request_model="model", prompt_redacted="request", response_redacted="ok",
            tenant_id=tenant,
            error="provider failed" if index >= errors_from else None,
        ))
    storage.close()


def _insert_quality_judgments(path, trace_ids, fingerprint, verdict):
    storage = SQLiteStorage(str(path))
    for trace_id in trace_ids:
        storage.insert_judgment(Judgment(
            judgment_id=f"judgment-{trace_id}-{verdict.value}", trace_id=trace_id,
            evaluator_provider="openai", judge_models=["judge"],
            evaluator_fingerprint=fingerprint, expected_dimensions=["quality"],
            dimensions=[DimensionScore("quality", verdict)],
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
    assert state.json()["active"]["snapshot"]["comparison"]["status"] == "insufficient"
    assert state.json()["active"]["approvedHistoricalSnapshot"] == preview.json()["snapshot"]
    prospective_start = datetime.fromisoformat(
        activate.json()["snapshot"]["manifest"]["prospective_start_at"]
    )

    _insert_traces(
        database, 3, start=50,
        at=prospective_start + timedelta(microseconds=1),
    )

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

    _insert_traces(
        database, 2, start=53,
        at=prospective_start + timedelta(seconds=3),
    )

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


def test_monitor_activation_starts_empty_after_explicit_historical_preview(tmp_path):
    database = tmp_path / "explicit-activation.db"
    _insert_traces(database, 20)
    storage = SQLiteStorage(str(database))
    storage.insert_trace(Trace(
        trace_id="future-dated-before-activation",
        started_at=datetime(2100, 1, 1, tzinfo=timezone.utc),
        ended_at=datetime(2100, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        provider="openai",
        request_model="model",
        prompt_redacted="request",
        response_redacted="ok",
        tenant_id="__verdict_local__",
    ))
    storage.close()

    async def request(path, *, payload=None):
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                path, headers={"X-Verdict-Setup": token}, json=payload,
            )

    preview = asyncio.run(request("/api/monitor/preview", payload={
        "windowMode": "explicit",
        "referenceStart": NOW.isoformat(),
        "referenceEnd": (NOW + timedelta(days=5)).isoformat(),
        "currentStart": (NOW + timedelta(days=10)).isoformat(),
        "currentEnd": (NOW + timedelta(days=15)).isoformat(),
        "minimumReference": 2,
        "minimumCurrent": 2,
        "prospectiveTarget": 1,
    }))
    assert preview.status_code == 200

    activated = asyncio.run(request("/api/monitor/activate", payload={
        "policyId": preview.json()["policy"]["policy_id"],
        "expectedActivePolicyId": None,
    }))
    assert activated.status_code == 200
    manifest = activated.json()["snapshot"]["manifest"]
    assert manifest["current_unit_ids"] == []
    activated_at = datetime.fromisoformat(manifest["prospective_start_at"])

    storage = SQLiteStorage(str(database))
    storage.insert_trace(Trace(
        trace_id="post-activation",
        started_at=activated_at + timedelta(microseconds=1),
        ended_at=activated_at + timedelta(seconds=1),
        provider="openai",
        request_model="model",
        prompt_redacted="request",
        response_redacted="ok",
        tenant_id="__verdict_local__",
    ))
    storage.close()

    cycle = asyncio.run(request("/api/monitor/run"))
    assert cycle.status_code == 200
    current_ids = cycle.json()["snapshot"]["manifest"]["current_unit_ids"]
    assert current_ids == ["post-activation"]
    assert not any(trace_id.startswith("trace-") for trace_id in current_ids)


def test_monitor_waits_for_late_judgments_then_alerts_on_same_members(tmp_path):
    database = tmp_path / "late-judgments.db"
    fingerprint = "a" * 64
    _insert_traces(database, 20)
    _insert_quality_judgments(
        database, [f"trace-{index:03d}" for index in range(20)],
        fingerprint, Verdict.PASS,
    )

    async def request(path, *, json=None):
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                path, headers={"X-Verdict-Setup": token}, json=json,
            )

    preview = asyncio.run(request("/api/monitor/preview", json={
        "windowMode": "count", "referenceRatio": 0.8,
        "minimumReference": 5, "minimumCurrent": 5,
        "prospectiveTarget": 10, "minimumEffect": 0.5,
        "evaluatorFingerprint": fingerprint,
    }))
    assert preview.status_code == 200
    activated = asyncio.run(request("/api/monitor/activate", json={
        "policyId": preview.json()["policy"]["policy_id"],
        "expectedActivePolicyId": None,
    }))
    assert activated.status_code == 200
    prospective_start = datetime.fromisoformat(
        activated.json()["snapshot"]["manifest"]["prospective_start_at"]
    )
    _insert_traces(
        database, 10, start=20,
        at=prospective_start + timedelta(microseconds=1),
    )

    waiting = asyncio.run(request("/api/monitor/run"))

    assert waiting.status_code == 200
    waiting_manifest = waiting.json()["snapshot"]["manifest"]
    assert waiting_manifest["current_unit_ids"] == [
        f"trace-{index:03d}" for index in range(20, 30)
    ]
    assert len(waiting_manifest["pending_evaluator_units"]) == 10
    assert waiting_manifest["prospective_open"] is True
    assert waiting.json()["snapshot"]["comparison"]["status"] == "insufficient"

    _insert_quality_judgments(
        database, [f"trace-{index:03d}" for index in range(20, 30)],
        fingerprint, Verdict.FAIL,
    )
    completed = asyncio.run(request("/api/monitor/run"))

    completed_snapshot = completed.json()["snapshot"]
    assert completed.status_code == 200
    assert completed_snapshot["manifest"]["current_unit_ids"] == (
        waiting_manifest["current_unit_ids"]
    )
    assert completed_snapshot["manifest"]["pending_evaluator_units"] == []
    assert completed_snapshot["comparison"]["status"] == "alert"
    [quality] = [
        metric for metric in completed_snapshot["comparison"]["metrics"]
        if metric["metric"] == "judge.quality.pass"
    ]
    assert quality["reference_value"] == 1.0
    assert quality["current_value"] == 0.0


def test_monitor_run_names_rebootstrap_when_pending_evidence_disappears(tmp_path):
    database = tmp_path / "missing-pending.db"
    fingerprint = "a" * 64
    _insert_traces(database, 2)
    _insert_quality_judgments(
        database, ["trace-000", "trace-001"], fingerprint, Verdict.PASS,
    )

    async def request(path, *, json=None):
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                path, headers={"X-Verdict-Setup": token}, json=json,
            )

    preview = asyncio.run(request("/api/monitor/preview", json={
        "referenceRatio": 0.5,
        "minimumReference": 1,
        "minimumCurrent": 1,
        "prospectiveTarget": 1,
        "evaluatorFingerprint": fingerprint,
    }))
    activated = asyncio.run(request("/api/monitor/activate", json={
        "policyId": preview.json()["policy"]["policy_id"],
        "expectedActivePolicyId": None,
    }))
    assert preview.status_code == activated.status_code == 200

    prospective_start = datetime.fromisoformat(
        activated.json()["snapshot"]["manifest"]["prospective_start_at"]
    )
    _insert_traces(
        database, 1, start=2,
        at=prospective_start + timedelta(microseconds=1),
    )
    waiting = asyncio.run(request("/api/monitor/run"))
    assert waiting.status_code == 200
    assert waiting.json()["snapshot"]["manifest"]["pending_evaluator_units"]

    storage = SQLiteStorage(str(database))
    storage.delete_trace("trace-002")
    storage.close()

    response = asyncio.run(request("/api/monitor/run"))

    assert response.status_code == 409
    assert response.json() == {
        "error": "pending evaluator evidence is unavailable or changed; "
        "re-bootstrap the monitor",
        "state": "requires_rebootstrap",
    }


def test_monitor_preview_names_pending_selected_evaluator_work(tmp_path):
    database = tmp_path / "pending-preview.db"
    fingerprint = "a" * 64
    _insert_traces(database, 2)
    _insert_quality_judgments(database, ["trace-000"], fingerprint, Verdict.PASS)

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
                    "referenceRatio": 0.5,
                    "minimumReference": 1,
                    "minimumCurrent": 1,
                    "evaluatorFingerprint": fingerprint,
                },
            )

    response = asyncio.run(preview())

    assert response.status_code == 409
    assert response.json() == {
        "error": "Run the selected evaluator for 1 eligible trace, then preview "
        "this monitor again.",
        "state": "evaluator_pending",
    }


def test_monitor_candidate_survives_reload_and_matches_dashboard_status(tmp_path):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 20, errors_from=16)

    async def preview_and_reload():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            preview = await client.post(
                "/api/monitor/preview",
                headers={"X-Verdict-Setup": token},
                json={
                    "windowMode": "count",
                    "referenceRatio": 0.8,
                    "minimumReference": 10,
                    "minimumCurrent": 4,
                    "prospectiveTarget": 4,
                    "minimumEffect": 0.2,
                },
            )
            monitor = await client.get("/api/monitor")
            dashboard = await client.get("/api/data")
            return preview, monitor, dashboard

    preview, monitor, dashboard = asyncio.run(preview_and_reload())

    assert preview.status_code == 200
    expected = preview.json()
    assert monitor.status_code == 200
    assert monitor.json()["state"] == "candidate"
    assert monitor.json()["candidate"] == expected
    assert monitor.json()["active"] is None
    assert dashboard.status_code == 200
    assert dashboard.json()["monitor"] == monitor.json()


def test_monitor_read_model_keeps_new_candidate_separate_from_active(tmp_path):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 20, errors_from=16)

    async def create_active_and_candidate():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            request = {
                "windowMode": "count",
                "referenceRatio": 0.8,
                "minimumReference": 10,
                "minimumCurrent": 4,
                "prospectiveTarget": 4,
                "minimumEffect": 0.2,
            }
            first = await client.post(
                "/api/monitor/preview", headers=headers, json=request
            )
            activated = await client.post(
                "/api/monitor/activate",
                headers=headers,
                json={
                    "policyId": first.json()["policy"]["policy_id"],
                    "expectedActivePolicyId": None,
                },
            )
            second = await client.post(
                "/api/monitor/preview", headers=headers, json=request
            )
            before = await client.get("/api/monitor")
            replacement = await client.post(
                "/api/monitor/activate",
                headers=headers,
                json={
                    "policyId": second.json()["policy"]["policy_id"],
                    "expectedActivePolicyId": activated.json()["policy"]["policy_id"],
                },
            )
            after = await client.get("/api/monitor")
            return (
                activated.json(), second.json(), before.json(),
                replacement.json(), after.json(),
            )

    activated, candidate, state, replacement, replaced_state = asyncio.run(
        create_active_and_candidate()
    )

    assert state["state"] == "active"
    assert state["active"]["policy"] == activated["policy"]
    assert state["candidate"] == candidate
    assert state["candidate"]["policy"]["policy_id"] != state["active"]["policy"]["policy_id"]
    assert replaced_state["active"]["policy"] == replacement["policy"]
    assert replaced_state["candidate"] is None


def test_monitor_activation_prepares_before_cas_and_reuses_retry(tmp_path, monkeypatch):
    database = tmp_path / "verdict.db"
    _insert_traces(database, 20)

    def fail_preparation(*_args, **_kwargs):
        raise ValueError("projection unavailable")

    async def exercise():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            first = await client.post(
                "/api/monitor/preview", headers=headers,
                json={"referenceRatio": 0.5, "minimumReference": 5,
                      "minimumCurrent": 5, "prospectiveTarget": 5},
            )
            first_id = first.json()["policy"]["policy_id"]
            assert (await client.post(
                "/api/monitor/activate", headers=headers,
                json={"policyId": first_id, "expectedActivePolicyId": None},
            )).status_code == 200
            second = await client.post(
                "/api/monitor/preview", headers=headers,
                json={"referenceRatio": 0.5, "minimumReference": 5,
                      "minimumCurrent": 5, "prospectiveTarget": 5},
            )
            second_id = second.json()["policy"]["policy_id"]
            with monkeypatch.context() as patch:
                patch.setattr(
                    SQLiteStorage,
                    "save_monitor_successor",
                    fail_preparation,
                )
                preparation_failure = await client.post(
                    "/api/monitor/activate", headers=headers,
                    json={"policyId": second_id, "expectedActivePolicyId": first_id},
                )
            active_after_failure = (await client.get("/api/monitor")).json()
            cas_failure = await client.post(
                "/api/monitor/activate", headers=headers,
                json={"policyId": second_id, "expectedActivePolicyId": "stale"},
            )
            with sqlite3.connect(database) as connection:
                prepared_count = connection.execute(
                    "SELECT COUNT(*) FROM monitor_snapshots WHERE policy_id=?",
                    (second_id,),
                ).fetchone()[0]
            retry = await client.post(
                "/api/monitor/activate", headers=headers,
                json={"policyId": second_id, "expectedActivePolicyId": first_id},
            )
            with sqlite3.connect(database) as connection:
                final_count = connection.execute(
                    "SELECT COUNT(*) FROM monitor_snapshots WHERE policy_id=?",
                    (second_id,),
                ).fetchone()[0]
            return (
                preparation_failure, active_after_failure, cas_failure,
                prepared_count, retry, final_count, first_id, second_id,
            )

    (preparation_failure, active_after_failure, cas_failure, prepared_count,
     retry, final_count, first_id, second_id) = asyncio.run(exercise())
    assert preparation_failure.status_code == 400
    assert active_after_failure["active"]["policy"]["policy_id"] == first_id
    assert cas_failure.status_code == 400
    assert prepared_count == 2
    assert retry.status_code == 200
    assert retry.json()["policy"]["policy_id"] == second_id
    assert retry.json()["snapshot"]["manifest"]["comparison_index"] == 1
    assert final_count == prepared_count


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
                status=JudgmentStatus.COMPLETED,
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
    storage.insert_trace(Trace(
        trace_id="trace-019", tenant_id=None,
        started_at=NOW + timedelta(days=19),
        ended_at=NOW + timedelta(days=19, seconds=1),
        prompt_redacted="request", response_redacted=None,
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
        "current_missing": 2, "current_error": 0,
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


def test_monitor_explains_group_cardinality_limit(tmp_path):
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    for index in range(251):
        storage.insert_trace(
            Trace(
                trace_id=f"trace-{index}",
                tenant_id="__verdict_local__",
                started_at=NOW + timedelta(seconds=index),
                ended_at=NOW + timedelta(seconds=index + 1),
                provider=f"provider-{index}",
                request_model="model",
                response_redacted="ok",
            )
        )
    storage.close()

    async def preview():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/preview",
                headers={"X-Verdict-Setup": token},
                json={
                    "groupingMode": "provider_model",
                    "referenceRatio": 0.5,
                    "minimumReference": 1,
                    "minimumCurrent": 1,
                },
            )

    response = asyncio.run(preview())

    assert response.status_code == 400
    assert response.json() == {
        "error": (
            "Monitor supports at most 250 groups. Choose no grouping or reduce "
            "the number of provider/model or cluster groups."
        ),
    }


def test_active_monitor_uses_approved_judgment_facts_after_rejudge_and_delete(tmp_path):
    database = tmp_path / "verdict.db"
    selected = "a" * 64
    storage = SQLiteStorage(str(database))
    for index in range(20):
        trace_id = f"trace-{index:03d}"
        storage.insert_trace(
            Trace(
                trace_id=trace_id,
                tenant_id=None,
                started_at=NOW + timedelta(minutes=index),
                ended_at=NOW + timedelta(minutes=index, seconds=1),
                prompt_redacted="request", response_redacted="ok",
            )
        )
        storage.insert_judgment(
            Judgment(
                judgment_id=f"approved-{index}",
                trace_id=trace_id,
                evaluator_provider="anthropic",
                judge_models=["judge"],
                evaluator_fingerprint=selected,
                expected_dimensions=["quality"],
                dimensions=[DimensionScore("quality", Verdict.PASS)],
            )
        )
    storage.close()

    async def approve():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = await client.post(
                "/api/monitor/preview",
                headers=headers,
                json={
                    "referenceRatio": 0.5,
                    "minimumReference": 5,
                    "minimumCurrent": 5,
                    "prospectiveTarget": 5,
                    "minimumEffect": 0.5,
                    "evaluatorFingerprint": selected,
                },
            )
            activation = await client.post(
                "/api/monitor/activate",
                headers=headers,
                json={
                    "policyId": preview.json()["policy"]["policy_id"],
                    "expectedActivePolicyId": None,
                },
            )
            return preview, activation

    preview, activation = asyncio.run(approve())
    assert preview.status_code == activation.status_code == 200
    prospective_start = datetime.fromisoformat(
        activation.json()["snapshot"]["manifest"]["prospective_start_at"]
    )

    storage = SQLiteStorage(str(database))
    storage.delete_trace("trace-000")
    for index in range(1, 10):
        storage.insert_judgment(
            Judgment(
                judgment_id=f"later-{index}",
                trace_id=f"trace-{index:03d}",
                created_at=NOW + timedelta(days=1),
                evaluator_provider="anthropic",
                judge_models=["judge"],
                evaluator_fingerprint=selected,
                expected_dimensions=["quality"],
                dimensions=[DimensionScore("quality", Verdict.FAIL)],
            )
        )
    for index in range(20, 25):
        trace_id = f"trace-{index:03d}"
        storage.insert_trace(
            Trace(
                trace_id=trace_id,
                tenant_id=None,
                started_at=prospective_start + timedelta(minutes=index),
                ended_at=prospective_start + timedelta(minutes=index, seconds=1),
                prompt_redacted="request", response_redacted="ok",
            )
        )
        storage.insert_judgment(
            Judgment(
                judgment_id=f"current-{index}",
                trace_id=trace_id,
                evaluator_provider="anthropic",
                judge_models=["judge"],
                evaluator_fingerprint=selected,
                expected_dimensions=["quality"],
                dimensions=[DimensionScore("quality", Verdict.PASS)],
            )
        )
    storage.close()

    async def run():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/run",
                headers={"X-Verdict-Setup": token},
            )

    response = asyncio.run(run())
    assert response.status_code == 200
    quality = next(
        item
        for item in response.json()["snapshot"]["comparison"]["metrics"]
        if item["metric"] == "judge.quality.pass"
    )
    assert quality["reference_value"] == quality["current_value"] == 1.0
    assert response.json()["snapshot"]["comparison"]["status"] == "no_alert"


def test_cluster_monitor_projects_new_traffic_and_keeps_reviewed_label(tmp_path):
    database = tmp_path / "verdict.db"
    tenant = "__verdict_local__"
    storage = SQLiteStorage(str(database))
    for index in range(20):
        storage.insert_trace(
            Trace(
                trace_id=f"historical-{index}",
                tenant_id=tenant,
                started_at=NOW + timedelta(minutes=index),
                ended_at=NOW + timedelta(minutes=index, seconds=1),
                response_redacted="ok",
                tags={"verdict.intent_key": "billing"},
            )
        )
    service = ClusterRegistryService(storage)
    version = service.fit(
        tenant,
        actor="test",
        strategy="explicit",
        cutoff=NOW + timedelta(hours=1),
        config=FitConfig(strategy="explicit"),
    )
    assert service.validate(tenant, version.version_id, actor="test")["passed"]
    [identity] = storage.list_cluster_identities(tenant)
    service.rename(tenant, identity.cluster_id, "Billing questions", actor="test")
    service.activate(
        tenant,
        version.version_id,
        expected_generation=0,
        actor="test",
    )
    storage.close()

    async def approve():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = await client.post(
                "/api/monitor/preview",
                headers=headers,
                json={
                    "groupingMode": "cluster",
                    "referenceRatio": 0.5,
                    "minimumReference": 2,
                    "minimumCurrent": 2,
                    "prospectiveTarget": 2,
                },
            )
            activation = await client.post(
                "/api/monitor/activate",
                headers=headers,
                json={
                    "policyId": preview.json()["policy"]["policy_id"],
                    "expectedActivePolicyId": None,
                },
            )
            return preview, activation

    preview, activation = asyncio.run(approve())
    assert preview.status_code == activation.status_code == 200
    assert preview.json()["snapshot"]["comparison"]["groups"][0]["label"] == "Billing questions"
    prospective_start = datetime.fromisoformat(
        activation.json()["snapshot"]["manifest"]["prospective_start_at"]
    )

    storage = SQLiteStorage(str(database))
    for index in range(2):
        storage.insert_trace(
            Trace(
                trace_id=f"new-{index}",
                tenant_id=tenant,
                started_at=prospective_start + timedelta(minutes=index),
                ended_at=prospective_start + timedelta(minutes=index, seconds=1),
                response_redacted="ok",
                tags={"verdict.intent_key": "billing"},
            )
        )
    storage.close()

    async def run():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/monitor/run",
                headers={"X-Verdict-Setup": token},
            )

    response = asyncio.run(run())
    assert response.status_code == 200
    comparison = response.json()["snapshot"]["comparison"]
    assert comparison["status"] == "no_alert"
    assert comparison["unassigned_group_share"] == 0.0
    assert comparison["groups"][0]["label"] == "Billing questions"


def test_legacy_monitor_requires_guided_rebootstrap(tmp_path):
    database = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(database))
    policy = MonitorPolicy("legacy", "__verdict_local__:application:trace")
    manifest = CohortManifest(
        "1" * 64,
        policy.fingerprint,
        NOW,
        ("reference",),
        ("current",),
        ("reference", "current"),
    )
    comparison = MonitorComparison(MonitorStatus.NO_ALERT, (), 0.0, 0.05)
    storage.save_monitor_policy(policy)
    storage.save_monitor_snapshot(policy.policy_id, manifest, comparison)
    storage.activate_monitor_policy(
        policy.scope_key,
        policy.policy_id,
        expected_active_policy_id=None,
    )
    storage.close()

    async def inspect_and_run():
        app = create_app(storage=f"sqlite:///{database}")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            state = await client.get("/api/monitor")
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            run = await client.post(
                "/api/monitor/run",
                headers={"X-Verdict-Setup": token},
            )
            return state, run

    state, run = asyncio.run(inspect_and_run())
    assert state.status_code == 200
    assert state.json()["state"] == "requires_rebootstrap"
    assert state.json()["active"]["rebootstrapRequired"] is True
    assert run.status_code == 409
    assert run.json() == {"error": "monitor requires re-bootstrap"}
