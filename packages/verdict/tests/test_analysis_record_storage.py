from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from verdict.analysis_records import (
    AnalysisRunStatus,
    DeliveryOutcome,
    DeterministicAnalysisRun,
    NotificationDeliveryAttempt,
)
from verdict.storage import BufferedStorage, InMemoryStorage, SQLiteStorage

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "sqlite", "buffered"])
def storage(request: pytest.FixtureRequest, tmp_path):
    if request.param == "memory":
        value = InMemoryStorage()
    elif request.param == "buffered":
        value = BufferedStorage(InMemoryStorage())
    else:
        value = SQLiteStorage(str(tmp_path / "analysis.db"))
    try:
        yield value
    finally:
        value.close()


def _analysis(
    *,
    analysis_id: str = "a" * 64,
    tenant_id: str = "tenant-a",
    input_fingerprint: str = "b" * 64,
    findings: list[dict[str, object]] | None = None,
) -> DeterministicAnalysisRun:
    return DeterministicAnalysisRun(
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        scope_key="agent-and-trace",
        cutoff=NOW,
        completed_at=NOW + timedelta(seconds=1),
        status=AnalysisRunStatus.COMPLETED,
        analyzer_version="agent-insights-v1",
        input_fingerprint=input_fingerprint,
        result={
            "schema": "deterministic-analysis-v1",
            "coverage": {"runs": 2, "traces": 3},
            "findings": findings if findings is not None else [],
        },
    )


def _attempt(
    *,
    attempt_id: str = "c" * 64,
    notification_id: str = "d" * 64,
    outcome: DeliveryOutcome = DeliveryOutcome.FAILED,
) -> NotificationDeliveryAttempt:
    return NotificationDeliveryAttempt(
        attempt_id=attempt_id,
        notification_id=notification_id,
        tenant_id="tenant-a",
        source_kind="analysis",
        source_id="a" * 64,
        destination_fingerprint="e" * 64,
        attempted_at=NOW,
        outcome=outcome,
        payload={"kind": "finding", "code": "tool_error", "runs": 2},
        http_status=503 if outcome is DeliveryOutcome.FAILED else 204,
        error_code="http_rejected" if outcome is DeliveryOutcome.FAILED else None,
    )


def test_analysis_snapshot_round_trips_explicit_zero_findings(storage) -> None:
    run = _analysis()

    storage.save_deterministic_analysis_run(run)
    storage.save_deterministic_analysis_run(run)

    assert storage.get_latest_deterministic_analysis_run(
        "tenant-a", "agent-and-trace"
    ) == run
    assert run.result["findings"] == []


def test_analysis_identity_cannot_change_content(storage) -> None:
    run = _analysis()
    storage.save_deterministic_analysis_run(run)

    with pytest.raises(ValueError, match="different content"):
        storage.save_deterministic_analysis_run(
            replace(run, result={**run.result, "findings": [{"code": "changed"}]})
        )


def test_same_analysis_input_is_idempotent_across_new_attempt_identity(storage) -> None:
    first = _analysis()
    duplicate = replace(
        first,
        analysis_id="f" * 64,
        completed_at=first.completed_at + timedelta(minutes=1),
    )

    storage.save_deterministic_analysis_run(first)
    storage.save_deterministic_analysis_run(duplicate)

    assert storage.get_latest_deterministic_analysis_run(
        "tenant-a", "agent-and-trace"
    ) == first


def test_analysis_snapshot_is_tenant_scoped_and_detached(storage) -> None:
    run = _analysis(findings=[{"code": "tool_error", "message": "safe"}])
    storage.save_deterministic_analysis_run(run)

    assert storage.get_latest_deterministic_analysis_run(
        "tenant-b", "agent-and-trace"
    ) is None
    loaded = storage.get_latest_deterministic_analysis_run(
        "tenant-a", "agent-and-trace"
    )
    assert loaded is not None
    loaded.result["findings"][0]["message"] = "mutated"
    assert storage.get_latest_deterministic_analysis_run(
        "tenant-a", "agent-and-trace"
    ).result["findings"][0]["message"] == "safe"


def test_analysis_snapshot_recursively_redacts_before_persistence(storage) -> None:
    run = _analysis(findings=[{
        "code": "tool_error",
        "metadata": {"contact": "customer@example.com"},
    }])

    storage.save_deterministic_analysis_run(run)

    loaded = storage.get_latest_deterministic_analysis_run(
        "tenant-a", "agent-and-trace"
    )
    assert loaded is not None
    assert loaded.result["findings"][0]["metadata"]["contact"] == "<EMAIL>"
    assert "customer@example.com" not in repr(loaded)


def test_delivery_attempts_are_immutable_ordered_and_record_success(storage) -> None:
    failed = _attempt()
    delivered = _attempt(
        attempt_id="1" * 64,
        outcome=DeliveryOutcome.DELIVERED,
    )
    delivered = replace(delivered, attempted_at=NOW + timedelta(seconds=1))

    storage.save_notification_delivery_attempt(failed)
    storage.save_notification_delivery_attempt(delivered)
    storage.save_notification_delivery_attempt(delivered)

    assert storage.list_notification_delivery_attempts(
        failed.notification_id, failed.destination_fingerprint
    ) == [delivered, failed]
    assert storage.notification_was_delivered(
        failed.notification_id, failed.destination_fingerprint
    ) is True
    assert storage.list_notification_delivery_attempts_for_tenant("tenant-a") == [
        delivered, failed,
    ]
    assert storage.list_notification_delivery_attempts_for_tenant("tenant-b") == []
    with pytest.raises(ValueError, match="different content"):
        storage.save_notification_delivery_attempt(
            replace(delivered, http_status=200)
        )


def test_delivery_attempt_query_rejects_unbounded_limit(storage) -> None:
    with pytest.raises(ValueError, match="limit"):
        storage.list_notification_delivery_attempts("d" * 64, "e" * 64, limit=0)
