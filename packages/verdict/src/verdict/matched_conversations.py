"""Descriptive comparison of source-declared matched conversation cases."""

from __future__ import annotations

import json
import math

from verdict.conversation_assessments import _name, conversation_eligibility_reason
from verdict.conversation_monitoring import _boundary
from verdict.conversations import _LABEL_KEY
from verdict.redaction import redact
from verdict.telemetry.model import safe_routing_id

MAX_MATCHED_ROWS = 10_000
MAX_EXAMPLES = 50


def _label(value: object) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 128
            or redact(value) != value):
        raise ValueError("invalid matched comparison label value")
    return value


def matched_query(tenant_id: str, payload: dict) -> dict:
    if tenant_id != "__verdict_local__" and safe_routing_id(tenant_id) is None:
        raise ValueError("invalid tenant")
    if not isinstance(payload, dict) or set(payload) != {
        "analysisUnit", "windowStart", "windowEnd", "evaluatorFingerprint", "dimension",
        "pairKey", "variantKey", "leftVariant", "rightVariant",
    } or payload.get("analysisUnit") != "matched_conversation":
        raise ValueError("invalid matched conversation request")
    fingerprint = payload["evaluatorFingerprint"]
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)):
        raise ValueError("matched comparison requires an evaluator fingerprint")
    pair_key, variant_key = payload["pairKey"], payload["variantKey"]
    if any(not isinstance(key, str) or _LABEL_KEY.fullmatch(key) is None
           for key in (pair_key, variant_key)) or pair_key == variant_key:
        raise ValueError("matched comparison requires distinct valid label keys")
    result = {
        "tenant_id": tenant_id, "window_start": _boundary(payload["windowStart"]),
        "window_end": _boundary(payload["windowEnd"]),
        "evaluator_fingerprint": fingerprint, "dimension": _name(payload["dimension"]),
        "pair_key": pair_key, "variant_key": variant_key,
        "left_variant": _label(payload["leftVariant"]),
        "right_variant": _label(payload["rightVariant"]),
    }
    if result["window_start"] >= result["window_end"]:
        raise ValueError("matched comparison window must be ordered")
    if result["left_variant"] == result["right_variant"]:
        raise ValueError("matched comparison variants must differ")
    return result


def validate_storage_query(query: dict, limit: int) -> dict:
    if not isinstance(query, dict) or limit != MAX_MATCHED_ROWS + 1:
        raise ValueError("invalid matched comparison storage query")
    try:
        canonical = matched_query(query["tenant_id"], {
            "analysisUnit": "matched_conversation", "windowStart": query["window_start"],
            "windowEnd": query["window_end"], "evaluatorFingerprint": query["evaluator_fingerprint"],
            "dimension": query["dimension"], "pairKey": query["pair_key"],
            "variantKey": query["variant_key"], "leftVariant": query["left_variant"],
            "rightVariant": query["right_variant"],
        })
    except KeyError as exc:
        raise ValueError("invalid matched comparison storage query") from exc
    if canonical != query:
        raise ValueError("noncanonical matched comparison storage query")
    return canonical


def _labels(value: object) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("stored conversation labels are invalid")
    return value


def _grade(row: dict) -> dict:
    reason = row.get("ineligible_reason")
    if "ineligible_reason" not in row:
        reason = conversation_eligibility_reason(row["end_status"], row["issue_count"],
                                                 row["first_user"], row["last_assistant"])
    status = row["assessment_status"]
    if status is not None and (row["rubric_target"] != "conversation"
                               or row["dimension_type"] not in {"binary", "number"}):
        raise ValueError("selected evaluator dimension is not a whole-conversation grade")
    if status not in (None, "completed", "error"):
        raise ValueError("stored conversation grade is invalid")
    score = row.get("dimension_score")
    if score is not None:
        if row.get("dimension_score_type") not in {"number", "integer", "real"}:
            raise ValueError("stored numeric score is invalid")
        try:
            score = float(score)
        except (TypeError, ValueError) as exc:
            raise ValueError("stored numeric score is invalid") from exc
        if not math.isfinite(score):
            raise ValueError("stored numeric score is invalid")
    state = row.get("dimension_state")
    if status == "completed" and state not in {"pass", "fail", "unclear"}:
        raise ValueError("stored conversation grade is invalid")
    kind = row["dimension_type"]
    direction = row.get("dimension_direction")
    low, high = row.get("dimension_min"), row.get("dimension_max")
    if kind == "binary" and (score is not None or direction is not None or low is not None or high is not None):
        raise ValueError("stored binary grade is invalid")
    if kind == "number":
        try:
            low, high = float(low), float(high)
        except (TypeError, ValueError) as exc:
            raise ValueError("stored numeric range is invalid") from exc
        if (direction not in {"higher_is_better", "lower_is_better"} or not math.isfinite(low)
                or not math.isfinite(high) or low >= high
                or (score is not None and not low <= score <= high)):
            raise ValueError("stored numeric grade is invalid")
    return {"id": row["id"], "revision": row["revision"], "status": status,
            "state": state, "score": score, "type": kind, "direction": direction,
            "min": low, "max": high,
            "ineligibleReason": reason, "endStatus": row["end_status"]}


def preview_matched_conversations(storage, *, tenant_id: str, payload: dict) -> dict:
    query = matched_query(tenant_id, payload)
    reader = getattr(storage, "load_matched_conversation_rows", None)
    if not callable(reader):
        raise ValueError("matched comparison is unavailable for this storage")
    rows = reader(query, limit=MAX_MATCHED_ROWS + 1)
    if len(rows) > MAX_MATCHED_ROWS:
        raise ValueError("selected matched comparison exceeds 10,000 variant rows; choose narrower dates")
    candidates: dict[str, dict[str, list[dict]]] = {}
    missing_pair = 0
    kind = direction = low = high = None
    for row in rows:
        if _boundary(row["event_at"]) != row["event_at"]:
            raise ValueError("stored matched row has noncanonical event time")
        if not query["window_start"] <= row["event_at"] < query["window_end"]:
            raise ValueError("stored matched row is outside selected window")
        labels = _labels(row["labels"])
        variant = labels.get(query["variant_key"])
        if variant not in (query["left_variant"], query["right_variant"]):
            raise ValueError("stored matched row has another variant")
        pair_id = labels.get(query["pair_key"])
        if pair_id is None:
            missing_pair += 1
            continue
        _label(pair_id)
        item = _grade(row)
        if item["type"] is not None:
            if kind is None:
                kind, direction, low, high = item["type"], item["direction"], item["min"], item["max"]
            elif (kind, direction, low, high) != (item["type"], item["direction"], item["min"], item["max"]):
                raise ValueError("selected grades have inconsistent rubric dimensions")
        pair = candidates.setdefault(pair_id, {"left": [], "right": []})
        pair["left" if variant == query["left_variant"] else "right"].append(item)

    excluded = {"missingPairRows": missing_pair, "duplicatePairIds": 0,
                "unmatchedPairIds": 0, "ineligiblePairs": 0, "ungradedPairs": 0,
                "errorPairs": 0, "unclearPairs": 0, "missingScorePairs": 0}
    binary = {"bothPass": 0, "bothFail": 0, "leftPassRightFail": 0,
              "leftFailRightPass": 0}
    left_scores: list[float] = []
    right_scores: list[float] = []
    declared = usable = technical = 0
    examples = []
    for pair_id, sides in sorted(candidates.items()):
        left, right = sides["left"], sides["right"]
        if len(left) > 1 or len(right) > 1:
            excluded["duplicatePairIds"] += 1
            continue
        if not left or not right:
            excluded["unmatchedPairIds"] += 1
            continue
        declared += 1
        a, b = left[0], right[0]
        if a["ineligibleReason"] or b["ineligibleReason"]:
            excluded["ineligiblePairs"] += 1
            continue
        if a["status"] is None or b["status"] is None:
            excluded["ungradedPairs"] += 1
            continue
        if a["status"] == "error" or b["status"] == "error":
            excluded["errorPairs"] += 1
            continue
        if kind == "binary":
            if a["state"] == "unclear" or b["state"] == "unclear":
                excluded["unclearPairs"] += 1
                continue
            key = ("bothPass" if a["state"] == b["state"] == "pass" else
                   "bothFail" if a["state"] == b["state"] == "fail" else
                   "leftPassRightFail" if a["state"] == "pass" else "leftFailRightPass")
            binary[key] += 1
        elif kind == "number":
            if a["score"] is None or b["score"] is None:
                excluded["missingScorePairs"] += 1
                continue
            left_scores.append(a["score"])
            right_scores.append(b["score"])
        else:
            excluded["ungradedPairs"] += 1
            continue
        usable += 1
        if a["endStatus"] == "technical_failure" or b["endStatus"] == "technical_failure":
            technical += 1
        if len(examples) < MAX_EXAMPLES:
            examples.append({"pairId": pair_id, "left": a, "right": b})
    numeric = None
    if kind == "number" and usable:
        width = high - low
        if not math.isfinite(width):
            numeric = {"unavailableReason": "score_arithmetic_overflow"}
        else:
            left_mean = math.fsum(value / usable for value in left_scores)
            right_mean = math.fsum(value / usable for value in right_scores)
            delta = math.fsum((right - left) / usable
                              for left, right in zip(left_scores, right_scores, strict=True))
            radius = width * math.sqrt(2 * math.log(40) / usable)
            numeric = {"leftMean": left_mean, "rightMean": right_mean,
                       "meanRightMinusLeft": delta,
                       "deltaInterval95": [max(-width, delta - radius),
                                           min(width, delta + radius)],
                       "intervalMethod": "bounded_hoeffding_95"}
    return {
        "state": "descriptive", "unit": "matched_conversation",
        "method": "source_declared_current_pairs_v1", "windowStart": query["window_start"],
        "windowEnd": query["window_end"], "evaluatorFingerprint": query["evaluator_fingerprint"],
        "dimension": query["dimension"], "pairKey": query["pair_key"],
        "variantKey": query["variant_key"], "leftVariant": query["left_variant"],
        "rightVariant": query["right_variant"], "dimensionType": kind,
        "direction": direction, "scoreRange": [low, high] if kind == "number" else None,
        "candidateRows": len(rows),
        "declaredPairs": declared, "usablePairs": usable,
        "technicalFailurePairs": technical, "exclusions": excluded,
        "binary": binary if kind == "binary" else None,
        "numeric": numeric,
        "examples": examples,
    }
