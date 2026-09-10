"""Live PostgreSQL adapter contract tests.

Skipped unless VERDICT_TEST_POSTGRES_DSN points at a disposable database. CI
provides an ephemeral PostgreSQL service; these are not mocked SQL tests.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
import verdict
import verdict.client as client_module
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.analysis_records import (
    AnalysisRunStatus,
    DeliveryOutcome,
    DeterministicAnalysisRun,
    NotificationDeliveryAttempt,
)
from verdict.dashboard.analysis_service import read_latest_analysis, run_analysis
from verdict.dashboard.app import build_agent_run_detail
from verdict.evidence import AgentCaptureBatch
from verdict.instrumentors.base import apply_routing_context, persist_trace
from verdict.monitor_inputs import load_monitor_units
from verdict.monitoring import (
    AnalysisUnitRecord,
    MonitorPolicy,
    MonitorStatus,
    compare_manifest,
    plan_historical_manifest,
    plan_prospective_manifest,
)
from verdict.schema import (
    ClusterIdentity,
    ClusterRegistryCluster,
    ClusterRegistryEvent,
    ClusterRegistryVersion,
    DimensionScore,
    DriftDirection,
    DriftRun,
    DriftSignal,
    EvaluatorHealthRecord,
    EvaluatorHealthStatus,
    Judgment,
    JudgmentStatus,
    SpanRecord,
    Trace,
    TraceClusterAssignment,
    UserSignalRecord,
    Verdict,
    cluster_candidate_digest,
    datetime_to_utc_us,
)
from verdict.storage import BufferedStorage
from verdict.storage.postgres import PostgresStorage
from verdict.trace import span
from verdict_eval.cluster_registry import ClusterRegistryService
from verdict_eval.clustering_strategies import FitConfig

DSN, POSTGRES_SKIP_REASON = validate_test_dsn(
    os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
    allow_any_database=os.environ.get("VERDICT_TEST_POSTGRES_ALLOW_ANY_DB") == "1",
)
if os.environ.get("VERDICT_REQUIRE_POSTGRES_TESTS") == "1" and DSN is None:
    raise RuntimeError(
        "VERDICT_REQUIRE_POSTGRES_TESTS=1 but live PostgreSQL tests are unsafe: "
        f"{POSTGRES_SKIP_REASON}"
    )

pytestmark = [
    pytest.mark.skipif(DSN is None, reason=POSTGRES_SKIP_REASON),
    pytest.mark.filterwarnings("error::DeprecationWarning:psycopg_pool.*"),
]


@contextmanager
def _isolated_postgres_storage():
    with isolated_test_dsn(DSN) as scoped_dsn:
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=2)
        try:
            yield storage
        finally:
            storage.close()


def test_live_postgres_local_scope_includes_only_tenantless_and_local_traces():
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    traces = [
        Trace(
            trace_id=trace_id,
            tenant_id=tenant_id,
            started_at=now,
            ended_at=now + timedelta(seconds=1),
            raw_messages=[{"role": "user", "content": trace_id}],
            tags={"verdict.workload": "agent"},
        )
        for trace_id, tenant_id in (
            ("tenantless", None),
            ("explicit-local", "__verdict_local__"),
            ("unrelated", "other"),
        )
    ]
    with _isolated_postgres_storage() as storage:
        for trace in traces:
            storage.insert_trace(trace)
        storage._exec(
            "UPDATE traces SET analysis_started_at_state='pending', "
            "analysis_raw_messages_state='pending'",
            (),
        )

        assert storage.count_pending_analysis_rows("__verdict_local__") == 2
        assert storage.normalize_cluster_trace_analysis("__verdict_local__") == 2
        assert {
            trace.trace_id
            for trace in storage.list_traces(tenant_id="__verdict_local__")
        } == {"tenantless", "explicit-local"}
        assert storage.cluster_trace_time_bounds(
            "__verdict_local__", target_workload="agent"
        )[0] == 2
        candidates = storage.list_cluster_trace_candidates(
            "__verdict_local__",
            datetime_to_utc_us(now - timedelta(seconds=1)),
            datetime_to_utc_us(now + timedelta(seconds=2)),
            target_workload="agent",
            limit=10,
        )
        assert {candidate.trace_id for candidate in candidates} == {
            "tenantless",
            "explicit-local",
        }
        assert set(
            storage.get_cluster_trace_messages(
                "__verdict_local__", [trace.trace_id for trace in traces]
            )
        ) == {"tenantless", "explicit-local"}


def test_live_postgres_analysis_and_delivery_contracts():
    suffix = uuid4().hex
    tenant = f"analysis-{suffix}"
    now = datetime.now(timezone.utc)
    run = DeterministicAnalysisRun(
        analysis_id=uuid4().hex * 2,
        tenant_id=tenant,
        scope_key="agent-and-trace",
        cutoff=now,
        completed_at=now + timedelta(seconds=1),
        status=AnalysisRunStatus.COMPLETED,
        analyzer_version="agent-insights-v1",
        input_fingerprint=uuid4().hex * 2,
        result={"scope": {"runs": 0}, "findings": []},
    )
    notification_id = uuid4().hex * 2
    destination = uuid4().hex * 2
    failed = NotificationDeliveryAttempt(
        attempt_id=uuid4().hex * 2,
        notification_id=notification_id,
        tenant_id=tenant,
        source_kind="analysis",
        source_id=run.analysis_id,
        destination_fingerprint=destination,
        attempted_at=now,
        outcome=DeliveryOutcome.FAILED,
        payload={"kind": "finding", "code": "tool_error", "runs": 0},
        http_status=503,
        error_code="http_rejected",
    )
    delivered = NotificationDeliveryAttempt(
        attempt_id=uuid4().hex * 2,
        notification_id=notification_id,
        tenant_id=tenant,
        source_kind="analysis",
        source_id=run.analysis_id,
        destination_fingerprint=destination,
        attempted_at=now + timedelta(seconds=1),
        outcome=DeliveryOutcome.DELIVERED,
        payload=failed.payload,
        http_status=204,
    )
    storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
    try:
        storage.save_deterministic_analysis_run(run)
        storage.save_deterministic_analysis_run(run)
        assert storage.get_latest_deterministic_analysis_run(
            tenant, "agent-and-trace"
        ) == run
        assert storage.get_latest_deterministic_analysis_run(
            f"other-{tenant}", "agent-and-trace"
        ) is None
        assert storage.get_latest_deterministic_analysis_run(
            tenant, "agent-and-trace", analyzer_version="agent-insights-v1"
        ) == run

        storage.save_notification_delivery_attempt(failed)
        storage.save_notification_delivery_attempt(delivered)
        storage.save_notification_delivery_attempt(delivered)
        assert storage.list_notification_delivery_attempts(
            notification_id, destination
        ) == [delivered, failed]
        assert storage.notification_was_delivered(notification_id, destination)
        assert storage.list_notification_delivery_attempts_for_tenant(tenant) == [
            delivered, failed
        ]
    finally:
        storage._exec(
            "DELETE FROM notification_delivery_attempts WHERE tenant_id=%s", (tenant,)
        )
        storage._exec(
            "DELETE FROM deterministic_analysis_runs WHERE tenant_id=%s", (tenant,)
        )
        storage.close()


def test_live_postgres_current_analysis_is_reused_after_rollback_version():
    with isolated_test_dsn(DSN) as scoped_dsn:
        tenant = f"analysis-rollback-{uuid4().hex}"
        fingerprint = uuid4().hex * 2

        def build():
            return {
                "schema": "agent-insights-v2",
                "sourceActivity": [],
                "_analysisInputFingerprint": fingerprint,
            }

        first = run_analysis(scoped_dsn, tenant=tenant, build=build)
        rollback_time = datetime.now(timezone.utc) + timedelta(days=1)
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.save_deterministic_analysis_run(DeterministicAnalysisRun(
            analysis_id=uuid4().hex * 2,
            tenant_id=tenant,
            scope_key="agent-and-trace",
            cutoff=rollback_time,
            completed_at=rollback_time,
            status=AnalysisRunStatus.COMPLETED,
            analyzer_version="agent-insights-v1",
            input_fingerprint=fingerprint,
            result={"schema": "agent-insights-v1", "comparisons": []},
        ))
        storage.close()

        second = run_analysis(scoped_dsn, tenant=tenant, build=build)
        current = read_latest_analysis(
            scoped_dsn,
            tenant=tenant,
            empty_result={"schema": "agent-insights-v2", "sourceActivity": []},
        )

    assert second == first
    assert current == first


def test_live_postgres_versioned_registry_and_analysis_normalization():
    suffix = uuid4().hex
    tenant = f"registry-{suffix}"
    version_id = f"version-{suffix}"
    cluster_id = f"cluster-{suffix}"
    trace_id = f"trace-{suffix}"
    now = datetime.now(timezone.utc)
    with _isolated_postgres_storage() as storage:
        storage.insert_trace(
            Trace(
                trace_id=trace_id, tenant_id=tenant, started_at=now, ended_at=now,
                raw_messages=[{"role": "user", "content": "billing"}],
                tags={"verdict.workload": "agent", "verdict.intent_key": "billing"},
            )
        )
        storage._exec(
            "UPDATE traces SET analysis_started_at_state='pending', "
            "analysis_raw_messages_state='pending' WHERE trace_id=%s",
            (trace_id,),
        )
        assert storage.normalize_cluster_trace_analysis(tenant, limit=1) == 1
        assert storage.count_pending_analysis_rows(tenant) == 0
        assert storage.cluster_trace_time_bounds(
            tenant, target_workload="agent"
        ) == (1, datetime_to_utc_us(now), datetime_to_utc_us(now))

        identity = ClusterIdentity(
            tenant_id=tenant, cluster_id=cluster_id, kind="explicit",
            explicit_key="billing", display_name="Billing", created_by="admin",
            updated_by="admin",
        )
        version = ClusterRegistryVersion(
            tenant_id=tenant, version_id=version_id, strategy="explicit", cutoff=now,
            fit_definition_json='{"model_fingerprint":null}',
            fit_definition_fingerprint="definition", created_by="admin",
        )
        cluster = ClusterRegistryCluster(tenant,version_id,cluster_id,"explicit",member_count=1)
        assignment = TraceClusterAssignment(
            tenant,version_id,trace_id,"fit","assigned",cluster_id,"explicit"
        )
        storage.insert_cluster_preview(version,[identity],[cluster],[assignment])
        storage.insert_cluster_registry_event(
            ClusterRegistryEvent(
                tenant,action="validated",to_version_id=version_id,actor="admin",
                details_json='{"schema":"validation-report-v1","passed":true}',
            )
        )
        pointer = storage.activate_cluster_registry(
            tenant,
            version_id,
            expected_generation=0,
            actor="admin",
            action="activated",
            expected_candidate_digest=cluster_candidate_digest([trace_id]),
        )
        assert pointer.version_id == version_id
        storage.rename_cluster_identity(tenant,cluster_id,"Billing support",actor="admin")
        [loaded] = storage.list_cluster_identities(tenant)
        assert loaded.display_name == "Billing support"
        [candidate] = storage.list_cluster_trace_candidates(
            tenant, int(now.timestamp() * 1_000_000),
            int(now.timestamp() * 1_000_000) + 1,
            target_workload="agent", limit=2,
        )
        assert candidate.intent_key == "billing"
        assert storage.list_cluster_trace_candidates(
            tenant,
            int(now.timestamp() * 1_000_000),
            int(now.timestamp() * 1_000_000) + 1,
            target_workload="agent",
            limit=2,
            missing_version_id=version_id,
        ) == []
        storage.insert_trace(
            Trace(
                trace_id=trace_id,
                tenant_id=f"other-{tenant}",
                started_at=now + timedelta(days=1),
                ended_at=now + timedelta(days=1),
                raw_messages=[{"role": "user", "content": "shipping"}],
                tags={"verdict.workload": "judge", "verdict.intent_key": "shipping"},
            )
        )
        preserved = storage.get_trace(trace_id)
        assert preserved is not None
        assert preserved.tenant_id == tenant
        assert preserved.tags["verdict.intent_key"] == "billing"
        assert preserved.analysis_started_at_us == int(now.timestamp() * 1_000_000)


def test_live_postgres_agent_run_bundle_is_atomic_redacted_and_tenant_scoped():
    suffix = uuid4().hex
    tenant = f"evidence-{suffix}"
    now = datetime.now(timezone.utc)
    bundle = verdict.AgentRunBundle(
        session=verdict.SourceSession(
            source_session_id=f"session-{suffix}",
            tenant_id=tenant,
            source_kind="unknown-agent",
            source_locator_hash="a" * 64,
            started_at=now,
            ended_at=now,
            observed_at=now,
        ),
        run=verdict.AgentRun(
            run_id=f"run-{suffix}",
            source_session_id=f"session-{suffix}",
            tenant_id=tenant,
            started_at=now,
            ended_at=now,
            status=verdict.ExecutionStatus.COMPLETED,
        ),
        turns=(
            verdict.AgentTurn(
                turn_id=f"turn-{suffix}",
                run_id=f"run-{suffix}",
                sequence=0,
                started_at=now,
                ended_at=now,
                status=verdict.ExecutionStatus.COMPLETED,
                user_request_redacted="email customer@example.com",
                final_response_redacted="done",
                request_state=verdict.EvidenceState.PRESENT,
                response_state=verdict.EvidenceState.PRESENT,
                input_tokens=9,
                cached_input_tokens=4,
                output_tokens=3,
                total_tokens=12,
                token_usage_basis="codex_turn_delta",
                response_truncated=True,
            ),
        ),
    )
    storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
    try:
        storage.replace_agent_run_bundle(bundle)
        storage.replace_agent_run_bundle(bundle)

        loaded = storage.get_agent_run_bundle(tenant, bundle.run.run_id)
        assert loaded is not None
        assert loaded.turns[0].user_request_redacted == "email <EMAIL>"
        assert loaded.turns[0].total_tokens == 12
        assert loaded.turns[0].cached_input_tokens == 4
        assert loaded.turns[0].response_truncated is True
        assert storage.get_agent_run_bundle(f"other-{tenant}", bundle.run.run_id) is None
        assert storage.list_agent_run_bundles(tenant, limit=10) == [loaded]
        assert storage.has_agent_run_source_kind(tenant, "unknown-agent") is True
        assert storage.has_agent_run_source_kind(tenant, "codex") is False
        assert storage._fetchone(
            "SELECT 1 FROM agent_run_bundles WHERE tenant_id=%s", (tenant,)
        ) is None
    finally:
        storage._exec("DELETE FROM agent_runs WHERE tenant_id = %s", (tenant,))
        storage._exec("DELETE FROM import_sources WHERE tenant_id = %s", (tenant,))
        storage.close()


def test_live_postgres_serializes_concurrent_fresh_schema_initialization():
    with isolated_test_dsn(DSN) as scoped_dsn:
        barrier = threading.Barrier(6)

        def initialize(_index: int) -> None:
            barrier.wait()
            storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
            storage.close()

        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(initialize, range(6)))


def test_live_postgres_adds_turn_usage_columns_to_existing_schema():
    import psycopg

    with isolated_test_dsn(DSN) as scoped_dsn:
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute(
                """CREATE TABLE agent_turns (
                    tenant_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
                    status TEXT NOT NULL, user_request_redacted TEXT,
                    final_response_redacted TEXT, request_state TEXT NOT NULL,
                    response_state TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, run_id, turn_id),
                    UNIQUE (tenant_id, run_id, sequence)
                )"""
            )
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        try:
            rows = storage._fetchall(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='agent_turns'",
                (),
            )
        finally:
            storage.close()

    assert {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "token_usage_basis",
        "request_truncated",
        "response_truncated",
    } <= {row[0] for row in rows}


def test_live_postgres_dashboard_reads_existing_turn_schema_without_migration():
    import psycopg

    with isolated_test_dsn(DSN) as scoped_dsn:
        now = datetime.now(timezone.utc)
        tenant = f"legacy-detail-{uuid4().hex}"
        source = verdict.SourceSession(
            "source", tenant, "codex", "a" * 64, now, now
        )
        run = verdict.AgentRun(
            "run", source.source_session_id, tenant, now,
            verdict.ExecutionStatus.COMPLETED, ended_at=now,
        )
        turn = verdict.AgentTurn(
            "turn", run.run_id, 0, now,
            verdict.ExecutionStatus.COMPLETED, ended_at=now,
        )
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.replace_agent_run_bundle(verdict.AgentRunBundle(source, run, (turn,)))
        storage.close()
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute(
                """ALTER TABLE agent_turns
                   DROP COLUMN input_tokens,
                   DROP COLUMN cached_input_tokens,
                   DROP COLUMN cache_write_input_tokens,
                   DROP COLUMN output_tokens,
                   DROP COLUMN reasoning_output_tokens,
                   DROP COLUMN total_tokens,
                   DROP COLUMN token_usage_basis,
                   DROP COLUMN request_truncated,
                   DROP COLUMN response_truncated"""
            )

        detail = build_agent_run_detail(scoped_dsn, tenant=tenant, run_id=run.run_id)

    assert detail["turns"] == [{
        "turnId": "turn",
        "sequence": 0,
        "startedAt": now.isoformat(),
        "status": "completed",
        "requestState": "not_captured",
        "responseState": "not_captured",
        "request": None,
        "response": None,
        "requestTruncated": False,
        "responseTruncated": False,
        "tokenUsage": {
            "inputTokens": None,
            "cachedInputTokens": None,
            "cacheWriteInputTokens": None,
            "outputTokens": None,
            "reasoningOutputTokens": None,
            "totalTokens": None,
            "basis": None,
        },
    }]


def test_live_postgres_extends_legacy_local_agent_trace_prefixes():
    with isolated_test_dsn(DSN) as scoped_dsn:
        tenant = f"local-preview-upgrade-{uuid4().hex}"
        now = datetime.now(timezone.utc)
        source = verdict.SourceSession(
            "source", tenant, "claude-code", "a" * 64, now, now, ended_at=now
        )
        run = verdict.AgentRun(
            "run",
            source.source_session_id,
            tenant,
            now,
            verdict.ExecutionStatus.COMPLETED,
            ended_at=now,
        )
        first_turn = verdict.AgentTurn(
            "turn",
            run.run_id,
            0,
            now,
            verdict.ExecutionStatus.COMPLETED,
            ended_at=now,
            user_request_redacted="p" * 1_000,
            final_response_redacted="r" * 1_000,
            request_state=verdict.EvidenceState.PRESENT,
            response_state=verdict.EvidenceState.PRESENT,
        )
        event = verdict.AgentEvent(
            "event",
            first_turn.turn_id,
            0,
            now,
            verdict.AgentEventType.MODEL_CALL,
            verdict.ExecutionStatus.COMPLETED,
            "claude-code:assistant",
            attributes={"provider": "anthropic", "response_model": "claude-test"},
            trace_id="trace",
        )
        first_bundle = verdict.AgentRunBundle(source, run, (first_turn,), (event,))
        tags = {
            "verdict.source": "claude-code",
            "verdict.workload": "agent",
            "verdict.agent_run_id": run.run_id,
            "verdict.agent_event_id": event.event_id,
            "verdict.input_evidence": "turn_request_only",
            "verdict.time_evidence": "response_observed_at",
        }
        first_trace = verdict.Trace(
            trace_id=event.trace_id,
            tenant_id=tenant,
            started_at=now,
            ended_at=now,
            provider="anthropic",
            request_model="claude-test",
            response_model="claude-test",
            prompt_redacted="p" * 1_000,
            response_redacted="r" * 1_000,
            raw_messages=[
                {"role": "user", "content": "p" * 1_000},
                {"role": "assistant", "content": "r" * 1_000},
            ],
            tags=tags,
        )
        extended_bundle = replace(
            first_bundle,
            turns=(replace(
                first_turn,
                user_request_redacted="p" * 2_000,
                final_response_redacted="r" * 2_000,
            ),),
        )
        extended_trace = replace(
            first_trace,
            prompt_redacted="p" * 2_000,
            response_redacted="r" * 2_000,
            raw_messages=[
                {"role": "user", "content": "p" * 2_000},
                {"role": "assistant", "content": "r" * 2_000},
            ],
        )
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        try:
            storage.replace_agent_capture(first_bundle, (first_trace,))

            storage.replace_agent_capture(extended_bundle, (extended_trace,))

            assert storage.get_agent_run_bundle(tenant, run.run_id) == extended_bundle
            stored_trace = storage.get_trace(event.trace_id)
            assert stored_trace is not None
            assert stored_trace.prompt_redacted == extended_trace.prompt_redacted
            assert stored_trace.response_redacted == extended_trace.response_redacted
            assert stored_trace.raw_messages == extended_trace.raw_messages
        finally:
            storage.close()


def test_live_postgres_serializes_equivalent_agent_capture_replays():
    suffix = uuid4().hex
    tenant = f"capture-race-{suffix}"
    now = datetime.now(timezone.utc)
    bundle = verdict.AgentRunBundle(
        verdict.SourceSession(
            f"source-{suffix}", tenant, "unknown-agent", "b" * 64, now, now
        ),
        verdict.AgentRun(
            f"run-{suffix}",
            f"source-{suffix}",
            tenant,
            now,
            verdict.ExecutionStatus.UNKNOWN,
        ),
    )
    first = PostgresStorage(DSN, min_pool=1, max_pool=1)
    second = PostgresStorage(DSN, min_pool=1, max_pool=1)
    barrier = threading.Barrier(2)

    def capture(storage: PostgresStorage) -> None:
        barrier.wait()
        storage.replace_agent_run_bundle(bundle)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(capture, (first, second)))
        assert first.get_agent_run_bundle(tenant, bundle.run.run_id) == bundle
        assert first._fetchone(
            "SELECT COUNT(*) FROM agent_runs WHERE tenant_id=%s", (tenant,)
        )[0] == 1
    finally:
        first._exec("DELETE FROM agent_runs WHERE tenant_id=%s", (tenant,))
        first._exec("DELETE FROM import_sources WHERE tenant_id=%s", (tenant,))
        first.close()
        second.close()


def test_live_postgres_serializes_concurrent_agent_event_appends():
    suffix = uuid4().hex
    tenant = f"agent-append-{suffix}"
    now = datetime.now(timezone.utc)
    source = verdict.SourceSession(
        f"source-{suffix}", tenant, "verdict_sdk", "c" * 64, now, now
    )
    run = verdict.AgentRun(
        f"run-{suffix}", source.source_session_id, tenant, now,
        verdict.ExecutionStatus.UNKNOWN,
    )
    turn = verdict.AgentTurn(
        f"turn-{suffix}", run.run_id, 0, now, verdict.ExecutionStatus.UNKNOWN
    )
    events = tuple(
        verdict.AgentEvent(
            f"event-{index}-{suffix}",
            turn.turn_id,
            index,
            now,
            verdict.AgentEventType.FEEDBACK,
            verdict.ExecutionStatus.COMPLETED,
            "verdict:sdk",
            {"kind": "thumbs_up", "value": True},
            verdict.PrivacyClassification.REDACTED,
            producer_id="worker",
            producer_sequence=index,
        )
        for index in range(2)
    )
    first = PostgresStorage(DSN, min_pool=1, max_pool=1)
    second = PostgresStorage(DSN, min_pool=1, max_pool=1)
    first.append_agent_capture(AgentCaptureBatch(source, run, (turn,)))
    barrier = threading.Barrier(2)

    def append(item):
        storage, event = item
        barrier.wait()
        storage.append_agent_capture(AgentCaptureBatch(source, run, (turn,), (event,)))

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(append, ((first, events[0]), (second, events[1]))))
        loaded = first.get_agent_run_bundle(tenant, run.run_id)
        assert loaded is not None
        assert [event.sequence for event in loaded.events] == [0, 1]
    finally:
        first._exec("DELETE FROM agent_runs WHERE tenant_id=%s", (tenant,))
        first._exec("DELETE FROM import_sources WHERE tenant_id=%s", (tenant,))
        first.close()
        second.close()


def test_live_postgres_agent_capture_rolls_back_trace_and_hierarchy(monkeypatch):
    suffix = uuid4().hex
    tenant = f"capture-rollback-{suffix}"
    now = datetime.now(timezone.utc)
    trace = verdict.Trace(
        trace_id=f"trace-{suffix}",
        tenant_id=tenant,
        started_at=now,
        ended_at=now,
        provider="anthropic",
        request_model="test-model",
        response_model="test-model",
    )
    source = verdict.SourceSession(
        f"source-{suffix}", tenant, "unknown-agent", "d" * 64, now, now
    )
    run = verdict.AgentRun(
        f"run-{suffix}", source.source_session_id, tenant, now,
        verdict.ExecutionStatus.UNKNOWN,
    )
    turn = verdict.AgentTurn(
        f"turn-{suffix}", run.run_id, 0, now, verdict.ExecutionStatus.UNKNOWN
    )
    event = verdict.AgentEvent(
        f"event-{suffix}", turn.turn_id, 0, now,
        verdict.AgentEventType.MODEL_CALL, verdict.ExecutionStatus.UNKNOWN,
        "unknown-agent:model", {"provider": "anthropic"},
        trace_id=trace.trace_id,
    )
    bundle = verdict.AgentRunBundle(source, run, (turn,), (event,))
    storage = PostgresStorage(DSN, min_pool=1, max_pool=1)
    original = storage._write_normalized_bundle_cursor

    def fail_after_hierarchy(cur, candidate):
        original(cur, candidate)
        raise RuntimeError("injected capture failure")

    monkeypatch.setattr(storage, "_write_normalized_bundle_cursor", fail_after_hierarchy)
    try:
        with pytest.raises(RuntimeError, match="injected capture failure"):
            storage.replace_agent_capture(bundle, (trace,))
        assert storage.get_trace(trace.trace_id) is None
        assert storage.get_agent_run_bundle(tenant, run.run_id) is None
    finally:
        storage._exec("DELETE FROM agent_runs WHERE tenant_id=%s", (tenant,))
        storage._exec("DELETE FROM import_sources WHERE tenant_id=%s", (tenant,))
        storage._exec("DELETE FROM traces WHERE trace_id=%s", (trace.trace_id,))
        storage.close()


def test_live_postgres_trace_retention_clears_agent_event_link():
    suffix = uuid4().hex
    tenant = f"capture-retention-{suffix}"
    now = datetime.now(timezone.utc)
    trace = verdict.Trace(
        trace_id=f"trace-{suffix}", tenant_id=tenant, started_at=now, ended_at=now,
    )
    source = verdict.SourceSession(
        f"source-{suffix}", tenant, "unknown-agent", "e" * 64, now, now
    )
    run = verdict.AgentRun(
        f"run-{suffix}", source.source_session_id, tenant, now,
        verdict.ExecutionStatus.UNKNOWN,
    )
    turn = verdict.AgentTurn(
        f"turn-{suffix}", run.run_id, 0, now, verdict.ExecutionStatus.UNKNOWN
    )
    event = verdict.AgentEvent(
        f"event-{suffix}", turn.turn_id, 0, now,
        verdict.AgentEventType.MODEL_CALL, verdict.ExecutionStatus.UNKNOWN,
        "unknown-agent:model", {}, trace_id=trace.trace_id,
    )
    bundle = verdict.AgentRunBundle(source, run, (turn,), (event,))
    storage = PostgresStorage(DSN, min_pool=1, max_pool=1)
    try:
        storage.replace_agent_capture(bundle, (trace,))
        storage.delete_trace(trace.trace_id)

        retained = storage.get_agent_run_bundle(tenant, run.run_id)
        assert retained is not None
        assert retained.events[0].trace_id is None
    finally:
        storage._exec("DELETE FROM agent_runs WHERE tenant_id=%s", (tenant,))
        storage._exec("DELETE FROM import_sources WHERE tenant_id=%s", (tenant,))
        storage._exec("DELETE FROM traces WHERE trace_id=%s", (trace.trace_id,))
        storage.close()


def test_live_postgres_capture_extends_prompt_only_trace_messages():
    suffix = uuid4().hex
    tenant = f"capture-completion-{suffix}"
    now = datetime.now(timezone.utc)
    source = verdict.SourceSession(
        f"source-{suffix}", tenant, "claude-code", "f" * 64, now, now
    )
    run = verdict.AgentRun(
        f"run-{suffix}", source.source_session_id, tenant, now,
        verdict.ExecutionStatus.COMPLETED, ended_at=now,
    )
    prompt_only_turn = verdict.AgentTurn(
        f"turn-{suffix}", run.run_id, 0, now, verdict.ExecutionStatus.COMPLETED,
        ended_at=now, user_request_redacted="inspect the build",
        request_state=verdict.EvidenceState.PRESENT,
        response_state=verdict.EvidenceState.MISSING,
    )
    event = verdict.AgentEvent(
        f"event-{suffix}", prompt_only_turn.turn_id, 0, now,
        verdict.AgentEventType.MODEL_CALL, verdict.ExecutionStatus.COMPLETED,
        "claude-code:model", {"provider": "anthropic"},
        trace_id=f"trace-{suffix}",
    )
    prompt_only = verdict.Trace(
        trace_id=event.trace_id, tenant_id=tenant, started_at=now, ended_at=now,
        provider="anthropic", request_model="claude-test",
        prompt_redacted="inspect the build",
        raw_messages=[{"role": "user", "content": "inspect the build"}],
    )
    completed_turn = replace(
        prompt_only_turn,
        final_response_redacted="the build passes",
        response_state=verdict.EvidenceState.PRESENT,
    )
    completed_trace = replace(
        prompt_only,
        response_redacted="the build passes",
        raw_messages=[
            *prompt_only.raw_messages,
            {"role": "assistant", "content": "the build passes"},
        ],
    )

    with _isolated_postgres_storage() as storage:
        storage.replace_agent_capture(
            verdict.AgentRunBundle(source, run, (prompt_only_turn,), (event,)),
            (prompt_only,),
        )
        before = storage.get_trace(prompt_only.trace_id)
        storage.replace_agent_capture(
            verdict.AgentRunBundle(source, run, (completed_turn,), (event,)),
            (completed_trace,),
        )
        stored = storage.get_trace(prompt_only.trace_id)

    assert before is not None
    assert stored is not None
    assert stored.response_redacted == "the build passes"
    assert stored.raw_messages == completed_trace.raw_messages
    assert stored.analysis_raw_messages_state == "valid"
    assert before.analysis_raw_messages_utf8_bytes is not None
    assert stored.analysis_raw_messages_utf8_bytes is not None
    assert stored.analysis_raw_messages_utf8_bytes > before.analysis_raw_messages_utf8_bytes


def test_live_postgres_migrates_a17_agent_bundle_transactionally():
    import psycopg
    from psycopg.errors import RaiseException

    tenant = f"legacy-{uuid4().hex}"
    now = datetime.now(timezone.utc)
    def legacy_bundle(suffix: str) -> verdict.AgentRunBundle:
        source_id = f"legacy-source-{suffix}"
        run_id = f"legacy-run-{suffix}"
        turn = verdict.AgentTurn(
            "source-local-turn", run_id, 0, now, verdict.ExecutionStatus.UNKNOWN
        )
        event = verdict.AgentEvent(
            "source-local-event", turn.turn_id, 0, now,
            verdict.AgentEventType.MODEL_CALL if suffix == "c" else verdict.AgentEventType.COMMAND,
            verdict.ExecutionStatus.UNKNOWN,
            "codex:model" if suffix == "c" else "codex:command", {},
            trace_id="already-deleted-trace" if suffix == "c" else None,
        )
        return verdict.AgentRunBundle(
            verdict.SourceSession(
                source_id, tenant, "codex", suffix * 64, now, now
            ),
            verdict.AgentRun(
                run_id, source_id, tenant, now, verdict.ExecutionStatus.UNKNOWN
            ),
            (turn,),
            (event,),
        )

    bundles = (legacy_bundle("c"), legacy_bundle("d"))
    with isolated_test_dsn(DSN) as scoped_dsn:
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute(
                """CREATE TABLE agent_run_bundles (
                    tenant_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    source_session_id TEXT NOT NULL, source_kind TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
                    status TEXT NOT NULL, content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (tenant_id, run_id))"""
            )
            for values in [
                    (
                        tenant,
                        bundle.run.run_id,
                        bundle.session.source_session_id,
                        bundle.session.source_kind,
                        now,
                        None,
                        bundle.run.status.value,
                        bundle.content_hash,
                        verdict.agent_run_bundle_to_json(bundle),
                        now,
                    )
                    for bundle in bundles
            ]:
                connection.execute(
                    "INSERT INTO agent_run_bundles VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    values,
                )

        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        try:
            for bundle in bundles:
                expected = replace(
                    bundle,
                    events=(replace(bundle.events[0], trace_id=None),),
                )
                assert storage.get_agent_run_bundle(tenant, bundle.run.run_id) == expected
            assert storage._fetchone(
                "SELECT COUNT(*) FROM agent_run_bundles WHERE tenant_id=%s", (tenant,)
            )[0] == 2
            assert storage._fetchone(
                "SELECT COUNT(*) FROM agent_runs WHERE tenant_id=%s", (tenant,)
            )[0] == 2
            assert storage._fetchone(
                "SELECT COUNT(*) FROM agent_turns WHERE tenant_id=%s", (tenant,)
            )[0] == 2
            assert storage._fetchone(
                "SELECT COUNT(*) FROM agent_events WHERE tenant_id=%s", (tenant,)
            )[0] == 2
            with pytest.raises(
                RaiseException,
                match="legacy agent evidence writer detected after normalized migration",
            ):
                storage._exec(
                    "UPDATE agent_run_bundles SET status='failed' WHERE tenant_id=%s",
                    (tenant,),
                )
        finally:
            storage.close()


def test_live_postgres_rejects_new_legacy_bundle_writes_after_migration():
    import psycopg
    from psycopg.errors import RaiseException

    with isolated_test_dsn(DSN) as scoped_dsn:
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.close()

        with psycopg.connect(scoped_dsn, autocommit=True) as connection, pytest.raises(
            RaiseException,
            match="legacy agent evidence writer detected after normalized migration",
        ):
            connection.execute(
                """INSERT INTO agent_run_bundles (
                    tenant_id,run_id,source_session_id,source_kind,started_at,
                    status,content_hash,payload_json,updated_at
                ) VALUES ('tenant-a','run-a','source-a','custom-agent',now(),
                          'unknown',repeat('0',64),'{}',now())"""
            )


def test_live_postgres_detects_orphan_from_an_earlier_normalized_writer():
    import psycopg
    from psycopg.errors import RaiseException

    with isolated_test_dsn(DSN) as scoped_dsn:
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)
        storage.close()
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute(
                "DROP TRIGGER reject_legacy_agent_run_bundle_write "
                "ON agent_run_bundles"
            )
            connection.execute(
                "DELETE FROM verdict_schema_migrations "
                "WHERE name='block_legacy_agent_evidence_writes_v1'"
            )
            connection.execute(
                """INSERT INTO agent_run_bundles (
                    tenant_id,run_id,source_session_id,source_kind,started_at,
                    status,content_hash,payload_json,updated_at
                ) VALUES ('tenant-a','orphan','source-a','custom-agent',now(),
                          'unknown',repeat('0',64),'{}',now())"""
            )

        with pytest.raises(
            RuntimeError,
            match=r"^legacy agent evidence writer detected after normalized migration$",
        ):
            PostgresStorage(scoped_dsn, min_pool=1, max_pool=1)

        with psycopg.connect(scoped_dsn, autocommit=True) as connection, pytest.raises(
            RaiseException,
            match="legacy agent evidence writer detected after normalized migration",
        ):
            connection.execute(
                "UPDATE agent_run_bundles SET status='failed' WHERE run_id='orphan'"
            )


def test_live_postgres_monitor_policy_activation_and_snapshot():
    suffix = uuid4().hex
    scope = f"monitor-{suffix}"
    first = MonitorPolicy(
        f"policy-a-{suffix}", scope, reference_ratio=0.5,
        minimum_reference=2, minimum_current=2, grouping_mode="cluster",
        cluster_registry_version_id=f"registry-{suffix}",
    )
    second = MonitorPolicy(f"policy-b-{suffix}", scope, reference_ratio=0.5,
                           minimum_reference=2, minimum_current=2)
    now = datetime.now(timezone.utc)
    units = tuple(
        AnalysisUnitRecord(
            f"unit-{suffix}-{index}", now + timedelta(minutes=index),
            {"failed": index >= 4}, group_id=f"cluster-{index % 2}",
        )
        for index in range(8)
    )
    manifest = plan_historical_manifest(units, first, cutoff=now + timedelta(hours=1))
    comparison = compare_manifest(units, manifest, first)
    storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
    try:
        storage.save_monitor_candidate(first, manifest, comparison)
        assert storage.get_initial_monitor_snapshot(first.policy_id) == (
            manifest, comparison,
        )
        invalid = replace(second, policy_id=f"invalid-{suffix}", minimum_effect=0.4)
        with pytest.raises(ValueError, match="does not match policy"):
            storage.save_monitor_candidate(invalid, manifest, comparison)
        assert storage.get_monitor_policy(invalid.policy_id) is None
        incomplete = replace(
            second,
            policy_id=f"incomplete-{suffix}",
            scope_key=f"{scope}:incomplete",
        )
        storage.save_monitor_policy(incomplete)
        assert storage.get_latest_monitor_candidate(incomplete.scope_key) is None
        storage.save_monitor_policy(second)
        assert storage.activate_monitor_policy(
            scope, first.policy_id, expected_active_policy_id=None,
        ) == first
        assert storage.get_latest_monitor_candidate(scope) is None
        assert storage.get_monitor_policy(second.policy_id) == (second, "retired")
        storage.save_monitor_snapshot(first.policy_id, manifest, comparison)
        assert storage.get_latest_monitor_snapshot(first.policy_id) == (manifest, comparison)
        changed = tuple(
            replace(unit, metrics={"failed": not unit.metrics["failed"]}) for unit in units
        )
        stored_manifest = storage.get_latest_monitor_snapshot(first.policy_id)[0]
        assert compare_manifest(changed, stored_manifest, first) == comparison
        alert = replace(comparison, status=MonitorStatus.ALERT)
        alert_manifest = replace(
            manifest, snapshot_id=sha256(f"alert-{suffix}".encode()).hexdigest()
        )
        storage.save_monitor_snapshot(first.policy_id, alert_manifest, alert)
        assert storage.get_latest_monitor_alert(first.policy_id) == (
            alert_manifest, alert,
        )
        assert storage.get_initial_monitor_snapshot(first.policy_id) == (
            manifest, comparison,
        )
        with pytest.raises(ValueError, match="does not match policy"):
            storage.save_monitor_snapshot(second.policy_id, manifest, comparison)
        collecting = plan_prospective_manifest(manifest, (), first)
        collecting_result = compare_manifest((), collecting, first)
        storage.save_monitor_successor(
            first.policy_id, alert_manifest.snapshot_id,
            collecting, collecting_result, expected_state="active",
        )
        assert storage.get_latest_monitor_snapshot(first.policy_id) == (
            collecting, collecting_result,
        )
        with pytest.raises(ValueError, match="snapshot changed"):
            storage.save_monitor_successor(
                first.policy_id, alert_manifest.snapshot_id,
                collecting, collecting_result, expected_state="active",
            )
        assert storage.activate_monitor_policy(
            scope, second.policy_id, expected_active_policy_id=first.policy_id,
        ) == second
        assert storage.get_monitor_policy(first.policy_id) == (first, "retired")
        assert storage.get_latest_monitor_candidate(scope) is None
    finally:
        storage._exec("DELETE FROM monitor_snapshots WHERE policy_id IN (%s,%s)",
                      (first.policy_id, second.policy_id))
        storage._exec(
            "DELETE FROM monitor_policies WHERE scope_key IN (%s,%s)",
            (scope, f"{scope}:incomplete"),
        )
        storage.close()


def test_live_postgres_concurrent_monitor_successors_have_one_winner():
    suffix = uuid4().hex
    scope = f"monitor-concurrent-{suffix}"
    policy = MonitorPolicy(
        f"policy-{suffix}", scope, reference_ratio=0.5,
        minimum_reference=2, minimum_current=2,
    )
    now = datetime.now(timezone.utc)
    units = tuple(
        AnalysisUnitRecord(
            f"unit-{suffix}-{index}", now + timedelta(minutes=index),
            {"failed": index >= 3},
        )
        for index in range(6)
    )
    manifest = plan_historical_manifest(
        units, policy, cutoff=now + timedelta(hours=1),
    )
    comparison = compare_manifest(units, manifest, policy)
    successor = plan_prospective_manifest(manifest, (), policy)
    successor_result = compare_manifest((), successor, policy)
    setup = PostgresStorage(DSN, min_pool=1, max_pool=2)
    setup.save_monitor_candidate(policy, manifest, comparison)
    setup.activate_monitor_policy(
        scope, policy.policy_id, expected_active_policy_id=None,
    )
    setup.close()
    barrier = threading.Barrier(2)
    outcomes = []

    def save() -> None:
        storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
        try:
            barrier.wait()
            storage.save_monitor_successor(
                policy.policy_id, manifest.snapshot_id,
                successor, successor_result, expected_state="active",
            )
        except ValueError:
            outcomes.append("conflict")
        else:
            outcomes.append("saved")
        finally:
            storage.close()

    threads = [threading.Thread(target=save) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(outcomes) == ["conflict", "saved"]
        verifier = PostgresStorage(DSN, min_pool=1, max_pool=2)
        try:
            assert verifier.get_latest_monitor_snapshot(policy.policy_id) == (
                successor, successor_result,
            )
        finally:
            verifier.close()
    finally:
        setup = PostgresStorage(DSN, min_pool=1, max_pool=2)
        setup._exec(
            "DELETE FROM monitor_snapshots WHERE policy_id=%s", (policy.policy_id,),
        )
        setup._exec(
            "DELETE FROM monitor_policies WHERE policy_id=%s", (policy.policy_id,),
        )
        setup.close()


def test_live_postgres_monitor_order_does_not_depend_on_timestamp_or_hash():
    suffix = uuid4().hex
    policy = MonitorPolicy(
        f"policy-{suffix}", f"scope-{suffix}", reference_ratio=0.5,
        minimum_reference=1, minimum_current=1, prospective_target=1,
    )
    now = datetime.now(timezone.utc)
    units = tuple(
        AnalysisUnitRecord(
            f"unit-{index}", now + timedelta(seconds=index), {"failed": False},
        )
        for index in range(2)
    )

    with _isolated_postgres_storage() as storage:
        initial = plan_historical_manifest(
            units, policy, cutoff=now + timedelta(minutes=1),
        )
        initial_result = compare_manifest(units, initial, policy)
        storage.save_monitor_candidate(policy, initial, initial_result)
        storage.activate_monitor_policy(
            policy.scope_key, policy.policy_id, expected_active_policy_id=None,
        )
        successor = plan_prospective_manifest(initial, units, policy)
        successor_result = compare_manifest(units, successor, policy)
        storage.save_monitor_successor(
            policy.policy_id, initial.snapshot_id, successor, successor_result,
            expected_state="active",
        )
        storage._exec(
            "UPDATE monitor_snapshots SET created_at=%s WHERE policy_id=%s",
            (now, policy.policy_id),
        )

        assert storage.get_initial_monitor_snapshot(policy.policy_id) == (
            initial, initial_result,
        )
        assert storage.get_latest_monitor_snapshot(policy.policy_id) == (
            successor, successor_result,
        )


def test_live_postgres_backfills_monitor_write_order_on_upgrade():
    suffix = uuid4().hex
    policy = MonitorPolicy(
        f"policy-{suffix}", f"scope-{suffix}", reference_ratio=0.5,
        minimum_reference=1, minimum_current=1, prospective_target=1,
    )
    now = datetime.now(timezone.utc)
    units = tuple(
        AnalysisUnitRecord(
            f"unit-{index}", now + timedelta(seconds=index), {"failed": False},
        )
        for index in range(2)
    )

    with isolated_test_dsn(DSN) as scoped_dsn:
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=2)
        initial = plan_historical_manifest(
            units, policy, cutoff=now + timedelta(minutes=1),
        )
        initial_result = compare_manifest(units, initial, policy)
        successor = plan_prospective_manifest(initial, units, policy)
        successor_result = compare_manifest(units, successor, policy)
        storage.save_monitor_candidate(policy, initial, initial_result)
        storage.save_monitor_snapshot(policy.policy_id, successor, successor_result)
        storage._exec(
            "ALTER TABLE monitor_snapshots DROP COLUMN write_sequence CASCADE", (),
        )
        storage.close()

        upgraded = PostgresStorage(scoped_dsn, min_pool=1, max_pool=2)
        try:
            sequences = upgraded._fetchall(
                "SELECT write_sequence FROM monitor_snapshots "
                "WHERE policy_id=%s ORDER BY write_sequence",
                (policy.policy_id,),
            )
            assert len(sequences) == 2
            assert all(isinstance(row[0], int) for row in sequences)
            assert upgraded.get_latest_monitor_snapshot(policy.policy_id) == (
                successor, successor_result,
            )
        finally:
            upgraded.close()


def test_live_postgres_projects_new_traces_through_pinned_explicit_clusters():
    suffix = uuid4().hex
    tenant = f"monitor-cluster-{suffix}"
    now = datetime.now(timezone.utc)
    with _isolated_postgres_storage() as storage:
        storage.insert_trace(
            Trace(
                trace_id=f"historical-{suffix}",
                tenant_id=tenant,
                started_at=now - timedelta(hours=1),
                ended_at=now - timedelta(minutes=59),
                response_redacted="ok",
                tags={"verdict.intent_key": "billing"},
            )
        )
        service = ClusterRegistryService(storage)
        version = service.fit(
            tenant,
            actor="test",
            strategy="explicit",
            cutoff=now,
            config=FitConfig(strategy="explicit"),
        )
        storage.insert_trace(
            Trace(
                trace_id=f"new-{suffix}",
                tenant_id=tenant,
                started_at=now + timedelta(hours=1),
                ended_at=now + timedelta(hours=1, seconds=1),
                response_redacted="ok",
                tags={"verdict.intent_key": "billing"},
            )
        )
        policy = MonitorPolicy(
            f"policy-{suffix}",
            f"scope-{suffix}",
            grouping_mode="cluster",
            cluster_registry_version_id=version.version_id,
        )

        units = load_monitor_units(storage, policy, tenant_id=tenant)

        assert len(units) == 2
        assert len({unit.group_id for unit in units}) == 1
        assert units[0].group_id is not None


def test_live_postgres_latest_evaluator_judgments_are_scoped_and_latest_wins():
    suffix = uuid4().hex
    tenant = "__verdict_local__"
    fingerprint = sha256(f"evaluator-{suffix}".encode()).hexdigest()
    now = datetime.now(timezone.utc)
    storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
    trace_ids = [f"trace-{suffix}-{index}" for index in range(3)]
    try:
        for index, trace_id in enumerate(trace_ids):
            storage.insert_trace(Trace(
                trace_id=trace_id,
                tenant_id=(None if index == 0 else (
                    tenant if index == 1 else f"other-{suffix}"
                )),
                started_at=now + timedelta(seconds=index),
                ended_at=now + timedelta(seconds=index + 1),
                prompt_redacted="request", response_redacted="response",
            ))
        storage.insert_judgment(Judgment(
            judgment_id=f"old-{suffix}", trace_id=trace_ids[0], created_at=now,
            evaluator_provider="openai", judge_models=["judge"],
            evaluator_fingerprint=fingerprint, expected_dimensions=["quality"],
            dimensions=[DimensionScore("quality", Verdict.PASS)],
        ))
        storage.insert_judgment(Judgment(
            judgment_id=f"new-{suffix}", trace_id=trace_ids[0],
            created_at=now + timedelta(seconds=1),
            evaluator_provider="openai", judge_models=["judge"],
            evaluator_fingerprint=fingerprint, expected_dimensions=["quality"],
            status=JudgmentStatus.ERROR, error="judge unavailable",
        ))
        storage.insert_judgment(Judgment(
            judgment_id=f"other-{suffix}", trace_id=trace_ids[1], created_at=now,
            evaluator_provider="openai", judge_models=["other"],
            evaluator_fingerprint="b" * 64, expected_dimensions=["quality"],
            dimensions=[DimensionScore("quality", Verdict.FAIL)],
        ))
        storage.insert_judgment(Judgment(
            judgment_id=f"foreign-{suffix}", trace_id=trace_ids[2], created_at=now,
            evaluator_provider="openai", judge_models=["judge"],
            evaluator_fingerprint=fingerprint, expected_dimensions=["quality"],
            dimensions=[DimensionScore("quality", Verdict.FAIL)],
        ))
        rows = storage.list_latest_judgments_for_evaluator(tenant, fingerprint)
        assert [row.judgment_id for row in rows] == [f"old-{suffix}"]
        assert rows[0].status is JudgmentStatus.COMPLETED
        policy = MonitorPolicy(
            f"evaluator-policy-{suffix}", f"scope-{suffix}",
            evaluator_fingerprint=fingerprint, evaluator_dimensions=("quality",),
        )
        projected = {
            unit.unit_id: unit
            for unit in load_monitor_units(storage, policy, tenant_id=tenant)
        }
        assert projected[trace_ids[0]].metric_states == {
            "judge.quality.pass": "pass"
        }
    finally:
        storage._exec("DELETE FROM judgments WHERE trace_id = ANY(%s)", (trace_ids,))
        storage._exec("DELETE FROM traces WHERE trace_id = ANY(%s)", (trace_ids,))
        storage.close()


def test_live_postgres_migrates_legacy_tables_before_creating_indexes():
    """Open the adapter over pre-remediation tables, not only a fresh schema."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"verdict_legacy_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped_dsn = make_conninfo(DSN, options=f"-csearch_path={schema}")
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute("""
                CREATE TABLE traces (
                    trace_id TEXT PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL,
                    ended_at TIMESTAMPTZ, provider TEXT, operation TEXT,
                    request_model TEXT, response_model TEXT, input_tokens INTEGER,
                    output_tokens INTEGER, temperature DOUBLE PRECISION,
                    max_tokens INTEGER, finish_reason TEXT, error TEXT,
                    latency_ms DOUBLE PRECISION, prompt_redacted TEXT,
                    response_redacted TEXT, raw_messages JSONB, tenant_id TEXT,
                    session_id TEXT, user_id_hash TEXT, tags JSONB,
                    cost_usd DOUBLE PRECISION
                );
                CREATE TABLE judgments (
                    judgment_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
                    rubric_name TEXT, rubric_version TEXT,
                    created_at TIMESTAMPTZ NOT NULL, judge_models JSONB,
                    dimensions JSONB, position_swap_consistent BOOLEAN
                );
                CREATE TABLE drift_signals (
                    signal_id TEXT PRIMARY KEY, detected_at TIMESTAMPTZ NOT NULL,
                    cluster_id TEXT, dimension TEXT, direction TEXT,
                    statistic_name TEXT, statistic_value DOUBLE PRECISION,
                    p_value DOUBLE PRECISION, p_value_adjusted DOUBLE PRECISION,
                    effect_size_cohens_d DOUBLE PRECISION,
                    sample_size_current INTEGER, sample_size_baseline INTEGER,
                    contributing_layers JSONB, example_trace_ids JSONB,
                    recommended_action TEXT
                );
                CREATE TABLE evaluator_health (
                    health_id TEXT PRIMARY KEY,
                    evaluated_at TIMESTAMPTZ NOT NULL,
                    evaluator_fingerprint TEXT NOT NULL,
                    sentinel_set_name TEXT,
                    sentinel_set_fingerprint TEXT NOT NULL,
                    correct_labels INTEGER NOT NULL,
                    total_labels INTEGER NOT NULL,
                    agreement DOUBLE PRECISION,
                    confidence_low DOUBLE PRECISION,
                    confidence_high DOUBLE PRECISION,
                    status TEXT NOT NULL,
                    error_count INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO evaluator_health (
                    health_id, evaluated_at, evaluator_fingerprint,
                    sentinel_set_name, sentinel_set_fingerprint,
                    correct_labels, total_labels, agreement,
                    confidence_low, confidence_high, status, error_count
                ) VALUES (
                    'legacy-health', '2026-08-16T12:00:00Z',
                    'legacy-evaluator', 'anchors', 'anchor-set',
                    200, 229, 0.8733624454, 0.82, 0.91, 'healthy', 0
                );
                CREATE TABLE spans (
                    span_id TEXT PRIMARY KEY, name TEXT, trace_id TEXT,
                    started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
                    duration_ms DOUBLE PRECISION, attributes JSONB, error TEXT
                );
                INSERT INTO traces (
                    trace_id, started_at, ended_at, provider, operation,
                    tenant_id, tags
                ) VALUES (
                    'legacy-task5-trace', '2026-08-16T12:00:00Z',
                    '2026-08-16T12:00:01Z', 'anthropic', 'chat',
                    'tenant-upgrade', '{"verdict.intent_key":"billing.v1"}'::jsonb
                );
            """)

        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=2)
        try:
            columns = {
                (table, column)
                for table, column in storage._fetchall(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema()",
                    (),
                )
            }
            assert {
                ("traces", "cluster_id"),
                ("traces", "parent_span_id"),
                ("traces", "analysis_started_at_us"),
                ("traces", "analysis_started_at_state"),
                ("traces", "analysis_raw_messages_utf8_bytes"),
                ("traces", "analysis_raw_messages_state"),
                ("spans", "parent_name"),
                ("judgments", "evaluator_fingerprint"),
                ("judgments", "status"),
                ("drift_signals", "evaluator_fingerprint"),
                ("drift_signals", "run_id"),
                ("drift_signals", "effect_size_cliffs_delta"),
                ("drift_signals", "wasserstein_distance"),
                ("drift_signals", "psi"),
                ("evaluator_health", "correct_examples"),
                ("evaluator_health", "total_examples"),
                ("evaluator_health", "example_agreement"),
                ("evaluator_health", "method_version"),
            } <= columns

            tables = {
                row[0]
                for row in storage._fetchall(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()",
                    (),
                )
            }
            assert {
                "cluster_registries",
                "cluster_identities",
                "cluster_registry_versions",
                "cluster_registry_clusters",
                "trace_cluster_assignments",
                "active_cluster_registry",
                "cluster_registry_events",
            } <= tables

            assert storage.count_pending_analysis_rows("tenant-upgrade") == 1
            assert (
                storage.normalize_cluster_trace_analysis("tenant-upgrade", limit=100)
                == 1
            )
            assert storage.count_pending_analysis_rows("tenant-upgrade") == 0
            legacy_trace = storage.get_trace("legacy-task5-trace")
            assert legacy_trace is not None
            assert legacy_trace.tags["verdict.intent_key"] == "billing.v1"
            assert legacy_trace.analysis_started_at_state == "valid"

            [legacy_health] = storage.list_evaluator_health(
                evaluator_fingerprint="legacy-evaluator"
            )
            assert legacy_health.method_version == "1"
            assert legacy_health.status is EvaluatorHealthStatus.INSUFFICIENT_DATA
            assert legacy_health.total_examples == 0
            assert legacy_health.example_agreement is None
            assert legacy_health.label_agreement is None

            trace = Trace(
                trace_id=f"legacy-upgrade-{uuid4().hex}",
                cluster_id="upgraded",
                parent_span_id="parent",
            )
            storage.insert_trace(trace)
            assert storage.get_trace(trace.trace_id).parent_span_id == "parent"
        finally:
            storage.close()
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def test_live_postgres_round_trip_and_mutation_contracts():
    prefix = f"integration-{uuid4().hex}"
    trace_id = f"{prefix}-trace"
    fingerprint = f"{prefix}-evaluator"
    registry_version = f"{prefix}-registry"
    now = datetime.now(timezone.utc)
    storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
    try:
        trace = Trace(
            trace_id=trace_id,
            parent_span_id=f"{prefix}-parent",
            started_at=now,
            provider="custom-provider",
            request_model="custom/model",
            response_model="custom/model-v2",
            prompt_redacted="Prompt from 2001:db8:ac1d:5eed::cafe: timeout",
            response_redacted="Response to 2001:db8:ac1d:5eed::cafe:54321",
            error="Failure from 2001:db8:ac1d:5eed::cafe: refused",
            raw_messages=[{
                "role": "user",
                "content": "Peer 2001:db8:ac1d:5eed::cafe: unavailable",
            }],
            tenant_id=f"{prefix}-tenant",
            session_id=f"{prefix}-session",
            cluster_id=f"{prefix}-cluster",
            tags={
                "source": "integration",
                "peer": "2001:db8:ac1d:5eed::cafe: unavailable",
            },
        )
        storage.insert_trace(trace)
        assert storage.trace_exists(trace_id)
        fetched = storage.get_trace(trace_id)
        assert fetched is not None
        assert fetched.parent_span_id == trace.parent_span_id
        assert fetched.tags == {"source": "integration", "peer": "<IPV6>: unavailable"}
        assert fetched.prompt_redacted == "Prompt from <IPV6>: timeout"
        assert fetched.response_redacted == "Response to <IPV6>:54321"
        assert fetched.error == "Failure from <IPV6>: refused"
        assert fetched.raw_messages == [{
            "content": "Peer <IPV6>: unavailable",
            "role": "user",
        }]
        assert [item.trace_id for item in storage.list_traces(
            tenant_id=trace.tenant_id,
            cluster_id=trace.cluster_id,
        )] == [trace_id]

        span = SpanRecord(
            span_id=f"{prefix}-span",
            name="retrieval",
            parent_name="request",
            attributes={"k": 3},
        )
        storage.insert_span(span)
        span.trace_id = trace_id
        storage.insert_span(span)
        [persisted_span] = storage.list_spans(trace_id=trace_id)
        assert persisted_span.parent_name == "request"
        assert persisted_span.attributes == {"k": 3}

        judgment = Judgment(
            judgment_id=f"{prefix}-judgment",
            trace_id=trace_id,
            rubric_name="quality",
            rubric_version="2",
            judge_models=["judge-a"],
            dimensions=[DimensionScore("quality", Verdict.FAIL, "reason", "judge-a")],
            position_swap_consistent=True,
            evaluator_provider="openai",
            evaluator_config={"temperature": 0},
            evaluator_fingerprint=fingerprint,
            expected_dimensions=["quality"],
        )
        storage.insert_judgment(judgment)
        [persisted_judgment] = storage.list_judgments_for_cluster(trace.cluster_id)
        assert storage.has_completed_judgment(trace_id, fingerprint) is True
        assert storage.has_completed_judgment(trace_id, "missing") is False
        assert persisted_judgment.judgment_id == judgment.judgment_id
        assert persisted_judgment.position_swap_consistent is True
        assert persisted_judgment.evaluator_config == {"temperature": 0}
        assert persisted_judgment.dimensions[0].verdict is Verdict.FAIL

        health = EvaluatorHealthRecord(
            health_id=f"{prefix}-health",
            evaluator_fingerprint=fingerprint,
            sentinel_set_name="anchors",
            sentinel_set_fingerprint=f"{prefix}-anchors",
            correct_examples=30,
            total_examples=30,
            example_agreement=1.0,
            example_confidence_low=0.88,
            example_confidence_high=1.0,
            correct_labels=30,
            total_labels=30,
            label_agreement=1.0,
            status=EvaluatorHealthStatus.HEALTHY,
        )
        storage.insert_evaluator_health(health)
        assert storage.list_evaluator_health(
            evaluator_fingerprint=fingerprint
        )[0].status is EvaluatorHealthStatus.HEALTHY

        signal = DriftSignal(
            signal_id=f"{prefix}-signal",
            detected_at=now,
            cluster_id=trace.cluster_id or "",
            dimension="quality",
            direction=DriftDirection.REGRESSION,
            evaluator_fingerprint=fingerprint,
            run_id=f"{prefix}-run",
            statistic_name="fisher_exact",
            statistic_value=0.04,
            p_value=0.001,
            p_value_adjusted=0.003,
            effect_size_cliffs_delta=-0.5,
            effect_size_cohens_d=-1.0,
            wasserstein_distance=0.5,
            psi=0.8,
            sample_size_current=30,
            sample_size_baseline=30,
        )
        run = DriftRun(
            run_id=signal.run_id,
            analysis_time=now,
            completed_at=now + timedelta(seconds=1),
            evaluator_fingerprint=fingerprint,
            signal_count=1,
        )
        storage.replace_drift_run(run, [signal])
        snapshot = storage.get_latest_drift_run_snapshot(fingerprint)
        assert snapshot is not None
        assert snapshot[0].run_id == run.run_id
        assert [item.signal_id for item in snapshot[1]] == [signal.signal_id]
        persisted_signal = next(
            item for item in storage.list_drift_signals() if item.signal_id == signal.signal_id
        )
        assert persisted_signal.effect_size_cliffs_delta == -0.5
        assert persisted_signal.wasserstein_distance == 0.5
        storage.delete_drift_signals_between(
            now - timedelta(seconds=1),
            now + timedelta(seconds=1),
            evaluator_fingerprint=fingerprint,
        )
        assert all(
            item.signal_id != signal.signal_id for item in storage.list_drift_signals()
        )

        user_signal = UserSignalRecord(
            signal_id=f"{prefix}-user-signal",
            trace_id=trace_id,
            kind="thumbs_down",
        )
        storage.insert_user_signal(user_signal)
        assert any(
            item.signal_id == user_signal.signal_id
            for item in storage.list_user_signals()
        )

        storage.save_cluster_registry(registry_version, '{"clusters": []}')
        assert storage.load_cluster_registry(registry_version) == '{"clusters": []}'

        storage.delete_trace(trace_id)
        assert storage.get_trace(trace_id) is None
        assert storage.list_spans(trace_id=trace_id) == []
        assert storage.list_judgments_for_cluster(trace.cluster_id or "") == []
    finally:
        # The CI database is disposable, but scoped cleanup also makes reruns
        # deterministic and safe when a developer supplies a test database.
        for table, column, value in (
            ("evaluator_health", "health_id", f"{prefix}-health"),
            ("cluster_registries", "version", registry_version),
            ("drift_signals", "evaluator_fingerprint", fingerprint),
            ("drift_runs", "evaluator_fingerprint", fingerprint),
            ("user_signals", "trace_id", trace_id),
            ("spans", "trace_id", trace_id),
            ("judgments", "trace_id", trace_id),
            ("traces", "trace_id", trace_id),
        ):
            storage._exec(f"DELETE FROM {table} WHERE {column} = %s", (value,))  # nosec B608
        storage.close()


def test_live_postgres_persists_normalized_trace_scalars():
    """Schema normalization must protect the real PostgreSQL bind boundary."""
    trace_id = f"scalar-normalization-{uuid4().hex}"
    trace = Trace(
        trace_id=trace_id,
        provider="custom-provider",
        temperature=object(),
        max_tokens=True,
        input_tokens=object(),
        output_tokens=2**31,
        latency_ms=float("inf"),
        cost_usd=-1.0,
    )
    storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
    try:
        storage.insert_trace(trace)
        persisted = storage.get_trace(trace_id)
        assert persisted is not None
        assert persisted.temperature is None
        assert persisted.max_tokens is None
        assert persisted.input_tokens is None
        assert persisted.output_tokens is None
        assert persisted.latency_ms is None
        assert persisted.cost_usd is None
    finally:
        storage.delete_trace(trace_id)
        storage.close()


def test_live_postgres_drift_run_replacement_rolls_back_as_one_transaction():
    prefix = f"drift-rollback-{uuid4().hex}"
    fingerprint = f"{prefix}-evaluator"
    run = DriftRun(
        run_id=f"{prefix}-run",
        evaluator_fingerprint=fingerprint,
        signal_count=1,
    )
    original = DriftSignal(
        signal_id=f"{prefix}-original",
        evaluator_fingerprint=fingerprint,
        run_id=run.run_id,
    )
    storage = PostgresStorage(DSN, min_pool=1, max_pool=2)
    try:
        storage.replace_drift_run(run, [original])
        replacement_run = DriftRun(
            run_id=run.run_id,
            analysis_time=run.analysis_time,
            completed_at=run.completed_at + timedelta(minutes=1),
            evaluator_fingerprint=fingerprint,
            signal_count=1,
        )
        malformed = DriftSignal(
            signal_id=f"{prefix}-malformed",
            evaluator_fingerprint=fingerprint,
            run_id=run.run_id,
            contributing_layers=[object()],
        )

        with pytest.raises(TypeError):
            storage.replace_drift_run(replacement_run, [malformed])

        snapshot = storage.get_latest_drift_run_snapshot(fingerprint)
        assert snapshot is not None
        assert snapshot[0].completed_at == run.completed_at
        assert [signal.signal_id for signal in snapshot[1]] == [original.signal_id]
    finally:
        storage._exec(
            "DELETE FROM drift_signals WHERE evaluator_fingerprint = %s",
            (fingerprint,),
        )
        storage._exec(
            "DELETE FROM drift_runs WHERE evaluator_fingerprint = %s",
            (fingerprint,),
        )
        storage.close()


def test_live_postgres_concurrent_runs_cannot_claim_the_same_signal_id():
    prefix = f"drift-owner-race-{uuid4().hex}"
    fingerprint = f"{prefix}-evaluator"
    signal_id = f"{prefix}-shared-signal"
    storages = [
        PostgresStorage(DSN, min_pool=1, max_pool=2),
        PostgresStorage(DSN, min_pool=1, max_pool=2),
    ]
    gate = threading.Barrier(3)
    completed: list[str] = []
    errors: list[BaseException] = []

    def replace(storage: PostgresStorage, suffix: str) -> None:
        run_id = f"{prefix}-run-{suffix}"
        try:
            gate.wait()
            storage.replace_drift_run(
                DriftRun(
                    run_id=run_id,
                    evaluator_fingerprint=fingerprint,
                    signal_count=1,
                ),
                [DriftSignal(
                    signal_id=signal_id,
                    evaluator_fingerprint=fingerprint,
                    run_id=run_id,
                )],
            )
            completed.append(run_id)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=replace, args=(storages[0], "a")),
        threading.Thread(target=replace, args=(storages[1], "b")),
    ]
    try:
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(timeout=5)

        assert all(not thread.is_alive() for thread in threads)
        assert len(completed) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], ValueError)
        assert "another drift run" in str(errors[0])
        run_rows = storages[0]._fetchall(
            "SELECT run_id FROM drift_runs WHERE evaluator_fingerprint = %s",
            (fingerprint,),
        )
        assert run_rows == [(completed[0],)]
        snapshot = storages[0].get_latest_drift_run_snapshot(fingerprint)
        assert snapshot is not None
        assert snapshot[0].run_id == completed[0]
        assert [signal.signal_id for signal in snapshot[1]] == [signal_id]
    finally:
        for thread in threads:
            thread.join(timeout=5)
        storages[0]._exec(
            "DELETE FROM drift_signals WHERE evaluator_fingerprint = %s",
            (fingerprint,),
        )
        storages[0]._exec(
            "DELETE FROM drift_runs WHERE evaluator_fingerprint = %s",
            (fingerprint,),
        )
        for storage in storages:
            storage.close()


def test_live_postgres_span_upsert_can_clear_an_explicit_trace_link():
    """The non-NULL -> NULL update direction on the real backend.

    Only the NULL -> non-NULL direction was covered before. A COALESCE-based
    upsert passes that one and silently keeps a stale link on this one, which
    Explicit links may be cleared when their trace is deleted while another
    provider Trace still references the same manual span.
    """
    storage = PostgresStorage(DSN)
    try:
        suffix = uuid4().hex[:8]
        trace_id = f"trace-{suffix}"
        span_id = f"span-{suffix}"
        storage.insert_trace(Trace(trace_id=trace_id, provider="anthropic"))

        storage.insert_span(
            SpanRecord(span_id=span_id, name="retrieve", trace_id=trace_id)
        )
        linked = [s for s in storage.list_spans(limit=500) if s.span_id == span_id]
        assert linked and linked[0].trace_id == trace_id

        storage.insert_span(
            SpanRecord(
                span_id=span_id,
                name="retrieve",
                trace_id=None,
            )
        )
        cleared = [s for s in storage.list_spans(limit=500) if s.span_id == span_id]
        assert cleared, "span disappeared while clearing its explicit link"
        assert cleared[0].trace_id is None, "COALESCE kept a stale trace link"

        storage.delete_trace(trace_id)
    finally:
        storage.close()


def test_live_postgres_prune_removes_expired_standalone_and_orphan_spans():
    storage = PostgresStorage(DSN)
    suffix = uuid4().hex[:8]
    old_standalone = SpanRecord(
        span_id=f"old-standalone-{suffix}",
        name="old-standalone",
        started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    old_orphan = SpanRecord(
        span_id=f"old-orphan-{suffix}",
        name="old-orphan",
        trace_id=f"missing-trace-{suffix}",
        started_at=datetime(2020, 2, 1, tzinfo=timezone.utc),
    )
    recent_standalone = SpanRecord(
        span_id=f"recent-standalone-{suffix}",
        name="recent-standalone",
        started_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    try:
        for record in (old_standalone, old_orphan, recent_standalone):
            storage.insert_span(record)

        assert storage.prune_before(
            datetime(2021, 1, 1, tzinfo=timezone.utc).isoformat()
        ) == 0

        persisted = {
            record.span_id for record in storage.list_spans(limit=500)
        }
        assert old_standalone.span_id not in persisted
        assert old_orphan.span_id not in persisted
        assert recent_standalone.span_id in persisted
    finally:
        for record in (old_standalone, old_orphan, recent_standalone):
            storage._exec("DELETE FROM spans WHERE span_id = %s", (record.span_id,))
        storage.close()


def test_live_postgres_prune_preserves_span_referenced_by_retained_trace():
    storage = PostgresStorage(DSN)
    suffix = uuid4().hex[:8]
    parent = SpanRecord(
        span_id=f"retained-parent-{suffix}",
        name="long-running-parent",
        started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    child = Trace(
        trace_id=f"retained-child-{suffix}",
        parent_span_id=parent.span_id,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    try:
        storage.insert_span(parent)
        storage.insert_trace(child)

        storage.prune_before(datetime(2021, 1, 1, tzinfo=timezone.utc).isoformat())

        persisted = {record.span_id for record in storage.list_spans(limit=500)}
        assert parent.span_id in persisted
        assert storage.get_trace(child.trace_id) is not None
    finally:
        storage._exec("DELETE FROM traces WHERE trace_id = %s", (child.trace_id,))
        storage._exec("DELETE FROM spans WHERE span_id = %s", (parent.span_id,))
        storage.close()


def test_live_postgres_delete_clears_explicit_link_but_preserves_shared_span():
    storage = PostgresStorage(DSN)
    suffix = uuid4().hex[:8]
    explicit_id = f"explicit-{suffix}"
    provider_id = f"provider-{suffix}"
    parent = SpanRecord(
        span_id=f"shared-{suffix}",
        name="shared-parent",
        trace_id=explicit_id,
    )
    try:
        storage.insert_trace(Trace(trace_id=explicit_id))
        storage.insert_span(parent)
        storage.insert_trace(Trace(trace_id=provider_id, parent_span_id=parent.span_id))

        storage.delete_trace(explicit_id)

        [persisted] = [
            record for record in storage.list_spans(limit=500)
            if record.span_id == parent.span_id
        ]
        assert persisted.trace_id is None
        assert storage.get_trace(provider_id).parent_span_id == parent.span_id
    finally:
        storage._exec("DELETE FROM traces WHERE trace_id = %s", (provider_id,))
        storage._exec("DELETE FROM spans WHERE span_id = %s", (parent.span_id,))
        storage.close()


def test_live_postgres_delete_trace_rolls_back_every_cleanup_write_on_failure():
    import psycopg
    from psycopg import sql

    storage = PostgresStorage(DSN)
    suffix = uuid4().hex[:8]
    owner_id = f"atomic-owner-{suffix}"
    retained_id = f"atomic-retained-{suffix}"
    span_id = f"atomic-span-{suffix}"
    trigger_name = f"fail_span_clear_{suffix}"
    function_name = f"fail_span_clear_fn_{suffix}"
    try:
        storage.insert_trace(Trace(trace_id=owner_id))
        storage.insert_span(SpanRecord(
            span_id=span_id,
            name="atomic-shared",
            trace_id=owner_id,
        ))
        storage.insert_trace(Trace(
            trace_id=retained_id,
            parent_span_id=span_id,
        ))
        with storage._pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL(
                    """CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
                       BEGIN
                         IF OLD.trace_id = {} THEN
                           RAISE EXCEPTION 'forced cleanup failure';
                         END IF;
                         RETURN NEW;
                       END
                       $$"""
                ).format(sql.Identifier(function_name), sql.Literal(owner_id)))
                cursor.execute(sql.SQL(
                    """CREATE TRIGGER {} BEFORE UPDATE OF trace_id ON spans
                       FOR EACH ROW EXECUTE FUNCTION {}()"""
                ).format(
                    sql.Identifier(trigger_name),
                    sql.Identifier(function_name),
                ))

        with pytest.raises(psycopg.DatabaseError, match="forced cleanup failure"):
            storage.delete_trace(owner_id)

        assert storage.get_trace(owner_id) is not None
        [persisted] = [
            record for record in storage.list_spans(limit=500)
            if record.span_id == span_id
        ]
        assert persisted.trace_id == owner_id
    finally:
        with storage._pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP TRIGGER IF EXISTS {} ON spans").format(
                    sql.Identifier(trigger_name)
                ))
                cursor.execute(sql.SQL("DROP FUNCTION IF EXISTS {}()").format(
                    sql.Identifier(function_name)
                ))
        storage._exec("DELETE FROM traces WHERE trace_id = %s", (retained_id,))
        storage._exec("DELETE FROM spans WHERE span_id = %s", (span_id,))
        storage._exec("DELETE FROM traces WHERE trace_id = %s", (owner_id,))
        storage.close()


def test_live_postgres_delete_trace_serializes_concurrent_trace_insertion():
    import psycopg
    from psycopg import sql

    storage = PostgresStorage(DSN)
    inserting = PostgresStorage(DSN)
    suffix = uuid4().hex[:8]
    owner_id = f"concurrent-owner-{suffix}"
    child_id = f"concurrent-child-{suffix}"
    span_id = f"concurrent-span-{suffix}"
    trigger_name = f"pause_span_delete_{suffix}"
    function_name = f"pause_span_delete_fn_{suffix}"
    insert_done = threading.Event()
    delete_error: list[BaseException] = []
    try:
        storage.insert_trace(Trace(trace_id=owner_id))
        storage.insert_span(SpanRecord(
            span_id=span_id,
            name="concurrent-shared",
            trace_id=owner_id,
        ))
        with storage._pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL(
                    """CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
                       BEGIN
                         IF OLD.trace_id = {} THEN
                           PERFORM pg_sleep(1.5);
                         END IF;
                         RETURN OLD;
                       END
                       $$"""
                ).format(sql.Identifier(function_name), sql.Literal(owner_id)))
                cursor.execute(sql.SQL(
                    """CREATE TRIGGER {} BEFORE DELETE ON spans
                       FOR EACH ROW EXECUTE FUNCTION {}()"""
                ).format(
                    sql.Identifier(trigger_name),
                    sql.Identifier(function_name),
                ))

        def delete_owner() -> None:
            try:
                storage.delete_trace(owner_id)
            except BaseException as exc:
                delete_error.append(exc)

        delete_thread = threading.Thread(target=delete_owner)
        delete_thread.start()

        with psycopg.connect(DSN, autocommit=True) as observer:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                sleeping = observer.execute(
                    """SELECT EXISTS (
                         SELECT 1 FROM pg_stat_activity
                         WHERE wait_event = 'PgSleep'
                           AND query LIKE 'DELETE FROM spans AS candidate%'
                       )"""
                ).fetchone()[0]
                if sleeping:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("delete never reached the PostgreSQL cleanup trigger")

        def insert_child() -> None:
            inserting.insert_trace(Trace(
                trace_id=child_id,
                parent_span_id=span_id,
            ))
            insert_done.set()

        insert_thread = threading.Thread(target=insert_child)
        insert_thread.start()
        inserted_during_cleanup = insert_done.wait(timeout=0.25)
        delete_thread.join(timeout=5)
        insert_thread.join(timeout=5)

        assert not delete_thread.is_alive()
        assert not insert_thread.is_alive()
        assert delete_error == []
        assert not inserted_during_cleanup
    finally:
        with storage._pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP TRIGGER IF EXISTS {} ON spans").format(
                    sql.Identifier(trigger_name)
                ))
                cursor.execute(sql.SQL("DROP FUNCTION IF EXISTS {}()").format(
                    sql.Identifier(function_name)
                ))
        storage._exec("DELETE FROM traces WHERE trace_id = %s", (child_id,))
        storage._exec("DELETE FROM spans WHERE span_id = %s", (span_id,))
        storage._exec("DELETE FROM traces WHERE trace_id = %s", (owner_id,))
        inserting.close()
        storage.close()


def test_live_postgres_provider_span_correlation_succeeds_and_fails_independently():
    class FailingTraceOnly:
        def __init__(self, inner):
            self.inner = inner

        def insert_trace(self, trace):
            raise RuntimeError("simulated trace failure")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    suffix = uuid4().hex[:8]
    success_trace_id = f"lifecycle-success-{suffix}"
    failed_trace_id = f"lifecycle-failed-{suffix}"
    span_names = {
        f"success-parent-{suffix}",
        f"success-child-{suffix}",
        f"failed-parent-{suffix}",
        f"failed-child-{suffix}",
    }
    storage = PostgresStorage(DSN)
    try:
        client_module.shutdown()
        client = verdict.init(storage=storage, instrumentors=["none"])
        with span(f"success-parent-{suffix}") as success_parent:
            trace = Trace(trace_id=success_trace_id)
            apply_routing_context(client, trace)
            persist_trace(client, trace)
            with span(f"success-child-{suffix}"):
                pass

        success_records = [
            record for record in storage.list_spans(limit=500)
            if record.name in span_names
        ]
        assert len(success_records) == 2
        assert all(record.trace_id is None for record in success_records)
        assert storage.get_trace(success_trace_id).parent_span_id == success_parent.span_id

        client_module._client = None
        failing = FailingTraceOnly(storage)
        client = verdict.init(storage=failing, instrumentors=["none"])
        with span(f"failed-parent-{suffix}"):
            trace = Trace(trace_id=failed_trace_id)
            apply_routing_context(client, trace)
            with pytest.raises(RuntimeError, match="simulated trace failure"):
                persist_trace(client, trace)
            with span(f"failed-child-{suffix}"):
                pass

        failed_records = [
            record for record in storage.list_spans(limit=500)
            if record.name in span_names
        ]
        assert len(failed_records) == 4
        failed_records = [
            record for record in failed_records if record.name.startswith("failed-")
        ]
        assert all(record.trace_id is None for record in failed_records)
    finally:
        client_module._client = None
        storage._exec("DELETE FROM spans WHERE name = ANY(%s)", (list(span_names),))
        storage.delete_trace(success_trace_id)
        storage.close()


def test_live_buffered_postgres_inserts_each_manual_span_once():
    class CountingPostgres:
        def __init__(self, inner):
            self.inner = inner
            self.span_writes = 0

        def insert_span(self, record):
            self.span_writes += 1
            self.inner.insert_span(record)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    suffix = uuid4().hex[:8]
    trace_id = f"buffered-{suffix}"
    names = {f"buffered-parent-{suffix}", f"buffered-child-{suffix}"}
    postgres = PostgresStorage(DSN)
    counting = CountingPostgres(postgres)
    buffered = BufferedStorage(counting, flush_interval=10.0)
    try:
        client_module.shutdown()
        client = verdict.init(storage=buffered, instrumentors=["none"])
        with span(f"buffered-parent-{suffix}") as parent:
            trace = Trace(trace_id=trace_id)
            apply_routing_context(client, trace)
            persist_trace(client, trace)
            with span(f"buffered-child-{suffix}"):
                pass
        buffered.flush(timeout=5)

        records = [record for record in postgres.list_spans(limit=500) if record.name in names]
        assert len(records) == 2
        assert counting.span_writes == 2
        assert all(record.trace_id is None for record in records)
        assert postgres.get_trace(trace_id).parent_span_id == parent.span_id
    finally:
        client_module._client = None
        buffered.flush(timeout=5)
        postgres._exec("DELETE FROM spans WHERE name = ANY(%s)", (list(names),))
        postgres._exec("DELETE FROM traces WHERE trace_id = %s", (trace_id,))
        buffered.close()
