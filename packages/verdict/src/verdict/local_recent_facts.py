"""Selected local-history facts outside the bounded legacy run projection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum

from verdict.evidence import AgentEvent, AgentEventType, AgentRunBundle, AgentTurn
from verdict.local_source_revision import LocalSourceRevision
from verdict.schema import Trace


@dataclass(frozen=True)
class LocalRecentFact:
    source_ordinal: int
    turn_sequence: int
    event: AgentEvent

    def __post_init__(self) -> None:
        if (type(self.source_ordinal) is not int or self.source_ordinal < 0
                or type(self.turn_sequence) is not int or self.turn_sequence < 0):
            raise ValueError("invalid local recent fact ordinal")


@dataclass(frozen=True)
class LocalFullProjection:
    """Admitted facts outside the stable V1 prefix and selected recent index."""

    facts: tuple[LocalRecentFact, ...]
    tail_turns: tuple[AgentTurn, ...]


def tail_turn_json(turn: AgentTurn) -> str:
    def encode(value: object) -> str:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, Enum):
            return str(value.value)
        raise TypeError("unsupported tail turn value")

    return json.dumps(asdict(turn), default=encode, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def validate_full_projection(
    projection: LocalFullProjection | None, bundle: AgentRunBundle,
    selected: tuple[LocalRecentFact, ...], historical: tuple[LocalRecentFact, ...],
    revision: LocalSourceRevision | None,
) -> None:
    if projection is None:
        return
    if revision is None or revision.partial_final_line:
        raise ValueError("local_full_projection_incomplete_source")
    real_events = tuple(event for event in bundle.events if event.provenance not in {
        "verdict:capture_limit", "verdict:partial_source_line",
    })
    all_facts = (*selected, *historical, *projection.facts)
    ids = [event.event_id for event in real_events] + [fact.event.event_id for fact in all_facts]
    ordinals = list(range(len(real_events))) + [fact.source_ordinal for fact in all_facts]
    turns = (*bundle.turns, *projection.tail_turns)
    if (len(ids) != revision.source_event_count or len(ids) != len(set(ids))
            or sorted(ordinals) != list(range(revision.source_event_count))
            or len(turns) != revision.source_turn_count
            or [turn.sequence for turn in turns] != list(range(revision.source_turn_count))
            or len({turn.turn_id for turn in turns}) != len(turns)):
        raise ValueError("local_full_projection_conflict")
    owners = {turn.sequence: turn.turn_id for turn in turns}
    if any(owners.get(fact.turn_sequence) != fact.event.turn_id for fact in all_facts):
        raise ValueError("local_full_projection_conflict")


def validate_recent_facts(
    facts: tuple[LocalRecentFact, ...], window: dict[str, object] | None,
    prefix_event_ids: frozenset[str],
) -> None:
    """Require the selected non-prefix facts to match the current window."""
    if window is None:
        if facts:
            raise ValueError("local recent facts require a current window")
        return
    prefix_count = window.get("canonicalPrefixEvents")
    rows = window.get("events")
    if type(prefix_count) is not int or not isinstance(rows, list):
        raise ValueError("invalid local recent fact index")
    selected = {
        row["eventId"]: row for row in rows
        if isinstance(row, dict) and type(row.get("sourceOrdinal")) is int
        and row["sourceOrdinal"] >= prefix_count
    }
    if (len(selected) != len(facts)
            or {fact.event.event_id for fact in facts} != set(selected)
            or any(fact.event.event_id in prefix_event_ids for fact in facts)):
        raise ValueError("local recent facts do not match selected window")
    ordinals: set[int] = set()
    for fact in facts:
        event = fact.event
        row = selected[event.event_id]
        if (fact.source_ordinal in ordinals
                or row.get("sourceOrdinal") != fact.source_ordinal
                or row.get("turnSequence") != fact.turn_sequence
                or row.get("turnId") != event.turn_id
                or row.get("eventSequence") != event.sequence):
            raise ValueError("local recent fact differs from selected window")
        ordinals.add(fact.source_ordinal)


def validate_historical_facts(
    selected: tuple[LocalRecentFact, ...], historical: tuple[LocalRecentFact, ...],
    prefix_event_ids: frozenset[str],
) -> None:
    """Reverified facts may update old IDs, never admit unselected new ones."""
    selected_ids = {fact.event.event_id for fact in selected}
    historical_ids = [fact.event.event_id for fact in historical]
    if (len(historical_ids) != len(set(historical_ids))
            or selected_ids.intersection(historical_ids)
            or prefix_event_ids.intersection(historical_ids)):
        raise ValueError("local_source_conflict")


def require_all_retained_facts(
    retained_ids: set[str], selected: tuple[LocalRecentFact, ...],
    historical: tuple[LocalRecentFact, ...],
) -> None:
    selected_ids = {fact.event.event_id for fact in selected}
    historical_ids = {fact.event.event_id for fact in historical}
    if retained_ids - selected_ids != historical_ids:
        raise ValueError("local_source_conflict")


def reject_lost_model_usage(previous: AgentEvent | None, incoming: AgentEvent) -> None:
    """An invalid appended Claude usage row cannot leave an old current claim."""
    if previous is None or incoming.event_type is not AgentEventType.MODEL_CALL:
        return
    if any(previous.attributes.get(key) is not None
           and incoming.attributes.get(key) is None
           for key in ("input_tokens", "output_tokens")):
        raise ValueError("local_source_conflict")


def trace_evidence_fingerprint(trace: Trace) -> str:
    """Version the fields a judge or reader can use for a local model call."""
    fields = (
        trace.trace_id, trace.started_at.astimezone(timezone.utc).isoformat(),
        trace.ended_at.astimezone(timezone.utc).isoformat() if trace.ended_at else None,
        trace.provider, trace.operation.value, trace.request_model,
        trace.response_model, trace.input_tokens, trace.output_tokens,
        trace.finish_reason, trace.error, trace.prompt_redacted,
        trace.response_redacted, trace.raw_messages, trace.tags,
    )
    encoded = json.dumps(fields, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
