from datetime import datetime, timedelta, timezone

import pytest
from verdict.monitor_inputs import load_monitor_units
from verdict.monitoring import MonitorPolicy
from verdict.schema import (
    ClusterIdentity,
    ClusterRegistryCluster,
    ClusterRegistryVersion,
    DimensionScore,
    Judgment,
    Trace,
    TraceClusterAssignment,
    Verdict,
)
from verdict.storage import InMemoryStorage, SQLiteStorage
from verdict_eval.cluster_registry import ClusterRegistryService
from verdict_eval.clustering_strategies import FitConfig

NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)
TENANT = "__verdict_local__"


@pytest.fixture(params=["memory", "sqlite"])
def storage(request, tmp_path):
    value = (
        InMemoryStorage()
        if request.param == "memory"
        else SQLiteStorage(str(tmp_path / "monitor-inputs.db"))
    )
    try:
        yield value
    finally:
        value.close()


def _insert_registry(storage, version_id: str, cluster_id: str) -> None:
    identity = ClusterIdentity(
        tenant_id=TENANT, cluster_id=cluster_id, kind="explicit",
        explicit_key=cluster_id, display_name=cluster_id,
    )
    storage.insert_cluster_preview(
        ClusterRegistryVersion(
            tenant_id=TENANT, version_id=version_id,
            strategy="explicit", cutoff=NOW,
        ),
        [identity],
        [ClusterRegistryCluster(
            tenant_id=TENANT, version_id=version_id,
            cluster_id=cluster_id, kind="explicit", member_count=1,
        )],
        [TraceClusterAssignment(
            tenant_id=TENANT, version_id=version_id,
            trace_id="trace-a", origin="fit", status="assigned",
            cluster_id=cluster_id, cluster_kind="explicit",
        )],
    )


def test_input_projection_uses_one_evaluator_and_frozen_cluster_version(storage) -> None:
    selected = "a" * 64
    storage.insert_trace(Trace(
        trace_id="trace-a", tenant_id=None, started_at=NOW,
        provider="openai", request_model="model", response_redacted="ok",
    ))
    storage.insert_trace(Trace(
        trace_id="foreign", tenant_id="tenant-b", started_at=NOW,
        response_redacted="foreign",
    ))
    storage.insert_judgment(Judgment(
        judgment_id="selected", trace_id="trace-a",
        evaluator_provider="anthropic", judge_models=["judge"],
        evaluator_fingerprint=selected, expected_dimensions=["quality"],
        dimensions=[DimensionScore("quality", Verdict.PASS)],
    ))
    storage.insert_judgment(Judgment(
        judgment_id="foreign", trace_id="foreign",
        evaluator_provider="anthropic", judge_models=["judge"],
        evaluator_fingerprint=selected, expected_dimensions=["quality"],
        dimensions=[DimensionScore("quality", Verdict.FAIL)],
    ))
    storage.insert_judgment(Judgment(
        judgment_id="other", trace_id="trace-a",
        evaluator_provider="anthropic", judge_models=["other"],
        evaluator_fingerprint="b" * 64, expected_dimensions=["quality"],
        dimensions=[DimensionScore("quality", Verdict.FAIL)],
    ))
    _insert_registry(storage, "registry-1", "cluster-old")
    _insert_registry(storage, "registry-2", "cluster-new")
    policy = MonitorPolicy(
        "policy", "scope", grouping_mode="cluster",
        evaluator_fingerprint=selected, evaluator_dimensions=("quality",),
        cluster_registry_version_id="registry-1",
    )

    units = load_monitor_units(storage, policy, tenant_id=TENANT)

    assert len(units) == 1
    assert units[0].unit_id == "trace-a"
    assert units[0].group_id == "cluster-old"
    assert units[0].metrics["judge.quality.pass"] is True


def test_input_projection_fails_when_frozen_inputs_cannot_be_resolved(storage) -> None:
    evaluator_policy = MonitorPolicy(
        "evaluator", "scope", evaluator_fingerprint="a" * 64,
        evaluator_dimensions=("quality",),
    )
    cluster_policy = MonitorPolicy(
        "cluster", "scope", grouping_mode="cluster",
        cluster_registry_version_id="missing",
    )
    legacy_cluster_policy = MonitorPolicy(
        "legacy", "scope", grouping_mode="cluster",
    )

    with pytest.raises(ValueError, match="selected evaluator is unavailable"):
        load_monitor_units(storage, evaluator_policy, tenant_id=TENANT)
    with pytest.raises(ValueError, match="frozen cluster registry is unavailable"):
        load_monitor_units(storage, cluster_policy, tenant_id=TENANT)
    with pytest.raises(ValueError, match="requires a frozen registry"):
        load_monitor_units(storage, legacy_cluster_policy, tenant_id=TENANT)


def test_cluster_monitor_projects_new_traces_through_its_pinned_version(storage) -> None:
    storage.insert_trace(
        Trace(
            trace_id="historical",
            tenant_id=TENANT,
            started_at=NOW - timedelta(hours=1),
            ended_at=NOW - timedelta(minutes=59),
            response_redacted="ok",
            tags={"verdict.intent_key": "billing"},
        )
    )
    service = ClusterRegistryService(storage)
    version = service.fit(
        TENANT,
        actor="test",
        strategy="explicit",
        cutoff=NOW,
        config=FitConfig(strategy="explicit"),
    )
    storage.insert_trace(
        Trace(
            trace_id="new",
            tenant_id=TENANT,
            started_at=NOW + timedelta(hours=1),
            ended_at=NOW + timedelta(hours=1, seconds=1),
            response_redacted="ok",
            tags={"verdict.intent_key": "billing"},
        )
    )
    policy = MonitorPolicy(
        "policy",
        "scope",
        grouping_mode="cluster",
        cluster_registry_version_id=version.version_id,
    )

    units = load_monitor_units(storage, policy, tenant_id=TENANT)

    by_id = {unit.unit_id: unit for unit in units}
    assert by_id["historical"].group_id is not None
    assert by_id["new"].group_id == by_id["historical"].group_id
    assert storage.list_trace_cluster_assignments(TENANT, version.version_id)[-1].origin in {
        "fit",
        "incremental",
    }


def test_explicit_cluster_projection_does_not_require_a_semantic_model(
    storage,
    monkeypatch,
) -> None:
    storage.insert_trace(
        Trace(
            trace_id="historical",
            tenant_id=TENANT,
            started_at=NOW - timedelta(hours=1),
            ended_at=NOW - timedelta(minutes=59),
            response_redacted="ok",
            tags={"verdict.intent_key": "billing"},
        )
    )
    service = ClusterRegistryService(storage)
    version = service.fit(
        TENANT,
        actor="test",
        strategy="explicit",
        cutoff=NOW,
        config=FitConfig(strategy="explicit"),
    )
    storage.insert_trace(
        Trace(
            trace_id="new",
            tenant_id=TENANT,
            started_at=NOW + timedelta(hours=1),
            ended_at=NOW + timedelta(hours=1, seconds=1),
            response_redacted="ok",
            tags={"verdict.intent_key": "billing"},
        )
    )
    monkeypatch.setenv("VERDICT_CLUSTER_MODEL_PATH", "/does/not/exist")
    policy = MonitorPolicy(
        "policy",
        "scope",
        grouping_mode="cluster",
        cluster_registry_version_id=version.version_id,
    )

    units = load_monitor_units(storage, policy, tenant_id=TENANT)

    assert {unit.group_id for unit in units} != {None}
