"""Small, versioned read contract for trusted dependent packages.

The public DTOs in this module deliberately expose no storage rows, prompts,
responses, provider/model display names, or arbitrary event attributes.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol

import verdict.analysis as analysis
from verdict.evidence import AgentEvent, AgentEventType, AgentRunBundle
from verdict.storage.base import Storage

AGENT_RUN_READ_SCHEMA_VERSION = "verdict.agent-run-read.v1"
AGENT_ANALYSIS_VERSION = "verdict.agent-analysis.v1"
SUPPORTED_AGENT_ANALYSIS_VERSIONS = frozenset({AGENT_ANALYSIS_VERSION})

_ERROR_CODES = frozenset(
    {
        "invalid_query",
        "read_unavailable",
        "invalid_read_model",
        "response_limit_exceeded",
    }
)
_STATUSES = frozenset({"completed", "failed", "timed_out", "cancelled", "unknown"})
_SEVERITIES = frozenset({"info", "warning", "error"})
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
_VERSION_PATTERN = re.compile(r"[a-z0-9.-]{1,64}")
_MAX_IDENTIFIER_BYTES = 256
_MAX_FINDING_CODE_BYTES = 64
_MAX_SOURCE_TURNS = 1_000
_MAX_SOURCE_EVENTS = 1_500
_MAX_MODEL_CALLS = 16
_MAX_FINDINGS = 4
_MAX_WITNESSES = 20
_MAX_COUNT = 2**63 - 1
_MAX_RESPONSE_BYTES = 262_144


class VerdictReadError(RuntimeError):
    """Stable read-boundary failure with no storage exception details."""

    code: str

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in _ERROR_CODES:
            raise ValueError("unsupported Verdict read error code")
        self.code = code
        super().__init__(code)


def _validate_text(value: object, *, maximum: int, field_name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field_name} must be bounded text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{field_name} must be bounded text") from None
    if len(encoded) > maximum:
        raise ValueError(f"{field_name} must be bounded text")
    return value


def _validate_version(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded version")
    return value


def _utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    try:
        normalized = value.astimezone(timezone.utc)
    except Exception:
        normalized = None
    if normalized is None:
        raise ValueError(f"{field_name} must be a valid timezone-aware datetime")
    return normalized


def _validate_count(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        raise ValueError(f"{field_name} must be a bounded count")
    return value


def _validate_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


@dataclass(frozen=True)
class ModelCallRead:
    event_id: str
    occurred_at: datetime
    status: str
    trace_id: str | None
    latency_ms: float | None

    def __post_init__(self) -> None:
        _validate_model_call(self, normalize=True)


def _validate_model_call(value: ModelCallRead, *, normalize: bool) -> None:
    _validate_text(value.event_id, maximum=_MAX_IDENTIFIER_BYTES, field_name="event_id")
    occurred_at = _utc(value.occurred_at, field_name="occurred_at")
    if normalize:
        object.__setattr__(value, "occurred_at", occurred_at)
    if not isinstance(value.status, str) or value.status not in _STATUSES:
        raise ValueError("status is unsupported")
    if value.trace_id is not None:
        _validate_text(value.trace_id, maximum=_MAX_IDENTIFIER_BYTES, field_name="trace_id")
    latency = value.latency_ms
    if latency is not None:
        if isinstance(latency, bool) or not isinstance(latency, (int, float)):
            raise ValueError("latency_ms must be a finite non-negative number")
        try:
            normalized_latency = float(latency)
        except (OverflowError, ValueError):
            raise ValueError("latency_ms must be a finite non-negative number") from None
        if not math.isfinite(normalized_latency) or normalized_latency < 0:
            raise ValueError("latency_ms must be a finite non-negative number")
        if normalize:
            object.__setattr__(value, "latency_ms", normalized_latency)


@dataclass(frozen=True)
class FindingRead:
    code: str
    severity: str
    witness_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_finding(self)


def _validate_finding(value: FindingRead) -> None:
    _validate_text(value.code, maximum=_MAX_FINDING_CODE_BYTES, field_name="code")
    if not isinstance(value.severity, str) or value.severity not in _SEVERITIES:
        raise ValueError("severity is unsupported")
    if type(value.witness_event_ids) is not tuple or len(value.witness_event_ids) > _MAX_WITNESSES:
        raise ValueError("witness_event_ids must be a bounded tuple")
    for event_id in value.witness_event_ids:
        _validate_text(event_id, maximum=_MAX_IDENTIFIER_BYTES, field_name="witness_event_id")
    if len(set(value.witness_event_ids)) != len(value.witness_event_ids):
        raise ValueError("witness_event_ids must be unique")


@dataclass(frozen=True)
class AgentRunRead:
    schema_version: str
    analysis_version: str
    tenant_id: str
    run_id: str
    started_at: datetime
    ended_at: datetime | None
    status: str
    model_call_count: int
    model_calls: tuple[ModelCallRead, ...]
    model_calls_truncated: bool
    finding_count: int
    findings: tuple[FindingRead, ...]
    findings_truncated: bool

    def __post_init__(self) -> None:
        _validate_agent_run_read(self, normalize=True)


def _validate_agent_run_read(value: AgentRunRead, *, normalize: bool) -> None:
    if _validate_version(value.schema_version, field_name="schema_version") != (
        AGENT_RUN_READ_SCHEMA_VERSION
    ):
        raise ValueError("schema_version is unsupported")
    analysis_version = _validate_version(value.analysis_version, field_name="analysis_version")
    if analysis_version not in SUPPORTED_AGENT_ANALYSIS_VERSIONS:
        raise ValueError("analysis_version is unsupported")
    _validate_text(value.tenant_id, maximum=_MAX_IDENTIFIER_BYTES, field_name="tenant_id")
    _validate_text(value.run_id, maximum=_MAX_IDENTIFIER_BYTES, field_name="run_id")
    started_at = _utc(value.started_at, field_name="started_at")
    ended_at = None if value.ended_at is None else _utc(value.ended_at, field_name="ended_at")
    if ended_at is not None and ended_at < started_at:
        raise ValueError("ended_at cannot precede started_at")
    if normalize:
        object.__setattr__(value, "started_at", started_at)
        object.__setattr__(value, "ended_at", ended_at)
    if not isinstance(value.status, str) or value.status not in _STATUSES:
        raise ValueError("status is unsupported")
    if value.status != "unknown" and ended_at is None:
        raise ValueError("terminal status requires ended_at")

    model_call_count = _validate_count(value.model_call_count, field_name="model_call_count")
    if type(value.model_calls) is not tuple or any(
        type(item) is not ModelCallRead for item in value.model_calls
    ):
        raise ValueError("model_calls must contain exact ModelCallRead values")
    for item in value.model_calls:
        _validate_model_call(item, normalize=normalize)
    model_calls_truncated = _validate_bool(
        value.model_calls_truncated, field_name="model_calls_truncated"
    )
    if len(value.model_calls) != min(model_call_count, _MAX_MODEL_CALLS):
        raise ValueError("model_calls does not match model_call_count")
    if model_calls_truncated is not (model_call_count > _MAX_MODEL_CALLS):
        raise ValueError("model_calls_truncated does not match model_call_count")
    event_ids = [item.event_id for item in value.model_calls]
    trace_ids = [item.trace_id for item in value.model_calls if item.trace_id is not None]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("model-call event IDs must be unique")
    if len(trace_ids) != len(set(trace_ids)):
        raise ValueError("non-null model-call Trace IDs must be unique")

    finding_count = _validate_count(value.finding_count, field_name="finding_count")
    if type(value.findings) is not tuple or any(
        type(item) is not FindingRead for item in value.findings
    ):
        raise ValueError("findings must contain exact FindingRead values")
    for item in value.findings:
        _validate_finding(item)
    findings_truncated = _validate_bool(value.findings_truncated, field_name="findings_truncated")
    if len(value.findings) != min(finding_count, _MAX_FINDINGS):
        raise ValueError("findings does not match finding_count")
    if findings_truncated is not (finding_count > _MAX_FINDINGS):
        raise ValueError("findings_truncated does not match finding_count")


class VerdictReadPort(Protocol):
    """Exact selected-run read boundary implemented by trusted composition roots."""

    def get_agent_run(self, *, tenant_id: str, run_id: str) -> AgentRunRead | None: ...


class StorageVerdictReadPort:
    """Project one exact stored Agent Run into the stable read contract."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def get_agent_run(self, *, tenant_id: str, run_id: str) -> AgentRunRead | None:
        invalid_query = False
        try:
            _validate_text(tenant_id, maximum=_MAX_IDENTIFIER_BYTES, field_name="tenant_id")
            _validate_text(run_id, maximum=_MAX_IDENTIFIER_BYTES, field_name="run_id")
        except (TypeError, ValueError, UnicodeError):
            invalid_query = True
        if invalid_query:
            raise VerdictReadError("invalid_query")

        read_unavailable = False
        try:
            bundle = self._storage.get_agent_run_bundle(tenant_id, run_id)
        except Exception:
            bundle = None
            read_unavailable = True
        if read_unavailable:
            raise VerdictReadError("read_unavailable")
        if bundle is None:
            return None

        invalid_read_model = False
        try:
            projected = _project_agent_run(bundle, tenant_id=tenant_id, run_id=run_id)
        except Exception:
            projected = None
            invalid_read_model = True
        if invalid_read_model:
            raise VerdictReadError("invalid_read_model")
        return projected


def _project_agent_run(bundle: object, *, tenant_id: str, run_id: str) -> AgentRunRead:
    if type(bundle) is not AgentRunBundle:
        raise ValueError("storage returned an invalid Agent Run bundle")
    if type(bundle.turns) is not tuple or type(bundle.events) is not tuple:
        raise ValueError("storage returned invalid Agent Run collections")
    if len(bundle.turns) > _MAX_SOURCE_TURNS or len(bundle.events) > _MAX_SOURCE_EVENTS:
        raise ValueError("stored Agent Run exceeds the V1 analysis limit")
    if bundle.run.tenant_id != tenant_id or bundle.run.run_id != run_id:
        raise ValueError("stored Agent Run does not match the exact query")

    canonical = _canonical_bundle(bundle)
    report = analysis.analyze_agent_run(canonical)
    if (
        type(report) is not analysis.AgentRunAnalysis
        or report.run_id != canonical.run.run_id
        or type(report.findings) is not tuple
        or any(type(item) is not analysis.Finding for item in report.findings)
    ):
        raise ValueError("analysis does not match the Agent Run")

    model_calls = tuple(
        _model_call_read(event)
        for event in canonical.events
        if event.event_type is AgentEventType.MODEL_CALL
    )
    findings = tuple(_finding_read(finding) for finding in report.findings)
    findings = tuple(
        sorted(
            findings,
            key=lambda item: (
                _SEVERITY_RANK[item.severity],
                item.code,
                item.witness_event_ids,
            ),
        )
    )
    return AgentRunRead(
        schema_version=AGENT_RUN_READ_SCHEMA_VERSION,
        analysis_version=AGENT_ANALYSIS_VERSION,
        tenant_id=canonical.run.tenant_id,
        run_id=canonical.run.run_id,
        started_at=canonical.run.started_at,
        ended_at=canonical.run.ended_at,
        status=canonical.run.status.value,
        model_call_count=len(model_calls),
        model_calls=model_calls[:_MAX_MODEL_CALLS],
        model_calls_truncated=len(model_calls) > _MAX_MODEL_CALLS,
        finding_count=len(findings),
        findings=findings[:_MAX_FINDINGS],
        findings_truncated=len(findings) > _MAX_FINDINGS,
    )


def _canonical_bundle(bundle: AgentRunBundle) -> AgentRunBundle:
    session = replace(
        bundle.session,
        started_at=_utc(bundle.session.started_at, field_name="session.started_at"),
        observed_at=_utc(bundle.session.observed_at, field_name="session.observed_at"),
        ended_at=(
            None
            if bundle.session.ended_at is None
            else _utc(bundle.session.ended_at, field_name="session.ended_at")
        ),
    )
    run = replace(
        bundle.run,
        started_at=_utc(bundle.run.started_at, field_name="run.started_at"),
        ended_at=(
            None
            if bundle.run.ended_at is None
            else _utc(bundle.run.ended_at, field_name="run.ended_at")
        ),
    )
    turns = tuple(
        sorted(
            (
                replace(
                    turn,
                    started_at=_utc(turn.started_at, field_name="turn.started_at"),
                    ended_at=(
                        None
                        if turn.ended_at is None
                        else _utc(turn.ended_at, field_name="turn.ended_at")
                    ),
                )
                for turn in bundle.turns
            ),
            key=lambda item: (item.sequence, item.turn_id),
        )
    )
    turn_rank = {turn.turn_id: rank for rank, turn in enumerate(turns)}
    events = tuple(
        sorted(
            (
                replace(
                    event,
                    occurred_at=_utc(event.occurred_at, field_name="event.occurred_at"),
                )
                for event in bundle.events
            ),
            key=lambda item: (
                item.occurred_at,
                turn_rank[item.turn_id],
                item.sequence,
                item.event_id,
            ),
        )
    )
    return AgentRunBundle(session=session, run=run, turns=turns, events=events)


def _model_call_read(event: AgentEvent) -> ModelCallRead:
    raw_latency = event.attributes.get("latency_ms")
    latency = None if raw_latency is None else float(raw_latency)
    return ModelCallRead(
        event_id=event.event_id,
        occurred_at=event.occurred_at,
        status=event.status.value,
        trace_id=event.trace_id,
        latency_ms=latency,
    )


def _finding_read(finding: analysis.Finding) -> FindingRead:
    if finding.judge_used is not False:
        raise ValueError("judge-backed findings are not part of the read contract")
    return FindingRead(
        code=finding.code,
        severity=finding.severity,
        witness_event_ids=finding.evidence_event_ids,
    )


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="auto").replace("+00:00", "Z")


def agent_run_read_to_json(value: AgentRunRead) -> str:
    """Serialize an exact V1 read DTO to bounded canonical JSON."""

    invalid_read_model = False
    try:
        if type(value) is not AgentRunRead:
            raise ValueError("value must be an exact AgentRunRead")
        _validate_agent_run_read(value, normalize=False)
        payload = {
            "schema_version": value.schema_version,
            "analysis_version": value.analysis_version,
            "tenant_id": value.tenant_id,
            "run_id": value.run_id,
            "started_at": _rfc3339(value.started_at),
            "ended_at": None if value.ended_at is None else _rfc3339(value.ended_at),
            "status": value.status,
            "model_call_count": value.model_call_count,
            "model_calls": [
                {
                    "event_id": item.event_id,
                    "occurred_at": _rfc3339(item.occurred_at),
                    "status": item.status,
                    "trace_id": item.trace_id,
                    "latency_ms": item.latency_ms,
                }
                for item in value.model_calls
            ],
            "model_calls_truncated": value.model_calls_truncated,
            "finding_count": value.finding_count,
            "findings": [
                {
                    "code": item.code,
                    "severity": item.severity,
                    "witness_event_ids": item.witness_event_ids,
                }
                for item in value.findings
            ],
            "findings_truncated": value.findings_truncated,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except Exception:
        encoded = ""
        invalid_read_model = True
    if invalid_read_model:
        raise VerdictReadError("invalid_read_model")
    if len(encoded.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise VerdictReadError("response_limit_exceeded")
    return encoded


__all__ = [
    "AGENT_ANALYSIS_VERSION",
    "AGENT_RUN_READ_SCHEMA_VERSION",
    "SUPPORTED_AGENT_ANALYSIS_VERSIONS",
    "AgentRunRead",
    "FindingRead",
    "ModelCallRead",
    "StorageVerdictReadPort",
    "VerdictReadError",
    "VerdictReadPort",
    "agent_run_read_to_json",
]
