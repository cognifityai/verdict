from __future__ import annotations

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
    agent_run_bundle_from_json,
    agent_run_bundle_to_json,
    stable_evidence_id,
)

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _bundle() -> AgentRunBundle:
    session = SourceSession(
        source_session_id="ses_1",
        tenant_id="tenant-a",
        source_kind="unknown-agent",
        source_locator_hash="a" * 64,
        started_at=NOW,
        ended_at=NOW,
        observed_at=NOW,
    )
    run = AgentRun(
        run_id="run_1",
        source_session_id="ses_1",
        tenant_id="tenant-a",
        started_at=NOW,
        ended_at=NOW,
        status=ExecutionStatus.COMPLETED,
        agent_name="custom-agent",
        agent_version="1",
    )
    turn = AgentTurn(
        turn_id="turn_1",
        run_id="run_1",
        sequence=0,
        started_at=NOW,
        ended_at=NOW,
        status=ExecutionStatus.COMPLETED,
        user_request_redacted="run the tests",
        final_response_redacted="tests passed",
        request_state=EvidenceState.PRESENT,
        response_state=EvidenceState.PRESENT,
    )
    event = AgentEvent(
        event_id="evt_1",
        turn_id="turn_1",
        sequence=0,
        occurred_at=NOW,
        event_type=AgentEventType.TEST_RESULT,
        status=ExecutionStatus.COMPLETED,
        provenance="unknown-agent:test-event",
        attributes={"command": "pytest", "exit_code": 0, "passed": 12},
        privacy_classification=PrivacyClassification.REDACTED,
    )
    return AgentRunBundle(session=session, run=run, turns=(turn,), events=(event,))


def test_bundle_has_canonical_hash_and_accepts_unknown_source_kind() -> None:
    first = _bundle()
    second = _bundle()

    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64
    assert first.session.source_kind == "unknown-agent"


def test_bundle_round_trip_revalidates_canonical_storage_shape() -> None:
    original = _bundle()

    loaded = agent_run_bundle_from_json(agent_run_bundle_to_json(original))

    assert loaded == original
    assert loaded.content_hash == original.content_hash


def test_bundle_reader_rejects_unknown_persisted_fields() -> None:
    payload = agent_run_bundle_to_json(_bundle()).replace(
        '"source_kind":"unknown-agent"',
        '"unexpected":"field","source_kind":"unknown-agent"',
    )

    with pytest.raises(ValueError, match="invalid typed fields"):
        agent_run_bundle_from_json(payload)


def test_stable_identity_does_not_expose_source_locator() -> None:
    identifier = stable_evidence_id(
        "run", "claude-code", "/private/customer/project", "source-id-123"
    )

    assert identifier.startswith("run_")
    assert "private" not in identifier
    assert "source-id" not in identifier
    assert identifier == stable_evidence_id(
        "run", "claude-code", "/private/customer/project", "source-id-123"
    )


def test_bundle_rejects_event_with_foreign_turn() -> None:
    original = _bundle()
    bad_event = AgentEvent(
        event_id="evt_bad",
        turn_id="turn_other",
        sequence=0,
        occurred_at=NOW,
        event_type=AgentEventType.TOOL_CALL,
        status=ExecutionStatus.COMPLETED,
        provenance="unknown-agent:tool",
        attributes={"tool_name": "shell", "call_id": "call-1"},
        privacy_classification=PrivacyClassification.METADATA,
    )

    with pytest.raises(ValueError, match="unknown turn"):
        AgentRunBundle(
            session=original.session,
            run=original.run,
            turns=original.turns,
            events=(bad_event,),
        )


def test_bundle_rejects_duplicate_or_noncontiguous_sequences() -> None:
    original = _bundle()
    second_turn = AgentTurn(
        turn_id="turn_2",
        run_id="run_1",
        sequence=2,
        started_at=NOW,
        status=ExecutionStatus.UNKNOWN,
        request_state=EvidenceState.NOT_CAPTURED,
        response_state=EvidenceState.NOT_CAPTURED,
    )

    with pytest.raises(ValueError, match="contiguous"):
        AgentRunBundle(
            session=original.session,
            run=original.run,
            turns=(*original.turns, second_turn),
            events=original.events,
        )


def test_event_rejects_source_envelope_and_unbounded_nested_values() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        AgentEvent(
            event_id="evt_envelope",
            turn_id="turn_1",
            sequence=0,
            occurred_at=NOW,
            event_type=AgentEventType.TOOL_RESULT,
            status=ExecutionStatus.COMPLETED,
            provenance="claude-code:tool",
            attributes={"raw_source_envelope": {"secret": "do-not-store"}},
            privacy_classification=PrivacyClassification.REDACTED,
        )

    with pytest.raises(ValueError, match="bounded"):
        AgentEvent(
            event_id="evt_large",
            turn_id="turn_1",
            sequence=0,
            occurred_at=NOW,
            event_type=AgentEventType.TOOL_RESULT,
            status=ExecutionStatus.COMPLETED,
            provenance="claude-code:tool",
            attributes={"result": "x" * 4097},
            privacy_classification=PrivacyClassification.REDACTED,
        )


def test_trace_link_is_only_valid_for_model_call_event() -> None:
    with pytest.raises(ValueError, match="trace_id"):
        AgentEvent(
            event_id="evt_tool",
            turn_id="turn_1",
            sequence=0,
            occurred_at=NOW,
            event_type=AgentEventType.TOOL_CALL,
            status=ExecutionStatus.COMPLETED,
            provenance="codex:tool",
            attributes={"tool_name": "shell", "call_id": "call-1"},
            privacy_classification=PrivacyClassification.METADATA,
            trace_id="trace_1",
        )


def test_omitted_content_requires_reason_and_cannot_retain_text() -> None:
    with pytest.raises(ValueError, match="omission_reason"):
        AgentEvent(
            event_id="evt_context",
            turn_id="turn_1",
            sequence=0,
            occurred_at=NOW,
            event_type=AgentEventType.CONTEXT,
            status=ExecutionStatus.UNKNOWN,
            provenance="codex:context",
            attributes={"name": "system"},
            privacy_classification=PrivacyClassification.OMITTED,
        )

    with pytest.raises(ValueError, match="cannot retain"):
        AgentEvent(
            event_id="evt_context",
            turn_id="turn_1",
            sequence=0,
            occurred_at=NOW,
            event_type=AgentEventType.CONTEXT,
            status=ExecutionStatus.UNKNOWN,
            provenance="codex:context",
            attributes={"name": "system", "text": "secret"},
            privacy_classification=PrivacyClassification.OMITTED,
            omission_reason="content capture disabled",
        )


def test_content_attributes_cannot_be_mislabeled_as_metadata() -> None:
    with pytest.raises(ValueError, match="cannot be classified as metadata"):
        AgentEvent(
            event_id="evt_content",
            turn_id="turn_1",
            sequence=0,
            occurred_at=NOW,
            event_type=AgentEventType.TOOL_RESULT,
            status=ExecutionStatus.COMPLETED,
            provenance="codex:tool_result",
            attributes={"tool_name": "shell", "result": "sensitive"},
            privacy_classification=PrivacyClassification.METADATA,
        )


@pytest.mark.parametrize(
    ("event_type", "attributes", "message"),
    [
        (AgentEventType.MODEL_CALL, {"input_tokens": True}, "non-negative integer"),
        (AgentEventType.MODEL_CALL, {"latency_ms": -1}, "non-negative number"),
        (AgentEventType.TOOL_RESULT, {"is_error": "yes"}, "must be boolean"),
        (AgentEventType.COMMAND, {"exit_code": 1.5}, "bounded integer"),
        (AgentEventType.CONTEXT, {"name": 7}, "must be text"),
    ],
)
def test_event_fields_enforce_declared_scalar_types(
    event_type: AgentEventType, attributes: dict[str, object], message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        AgentEvent(
            event_id="evt_typed",
            turn_id="turn_1",
            sequence=0,
            occurred_at=NOW,
            event_type=event_type,
            status=ExecutionStatus.UNKNOWN,
            provenance="source:event",
            attributes=attributes,
        )
