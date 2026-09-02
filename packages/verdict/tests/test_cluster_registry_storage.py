from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from verdict.schema import (
    ClusterIdentity,
    ClusterRegistryCluster,
    ClusterRegistryEvent,
    ClusterRegistryVersion,
    Judgment,
    Trace,
    TraceClusterAssignment,
    cluster_candidate_digest,
)
from verdict.storage.memory import InMemoryStorage
from verdict.storage.sqlite import SQLiteStorage


@pytest.fixture(params=["memory", "sqlite"])
def registry_storage(request, tmp_path):
    if request.param == "memory":
        storage = InMemoryStorage()
    else:
        storage = SQLiteStorage(str(tmp_path / "registry.db"))
    yield storage
    storage.close()


def _preview(tenant: str = "tenant-a", version_id: str = "version-1"):
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    identity = ClusterIdentity(
        tenant_id=tenant,
        cluster_id="cluster-billing",
        kind="explicit",
        explicit_key="billing",
        display_name="Billing",
        created_at=now,
        updated_at=now,
        created_by="admin@example.test",
        updated_by="admin@example.test",
    )
    version = ClusterRegistryVersion(
        tenant_id=tenant,
        version_id=version_id,
        strategy="explicit",
        cutoff=now,
        fit_definition_json='{"schema":"fit-definition-v1"}',
        fit_definition_fingerprint="fingerprint",
        preview_report_json='{"schema":"preview-report-v1"}',
        created_at=now,
        created_by="admin@example.test",
    )
    cluster = ClusterRegistryCluster(
        tenant_id=tenant,
        version_id=version_id,
        cluster_id=identity.cluster_id,
        kind="explicit",
        member_count=1,
    )
    assignment = TraceClusterAssignment(
        tenant_id=tenant,
        version_id=version_id,
        trace_id="trace-1",
        origin="fit",
        status="assigned",
        cluster_id=identity.cluster_id,
        cluster_kind="explicit",
        assigned_at=now,
    )
    return version, [identity], [cluster], [assignment]


def test_preview_assignments_are_tenant_scoped_immutable_and_idempotent(
    registry_storage,
) -> None:
    version, identities, clusters, assignments = _preview()
    registry_storage.insert_cluster_preview(version, identities, clusters, assignments)

    loaded = registry_storage.get_cluster_registry_version("tenant-a", "version-1")
    assert loaded == version
    assert registry_storage.list_cluster_registry_clusters("tenant-a", "version-1") == clusters
    assert registry_storage.list_trace_cluster_assignments("tenant-a", "version-1") == assignments
    assert (
        registry_storage.list_trace_cluster_assignments("tenant-a", "version-1", limit=1)
        == assignments
    )
    assert (
        registry_storage.list_cluster_identities(
            "tenant-a", cluster_ids=["cluster-billing"], limit=1
        )
        == identities
    )
    with pytest.raises(ValueError, match="positive integer"):
        registry_storage.list_trace_cluster_assignments("tenant-a", "version-1", limit=0)

    registry_storage.insert_trace_cluster_assignments("tenant-a", assignments)
    conflict = TraceClusterAssignment(
        tenant_id="tenant-a",
        version_id="version-1",
        trace_id="trace-1",
        origin="incremental",
        status="outlier",
        reason="explicit_key_not_in_version",
    )
    with pytest.raises(ValueError, match="immutable assignment conflict"):
        registry_storage.insert_trace_cluster_assignments("tenant-a", [conflict])

    other = _preview("tenant-b", "version-1")
    registry_storage.insert_cluster_preview(*other)
    assert registry_storage.get_cluster_registry_version("tenant-b", "version-1")
    assert registry_storage.get_cluster_registry_version("tenant-a", "version-1")


def test_python_geometry_bounds_match_database_checks() -> None:
    with pytest.raises(ValueError, match=r"\[0,2\]"):
        ClusterRegistryCluster(
            "tenant-a",
            "version-1",
            "semantic-1",
            "semantic",
            [1.0, 0.0],
            2.000001,
        )
    with pytest.raises(ValueError, match=r"\[0,2\]"):
        TraceClusterAssignment(
            "tenant-a",
            "version-1",
            "trace-1",
            "incremental",
            "assigned",
            "semantic-1",
            "semantic",
            distance=2.000001,
        )


def test_validation_and_activation_use_one_generation_cas(registry_storage) -> None:
    version, identities, clusters, assignments = _preview()
    registry_storage.insert_cluster_preview(version, identities, clusters, assignments)
    registry_storage.insert_cluster_registry_event(
        ClusterRegistryEvent(
            tenant_id="tenant-a",
            event_id="validation-1",
            action="validated",
            to_version_id="version-1",
            actor="admin@example.test",
            details_json='{"schema":"validation-report-v1","passed":true}',
        )
    )
    assert len(registry_storage.list_cluster_registry_events("tenant-a", "version-1", limit=1)) == 1

    active = registry_storage.activate_cluster_registry(
        "tenant-a",
        "version-1",
        expected_generation=0,
        actor="admin@example.test",
        action="activated",
        expected_candidate_digest=cluster_candidate_digest(["trace-1"]),
    )

    assert active.version_id == "version-1"
    assert active.generation == 1
    [identity] = registry_storage.list_cluster_identities("tenant-a")
    assert identity.lifecycle == "active"
    assert identity.last_version_id == "version-1"
    assert identity.last_model_fingerprint is None
    with pytest.raises(ValueError, match="generation conflict"):
        registry_storage.activate_cluster_registry(
            "tenant-a",
            "version-1",
            expected_generation=0,
            actor="admin@example.test",
            action="activated",
            expected_candidate_digest=cluster_candidate_digest(["trace-1"]),
        )


def test_activation_rejects_a_stale_parent_and_rename_is_audited(
    registry_storage,
) -> None:
    first = _preview(version_id="version-1")
    registry_storage.insert_cluster_preview(*first)
    registry_storage.insert_cluster_registry_event(
        ClusterRegistryEvent(
            tenant_id="tenant-a",
            action="validated",
            to_version_id="version-1",
            actor="admin@example.test",
            details_json='{"schema":"validation-report-v1","passed":true}',
        )
    )
    registry_storage.activate_cluster_registry(
        "tenant-a",
        "version-1",
        expected_generation=0,
        actor="admin@example.test",
        action="activated",
        expected_candidate_digest=cluster_candidate_digest(["trace-1"]),
    )

    stale_version, _, stale_clusters, stale_assignments = _preview(version_id="version-stale")
    stale_version = replace(stale_version, parent_version_id=None)
    registry_storage.insert_cluster_preview(stale_version, [], stale_clusters, stale_assignments)
    registry_storage.insert_cluster_registry_event(
        ClusterRegistryEvent(
            tenant_id="tenant-a",
            action="validated",
            to_version_id="version-stale",
            actor="admin@example.test",
            details_json='{"schema":"validation-report-v1","passed":true}',
        )
    )
    with pytest.raises(ValueError, match="parent conflict"):
        registry_storage.activate_cluster_registry(
            "tenant-a",
            "version-stale",
            expected_generation=1,
            actor="admin@example.test",
            action="activated",
            expected_candidate_digest=cluster_candidate_digest(["trace-1"]),
        )

    registry_storage.rename_cluster_identity(
        "tenant-a", "cluster-billing", "Billing support", actor="admin@example.test"
    )
    [identity] = registry_storage.list_cluster_identities("tenant-a")
    assert identity.display_name == "Billing support"
    assert registry_storage.list_cluster_registry_events("tenant-a")[-1].action == "renamed"


def test_activation_enforces_active_identity_cap_atomically(registry_storage) -> None:
    first = _preview(version_id="version-1")
    registry_storage.insert_cluster_preview(*first)
    registry_storage.insert_cluster_registry_event(
        ClusterRegistryEvent(
            tenant_id="tenant-a",
            action="validated",
            to_version_id="version-1",
            actor="admin",
            details_json='{"passed":true}',
        )
    )
    registry_storage.activate_cluster_registry(
        "tenant-a",
        "version-1",
        expected_generation=0,
        actor="admin",
        action="activated",
        expected_candidate_digest=cluster_candidate_digest(["trace-1"]),
    )

    version, identities, clusters, assignments = _preview(version_id="version-2")
    version = replace(
        version,
        parent_version_id="version-1",
        fit_definition_json=(
            '{"config":{"max_explicit_identities_per_tenant":1,'
            '"max_semantic_identities_per_tenant":5000}}'
        ),
    )
    identities = [
        replace(
            identities[0],
            cluster_id="cluster-shipping",
            explicit_key="shipping",
            display_name="Shipping",
        )
    ]
    clusters = [replace(clusters[0], cluster_id="cluster-shipping")]
    assignments = [replace(assignments[0], trace_id="trace-2", cluster_id="cluster-shipping")]
    registry_storage.insert_cluster_preview(version, identities, clusters, assignments)
    registry_storage.insert_cluster_registry_event(
        ClusterRegistryEvent(
            tenant_id="tenant-a",
            action="validated",
            to_version_id="version-2",
            actor="admin",
            details_json='{"passed":true}',
        )
    )
    with pytest.raises(ValueError, match="identity_limit"):
        registry_storage.activate_cluster_registry(
            "tenant-a",
            "version-2",
            expected_generation=1,
            actor="admin",
            action="activated",
            expected_candidate_digest=cluster_candidate_digest(["trace-2"]),
        )
    assert registry_storage.get_active_cluster_registry("tenant-a").version_id == "version-1"


def test_sqlite_registry_history_rejects_direct_update_and_delete(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "registry.db"))
    version, identities, clusters, assignments = _preview()
    storage.insert_cluster_preview(version, identities, clusters, assignments)
    try:
        with pytest.raises(Exception, match="immutable"):
            storage._conn.execute("UPDATE cluster_registry_versions SET strategy='semantic'")
        with pytest.raises(Exception, match="immutable"):
            storage._conn.execute("DELETE FROM trace_cluster_assignments")
    finally:
        storage.close()


def test_trace_writer_persists_normalized_analysis_fields_across_adapters(
    registry_storage,
) -> None:
    trace = Trace(
        trace_id="trace-analysis",
        tenant_id="tenant-a",
        started_at=datetime.fromisoformat("2026-08-22T01:00:00+02:00"),
        ended_at=datetime.fromisoformat("2026-08-22T01:00:01+02:00"),
        raw_messages=[{"role": "user", "content": "billing"}],
        tags={"verdict.workload": "agent", "verdict.intent_key": "billing"},
    )
    registry_storage.insert_trace(trace)

    stored = registry_storage.get_trace("trace-analysis")
    assert stored is not None
    assert stored.analysis_started_at_state == "valid"
    assert stored.analysis_started_at_us == 1_787_353_200_000_000
    assert stored.analysis_raw_messages_state == "valid"
    assert stored.analysis_raw_messages_utf8_bytes == len(b'[{"content":"billing","role":"user"}]')
    assert registry_storage.cluster_trace_time_bounds(
        "tenant-a", target_workload="agent"
    ) == (1, 1_787_353_200_000_000, 1_787_353_200_000_000)


def test_candidate_projection_preserves_json_type_and_bounds_routing_bodies(
    registry_storage,
) -> None:
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    for trace_id, workload, intent in [
        ("valid", "agent", "billing"),
        ("invalid-workload", 7, "shipping"),
        ("internal", "judge", "internal"),
        ("long-intent", "agent", "x" * 1_000),
        ("null-routing", None, None),
    ]:
        registry_storage.insert_trace(
            Trace(
                trace_id=trace_id,
                tenant_id="tenant-a",
                started_at=started,
                ended_at=started,
                raw_messages=[{"role": "user", "content": trace_id}],
                tags={"verdict.workload": workload, "verdict.intent_key": intent},
            )
        )
    start_us = 1_787_356_800_000_000
    rows = registry_storage.list_cluster_trace_candidates(
        "tenant-a", start_us, start_us + 1, target_workload=None, limit=10
    )

    assert [row.trace_id for row in rows] == [
        "invalid-workload",
        "long-intent",
        "null-routing",
        "valid",
    ]
    assert rows[0].workload_json_type == "number"
    assert rows[1].intent_key_utf8_bytes == 1_000
    assert rows[1].intent_key is None
    assert rows[2].workload_json_type == "null"
    assert rows[2].intent_key_json_type == "null"
    exact = registry_storage.list_cluster_trace_candidates(
        "tenant-a", start_us, start_us + 1, target_workload="agent", limit=10
    )
    assert [row.trace_id for row in exact] == ["long-intent", "valid"]
    assert registry_storage.get_cluster_trace_messages("tenant-a", ["valid"])["valid"] == [
        {"role": "user", "content": "valid"}
    ]


def test_local_scope_maps_missing_and_internal_local_trace_tenants(registry_storage) -> None:
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    registry_storage.insert_trace(
        Trace(
            trace_id="local-trace",
            started_at=started,
            ended_at=started,
            raw_messages=[{"role": "user", "content": "local"}],
            tags={"verdict.workload": "agent"},
        )
    )
    registry_storage.insert_trace(
        Trace(
            trace_id="internal-local-trace",
            tenant_id="__verdict_local__",
            started_at=started,
            ended_at=started,
            raw_messages=[{"role": "user", "content": "internal local"}],
            tags={"verdict.workload": "agent"},
        )
    )
    start_us = 1_787_356_800_000_000
    local = registry_storage.list_cluster_trace_candidates(
        "__verdict_local__",
        start_us,
        start_us + 1,
        target_workload="agent",
        limit=10,
    )
    assert [row.trace_id for row in local] == ["internal-local-trace", "local-trace"]
    assert (
        registry_storage.list_cluster_trace_candidates(
            "tenant-a", start_us, start_us + 1, target_workload="agent", limit=10
        )
        == []
    )


def test_analysis_normalization_is_bounded_resumable_and_idempotent(
    registry_storage,
) -> None:
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    registry_storage.insert_trace(
        Trace(
            trace_id="pending-trace",
            tenant_id="tenant-a",
            started_at=started,
            ended_at=started,
            raw_messages=[{"role": "user", "content": "pending"}],
        )
    )
    if isinstance(registry_storage, InMemoryStorage):
        row = registry_storage._traces["pending-trace"]
        row.analysis_started_at_state = "pending"
        row.analysis_raw_messages_state = "pending"
    else:
        registry_storage._conn.execute(
            "UPDATE traces SET analysis_started_at_state='pending', "
            "analysis_raw_messages_state='pending' WHERE trace_id='pending-trace'"
        )

    assert registry_storage.count_pending_analysis_rows("tenant-a") == 1
    assert registry_storage.normalize_cluster_trace_analysis("tenant-a", limit=1) == 1
    assert registry_storage.count_pending_analysis_rows("tenant-a") == 0
    assert registry_storage.normalize_cluster_trace_analysis("tenant-a", limit=1) == 0


def test_registry_cluster_judgment_reads_follow_versioned_assignment(
    registry_storage,
) -> None:
    registry_storage.insert_trace(Trace(trace_id="trace-1", tenant_id="tenant-a"))
    registry_storage.insert_cluster_preview(*_preview())
    registry_storage.insert_judgment(Judgment(trace_id="trace-1"))

    rows = registry_storage.list_judgments_for_registry_cluster(
        "tenant-a", "version-1", "cluster-billing"
    )
    assert [row.trace_id for row in rows] == ["trace-1"]
    assert (
        registry_storage.list_judgments_for_registry_cluster(
            "tenant-b", "version-1", "cluster-billing"
        )
        == []
    )


def test_candidate_projection_can_page_only_missing_version_assignments(
    registry_storage,
) -> None:
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    for trace_id in ("trace-1", "trace-2"):
        registry_storage.insert_trace(
            Trace(
                trace_id=trace_id,
                tenant_id="tenant-a",
                started_at=started,
                ended_at=started,
                tags={"verdict.workload": "agent", "verdict.intent_key": "billing"},
            )
        )
    registry_storage.insert_cluster_preview(*_preview())
    start_us = 1_787_356_800_000_000

    rows = registry_storage.list_cluster_trace_candidates(
        "tenant-a",
        start_us,
        start_us + 1,
        target_workload="agent",
        limit=10,
        missing_version_id="version-1",
    )

    assert [row.trace_id for row in rows] == ["trace-2"]


def test_explicit_key_cannot_be_remapped_to_another_identity(registry_storage) -> None:
    registry_storage.insert_cluster_preview(*_preview())
    version, identities, clusters, assignments = _preview(version_id="version-2")
    identities = [replace(identities[0], cluster_id="cluster-other")]
    clusters = [replace(clusters[0], cluster_id="cluster-other")]
    assignments = [replace(assignments[0], cluster_id="cluster-other")]
    with pytest.raises(Exception, match=r"conflict|UNIQUE"):
        registry_storage.insert_cluster_preview(version, identities, clusters, assignments)


def test_distance_outlier_requires_one_finite_distance() -> None:
    with pytest.raises(ValueError, match="outlier distance"):
        TraceClusterAssignment(
            tenant_id="tenant-a",
            version_id="version-1",
            trace_id="trace-1",
            origin="fit",
            status="outlier",
            reason="distance",
        )
    with pytest.raises(ValueError, match="outlier distance"):
        TraceClusterAssignment(
            tenant_id="tenant-a",
            version_id="version-1",
            trace_id="trace-1",
            origin="fit",
            status="outlier",
            reason="semantic_fit_too_small",
            distance=0.1,
        )


def test_preview_rejects_active_new_identity_and_cross_version_children(
    registry_storage,
) -> None:
    version, identities, clusters, assignments = _preview()
    with pytest.raises(ValueError, match="preview tenant mismatch"):
        registry_storage.insert_cluster_preview(
            version,
            [replace(identities[0], lifecycle="active")],
            clusters,
            assignments,
        )
    assert registry_storage.get_cluster_registry_version("tenant-a", "version-1") is None

    with pytest.raises(ValueError, match="preview version mismatch"):
        registry_storage.insert_cluster_preview(
            version,
            identities,
            [replace(clusters[0], version_id="another-version")],
            assignments,
        )
    assert registry_storage.get_cluster_registry_version("tenant-a", "version-1") is None

    with pytest.raises(ValueError, match=r"assignment (tenant|scope) mismatch"):
        registry_storage.insert_cluster_preview(
            version,
            identities,
            clusters,
            [replace(assignments[0], version_id="another-version")],
        )
    assert registry_storage.get_cluster_registry_version("tenant-a", "version-1") is None
