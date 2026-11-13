"""Opt-in evaluation of bounded session, run, turn, or tool evidence."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from verdict.dashboard.control_plane import ControlStore, _safe_json
from verdict.dashboard.evaluator_lab import (
    _destination,
    _IdentityOnlyProvider,
    _judge,
    _output_maximum,
    _provider,
    _rubric_summary,
    _uses_public_list_price,
    _validated_config,
)
from verdict.dashboard.scoped_evidence import paired_evidence, suitability
from verdict.dashboard.setup_routes import SetupRoutes
from verdict.dashboard.spend_limit import within_static_spend_limit
from verdict.dashboard.storage_url import is_postgres_storage
from verdict.evidence import (
    AgentEventType,
    EvidenceState,
    disclosed_event_attributes,
    event_order_key,
)
from verdict.pricing import compute_cost_usd, compute_exact_list_price_usd
from verdict.redaction import _PLACEHOLDER, redact_structure
from verdict.storage.sqlite import sqlite_path

_MAX_TURNS = 20
_MAX_EVENTS = 80
_MAX_EVIDENCE_CHARS = 8_000
_PROMPT_VERSION = "ordered-agent-evidence-v4"
_FOLLOWUP_TEXT_RUBRIC = {
    "name": "followup_text_review", "version": "1", "dimensions": [{
        "name": "correction",
        "description": (
            "PASS only when the later user request is a new task or ordinary refinement. "
            "FAIL only when that request explicitly corrects or re-explains the earlier "
            "exchange. Cite both recorded turns. Do not decide whether the agent was wrong, "
            "what caused the follow-up, or whether the task succeeded. Return UNCLEAR when "
            "the relationship is not supported by these texts."
        ),
    }],
}
_REF = re.compile(r"\[([a-z_]+):([^\]]+)\]")
_CITATION_REQUEST = ("For each scored dimension, cite at least one exact [turn:...] or "
                     "[event:...] reference from the recorded evidence in the reasoning. "
                     "If the evidence does not support a judgment, return UNCLEAR.\n")
_LOCK = threading.Lock()


def _recent_progress_excerpt(value: str | None) -> str | None:
    if value is None or len(value) <= 512:
        return value
    excerpt = value[-512:]
    if "\n" in excerpt:
        excerpt = excerpt.split("\n", 1)[1]
    return "[earlier recorded progress omitted]\n" + excerpt


class UnsupportedScopedPlatform(OSError):
    """Cross-process SQLite scoped evaluation locking is unavailable."""


@contextlib.contextmanager
def _cross_process_scoped_guard(storage_url: str):
    """Serialize scoped calls for one store until the saved-result replay check completes."""
    if is_postgres_storage(storage_url):
        import psycopg

        with psycopg.connect(storage_url, autocommit=True) as connection:
            connection.execute("SELECT pg_advisory_lock(%s)", (0x5645524449435402,))
            try:
                yield
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s)", (0x5645524449435402,))
        return
    if os.name != "posix":
        raise UnsupportedScopedPlatform(
            "SQLite scoped evaluation locking requires POSIX; use PostgreSQL"
        )
    import fcntl

    path = sqlite_path(storage_url)
    lock_path = path.with_name(path.name + ".scoped.lock")
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _finish_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    evidence["evidenceFingerprint"] = hashlib.sha256(_canonical(evidence)).hexdigest()
    return evidence


def _usable_tool_content(value: object) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    try:
        return _PLACEHOLDER.search(json.dumps(value, ensure_ascii=False)) is None
    except (TypeError, ValueError, UnicodeError):
        return False


def _session_projection(storage_url: str, tenant: str, config: dict[str, Any]) -> dict[str, Any]:
    from verdict.dashboard.app import build_agent_session_bundle

    session_id, source_kind = config.get("sessionId"), config.get("sourceKind")
    if (not isinstance(session_id, str) or not session_id or len(session_id.encode()) > 256
            or not isinstance(source_kind, str) or not source_kind
            or len(source_kind.encode()) > 256
            or config.get("runId") is not None or config.get("eventId") is not None
            or config.get("turnId") is not None):
        raise ValueError("invalid session evaluation selection")
    report = build_agent_session_bundle(
        storage_url, tenant=tenant, source_kind=source_kind, session_id=session_id,
    )
    return session_projection_from_report(report, session_id=session_id, source_kind=source_kind)


def session_projection_from_report(
    report: dict[str, Any], *, session_id: str, source_kind: str,
) -> dict[str, Any]:
    evidence = _session_coverage(report, session_id=session_id, source_kind=source_kind)
    paired = paired_evidence(report["turns"], report["events"],
                             source_kind=source_kind, session_id=session_id)
    return _attach_pairs(evidence, paired, {"request", "response"})


def _session_coverage(report: dict[str, Any], *, session_id: str, source_kind: str) -> dict:
    """Use the same evidence and size checks for the session view and preview."""
    reason = report["unavailableReason"]
    turns, events = report["turns"], report["events"]
    if reason is None:
        if not turns:
            reason = "no_turns"
        elif any(turn["requestState"] != "present" or turn["responseState"] != "present"
                 or turn["requestTruncated"] or turn["responseTruncated"]
                 or turn["endedAt"] is None for turn in turns):
            reason = "turn_content_partial"
        elif any(event["provenance"] == "verdict:capture_limit" for event in events):
            reason = "source_capture_partial"
        elif any(len({event["producerId"] for event in events
                      if event["runId"] == run_id and event["producerId"]}) > 1
                 for run_id in report["runIds"]):
            reason = "event_order_uncertain"
    if reason is not None:
        return _finish_evidence({
            "scope": "session", "sessionId": session_id, "sourceKind": source_kind,
            "runIds": report["runIds"], "runId": None, "eventId": None,
            "query": "", "response": "", "evidenceReferences": [],
            "coverage": "partial", "unavailableReason": reason,
        })
    requests = [{"reference": f"[turn:{turn['runId']}/{turn['turnId']}]",
                 "request": turn["request"]} for turn in turns]
    timeline = [
        (turn["endedAt"] or turn["startedAt"], 1, turn["runId"],
         {"reference": f"[turn:{turn['runId']}/{turn['turnId']}]", "response": turn["response"]})
        for turn in turns
    ] + [
        (event["occurredAt"], 0, event["runId"],
         {"reference": f"[event:{event['runId']}/{event['eventId']}]",
          "type": event["type"], "status": event["status"],
          "attributes": event["attributes"]})
        for event in events
    ]
    query = _CITATION_REQUEST + "Recorded session requests (ordered by turn):\n" + json.dumps(requests, ensure_ascii=False)
    response = "Recorded session work (timestamp order, not causal proof):\n" + json.dumps(
        [item for _, _, _, item in sorted(timeline, key=lambda row: row[:3])],
        ensure_ascii=False,
    )
    safe = redact_structure({"query": query, "response": response})
    if not isinstance(safe, dict) or not all(isinstance(safe.get(k), str) for k in ("query", "response")):
        reason = "redaction_unavailable"
        safe = {"query": "", "response": ""}
    if len(safe["query"]) + len(safe["response"]) > _MAX_EVIDENCE_CHARS:
        reason = "evidence_limit"
        safe = {"query": "", "response": ""}
    evidence = {
        "scope": "session", "sessionId": session_id, "sourceKind": source_kind,
        "runIds": report["runIds"], "runId": None, "eventId": None,
        "query": safe["query"], "response": safe["response"],
        "evidenceReferences": [f"turn:{turn['runId']}/{turn['turnId']}" for turn in turns]
        + [f"event:{event['runId']}/{event['eventId']}" for event in events],
        "coverage": "complete" if reason is None else "partial",
        "unavailableReason": reason,
    }
    return _finish_evidence(evidence)


def _raw_projection(storage, tenant: str, config: dict[str, Any],
                storage_url: str | None = None, *, bundle=None,
                local_revision=None) -> dict[str, Any]:
    scope = config.get("scope")
    if scope == "session":
        if storage_url is None:
            raise ValueError("session evaluation requires storage URL")
        return _session_projection(storage_url, tenant, config)
    run_id = config.get("runId")
    event_id = config.get("eventId")
    turn_id = config.get("turnId")
    if (scope not in {"run", "turn_window", "followup_text", "tool_action"}
            or not isinstance(run_id, str)
            or not run_id or len(run_id.encode()) > 256 or
            (scope == "tool_action" and (not isinstance(event_id, str) or not event_id
             or len(event_id.encode()) > 256)) or
            (scope in {"turn_window", "followup_text"} and (not isinstance(turn_id, str) or not turn_id
             or len(turn_id.encode()) > 256)) or
            (scope not in {"turn_window", "followup_text"} and turn_id is not None) or
            (scope != "tool_action" and event_id is not None)):
        raise ValueError("invalid scoped evaluation selection")
    if bundle is None:
        snapshot = getattr(storage, "get_agent_capture_snapshot", None)
        if callable(snapshot):
            bundle, local_revision = snapshot(tenant, run_id)
        else:
            bundle = storage.get_agent_run_bundle(tenant, run_id)
    if bundle is None:
        raise KeyError(run_id)
    from verdict.local_source_revision import local_scope_partial
    turns = sorted(bundle.turns, key=lambda turn: (turn.sequence, turn.turn_id))
    events = sorted(bundle.events, key=event_order_key)
    extra_evidence: dict[str, Any] = {}
    if scope == "followup_text":
        selected_index = next((index for index, turn in enumerate(turns)
                               if turn.turn_id == turn_id), None)
        if selected_index is None:
            raise KeyError(turn_id)
        if (selected_index == 0 or
                turns[selected_index].sequence != turns[selected_index - 1].sequence + 1):
            raise ValueError("adjacent turn evidence unavailable")
        prior, selected = turns[selected_index - 1:selected_index + 1]
        reason = None
        if (prior.request_state is not EvidenceState.PRESENT
                or prior.response_state is not EvidenceState.PRESENT
                or selected.request_state is not EvidenceState.PRESENT
                or prior.request_truncated or prior.response_truncated
                or selected.request_truncated or prior.ended_at is None
                or selected.ended_at is None):
            reason = "turn_content_partial"
        if reason is not None:
            return _finish_evidence({
                "scope": scope, "runId": run_id, "turnId": turn_id,
                "eventId": None, "query": "", "response": "",
                "evidenceReferences": [], "coverage": "partial",
                "unavailableReason": reason,
            })
        query = (_CITATION_REQUEST
                 + "Text-only question: did the later user request explicitly correct or "
                   "re-explain the preceding exchange? Do not infer task quality or cause "
                   "from omitted events.\nRecorded user requests:\n"
                 + json.dumps([
                     {"reference": f"[turn:{prior.turn_id}]", "request": prior.user_request_redacted},
                     {"reference": f"[turn:{selected.turn_id}]",
                      "request": selected.user_request_redacted},
                 ], ensure_ascii=False))
        response = "Prior recorded answer only; later agent work and typed events are excluded:\n" + json.dumps({
            "reference": f"[turn:{prior.turn_id}]", "response": prior.final_response_redacted,
        }, ensure_ascii=False)
        refs = [f"turn:{prior.turn_id}", f"turn:{selected.turn_id}"]
        partial_source = (any(event.provenance == "verdict:capture_limit"
                              for event in bundle.events)
                          or local_scope_partial(
                              bundle.session.source_kind, local_revision,
                              max((turn.sequence for turn in bundle.turns), default=-1),
                              selected.sequence,
                          ))
        extra_evidence = {
            "coverageBasis": "recorded_text_only",
            "sourceCoveragePartial": partial_source,
            "excludedEvidence": "Typed events and later agent work are not reviewed.",
        }
        reason = None
    elif scope in {"run", "turn_window"}:
        if scope == "turn_window":
            selected_index = next((index for index, turn in enumerate(turns)
                                   if turn.turn_id == turn_id), None)
            if selected_index is None:
                raise KeyError(turn_id)
            if selected_index == 0 or turns[selected_index].sequence != turns[selected_index - 1].sequence + 1:
                raise ValueError("adjacent turn evidence unavailable")
            turns = turns[selected_index - 1:selected_index + 1]
            selected_ids = {turn.turn_id for turn in turns}
            events = [event for event in events if event.turn_id in selected_ids]
        producers = {event.producer_id for event in events if event.producer_id}
        turn_sequences = {turn.turn_id: turn.sequence for turn in bundle.turns}
        reason = None
        if not turns:
            reason = "no_turns"
        elif len(turns) > _MAX_TURNS or len(events) > _MAX_EVENTS:
            reason = "evidence_limit"
        elif len(producers) > 1:
            reason = "event_order_uncertain"
        elif (any(event.provenance == "verdict:capture_limit"
                  and (scope == "run" or turn_sequences.get(event.turn_id, -1) <= turns[-1].sequence)
                  for event in bundle.events)
              or local_scope_partial(
                  bundle.session.source_kind, local_revision,
                  max((turn.sequence for turn in bundle.turns), default=-1),
                  turns[-1].sequence if scope == "turn_window" else None,
              )):
            reason = "source_capture_partial"
        elif any(turn.request_state is not EvidenceState.PRESENT or
                 turn.response_state is not EvidenceState.PRESENT or
                 turn.request_truncated or turn.response_truncated or turn.ended_at is None
                 for turn in turns):
            reason = "turn_content_partial"
        if reason is not None:
            return _finish_evidence({
                "scope": scope, "runId": run_id, "eventId": None,
                **({"turnId": turn_id} if scope == "turn_window" else {}),
                "query": "", "response": "", "evidenceReferences": [],
                "coverage": "partial", "unavailableReason": reason,
            })
        requests = [{"reference": f"[turn:{turn.turn_id}]", "request": turn.user_request_redacted,
                     **({"startedAt": turn.started_at.isoformat()} if scope == "turn_window" else {})}
                    for turn in turns]
        timed = [
            (turn.ended_at or turn.started_at, 1, turn.sequence,
             {"reference": f"[turn:{turn.turn_id}]", "response": turn.final_response_redacted,
              **({"endedAt": turn.ended_at.isoformat(),
                  "recordedProgressExcerpt": _recent_progress_excerpt(
                      turn.progress_redacted
                  )} if scope == "turn_window" else {})})
            for turn in turns
        ] + [
            (event.occurred_at, 0, event_order_key(event),
             {"reference": f"[event:{event.event_id}]", "type": event.event_type.value,
              "status": event.status.value,
              "attributes": disclosed_event_attributes(event.attributes)})
            for event in events
        ]
        timeline = [item for _, _, _, item in sorted(timed, key=lambda row: row[:3])]
        query = (_CITATION_REQUEST
                 + ("Recorded progress is a bounded excerpt when available; it does not prove delivery, and absence is not proof of silence.\n"
                    if scope == "turn_window" else "")
                 + "Recorded task requests (ordered by turn):\n"
                 + json.dumps(requests, ensure_ascii=False))
        response = "Recorded agent work (timestamp ordered events; turn outputs included):\n" + json.dumps(
            timeline, ensure_ascii=False,
        )
        refs = [f"turn:{turn.turn_id}" for turn in turns] + [f"event:{event.event_id}" for event in events]
    else:
        matching = [event for event in events if event.event_id == event_id]
        if len(matching) != 1 or matching[0].event_type is not AgentEventType.TOOL_CALL:
            raise KeyError(event_id)
        call = matching[0]
        turn = next((item for item in turns if item.turn_id == call.turn_id), None)
        if turn is None:
            raise KeyError(call.turn_id)
        turn_events = [event for event in events if event.turn_id == call.turn_id]
        call_id = call.attributes.get("call_id")
        matching_calls = [event for event in turn_events
                          if event.event_type is AgentEventType.TOOL_CALL
                          and event.attributes.get("call_id") == call_id]
        results = [event for event in events if event.event_type is AgentEventType.TOOL_RESULT
                   and event.turn_id == call.turn_id and call_id and
                   event.attributes.get("call_id") == call_id]
        contracts = [event for event in events[:events.index(call)]
                     if event.event_type is AgentEventType.INSTRUCTION
                     and event.attributes.get("name") in {"tool_contract", "available_tools"}][-1:]
        reason = None
        turn_sequences = {item.turn_id: item.sequence for item in turns}
        if (any(turn_sequences.get(event.turn_id, -1) <= turn.sequence
                for event in events if event.provenance == "verdict:capture_limit")
                or local_scope_partial(
                    bundle.session.source_kind, local_revision,
                    max((item.sequence for item in turns), default=-1), turn.sequence,
                )):
            reason = "source_capture_partial"
        elif (len({event.producer_id for event in turn_events if event.producer_id}) > 1
                or not isinstance(call_id, str) or not call_id or not _usable_tool_content(call_id)
                or len(matching_calls) != 1 or len(results) != 1
                or not _usable_tool_content(call.attributes.get("tool_name"))
                or not _usable_tool_content(results[0].attributes.get("result"))
                or (results[0].attributes.get("tool_name") is not None
                    and results[0].attributes["tool_name"] != call.attributes.get("tool_name"))):
            reason = "tool_result_or_order_unavailable"
        elif (not contracts or turn.request_state is not EvidenceState.PRESENT
              or turn.request_truncated
              or not _usable_tool_content(turn.user_request_redacted)
              or contracts[0].attributes.get("available") is False
              or not _usable_tool_content(contracts[0].attributes.get("text"))):
            reason = "tool_contract_or_task_unavailable"
        elif ("arguments" not in call.attributes
              or not _usable_tool_content(call.attributes["arguments"])):
            reason = "tool_arguments_unavailable"
        query = _CITATION_REQUEST + "Task and available tool contract:\n" + json.dumps({
            "task": turn.user_request_redacted,
            "contract": [{"reference": f"[event:{item.event_id}]", "text": item.attributes.get("text")}
                         for item in contracts],
        }, ensure_ascii=False)
        response = "Actual tool action and recorded result:\n" + json.dumps({
            "call": {"reference": f"[event:{call.event_id}]",
                     "attributes": disclosed_event_attributes(call.attributes)},
            "results": [{"reference": f"[event:{item.event_id}]", "status": item.status.value,
                         "attributes": disclosed_event_attributes(item.attributes)}
                        for item in results],
        }, ensure_ascii=False)
        refs = [f"turn:{turn.turn_id}", f"event:{call.event_id}"] + [
            f"event:{item.event_id}" for item in (*contracts, *results)
        ]
    if reason is not None:
        return _finish_evidence({
            "scope": scope, "runId": run_id, "eventId": event_id,
            "query": "", "response": "", "evidenceReferences": refs[:_MAX_EVENTS + _MAX_TURNS],
            "coverage": "partial", "unavailableReason": reason,
        })
    safe = redact_structure({"query": query, "response": response})
    if not isinstance(safe, dict) or not all(isinstance(safe.get(k), str) for k in ("query", "response")):
        reason = "redaction_unavailable"
        safe = {"query": "", "response": ""}
    if len(safe["query"]) + len(safe["response"]) > _MAX_EVIDENCE_CHARS:
        reason = "evidence_limit"
        safe = {"query": "", "response": ""}
    evidence = {"scope": scope, "runId": run_id, "eventId": event_id,
                **({"turnId": turn_id} if scope in {"turn_window", "followup_text"} else {}),
                "query": safe["query"], "response": safe["response"],
                "evidenceReferences": refs, "coverage": "complete" if reason is None else "partial",
                "unavailableReason": reason, **extra_evidence}
    return _finish_evidence(evidence)


_SIZE_LIMITS = {"evidence_limit", "run_limit", "turn_limit", "event_limit"}
_NO_PAIRS = {"turnPairs": [], "documents": {}, "chronologyCertain": False}


def _attach_pairs(evidence: dict, paired: dict, required: set[str]) -> dict:
    evidence.pop("evidenceFingerprint", None)
    if evidence["unavailableReason"] in _SIZE_LIMITS:
        # Oversized evidence is withheld whole, never projected or redacted.
        paired = dict(_NO_PAIRS)
    elif paired["turnPairs"]:
        # Stored turn text was sanitized by whichever release captured it.
        # Apply the current sanitizer, as the ordinary query/response path
        # does, before the pairs are fingerprinted, previewed, or sent.
        paired = redact_structure(json.loads(json.dumps(paired, ensure_ascii=False)))
        if not isinstance(paired, dict):
            paired = dict(_NO_PAIRS)
            evidence["unavailableReason"] = evidence["unavailableReason"] or "redaction_unavailable"
            evidence["coverage"] = "partial"
    if evidence["unavailableReason"] == "event_order_uncertain":
        paired["chronologyCertain"] = False
    evidence.update(paired)
    evidence["suitability"] = suitability(paired, required)
    return _finish_evidence(evidence)


def _projection(storage, tenant: str, config: dict[str, Any],
                storage_url: str | None = None, *, bundle=None, local_revision=None) -> dict:
    if config.get("scope") == "session":
        evidence = _session_projection(storage_url, tenant, config)
    else:
        if bundle is None:
            snapshot = getattr(storage, "get_agent_capture_snapshot", None)
            if callable(snapshot):
                bundle, local_revision = snapshot(tenant, config.get("runId"))
            else:
                bundle = storage.get_agent_run_bundle(tenant, config.get("runId"))
        evidence = _raw_projection(storage, tenant, config, storage_url,
                                   bundle=bundle, local_revision=local_revision)
        turns = sorted(bundle.turns, key=lambda turn: (turn.sequence, turn.turn_id))
        if config["scope"] in {"turn_window", "followup_text"}:
            index = next(i for i, turn in enumerate(turns) if turn.turn_id == config["turnId"])
            turns = turns[index - 1:index + 1]
        elif config["scope"] == "tool_action":
            selected = next(event for event in bundle.events if event.event_id == config["eventId"])
            turns = [turn for turn in turns if turn.turn_id == selected.turn_id]
        selected_ids = {turn.turn_id for turn in turns}
        selected_events = [event for event in bundle.events if event.turn_id in selected_ids]
        if len(turns) > _MAX_TURNS or len(selected_events) > _MAX_EVENTS:
            return _attach_pairs(evidence, dict(_NO_PAIRS), {"request", "response"})
        turn_rows = [{"runId": turn.run_id, "turnId": turn.turn_id, "sequence": turn.sequence,
                      "startedAt": turn.started_at.isoformat(),
                      "endedAt": turn.ended_at.isoformat() if turn.ended_at else None,
                      "request": turn.user_request_redacted, "response": turn.final_response_redacted,
                      "requestState": turn.request_state.value, "responseState": turn.response_state.value,
                      "requestTruncated": turn.request_truncated, "responseTruncated": turn.response_truncated}
                     for turn in turns]
        event_rows = [{"runId": bundle.run.run_id, "turnId": event.turn_id,
                       "eventId": event.event_id, "occurredAt": event.occurred_at.isoformat(),
                       "type": event.event_type.value, "status": event.status.value,
                       "attributes": disclosed_event_attributes(event.attributes),
                       "provenance": event.provenance}
                      for event in selected_events]
        paired = paired_evidence(turn_rows, event_rows, source_kind=bundle.session.source_kind,
                                 session_id=bundle.session.source_session_id)
        if config["scope"] == "followup_text":
            # This mode deliberately reviews no later response or typed events.
            # Preserve that narrow egress contract in the additional disclosure.
            for pair in paired["turnPairs"]:
                pair["sourceReferences"] = []
                pair["captureProvenance"] = []
                pair["originalEvidenceReferences"] = []
                pair["laterEvidenceReferences"] = []
                pair["unplacedEvidenceReferences"] = []
            paired["turnPairs"][-1]["response"] = None
            paired["documents"] = {}
        required = {"request"} if config["scope"] in {"tool_action", "followup_text"} else {"request", "response"}
        evidence = _attach_pairs(evidence, paired, required)
    required = {"request", "response"}
    if config.get("evaluationMode") == "historical_audit":
        # Canonical stored policies suffice for key-free freshness reads. The
        # optional evaluator validates them at preview, before any judge call.
        for dimension in config["rubric"]["dimensions"]:
            required.update(dimension["requiredEvidence"])
    elif any(d.get("requiresContext") is True for d in config.get("rubric", {}).get("dimensions", [])):
        required.add("sources")
    else:
        return evidence
    return _attach_pairs(evidence, {key: evidence[key] for key in (
        "turnPairs", "documents", "chronologyCertain",
    )}, required)


def historical_audit_allowed(evidence: dict) -> bool:
    """Audits also accept partial event capture or uncertain order, which they disclose.

    Turn text must still be complete, and suitability must not be unavailable.
    Size and shape limits are checked at preview, against the selected rubric.
    """
    reason = evidence["unavailableReason"]
    return (reason in {None, "source_capture_partial", "event_order_uncertain"}
            and bool(evidence.get("turnPairs"))
            and evidence["suitability"]["state"] != "unavailable")


def _plan(storage, tenant: str, config: dict[str, Any],
          storage_url: str | None = None) -> tuple[dict[str, Any], Any]:
    mode = config.get("evaluationMode", "original_response")
    if mode not in {"original_response", "historical_audit"}:
        raise ValueError("unsupported scoped evaluation mode")
    audit = mode == "historical_audit"
    if audit and config.get("scope") not in {"session", "run", "turn_window"}:
        raise ValueError("historical audit requires session/run/turn-window evidence")
    if audit:
        config = {**config, "maxOutputTokens": config.get("maxOutputTokens", 4096)}
    if config.get("scope") == "followup_text":
        if config.get("rubric") is not None:
            raise ValueError("text-only follow-up review uses a fixed criterion")
        config = {**config, "rubric": _FOLLOWUP_TEXT_RUBRIC}
    provider, model, max_calls, max_output, rubric = _validated_config({
        **config, "unit": "trace", "maxCalls": 1,
    })
    if max_calls != 1 or config.get("toolEvidence") is not None:
        raise ValueError("scoped evaluation supports one bounded call")
    policy = None
    if audit:
        from verdict_eval import historical_audit

        if provider == "jev":
            raise ValueError("historical audit requires a structured provider")
        policy = historical_audit.policies(config)
    selection = {key: config[key] for key in (
        "scope", "runId", "turnId", "eventId", "sessionId", "sourceKind",
    ) if key in config}
    selection["evaluationMode"] = mode
    selection["rubric"] = {"name": rubric.name, "version": rubric.version, "dimensions": [
        {"name": d.name, "description": d.description, "requiresContext": d.requires_context,
         **(policy[d.name] if policy else {})} for d in rubric.dimensions
    ]}
    # Caller extras and dynamic map keys never cross the control-store boundary.
    _safe_json({"selection": selection})
    evidence = _projection(storage, tenant, selection, storage_url)
    identity = _judge(_IdentityOnlyProvider(provider), model, rubric, max_output,
                      skip_context_dependent_when_missing=False).evaluator_identity(None)
    audit_identity = None
    if audit:
        audit_identity = [historical_audit.VERSION, historical_audit.PROMPT_FINGERPRINT, policy]
    destination, destination_binding = _destination(provider, identity)
    identity_fp = hashlib.sha256(_canonical([
        _PROMPT_VERSION, config["scope"], identity["evaluator_fingerprint"], destination_binding, audit_identity,
    ])).hexdigest()
    fingerprint = hashlib.sha256(_canonical([
        tenant, evidence["evidenceFingerprint"], identity_fp,
    ])).hexdigest()
    eligible = (historical_audit_allowed(evidence) if audit else
                evidence["coverage"] == "complete" and evidence["suitability"]["state"] != "unavailable")
    reason = evidence["unavailableReason"]
    if evidence["suitability"]["state"] == "unavailable" and reason is None:
        reason = "required_evidence_unavailable"
    if audit and not historical_audit.shape_available(evidence, policy, max_output):
        eligible, reason = False, "audit_shape_limit"
    elif audit and len(historical_audit.messages(evidence, rubric, policy)[1]["content"].encode()) > 8_000:
        eligible, reason = False, "evidence_limit"
    input_estimate = math.ceil((len(evidence["query"]) + len(evidence["response"]) +
                                sum(len(d.description) for d in rubric.dimensions)) / 4) + 400
    if audit:
        input_estimate = math.ceil(sum(len(m["content"].encode()) for m in
                                       historical_audit.messages(evidence, rubric, policy)) / 4)
    output_max = _output_maximum(provider, model, max_output, 1) if eligible else 0
    preview = {
        "scope": config["scope"], "provider": provider, "model": model,
        "rubric": _rubric_summary(rubric, identity), "evaluatorFingerprint": identity_fp,
        "promptVersion": _PROMPT_VERSION, "planFingerprint": fingerprint,
        "plannedCalls": 1 if eligible else 0, "coverage": evidence["coverage"],
        "unavailableReason": reason, "evaluationMode": mode,
        "suitability": evidence["suitability"], "requestedOutputTokens": max_output,
        "auditPolicy": policy if audit else None,
        "evidenceSelection": selection,
        "evidencePreview": evidence, "estimatedInputTokens": input_estimate if eligible else 0,
        "maximumOutputTokens": output_max,
        "estimatedMaximumCostUsd": compute_cost_usd(model, input_estimate, output_max)
        if eligible and provider != "jev" else None,
        "destination": destination, "externalEgressRequired": True,
    }
    preview["upperStaticListPriceUsd"] = _upper_static_cost(preview, rubric)
    preview["scheduledUpperStaticListPriceUsd"] = (
        preview["upperStaticListPriceUsd"] if provider in {"anthropic", "openai"} else None
    )
    if preview["plannedCalls"] == 1:
        try:
            _persistable_result({**_result_metadata(preview, rubric), "status": "completed",
                                 "error": "invalid_judge_output", "dimensions": []})
        except ValueError:
            preview.update(plannedCalls=0, unavailableReason="result_limit", maximumOutputTokens=0,
                           estimatedInputTokens=0, estimatedMaximumCostUsd=None,
                           upperStaticListPriceUsd=None, scheduledUpperStaticListPriceUsd=None)
    return preview, rubric


def preview_scoped(storage, *, tenant: str, config: dict[str, Any],
                   storage_url: str | None = None) -> dict[str, Any]:
    preview = _plan(storage, tenant, config, storage_url)[0]
    if storage_url is not None and preview["plannedCalls"] == 1:
        saved = _saved_result(ControlStore(storage_url), tenant, preview["planFingerprint"])
        if saved is not None:
            preview["savedResult"] = saved
    return preview


def _saved_result(store: ControlStore, tenant: str, plan_fingerprint: str) -> dict[str, Any] | None:
    for status in ("completed", "error"):
        result_id = hashlib.sha256(_canonical([tenant, plan_fingerprint, status])).hexdigest()
        history = store.history(tenant, "scoped_evaluation", result_id)
        if history:
            return {"resultId": result_id, **_result_view(history[0]["payload"])}
    return None


def _result_view(payload: dict) -> dict:
    """Keep response maps compatible while storage uses bounded typed lists."""
    result = dict(payload)
    if "criteria" in result:
        result["criterionDescriptions"] = {item["name"]: item["description"] for item in result["criteria"]}
    if "auditPolicies" in result:
        result["auditPolicy"] = ({item["dimension"]: {key: value for key, value in item.items()
                                                    if key != "dimension"}
                                  for item in result["auditPolicies"]} or None)
    return result


def _persistable_result(payload: dict) -> dict:
    # Serialization makes the bounded generated payload a JSON tree before
    # redaction; borrowed policy lists must not become shared Python graphs.
    safe = redact_structure(json.loads(_safe_json(payload)))
    if not isinstance(safe, dict):
        raise ValueError("scoped result redaction failed")
    if safe.get("evidenceSelection") != payload.get("evidenceSelection"):
        # A redacted identifier no longer selects the evidence, so freshness
        # would always read as stale. Keep no selection: freshness is unknown.
        safe["evidenceSelection"] = None
    _safe_json(safe)
    return safe


def _result_metadata(preview: dict, rubric) -> dict:
    """One durable envelope, checked before egress and reused for terminal results."""
    evidence = preview["evidencePreview"]
    return {
        "scope": evidence["scope"], "runId": evidence["runId"],
        "sessionId": evidence.get("sessionId"), "sourceKind": evidence.get("sourceKind"),
        "runIds": evidence.get("runIds"),
        **({"turnId": evidence["turnId"]}
           if evidence["scope"] in {"turn_window", "followup_text"} else {}),
        "eventId": evidence["eventId"], "coverage": evidence["coverage"],
        "evidenceFingerprint": evidence["evidenceFingerprint"],
        "evidenceReferences": evidence["evidenceReferences"],
        "evaluatorFingerprint": preview["evaluatorFingerprint"],
        "provider": preview["provider"], "model": preview["model"],
        "rubric": preview["rubric"], "promptVersion": _PROMPT_VERSION,
        "criteria": [{"name": d.name, "description": d.description} for d in rubric.dimensions],
        "evaluationMode": preview["evaluationMode"],
        "auditPolicies": [{"dimension": name, **definition}
                          for name, definition in (preview["auditPolicy"] or {}).items()],
        "outputAllowance": {"requested": preview["requestedOutputTokens"],
                            "maximum": preview["maximumOutputTokens"]},
        "suitability": preview["suitability"], "evidenceCurrent": False,
        "evaluatedAt": "2000-01-01T00:00:00.000000+00:00",
        "evidenceSelection": preview["evidenceSelection"],
    }


def execute_scoped(storage, *, storage_url: str, tenant: str, config: dict[str, Any],
                   confirm_external_egress: bool, provider=None) -> dict[str, Any]:
    if confirm_external_egress is not True:
        raise ValueError("external judge egress was not confirmed")
    with _LOCK, _cross_process_scoped_guard(storage_url):
        return _execute_scoped_locked(storage, storage_url=storage_url, tenant=tenant,
                                      config=config, provider=provider)


def _execute_scoped_locked(storage, *, storage_url: str, tenant: str,
                           config: dict[str, Any], provider=None,
                           planned: tuple[dict[str, Any], Any] | None = None) -> dict[str, Any]:
    """Execute only while the caller holds both scoped locks."""
    preview, rubric = planned if planned is not None else _plan(storage, tenant, config, storage_url)
    if preview["plannedCalls"] != 1 or config.get("planFingerprint") != preview["planFingerprint"]:
        raise ValueError("scoped preview is no longer current")
    store = ControlStore(storage_url)
    saved = _saved_result(store, tenant, preview["planFingerprint"])
    if saved is not None:
        return saved
    if planned is None:
        cap = config.get("maxEstimatedUsd")
        upper_cost = preview["upperStaticListPriceUsd"]
        if not within_static_spend_limit(upper_cost, cap):
            raise ValueError("direct evaluation exceeds estimated-spend limit")
    provider_name = preview["provider"]
    judge = _judge(provider or _provider(provider_name), preview["model"], rubric,
                   preview["requestedOutputTokens"], skip_context_dependent_when_missing=False)
    base_identity = judge.evaluator_identity(None)
    expected = _judge(_IdentityOnlyProvider(provider_name), preview["model"], rubric,
                      preview["requestedOutputTokens"], skip_context_dependent_when_missing=False).evaluator_identity(None)
    if base_identity["evaluator_fingerprint"] != expected["evaluator_fingerprint"]:
        raise ValueError("judge provider behavior changed after preview")
    evidence = preview["evidencePreview"]
    audit_result = {}
    try:
        if preview["evaluationMode"] == "historical_audit":
            from verdict_eval import historical_audit

            audit_result = historical_audit.execute(
                judge.provider, model=preview["model"], max_tokens=preview["requestedOutputTokens"],
                evidence=evidence, rubric=rubric, policy=preview["auditPolicy"],
            )
            scores = []
        else:
            scores = judge.score(query=evidence["query"], response=evidence["response"])
        allowed_refs = set(evidence["evidenceReferences"])
        dimensions = audit_result.get("dimensions", [])
        for score in scores:
            references = [
                (kind, identifier) for kind, identifier in _REF.findall(score.reasoning)
                if kind in {"turn", "event"}
            ]
            cited = [f"{kind}:{identifier}" for kind, identifier in references]
            if any(f"{kind}:{identifier}" not in allowed_refs
                   for kind, identifier in references):
                raise ValueError("judge cited evidence outside the selected scope")
            dimensions.append({
                "name": score.name, "verdict": score.verdict.value,
                "explanation": (redact_structure(score.reasoning) or "")[:2_000],
                "evidenceReferences": cited,
                "citationStatus": "linked" if cited else "uncited_opinion",
                "supportStatus": "unverified",
            })
        status, error = "completed", None
    except Exception as exc:
        status, error, dimensions = "error", type(exc).__name__, []
    # An append or corrected import during the provider call invalidates the
    # judgment even when the user approved the pre-call projection.
    try:
        still_current = _plan(storage, tenant, config, storage_url)[0]["planFingerprint"] == preview["planFingerprint"]
    except (KeyError, TypeError, ValueError, UnicodeError):
        still_current = False
    if not still_current:
        status, error, dimensions = "error", "evidence_changed_during_evaluation", []
    result = {
        **_result_metadata(preview, rubric),
        "status": status, "error": error, "dimensions": dimensions,
        "evidenceCurrent": still_current,
        "evaluatedAt": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        **({key: audit_result[key] for key in (
            "turnAssessments", "historicalFindings", "holisticAssessments", "citationValidation",
        )} if "turnAssessments" in audit_result else {}),
        **({"turnPairs": evidence["turnPairs"]} if "turnAssessments" in audit_result else {}),
    }
    try:
        safe = _persistable_result(result)
    except ValueError:
        # Structurally valid provider output can exceed durable payload limits.
        # Persist one terminal error using the preflighted metadata only.
        status = "error"
        safe = _persistable_result({**_result_metadata(preview, rubric),
                                    "evidenceCurrent": still_current,
                                    "evaluatedAt": result["evaluatedAt"],
                                    "status": "error", "error": "invalid_judge_output", "dimensions": []})
    document_id = hashlib.sha256(_canonical([
        tenant, preview["planFingerprint"], status,
    ])).hexdigest()
    existing = store.history(tenant, "scoped_evaluation", document_id)
    if existing:
        return {"resultId": document_id, **_result_view(existing[0]["payload"])}
    store.append(tenant, kind="scoped_evaluation", document_id=document_id,
                 state=status, payload=safe, expected_revision=None)
    return {"resultId": document_id, **_result_view(safe)}


def _upper_static_cost(preview: dict[str, Any], rubric) -> float | None:
    """Conservative static list-price planning bound, never an invoice guarantee."""
    if preview["plannedCalls"] != 1 or not _uses_public_list_price(preview["provider"]):
        return None
    from verdict_eval.judge import SYSTEM_PROMPT, _user_prompt

    evidence = preview["evidencePreview"]
    if preview.get("evaluationMode") == "historical_audit":
        from verdict_eval.historical_audit import messages

        request_messages = messages(evidence, rubric, preview["auditPolicy"])
        input_bound = sum(len(message["content"].encode()) for message in request_messages) + 1_024
        return compute_exact_list_price_usd(preview["model"], input_bound, preview["maximumOutputTokens"])
    prompt = _user_prompt(evidence["query"], evidence["response"], None, rubric)
    # A byte may become at most one text token in supported provider tokenizers;
    # the extra envelope allowance covers message framing and SDK metadata.
    input_bound = len(SYSTEM_PROMPT.encode()) + len(prompt.encode()) + 1_024
    return compute_exact_list_price_usd(preview["model"], input_bound, preview["maximumOutputTokens"])


def _scheduled_config(payload: dict[str, Any]) -> dict[str, Any]:
    stored = payload.get("config")
    if not isinstance(stored, dict):
        raise ValueError("invalid scheduled evaluation configuration")
    return {**stored, "maxOutputTokens": stored.get("maxOutput", 256)}


def schedule_scoped_once(storage, *, storage_url: str, tenant: str,
                         config: dict[str, Any], max_estimated_usd: float,
                         confirm_persistent_egress: bool) -> dict[str, Any]:
    if confirm_persistent_egress is not True:
        raise ValueError("persistent external judge egress was not confirmed")
    preview, rubric = _plan(storage, tenant, config, storage_url)
    if (preview["provider"] not in {"anthropic", "openai"}
            or preview["plannedCalls"] != 1
            or config.get("planFingerprint") != preview["planFingerprint"]):
        raise ValueError("scheduled evaluation requires a current bounded preview")
    upper_cost = _upper_static_cost(preview, rubric)
    if not within_static_spend_limit(upper_cost, max_estimated_usd):
        raise ValueError("scheduled evaluation exceeds estimated-spend limit")
    stored_config = {**preview["evidenceSelection"], "provider": preview["provider"], "model": preview["model"]}
    if stored_config["scope"] == "followup_text":
        stored_config.pop("rubric")
    stored_config["maxOutput"] = preview["requestedOutputTokens"]
    document_id = secrets.token_hex(16)
    payload = {
        "config": stored_config,
        "planFingerprint": preview["planFingerprint"],
        "destination": preview["destination"],
        "maxCalls": 1,
        "maxEstimatedUsd": float(max_estimated_usd),
        "upperStaticListPriceUsd": upper_cost,
    }
    entry = ControlStore(storage_url).append(
        tenant, kind="scheduled_evaluation", document_id=document_id,
        state="pending", payload=payload, expected_revision=None,
    )
    return {"jobId": document_id, "state": "pending", "revision": entry["revision"],
            "plannedCalls": 1, "destination": preview["destination"],
            "upperStaticListPriceUsd": upper_cost,
            "maxEstimatedUsd": float(max_estimated_usd)}


def cancel_scheduled_once(storage_url: str, *, tenant: str, job_id: str,
                          expected_revision: int) -> dict[str, Any]:
    store = ControlStore(storage_url)
    current = store.history(tenant, "scheduled_evaluation", job_id)
    if not current or current[0]["state"] != "pending":
        raise ValueError("scheduled evaluation is not pending")
    payload = dict(current[0]["payload"])
    payload["outcome"] = "cancelled_by_user"
    entry = store.append(tenant, kind="scheduled_evaluation", document_id=job_id,
                         state="cancelled", payload=payload,
                         expected_revision=expected_revision)
    return {"jobId": job_id, "state": entry["state"], "revision": entry["revision"]}


def _validated_scheduled_plan(storage, storage_url: str, tenant: str,
                             config: dict[str, Any], payload: dict[str, Any]):
    preview, rubric = _plan(storage, tenant, config, storage_url)
    upper_cost = _upper_static_cost(preview, rubric)
    if not (
        preview["plannedCalls"] == 1
        and preview["planFingerprint"] == payload.get("planFingerprint")
        and preview["destination"] == payload.get("destination")
        and preview["provider"] in {"anthropic", "openai"}
        and payload.get("maxCalls") == 1
        and within_static_spend_limit(upper_cost, payload.get("maxEstimatedUsd"))
    ):
        raise ValueError("scheduled evidence or authorization is stale")
    return preview, rubric


def _scheduled_storage(storage_url: str):
    if is_postgres_storage(storage_url):
        from verdict.storage.postgres import PostgresStorage

        return PostgresStorage(storage_url)
    from verdict.client import _resolve_storage

    return _resolve_storage(storage_url)


def run_scheduled_once(storage_url: str, *, tenant: str, provider=None) -> dict[str, Any]:
    """Consume at most one pending approval before any network call."""
    store = ControlStore(storage_url)
    pending_jobs = [item for item in store.list_current(tenant)
                    if item["kind"] == "scheduled_evaluation" and item["state"] == "pending"]
    if not pending_jobs:
        return {"status": "idle"}
    pending = pending_jobs[0]
    if provider is None:
        for candidate in pending_jobs:
            stored = candidate["payload"].get("config")
            requested = stored.get("provider") if isinstance(stored, dict) else None
            key_name = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(requested)
            if key_name and os.environ.get(key_name):
                pending = candidate
                break
    job_id = str(pending["documentId"])
    payload = dict(pending["payload"])
    config = _scheduled_config(payload)

    def cancel_before_egress(outcome: str, error: str | None = None) -> dict[str, Any]:
        cancelled_payload = {**payload, "outcome": outcome}
        if error is not None:
            cancelled_payload["error"] = error
        try:
            cancelled = store.append(
                tenant, kind="scheduled_evaluation", document_id=job_id,
                state="cancelled", payload=cancelled_payload,
                expected_revision=pending["revision"],
            )
        except ValueError:
            return {"jobId": job_id, "status": "approval_changed"}
        return {"jobId": job_id, "status": cancelled["state"],
                "reason": outcome, "revision": cancelled["revision"]}

    storage = None
    try:
        storage = _scheduled_storage(storage_url)
        preview, rubric = _validated_scheduled_plan(
            storage, storage_url, tenant, config, payload,
        )
    except (KeyError, TypeError, ValueError, UnicodeError):
        return cancel_before_egress("stale_or_invalid")
    except ImportError as exc:
        return cancel_before_egress("preflight_failed", type(exc).__name__)
    finally:
        if storage is not None:
            storage.close()
    if provider is None:
        key_name = "ANTHROPIC_API_KEY" if preview["provider"] == "anthropic" else "OPENAI_API_KEY"
        if not os.environ.get(key_name):
            return {"jobId": job_id, "status": "waiting_provider"}
    adapter = provider
    try:
        if adapter is None:
            from verdict_eval.providers import AnthropicAdapter, OpenAIAdapter

            adapter = (AnthropicAdapter(max_retries=1, sdk_max_retries=0)
                       if preview["provider"] == "anthropic"
                       else OpenAIAdapter(max_retries=1, sdk_max_retries=0))
    except Exception as exc:
        client = getattr(adapter, "_client", None)
        if client is not None and hasattr(client, "close"):
            client.close()
        return cancel_before_egress("preflight_failed", type(exc).__name__)
    storage = None
    try:
        with _LOCK, _cross_process_scoped_guard(storage_url):
            storage = _scheduled_storage(storage_url)
            try:
                preview, rubric = _validated_scheduled_plan(
                    storage, storage_url, tenant, config, payload,
                )
            except (KeyError, TypeError, ValueError, UnicodeError):
                return cancel_before_egress("stale_or_invalid")
            except ImportError as exc:
                return cancel_before_egress("preflight_failed", type(exc).__name__)
            if provider is None and not os.environ.get(key_name):
                return {"jobId": job_id, "status": "waiting_provider"}
            try:
                actual = _judge(adapter, preview["model"], rubric, config["maxOutputTokens"], skip_context_dependent_when_missing=False)
                expected = _judge(_IdentityOnlyProvider(preview["provider"]),
                                  preview["model"], rubric, config["maxOutputTokens"], skip_context_dependent_when_missing=False)
                if actual.evaluator_identity(None)["evaluator_fingerprint"] != expected.evaluator_identity(None)["evaluator_fingerprint"]:
                    raise ValueError("judge provider behavior changed after preview")
            except Exception as exc:
                return cancel_before_egress("preflight_failed", type(exc).__name__)
            try:
                spent = store.append(
                    tenant, kind="scheduled_evaluation", document_id=job_id,
                    state="spent", payload={**payload, "outcome": "attempt_reserved"},
                    expected_revision=pending["revision"],
                )
            except ValueError:
                return {"jobId": job_id, "status": "approval_changed"}
            try:
                result = _execute_scoped_locked(
                    storage, storage_url=storage_url, tenant=tenant,
                    config={**config, "planFingerprint": payload["planFingerprint"]},
                    provider=adapter, planned=(preview, rubric),
                )
                recorded = store.append(
                    tenant, kind="scheduled_evaluation", document_id=job_id,
                    state="spent", payload={**payload, "outcome": result["status"],
                                            "resultId": result["resultId"],
                                            "error": result.get("error")},
                    expected_revision=spent["revision"],
                )
                return {"jobId": job_id, "status": result["status"],
                        "resultId": result["resultId"], "revision": recorded["revision"]}
            except Exception as exc:
                return {"jobId": job_id, "status": "spent_outcome_unknown",
                        "error": type(exc).__name__, "revision": spent["revision"]}
    except UnsupportedScopedPlatform as exc:
        return {"jobId": job_id, "status": "unsupported_platform", "error": str(exc)}
    finally:
        if storage is not None:
            storage.close()
        client = getattr(adapter, "_client", None)
        if client is not None and hasattr(client, "close"):
            client.close()


def register_scoped_routes(app, setup: SetupRoutes) -> None:
    @app.get("/api/scoped/result/{result_id}")
    def saved_result(result_id: str, request: Request):
        if not setup.request_matches_tenant(request):
            return JSONResponse({"error": "scoped result unavailable"}, status_code=403)
        try:
            history = ControlStore(setup.storage_url).history(
                setup.tenant_id, "scoped_evaluation", result_id,
            )
        except ValueError:
            return JSONResponse({"error": "scoped result unavailable"}, status_code=404)
        if not history:
            return JSONResponse({"error": "scoped result unavailable"}, status_code=404)
        payload = history[0]["payload"]
        evidence_status = "unknown"
        if payload.get("evidenceSelection"):
            storage = setup.writable_storage()
            try:
                current = _projection(storage, setup.tenant_id, payload["evidenceSelection"], setup.storage_url)
                evidence_status = ("current" if current["evidenceFingerprint"] == payload["evidenceFingerprint"]
                                   else "stale")
            except (KeyError, ValueError, TypeError):
                evidence_status = "stale"
            finally:
                storage.close()
        return {"resultId": result_id, **_result_view(payload), "evidenceStatus": evidence_status}

    @app.get("/api/scoped/scheduled")
    def scheduled_list(request: Request):
        if not setup.request_matches_tenant(request):
            return JSONResponse({"error": "scheduled evaluations unavailable"}, status_code=403)
        return {"jobs": [
            {"jobId": item["documentId"], "state": item["state"],
             "revision": item["revision"], "createdAt": item["createdAt"],
             "scope": item["payload"].get("config", {}).get("scope"),
             "planFingerprint": item["payload"].get("planFingerprint"),
             "destination": item["payload"].get("destination"),
             "maxEstimatedUsd": item["payload"].get("maxEstimatedUsd"),
             "outcome": item["payload"].get("outcome"),
             "resultId": item["payload"].get("resultId"),
             "error": item["payload"].get("error")}
            for item in sorted(
                (row for row in ControlStore(setup.storage_url).list_current(setup.tenant_id)
                 if row["kind"] == "scheduled_evaluation"),
                key=lambda row: (str(row["createdAt"]), str(row["documentId"])), reverse=True,
            )
        ][:50]}

    @app.post("/api/scoped/schedule")
    def scheduled_create(request: Request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse({"error": "evaluation authorization required"}, status_code=403)
        storage = setup.writable_storage()
        try:
            return schedule_scoped_once(
                storage, storage_url=setup.storage_url, tenant=setup.tenant_id,
                config=payload, max_estimated_usd=payload.get("maxEstimatedUsd"),
                confirm_persistent_egress=payload.get("confirmPersistentExternalEgress") is True,
            )
        except ImportError:
            return JSONResponse({"error": "optional evaluation package unavailable"}, status_code=503)
        except (KeyError, TypeError, ValueError, UnicodeError):
            return JSONResponse({"error": "invalid, stale, or duplicate scheduled approval"}, status_code=409)
        finally:
            storage.close()

    @app.post("/api/scoped/scheduled/{job_id}/cancel")
    def scheduled_cancel(job_id: str, request: Request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse({"error": "evaluation authorization required"}, status_code=403)
        try:
            return cancel_scheduled_once(
                setup.storage_url, tenant=setup.tenant_id, job_id=job_id,
                expected_revision=payload.get("expectedRevision"),
            )
        except (KeyError, TypeError, ValueError, UnicodeError):
            return JSONResponse({"error": "scheduled approval is no longer pending"}, status_code=409)

    @app.post("/api/scoped/preview")
    def preview_route(request: Request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse({"error": "evaluation authorization required"}, status_code=403)
        storage = setup.writable_storage()
        try:
            return preview_scoped(storage, tenant=setup.tenant_id, config=payload,
                                  storage_url=setup.storage_url)
        except ImportError:
            return JSONResponse({"error": "optional evaluation package unavailable"}, status_code=503)
        except (KeyError, TypeError, ValueError, UnicodeError):
            return JSONResponse({"error": "invalid scoped preview"}, status_code=400)
        finally:
            storage.close()

    @app.post("/api/scoped/run")
    def run_route(request: Request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse({"error": "evaluation authorization required"}, status_code=403)
        storage = setup.writable_storage()
        try:
            return execute_scoped(storage, storage_url=setup.storage_url,
                                  tenant=setup.tenant_id, config=payload,
                                  confirm_external_egress=payload.get("confirmExternalEgress") is True)
        except ImportError:
            return JSONResponse({"error": "optional evaluation package unavailable"}, status_code=503)
        except UnsupportedScopedPlatform as exc:
            return JSONResponse({"error": str(exc)}, status_code=501)
        except (KeyError, TypeError, ValueError, UnicodeError):
            return JSONResponse({"error": "invalid or stale scoped evaluation"}, status_code=400)
        finally:
            storage.close()
