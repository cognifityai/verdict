"""Descriptive comparison of current whole-conversation binary grades."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from verdict.conversation_assessments import _name, conversation_eligibility_reason
from verdict.conversations import _LABEL_KEY
from verdict.statistics import wilson_interval
from verdict.telemetry.model import safe_routing_id

MAX_COMPARISON_ROWS = 10_000
MAX_GROUPS = 100
_STATES = ("pass", "fail", "unclear", "error", "ungraded", "ineligible")


def _boundary(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("comparison window requires an ISO-8601 time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("comparison window requires an ISO-8601 time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("comparison window requires a timezone")
    try:
        return parsed.astimezone(timezone.utc).isoformat()
    except OverflowError as exc:
        raise ValueError("comparison window is out of range") from exc


def comparison_query(tenant_id: str, payload: dict) -> dict:
    if tenant_id != "__verdict_local__" and safe_routing_id(tenant_id) is None:
        raise ValueError("invalid tenant")
    if not isinstance(payload, dict) or set(payload) - {
        "analysisUnit", "referenceStart", "referenceEnd", "currentStart", "currentEnd",
        "evaluatorFingerprint", "dimension", "labelKey",
    } or payload.get("analysisUnit") != "conversation":
        raise ValueError("invalid conversation comparison request")
    fingerprint = payload.get("evaluatorFingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise ValueError("conversation comparison requires an evaluator fingerprint")
    dimension = _name(payload.get("dimension"))
    raw_label_key = payload.get("labelKey")
    label_key = None if raw_label_key in (None, "") else raw_label_key
    if label_key is not None and (
        not isinstance(label_key, str) or _LABEL_KEY.fullmatch(label_key) is None
    ):
        raise ValueError("invalid comparison label key")
    result = {
        "tenant_id": tenant_id,
        "reference_start": _boundary(payload.get("referenceStart")),
        "reference_end": _boundary(payload.get("referenceEnd")),
        "current_start": _boundary(payload.get("currentStart")),
        "current_end": _boundary(payload.get("currentEnd")),
        "evaluator_fingerprint": fingerprint,
        "dimension": dimension,
        "label_key": label_key,
    }
    if not (result["reference_start"] < result["reference_end"] <= result["current_start"]
            < result["current_end"]):
        raise ValueError("comparison windows must be ordered and nonoverlapping")
    return result


def validate_storage_query(query: dict, limit: int) -> dict:
    if not isinstance(query, dict) or limit != MAX_COMPARISON_ROWS + 1:
        raise ValueError("invalid conversation comparison query")
    try:
        canonical = comparison_query(query["tenant_id"], {
            "analysisUnit": "conversation", "referenceStart": query["reference_start"],
            "referenceEnd": query["reference_end"], "currentStart": query["current_start"],
            "currentEnd": query["current_end"],
            "evaluatorFingerprint": query["evaluator_fingerprint"],
            "dimension": query["dimension"], "labelKey": query["label_key"],
        })
    except KeyError as exc:
        raise ValueError("invalid conversation comparison query") from exc
    if canonical != query:
        raise ValueError("noncanonical conversation comparison query")
    return canonical


def _summary(rows: list[dict]) -> dict:
    counts = {state: 0 for state in _STATES}
    examples = {state: [] for state in _STATES}
    reasons = {reason: 0 for reason in ("incomplete_evidence", "not_closed", "no_completed_reply")}
    for row in rows:
        state = row["quality_state"]
        counts[state] += 1
        if state == "ineligible":
            reasons[row["ineligible_reason"]] += 1
        if len(examples[state]) < 5:
            examples[state].append({"id": row["id"], "revision": row["revision"],
                                    "qualityState": state,
                                    "ineligibleReason": row["ineligible_reason"]})
    total = counts["pass"] + counts["fail"]
    interval = wilson_interval(counts["pass"], total)
    return {
        "captured": len(rows), "eligible": len(rows) - counts["ineligible"],
        "ineligible": counts["ineligible"], "ineligibleReasons": reasons,
        "pass": counts["pass"], "fail": counts["fail"],
        "unclear": counts["unclear"], "error": counts["error"],
        "ungraded": counts["ungraded"], "evaluable": total,
        "passRate": counts["pass"] / total if total else None,
        "interval": list(interval), "examples": examples,
    }


def preview_conversation_comparison(storage, *, tenant_id: str, payload: dict) -> dict:
    query = comparison_query(tenant_id, payload)
    reader = getattr(storage, "load_conversation_comparison_rows", None)
    if not callable(reader):
        raise ValueError("conversation comparison is unavailable for this storage")
    rows = reader(query, limit=MAX_COMPARISON_ROWS + 1)
    if len(rows) > MAX_COMPARISON_ROWS:
        raise ValueError("selected conversation windows exceed 10,000 rows; choose narrower dates")
    reference: list[dict] = []
    current: list[dict] = []
    grouped: dict[str | None, tuple[list[dict], list[dict]]] = {}
    grade_evidence_count = 0
    for row in rows:
        event_at = row["event_at"]
        if _boundary(event_at) != event_at:
            raise ValueError("stored comparison row has noncanonical event time")
        if query["reference_start"] <= event_at < query["reference_end"]:
            bucket = reference
            group_side = 0
        elif query["current_start"] <= event_at < query["current_end"]:
            bucket = current
            group_side = 1
        else:
            raise ValueError("stored comparison row is outside selected windows")
        reason = row.get("ineligible_reason")
        if "ineligible_reason" not in row:
            reason = conversation_eligibility_reason(
                row["end_status"], row["issue_count"],
                row["first_user"], row["last_assistant"],
            )
        status = row["assessment_status"]
        if reason is not None:
            state = "ineligible"
        elif status is None:
            state = "ungraded"
        else:
            grade_evidence_count += 1
            if row["rubric_target"] != "conversation" or row["dimension_type"] != "binary":
                raise ValueError("selected evaluator dimension is not a whole-conversation binary grade")
            if status == "error":
                state = "error"
            elif status == "completed" and row["dimension_state"] in {"pass", "fail", "unclear"}:
                state = row["dimension_state"]
            else:
                raise ValueError("stored conversation grade is invalid")
        selected = {"id": row["id"], "revision": row["revision"],
                    "quality_state": state, "ineligible_reason": reason}
        bucket.append(selected)
        if query["label_key"] is not None:
            labels = row.get("labels") or {}
            if isinstance(labels, str):
                labels = json.loads(labels)
            if not isinstance(labels, dict):
                raise ValueError("stored conversation labels are invalid")
            group = labels.get(query["label_key"])
            if group is not None and (not isinstance(group, str) or len(group.encode("utf-8")) > 128):
                raise ValueError("stored conversation label is invalid")
            if group not in grouped:
                if len(grouped) >= MAX_GROUPS:
                    raise ValueError("selected label has more than 100 groups")
                grouped[group] = ([], [])
            grouped[group][group_side].append(selected)
    base, now = _summary(reference), _summary(current)
    groups = []
    for value, (base_rows, current_rows) in sorted(grouped.items(), key=lambda item: item[0] or ""):
        left, right = _summary(base_rows), _summary(current_rows)
        groups.append({
            "value": value, "label": value if value is not None else "(missing label)",
            "reference": left, "current": right,
            "referenceShare": left["eligible"] / base["eligible"] if base["eligible"] else None,
            "currentShare": right["eligible"] / now["eligible"] if now["eligible"] else None,
            "effect": right["passRate"] - left["passRate"]
            if left["passRate"] is not None and right["passRate"] is not None else None,
        })
    return {
        "state": "descriptive", "unit": "conversation", "method": "current_snapshot_binary_v1",
        "evaluatorFingerprint": query["evaluator_fingerprint"],
        "dimension": query["dimension"], "labelKey": query["label_key"],
        "referenceStart": query["reference_start"], "referenceEnd": query["reference_end"],
        "currentStart": query["current_start"], "currentEnd": query["current_end"],
        "reference": base, "current": now,
        "gradeEvidenceCount": grade_evidence_count,
        "effect": now["passRate"] - base["passRate"]
        if base["passRate"] is not None and now["passRate"] is not None else None,
        "groups": groups,
    }
