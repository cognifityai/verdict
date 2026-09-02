from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from verdict import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
)
from verdict.storage import BufferedStorage, InMemoryStorage, SQLiteStorage

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _bundle(
    *, tenant_id: str = "tenant-a", response: str = "done", include_pii: bool = False
) -> AgentRunBundle:
    session = SourceSession(
        source_session_id="ses_1",
        tenant_id=tenant_id,
        source_kind="custom-agent",
        source_locator_hash="a" * 64,
        started_at=NOW,
        ended_at=NOW,
        observed_at=NOW,
    )
    run = AgentRun(
        run_id="run_1",
        source_session_id="ses_1",
        tenant_id=tenant_id,
        started_at=NOW,
        ended_at=NOW,
        status=ExecutionStatus.COMPLETED,
    )
    turn = AgentTurn(
        turn_id="turn_1",
        run_id="run_1",
        sequence=0,
        started_at=NOW,
        ended_at=NOW,
        status=ExecutionStatus.COMPLETED,
        user_request_redacted=(
            "test account customer@example.com" if include_pii else "test account"
        ),
        final_response_redacted=response,
        request_state=EvidenceState.PRESENT,
        response_state=EvidenceState.PRESENT,
    )
    event = AgentEvent(
        event_id="event_1",
        turn_id="turn_1",
        sequence=0,
        occurred_at=NOW,
        event_type=AgentEventType.TOOL_RESULT,
        status=ExecutionStatus.COMPLETED,
        provenance="custom-agent:tool-result",
        attributes={
            "tool_name": "lookup",
            "call_id": "call-1",
            "result": {
                "email": "customer@example.com" if include_pii else "redacted",
                "status": "found",
            },
            "is_error": False,
        },
        privacy_classification=PrivacyClassification.REDACTED,
    )
    return AgentRunBundle(session=session, run=run, turns=(turn,), events=(event,))


@pytest.fixture(params=["memory", "sqlite", "buffered"])
def evidence_storage(request: pytest.FixtureRequest, tmp_path):
    if request.param == "memory":
        storage = InMemoryStorage()
    elif request.param == "buffered":
        storage = BufferedStorage(InMemoryStorage())
    else:
        storage = SQLiteStorage(str(tmp_path / "evidence.db"))
    try:
        yield storage
    finally:
        storage.close()


def test_replace_and_read_bundle_is_idempotent_and_tenant_scoped(evidence_storage) -> None:
    original = _bundle()

    evidence_storage.replace_agent_run_bundle(original)
    evidence_storage.replace_agent_run_bundle(original)

    assert evidence_storage.get_agent_run_bundle("tenant-a", "run_1") == original
    assert evidence_storage.get_agent_run_bundle("tenant-b", "run_1") is None
    assert evidence_storage.list_agent_run_bundles("tenant-a", limit=10) == [original]
    assert evidence_storage.has_agent_run_source_kind("tenant-a", "custom-agent") is True
    assert evidence_storage.has_agent_run_source_kind("tenant-a", "codex") is False


def test_replace_bundle_atomically_publishes_one_complete_revision(evidence_storage) -> None:
    original = _bundle(response="first")
    replacement = AgentRunBundle(
        session=original.session,
        run=original.run,
        turns=(replace(original.turns[0], final_response_redacted="second"),),
        events=original.events,
    )
    evidence_storage.replace_agent_run_bundle(original)

    evidence_storage.replace_agent_run_bundle(replacement)

    loaded = evidence_storage.get_agent_run_bundle("tenant-a", "run_1")
    assert loaded == replacement
    assert loaded is not None
    assert loaded.content_hash == replacement.content_hash


def test_storage_redacts_nested_agent_evidence_before_persistence(evidence_storage) -> None:
    evidence_storage.replace_agent_run_bundle(_bundle(include_pii=True))

    loaded = evidence_storage.get_agent_run_bundle("tenant-a", "run_1")

    assert loaded is not None
    assert loaded.turns[0].user_request_redacted == "test account <EMAIL>"
    assert loaded.events[0].attributes["result"]["email"] == "<EMAIL>"
    assert "customer@example.com" not in repr(loaded)


def test_read_bundle_is_detached_from_stored_state(evidence_storage) -> None:
    evidence_storage.replace_agent_run_bundle(_bundle())
    loaded = evidence_storage.get_agent_run_bundle("tenant-a", "run_1")
    assert loaded is not None

    loaded.events[0].attributes["result"]["status"] = "mutated"

    reread = evidence_storage.get_agent_run_bundle("tenant-a", "run_1")
    assert reread is not None
    assert reread.events[0].attributes["result"]["status"] == "found"


def test_list_bundle_rejects_unbounded_limit(evidence_storage) -> None:
    with pytest.raises(ValueError, match="limit"):
        evidence_storage.list_agent_run_bundles("tenant-a", limit=0)

    with pytest.raises(ValueError, match="limit"):
        evidence_storage.list_agent_run_bundles("tenant-a", limit=1001)
