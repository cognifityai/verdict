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
    Trace,
)
from verdict.capture import AgentCaptureService
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


def _linked_capture() -> tuple[AgentRunBundle, Trace]:
    bundle = _bundle()
    trace = Trace(
        trace_id="trace_1",
        tenant_id="tenant-a",
        started_at=NOW,
        ended_at=NOW,
        provider="custom",
        request_model="model",
        response_model="model",
        prompt_redacted="request",
        response_redacted="response",
    )
    event = replace(
        bundle.events[0],
        event_type=AgentEventType.MODEL_CALL,
        provenance="custom-agent:model",
        attributes={"provider": "custom", "response_model": "model"},
        privacy_classification=PrivacyClassification.METADATA,
        trace_id=trace.trace_id,
    )
    return replace(bundle, events=(event,)), trace


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


def test_atomic_agent_capture_has_adapter_parity(evidence_storage) -> None:
    linked, trace = _linked_capture()

    AgentCaptureService(evidence_storage).capture(linked, traces=(trace,))
    AgentCaptureService(evidence_storage).capture(linked, traces=(trace,))

    stored_trace = evidence_storage.get_trace(trace.trace_id)
    assert stored_trace is not None
    assert stored_trace.prompt_redacted == "request"
    assert stored_trace.response_redacted == "response"
    assert evidence_storage.get_agent_run_bundle("tenant-a", "run_1") == linked


def test_atomic_capture_guards_have_adapter_parity(evidence_storage) -> None:
    bundle, trace = _linked_capture()

    with pytest.raises(
        ValueError,
        match=r"^model-call event requires a same-tenant Trace$",
    ):
        evidence_storage.replace_agent_capture(
            bundle,
            (replace(trace, tenant_id="tenant-b"),),
        )
    with pytest.raises(ValueError, match=r"^agent capture contains duplicate trace_id$"):
        evidence_storage.replace_agent_capture(bundle, (trace, trace))
    with pytest.raises(
        ValueError,
        match=r"^model-call event requires a same-tenant Trace$",
    ):
        evidence_storage.replace_agent_capture(bundle)

    duplicate_link = replace(
        bundle.events[0],
        event_id="event_2",
        sequence=1,
    )
    with pytest.raises(
        ValueError,
        match=r"^agent capture links one Trace to multiple AgentEvents$",
    ):
        evidence_storage.replace_agent_capture(
            replace(bundle, events=(*bundle.events, duplicate_link)),
            (trace,),
        )

    evidence_storage.insert_trace(replace(trace, tenant_id="tenant-b"))
    with pytest.raises(
        ValueError,
        match=r"^model-call event requires a same-tenant Trace$",
    ):
        evidence_storage.replace_agent_capture(bundle)
    assert evidence_storage.get_agent_run_bundle("tenant-a", "run_1") is None


def test_atomic_capture_sanitizes_trace_without_mutating_input(evidence_storage) -> None:
    bundle, trace = _linked_capture()
    trace.prompt_redacted = "email customer@example.com"
    trace.raw_messages = [{"role": "user", "content": "email customer@example.com"}]

    evidence_storage.replace_agent_capture(bundle, (trace,))

    stored = evidence_storage.get_trace(trace.trace_id)
    assert stored is not None
    assert stored.prompt_redacted == "email <EMAIL>"
    assert stored.raw_messages == [{"role": "user", "content": "email <EMAIL>"}]
    assert trace.prompt_redacted == "email customer@example.com"


def test_atomic_capture_rejects_terminal_status_replacement(evidence_storage) -> None:
    bundle, trace = _linked_capture()
    evidence_storage.replace_agent_capture(bundle, (trace,))
    failed = replace(bundle, run=replace(bundle.run, status=ExecutionStatus.FAILED))

    with pytest.raises(ValueError, match=r"^run terminal status cannot be replaced$"):
        evidence_storage.replace_agent_capture(failed, (trace,))

    assert evidence_storage.get_agent_run_bundle("tenant-a", "run_1") == bundle


def test_deleting_linked_trace_preserves_event_and_clears_link(evidence_storage) -> None:
    bundle, trace = _linked_capture()
    AgentCaptureService(evidence_storage).capture(bundle, traces=(trace,))

    evidence_storage.delete_trace(trace.trace_id)

    loaded = evidence_storage.get_agent_run_bundle("tenant-a", "run_1")
    assert evidence_storage.get_trace(trace.trace_id) is None
    assert loaded is not None
    assert loaded.events[0].trace_id is None


def test_pruning_linked_trace_preserves_event_and_clears_link(evidence_storage) -> None:
    bundle, trace = _linked_capture()
    AgentCaptureService(evidence_storage).capture(bundle, traces=(trace,))

    assert evidence_storage.prune_before("2026-09-01T00:00:00+00:00") == 1

    loaded = evidence_storage.get_agent_run_bundle("tenant-a", "run_1")
    assert loaded is not None
    assert loaded.events[0].trace_id is None


def test_replace_bundle_rejects_conflicting_complete_revision(evidence_storage) -> None:
    original = _bundle(response="first")
    replacement = AgentRunBundle(
        session=original.session,
        run=original.run,
        turns=(replace(original.turns[0], final_response_redacted="second"),),
        events=original.events,
    )
    evidence_storage.replace_agent_run_bundle(original)

    with pytest.raises(ValueError, match="turn response"):
        evidence_storage.replace_agent_run_bundle(replacement)

    loaded = evidence_storage.get_agent_run_bundle("tenant-a", "run_1")
    assert loaded == original
    assert loaded is not None
    assert loaded.content_hash == original.content_hash


def test_import_source_locator_cannot_be_reassigned(evidence_storage) -> None:
    original = _bundle()
    conflicting = AgentRunBundle(
        session=replace(original.session, source_session_id="ses_2"),
        run=replace(original.run, run_id="run_2", source_session_id="ses_2"),
        turns=(replace(original.turns[0], turn_id="turn_2", run_id="run_2"),),
        events=(replace(original.events[0], event_id="event_2", turn_id="turn_2"),),
    )
    evidence_storage.replace_agent_run_bundle(original)

    with pytest.raises(ValueError, match=r"import source|existing evidence"):
        evidence_storage.replace_agent_run_bundle(conflicting)

    assert evidence_storage.get_agent_run_bundle("tenant-a", "run_1") == original
    assert evidence_storage.get_agent_run_bundle("tenant-a", "run_2") is None


def test_producer_sequence_cannot_be_reassigned_within_one_run(evidence_storage) -> None:
    original = _bundle()
    original = replace(
        original,
        events=(replace(original.events[0], producer_id="worker", producer_sequence=1),),
    )
    conflicting = replace(
        original,
        events=(
            *original.events,
            replace(
                original.events[0],
                event_id="event_2",
                sequence=1,
            ),
        ),
    )
    evidence_storage.replace_agent_run_bundle(original)

    with pytest.raises(ValueError, match=r"producer sequence|existing evidence"):
        evidence_storage.replace_agent_run_bundle(conflicting)

    assert evidence_storage.get_agent_run_bundle("tenant-a", "run_1") == original


def test_source_local_turn_and_event_ids_may_repeat_across_runs(evidence_storage) -> None:
    first = _bundle()
    second = AgentRunBundle(
        session=replace(
            first.session,
            source_session_id="ses_2",
            source_locator_hash="b" * 64,
        ),
        run=replace(first.run, run_id="run_2", source_session_id="ses_2"),
        turns=(replace(first.turns[0], run_id="run_2"),),
        events=first.events,
    )

    evidence_storage.replace_agent_run_bundle(first)
    evidence_storage.replace_agent_run_bundle(second)

    assert evidence_storage.get_agent_run_bundle("tenant-a", "run_1") == first
    assert evidence_storage.get_agent_run_bundle("tenant-a", "run_2") == second


def test_live_capture_limit_can_increase_without_rewriting_other_event_facts(
    evidence_storage,
) -> None:
    original = _bundle()
    marker = replace(
        original.events[0],
        event_type=AgentEventType.CONTEXT,
        provenance="verdict:capture_limit",
        attributes={"name": "source_events_omitted", "source": "1", "available": False},
        privacy_classification=PrivacyClassification.METADATA,
    )
    first = replace(original, events=(marker,))
    second = replace(
        first,
        events=(
            replace(
                marker,
                occurred_at=NOW.replace(microsecond=1),
                attributes=marker.attributes | {"source": "2"},
            ),
        ),
    )

    evidence_storage.replace_agent_run_bundle(first)
    evidence_storage.replace_agent_run_bundle(second)

    loaded = evidence_storage.get_agent_run_bundle("tenant-a", "run_1")
    assert loaded is not None
    assert loaded.events[0].occurred_at == NOW
    assert loaded.events[0].attributes["source"] == "2"


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
