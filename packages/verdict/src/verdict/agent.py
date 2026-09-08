"""Framework-neutral Agent Run, turn, and typed evidence contexts."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import math
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from verdict.agent_transport import AgentCaptureSink, StorageCaptureSink
from verdict.evidence import (
    EVENT_CONTENT_FIELDS,
    AgentCaptureBatch,
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
    stable_evidence_id,
)
from verdict.redaction import redact, redact_structure
from verdict.schema import Trace
from verdict.signals import VALID_SIGNAL_KINDS

log = logging.getLogger("verdict.agent")
_LOCAL_TENANT = "__verdict_local__"
_PROVENANCE = "verdict:sdk"
_MAX_CONTENT_BYTES = 4096
_MAX_VALUE_NODES = 128
_MAX_VALUE_DEPTH = 4
_active_run: contextvars.ContextVar[AgentRunContext | None] = contextvars.ContextVar(
    "verdict_active_agent_run", default=None
)
_active_turn: contextvars.ContextVar[_TurnState | None] = contextvars.ContextVar(
    "verdict_active_agent_turn", default=None
)
_identity_lock = threading.Lock()
_identity_pid = -1
_identity_value = ""
_identity_started_at: datetime | None = None


def _process_identity() -> tuple[str, datetime]:
    global _identity_pid, _identity_value, _identity_started_at
    pid = os.getpid()
    with _identity_lock:
        if _identity_pid != pid:
            _identity_pid = pid
            _identity_value = uuid4().hex
            _identity_started_at = datetime.now(timezone.utc)
        assert _identity_started_at is not None
        return _identity_value, _identity_started_at


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _truncate_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", "ignore")


def _content(client: Any, value: object) -> str | None:
    if not client.capture_content:
        return None
    text = value if isinstance(value, str) else str(value)
    sanitized = redact(
        text,
        mode=client.redaction_mode,
        secret=client.redaction_secret,
    )
    return _truncate_utf8(sanitized or "", _MAX_CONTENT_BYTES)


def _bounded_value(client: Any, value: object) -> Any:
    sanitized = redact_structure(
        value,
        mode=client.redaction_mode,
        secret=client.redaction_secret,
    )
    nodes = [0]

    def visit(item: Any, depth: int) -> Any:
        nodes[0] += 1
        if nodes[0] > _MAX_VALUE_NODES or depth > _MAX_VALUE_DEPTH:
            return "<OMITTED:bounded>"
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else "<OMITTED:non_finite>"
        if isinstance(item, str):
            return _truncate_utf8(item, 1024)
        if isinstance(item, list):
            return [visit(child, depth + 1) for child in item[:24]]
        if isinstance(item, dict):
            return {
                _truncate_utf8(str(key), 64): visit(child, depth + 1)
                for key, child in list(item.items())[:24]
            }
        return "<REDACTED>"

    bounded = visit(sanitized, 0)
    if len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 12_000:
        return "<OMITTED:oversize>"
    return bounded


def _fingerprint(client: Any, configuration: Mapping[str, object] | None) -> str:
    if not configuration:
        return ""
    bounded = _bounded_value(client, dict(configuration))
    encoded = json.dumps(
        bounded,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(b"verdict-agent-configuration-v1\0" + encoded).hexdigest()


def _hashed_field(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _terminal_status(error: BaseException | None) -> ExecutionStatus:
    if error is None:
        return ExecutionStatus.COMPLETED
    if isinstance(error, asyncio.CancelledError):
        return ExecutionStatus.CANCELLED
    if isinstance(error, TimeoutError):
        return ExecutionStatus.TIMED_OUT
    return ExecutionStatus.FAILED


def _exception_evidence(error: BaseException) -> dict[str, str]:
    try:
        message = str(error)
    except BaseException:
        message = "<UNAVAILABLE>"
    return {"error_type": type(error).__name__, "message": message}


def _sink(client: Any) -> AgentCaptureSink:
    if client._capture_sink is not None:
        return client._capture_sink
    if client.storage is None:
        raise RuntimeError("Verdict capture transport is unavailable")
    return StorageCaptureSink(client.storage)


@dataclass(frozen=True)
class AgentTraceContext:
    """The initiating turn snapshot attached to a provider Trace."""

    turn: _TurnState
    sampled: bool

    @property
    def tenant_id(self) -> str:
        return self.turn.owner.tenant_id

    @property
    def session_id(self) -> str | None:
        return self.turn.owner.session_id

    @property
    def run_id(self) -> str:
        return self.turn.owner.run_id

    def capture(self, trace: Trace) -> None:
        self.turn.capture_model_trace(trace)


class _TurnState:
    def __init__(self, owner: AgentRunContext, turn: AgentTurn) -> None:
        self.owner = owner
        self.turn = turn
        self._lock = threading.Lock()
        self._next_event_sequence = 0

    def _event(
        self,
        event_type: AgentEventType,
        attributes: dict[str, object],
        *,
        status: ExecutionStatus = ExecutionStatus.COMPLETED,
        occurred_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> AgentEvent:
        with self._lock:
            sequence = self._next_event_sequence
            self._next_event_sequence += 1
            producer_sequence = self.owner._next_producer_sequence()
        has_content = bool(EVENT_CONTENT_FIELDS & attributes.keys())
        safe_attributes = {
            key: _bounded_value(self.owner.client, value)
            for key, value in attributes.items()
            if self.owner.client.capture_content or key not in EVENT_CONTENT_FIELDS
        }
        privacy = (
            PrivacyClassification.REDACTED
            if has_content and self.owner.client.capture_content
            else PrivacyClassification.OMITTED
            if has_content
            else PrivacyClassification.METADATA
        )
        return AgentEvent(
            event_id=f"event_{uuid4().hex}",
            turn_id=self.turn.turn_id,
            sequence=sequence,
            occurred_at=occurred_at or _utcnow(),
            event_type=event_type,
            status=status,
            provenance=_PROVENANCE,
            attributes=safe_attributes,
            privacy_classification=privacy,
            omission_reason=(
                "content_capture_disabled"
                if has_content and not self.owner.client.capture_content
                else None
            ),
            trace_id=trace_id,
            producer_id=self.owner.producer_id,
            producer_sequence=producer_sequence,
        )

    def emit(self, event: AgentEvent, *, trace: Trace | None = None) -> None:
        self.owner._emit(turn=self.turn, event=event, trace=trace)

    def capture_model_trace(self, trace: Trace) -> None:
        attributes = {
            key: value
            for key, value in {
                "provider": trace.provider,
                "request_model": trace.request_model,
                "response_model": trace.response_model,
                "operation": trace.operation.value,
                "finish_reason": trace.finish_reason,
                "input_tokens": trace.input_tokens,
                "output_tokens": trace.output_tokens,
                "latency_ms": trace.latency_ms,
                "error": trace.error,
            }.items()
            if value is not None and value != ""
        }
        event = self._event(
            AgentEventType.MODEL_CALL,
            attributes,
            status=(ExecutionStatus.FAILED if trace.error else ExecutionStatus.COMPLETED),
            occurred_at=trace.started_at,
            trace_id=trace.trace_id,
        )
        self.emit(event, trace=trace)


class ToolContext:
    def __init__(
        self,
        turn: TurnContext,
        name: str,
        arguments: object,
        call_id: str | None,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("tool name must be non-empty text")
        self._turn = turn
        self._name = _truncate_utf8(name, 256)
        self._arguments = arguments
        self._call_id = call_id or f"call_{uuid4().hex}"
        self._output: object = None
        self._entered = False
        self._closed = False

    def set_output(self, output: object) -> None:
        if not self._entered or self._closed:
            raise RuntimeError("tool output requires an active tool context")
        self._output = output

    def __enter__(self) -> ToolContext:
        self._turn._require_active()
        if self._entered:
            raise RuntimeError("tool context cannot be entered twice")
        self._entered = True
        event = self._turn._state._event(
            AgentEventType.TOOL_CALL,
            {
                "tool_name": self._name,
                "arguments": self._arguments,
                "call_id": self._call_id,
            },
        )
        self._turn._state.emit(event)
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> Literal[False]:
        if not self._entered or self._closed:
            raise RuntimeError("tool context is not active")
        self._turn._require_active()
        self._closed = True
        output = self._output
        if exc is not None:
            output = _exception_evidence(exc)
        event = self._turn._state._event(
            AgentEventType.TOOL_RESULT,
            {
                "tool_name": self._name,
                "result": output,
                "call_id": self._call_id,
                "is_error": exc is not None,
            },
            status=_terminal_status(exc),
        )
        self._turn._state.emit(event)
        return False

    async def __aenter__(self) -> ToolContext:
        return self.__enter__()

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        return self.__exit__(exc_type, exc, tb)


class TurnContext:
    def __init__(self, owner: AgentRunContext, user_input: object) -> None:
        if not isinstance(user_input, str):
            raise ValueError("agent turn user_input must be text")
        self._owner = owner
        self._user_input = user_input
        self._state: _TurnState
        self._token: contextvars.Token[_TurnState | None] | None = None
        self._entered = False
        self._output: object | None = None
        self._has_output = False

    def __enter__(self) -> TurnContext:
        if self._entered:
            raise RuntimeError("agent turn context cannot be entered twice")
        if _active_turn.get() is not None:
            raise RuntimeError("agent turns cannot be nested")
        self._entered = True
        started_at = _utcnow()
        sequence = self._owner._next_turn_sequence()
        request = _content(self._owner.client, self._user_input)
        turn = AgentTurn(
            turn_id=f"turn_{uuid4().hex}",
            run_id=self._owner.run_id,
            sequence=sequence,
            started_at=started_at,
            status=ExecutionStatus.UNKNOWN,
            user_request_redacted=request,
            request_state=(
                EvidenceState.PRESENT if request is not None else EvidenceState.NOT_CAPTURED
            ),
        )
        self._state = _TurnState(self._owner, turn)
        self._owner._latest_turn = self._state
        self._owner._emit(turn=turn)
        self._token = _active_turn.set(self._state)
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> Literal[False]:
        if self._token is None:
            raise RuntimeError("agent turn context was not entered")
        _active_turn.reset(self._token)
        self._token = None
        response = (
            _content(self._owner.client, self._output) if exc is None and self._has_output else None
        )
        self._state.turn = replace(
            self._state.turn,
            status=_terminal_status(exc),
            ended_at=_utcnow(),
            final_response_redacted=response,
            response_state=(
                EvidenceState.PRESENT if response is not None else EvidenceState.NOT_CAPTURED
            ),
        )
        self._owner._emit(turn=self._state.turn)
        return False

    async def __aenter__(self) -> TurnContext:
        return self.__enter__()

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        return self.__exit__(exc_type, exc, tb)

    def set_output(self, output: object) -> None:
        self._require_active()
        if not isinstance(output, str):
            raise ValueError("agent turn output must be text")
        self._output = output
        self._has_output = True

    def tool(
        self, name: str, *, arguments: object = None, call_id: str | None = None
    ) -> ToolContext:
        self._require_active()
        return ToolContext(self, name, arguments, call_id)

    def _require_active(self) -> None:
        if self._token is None or _active_turn.get() is not self._state:
            raise RuntimeError("operation requires an active agent turn")

    def _record(
        self,
        event_type: AgentEventType,
        attributes: dict[str, object],
        *,
        status: ExecutionStatus = ExecutionStatus.COMPLETED,
    ) -> None:
        self._require_active()
        self._state.emit(self._state._event(event_type, attributes, status=status))

    def record_instruction(
        self,
        *,
        name: str,
        text: str,
        source: str = "application",
        available: bool = True,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("instruction name must be non-empty text")
        self._record(
            AgentEventType.INSTRUCTION,
            {"name": name, "text": text, "source": source, "available": available},
        )

    def record_context(
        self,
        *,
        name: str,
        value: object,
        source: str = "application",
        available: bool = True,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("context name must be non-empty text")
        self._record(
            AgentEventType.CONTEXT,
            {"name": name, "value": value, "source": source, "available": available},
        )

    def record_command(
        self,
        *,
        command: str,
        exit_code: int,
        cwd: str | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        attributes: dict[str, object] = {"command": command, "exit_code": exit_code}
        if cwd is not None:
            attributes["cwd_hash"] = _hashed_field(cwd)
        if stdout is not None:
            attributes["stdout"] = stdout
        if stderr is not None:
            attributes["stderr"] = stderr
        self._record(
            AgentEventType.COMMAND,
            attributes,
            status=(ExecutionStatus.COMPLETED if exit_code == 0 else ExecutionStatus.FAILED),
        )

    def record_test(
        self,
        *,
        command: str,
        exit_code: int,
        passed: int = 0,
        failed: int = 0,
        skipped: int = 0,
        output: str | None = None,
    ) -> None:
        attributes: dict[str, object] = {
            "command": command,
            "exit_code": exit_code,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        }
        if output is not None:
            attributes["output"] = output
        self._record(
            AgentEventType.TEST_RESULT,
            attributes,
            status=(ExecutionStatus.COMPLETED if exit_code == 0 else ExecutionStatus.FAILED),
        )

    def record_artifact(
        self,
        *,
        path: str,
        action: str,
        authoritative: bool = False,
        state: str | None = None,
    ) -> None:
        attributes: dict[str, object] = {
            "path_hash": _hashed_field(path),
            "action": action,
            "authoritative": authoritative,
        }
        if state is not None:
            attributes["state"] = state
        self._record(AgentEventType.ARTIFACT, attributes)

    def record_retry(self, *, reason: str, attempt: int, operation: str = "") -> None:
        attributes: dict[str, object] = {"reason": reason, "attempt": attempt}
        if operation:
            attributes["operation"] = operation
        self._record(AgentEventType.RETRY, attributes)

    def record_handoff(
        self,
        *,
        agent_name: str,
        child_run_id: str,
        state: str = "started",
    ) -> None:
        self._record(
            AgentEventType.SUBAGENT,
            {
                "agent_name": agent_name,
                "action": "handoff",
                "child_run_id": child_run_id,
                "state": state,
            },
        )

    def record_feedback(self, *, kind: str, value: object = True) -> None:
        if kind not in VALID_SIGNAL_KINDS:
            raise ValueError(f"unknown feedback kind {kind!r}")
        self._record(AgentEventType.FEEDBACK, {"kind": kind, "value": value})

    def record_outcome(self, name: str, value: object, *, source: str = "application") -> None:
        self._record(
            AgentEventType.OUTCOME,
            {"name": name, "value": value, "source": source},
        )


class AgentRunContext:
    def __init__(
        self,
        client: Any,
        *,
        name: str,
        version: str,
        session_id: str | None,
        external_id: str | None,
        parent_run_id: str | None,
        configuration: Mapping[str, object] | None,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("agent name must be non-empty text")
        self.client = client
        self.name = name
        self.version = version
        self.session_id = session_id
        self.external_id = external_id
        self.parent_run_id = parent_run_id
        self.configuration = configuration
        self.producer_id, self._source_started_at = _process_identity()
        self.run_id = ""
        self._run: AgentRun | None = None
        self._source: SourceSession | None = None
        self._token: contextvars.Token[AgentRunContext | None] | None = None
        self._turn_token: contextvars.Token[_TurnState | None] | None = None
        self._lock = threading.Lock()
        self._turn_sequence = 0
        self._producer_sequence = 0
        self._latest_turn: _TurnState | None = None
        self._capture_failed = False
        self._entered = False
        self._open = False
        self.sampled = False

    @property
    def tenant_id(self) -> str:
        return (
            self._run.tenant_id if self._run is not None else self.client.tenant_id or _LOCAL_TENANT
        )

    def _next_turn_sequence(self) -> int:
        with self._lock:
            value = self._turn_sequence
            self._turn_sequence += 1
            return value

    def _next_producer_sequence(self) -> int:
        with self._lock:
            value = self._producer_sequence
            self._producer_sequence += 1
            return value

    def __enter__(self) -> AgentRunContext:
        if self._entered:
            raise RuntimeError("agent run context cannot be entered twice")
        self._entered = True
        self._open = True
        parent = _active_run.get()
        tenant_id = self.client.tenant_id or _LOCAL_TENANT
        source_id = stable_evidence_id("source", "verdict_sdk", "process", self.producer_id)
        raw_run_id = self.external_id or uuid4().hex
        self.run_id = stable_evidence_id("run", "verdict_sdk", self.producer_id, raw_run_id)
        inherited_parent = parent.run_id if parent is not None else None
        parent_id = self.parent_run_id or inherited_parent
        if parent_id == self.run_id:
            raise ValueError("agent run cannot be its own parent")
        digest = int(hashlib.sha256(self.run_id.encode("utf-8")).hexdigest()[:16], 16)
        self.sampled = (
            parent.sampled
            if parent is not None
            else (digest / float(2**64) < self.client.sample_rate)
        )
        now = _utcnow()
        self._source = SourceSession(
            source_session_id=source_id,
            tenant_id=tenant_id,
            source_kind="verdict_sdk",
            source_locator_hash=_hashed_field(self.producer_id),
            started_at=self._source_started_at,
            observed_at=now,
        )
        self._run = AgentRun(
            run_id=self.run_id,
            source_session_id=source_id,
            tenant_id=tenant_id,
            started_at=now,
            status=ExecutionStatus.UNKNOWN,
            agent_name=self.name,
            agent_version=self.version,
            configuration_fingerprint=_fingerprint(self.client, self.configuration),
            session_id=self.session_id,
            parent_run_id=parent_id,
            service_name=self.client.service_name,
            environment=self.client.environment,
            instance_id=self.producer_id,
        )
        self._emit()
        self._turn_token = _active_turn.set(None)
        self._token = _active_run.set(self)
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> Literal[False]:
        if self._token is None or self._run is None:
            raise RuntimeError("agent run context was not entered")
        if _active_turn.get() is not None:
            raise RuntimeError("agent run cannot close with an active turn")
        _active_run.reset(self._token)
        self._token = None
        assert self._turn_token is not None
        _active_turn.reset(self._turn_token)
        self._turn_token = None
        self._run = replace(
            self._run,
            status=_terminal_status(exc),
            ended_at=_utcnow(),
        )
        self._emit()
        self._open = False
        return False

    async def __aenter__(self) -> AgentRunContext:
        return self.__enter__()

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        return self.__exit__(exc_type, exc, tb)

    def turn(self, *, user_input: object) -> TurnContext:
        self._require_active()
        return TurnContext(self, user_input)

    def _require_active(self) -> None:
        if not self._open or _active_run.get() is not self:
            raise RuntimeError("operation requires an active agent run")

    def record_business_outcome(
        self, name: str, value: object, *, source: str = "application"
    ) -> None:
        self._require_active()
        if self._latest_turn is None:
            raise RuntimeError("a business outcome requires at least one agent turn")
        event = self._latest_turn._event(
            AgentEventType.OUTCOME,
            {"name": name, "value": value, "source": source},
        )
        self._latest_turn.emit(event)

    def _emit(
        self,
        *,
        turn: AgentTurn | None = None,
        event: AgentEvent | None = None,
        trace: Trace | None = None,
    ) -> None:
        if not self.sampled or self._source is None or self._run is None:
            return
        if self._capture_failed:
            self.client.runtime_metrics.record_dropped()
            return
        try:
            source = replace(self._source, observed_at=_utcnow())
            batch = AgentCaptureBatch(
                source,
                self._run,
                (turn,) if turn is not None else (),
                (event,) if event is not None else (),
            )
            sink = _sink(self.client)
            sink.capture_agent(batch, (trace,) if trace is not None else ())
        except Exception as error:
            self._capture_failed = True
            self.client.runtime_metrics.record_dropped()
            target = self.client._capture_sink or self.client.storage
            self.client.runtime_metrics.warn_capture_failure_once(
                log,
                evidence="agent evidence",
                target=target,
                error=error,
            )


def agent_run(
    *,
    name: str,
    version: str = "",
    session_id: str | None = None,
    external_id: str | None = None,
    parent_run_id: str | None = None,
    configuration: Mapping[str, object] | None = None,
) -> AgentRunContext:
    """Create a sampled Agent Run context using the initialized Verdict client."""
    from verdict.client import get_client

    client = get_client()
    if client is None:
        raise RuntimeError("verdict.init() must be called before verdict.agent_run()")
    return AgentRunContext(
        client,
        name=name,
        version=version,
        session_id=session_id,
        external_id=external_id,
        parent_run_id=parent_run_id,
        configuration=configuration,
    )


def current_agent_trace_context() -> AgentTraceContext | None:
    """Snapshot the active initiating turn for provider instrumentation."""
    turn = _active_turn.get()
    if turn is None:
        return None
    return AgentTraceContext(turn=turn, sampled=turn.owner.sampled)


def trace_agent_context(trace: Trace) -> AgentTraceContext | None:
    value = getattr(trace, "_verdict_agent_context", None)
    return value if isinstance(value, AgentTraceContext) else None


def clear_agent_context() -> None:
    _active_turn.set(None)
    _active_run.set(None)
