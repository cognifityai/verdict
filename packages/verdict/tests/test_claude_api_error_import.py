"""Claude Code API-error rows import as failed provider calls."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from verdict import AgentEventType, ExecutionStatus
from verdict.capture import AgentCaptureService
from verdict.evidence import AgentEvent
from verdict.monitoring import trace_monitor_units
from verdict.normalized_evidence import merge_agent_event
from verdict.storage import BufferedStorage, InMemoryStorage, SQLiteStorage
from verdict.telemetry.local_agents import capture_local_agents


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _assistant(uuid: str, message_id: str, text: str, **row: object) -> dict[str, object]:
    return {
        "timestamp": row.pop("timestamp"),
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "claude-session-1",
        "message": {
            "id": message_id,
            "model": row.pop("model", "claude-sonnet-4-5"),
            "stop_reason": row.pop("stop_reason", "end_turn"),
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "content": [{"type": "text", "text": text}],
        },
        **row,
    }


def _records() -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-08-30T11:00:00Z", "type": "user", "uuid": "user-1",
            "sessionId": "claude-session-1", "message": {"content": "refactor the parser"},
        },
        _assistant(
            "limit-1", "msg-limit", "Usage limit reached for this synthetic plan.",
            timestamp="2026-08-30T11:00:01Z", model="<synthetic>",
            stop_reason="stop_sequence", isApiErrorMessage=True,
            error="rate_limit", apiErrorStatus=429,
        ),
        {
            "timestamp": "2026-08-30T12:00:00Z", "type": "user", "uuid": "user-2",
            "sessionId": "claude-session-1", "message": {"content": "continue"},
        },
        _assistant(
            "auth-1", "msg-auth", "Synthetic authentication notice.",
            timestamp="2026-08-30T12:00:01Z", model="<synthetic>",
            stop_reason="stop_sequence", isApiErrorMessage=True,
            error="authentication_failed",
        ),
        _assistant(
            "ok-1", "msg-ok", "Parser refactored.", timestamp="2026-08-30T12:00:02Z",
        ),
    ]


def _storage(kind: str, tmp_path: Path, name: str):
    if kind == "memory":
        return InMemoryStorage()
    if kind == "buffered":
        return BufferedStorage(SQLiteStorage(str(tmp_path / f"{name}.db")))
    return SQLiteStorage(str(tmp_path / f"{name}.db"))


def _model_events(storage) -> list[AgentEvent]:
    [bundle] = storage.list_agent_run_bundles("local")
    return sorted(
        (event for event in bundle.events if event.event_type is AgentEventType.MODEL_CALL),
        key=lambda event: event.occurred_at,
    )


@pytest.mark.parametrize("kind", ["memory", "sqlite", "buffered"])
def test_api_error_rows_become_failed_provider_calls(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "claude"
    _write_jsonl(root / "session.jsonl", _records())
    storage = _storage(kind, tmp_path, "store")
    try:
        assert capture_local_agents(storage, tenant_id="local", claude_root=root).stored == 1

        events = _model_events(storage)
        assert [(event.status, event.attributes.get("error")) for event in events] == [
            (ExecutionStatus.FAILED, "rate_limit (HTTP 429)"),
            (ExecutionStatus.FAILED, "authentication_failed"),
            (ExecutionStatus.COMPLETED, None),
        ]
        traces = {trace.trace_id: trace for trace in storage.list_traces(tenant_id="local")}
        assert [traces[event.trace_id].error for event in events] == [
            "rate_limit (HTTP 429)", "authentication_failed", None,
        ]
        units = {unit.unit_id: unit for unit in trace_monitor_units(traces.values())}
        assert [units[event.trace_id].metrics["provider_error"] for event in events] == [
            True, True, False,
        ]
        # Run outcome is not inferred from a provider error: the run continued.
        [bundle] = storage.list_agent_run_bundles("local")
        assert bundle.run.status is not ExecutionStatus.FAILED
    finally:
        storage.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite", "buffered"])
@pytest.mark.parametrize("appended_later", [False, True])
def test_late_api_error_on_an_existing_message_fails_that_call(
    tmp_path: Path, kind: str, appended_later: bool,
) -> None:
    """Content streamed for a message, then the same message reported an API error."""
    root = tmp_path / "claude"
    records = [
        {
            "timestamp": "2026-08-30T11:00:00Z", "type": "user", "uuid": "user-1",
            "sessionId": "claude-session-1", "message": {"content": "refactor the parser"},
        },
        # Still streaming: no stop reason yet.
        _assistant("part-1", "msg-1", "Starting the refactor.",
                   timestamp="2026-08-30T11:00:01Z", stop_reason=None),
    ]
    late_error = _assistant(
        "late-1", "msg-1", "Synthetic overload notice.", timestamp="2026-08-30T11:00:02Z",
        stop_reason="stop_sequence", isApiErrorMessage=True, error="overloaded", apiErrorStatus=529,
    )
    storage = _storage(kind, tmp_path, "store")
    try:
        if appended_later:
            _write_jsonl(root / "session.jsonl", records)
            capture_local_agents(storage, tenant_id="local", claude_root=root)
            assert [e.status for e in _model_events(storage)] == [ExecutionStatus.COMPLETED]
        _write_jsonl(root / "session.jsonl", [*records, late_error])
        capture_local_agents(storage, tenant_id="local", claude_root=root)

        assert [(e.status, e.attributes.get("error")) for e in _model_events(storage)] == [
            (ExecutionStatus.FAILED, "overloaded (HTTP 529)"),
        ]
        assert [t.error for t in storage.list_traces(tenant_id="local")] == ["overloaded (HTTP 529)"]
    finally:
        storage.close()


def test_api_error_classification_ignores_message_text(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    records = _records()
    for record in records:
        record.pop("isApiErrorMessage", None)
    records[-1]["message"]["content"][0]["text"] = "You've hit your session limit"
    _write_jsonl(root / "session.jsonl", records)
    storage = InMemoryStorage()
    capture_local_agents(storage, tenant_id="local", claude_root=root)

    assert {event.status for event in _model_events(storage)} == {ExecutionStatus.COMPLETED}
    assert {trace.error for trace in storage.list_traces(tenant_id="local")} == {None}


@pytest.mark.parametrize(
    ("error", "status", "expected"),
    [
        (None, 429, "api_error (HTTP 429)"),
        ("", None, "api_error"),
        ("overloaded", True, "overloaded"),
        ("overloaded", 99, "overloaded"),
        ("overloaded", "529", "overloaded"),
        ("x" * 400, 503, "x" * 128 + " (HTTP 503)"),
    ],
)
def test_malformed_api_error_fields_stay_bounded(
    tmp_path: Path, error: object, status: object, expected: str,
) -> None:
    root = tmp_path / "claude"
    records = _records()[:2]
    records[1]["error"] = error
    records[1]["apiErrorStatus"] = status
    _write_jsonl(root / "session.jsonl", records)
    storage = InMemoryStorage()
    capture_local_agents(storage, tenant_id="local", claude_root=root)

    [trace] = storage.list_traces(tenant_id="local")
    assert trace.error == expected


@pytest.mark.parametrize("kind", ["memory", "sqlite", "buffered"])
def test_rescan_reclassifies_calls_stored_by_earlier_releases(tmp_path: Path, kind: str) -> None:
    """A store written before this classification rescans without a conflict."""
    root = tmp_path / "claude"
    _write_jsonl(root / "session.jsonl", _records())
    seed = SQLiteStorage(str(tmp_path / "seed.db"))
    capture_local_agents(seed, tenant_id="local", claude_root=root)
    [current_bundle] = seed.list_agent_run_bundles("local")
    current_traces = seed.list_traces(tenant_id="local")
    seed.close()

    def earlier(event: AgentEvent) -> AgentEvent:
        if event.event_type is not AgentEventType.MODEL_CALL:
            return event
        attributes = {
            key: value for key, value in event.attributes.items() if key != "error"
        }
        return replace(event, status=ExecutionStatus.COMPLETED, attributes=attributes)

    legacy_bundle = replace(
        current_bundle, events=tuple(earlier(event) for event in current_bundle.events),
    )
    legacy_traces = tuple(replace(trace, error=None) for trace in current_traces)
    storage = _storage(kind, tmp_path, "upgrade")
    try:
        AgentCaptureService(storage).capture(legacy_bundle, traces=legacy_traces)

        summary = capture_local_agents(storage, tenant_id="local", claude_root=root)

        assert summary.as_dict()["skipped"] == 0
        [upgraded] = storage.list_agent_run_bundles("local")
        assert upgraded == current_bundle
        assert storage.list_traces(tenant_id="local") == current_traces
    finally:
        storage.close()


def _model_event(status: ExecutionStatus, **attributes: object) -> AgentEvent:
    from datetime import datetime, timezone

    return AgentEvent(
        event_id="event-1", turn_id="turn-1", sequence=0,
        occurred_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        event_type=AgentEventType.MODEL_CALL, status=status,
        provenance="claude:assistant_message",
        attributes={"provider": "anthropic", **attributes},
    )


@pytest.mark.parametrize(
    ("current", "incoming"),
    [
        # A recorded failure never becomes a success.
        (_model_event(ExecutionStatus.FAILED, error="rate_limit"),
         _model_event(ExecutionStatus.COMPLETED)),
        # Reclassification needs the provider error the stored call lacked.
        (_model_event(ExecutionStatus.COMPLETED), _model_event(ExecutionStatus.FAILED)),
        # A stored error cannot be replaced by a different one.
        (_model_event(ExecutionStatus.COMPLETED, error="rate_limit"),
         _model_event(ExecutionStatus.FAILED, error="server_error")),
        # Other terminal transitions stay immutable.
        (_model_event(ExecutionStatus.COMPLETED),
         _model_event(ExecutionStatus.TIMED_OUT, error="rate_limit")),
    ],
)
def test_terminal_model_call_status_stays_immutable(
    current: AgentEvent, incoming: AgentEvent,
) -> None:
    with pytest.raises(ValueError, match="cannot be replaced"):
        merge_agent_event(current, incoming)


def test_non_model_events_are_not_reclassified() -> None:
    current = replace(
        _model_event(ExecutionStatus.COMPLETED),
        event_type=AgentEventType.TOOL_RESULT, attributes={"tool_name": "Bash"},
    )
    incoming = replace(current, status=ExecutionStatus.FAILED, attributes={"is_error": True})
    with pytest.raises(ValueError, match="cannot be replaced"):
        merge_agent_event(current, incoming)
