from datetime import datetime, timedelta, timezone

import pytest
from verdict.monitor_inputs import MonitorProjectionPending, advance_monitor, load_monitor_units
from verdict.monitoring import (
    MonitorPolicy,
    MonitorStateConflict,
    compare_manifest,
    plan_historical_manifest,
    plan_prospective_manifest,
)
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
        ended_at=NOW + timedelta(seconds=1),
        provider="openai", request_model="model",
        prompt_redacted="request", response_redacted="ok",
    ))
    storage.insert_trace(Trace(
        trace_id="foreign", tenant_id="tenant-b", started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
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

    assert load_monitor_units(storage, evaluator_policy, tenant_id=TENANT) == ()
    with pytest.raises(ValueError, match="frozen cluster registry is unavailable"):
        load_monitor_units(storage, cluster_policy, tenant_id=TENANT)
    with pytest.raises(ValueError, match="requires a frozen registry"):
        load_monitor_units(storage, legacy_cluster_policy, tenant_id=TENANT)


def test_established_evaluator_treats_new_eligible_trace_as_pending(storage) -> None:
    fingerprint = "a" * 64
    storage.insert_trace(Trace(
        trace_id="pending", tenant_id=TENANT, started_at=NOW,
        ended_at=NOW + timedelta(seconds=1), prompt_redacted="request",
        response_redacted="response",
    ))
    policy = MonitorPolicy(
        "evaluator", "scope", evaluator_fingerprint=fingerprint,
        evaluator_dimensions=("quality",),
    )

    [unit] = load_monitor_units(storage, policy, tenant_id=TENANT)

    assert unit.evaluator_state == "pending"
    assert unit.metric_states["judge.quality.pass"] == "missing"


def test_completed_judgment_is_not_used_after_response_evidence_disappears(storage) -> None:
    fingerprint = "a" * 64
    storage.insert_trace(Trace(
        trace_id="missing-response", tenant_id=TENANT, started_at=NOW,
        ended_at=NOW + timedelta(seconds=1), prompt_redacted="request",
    ))
    storage.insert_judgment(Judgment(
        judgment_id="stale", trace_id="missing-response",
        evaluator_provider="anthropic", judge_models=["judge"],
        evaluator_fingerprint=fingerprint, expected_dimensions=["quality"],
        dimensions=[DimensionScore("quality", Verdict.PASS)],
    ))
    policy = MonitorPolicy(
        "evaluator", "scope", evaluator_fingerprint=fingerprint,
        evaluator_dimensions=("quality",),
    )

    [unit] = load_monitor_units(storage, policy, tenant_id=TENANT)

    assert unit.evaluator_state == "not_evaluable"
    assert unit.metric_states["judge.quality.pass"] == "missing"
    assert "judge.quality.pass" not in unit.metrics


def test_in_flight_trace_is_not_a_monitor_unit(storage) -> None:
    storage.insert_trace(Trace(
        trace_id="in-flight", tenant_id=TENANT, started_at=NOW,
        prompt_redacted="request", response_redacted="partial",
    ))

    assert load_monitor_units(
        storage, MonitorPolicy("policy", "scope"), tenant_id=TENANT,
    ) == ()


def test_internal_judge_and_replay_traces_are_not_monitor_units(storage) -> None:
    for workload in ("judge", "paired_replay"):
        storage.insert_trace(Trace(
            trace_id=workload, tenant_id=TENANT, started_at=NOW,
            ended_at=NOW + timedelta(seconds=1),
            tags={"verdict.workload": workload},
        ))

    assert load_monitor_units(
        storage, MonitorPolicy("policy", "scope"), tenant_id=TENANT,
    ) == ()


def test_canonical_input_projection_counts_one_unit_per_session(storage) -> None:
    for index in range(2):
        storage.insert_trace(Trace(
            trace_id=f"trace-{index}", tenant_id=TENANT,
            session_id="shared-session", started_at=NOW + timedelta(seconds=index),
            ended_at=NOW + timedelta(seconds=index + 1),
            response_redacted="ok" if index == 0 else "",
        ))
    policy = MonitorPolicy("session-policy", "scope", analysis_unit="session")

    [unit] = load_monitor_units(storage, policy, tenant_id=TENANT)

    assert unit.unit_id.startswith("session:")
    assert unit.metrics["response_empty"] is True
    assert unit.ingest_sequence == 1


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


def test_cluster_monitor_finishes_more_than_one_assignment_batch() -> None:
    storage = InMemoryStorage()
    try:
        storage.insert_trace(Trace(
            trace_id="historical", tenant_id=TENANT,
            started_at=NOW - timedelta(hours=1),
            ended_at=NOW - timedelta(minutes=59),
            prompt_redacted="billing", response_redacted="ok",
            tags={"verdict.intent_key": "billing"},
        ))
        service = ClusterRegistryService(storage)
        version = service.fit(
            TENANT, actor="test", strategy="explicit", cutoff=NOW,
            config=FitConfig(strategy="explicit"),
        )
        for index in range(10_200):
            started = NOW + timedelta(seconds=index + 1)
            storage.insert_trace(Trace(
                trace_id=f"new-{index:05d}", tenant_id=TENANT,
                started_at=started, ended_at=started + timedelta(milliseconds=1),
                prompt_redacted="billing", response_redacted="ok",
                tags={"verdict.intent_key": "billing"},
            ))
        policy = MonitorPolicy(
            "policy", "scope", grouping_mode="cluster",
            cluster_registry_version_id=version.version_id,
        )

        units = load_monitor_units(storage, policy, tenant_id=TENANT)

        assert len(units) == 10_201
        assert all(unit.group_id is not None for unit in units)
        assert len(storage.list_trace_cluster_assignments(
            TENANT, version.version_id, limit=20_000,
        )) == 10_201
    finally:
        storage.close()


def test_cluster_projection_exhaustion_is_typed_and_does_not_freeze_units(
    storage,
    monkeypatch,
) -> None:
    _insert_registry(storage, "registry", "cluster")
    storage.insert_trace(Trace(
        trace_id="new", tenant_id=TENANT,
        started_at=NOW + timedelta(seconds=1),
        ended_at=NOW + timedelta(seconds=2),
        prompt_redacted="billing", response_redacted="ok",
        tags={"verdict.intent_key": "billing"},
    ))

    class SlowProjection:
        @staticmethod
        def assign(*_args, **_kwargs):
            return 1

    monkeypatch.setattr(
        "verdict.monitor_inputs.cluster_registry_service",
        lambda *_args, **_kwargs: SlowProjection(),
    )
    monkeypatch.setattr("verdict.monitor_inputs.MAX_CLUSTER_PROJECTION_PASSES", 2)
    policy = MonitorPolicy(
        "policy", "scope", grouping_mode="cluster",
        cluster_registry_version_id="registry",
    )

    with pytest.raises(MonitorProjectionPending, match="still processing"):
        load_monitor_units(storage, policy, tenant_id=TENANT)


def test_losing_monitor_runner_returns_winner_without_advancing_another_cohort(
    monkeypatch,
) -> None:
    storage = InMemoryStorage()
    try:
        policy = MonitorPolicy(
            "policy", "scope", reference_ratio=0.5,
            minimum_reference=1, minimum_current=1, prospective_target=1,
            analysis_unit="session",
        )
        for index in range(2):
            started = NOW + timedelta(seconds=index)
            storage.insert_trace(Trace(
                trace_id=f"historical-{index}", tenant_id=TENANT,
                session_id=f"historical-session-{index}",
                started_at=started, ended_at=started + timedelta(milliseconds=1),
            ))
        units = load_monitor_units(storage, policy, tenant_id=TENANT)
        historical = plan_historical_manifest(
            units, policy, cutoff=NOW + timedelta(seconds=2),
        )
        storage.save_monitor_candidate(
            policy, historical, compare_manifest(units, historical, policy),
        )
        prepared = plan_prospective_manifest(
            historical, (), policy, prospective_start_at=NOW + timedelta(seconds=2),
            prospective_start_sequence=storage.trace_ingest_watermark(),
        )
        storage.save_monitor_successor(
            policy.policy_id,
            historical.snapshot_id,
            prepared,
            compare_manifest((), prepared, policy),
            expected_state="candidate",
        )
        storage.activate_monitor_policy(
            policy.scope_key, policy.policy_id, expected_active_policy_id=None,
        )
        storage.insert_trace(Trace(
            trace_id="new", tenant_id=TENANT,
            session_id="new-session",
            started_at=NOW + timedelta(seconds=3),
            ended_at=NOW + timedelta(seconds=4),
        ))
        expected_unit_id = load_monitor_units(storage, policy, tenant_id=TENANT)[-1].unit_id
        save_successor = storage.save_monitor_successor

        def lose_to_equivalent_winner(*args, **kwargs):
            save_successor(*args, **kwargs)
            raise MonitorStateConflict("monitor snapshot changed")

        monkeypatch.setattr(storage, "save_monitor_successor", lose_to_equivalent_winner)

        manifest, _comparison = advance_monitor(storage, policy, tenant_id=TENANT)

        assert manifest.current_unit_ids == (expected_unit_id,)
        assert manifest.prospective_open is False
        assert storage.get_latest_monitor_snapshot(policy.policy_id)[0] == manifest
    finally:
        storage.close()
