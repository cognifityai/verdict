from __future__ import annotations

import inspect
import json
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_type_hints

import pytest
import verdict.read_port as read_port_module
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
from verdict.analysis import AgentRunAnalysis, Finding
from verdict.read_port import (
    AGENT_ANALYSIS_VERSION,
    AGENT_RUN_READ_SCHEMA_VERSION,
    SUPPORTED_AGENT_ANALYSIS_VERSIONS,
    AgentRunRead,
    FindingRead,
    ModelCallRead,
    StorageVerdictReadPort,
    VerdictReadError,
    VerdictReadPort,
    agent_run_read_to_json,
)
from verdict.storage import BufferedStorage, InMemoryStorage, SQLiteStorage
from verdict.telemetry.local_agents import capture_local_agents

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)


def _model_call(
    event_id: str = "model-event",
    *,
    trace_id: str | None = "trace-1",
    occurred_at: datetime = NOW,
    sequence: int = 0,
    turn_id: str = "turn-1",
) -> AgentEvent:
    return AgentEvent(
        event_id=event_id,
        turn_id=turn_id,
        sequence=sequence,
        occurred_at=occurred_at,
        event_type=AgentEventType.MODEL_CALL,
        status=ExecutionStatus.COMPLETED,
        provenance="test:model",
        attributes={
            "provider": "PROVIDER_CONTENT_CANARY",
            "request_model": "REQUEST_MODEL_CANARY",
            "response_model": "RESPONSE_MODEL_CANARY",
            "latency_ms": 12.5,
        },
        privacy_classification=PrivacyClassification.REDACTED,
        trace_id=trace_id,
    )


def _bundle(
    *,
    tenant_id: str = "tenant-included",
    run_id: str = "run-included",
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    events: tuple[AgentEvent, ...] | None = None,
    turns: tuple[AgentTurn, ...] | None = None,
) -> AgentRunBundle:
    session = SourceSession(
        source_session_id="source-1",
        tenant_id=tenant_id,
        source_kind="custom-agent",
        source_locator_hash="a" * 64,
        started_at=NOW,
        observed_at=NOW,
        ended_at=NOW + timedelta(seconds=10),
    )
    run = AgentRun(
        run_id=run_id,
        source_session_id=session.source_session_id,
        tenant_id=tenant_id,
        started_at=NOW,
        status=status,
        ended_at=None if status is ExecutionStatus.UNKNOWN else NOW + timedelta(seconds=10),
        agent_name="AGENT_NAME_CANARY",
        agent_version="AGENT_VERSION_CANARY",
        configuration_fingerprint="CONFIGURATION_CANARY",
        service_name="SERVICE_NAME_CANARY",
        environment="ENVIRONMENT_CANARY",
        instance_id="INSTANCE_CANARY",
    )
    default_turn = AgentTurn(
        turn_id="turn-1",
        run_id=run_id,
        sequence=0,
        started_at=NOW,
        status=ExecutionStatus.COMPLETED,
        ended_at=NOW + timedelta(seconds=10),
        user_request_redacted="PROMPT_CONTENT_CANARY",
        final_response_redacted="RESPONSE_CONTENT_CANARY",
        request_state=EvidenceState.PRESENT,
        response_state=EvidenceState.PRESENT,
    )
    selected_turns = (default_turn,) if turns is None else turns
    selected_events = (_model_call(),) if events is None else events
    return AgentRunBundle(session, run, selected_turns, selected_events)


class _StorageStub:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def get_agent_run_bundle(self, tenant_id: str, run_id: str) -> object:
        self.calls.append((tenant_id, run_id))
        if self.error is not None:
            raise self.error
        return self.result


def _empty_read(**changes: object) -> AgentRunRead:
    values: dict[str, object] = {
        "schema_version": AGENT_RUN_READ_SCHEMA_VERSION,
        "analysis_version": AGENT_ANALYSIS_VERSION,
        "tenant_id": "tenant-a",
        "run_id": "run-a",
        "started_at": NOW,
        "ended_at": NOW,
        "status": "completed",
        "model_call_count": 0,
        "model_calls": (),
        "model_calls_truncated": False,
        "finding_count": 0,
        "findings": (),
        "findings_truncated": False,
    }
    values.update(changes)
    return AgentRunRead(**values)  # type: ignore[arg-type]


def test_public_v1_contract_has_exact_fields_and_signatures() -> None:
    assert [field.name for field in fields(ModelCallRead)] == [
        "event_id",
        "occurred_at",
        "status",
        "trace_id",
        "latency_ms",
    ]
    assert [field.name for field in fields(FindingRead)] == [
        "code",
        "severity",
        "witness_event_ids",
    ]
    assert [field.name for field in fields(AgentRunRead)] == [
        "schema_version",
        "analysis_version",
        "tenant_id",
        "run_id",
        "started_at",
        "ended_at",
        "status",
        "model_call_count",
        "model_calls",
        "model_calls_truncated",
        "finding_count",
        "findings",
        "findings_truncated",
    ]
    assert get_type_hints(ModelCallRead) == {
        "event_id": str,
        "occurred_at": datetime,
        "status": str,
        "trace_id": str | None,
        "latency_ms": float | None,
    }
    assert get_type_hints(FindingRead) == {
        "code": str,
        "severity": str,
        "witness_event_ids": tuple[str, ...],
    }
    assert get_type_hints(AgentRunRead) == {
        "schema_version": str,
        "analysis_version": str,
        "tenant_id": str,
        "run_id": str,
        "started_at": datetime,
        "ended_at": datetime | None,
        "status": str,
        "model_call_count": int,
        "model_calls": tuple[ModelCallRead, ...],
        "model_calls_truncated": bool,
        "finding_count": int,
        "findings": tuple[FindingRead, ...],
        "findings_truncated": bool,
    }
    assert list(inspect.signature(VerdictReadPort.get_agent_run).parameters) == [
        "self",
        "tenant_id",
        "run_id",
    ]
    assert (
        inspect.signature(VerdictReadPort.get_agent_run).parameters["tenant_id"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )
    assert list(inspect.signature(agent_run_read_to_json).parameters) == ["value"]
    assert AGENT_RUN_READ_SCHEMA_VERSION == "verdict.agent-run-read.v1"
    assert AGENT_ANALYSIS_VERSION == "verdict.agent-analysis.v1"
    assert SUPPORTED_AGENT_ANALYSIS_VERSIONS == frozenset({AGENT_ANALYSIS_VERSION})


def test_read_error_has_stable_public_shape() -> None:
    assert VerdictReadError.__bases__ == (RuntimeError,)
    error = VerdictReadError("read_unavailable")
    assert error.code == "read_unavailable"
    assert str(error) == "read_unavailable"
    assert error.args == ("read_unavailable",)
    with pytest.raises(ValueError, match="unsupported Verdict read error code"):
        VerdictReadError("database password leaked")
    with pytest.raises(ValueError, match="unsupported Verdict read error code"):
        VerdictReadError([])  # type: ignore[arg-type]


def _assert_detached_read_error(error: VerdictReadError, code: str, canary: str) -> None:
    assert error.code == code
    assert str(error) == code
    assert error.args == (code,)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert canary not in repr(error)


@pytest.mark.parametrize(
    ("status", "ended_at"),
    [
        ("completed", NOW),
        ("failed", NOW),
        ("timed_out", NOW),
        ("cancelled", NOW),
        ("unknown", None),
    ],
)
def test_agent_run_read_accepts_every_frozen_status(status: str, ended_at: datetime | None) -> None:
    assert _empty_read(status=status, ended_at=ended_at).status == status


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "verdict.agent-run-read.v2"},
        {"analysis_version": "verdict.agent-analysis.v2"},
        {"tenant_id": ""},
        {"tenant_id": "bad\x00tenant"},
        {"run_id": "x" * 257},
        {"run_id": "\ud800"},
        {"started_at": datetime(2026, 9, 11)},
        {"ended_at": NOW - timedelta(seconds=1)},
        {"status": "successful"},
        {"ended_at": None},
        {"model_call_count": True},
        {"model_calls": []},
        {"model_calls_truncated": 0},
        {"finding_count": -1},
        {"findings": []},
        {"findings_truncated": 0},
    ],
)
def test_agent_run_read_rejects_invalid_scalar_and_exact_container_shapes(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _empty_read(**changes)


def test_agent_run_read_enforces_collection_relations_and_unique_identities() -> None:
    call = ModelCallRead("event-1", NOW, "completed", "trace-1", 1)
    duplicate_event = ModelCallRead("event-1", NOW, "completed", "trace-2", 2)
    duplicate_trace = ModelCallRead("event-2", NOW, "completed", "trace-1", 2)
    finding = FindingRead("code", "warning", ("event-1",))

    with pytest.raises(ValueError, match="model_calls does not match"):
        _empty_read(model_call_count=0, model_calls=(call,))
    with pytest.raises(ValueError, match="model_calls_truncated does not match"):
        _empty_read(model_call_count=1, model_calls=(call,), model_calls_truncated=True)
    with pytest.raises(ValueError, match="event IDs must be unique"):
        _empty_read(model_call_count=2, model_calls=(call, duplicate_event))
    with pytest.raises(ValueError, match="Trace IDs must be unique"):
        _empty_read(model_call_count=2, model_calls=(call, duplicate_trace))
    with pytest.raises(ValueError, match="findings does not match"):
        _empty_read(finding_count=0, findings=(finding,))
    with pytest.raises(ValueError, match="findings_truncated does not match"):
        _empty_read(finding_count=1, findings=(finding,), findings_truncated=True)


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: ModelCallRead("", NOW, "completed", None, None),
        lambda: ModelCallRead("event", NOW, "other", None, None),
        lambda: ModelCallRead("event", NOW, "completed", "x" * 257, None),
        lambda: ModelCallRead("event", NOW, "completed", None, False),
        lambda: ModelCallRead("event", NOW, "completed", None, 10**1000),
        lambda: FindingRead("", "info", ()),
        lambda: FindingRead("code", "critical", ()),
        lambda: FindingRead("code", "info", ["event"]),
        lambda: FindingRead("code", "info", ("event", "event")),
        lambda: FindingRead("code", "info", tuple(f"event-{index}" for index in range(21))),
    ],
)
def test_nested_read_dtos_fail_closed(constructor) -> None:
    with pytest.raises(ValueError):
        constructor()


def test_datetime_normalization_and_canonical_json_shape() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    call = ModelCallRead(
        "event-1",
        datetime(2026, 9, 11, 17, 30, 0, 123456, tzinfo=offset),
        "completed",
        None,
        7,
    )
    finding = FindingRead("finding", "info", ("event-1",))
    value = _empty_read(
        started_at=datetime(2026, 9, 11, 17, 30, tzinfo=offset),
        ended_at=datetime(2026, 9, 11, 17, 31, tzinfo=offset),
        model_call_count=1,
        model_calls=(call,),
        finding_count=1,
        findings=(finding,),
    )

    encoded = agent_run_read_to_json(value)
    payload = json.loads(encoded)

    assert value.started_at == NOW
    assert call.occurred_at == NOW.replace(microsecond=123456)
    assert call.latency_ms == 7.0
    assert payload["started_at"] == "2026-09-11T12:00:00Z"
    assert payload["ended_at"] == "2026-09-11T12:01:00Z"
    assert payload["model_calls"][0]["occurred_at"] == "2026-09-11T12:00:00.123456Z"
    assert set(payload) == {field.name for field in fields(AgentRunRead)}
    assert set(payload["model_calls"][0]) == {field.name for field in fields(ModelCallRead)}
    assert set(payload["findings"][0]) == {field.name for field in fields(FindingRead)}
    assert list(payload) == sorted(payload)
    assert list(payload["model_calls"][0]) == sorted(payload["model_calls"][0])
    assert list(payload["findings"][0]) == sorted(payload["findings"][0])
    assert encoded == agent_run_read_to_json(value)


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: ModelCallRead(
            "event",
            datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
            "completed",
            None,
            None,
        ),
        lambda: ModelCallRead(
            "event",
            datetime.max.replace(tzinfo=timezone(-timedelta(hours=1))),
            "completed",
            None,
            None,
        ),
        lambda: _empty_read(started_at=datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))),
        lambda: _empty_read(started_at=datetime.max.replace(tzinfo=timezone(-timedelta(hours=1)))),
        lambda: _empty_read(ended_at=datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))),
        lambda: _empty_read(ended_at=datetime.max.replace(tzinfo=timezone(-timedelta(hours=1)))),
    ],
)
def test_datetime_normalization_overflow_is_a_detached_value_error(constructor) -> None:
    with pytest.raises(ValueError) as error:
        constructor()
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_worst_case_valid_json_remains_below_the_response_limit() -> None:
    def bounded_unique(prefix: str, index: int) -> str:
        suffix = f"{prefix}{index:03d}"
        return "\x01" * (256 - len(suffix)) + suffix

    calls = tuple(
        ModelCallRead(
            bounded_unique("e", index),
            NOW,
            "completed",
            bounded_unique("t", index),
            1.0,
        )
        for index in range(16)
    )
    findings = tuple(
        FindingRead(
            f"finding-{index}",
            "warning",
            tuple(bounded_unique(f"w{index}", witness) for witness in range(20)),
        )
        for index in range(4)
    )
    value = _empty_read(
        tenant_id="\x01" * 256,
        run_id="\x02" * 256,
        model_call_count=16,
        model_calls=calls,
        finding_count=4,
        findings=findings,
    )

    assert len(agent_run_read_to_json(value).encode("utf-8")) < 262_144


def test_serializer_revalidates_and_enforces_final_encoded_byte_limit(monkeypatch) -> None:
    value = _empty_read()
    object.__setattr__(value, "status", "tampered")
    with pytest.raises(VerdictReadError) as malformed:
        agent_run_read_to_json(value)
    assert malformed.value.code == "invalid_read_model"

    valid = _empty_read()
    monkeypatch.setattr(read_port_module, "_MAX_RESPONSE_BYTES", 1)
    with pytest.raises(VerdictReadError) as oversized:
        agent_run_read_to_json(valid)
    assert oversized.value.code == "response_limit_exceeded"


def test_exact_lookup_validates_before_io_and_detaches_storage_errors() -> None:
    canary = "postgres://user:secret@example.invalid"
    storage = _StorageStub(error=RuntimeError(canary))
    port = StorageVerdictReadPort(storage)  # type: ignore[arg-type]

    with pytest.raises(VerdictReadError) as invalid:
        port.get_agent_run(tenant_id="", run_id="run")
    _assert_detached_read_error(invalid.value, "invalid_query", canary)
    assert storage.calls == []

    with pytest.raises(VerdictReadError) as unavailable:
        port.get_agent_run(tenant_id="tenant", run_id="run")
    _assert_detached_read_error(unavailable.value, "read_unavailable", canary)


def test_projection_and_serializer_errors_are_detached(monkeypatch) -> None:
    canary = "projection-or-serializer-secret-canary"

    def invalid_projection(*args: object, **kwargs: object) -> AgentRunRead:
        raise RuntimeError(canary)

    monkeypatch.setattr(read_port_module, "_project_agent_run", invalid_projection)
    with pytest.raises(VerdictReadError) as projection:
        StorageVerdictReadPort(_StorageStub(_bundle())).get_agent_run(
            tenant_id="tenant-included", run_id="run-included"
        )
    _assert_detached_read_error(projection.value, "invalid_read_model", canary)

    def invalid_serializer(*args: object, **kwargs: object) -> str:
        raise RuntimeError(canary)

    monkeypatch.setattr(read_port_module.json, "dumps", invalid_serializer)
    with pytest.raises(VerdictReadError) as serializer:
        agent_run_read_to_json(_empty_read())
    _assert_detached_read_error(serializer.value, "invalid_read_model", canary)


def test_absent_and_mismatched_exact_lookups_are_distinct() -> None:
    absent = _StorageStub()
    assert (
        StorageVerdictReadPort(absent).get_agent_run(tenant_id="tenant", run_id="missing") is None
    )
    assert absent.calls == [("tenant", "missing")]

    mismatch = _StorageStub(_bundle(tenant_id="other", run_id="run"))
    with pytest.raises(VerdictReadError) as error:
        StorageVerdictReadPort(mismatch).get_agent_run(tenant_id="tenant", run_id="run")
    assert error.value.code == "invalid_read_model"


def test_projection_excludes_content_and_display_metadata_but_keeps_exact_ids() -> None:
    tool_event = AgentEvent(
        "tool-event",
        "turn-1",
        1,
        NOW,
        AgentEventType.TOOL_RESULT,
        ExecutionStatus.FAILED,
        "test:tool",
        {
            "tool_name": "TOOL_NAME_CANARY",
            "call_id": "call-1",
            "result": "TOOL_RESULT_CANARY",
            "is_error": True,
        },
        privacy_classification=PrivacyClassification.REDACTED,
    )
    command_event = AgentEvent(
        "command-event",
        "turn-1",
        2,
        NOW,
        AgentEventType.COMMAND,
        ExecutionStatus.FAILED,
        "test:command",
        {
            "command": "COMMAND_CONTENT_CANARY",
            "stderr": "ERROR_CONTENT_CANARY",
            "exit_code": 1,
        },
        privacy_classification=PrivacyClassification.REDACTED,
    )
    bundle = _bundle(events=(_model_call(), tool_event, command_event))
    value = StorageVerdictReadPort(_StorageStub(bundle)).get_agent_run(
        tenant_id=bundle.run.tenant_id,
        run_id=bundle.run.run_id,
    )
    assert value is not None
    encoded = agent_run_read_to_json(value)
    representation = repr(value)

    for excluded in (
        "PROMPT_CONTENT_CANARY",
        "RESPONSE_CONTENT_CANARY",
        "PROVIDER_CONTENT_CANARY",
        "REQUEST_MODEL_CANARY",
        "RESPONSE_MODEL_CANARY",
        "AGENT_NAME_CANARY",
        "AGENT_VERSION_CANARY",
        "CONFIGURATION_CANARY",
        "SERVICE_NAME_CANARY",
        "ENVIRONMENT_CANARY",
        "INSTANCE_CANARY",
        "TOOL_NAME_CANARY",
        "TOOL_RESULT_CANARY",
        "COMMAND_CONTENT_CANARY",
        "ERROR_CONTENT_CANARY",
    ):
        assert excluded not in encoded
        assert excluded not in representation
    assert value.tenant_id == "tenant-included"
    assert value.run_id == "run-included"
    assert value.model_calls[0].event_id == "model-event"
    assert value.model_calls[0].trace_id == "trace-1"


def test_analysis_v1_golden_finding_semantics() -> None:
    turn = AgentTurn(
        "turn-1",
        "run-included",
        0,
        NOW,
        ExecutionStatus.COMPLETED,
        NOW + timedelta(seconds=10),
        user_request_redacted="PROMPT_CONTENT_CANARY",
        request_state=EvidenceState.PRESENT,
        response_state=EvidenceState.NOT_CAPTURED,
    )
    tool_error = AgentEvent(
        "tool-error",
        "turn-1",
        0,
        NOW,
        AgentEventType.TOOL_RESULT,
        ExecutionStatus.FAILED,
        "test:tool",
        {"tool_name": "tool", "call_id": "call-1", "is_error": True},
    )
    capture_limit = AgentEvent(
        "capture-limit",
        "turn-1",
        1,
        NOW + timedelta(microseconds=1),
        AgentEventType.CONTEXT,
        ExecutionStatus.COMPLETED,
        "verdict:capture_limit",
        {},
    )
    value = StorageVerdictReadPort(
        _StorageStub(
            _bundle(
                status=ExecutionStatus.UNKNOWN,
                turns=(turn,),
                events=(capture_limit, tool_error),
            )
        )
    ).get_agent_run(tenant_id="tenant-included", run_id="run-included")

    assert value is not None
    assert value.analysis_version == "verdict.agent-analysis.v1"
    assert value.finding_count == 4
    assert value.findings_truncated is False
    assert value.findings == (
        FindingRead("tool_error", "error", ("tool-error",)),
        FindingRead("event_capture_partial", "warning", ("capture-limit",)),
        FindingRead("response_not_evaluable", "info", ()),
        FindingRead("run_status_unknown", "info", ()),
    )


def test_canonicalization_precedes_analysis_and_makes_storage_order_irrelevant(
    monkeypatch,
) -> None:
    first = AgentTurn("turn-a", "run-included", 0, NOW, ExecutionStatus.COMPLETED, NOW)
    second = AgentTurn("turn-b", "run-included", 1, NOW, ExecutionStatus.COMPLETED, NOW)
    earlier = _model_call(
        "event-a", trace_id="trace-a", occurred_at=NOW, sequence=0, turn_id="turn-a"
    )
    later = _model_call(
        "event-b",
        trace_id="trace-b",
        occurred_at=NOW + timedelta(seconds=1),
        sequence=0,
        turn_id="turn-b",
    )
    shuffled = _bundle(turns=(second, first), events=(later, earlier))
    ordered = _bundle(turns=(first, second), events=(earlier, later))
    observed_orders: list[tuple[str, ...]] = []
    real_analyze = read_port_module.analysis.analyze_agent_run

    def observing_analyzer(bundle: AgentRunBundle) -> AgentRunAnalysis:
        observed_orders.append(tuple(event.event_id for event in bundle.events))
        return real_analyze(bundle)

    monkeypatch.setattr(read_port_module.analysis, "analyze_agent_run", observing_analyzer)
    shuffled_read = StorageVerdictReadPort(_StorageStub(shuffled)).get_agent_run(
        tenant_id="tenant-included", run_id="run-included"
    )
    ordered_read = StorageVerdictReadPort(_StorageStub(ordered)).get_agent_run(
        tenant_id="tenant-included", run_id="run-included"
    )

    assert observed_orders == [("event-a", "event-b"), ("event-a", "event-b")]
    assert shuffled_read is not None and ordered_read is not None
    assert agent_run_read_to_json(shuffled_read) == agent_run_read_to_json(ordered_read)


def test_model_call_and_finding_totals_are_measured_before_truncation(monkeypatch) -> None:
    events = tuple(
        _model_call(
            f"event-{index:02d}",
            trace_id=f"trace-{index:02d}",
            occurred_at=NOW + timedelta(microseconds=index),
            sequence=index,
        )
        for index in range(17)
    )
    findings = tuple(
        Finding(
            code=f"finding-{index}",
            severity=("error", "warning", "info")[index % 3],
            message="excluded analyzer prose",
            evidence_event_ids=(f"event-{index:02d}",),
        )
        for index in range(5)
    )

    def five_findings(bundle: AgentRunBundle) -> AgentRunAnalysis:
        return AgentRunAnalysis(bundle.run.run_id, {}, {}, findings)

    monkeypatch.setattr(read_port_module.analysis, "analyze_agent_run", five_findings)
    value = StorageVerdictReadPort(_StorageStub(_bundle(events=events))).get_agent_run(
        tenant_id="tenant-included", run_id="run-included"
    )

    assert value is not None
    assert value.model_call_count == 17
    assert len(value.model_calls) == 16
    assert value.model_calls_truncated is True
    assert value.finding_count == 5
    assert len(value.findings) == 4
    assert value.findings_truncated is True
    assert [(item.severity, item.code) for item in value.findings] == [
        ("error", "finding-0"),
        ("error", "finding-3"),
        ("warning", "finding-1"),
        ("warning", "finding-4"),
    ]


def test_malformed_or_judge_backed_analysis_fails_at_the_port_boundary(monkeypatch) -> None:
    def judge_backed(bundle: AgentRunBundle) -> AgentRunAnalysis:
        return AgentRunAnalysis(
            bundle.run.run_id,
            {},
            {},
            (Finding("judge", "warning", "excluded", (), judge_used=True),),
        )

    monkeypatch.setattr(read_port_module.analysis, "analyze_agent_run", judge_backed)

    with pytest.raises(VerdictReadError) as error:
        StorageVerdictReadPort(_StorageStub(_bundle())).get_agent_run(
            tenant_id="tenant-included", run_id="run-included"
        )
    assert error.value.code == "invalid_read_model"


@pytest.mark.parametrize(("field_name", "count"), [("turns", 1_001), ("events", 1_501)])
def test_source_collection_limits_precede_canonicalization_and_analysis(
    monkeypatch, field_name: str, count: int
) -> None:
    if field_name == "turns":
        turns = tuple(
            AgentTurn(
                f"turn-{index}",
                "run-included",
                index,
                NOW,
                ExecutionStatus.COMPLETED,
                NOW,
            )
            for index in range(count)
        )
        bundle = _bundle(turns=turns, events=())
    else:
        events = tuple(
            AgentEvent(
                f"event-{index}",
                "turn-1",
                index,
                NOW,
                AgentEventType.CONTEXT,
                ExecutionStatus.COMPLETED,
                "test:context",
                {},
            )
            for index in range(count)
        )
        bundle = _bundle(events=events)
    analyzed = False

    def forbidden_analysis(bundle: AgentRunBundle) -> AgentRunAnalysis:
        nonlocal analyzed
        analyzed = True
        raise AssertionError("analysis must not run")

    monkeypatch.setattr(read_port_module.analysis, "analyze_agent_run", forbidden_analysis)
    with pytest.raises(VerdictReadError) as error:
        StorageVerdictReadPort(_StorageStub(bundle)).get_agent_run(
            tenant_id="tenant-included", run_id="run-included"
        )
    assert error.value.code == "invalid_read_model"
    assert analyzed is False


def test_source_event_limit_is_inclusive() -> None:
    events = tuple(
        AgentEvent(
            f"event-{index}",
            "turn-1",
            index,
            NOW,
            AgentEventType.CONTEXT,
            ExecutionStatus.COMPLETED,
            "test:context",
            {},
        )
        for index in range(1_500)
    )

    value = StorageVerdictReadPort(_StorageStub(_bundle(events=events))).get_agent_run(
        tenant_id="tenant-included", run_id="run-included"
    )

    assert value is not None
    assert value.model_call_count == 0


@pytest.fixture(params=["memory", "sqlite", "buffered"])
def read_port_storage(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "memory":
        storage = InMemoryStorage()
    elif request.param == "sqlite":
        storage = SQLiteStorage(str(tmp_path / "read-port.db"))
    else:
        storage = BufferedStorage(InMemoryStorage())
    try:
        yield storage
    finally:
        storage.close()


def test_read_port_has_memory_sqlite_buffered_and_tenant_parity(read_port_storage) -> None:
    bundle = _bundle(events=())
    read_port_storage.replace_agent_run_bundle(bundle)
    port = StorageVerdictReadPort(read_port_storage)

    value = port.get_agent_run(tenant_id=bundle.run.tenant_id, run_id=bundle.run.run_id)

    assert value is not None
    assert json.loads(agent_run_read_to_json(value))["run_id"] == bundle.run.run_id
    assert port.get_agent_run(tenant_id="other-tenant", run_id=bundle.run.run_id) is None


@pytest.mark.parametrize(("turn_count", "accepted"), [(1_000, True), (1_001, False)])
def test_real_local_import_storage_and_read_port_turn_boundary(
    tmp_path: Path, turn_count: int, accepted: bool
) -> None:
    root = tmp_path / f"codex-{turn_count}"
    path = root / "session.jsonl"
    path.parent.mkdir(parents=True)
    records: list[dict[str, object]] = [
        {
            "timestamp": NOW.isoformat(),
            "type": "session_meta",
            "payload": {"id": f"session-{turn_count}", "cli_version": "test"},
        }
    ]
    for index in range(turn_count):
        timestamp = (NOW + timedelta(seconds=index)).isoformat()
        records.extend(
            [
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": f"turn-{index}"},
                },
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": f"turn-{index}",
                        "last_agent_message": "",
                    },
                },
            ]
        )
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    storage = SQLiteStorage(str(tmp_path / f"local-{turn_count}.db"))
    try:
        summary = capture_local_agents(storage, tenant_id="local", codex_root=root)
        [bundle] = storage.list_agent_run_bundles("local")
        assert summary.stored == 1
        assert len(bundle.turns) == turn_count
        if accepted:
            value = StorageVerdictReadPort(storage).get_agent_run(
                tenant_id="local", run_id=bundle.run.run_id
            )
            assert value is not None
        else:
            with pytest.raises(VerdictReadError) as error:
                StorageVerdictReadPort(storage).get_agent_run(
                    tenant_id="local", run_id=bundle.run.run_id
                )
            assert error.value.code == "invalid_read_model"
    finally:
        storage.close()
