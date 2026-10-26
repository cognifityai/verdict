"""Revision facts for one committed local-history capture.

The file path is intentionally never persisted. These facts describe the
bytes observed by the importer, including an unobserved source tail.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from verdict.evidence import (
    EVENT_CONTENT_FIELDS,
    AgentRunBundle,
    PrivacyClassification,
)
from verdict.normalized_evidence import merge_agent_event, merge_agent_run, merge_agent_turn

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class LocalSourceRevision:
    full_bytes: int
    full_sha256: str
    complete_prefix_bytes: int
    complete_prefix_sha256: str
    partial_final_line: bool  # Stored legacy name; true for any unobserved tail.
    source_turn_count: int | None = None
    source_event_count: int | None = None
    v1_turn_count: int | None = None
    v1_event_count: int | None = None
    min_turn_at: str | None = None
    max_turn_at: str | None = None

    def __post_init__(self) -> None:
        if (type(self.full_bytes) is not int or type(self.complete_prefix_bytes) is not int
                or not 0 <= self.complete_prefix_bytes <= self.full_bytes <= 2**63 - 1
                or not isinstance(self.partial_final_line, bool)
                or not all(isinstance(value, str) and _DIGEST.fullmatch(value)
                           for value in (self.full_sha256, self.complete_prefix_sha256))
                or (not self.partial_final_line
                    and self.complete_prefix_bytes != self.full_bytes)):
            raise ValueError("invalid local source revision")
        counts = (self.source_turn_count, self.source_event_count,
                  self.v1_turn_count, self.v1_event_count)
        if any(value is not None for value in counts):
            if (any(type(value) is not int or value < 0 for value in counts)
                    or self.v1_turn_count > self.source_turn_count
                    or self.v1_event_count > self.source_event_count
                    or self.source_turn_count == 0
                    or self.min_turn_at is None or self.max_turn_at is None):
                raise ValueError("invalid local source coverage")


def revision_from_row(row: Mapping[str, object] | None) -> LocalSourceRevision | None:
    if row is None or row["local_full_bytes"] is None:
        return None
    partial = row["local_partial_line"]
    if type(partial) is bool:
        partial_final_line = partial
    elif type(partial) is int and partial in (0, 1):
        partial_final_line = bool(partial)
    else:
        raise ValueError("invalid stored local source revision")
    return LocalSourceRevision(
        full_bytes=int(row["local_full_bytes"]),
        full_sha256=str(row["local_full_sha256"]),
        complete_prefix_bytes=int(row["local_prefix_bytes"]),
        complete_prefix_sha256=str(row["local_prefix_sha256"]),
        partial_final_line=partial_final_line,
        source_turn_count=_optional_int(row, "local_source_turn_count"),
        source_event_count=_optional_int(row, "local_source_event_count"),
        v1_turn_count=_optional_int(row, "local_v1_turn_count"),
        v1_event_count=_optional_int(row, "local_v1_event_count"),
        min_turn_at=_optional_text(row, "local_min_turn_at"),
        max_turn_at=_optional_text(row, "local_max_turn_at"),
    )


def _optional_value(row: Mapping[str, object], name: str) -> object:
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _optional_int(row: Mapping[str, object], name: str) -> int | None:
    value = _optional_value(row, name)
    return None if value is None else int(value)


def _optional_text(row: Mapping[str, object], name: str) -> str | None:
    value = _optional_value(row, name)
    return None if value is None else str(value)


def validate_recent_window_json(
    value: str | None, *, run_id: str, revision: LocalSourceRevision,
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > 1_048_576:
        raise ValueError("recent_window_limit")
    try:
        decoded = json.loads(value)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid recent window") from exc
    if (not isinstance(decoded, dict)
            or decoded.get("schema") != "local-recent-window-v2"
            or decoded.get("runId") != run_id
            or decoded.get("sourceRevision") != revision.full_sha256
            or decoded.get("sourceTurnCount") != revision.source_turn_count
            or decoded.get("sourceEventCount") != revision.source_event_count
            or not isinstance(decoded.get("turns"), list)
            or not isinstance(decoded.get("events"), list)
            or len(decoded["turns"]) > 100
            or len(decoded["events"]) > 50):
        raise ValueError("invalid recent window")
    return decoded


def verified_recent_findings(
    sample: dict[str, object], *, partial_final_line: bool,
) -> list[dict[str, str]]:
    """Derive bounded findings from selected, complete turns on the read path."""
    from verdict.analysis import recorded_event_issue

    turns = sample.get("turns")
    events = sample.get("events")
    if not isinstance(turns, list) or not isinstance(events, list) or len(turns) > 100 or len(events) > 50:
        raise ValueError("invalid recent window")
    by_sequence: dict[int, dict] = {}
    selected: dict[int, int] = {}
    for turn in turns:
        if (not isinstance(turn, dict) or type(turn.get("sourceSequence")) is not int
                or not isinstance(turn.get("turnId"), str)
                or type(turn.get("sourceEventCount")) is not int
                or turn["sourceEventCount"] < 0
                or turn["sourceSequence"] in by_sequence):
            raise ValueError("invalid recent window turn")
        by_sequence[turn["sourceSequence"]] = turn
    previous_ordinal = -1
    for event in events:
        if (not isinstance(event, dict) or type(event.get("sourceOrdinal")) is not int
                or event["sourceOrdinal"] <= previous_ordinal
                or type(event.get("turnSequence")) is not int
                or not isinstance(event.get("eventId"), str)
                or not isinstance(event.get("turnId"), str)
                or not isinstance(event.get("type"), str)
                or not isinstance(event.get("status"), str)
                or not isinstance(event.get("attributes"), dict)):
            raise ValueError("invalid recent window event")
        previous_ordinal = event["sourceOrdinal"]
        turn = by_sequence.get(event["turnSequence"])
        if turn is None or turn["turnId"] != event["turnId"]:
            raise ValueError("invalid recent window event turn")
        selected[event["turnSequence"]] = selected.get(event["turnSequence"], 0) + 1
    eligible: set[int] = set()
    last_sequence = sample.get("sourceTurnCount")
    if type(last_sequence) is not int:
        raise ValueError("invalid recent window source count")
    for sequence, turn in by_sequence.items():
        count = selected.get(sequence, 0)
        if count != turn.get("selectedEventCount") or count > turn["sourceEventCount"]:
            raise ValueError("invalid recent window selection")
        complete = (turn.get("endedAt") is not None
                    and turn.get("status") in {"completed", "failed", "timed_out", "cancelled"}
                    and not (partial_final_line and sequence == last_sequence - 1))
        expected = complete and count == turn["sourceEventCount"]
        if turn.get("eligibleForFindings") is not expected:
            raise ValueError("invalid recent window eligibility")
        if expected:
            eligible.add(sequence)
    findings = []
    for event in events:
        if event["turnSequence"] not in eligible:
            continue
        code = recorded_event_issue(event["type"], event["status"], event["attributes"])
        if code is not None and len(findings) < 20:
            findings.append({"code": code, "eventId": event["eventId"],
                             "turnId": event["turnId"]})
    return findings


def same_recent_window(previous: object, candidate: dict[str, object]) -> bool:
    return (isinstance(previous, dict)
            and previous.get("schema") == "local-recent-window-v2"
            and {key: value for key, value in previous.items() if key != "capturedAt"}
            == {key: value for key, value in candidate.items() if key != "capturedAt"})


def hash_file_prefix(path: Path, size: int) -> str:
    """Hash exactly `size` bytes, failing if the source shrank mid-read."""
    if type(size) is not int or not 0 <= size <= 2**63 - 1:
        raise ValueError("invalid local source size")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = size
        while remaining:
            block = handle.read(min(remaining, 1024 * 1024))
            if not block:
                raise ValueError("local_source_changed")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def verify_source_revision(
    path: Path, candidate: LocalSourceRevision, prior: LocalSourceRevision | None,
) -> bool:
    """Verify source bytes; return whether a compatible larger rewrite needs proof."""
    compatible_rewrite = False
    if prior is not None:
        if candidate.full_bytes < prior.full_bytes:
            raise ValueError("local_source_conflict")
        if candidate.full_bytes == prior.full_bytes and candidate.full_sha256 != prior.full_sha256:
            raise ValueError("local_source_conflict")
        compatible_rewrite = (
            candidate.full_bytes > prior.full_bytes
            and hash_file_prefix(path, prior.full_bytes) != prior.full_sha256
        )
        if compatible_rewrite:
            if (prior.partial_final_line or candidate.partial_final_line
                    or prior.source_turn_count is None
                    or candidate.source_turn_count is None
                    or prior.source_turn_count != prior.v1_turn_count
                    or prior.source_event_count != prior.v1_event_count
                    or candidate.source_turn_count != candidate.v1_turn_count
                    or candidate.source_event_count != candidate.v1_event_count):
                raise ValueError("local_source_conflict")
    if path.stat().st_size != candidate.full_bytes:
        raise ValueError("local_source_changed")
    if hash_file_prefix(path, candidate.full_bytes) != candidate.full_sha256:
        raise ValueError("local_source_changed")
    return compatible_rewrite


def verify_retained_local_evidence(
    previous: AgentRunBundle | None,
    candidate: AgentRunBundle,
    *,
    require_recorded_content: bool = False,
    legacy_turn_previews: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> None:
    """Prove a rewrite or beta bootstrap extends every retained real fact."""
    if previous is None:
        return
    if require_recorded_content and any(
        event.provenance == "verdict:capture_limit"
        for event in (*previous.events, *candidate.events)
    ):
        raise ValueError("local_source_conflict")
    try:
        merge_agent_run(previous.run, candidate.run)
        new_turns = {turn.turn_id: turn for turn in candidate.turns}
        for old_turn in previous.turns:
            new_turn = new_turns[old_turn.turn_id]
            if require_recorded_content and (
                (old_turn.user_request_redacted is not None
                 and new_turn.user_request_redacted is None)
                or (old_turn.final_response_redacted is not None
                    and new_turn.final_response_redacted is None)
            ):
                raise ValueError("previously captured content is absent")
            merge_agent_turn(
                old_turn, new_turn,
                legacy_preview=(legacy_turn_previews or {}).get(old_turn.turn_id),
            )
        new_events = {
            event.event_id: event for event in candidate.events
            if event.provenance not in {"verdict:partial_source_line", "verdict:capture_limit"}
        }
        for old_event in previous.events:
            if old_event.provenance in {"verdict:partial_source_line", "verdict:capture_limit"}:
                continue
            new_event = new_events[old_event.event_id]
            if old_event.trace_id is not None and old_event.trace_id != new_event.trace_id:
                raise ValueError("previous Trace link changed")
            if require_recorded_content and (
                (old_event.privacy_classification is PrivacyClassification.REDACTED
                 and new_event.privacy_classification is not PrivacyClassification.REDACTED)
                or any(
                    (key in EVENT_CONTENT_FIELDS and value not in (None, "")
                     and new_event.attributes.get(key) in (None, ""))
                    for key, value in old_event.attributes.items()
                )
            ):
                raise ValueError("previously captured event content is absent")
            merge_agent_event(old_event, new_event)
    except (KeyError, ValueError) as exc:
        raise ValueError("local_source_conflict") from exc


def local_scope_partial(
    source_kind: str,
    revision: LocalSourceRevision | None,
    last_turn_sequence: int,
    selected_turn_sequence: int | None,
) -> bool:
    """Legacy local imports are unverified; an incomplete tail affects the last turn."""
    if source_kind not in {"codex", "claude-code"}:
        return False
    if revision is None:
        return True
    if revision.source_turn_count is None:
        return True
    if selected_turn_sequence is None:
        if (revision.source_turn_count != revision.v1_turn_count
                or revision.source_event_count != revision.v1_event_count):
            return True
    if not revision.partial_final_line:
        return False
    source_last = (revision.source_turn_count - 1
                   if revision.source_turn_count is not None else last_turn_sequence)
    return (selected_turn_sequence is None
            or selected_turn_sequence >= source_last)
