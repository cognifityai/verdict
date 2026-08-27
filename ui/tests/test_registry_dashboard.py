from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Request
from verdict.dashboard.app import build_bundle, build_registry_bundle, create_app
from verdict.schema import (
    ClusterIdentity,
    ClusterRegistryCluster,
    ClusterRegistryVersion,
    Trace,
    TraceClusterAssignment,
)
from verdict.storage import SQLiteStorage
from verdict_eval.cluster_registry import ClusterRegistryService
from verdict_eval.clustering_strategies import FitConfig


def _trace(tenant: str, trace_id: str, when: datetime, intent: str | None) -> Trace:
    tags: dict[str, str] = {"verdict.workload": "agent"}
    if intent is not None:
        tags["verdict.intent_key"] = intent
    return Trace(
        trace_id=trace_id,
        tenant_id=tenant,
        session_id=f"session-{trace_id}",
        started_at=when,
        ended_at=when + timedelta(seconds=1),
        provider="anthropic",
        request_model="claude-haiku-4-5",
        response_model="claude-haiku-4-5",
        prompt_redacted=f"Safe representative prompt for {intent or 'missing intent'}",
        tags=tags,
    )


def _active_explicit_registry(
    storage: SQLiteStorage,
    tenant: str,
    cutoff: datetime,
) -> tuple[ClusterRegistryService, str]:
    storage.insert_trace(
        _trace(tenant, f"{tenant}-trace", cutoff - timedelta(minutes=2), "billing")
    )
    service = ClusterRegistryService(storage)
    version = service.fit(
        tenant,
        actor="admin@example.test",
        strategy="explicit",
        cutoff=cutoff,
        config=FitConfig(strategy="explicit", target_workload="agent"),
    )
    service.assign(tenant, version.version_id, through_cutoff=cutoff)
    assert service.validate(tenant, version.version_id, actor="admin@example.test")["passed"]
    service.activate(
        tenant,
        version.version_id,
        expected_generation=0,
        actor="admin@example.test",
    )
    return service, version.version_id


def test_registry_api_uses_host_authorized_tenant_and_explains_terminal_membership(
    tmp_path,
) -> None:
    path = tmp_path / "registry-dashboard.db"
    storage = SQLiteStorage(str(path))
    cutoff = datetime(2026, 8, 23, tzinfo=timezone.utc)
    service_a, version_a = _active_explicit_registry(storage, "tenant-a", cutoff)
    _service_b, version_b = _active_explicit_registry(storage, "tenant-b", cutoff)
    later = cutoff + timedelta(minutes=1)
    storage.insert_trace(_trace("tenant-a", "missing-key", later, None))
    assert service_a.assign("tenant-a", version_a, through_cutoff=later + timedelta(minutes=1)) == 1
    cluster_id = storage.list_cluster_registry_clusters("tenant-a", version_a)[0].cluster_id
    tenantless = _trace("tenant-a", "tenantless-private", cutoff, "billing")
    tenantless.tenant_id = None
    tenantless.prompt_redacted = "TENANTLESS PRIVATE PROMPT"
    tenantless.session_id = "tenantless-private-session"
    storage.insert_trace(tenantless)
    storage.insert_trace_cluster_assignments(
        "tenant-a",
        [
            TraceClusterAssignment(
                "tenant-a",
                version_a,
                tenantless.trace_id,
                "incremental",
                "assigned",
                cluster_id,
                "explicit",
            )
        ],
    )
    for index in range(101):
        service_a.rename(
            "tenant-a",
            cluster_id,
            f"Billing requests {index}",
            actor="admin@example.test",
        )
    for _index in range(21):
        service_a.fit(
            "tenant-a",
            actor="admin@example.test",
            strategy="explicit",
            cutoff=cutoff,
            config=FitConfig(strategy="explicit", target_workload="agent"),
        )
    storage.close()

    host = FastAPI()

    @host.middleware("http")
    async def authorize_tenant(request: Request, call_next):
        request.state.verdict_registry_tenant = "tenant-a"
        return await call_next(request)

    host.mount("/admin/verdict", create_app(storage=f"sqlite:///{path}"))

    async def request_registry():
        transport = httpx.ASGITransport(app=host)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return (
                await client.get(
                    "/admin/verdict/api/registry",
                    params={"tenant": "tenant-b"},
                ),
                await client.get(
                    "/admin/verdict/api/registry",
                    params={"tenant": "tenant-b", "version": version_b},
                ),
                await client.get("/admin/verdict/api/data"),
                await client.get(
                    "/admin/verdict/api/traces",
                    params={"q": "Billing requests 100"},
                ),
                await client.get(
                    "/admin/verdict/api/traces/tenant-a-trace",
                ),
            )

    (
        response,
        rejected_version,
        data_response,
        traces_response,
        trace_detail_response,
    ) = asyncio.run(request_registry())
    payload = response.json()
    data_payload = data_response.json()
    traces_payload = traces_response.json()
    trace_detail_payload = trace_detail_response.json()

    assert response.status_code == 200
    assert payload["schema"] == "cluster-registry-dashboard-v1"
    assert payload["tenant"] == "tenant-a"
    assert payload["active"]["versionId"] == version_a
    assert payload["selectedVersion"]["versionId"] == version_a
    assert payload["selectedVersion"]["strategyStatus"] == {
        "strategy": "explicit",
        "experimental": False,
        "semantic_component": "none",
    }
    assert payload["selectedVersion"]["algorithm"] == "ward-best-k-v2"
    assert payload["selectedVersion"]["selector"] == "latest-user-v1"
    assert payload["readiness"]["status"] == "validated"
    assert payload["readiness"]["passed"] is True
    assert payload["activationHistory"] is True
    assert payload["counts"] == {
        "assigned": 2,
        "outlier": 0,
        "ineligible": 1,
        "total": 3,
    }
    assert payload["clusters"][0]["displayName"] == "Billing requests 100"
    assert payload["clusters"][0]["representatives"] == [
        {
            "traceId": "tenant-a-trace",
            "prompt": "Safe representative prompt for billing",
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
        }
    ]
    assert payload["clusters"][0]["modelDistribution"] == [
        {"provider": "anthropic", "model": "claude-haiku-4-5", "count": 1}
    ]
    assert payload["clusters"][0]["conversationReadiness"] == {
        "status": "collecting",
        "floor": 30,
        "baseline": 0,
        "current": 1,
        "remainingBaseline": 30,
        "remainingCurrent": 29,
        "estimatedDaysToReady": 30,
    }
    assert payload["modelDistribution"] == [
        {"provider": "anthropic", "model": "claude-haiku-4-5", "count": 1}
    ]
    assert payload["trafficWindow"] == {
        "cutoff": cutoff.isoformat(),
        "baselineDays": 7,
        "gapDays": 1,
        "currentDays": 1,
        "conversationFloor": 30,
        "diagnosticOnly": True,
    }
    assert len(payload["versions"]) == 10
    assert any(item["versionId"] == version_a for item in payload["versions"])
    assert payload["versionsTruncated"] is True
    assert {item["traceId"] for item in payload["assignments"]} == {
        "tenant-a-trace",
        "tenantless-private",
        "missing-key",
    }
    missing = next(item for item in payload["assignments"] if item["traceId"] == "missing-key")
    assert missing["status"] == "ineligible"
    assert missing["reason"] == "missing_intent_key"
    assert version_b not in str(payload)
    assert "TENANTLESS PRIVATE PROMPT" not in str(payload)
    assert "tenantless-private-session" not in str(payload)
    assert rejected_version.status_code == 404
    assert data_response.status_code == 200
    assert data_payload["meta"]["clusters"] == 1
    assert data_payload["clusters"] == [
        {
            "cluster_id": cluster_id,
            "display_name": "Billing requests 100",
            "n": 1,
        }
    ]
    tenant_a_sample = next(
        item for item in data_payload["samples"] if item["trace_id"] == "tenant-a-trace"
    )
    assert tenant_a_sample["cluster_id"] == cluster_id
    assert tenant_a_sample["cluster_label"] == "Billing requests 100"
    tenantless_sample = next(
        item for item in data_payload["samples"] if item["trace_id"] == "tenantless-private"
    )
    assert tenantless_sample["cluster_id"] is None
    assert tenantless_sample["cluster_label"] is None
    assert traces_response.status_code == 200
    assert [item["trace_id"] for item in traces_payload["items"]] == [
        "tenant-a-trace"
    ]
    assert traces_payload["items"][0]["cluster_id"] == cluster_id
    assert traces_payload["items"][0]["cluster_label"] == "Billing requests 100"
    assert trace_detail_response.status_code == 200
    assert trace_detail_payload["cluster_id"] == cluster_id
    assert trace_detail_payload["cluster_label"] == "Billing requests 100"

    with sqlite3.connect(path) as partial:
        partial.execute("DROP TABLE cluster_registry_versions")
    partial_bundle = build_bundle(path, registry_tenant="tenant-a")
    assert partial_bundle["meta"]["clusters"] == 0
    assert all(sample["cluster_id"] is None for sample in partial_bundle["samples"])


def test_registry_api_is_additive_for_old_stores_and_requires_explicit_shared_scope(
    tmp_path,
) -> None:
    path = tmp_path / "old-dashboard.db"
    path.touch()
    app = create_app(storage=f"sqlite:///{path}")

    async def requests():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return (
                await client.get("/api/registry"),
                await client.get("/api/registry", params={"tenant": "tenant-a"}),
            )

    local_response, tenant_response = asyncio.run(requests())

    assert local_response.status_code == 200
    assert local_response.json() == {
        "schema": "cluster-registry-dashboard-v1",
        "tenant": "__verdict_local__",
        "status": "unavailable",
        "reason": "registry_not_installed",
    }
    assert tenant_response.status_code == 200
    assert tenant_response.json()["tenant"] == "tenant-a"
    assert tenant_response.json()["status"] == "unavailable"


def test_registry_read_model_bounds_representatives_and_reports_semantic_health(
    tmp_path,
) -> None:
    path = tmp_path / "semantic-health.db"
    storage = SQLiteStorage(str(path))
    tenant = "tenant-health"
    cutoff = datetime(2026, 8, 23, tzinfo=timezone.utc)
    identities = []
    clusters = []
    assignments = []
    owners = [0, 0, 0, 1, 2, 3]
    for index in range(4):
        cluster_id = f"clu-health-{index}"
        identities.append(
            ClusterIdentity(
                tenant_id=tenant,
                cluster_id=cluster_id,
                kind="semantic",
                display_name=f"Semantic {index}",
            )
        )
        clusters.append(
            ClusterRegistryCluster(
                tenant,
                "crv-health",
                cluster_id,
                "semantic",
                centroid=[1.0],
                radius=0.4,
                member_count=5,
            )
        )
    for index, owner in enumerate(owners):
        trace = Trace(
            trace_id=f"trace-health-{index}",
            tenant_id=tenant,
            session_id=f"session-health-{index}",
            started_at=cutoff - timedelta(hours=1),
            ended_at=cutoff - timedelta(minutes=59),
            provider="anthropic" if index < 4 else "openai",
            request_model="model-a" if index < 4 else "model-b",
            response_model="model-a" if index < 4 else "model-b",
            prompt_redacted=f"Safe redacted semantic prompt {index}",
            tags={"verdict.workload": "agent"},
        )
        storage.insert_trace(trace)
        assignments.append(
            TraceClusterAssignment(
                tenant,
                "crv-health",
                trace.trace_id,
                "fit",
                "assigned",
                f"clu-health-{owner}",
                "semantic",
                distance=0.1,
            )
        )
    version = ClusterRegistryVersion(
        tenant_id=tenant,
        version_id="crv-health",
        strategy="semantic",
        cutoff=cutoff,
        fit_definition_json=json.dumps(
            {
                "selector": "latest-user-v1",
                "algorithm": "ward-best-k-v2",
                "model": {"name": "MiniLM", "revision": "frozen"},
                "config": {"target_workload": "agent"},
            }
        ),
        preview_report_json=json.dumps(
            {
                "candidate_count": 6,
                "fit_assignment_count": 6,
                "cluster_count": 4,
                "explicit_cluster_count": 0,
                "semantic_cluster_count": 4,
                "statuses": {"assigned": 6, "outlier": 0, "ineligible": 0},
                "metrics": {},
                "warnings": [],
                "candidate_summary": {},
            }
        ),
    )
    storage.insert_cluster_preview(version, identities, clusters, assignments)
    storage.close()

    bundle = build_registry_bundle(f"sqlite:///{path}", tenant=tenant)

    assert bundle["healthWarnings"] == [
        "fragmented_semantic_space",
        "oversized_semantic_cluster",
    ]
    dominant = next(item for item in bundle["clusters"] if item["clusterId"] == "clu-health-0")
    assert dominant["warnings"] == ["oversized_semantic_cluster"]
    assert len(dominant["representatives"]) == 3
    assert dominant["conversationReadiness"]["current"] == 3
    assert bundle["modelDistribution"] == [
        {"provider": "anthropic", "model": "model-a", "count": 4},
        {"provider": "openai", "model": "model-b", "count": 2},
    ]


def test_registry_readiness_rejects_invalid_sqlite_session_values(tmp_path) -> None:
    path = tmp_path / "registry-session-boundary.db"
    storage = SQLiteStorage(str(path))
    tenant = "tenant-session-boundary"
    cutoff = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
    service, version_id = _active_explicit_registry(storage, tenant, cutoff)
    traces: list[Trace] = []
    for window, when, valid_count in (
        ("baseline", cutoff - timedelta(days=2), 29),
        ("current", cutoff - timedelta(hours=1), 28),
    ):
        for index in range(valid_count):
            trace = _trace(tenant, f"{window}-valid-{index}", when, "billing")
            trace.session_id = f"{window}-session-{index}"
            traces.append(trace)
        invalid_sessions = ("", "nul\x00session", "a" * 257, "é" * 129)
        for index, session_id in enumerate(invalid_sessions):
            trace = _trace(tenant, f"{window}-invalid-{index}", when, "billing")
            trace.session_id = session_id
            traces.append(trace)
    invalid_text = _trace(tenant, "current-invalid-utf8", cutoff - timedelta(hours=1), "billing")
    invalid_blob = _trace(tenant, "current-invalid-blob", cutoff - timedelta(hours=1), "billing")
    traces.extend((invalid_text, invalid_blob))
    for trace in traces:
        storage.insert_trace(trace)
    storage._conn.execute(
        "UPDATE traces SET session_id=CAST(X'80' AS TEXT) WHERE trace_id=?",
        (invalid_text.trace_id,),
    )
    storage._conn.execute(
        "UPDATE traces SET session_id=X'80' WHERE trace_id=?",
        (invalid_blob.trace_id,),
    )
    service.assign(tenant, version_id, through_cutoff=cutoff)
    storage.close()

    bundle = build_registry_bundle(f"sqlite:///{path}", tenant=tenant)
    readiness = bundle["clusters"][0]["conversationReadiness"]
    assert readiness["baseline"] == 29
    assert readiness["current"] == 29
    assert readiness["status"] == "collecting"


def test_registry_maximum_cluster_shape_stays_within_api_redaction_budget(tmp_path) -> None:
    path = tmp_path / "registry-maximum-shape.db"
    storage = SQLiteStorage(str(path))
    tenant = "tenant-maximum-shape"
    cutoff = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
    version_id = "crv-maximum-shape"
    identities: list[ClusterIdentity] = []
    clusters: list[ClusterRegistryCluster] = []
    assignments: list[TraceClusterAssignment] = []
    for index in range(250):
        cluster_id = f"clu-maximum-{index:03d}"
        kind = "explicit" if index < 200 else "semantic"
        identities.append(
            ClusterIdentity(
                tenant_id=tenant,
                cluster_id=cluster_id,
                kind=kind,
                explicit_key=f"intent-{index:03d}" if kind == "explicit" else None,
                display_name=f"Cluster {index:03d}",
            )
        )
        clusters.append(
            ClusterRegistryCluster(
                tenant,
                version_id,
                cluster_id,
                kind,
                centroid=[1.0] if kind == "semantic" else None,
                radius=0.4 if kind == "semantic" else None,
                member_count=5,
            )
        )
        if index >= 20:
            continue
        for model_index in range(5):
            trace = _trace(
                tenant,
                f"trace-maximum-{index:03d}-{model_index}",
                cutoff - timedelta(hours=1),
                f"intent-{index:03d}",
            )
            trace.request_model = f"model-{model_index}"
            trace.response_model = f"model-{model_index}"
            storage.insert_trace(trace)
            assignments.append(
                TraceClusterAssignment(
                    tenant,
                    version_id,
                    trace.trace_id,
                    "fit",
                    "assigned",
                    cluster_id,
                    kind,
                    distance=0.1 if kind == "semantic" else None,
                )
            )
    version = ClusterRegistryVersion(
        tenant_id=tenant,
        version_id=version_id,
        strategy="hybrid",
        cutoff=cutoff,
        fit_definition_json=json.dumps(
            {
                "algorithm": "ward-best-k-v2",
                "selector": "latest-user-v1",
                "model": {"name": "MiniLM"},
                "config": {"target_workload": "agent"},
            }
        ),
        preview_report_json=json.dumps(
            {
                "candidate_count": len(assignments),
                "fit_assignment_count": len(assignments),
                "cluster_count": 250,
                "explicit_cluster_count": 200,
                "semantic_cluster_count": 50,
                "statuses": {"assigned": len(assignments), "outlier": 0, "ineligible": 0},
                "metrics": {},
                "warnings": [],
                "candidate_summary": {},
            }
        ),
    )
    storage.insert_cluster_preview(version, identities, clusters, assignments)
    storage.close()
    app = create_app(storage=f"sqlite:///{path}")

    async def request_registry():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/registry", params={"tenant": tenant})

    response = asyncio.run(request_registry())
    payload = response.json()
    assert response.status_code == 200
    assert len(payload["clusters"]) == 250
    assert sum(item["detailsAvailable"] for item in payload["clusters"]) == 20
    assert payload["clusterDetailsTruncated"] is True
    assert len(payload["assignments"]) == 50
