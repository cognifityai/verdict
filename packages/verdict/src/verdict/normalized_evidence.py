"""Shared merge and reconstruction rules for normalized Agent Run evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any

from verdict.evidence import (
    AgentCaptureBatch,
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
from verdict.redaction import (
    RedactionMode,
    redact,
    sanitize_agent_capture_batch,
    sanitize_agent_run_bundle,
    sanitize_trace,
)
from verdict.schema import Trace, populate_trace_analysis_fields

LEGACY_AGENT_WRITER_ERROR = "legacy agent evidence writer detected after normalized migration"
LEGACY_AGENT_WRITER_MIGRATION = "block_legacy_agent_evidence_writes_v1"


@dataclass(frozen=True)
class _LegacyLocalTextUpgrade:
    """Internal, non-persistent proof for the a17 local-preview transition."""

    version: str
    turn_previews: Mapping[str, tuple[str | None, str | None]]
    trace_previews: Mapping[str, tuple[str | None, str | None]]


def _sanitize_compatibility_preview(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("legacy local preview must be bounded text")
    try:
        if len(value.encode("utf-8")) > 4_000:
            raise ValueError("legacy local preview must be bounded text")
    except UnicodeError as exc:
        raise ValueError("legacy local preview must be bounded text") from exc
    return redact(value)


def _prepare_legacy_local_text_upgrade(
    bundle: AgentRunBundle,
    traces: Sequence[Trace],
    compatibility: _LegacyLocalTextUpgrade | None,
) -> _LegacyLocalTextUpgrade | None:
    """Validate and sanitize non-persistent a17 local-preview reconciliation hints."""
    if compatibility is None:
        return None
    if compatibility.version != "a17-truncate-before-redact-v1":
        raise ValueError("unsupported legacy local-preview transition")
    if bundle.session.source_kind not in {"claude-code", "codex"}:
        raise ValueError("legacy preview compatibility requires a local-history source")
    turns_by_id = {turn.turn_id: turn for turn in bundle.turns}
    traces_by_id = {trace.trace_id: trace for trace in traces}
    turn_ids = set(turns_by_id)
    trace_ids = set(traces_by_id)
    if not set(compatibility.turn_previews) <= turn_ids:
        raise ValueError("legacy preview references an unknown AgentTurn")
    if not set(compatibility.trace_previews) <= trace_ids:
        raise ValueError("legacy preview references an unknown Trace")

    def sanitized(
        previews: Mapping[str, tuple[str | None, str | None]],
        incoming_by_id: Mapping[str, tuple[str | None, str | None]],
    ) -> dict[str, tuple[str | None, str | None]]:
        result = {}
        for evidence_id, pair in previews.items():
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("legacy preview must contain request and response text")
            clean_pair = (
                _sanitize_compatibility_preview(pair[0]),
                _sanitize_compatibility_preview(pair[1]),
            )
            incoming_pair = incoming_by_id[evidence_id]
            for legacy, incoming in zip(clean_pair, incoming_pair, strict=True):
                if legacy is None:
                    continue
                if (
                    incoming is None
                    or legacy == incoming
                    or incoming.startswith(legacy)
                    or legacy.startswith(incoming)
                ):
                    raise ValueError("legacy preview is not a cutoff-boundary projection")
            if clean_pair == (None, None):
                raise ValueError("legacy preview contains no cutoff-boundary projection")
            result[evidence_id] = clean_pair
        return result

    return _LegacyLocalTextUpgrade(
        version=compatibility.version,
        turn_previews=sanitized(
            compatibility.turn_previews,
            {
                turn_id: (
                    turns_by_id[turn_id].user_request_redacted,
                    turns_by_id[turn_id].final_response_redacted,
                )
                for turn_id in compatibility.turn_previews
            },
        ),
        trace_previews=sanitized(
            compatibility.trace_previews,
            {
                trace_id: (
                    traces_by_id[trace_id].prompt_redacted,
                    traces_by_id[trace_id].response_redacted,
                )
                for trace_id in compatibility.trace_previews
            },
        ),
    )


def prepare_agent_capture(
    bundle: AgentRunBundle,
    traces: Sequence[Trace],
) -> tuple[AgentRunBundle, tuple[Trace, ...], frozenset[str]]:
    """Sanitize and validate capture-owned records before an atomic write."""
    sanitized_bundle = sanitize_agent_run_bundle(bundle)
    prepared, linked_ids = _prepare_linked_traces(sanitized_bundle.events, traces)
    return sanitized_bundle, prepared, linked_ids


def _prepare_linked_traces(
    events: Sequence[AgentEvent],
    traces: Sequence[Trace],
    *,
    mode: RedactionMode = "redact",
    secret: str | None = None,
) -> tuple[tuple[Trace, ...], frozenset[str]]:
    event_links = tuple(event.trace_id for event in events if event.trace_id is not None)
    linked_ids = frozenset(event_links)
    if len(linked_ids) != len(event_links):
        raise ValueError("agent capture links one Trace to multiple AgentEvents")
    prepared: list[Trace] = []
    seen: set[str] = set()
    for source_trace in traces:
        trace = sanitize_trace(deepcopy(source_trace), mode=mode, secret=secret)
        populate_trace_analysis_fields(trace)
        if trace.trace_id in seen:
            raise ValueError("agent capture contains duplicate trace_id")
        if trace.trace_id not in linked_ids:
            raise ValueError("agent capture contains an unlinked Trace")
        seen.add(trace.trace_id)
        prepared.append(trace)
    return tuple(prepared), linked_ids


def prepare_agent_capture_batch(
    batch: AgentCaptureBatch,
    traces: Sequence[Trace],
    *,
    mode: RedactionMode = "redact",
    secret: str | None = None,
) -> tuple[AgentCaptureBatch, tuple[Trace, ...], frozenset[str]]:
    """Sanitize and validate one append batch before an atomic write."""
    sanitized_batch = sanitize_agent_capture_batch(batch, mode=mode, secret=secret)
    prepared, linked_ids = _prepare_linked_traces(
        sanitized_batch.events, traces, mode=mode, secret=secret
    )
    return sanitized_batch, prepared, linked_ids


def require_same_tenant_linked_traces(
    tenant_id: str,
    linked_trace_ids: Sequence[str],
    trace_tenants: Mapping[str, str | None],
) -> None:
    """Validate transaction-visible Trace ownership for every event link."""
    if any(trace_tenants.get(trace_id) != tenant_id for trace_id in linked_trace_ids):
        raise ValueError("model-call event requires a same-tenant Trace")


def _advance_status(
    current: ExecutionStatus,
    incoming: ExecutionStatus,
    *,
    subject: str,
) -> ExecutionStatus:
    if current is incoming or incoming is ExecutionStatus.UNKNOWN:
        return current
    if current is ExecutionStatus.UNKNOWN:
        return incoming
    raise ValueError(f"{subject} terminal status cannot be replaced")


def _advance_end(
    current_status: ExecutionStatus,
    current: datetime | None,
    incoming_status: ExecutionStatus,
    incoming: datetime | None,
    *,
    subject: str,
) -> datetime | None:
    """Advance an open lifecycle while keeping terminal evidence immutable."""
    if current_status is not ExecutionStatus.UNKNOWN:
        if (
            current_status is incoming_status
            and current is not None
            and incoming is not None
            and incoming < current
        ):
            return current
        return _fill_optional(current, incoming, subject=subject)
    if incoming_status is not ExecutionStatus.UNKNOWN:
        return incoming
    if current is None:
        return incoming
    if incoming is None:
        return current
    return max(current, incoming)


def _fill_text(current: str, incoming: str, *, subject: str) -> str:
    if not current:
        return incoming
    if not incoming or incoming == current:
        return current
    raise ValueError(f"{subject} cannot be replaced")


def _fill_optional(current: Any, incoming: Any, *, subject: str) -> Any:
    if current is None:
        return incoming
    if incoming is None or incoming == current:
        return current
    raise ValueError(f"{subject} cannot be replaced")


def _advance_count(current: int | None, incoming: int | None, *, subject: str) -> int | None:
    """Allow append-only sources to complete or increase a provisional count."""
    if current is None:
        return incoming
    if incoming is None or incoming == current:
        return current
    if incoming > current:
        return incoming
    raise ValueError(f"{subject} cannot decrease")


def _extend_text(
    current: str | None,
    incoming: str | None,
    *,
    subject: str,
    legacy_projection: str | None = None,
) -> str | None:
    """Allow a later bounded scan to complete an earlier text prefix."""
    if current is None:
        return incoming
    if incoming is None or incoming == current:
        return current
    if incoming.startswith(current):
        return incoming
    if current.startswith(incoming):
        return current
    if legacy_projection is not None and current == legacy_projection:
        return incoming
    raise ValueError(f"{subject} cannot be replaced")


def _extend_messages(current: Any, incoming: Any) -> Any:
    """Allow capture completion to append messages without rewriting evidence."""
    if current is None:
        return incoming
    if incoming is None or incoming == current:
        return current
    if isinstance(current, list) and isinstance(incoming, list):
        if incoming[: len(current)] == current:
            return incoming
        if current[: len(incoming)] == incoming:
            return current
    raise ValueError("Trace messages cannot be replaced")


def _is_verdict_local_agent_trace(trace: Trace) -> bool:
    """Identify the linked Trace projection owned by the local-history adapter."""
    return (
        trace.tags.get("verdict.source") == "claude-code"
        and trace.tags.get("verdict.workload") == "agent"
        and bool(trace.tags.get("verdict.agent_run_id"))
        and bool(trace.tags.get("verdict.agent_event_id"))
        and trace.tags.get("verdict.input_evidence") == "turn_request_only"
        and trace.tags.get("verdict.time_evidence") == "response_observed_at"
    )


def _local_agent_messages(
    prompt: str | None,
    response: str | None,
) -> list[dict[str, str]] | None:
    """Build the local adapter's canonical two-role message view."""
    messages = []
    if prompt is not None:
        messages.append({"role": "user", "content": prompt})
    if response is not None:
        messages.append({"role": "assistant", "content": response})
    return messages or None


def _validate_local_agent_messages(trace: Trace) -> None:
    if trace.raw_messages != _local_agent_messages(
        trace.prompt_redacted,
        trace.response_redacted,
    ):
        raise ValueError("Trace messages cannot be replaced")


def _advance_evidence_state(
    current: EvidenceState,
    incoming: EvidenceState,
    *,
    has_content: bool,
) -> EvidenceState:
    if has_content:
        return EvidenceState.PRESENT
    if current is incoming or incoming is EvidenceState.NOT_CAPTURED:
        return current
    if current is EvidenceState.NOT_CAPTURED:
        return incoming
    raise ValueError("turn evidence state cannot be replaced")


def merge_capture_trace(
    current: Trace | None,
    incoming: Trace,
    *,
    legacy_preview: tuple[str | None, str | None] | None = None,
) -> Trace:
    """Preserve recorded request facts and only add compatible completion facts."""
    if current is None:
        return incoming
    immutable_fields = (
        "trace_id",
        "started_at",
        "provider",
        "operation",
        "request_model",
        "temperature",
        "max_tokens",
        "tenant_id",
        "session_id",
        "user_id_hash",
        "tags",
    )
    if any(getattr(current, name) != getattr(incoming, name) for name in immutable_fields):
        raise ValueError("Trace request identity facts cannot be replaced")
    local_agent_trace = _is_verdict_local_agent_trace(current)
    prompt = (
        _extend_text(
            current.prompt_redacted,
            incoming.prompt_redacted,
            subject="Trace prompt",
            legacy_projection=legacy_preview[0] if legacy_preview is not None else None,
        )
        if local_agent_trace
        else _fill_optional(
            current.prompt_redacted, incoming.prompt_redacted, subject="Trace prompt"
        )
    )
    response = (
        _extend_text(
            current.response_redacted,
            incoming.response_redacted,
            subject="Trace response",
            legacy_projection=legacy_preview[1] if legacy_preview is not None else None,
        )
        if local_agent_trace
        else _fill_optional(
            current.response_redacted, incoming.response_redacted, subject="Trace response"
        )
    )
    if local_agent_trace:
        _validate_local_agent_messages(current)
        _validate_local_agent_messages(incoming)
        raw_messages = _local_agent_messages(prompt, response)
    else:
        raw_messages = _extend_messages(current.raw_messages, incoming.raw_messages)
    merged = replace(
        incoming,
        ended_at=_fill_optional(current.ended_at, incoming.ended_at, subject="Trace end"),
        response_model=_fill_text(
            current.response_model, incoming.response_model, subject="Trace response model"
        ),
        input_tokens=_fill_optional(
            current.input_tokens, incoming.input_tokens, subject="Trace input tokens"
        ),
        output_tokens=_fill_optional(
            current.output_tokens, incoming.output_tokens, subject="Trace output tokens"
        ),
        finish_reason=_fill_optional(
            current.finish_reason, incoming.finish_reason, subject="Trace finish reason"
        ),
        error=_fill_optional(current.error, incoming.error, subject="Trace error"),
        latency_ms=_fill_optional(current.latency_ms, incoming.latency_ms, subject="Trace latency"),
        prompt_redacted=prompt,
        response_redacted=response,
        raw_messages=raw_messages,
        cost_usd=_fill_optional(current.cost_usd, incoming.cost_usd, subject="Trace cost"),
        parent_span_id=_fill_optional(
            current.parent_span_id, incoming.parent_span_id, subject="Trace parent span"
        ),
        cluster_id=incoming.cluster_id or current.cluster_id,
    )
    populate_trace_analysis_fields(merged)
    return merged


def merge_source_session(
    current: SourceSession | None,
    incoming: SourceSession,
) -> SourceSession:
    if current is None:
        return incoming
    if (
        current.source_session_id,
        current.tenant_id,
        current.source_kind,
        current.source_locator_hash,
        current.started_at,
    ) != (
        incoming.source_session_id,
        incoming.tenant_id,
        incoming.source_kind,
        incoming.source_locator_hash,
        incoming.started_at,
    ):
        raise ValueError("import source identity facts cannot be replaced")
    return SourceSession(
        source_session_id=current.source_session_id,
        tenant_id=current.tenant_id,
        source_kind=current.source_kind,
        source_locator_hash=current.source_locator_hash,
        started_at=current.started_at,
        observed_at=max(current.observed_at, incoming.observed_at),
        ended_at=max(value for value in (current.ended_at, incoming.ended_at) if value is not None)
        if current.ended_at is not None or incoming.ended_at is not None
        else None,
    )


def merge_agent_run(current: AgentRun | None, incoming: AgentRun) -> AgentRun:
    if current is None:
        return incoming
    if (
        current.run_id,
        current.source_session_id,
        current.tenant_id,
        current.started_at,
    ) != (
        incoming.run_id,
        incoming.source_session_id,
        incoming.tenant_id,
        incoming.started_at,
    ):
        raise ValueError("agent run identity facts cannot be replaced")
    return AgentRun(
        run_id=current.run_id,
        source_session_id=current.source_session_id,
        tenant_id=current.tenant_id,
        started_at=current.started_at,
        status=_advance_status(current.status, incoming.status, subject="run"),
        ended_at=_advance_end(
            current.status,
            current.ended_at,
            incoming.status,
            incoming.ended_at,
            subject="run end",
        ),
        agent_name=_fill_text(current.agent_name, incoming.agent_name, subject="agent name"),
        agent_version=_fill_text(
            current.agent_version,
            incoming.agent_version,
            subject="agent version",
        ),
        configuration_fingerprint=_fill_text(
            current.configuration_fingerprint,
            incoming.configuration_fingerprint,
            subject="agent configuration",
        ),
        session_id=_fill_optional(
            current.session_id, incoming.session_id, subject="logical session"
        ),
        parent_run_id=_fill_optional(
            current.parent_run_id, incoming.parent_run_id, subject="parent run"
        ),
        service_name=_fill_text(
            current.service_name, incoming.service_name, subject="service name"
        ),
        environment=_fill_text(current.environment, incoming.environment, subject="environment"),
        instance_id=_fill_text(current.instance_id, incoming.instance_id, subject="instance"),
    )


def merge_agent_turn(
    current: AgentTurn | None,
    incoming: AgentTurn,
    *,
    legacy_preview: tuple[str | None, str | None] | None = None,
) -> AgentTurn:
    if current is None:
        return incoming
    if (current.turn_id, current.run_id, current.sequence, current.started_at) != (
        incoming.turn_id,
        incoming.run_id,
        incoming.sequence,
        incoming.started_at,
    ):
        raise ValueError("turn identity facts cannot be replaced")
    request = _extend_text(
        current.user_request_redacted,
        incoming.user_request_redacted,
        subject="turn request",
        legacy_projection=legacy_preview[0] if legacy_preview is not None else None,
    )
    response = _extend_text(
        current.final_response_redacted,
        incoming.final_response_redacted,
        subject="turn response",
        legacy_projection=legacy_preview[1] if legacy_preview is not None else None,
    )
    return AgentTurn(
        turn_id=current.turn_id,
        run_id=current.run_id,
        sequence=current.sequence,
        started_at=current.started_at,
        status=_advance_status(current.status, incoming.status, subject="turn"),
        ended_at=_advance_end(
            current.status,
            current.ended_at,
            incoming.status,
            incoming.ended_at,
            subject="turn end",
        ),
        user_request_redacted=request,
        final_response_redacted=response,
        request_state=_advance_evidence_state(
            current.request_state,
            incoming.request_state,
            has_content=request is not None,
        ),
        response_state=_advance_evidence_state(
            current.response_state,
            incoming.response_state,
            has_content=response is not None,
        ),
        input_tokens=_advance_count(
            current.input_tokens, incoming.input_tokens, subject="turn input tokens"
        ),
        cached_input_tokens=_advance_count(
            current.cached_input_tokens,
            incoming.cached_input_tokens,
            subject="turn cached input tokens",
        ),
        cache_write_input_tokens=_advance_count(
            current.cache_write_input_tokens,
            incoming.cache_write_input_tokens,
            subject="turn cache-write input tokens",
        ),
        output_tokens=_advance_count(
            current.output_tokens, incoming.output_tokens, subject="turn output tokens"
        ),
        reasoning_output_tokens=_advance_count(
            current.reasoning_output_tokens,
            incoming.reasoning_output_tokens,
            subject="turn reasoning output tokens",
        ),
        total_tokens=_advance_count(
            current.total_tokens, incoming.total_tokens, subject="turn total tokens"
        ),
        token_usage_basis=_fill_optional(
            current.token_usage_basis,
            incoming.token_usage_basis,
            subject="turn token usage basis",
        ),
        request_truncated=(
            incoming.request_truncated
            if request == incoming.user_request_redacted != current.user_request_redacted
            else current.request_truncated
            if request == current.user_request_redacted != incoming.user_request_redacted
            else current.request_truncated or incoming.request_truncated
        ),
        response_truncated=(
            incoming.response_truncated
            if response == incoming.final_response_redacted != current.final_response_redacted
            else current.response_truncated
            if response == current.final_response_redacted != incoming.final_response_redacted
            else current.response_truncated or incoming.response_truncated
        ),
    )


def merge_agent_event(current: AgentEvent | None, incoming: AgentEvent) -> AgentEvent:
    if current is None:
        return incoming
    capture_limit_marker = (
        current.provenance == "verdict:capture_limit"
        and incoming.provenance == current.provenance
        and current.attributes.get("name") == "source_events_omitted"
        and incoming.attributes.get("name") == current.attributes.get("name")
    )
    if (
        current.event_id,
        current.turn_id,
        current.sequence,
        current.event_type,
        current.provenance,
    ) != (
        incoming.event_id,
        incoming.turn_id,
        incoming.sequence,
        incoming.event_type,
        incoming.provenance,
    ) or (current.occurred_at != incoming.occurred_at and not capture_limit_marker):
        raise ValueError("agent event facts cannot be replaced")
    attributes = dict(current.attributes)
    for name, value in incoming.attributes.items():
        if name not in attributes or attributes[name] is None or attributes[name] == "":
            attributes[name] = value
        elif name == "source" and capture_limit_marker:
            try:
                attributes[name] = str(max(int(str(attributes[name])), int(str(value))))
            except ValueError as exc:
                raise ValueError("agent event facts cannot be replaced") from exc
        elif value not in (None, "", attributes[name]):
            raise ValueError("agent event facts cannot be replaced")
    privacy_rank = {
        PrivacyClassification.OMITTED: 0,
        PrivacyClassification.METADATA: 1,
        PrivacyClassification.REDACTED: 2,
    }
    privacy = max(
        (current.privacy_classification, incoming.privacy_classification),
        key=privacy_rank.__getitem__,
    )
    omission_reason = (
        _fill_optional(
            current.omission_reason,
            incoming.omission_reason,
            subject="event omission reason",
        )
        if privacy is PrivacyClassification.OMITTED
        else None
    )
    return AgentEvent(
        event_id=current.event_id,
        turn_id=current.turn_id,
        sequence=current.sequence,
        occurred_at=current.occurred_at,
        event_type=current.event_type,
        status=_advance_status(current.status, incoming.status, subject="event"),
        provenance=current.provenance,
        attributes=attributes,
        privacy_classification=privacy,
        omission_reason=omission_reason,
        trace_id=_fill_optional(current.trace_id, incoming.trace_id, subject="event Trace link"),
        producer_id=_fill_text(current.producer_id, incoming.producer_id, subject="event producer"),
        producer_sequence=_fill_optional(
            current.producer_sequence,
            incoming.producer_sequence,
            subject="event producer sequence",
        ),
        parent_event_id=_fill_optional(
            current.parent_event_id,
            incoming.parent_event_id,
            subject="event parent",
        ),
    )


def normalize_bundle_timestamps(
    bundle: AgentRunBundle | AgentCaptureBatch,
) -> AgentRunBundle | AgentCaptureBatch:
    """Canonicalize persisted timestamps independently of SQL timezone settings."""
    utc = timezone.utc
    session = replace(
        bundle.session,
        started_at=bundle.session.started_at.astimezone(utc),
        observed_at=bundle.session.observed_at.astimezone(utc),
        ended_at=bundle.session.ended_at.astimezone(utc) if bundle.session.ended_at else None,
    )
    run = replace(
        bundle.run,
        started_at=bundle.run.started_at.astimezone(utc),
        ended_at=bundle.run.ended_at.astimezone(utc) if bundle.run.ended_at else None,
    )
    turns = tuple(
        replace(
            turn,
            started_at=turn.started_at.astimezone(utc),
            ended_at=turn.ended_at.astimezone(utc) if turn.ended_at else None,
        )
        for turn in bundle.turns
    )
    events = tuple(
        replace(event, occurred_at=event.occurred_at.astimezone(utc)) for event in bundle.events
    )
    if isinstance(bundle, AgentRunBundle):
        return AgentRunBundle(session, run, turns, events)
    return AgentCaptureBatch(session, run, turns, events)


def normalized_bundle_digest(bundle: AgentRunBundle) -> str:
    """Hash a normalized aggregate without reintroducing the legacy blob limit."""

    def encode_value(value: object) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        raise TypeError(f"unsupported evidence value {type(value).__name__}")

    def encode(record: object) -> bytes:
        return json.dumps(
            asdict(record),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=encode_value,
        ).encode("utf-8")

    digest = sha256()
    for record in (bundle.session, bundle.run, *bundle.turns, *bundle.events):
        digest.update(encode(record))
        digest.update(b"\0")
    return digest.hexdigest()


def detach_missing_trace_links(
    bundle: AgentRunBundle,
    available_trace_ids: set[str],
) -> AgentRunBundle:
    """Preserve legacy events whose linked Trace was already deleted."""
    events = tuple(
        replace(event, trace_id=None)
        if event.trace_id is not None and event.trace_id not in available_trace_ids
        else event
        for event in bundle.events
    )
    return replace(bundle, events=events) if events != bundle.events else bundle


def _datetime(value: object) -> datetime | None:
    if value is None:
        return value
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    raise ValueError("stored agent evidence has an invalid timestamp")


def _json_object(value: object) -> dict[str, Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ValueError("stored agent event attributes must be an object")
    return decoded


def source_session_from_row(row: Mapping[str, object]) -> SourceSession:
    return SourceSession(
        source_session_id=str(row["source_session_id"]),
        tenant_id=str(row["tenant_id"]),
        source_kind=str(row["source_kind"]),
        source_locator_hash=str(row["source_locator_hash"]),
        started_at=_datetime(row["started_at"]),  # type: ignore[arg-type]
        observed_at=_datetime(row["observed_at"]),  # type: ignore[arg-type]
        ended_at=_datetime(row["ended_at"]),
    )


def agent_run_from_row(row: Mapping[str, object]) -> AgentRun:
    return AgentRun(
        run_id=str(row["run_id"]),
        source_session_id=str(row["source_session_id"]),
        tenant_id=str(row["tenant_id"]),
        started_at=_datetime(row["started_at"]),  # type: ignore[arg-type]
        status=ExecutionStatus(str(row["status"])),
        ended_at=_datetime(row["ended_at"]),
        agent_name=str(row["agent_name"] or ""),
        agent_version=str(row["agent_version"] or ""),
        configuration_fingerprint=str(row["configuration_fingerprint"] or ""),
        session_id=str(row["session_id"]) if row.get("session_id") is not None else None,
        parent_run_id=(str(row["parent_run_id"]) if row.get("parent_run_id") is not None else None),
        service_name=str(row.get("service_name") or ""),
        environment=str(row.get("environment") or ""),
        instance_id=str(row.get("instance_id") or ""),
    )


def agent_turn_from_row(row: Mapping[str, object]) -> AgentTurn:
    return AgentTurn(
        turn_id=str(row["turn_id"]),
        run_id=str(row["run_id"]),
        sequence=int(row["sequence"]),
        started_at=_datetime(row["started_at"]),  # type: ignore[arg-type]
        status=ExecutionStatus(str(row["status"])),
        ended_at=_datetime(row["ended_at"]),
        user_request_redacted=(
            str(row["user_request_redacted"]) if row["user_request_redacted"] is not None else None
        ),
        final_response_redacted=(
            str(row["final_response_redacted"])
            if row["final_response_redacted"] is not None
            else None
        ),
        request_state=EvidenceState(str(row["request_state"])),
        response_state=EvidenceState(str(row["response_state"])),
        input_tokens=(int(row["input_tokens"]) if row.get("input_tokens") is not None else None),
        cached_input_tokens=(
            int(row["cached_input_tokens"])
            if row.get("cached_input_tokens") is not None else None
        ),
        cache_write_input_tokens=(
            int(row["cache_write_input_tokens"])
            if row.get("cache_write_input_tokens") is not None else None
        ),
        output_tokens=(
            int(row["output_tokens"]) if row.get("output_tokens") is not None else None
        ),
        reasoning_output_tokens=(
            int(row["reasoning_output_tokens"])
            if row.get("reasoning_output_tokens") is not None else None
        ),
        total_tokens=(
            int(row["total_tokens"]) if row.get("total_tokens") is not None else None
        ),
        token_usage_basis=(
            str(row["token_usage_basis"])
            if row.get("token_usage_basis") is not None else None
        ),
        request_truncated=bool(row.get("request_truncated", False)),
        response_truncated=bool(row.get("response_truncated", False)),
    )


def agent_event_from_row(row: Mapping[str, object]) -> AgentEvent:
    return AgentEvent(
        event_id=str(row["event_id"]),
        turn_id=str(row["turn_id"]),
        sequence=int(row["sequence"]),
        occurred_at=_datetime(row["occurred_at"]),  # type: ignore[arg-type]
        event_type=AgentEventType(str(row["event_type"])),
        status=ExecutionStatus(str(row["status"])),
        provenance=str(row["provenance"]),
        attributes=_json_object(row["attributes_json"]),
        privacy_classification=PrivacyClassification(str(row["privacy_classification"])),
        omission_reason=(
            str(row["omission_reason"]) if row["omission_reason"] is not None else None
        ),
        trace_id=str(row["trace_id"]) if row["trace_id"] is not None else None,
        producer_id=str(row.get("producer_id") or ""),
        producer_sequence=(
            int(row["producer_sequence"]) if row.get("producer_sequence") is not None else None
        ),
        parent_event_id=(
            str(row["parent_event_id"]) if row.get("parent_event_id") is not None else None
        ),
    )


def bundle_from_normalized_rows(
    source: Mapping[str, object],
    run: Mapping[str, object],
    turns: Sequence[Mapping[str, object]],
    events: Sequence[Mapping[str, object]],
) -> AgentRunBundle:
    """Reconstruct the existing public aggregate from normalized records."""
    return AgentRunBundle(
        source_session_from_row(source),
        agent_run_from_row(run),
        tuple(agent_turn_from_row(row) for row in turns),
        tuple(agent_event_from_row(row) for row in events),
    )
