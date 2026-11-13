"""Producer-declared suitability and source-qualified pairs for scoped review."""
from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote

from verdict.redaction import redact_structure


def identity_is_safe(value):
    return isinstance(value, str) and redact_structure(value) == value


def normalize_scoped_rubric(value, *, historical=False):
    """One safe rubric for planning, egress, identity, scheduling and replay."""
    if not isinstance(value, dict) or not isinstance(value.get("dimensions"), list):
        raise ValueError("rubric dimensions must be a list")
    dimensions = []
    for dimension in value["dimensions"]:
        if not isinstance(dimension, dict):
            raise ValueError("invalid rubric dimension")
        description = dimension.get("description")
        item = {"name": dimension.get("name"),
                "description": description.strip() if isinstance(description, str) else description,
                "requiresContext": dimension.get("requiresContext") is True}
        if historical:
            item.update(aggregation=dimension.get("aggregation"),
                        evidenceAuthority=dimension.get("evidenceAuthority"))
        dimensions.append(item)
    safe = redact_structure({"name": value.get("name"), "version": value.get("version"),
                             "dimensions": dimensions})
    if not isinstance(safe, dict) or not isinstance(safe.get("dimensions"), list):
        raise ValueError("rubric redaction failed")
    return safe


def _ref(kind, *parts):
    return kind + ":" + "/".join(quote(str(part), safe="") for part in parts)


def paired_evidence(turns, events, *, source_kind, session_id):
    """Use recorded ordering, with explicit uncertainty for overlaps and missing times."""
    identifiers = [source_kind, session_id]
    identifiers.extend(part for t in turns for part in (t["runId"], t["turnId"]))
    identifiers.extend(part for e in events for part in (e["runId"], e["eventId"]))
    if not all(identity_is_safe(value) for value in identifiers):
        return {"pairs": [], "sources": [], "chronologyKnown": False,
                "unavailableReason": "identifier_requires_redaction"}
    pairs, sources = [], []
    for event in events:
        if event["type"] not in {"context", "tool_result"}:
            continue
        attrs = event["attributes"]
        content = attrs.get("text", attrs.get("value", attrs.get("result")))
        available = (attrs.get("available", True) is not False
                     and attrs.get("is_error", False) is not True
                     and event.get("status", "unknown") not in {"failed", "timed_out", "cancelled"}
                     and content is not None
                     and not (isinstance(content, str) and not content.strip())
                     and not (isinstance(content, (dict, list)) and not content))
        if content is not None and not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        sources.append({
            "reference": _ref("event", source_kind, session_id, event["runId"], event["eventId"]),
            "runId": event["runId"], "turnId": event["turnId"],
            "occurredAt": event["occurredAt"], "content": content,
            "contentKind": attrs.get("content_kind", "unknown"),
            "available": available, "status": event.get("status", "unknown"),
            "isError": attrs.get("is_error", False),
        })
    chronological = True
    prior_end = None
    for turn in turns:
        start, end = turn["startedAt"], turn["endedAt"]
        valid = bool(start and end and _time(end) >= _time(start))
        if not valid or (prior_end is not None and _time(start) <= _time(prior_end)):
            chronological = False
        if end:
            prior_end = end
        pairs.append({
            "reference": _ref("turn", source_kind, session_id, turn["runId"], turn["turnId"]),
            "identity": {"sourceKind": source_kind, "sessionId": session_id,
                         "runId": turn["runId"], "turnId": turn["turnId"]},
            "sequence": turn["sequence"], "startedAt": start, "endedAt": end,
            "request": turn["request"], "response": turn["response"],
            "requestContentKind": turn.get("requestContentKind", "unknown"),
            "responseContentKind": turn.get("responseContentKind", "unknown"),
        })
    for pair in pairs:
        end = pair["endedAt"]
        pair["originalEvidenceReferences"] = [pair["reference"]] + [
            p["reference"] for p in pairs if p != pair and p["identity"]["runId"] == pair["identity"]["runId"] and end and p["endedAt"]
            and _time(p["endedAt"]) < _time(pair["startedAt"])
        ] + [s["reference"] for s in sources if s["runId"] == pair["identity"]["runId"] and end and s["occurredAt"]
             and _time(s["occurredAt"]) <= _time(end)]
    safe = redact_structure({"pairs": pairs, "sources": sources, "chronologyKnown": chronological,
                             "timeBasis": "recorded timestamps; ordering is not causal proof"})
    return safe


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def suitability(paired, coverage, *, require_sources=False):
    limitations, unavailable = [], None
    if coverage != "complete":
        unavailable = "structural_evidence_partial"
    for pair in paired["pairs"]:
        for field in ("requestContentKind", "responseContentKind"):
            kind = pair[field]
            if kind == "unknown":
                limitations.append(f"{pair['reference']}:{field}:unknown")
            elif kind != "substantive":
                unavailable = "substantive_task_or_output_unavailable"
    usable = [s for s in paired["sources"] if s["available"]
              and s["contentKind"] in {"unknown", "substantive"}]
    if not usable:
        limitations.append("substantive_source_evidence_unavailable")
        if require_sources:
            unavailable = "required_source_evidence_unavailable"
    if any(s["contentKind"] == "unknown" for s in usable):
        limitations.append("source_content_provenance_unknown")
    if not paired["chronologyKnown"]:
        limitations.append("chronology_uncertain")
    unavailable = paired.get("unavailableReason") or unavailable
    return {"state": "unavailable" if unavailable else "limited" if limitations else "available",
            "unavailableReason": unavailable, "limitations": limitations,
            "taskOutputProvenance": [{"reference": p["reference"],
                                      "request": p["requestContentKind"],
                                      "response": p["responseContentKind"]} for p in paired["pairs"]],
            "sources": [{k: s[k] for k in ("reference", "contentKind", "available", "status", "isError")}
                        for s in paired["sources"]]}
