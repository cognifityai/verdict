"""Source-qualified pairs and explicit capture suitability for scoped review.

Producers may use the existing ``evaluation_capture`` context event to declare
request/response/sources roles. Absence of that event is deliberately unknown.
"""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote

_ROLES = {"substantive", "summary", "count", "reference", "missing", "unknown"}
_NONCONTENT = {"summary", "count", "reference", "missing", "conflicting"}


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def paired_evidence(turns: list[dict], events: list[dict], *, source_kind: str,
                    session_id: str) -> dict:
    """Project only recorded evidence, preserving provenance and time limits."""
    prefix = "/".join(quote(value, safe="") for value in (source_kind, session_id))
    pairs, documents = [], {}
    run_starts = {}
    for turn in sorted(turns, key=lambda t: (t["runId"], t["sequence"], t["turnId"])):
        start = _time(turn["startedAt"])
        run_starts.setdefault(turn["runId"], start.timestamp() if start else float("inf"))
    ordered = sorted(turns, key=lambda t: (
        run_starts[t["runId"]], t["runId"], t["sequence"], t["turnId"],
    ))
    chronology = True
    previous_end = None
    for index, turn in enumerate(ordered):
        key = f"{prefix}/{quote(turn['runId'], safe='')}/{quote(turn['turnId'], safe='')}"
        start, end = _time(turn["startedAt"]), _time(turn.get("endedAt"))
        if start is None or end is None or end < start or (index and (
                previous_end is None or start <= previous_end)):
            chronology = False
        previous_end = end
        selected = [event for event in events if event["runId"] == turn["runId"]
                    and event["turnId"] == turn["turnId"]]
        roles, provenance = {}, []
        for field in ("request", "response", "sources"):
            declarations = []
            for event in selected:
                attributes = event["attributes"]
                if event["type"] == "context" and attributes.get("name") == "evaluation_capture":
                    value = attributes.get("value")
                    if isinstance(value, dict) and field in value:
                        role = value[field]
                        declarations.append(role if isinstance(role, str) and role in _ROLES
                                            else "conflicting")
                        provenance.append({"eventId": event["eventId"], "field": field,
                                           "role": declarations[-1],
                                           "producerProvenance": event.get("provenance")})
            roles[field] = (declarations[0] if len(set(declarations)) == 1 else
                            "conflicting" if declarations else "unknown")
        for field in ("request", "response"):
            if (turn[f"{field}State"] != "present" or turn[f"{field}Truncated"]
                    or not isinstance(turn[field], str) or not turn[field].strip()):
                roles[field] = "missing"
            reference = f"{field}:{key}"
            documents[reference] = {
                "text": turn[field] or "", "turnReference": f"turn:{key}",
                "occurredAt": turn["startedAt"] if field == "request" else turn.get("endedAt"),
                "kind": field, "contentRole": roles[field],
            }
        for event in selected:
            event_key = f"{prefix}/{quote(turn['runId'], safe='')}/{quote(event['eventId'], safe='')}"
            attributes = event["attributes"]
            fields = ("text", "value") if event["type"] == "context" else ("result",)
            source_available = attributes.get("available") is not False and any(
                attributes.get(field) not in (None, "", {}, []) for field in fields
            )
            documents[f"event:{event_key}"] = {
                "text": json.dumps(event["attributes"], ensure_ascii=False, sort_keys=True),
                "turnReference": f"turn:{key}", "occurredAt": event["occurredAt"],
                "kind": ("capture_provenance" if event["type"] == "context"
                         and event["attributes"].get("name") == "evaluation_capture" else event["type"]),
                "contentRole": roles["sources"] if source_available else "missing",
            }
        source_refs = [ref for ref, doc in documents.items() if doc["turnReference"] == f"turn:{key}"
                       and doc["kind"] in {"context", "tool_result"}
                       and doc["contentRole"] != "missing"]
        if (roles["sources"] in {"substantive", "unknown"} and not source_refs
                and (roles["sources"] == "substantive" or any(
                    doc["turnReference"] == f"turn:{key}" and doc["kind"] in {"context", "tool_result"}
                    for doc in documents.values()))):
            roles["sources"] = "missing"
        pairs.append({
            "reference": f"turn:{key}", "sourceKind": source_kind, "sourceSessionId": session_id,
            "runId": turn["runId"], "turnId": turn["turnId"], "sequence": turn["sequence"],
            "startedAt": turn["startedAt"], "endedAt": turn.get("endedAt"),
            "request": turn["request"], "response": turn["response"],
            "requestReference": f"request:{key}", "responseReference": f"response:{key}",
            "sourceReferences": source_refs, "capture": roles, "captureProvenance": provenance,
        })
    indices = {pair["reference"]: index for index, pair in enumerate(pairs)}
    for index, pair in enumerate(pairs):
        end = _time(pair["endedAt"])
        original = []
        for ref, doc in documents.items():
            occurred = _time(doc["occurredAt"])
            if (indices[doc["turnReference"]] <= index and end is not None
                    and occurred is not None and occurred <= end):
                original.append(ref)
        pair["originalEvidenceReferences"] = original
        pair["laterEvidenceReferences"] = [ref for ref, doc in documents.items()
                                           if end is not None and _time(doc["occurredAt"]) is not None
                                           and _time(doc["occurredAt"]) > end]
        placed = set(original) | set(pair["laterEvidenceReferences"])
        pair["unplacedEvidenceReferences"] = [ref for ref in documents if ref not in placed]
        if pair["unplacedEvidenceReferences"]:
            chronology = False
    return {"turnPairs": pairs, "documents": documents, "chronologyCertain": chronology}


def suitability(paired: dict, required: set[str]) -> dict:
    """Known absent required content withholds; legacy provenance stays unknown."""
    missing, limitations = [], []
    turns = []
    for pair in paired["turnPairs"]:
        roles = pair["capture"]
        turns.append({"reference": pair["reference"], **roles,
                      "provenance": pair["captureProvenance"]})
        for field in required:
            if roles[field] in _NONCONTENT:
                missing.append({"reference": pair["reference"], "field": field, "role": roles[field]})
        if "unknown" in roles.values():
            limitations.append("Legacy or undeclared content provenance is unknown.")
    if not paired["chronologyCertain"]:
        limitations.append("Chronology is incomplete or overlapping; ordering is not causal proof.")
    return {"state": "unavailable" if missing else "limited" if limitations else "available",
            "unavailableReason": "required_evidence_unavailable" if missing else None,
            "turns": turns, "missingRequiredEvidence": missing,
            "limitations": sorted(set(limitations)),
            "citationValidation": "Reference membership and exact quotes do not verify entailment."}
