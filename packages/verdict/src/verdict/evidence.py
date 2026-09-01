"""Typed, bounded agent-execution evidence owned by Verdict core.

Source adapters normalize into these records.  They never persist their raw
provider envelope, and a genuine model call remains a separate ``Trace`` linked
from a model-call event.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_IDENTIFIER_BYTES = 256
_MAX_PROVENANCE_BYTES = 512
_MAX_ATTRIBUTE_KEYS = 24
_MAX_ATTRIBUTE_DEPTH = 4
_MAX_ATTRIBUTE_NODES = 128
_MAX_ATTRIBUTE_STRING_BYTES = 4096
_MAX_ATTRIBUTES_JSON_BYTES = 16_384
_MAX_BUNDLE_JSON_BYTES = 4_194_304


class EvidenceBundleTooLarge(ValueError):
    """The validated bundle cannot fit the canonical atomic storage row."""


class ExecutionStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class EvidenceState(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    NOT_CAPTURED = "not_captured"
    NOT_APPLICABLE = "not_applicable"


class PrivacyClassification(str, Enum):
    METADATA = "metadata"
    REDACTED = "redacted"
    OMITTED = "omitted"


class AgentEventType(str, Enum):
    INSTRUCTION = "instruction"
    CONTEXT = "context"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    COMMAND = "command"
    TEST_RESULT = "test_result"
    ARTIFACT = "artifact"
    SUBAGENT = "subagent"


_EVENT_FIELDS: dict[AgentEventType, frozenset[str]] = {
    AgentEventType.INSTRUCTION: frozenset({"name", "text", "source", "available"}),
    AgentEventType.CONTEXT: frozenset({"name", "text", "source", "available"}),
    AgentEventType.MODEL_CALL: frozenset(
        {
            "provider",
            "request_model",
            "response_model",
            "operation",
            "finish_reason",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "error",
        }
    ),
    AgentEventType.TOOL_CALL: frozenset({"tool_name", "arguments", "call_id"}),
    AgentEventType.TOOL_RESULT: frozenset({"tool_name", "result", "call_id", "is_error"}),
    AgentEventType.COMMAND: frozenset({"command", "cwd_hash", "exit_code", "stdout", "stderr"}),
    AgentEventType.TEST_RESULT: frozenset(
        {"command", "exit_code", "passed", "failed", "skipped", "output"}
    ),
    AgentEventType.ARTIFACT: frozenset({"path_hash", "action", "authoritative", "state"}),
    AgentEventType.SUBAGENT: frozenset({"agent_name", "action", "child_run_id", "state"}),
}
_CONTENT_FIELDS = frozenset(
    {"text", "arguments", "result", "command", "stdout", "stderr", "output"}
)
_TEXT_FIELDS = frozenset({
    "name", "text", "source", "provider", "request_model", "response_model",
    "operation", "finish_reason", "error", "tool_name", "call_id", "command",
    "cwd_hash", "stdout", "stderr", "output", "path_hash", "action", "state",
    "agent_name", "child_run_id",
})
_BOOLEAN_FIELDS = frozenset({"available", "is_error", "authoritative"})
_COUNT_FIELDS = frozenset({"input_tokens", "output_tokens", "passed", "failed", "skipped"})


def _validate_text(value: str, *, field_name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc
    if size > maximum or "\x00" in value:
        raise ValueError(f"{field_name} is not bounded valid text")


def _validate_datetime(value: datetime | None, *, field_name: str) -> None:
    if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
        raise ValueError(f"{field_name} must be timezone-aware")


def stable_evidence_id(kind: str, source_kind: str, source_scope: str, source_id: str) -> str:
    """Return a deterministic opaque identity without exposing source paths/IDs."""
    for name, value in (
        ("kind", kind),
        ("source_kind", source_kind),
        ("source_scope", source_scope),
        ("source_id", source_id),
    ):
        _validate_text(value, field_name=name, maximum=4096)
    prefix = re.sub(r"[^a-z0-9]+", "_", kind.lower()).strip("_")
    if not prefix:
        raise ValueError("kind must contain an ASCII letter or number")
    digest = hashlib.sha256(
        b"verdict-evidence-v1\0"
        + source_kind.encode("utf-8")
        + b"\0"
        + source_scope.encode("utf-8")
        + b"\0"
        + source_id.encode("utf-8")
    ).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _bounded_json_value(value: Any, *, depth: int, seen: set[int], nodes: list[int]) -> None:
    nodes[0] += 1
    if nodes[0] > _MAX_ATTRIBUTE_NODES or depth > _MAX_ATTRIBUTE_DEPTH:
        raise ValueError("event attributes must be bounded")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("event attributes must contain finite numbers")
        return
    if isinstance(value, str):
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("event attributes must contain valid UTF-8") from exc
        if size > _MAX_ATTRIBUTE_STRING_BYTES or "\x00" in value:
            raise ValueError("event attributes must be bounded valid text")
        return
    if isinstance(value, (list, dict)):
        identity = id(value)
        if identity in seen:
            raise ValueError("event attributes cannot contain cycles")
        seen.add(identity)
        try:
            if isinstance(value, list):
                if len(value) > _MAX_ATTRIBUTE_KEYS:
                    raise ValueError("event attributes must be bounded")
                for item in value:
                    _bounded_json_value(item, depth=depth + 1, seen=seen, nodes=nodes)
            else:
                if len(value) > _MAX_ATTRIBUTE_KEYS:
                    raise ValueError("event attributes must be bounded")
                for key, item in value.items():
                    _validate_text(key, field_name="attribute key", maximum=64)
                    _bounded_json_value(item, depth=depth + 1, seen=seen, nodes=nodes)
        finally:
            seen.remove(identity)
        return
    raise ValueError("event attributes must contain JSON-compatible typed values")


def _validate_attributes(event_type: AgentEventType, attributes: dict[str, Any]) -> None:
    if not isinstance(attributes, dict):
        raise ValueError("event attributes must be an object")
    unexpected = set(attributes) - _EVENT_FIELDS[event_type]
    if unexpected:
        raise ValueError(
            f"event attribute {sorted(unexpected)[0]!r} is not allowed for {event_type.value}"
        )
    for name, value in attributes.items():
        if value is None or name in {"arguments", "result"}:
            continue
        if name in _TEXT_FIELDS and not isinstance(value, str):
            raise ValueError(f"event attribute {name!r} must be text")
        if name in _BOOLEAN_FIELDS and not isinstance(value, bool):
            raise ValueError(f"event attribute {name!r} must be boolean")
        if name in _COUNT_FIELDS and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1
        ):
            raise ValueError(f"event attribute {name!r} must be a non-negative integer")
        if name == "exit_code" and (
            isinstance(value, bool) or not isinstance(value, int) or abs(value) > 2**31 - 1
        ):
            raise ValueError("event attribute 'exit_code' must be a bounded integer")
        if name == "latency_ms" and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValueError("event attribute 'latency_ms' must be a non-negative number")
    _bounded_json_value(attributes, depth=0, seen=set(), nodes=[0])
    encoded = json.dumps(
        attributes,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_ATTRIBUTES_JSON_BYTES:
        raise ValueError("event attributes must be bounded")


@dataclass(frozen=True)
class SourceSession:
    source_session_id: str
    tenant_id: str
    source_kind: str
    source_locator_hash: str
    started_at: datetime
    observed_at: datetime
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("source_session_id", "tenant_id", "source_kind"):
            _validate_text(getattr(self, name), field_name=name, maximum=_MAX_IDENTIFIER_BYTES)
        if not _HEX_DIGEST.fullmatch(self.source_locator_hash):
            raise ValueError("source_locator_hash must be a lowercase SHA-256 digest")
        for name in ("started_at", "observed_at", "ended_at"):
            _validate_datetime(getattr(self, name), field_name=name)


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    source_session_id: str
    tenant_id: str
    started_at: datetime
    status: ExecutionStatus
    ended_at: datetime | None = None
    agent_name: str = ""
    agent_version: str = ""
    configuration_fingerprint: str = ""

    def __post_init__(self) -> None:
        for name in ("run_id", "source_session_id", "tenant_id"):
            _validate_text(getattr(self, name), field_name=name, maximum=_MAX_IDENTIFIER_BYTES)
        for name in ("started_at", "ended_at"):
            _validate_datetime(getattr(self, name), field_name=name)
        if not isinstance(self.status, ExecutionStatus):
            object.__setattr__(self, "status", ExecutionStatus(self.status))
        if self.status is not ExecutionStatus.UNKNOWN and self.ended_at is None:
            raise ValueError("terminal run status requires ended_at")
        for name in ("agent_name", "agent_version", "configuration_fingerprint"):
            value = getattr(self, name)
            if value:
                _validate_text(value, field_name=name, maximum=_MAX_IDENTIFIER_BYTES)


@dataclass(frozen=True)
class AgentTurn:
    turn_id: str
    run_id: str
    sequence: int
    started_at: datetime
    status: ExecutionStatus
    ended_at: datetime | None = None
    user_request_redacted: str | None = None
    final_response_redacted: str | None = None
    request_state: EvidenceState = EvidenceState.NOT_CAPTURED
    response_state: EvidenceState = EvidenceState.NOT_CAPTURED

    def __post_init__(self) -> None:
        for name in ("turn_id", "run_id"):
            _validate_text(getattr(self, name), field_name=name, maximum=_MAX_IDENTIFIER_BYTES)
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("turn sequence must be a non-negative integer")
        for name in ("started_at", "ended_at"):
            _validate_datetime(getattr(self, name), field_name=name)
        if not isinstance(self.status, ExecutionStatus):
            object.__setattr__(self, "status", ExecutionStatus(self.status))
        for state_name in ("request_state", "response_state"):
            state = getattr(self, state_name)
            if not isinstance(state, EvidenceState):
                object.__setattr__(self, state_name, EvidenceState(state))
        for state_name, content_name in (
            ("request_state", "user_request_redacted"),
            ("response_state", "final_response_redacted"),
        ):
            state = getattr(self, state_name)
            content = getattr(self, content_name)
            if state is EvidenceState.PRESENT and content is None:
                raise ValueError(f"{content_name} is required when evidence is present")
            if state is not EvidenceState.PRESENT and content is not None:
                raise ValueError(f"{content_name} must be absent when evidence is not present")
            if content is not None:
                try:
                    if len(content.encode("utf-8")) > _MAX_ATTRIBUTE_STRING_BYTES:
                        raise ValueError(f"{content_name} must be bounded")
                except UnicodeError as exc:
                    raise ValueError(f"{content_name} must be valid UTF-8") from exc


@dataclass(frozen=True)
class AgentEvent:
    event_id: str
    turn_id: str
    sequence: int
    occurred_at: datetime
    event_type: AgentEventType
    status: ExecutionStatus
    provenance: str
    attributes: dict[str, Any] = field(default_factory=dict)
    privacy_classification: PrivacyClassification = PrivacyClassification.METADATA
    omission_reason: str | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("event_id", "turn_id"):
            _validate_text(getattr(self, name), field_name=name, maximum=_MAX_IDENTIFIER_BYTES)
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("event sequence must be a non-negative integer")
        _validate_datetime(self.occurred_at, field_name="occurred_at")
        if not isinstance(self.event_type, AgentEventType):
            object.__setattr__(self, "event_type", AgentEventType(self.event_type))
        if not isinstance(self.status, ExecutionStatus):
            object.__setattr__(self, "status", ExecutionStatus(self.status))
        if not isinstance(self.privacy_classification, PrivacyClassification):
            object.__setattr__(
                self,
                "privacy_classification",
                PrivacyClassification(self.privacy_classification),
            )
        _validate_text(
            self.provenance,
            field_name="provenance",
            maximum=_MAX_PROVENANCE_BYTES,
        )
        _validate_attributes(self.event_type, self.attributes)
        if (
            self.privacy_classification is PrivacyClassification.METADATA
            and _CONTENT_FIELDS.intersection(self.attributes)
        ):
            raise ValueError("content-bearing event attributes cannot be classified as metadata")
        if self.trace_id is not None:
            _validate_text(self.trace_id, field_name="trace_id", maximum=_MAX_IDENTIFIER_BYTES)
            if self.event_type is not AgentEventType.MODEL_CALL:
                raise ValueError("trace_id is valid only for a model_call event")
        if self.privacy_classification is PrivacyClassification.OMITTED:
            if not self.omission_reason:
                raise ValueError("omitted event requires omission_reason")
            if _CONTENT_FIELDS.intersection(self.attributes):
                raise ValueError("omitted event cannot retain content attributes")
        elif self.omission_reason is not None:
            raise ValueError("omission_reason is valid only for omitted evidence")
        if self.omission_reason is not None:
            _validate_text(
                self.omission_reason,
                field_name="omission_reason",
                maximum=256,
            )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


@dataclass(frozen=True)
class AgentRunBundle:
    """One complete normalized projection, replaced atomically on rescan."""

    session: SourceSession
    run: AgentRun
    turns: tuple[AgentTurn, ...] = ()
    events: tuple[AgentEvent, ...] = ()

    def __post_init__(self) -> None:
        if self.run.source_session_id != self.session.source_session_id:
            raise ValueError("run references an unknown source session")
        if self.run.tenant_id != self.session.tenant_id:
            raise ValueError("run and source session tenant_id must match")
        turn_ids: set[str] = set()
        turn_sequences: set[int] = set()
        for turn in self.turns:
            if turn.run_id != self.run.run_id:
                raise ValueError("turn references an unknown run")
            if turn.turn_id in turn_ids:
                raise ValueError("bundle contains duplicate turn_id")
            turn_ids.add(turn.turn_id)
            turn_sequences.add(turn.sequence)
        if turn_sequences != set(range(len(self.turns))):
            raise ValueError("turn sequences must be contiguous from zero")

        event_ids: set[str] = set()
        event_sequences: dict[str, set[int]] = {turn_id: set() for turn_id in turn_ids}
        for event in self.events:
            if event.turn_id not in turn_ids:
                raise ValueError("event references an unknown turn")
            if event.event_id in event_ids:
                raise ValueError("bundle contains duplicate event_id")
            event_ids.add(event.event_id)
            event_sequences[event.turn_id].add(event.sequence)
        for sequences in event_sequences.values():
            if sequences != set(range(len(sequences))):
                raise ValueError("event sequences must be contiguous from zero per turn")

    @property
    def content_hash(self) -> str:
        encoded = agent_run_bundle_to_json(self).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def agent_run_bundle_to_json(bundle: AgentRunBundle) -> str:
    """Serialize a validated bundle to one canonical storage representation."""
    payload = {
        "session": _canonical_value(asdict(bundle.session)),
        "run": _canonical_value(asdict(bundle.run)),
        "turns": [
            _canonical_value(asdict(turn))
            for turn in sorted(bundle.turns, key=lambda item: (item.sequence, item.turn_id))
        ],
        "events": [
            _canonical_value(asdict(event))
            for event in sorted(
                bundle.events,
                key=lambda item: (item.turn_id, item.sequence, item.event_id),
            )
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > _MAX_BUNDLE_JSON_BYTES:
        raise EvidenceBundleTooLarge("agent run bundle exceeds the atomic evidence limit")
    return encoded


def _parse_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp") from exc
    _validate_datetime(parsed, field_name=field_name)
    return parsed


def _parse_optional_datetime(value: Any, *, field_name: str) -> datetime | None:
    return None if value is None else _parse_datetime(value, field_name=field_name)


def agent_run_bundle_from_json(payload_json: str) -> AgentRunBundle:
    """Load and revalidate a canonical bundle from durable storage."""
    if not isinstance(payload_json, str) or len(payload_json.encode("utf-8")) > _MAX_BUNDLE_JSON_BYTES:
        raise ValueError("agent run bundle JSON must be bounded text")
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("agent run bundle JSON is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {"session", "run", "turns", "events"}:
        raise ValueError("agent run bundle JSON has an invalid shape")
    session_data = payload["session"]
    run_data = payload["run"]
    turn_data = payload["turns"]
    event_data = payload["events"]
    if not isinstance(session_data, dict) or not isinstance(run_data, dict):
        raise ValueError("agent run bundle JSON has an invalid shape")
    if not isinstance(turn_data, list) or not isinstance(event_data, list):
        raise ValueError("agent run bundle JSON has an invalid shape")
    if set(session_data) != {
        "source_session_id",
        "tenant_id",
        "source_kind",
        "source_locator_hash",
        "started_at",
        "observed_at",
        "ended_at",
    } or set(run_data) != {
        "run_id",
        "source_session_id",
        "tenant_id",
        "started_at",
        "status",
        "ended_at",
        "agent_name",
        "agent_version",
        "configuration_fingerprint",
    }:
        raise ValueError("agent run bundle JSON has invalid typed fields")
    turn_fields = {
        "turn_id",
        "run_id",
        "sequence",
        "started_at",
        "status",
        "ended_at",
        "user_request_redacted",
        "final_response_redacted",
        "request_state",
        "response_state",
    }
    event_fields = {
        "event_id",
        "turn_id",
        "sequence",
        "occurred_at",
        "event_type",
        "status",
        "provenance",
        "attributes",
        "privacy_classification",
        "omission_reason",
        "trace_id",
    }
    if any(not isinstance(item, dict) or set(item) != turn_fields for item in turn_data):
        raise ValueError("agent run bundle JSON has invalid typed fields")
    if any(not isinstance(item, dict) or set(item) != event_fields for item in event_data):
        raise ValueError("agent run bundle JSON has invalid typed fields")
    try:
        session = SourceSession(
            source_session_id=session_data["source_session_id"],
            tenant_id=session_data["tenant_id"],
            source_kind=session_data["source_kind"],
            source_locator_hash=session_data["source_locator_hash"],
            started_at=_parse_datetime(session_data["started_at"], field_name="started_at"),
            observed_at=_parse_datetime(session_data["observed_at"], field_name="observed_at"),
            ended_at=_parse_optional_datetime(session_data.get("ended_at"), field_name="ended_at"),
        )
        run = AgentRun(
            run_id=run_data["run_id"],
            source_session_id=run_data["source_session_id"],
            tenant_id=run_data["tenant_id"],
            started_at=_parse_datetime(run_data["started_at"], field_name="started_at"),
            status=ExecutionStatus(run_data["status"]),
            ended_at=_parse_optional_datetime(run_data.get("ended_at"), field_name="ended_at"),
            agent_name=run_data.get("agent_name", ""),
            agent_version=run_data.get("agent_version", ""),
            configuration_fingerprint=run_data.get("configuration_fingerprint", ""),
        )
        turns = tuple(
            AgentTurn(
                turn_id=item["turn_id"],
                run_id=item["run_id"],
                sequence=item["sequence"],
                started_at=_parse_datetime(item["started_at"], field_name="started_at"),
                status=ExecutionStatus(item["status"]),
                ended_at=_parse_optional_datetime(item.get("ended_at"), field_name="ended_at"),
                user_request_redacted=item.get("user_request_redacted"),
                final_response_redacted=item.get("final_response_redacted"),
                request_state=EvidenceState(item["request_state"]),
                response_state=EvidenceState(item["response_state"]),
            )
            for item in turn_data
            if isinstance(item, dict)
        )
        events = tuple(
            AgentEvent(
                event_id=item["event_id"],
                turn_id=item["turn_id"],
                sequence=item["sequence"],
                occurred_at=_parse_datetime(item["occurred_at"], field_name="occurred_at"),
                event_type=AgentEventType(item["event_type"]),
                status=ExecutionStatus(item["status"]),
                provenance=item["provenance"],
                attributes=item.get("attributes", {}),
                privacy_classification=PrivacyClassification(item["privacy_classification"]),
                omission_reason=item.get("omission_reason"),
                trace_id=item.get("trace_id"),
            )
            for item in event_data
            if isinstance(item, dict)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("agent run bundle JSON has invalid typed fields") from exc
    if len(turns) != len(turn_data) or len(events) != len(event_data):
        raise ValueError("agent run bundle JSON has an invalid shape")
    return AgentRunBundle(session=session, run=run, turns=turns, events=events)
