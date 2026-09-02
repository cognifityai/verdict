from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import numpy as np
import pytest
from verdict.schema import (
    ClusterIdentity,
    ClusterRegistryEvent,
    ClusterTraceCandidate,
    Trace,
    TraceClusterAssignment,
)
from verdict.storage.memory import InMemoryStorage
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage
from verdict_eval.cli.cluster import _safe_error_code
from verdict_eval.cli.cluster import main as cluster_main
from verdict_eval.cluster_registry import ClusterRegistryService, clustering_strategy_status
from verdict_eval.clustering_strategies import FitConfig


class _SemanticEmbedder:
    dim = 2
    model_name = "test-model"
    model_revision = "v1"
    model_file_sha256 = "abc"

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray([[1.0, index / 100.0] for index, _ in enumerate(texts)])


class _RejectModelWork(_SemanticEmbedder):
    def embed(self, texts: list[str]) -> np.ndarray:
        raise AssertionError("model work must not begin")


def _trace(
    trace_id: str,
    when: datetime,
    *,
    intent_key: str | None = None,
    content: str | None = None,
) -> Trace:
    tags = {"verdict.workload": "agent"}
    if intent_key is not None:
        tags["verdict.intent_key"] = intent_key
    return Trace(
        trace_id=trace_id,
        tenant_id="tenant-a",
        started_at=when,
        ended_at=when + timedelta(seconds=1),
        raw_messages=([{"role": "user", "content": content}] if content is not None else None),
        tags=tags,
    )


def test_explicit_preview_assign_validate_activate_and_new_key_outlier() -> None:
    storage = InMemoryStorage()
    cutoff = datetime(2026, 8, 22, tzinfo=timezone.utc)
    storage.insert_trace(_trace("billing-1", cutoff - timedelta(hours=3), intent_key="billing"))
    storage.insert_trace(_trace("billing-2", cutoff - timedelta(hours=2), intent_key="billing"))
    storage.insert_trace(_trace("shipping-1", cutoff - timedelta(hours=1), intent_key="shipping"))
    service = ClusterRegistryService(storage)

    version = service.fit(
        "tenant-a",
        actor="admin@example.test",
        strategy="explicit",
        cutoff=cutoff,
        config=FitConfig(strategy="explicit", target_workload="agent"),
    )
    assert service.assign("tenant-a", version.version_id, through_cutoff=cutoff) == 0
    report = service.validate("tenant-a", version.version_id, actor="admin@example.test")
    active = service.activate(
        "tenant-a",
        version.version_id,
        expected_generation=0,
        actor="admin@example.test",
    )

    assert report["passed"] is True
    assert active.version_id == version.version_id
    assignments = storage.list_trace_cluster_assignments("tenant-a", version.version_id)
    assert len(assignments) == 3
    assert len({item.cluster_id for item in assignments if item.status == "assigned"}) == 2
    counts = {
        next(
            identity.explicit_key
            for identity in storage.list_cluster_identities("tenant-a")
            if identity.cluster_id == cluster.cluster_id
        ): cluster.member_count
        for cluster in storage.list_cluster_registry_clusters("tenant-a", version.version_id)
    }
    assert counts == {"billing": 2, "shipping": 1}
    identities = storage.list_cluster_identities("tenant-a")
    assert {item.created_by for item in identities} == {"admin@example.test"}
    assert {item.updated_by for item in identities} == {"admin@example.test"}

    later = cutoff + timedelta(hours=1)
    storage.insert_trace(_trace("returns-1", later, intent_key="returns"))
    assigned = service.assign(
        "tenant-a",
        version.version_id,
        through_cutoff=cutoff + timedelta(hours=2),
    )
    assert assigned == 1
    by_trace = {
        item.trace_id: item
        for item in storage.list_trace_cluster_assignments("tenant-a", version.version_id)
    }
    assert by_trace["returns-1"].status == "outlier"
    assert by_trace["returns-1"].reason == "explicit_key_not_in_version"

    # Incremental rows after the immutable fit window remain valid history;
    # they must not invalidate fit-window coverage or make rollback impossible.
    assert service.validate("tenant-a", version.version_id, actor="admin@example.test")["passed"]
    rolled_back = service.rollback(
        "tenant-a",
        version.version_id,
        expected_generation=1,
        actor="admin@example.test",
        through_cutoff=cutoff + timedelta(hours=2),
    )
    assert rolled_back.version_id == version.version_id
    assert rolled_back.generation == 2

    service.rename("tenant-a", identities[0].cluster_id, "Customer billing", actor="editor")
    inspected = service.inspect("tenant-a", version.version_id)
    renamed = {item["cluster_id"]: item["display_name"] for item in inspected["identities"]}
    assert renamed[identities[0].cluster_id] == "Customer billing"
    with pytest.raises(ValueError, match="unknown cluster registry version"):
        service.inspect("tenant-b", version.version_id)


def test_semantic_capture_off_is_persisted_as_ineligible() -> None:
    storage = InMemoryStorage()
    cutoff = datetime(2026, 8, 22, tzinfo=timezone.utc)
    storage.insert_trace(_trace("capture-off", cutoff - timedelta(minutes=1)))
    service = ClusterRegistryService(storage, embedder=_SemanticEmbedder())

    version = service.fit(
        "tenant-a",
        actor="admin@example.test",
        strategy="semantic",
        cutoff=cutoff,
        config=FitConfig(strategy="semantic", min_cluster_size=5),
    )

    [assignment] = storage.list_trace_cluster_assignments("tenant-a", version.version_id)
    assert assignment.status == "ineligible"
    assert assignment.reason == "content_not_captured"
    assert service.assign("tenant-a", version.version_id, through_cutoff=cutoff) == 0


def test_incremental_semantic_assignment_commits_byte_bounded_prefixes() -> None:
    storage = InMemoryStorage()
    cutoff = datetime(2026, 8, 22, tzinfo=timezone.utc)
    storage.insert_trace(_trace("base", cutoff - timedelta(minutes=1), content="base"))
    service = ClusterRegistryService(storage, embedder=_SemanticEmbedder())
    config = FitConfig(
        strategy="semantic",
        min_cluster_size=1,
        max_fit_content_scan_bytes=45,
    )
    version = service.fit(
        "tenant-a",
        actor="admin",
        strategy="semantic",
        cutoff=cutoff,
        config=config,
    )
    for index in range(2):
        storage.insert_trace(
            _trace(
                f"later-{index}",
                cutoff + timedelta(minutes=index + 1),
                content=f"later {index}",
            )
        )

    assert (
        service.assign("tenant-a", version.version_id, through_cutoff=cutoff + timedelta(minutes=3))
        == 1
    )
    assert (
        service.assign("tenant-a", version.version_id, through_cutoff=cutoff + timedelta(minutes=3))
        == 1
    )
    assert {
        item.trace_id
        for item in storage.list_trace_cluster_assignments("tenant-a", version.version_id)
    } == {"base", "later-0", "later-1"}


def test_cluster_cli_emits_versioned_json_without_model_for_explicit(
    tmp_path,
    capsys,
) -> None:
    status = cluster_main(
        [
            "--storage",
            f"sqlite:///{tmp_path / 'cluster.db'}",
            "--tenant",
            "tenant-a",
            "--actor",
            "admin",
            "fit",
            "--strategy",
            "explicit",
            "--cutoff",
            "2026-08-22T00:00:00Z",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["schema"] == "verdict-cluster-v1"
    assert payload["result"]["strategy"] == "explicit"
    assert payload["strategy_status"] == {
        "strategy": "explicit",
        "experimental": False,
        "semantic_component": "none",
    }


def test_cluster_cli_requires_deliberate_strategy_selection(tmp_path) -> None:
    with pytest.raises(SystemExit):
        cluster_main(
            [
                "--storage",
                f"sqlite:///{tmp_path / 'cluster.db'}",
                "--tenant",
                "tenant-a",
                "--actor",
                "admin",
                "fit",
                "--cutoff",
                "2026-08-22T00:00:00Z",
            ]
        )


def test_hybrid_status_discloses_experimental_semantic_fallback() -> None:
    assert clustering_strategy_status("hybrid") == {
        "strategy": "hybrid",
        "experimental": True,
        "semantic_component": "automatic_fallback",
    }


def test_cluster_cli_normalizes_an_upgraded_base_database_before_fit(
    tmp_path,
    capsys,
) -> None:
    db_path = tmp_path / "upgraded-a7.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """CREATE TABLE traces (
        trace_id TEXT PRIMARY KEY,parent_span_id TEXT,started_at TEXT NOT NULL,
        ended_at TEXT,provider TEXT,operation TEXT,request_model TEXT,
        response_model TEXT,input_tokens INTEGER,output_tokens INTEGER,
        temperature REAL,max_tokens INTEGER,finish_reason TEXT,error TEXT,
        latency_ms REAL,prompt_redacted TEXT,response_redacted TEXT,
        raw_messages_json TEXT,tenant_id TEXT,session_id TEXT,user_id_hash TEXT,
        cluster_id TEXT,tags_json TEXT,cost_usd REAL)"""
    )
    connection.execute(
        """INSERT INTO traces (
        trace_id,started_at,ended_at,tenant_id,tags_json,raw_messages_json
        ) VALUES (?,?,?,?,?,?)""",
        (
            "legacy-trace",
            "2026-08-21T23:00:00+00:00",
            "2026-08-21T23:00:01+00:00",
            "tenant-a",
            '{"verdict.intent_key":"billing","verdict.workload":"agent"}',
            '[{"role":"user","content":"billing help"}]',
        ),
    )
    connection.commit()
    connection.close()

    base = [
        "--storage",
        f"sqlite:///{db_path}",
        "--tenant",
        "tenant-a",
        "--actor",
        "admin",
    ]
    assert cluster_main([*base, "normalize", "--limit", "1"]) == 0
    normalized = json.loads(capsys.readouterr().out)
    assert normalized["result"] == {
        "schema": "analysis-normalization-v1",
        "processed": 1,
        "pending": 0,
        "complete": True,
    }
    assert (
        cluster_main(
            [
                *base,
                "fit",
                "--strategy",
                "explicit",
                "--target-workload",
                "agent",
                "--cutoff",
                "2026-08-22T00:00:00Z",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"]["strategy"] == "explicit"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ValueError("analysis_index_pending"), "analysis_index_pending"),
        (ValueError("fit_candidate_limit"), "fit_candidate_limit"),
        (ValueError("model_unavailable"), "model_unavailable"),
        (ValueError("identity_limit"), "identity_limit"),
        (ValueError("cluster registry generation conflict"), "generation_conflict"),
        (ValueError("sensitive provider detail"), "invalid_request"),
        (RuntimeError("sensitive provider detail"), "internal_error"),
    ],
)
def test_cluster_cli_uses_closed_safe_error_codes(exc: Exception, code: str) -> None:
    assert _safe_error_code(exc) == code


def test_inspect_discloses_experimental_semantic_strategy() -> None:
    storage = InMemoryStorage()
    cutoff = datetime(2026, 8, 22, tzinfo=timezone.utc)
    for index in range(5):
        storage.insert_trace(
            _trace(
                f"semantic-{index}",
                cutoff - timedelta(minutes=index + 1),
                content=f"billing {index}",
            )
        )
    service = ClusterRegistryService(storage, embedder=_SemanticEmbedder())
    version = service.fit(
        "tenant-a",
        actor="admin",
        strategy="semantic",
        cutoff=cutoff,
        config=FitConfig(strategy="semantic", min_cluster_size=5),
    )

    assert service.inspect("tenant-a", version.version_id)["strategy_status"] == {
        "strategy": "semantic",
        "experimental": True,
        "semantic_component": "automatic",
    }


def test_preview_reports_all_candidate_branches_and_ineligible_reasons() -> None:
    storage = InMemoryStorage()
    cutoff = datetime(2026, 8, 22, tzinfo=timezone.utc)
    storage.insert_trace(_trace("missing-key", cutoff - timedelta(minutes=3)))
    storage.insert_trace(_trace("billing-1", cutoff - timedelta(minutes=2), intent_key="billing"))
    storage.insert_trace(_trace("billing-2", cutoff - timedelta(minutes=1), intent_key="billing"))
    service = ClusterRegistryService(storage)

    version = service.fit(
        "tenant-a",
        actor="admin",
        strategy="explicit",
        cutoff=cutoff,
        config=FitConfig(strategy="explicit", target_workload="agent"),
    )
    preview = json.loads(version.preview_report_json)

    assert preview["candidate_summary"] == {
        "schema": "candidate-summary-v1",
        "candidate_count": 3,
        "branches": {"explicit": 2, "semantic": 0, "not_selected": 0},
        "ineligible_count": 1,
        "ineligible_reasons": {"missing_intent_key": 1},
        "fit_evidence_count": 1,
    }
    service.assign("tenant-a", version.version_id, through_cutoff=cutoff)
    assert service.validate("tenant-a", version.version_id, actor="admin")["passed"]


def test_inspect_bounds_incremental_assignments_and_events() -> None:
    storage = InMemoryStorage()
    cutoff = datetime(2026, 8, 22, tzinfo=timezone.utc)
    storage.insert_trace(_trace("billing", cutoff - timedelta(minutes=1), intent_key="billing"))
    service = ClusterRegistryService(storage)
    version = service.fit(
        "tenant-a",
        actor="admin",
        strategy="explicit",
        cutoff=cutoff,
        config=FitConfig(strategy="explicit"),
    )
    cluster_id = storage.list_cluster_registry_clusters("tenant-a", version.version_id)[
        0
    ].cluster_id
    storage.insert_trace_cluster_assignments(
        "tenant-a",
        [
            TraceClusterAssignment(
                "tenant-a",
                version.version_id,
                f"incremental-{index:04d}",
                "incremental",
                "assigned",
                cluster_id,
                "explicit",
            )
            for index in range(501)
        ],
    )
    for index in range(101):
        storage.insert_cluster_registry_event(
            ClusterRegistryEvent(
                "tenant-a",
                event_id=f"event-{index:04d}",
                action="validated",
                to_version_id=version.version_id,
                actor="admin",
            )
        )

    inspected = service.inspect("tenant-a", version.version_id)

    assert len(inspected["assignments"]) == 500
    assert len(inspected["events"]) == 100
    assert inspected["truncated"] == {
        "identities": False,
        "assignments": True,
        "events": True,
    }
    next_page = service.inspect(
        "tenant-a",
        version.version_id,
        assignment_limit=2,
        assignment_offset=500,
        event_limit=2,
        event_offset=100,
    )
    assert [item["trace_id"] for item in next_page["assignments"]] == [
        "incremental-0499",
        "incremental-0500",
    ]
    assert [item["event_id"] for item in next_page["events"]] == ["event-0100"]
    assert next_page["page"]["assignment_offset"] == 500


def test_sqlite_fit_reads_metadata_and_bodies_from_one_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    storage = SQLiteStorage(str(tmp_path / "snapshot.db"))
    cutoff = datetime(2026, 8, 22, tzinfo=timezone.utc)
    for index in range(5):
        storage.insert_trace(
            _trace(
                f"trace-{index}",
                cutoff - timedelta(minutes=index + 1),
                content=f"billing {index}",
            )
        )
    original = storage.get_cluster_trace_messages
    deletion: list[threading.Thread] = []

    def fetch(tenant, trace_ids):
        worker = threading.Thread(target=storage.delete_trace, args=("trace-0",))
        deletion.append(worker)
        worker.start()
        time.sleep(0.02)
        assert worker.is_alive()
        return original(tenant, trace_ids)

    monkeypatch.setattr(storage, "get_cluster_trace_messages", fetch)
    version = ClusterRegistryService(storage, embedder=_SemanticEmbedder()).fit(
        "tenant-a",
        actor="admin",
        strategy="semantic",
        cutoff=cutoff,
        config=FitConfig(strategy="semantic", min_cluster_size=5),
    )
    deletion[0].join(timeout=1)
    assert not deletion[0].is_alive()
    assert "trace-0" in {
        item.trace_id
        for item in storage.list_trace_cluster_assignments("tenant-a", version.version_id)
    }
    storage.close()


def test_metadata_cap_counts_an_oversized_routing_value_without_its_body() -> None:
    base = ClusterTraceCandidate(
        trace_id_utf8_bytes=7,
        trace_id="trace-1",
        tenant_id="tenant-a",
        started_at_us=1,
        workload_json_type="string",
        workload_utf8_bytes=5,
        workload="agent",
        intent_key_json_type="string",
        intent_key_utf8_bytes=1_000,
        intent_key=None,
        raw_messages_state="missing",
        raw_messages_utf8_bytes=None,
    )
    without_long_value = replace(base, intent_key_utf8_bytes=0)

    assert (
        ClusterRegistryService._metadata_size([base])
        - ClusterRegistryService._metadata_size([without_long_value])
        == 1_000
    )


def test_corrupt_active_identity_count_fails_before_model_work() -> None:
    storage = InMemoryStorage()
    for key in ("billing", "shipping"):
        identity = ClusterIdentity(
            tenant_id="tenant-a",
            cluster_id=f"cluster-{key}",
            kind="explicit",
            lifecycle="active",
            explicit_key=key,
            display_name=key,
        )
        storage._cluster_identities[("tenant-a", identity.cluster_id)] = identity
    service = ClusterRegistryService(storage, embedder=_RejectModelWork())

    with pytest.raises(ValueError, match="identity_limit"):
        service.fit(
            "tenant-a",
            actor="admin",
            strategy="semantic",
            cutoff=datetime.now(timezone.utc),
            config=FitConfig(
                strategy="semantic",
                max_explicit_identities_per_tenant=1,
            ),
        )


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_activation_uses_reviewed_fit_membership_when_historical_trace_arrives_late(
    adapter: str,
    tmp_path,
    monkeypatch,
) -> None:
    storage = (
        InMemoryStorage()
        if adapter == "memory"
        else SQLiteStorage(str(tmp_path / "activation-race.db"))
    )
    cutoff = datetime(2026, 8, 22, tzinfo=timezone.utc)
    storage.insert_trace(_trace("billing-1", cutoff - timedelta(minutes=2), intent_key="billing"))
    service = ClusterRegistryService(storage)
    version = service.fit(
        "tenant-a",
        actor="admin",
        strategy="explicit",
        cutoff=cutoff,
        config=FitConfig(strategy="explicit", target_workload="agent"),
    )
    original = storage.activate_cluster_registry

    def insert_late_candidate(*args, **kwargs):
        storage.insert_trace(
            _trace("billing-late", cutoff - timedelta(minutes=1), intent_key="billing")
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "activate_cluster_registry", insert_late_candidate)
    try:
        active = service.activate(
            "tenant-a",
            version.version_id,
            expected_generation=0,
            actor="admin",
        )
        assert active.version_id == version.version_id
        assert service.assign(
            "tenant-a", version.version_id, through_cutoff=cutoff
        ) == 1
        late = next(
            item
            for item in storage.list_trace_cluster_assignments(
                "tenant-a", version.version_id
            )
            if item.trace_id == "billing-late"
        )
        assert late.origin == "incremental"
        assert late.status == "assigned"
    finally:
        storage.close()


@pytest.mark.skipif(
    not os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
    reason="no disposable live Postgres DSN",
)
def test_live_postgres_service_fit_validate_and_activate() -> None:
    tenant = f"registry-service-{uuid4().hex}"
    cutoff = datetime.now(timezone.utc)
    storage = PostgresStorage(os.environ["VERDICT_TEST_POSTGRES_DSN"])
    try:
        storage.insert_trace(
            Trace(
                trace_id=f"trace-{uuid4().hex}",
                tenant_id=tenant,
                started_at=cutoff - timedelta(seconds=1),
                ended_at=cutoff,
                tags={"verdict.workload": "agent", "verdict.intent_key": "billing"},
            )
        )
        service = ClusterRegistryService(storage)
        version = service.fit(
            tenant,
            actor="admin",
            strategy="explicit",
            cutoff=cutoff,
            config=FitConfig(strategy="explicit", target_workload="agent"),
        )

        assert service.validate(tenant, version.version_id, actor="admin")["passed"]
        pointer = service.activate(
            tenant,
            version.version_id,
            expected_generation=0,
            actor="admin",
        )
        assert pointer.version_id == version.version_id
        inspected = service.inspect(
            tenant,
            version.version_id,
            identity_limit=1,
            assignment_limit=1,
            event_limit=1,
        )
        assert len(inspected["identities"]) == 1
        assert len(inspected["assignments"]) == 1
        assert len(inspected["events"]) == 1
    finally:
        storage.close()
