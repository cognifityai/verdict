"""Rubric and assessment contracts for current conversation snapshots."""

from __future__ import annotations

import hashlib
import math
import re

from verdict.conversations import _CLOSED, _json, _time, validate_conversation_query
from verdict.redaction import redact

MAX_RUBRIC_BYTES = 128_000
MAX_ASSESSMENT_BYTES = 64_000
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,127}\Z")


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _name(value: object) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value) or redact(value) != value:
        raise ValueError("invalid assessment identifier")
    return value


def _text(value: object, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or (not empty and not value.strip()):
        raise ValueError("invalid assessment text")
    safe = redact(value)
    if safe is None or len(safe.encode("utf-8")) > maximum:
        raise ValueError("assessment text exceeds limit")
    return safe


def _finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_rubric(value: object) -> dict:
    """Accept simple dimensions or a bounded deterministic scoring profile."""
    if not isinstance(value, dict) or len(_json(value).encode("utf-8")) > MAX_RUBRIC_BYTES:
        raise ValueError("invalid rubric JSON")
    from verdict.structured_rubrics import KIND, validate_profile
    if value.get("kind") == KIND:
        result = validate_profile(value)
        result["fingerprint"] = _digest(result)
        if value.get("fingerprint") not in (None, result["fingerprint"]):
            raise ValueError("rubric fingerprint mismatch")
        if len(_json(result).encode("utf-8")) > MAX_RUBRIC_BYTES:
            raise ValueError("rubric exceeds stored byte limit")
        return result
    if set(value) - {"name", "version", "target", "instructions", "dimensions", "fingerprint"}:
        raise ValueError("unsupported rubric field or scoring profile")
    target = value.get("target")
    if target not in {"response", "conversation"}:
        raise ValueError("rubric target must be response or conversation")
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, list) or not 1 <= len(dimensions) <= 12:
        raise ValueError("rubric requires 1-12 dimensions")
    result = {
        "name": _name(value.get("name")),
        "version": _name(value.get("version")),
        "target": target,
        "instructions": _text(value.get("instructions", ""), 64_000, empty=True),
        "dimensions": [],
    }
    for raw in dimensions:
        if not isinstance(raw, dict) or set(raw) - {
            "name", "description", "type", "min", "max", "direction", "passThreshold"
        }:
            raise ValueError("invalid rubric dimension")
        kind = raw.get("type", "binary")
        if kind not in {"binary", "number"}:
            raise ValueError("unsupported rubric dimension type")
        dimension = {
            "name": _name(raw.get("name")),
            "description": _text(raw.get("description"), 8_000),
            "type": kind,
        }
        if kind == "number":
            low, high = raw.get("min"), raw.get("max")
            if not _finite_number(low) or not _finite_number(high) or low >= high:
                raise ValueError("numeric dimension requires finite min < max")
            direction = raw.get("direction", "higher_is_better")
            if direction not in {"higher_is_better", "lower_is_better"}:
                raise ValueError("invalid numeric direction")
            dimension.update(min=low, max=high, direction=direction)
            if "passThreshold" in raw:
                threshold = raw["passThreshold"]
                if not _finite_number(threshold) or not low <= threshold <= high:
                    raise ValueError("invalid numeric pass threshold")
                dimension["passThreshold"] = threshold
        elif set(raw) & {"min", "max", "direction", "passThreshold"}:
            raise ValueError("binary dimension cannot have a score range")
        result["dimensions"].append(dimension)
    if len({d["name"] for d in result["dimensions"]}) != len(result["dimensions"]):
        raise ValueError("duplicate rubric dimension")
    result["fingerprint"] = _digest(result)
    if value.get("fingerprint") not in (None, result["fingerprint"]):
        raise ValueError("rubric fingerprint mismatch")
    if len(_json(result).encode("utf-8")) > MAX_RUBRIC_BYTES:
        raise ValueError("rubric exceeds stored byte limit")
    return result


def conversation_eligibility_reason(
    end_status: str | None, issue_count: int, first_user: int | None, last_assistant: int | None,
) -> str | None:
    """Decide eligibility from fields projected by every storage adapter."""
    if issue_count or end_status == "incomplete":
        return "incomplete_evidence"
    if end_status not in _CLOSED:
        return "not_closed"
    if first_user is None or last_assistant is None or first_user >= last_assistant:
        return "no_completed_reply"
    return None


def evaluation_targets(conversation: dict, rubric: dict) -> tuple[tuple[int | None, ...], str | None]:
    """One shared eligibility rule for preview, storage, review and Monitor."""
    seen_user = False
    first_user = last_assistant = None
    replies: list[int] = []
    for position, message in enumerate(conversation["messages"]):
        if message["role"] == "user" and message["status"] == "completed":
            seen_user = True
            if first_user is None:
                first_user = position
        elif message["role"] == "assistant" and message["status"] == "completed":
            last_assistant = position
            if seen_user:
                replies.append(position)
    reason = conversation_eligibility_reason(
        conversation.get("end_status"), len(conversation.get("input_issues") or []),
        first_user, last_assistant,
    )
    if reason is not None:
        return (), reason
    if rubric.get("kind") == "element_scoring_v1":
        declared = {e["phase"] for items in rubric["catalog"].values()
                    for e in items if e["phase"] != "general"}
        phases = conversation.get("enabled_phases")
        if phases and set(phases) - declared:
            return (), "unknown_enabled_phases"
    if rubric["target"] == "response":
        return tuple(replies), None
    return (None,), None


def _identity(value: object, rubric: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {
        "provider", "model", "rubric_fingerprint", "prompt_version", "max_output_tokens",
        "endpoint_fingerprint", "source",
    }:
        raise ValueError("invalid evaluator identity")
    result = {
        "provider": _name(value.get("provider")),
        "model": _name(value.get("model")),
        "rubric_fingerprint": rubric["fingerprint"],
        "prompt_version": _name(value.get("prompt_version")),
        "max_output_tokens": value.get("max_output_tokens"),
        "source": value.get("source", "judge"),
    }
    if value.get("rubric_fingerprint") != rubric["fingerprint"]:
        raise ValueError("evaluator rubric mismatch")
    if type(result["max_output_tokens"]) is not int or not 256 <= result["max_output_tokens"] <= 32_768:
        raise ValueError("invalid evaluator output budget")
    if result["source"] not in {"judge", "imported"}:
        raise ValueError("invalid evaluator source")
    if "endpoint_fingerprint" in value:
        endpoint = value["endpoint_fingerprint"]
        if not isinstance(endpoint, str) or not re.fullmatch(r"[0-9a-f]{64}", endpoint):
            raise ValueError("invalid evaluator endpoint fingerprint")
        result["endpoint_fingerprint"] = endpoint
    return result


def validate_assessment(value: object, conversation: dict) -> dict:
    """Bind a bounded, redacted result to the exact current evidence and target."""
    if not isinstance(value, dict) or set(value) - {
        "id", "tenant_id", "conversation_id", "revision", "target_position", "rubric",
        "evaluator", "evaluator_fingerprint", "status", "dimensions", "findings",
        "evaluated_at", "error", "structured",
    }:
        raise ValueError("invalid conversation assessment")
    if (value.get("tenant_id") != conversation["tenant_id"]
            or value.get("conversation_id") != conversation["id"]
            or value.get("revision") != conversation["revision"]):
        raise ValueError("conversation assessment revision or scope mismatch")
    rubric = validate_rubric(value.get("rubric"))
    targets, reason = evaluation_targets(conversation, rubric)
    target = value.get("target_position")
    if reason or target not in targets or (target is not None and type(target) is not int):
        raise ValueError("conversation assessment target unavailable")
    identity = _identity(value.get("evaluator"), rubric)
    fingerprint = _digest(identity)
    if value.get("evaluator_fingerprint") not in (None, fingerprint):
        raise ValueError("evaluator fingerprint mismatch")
    status = value.get("status")
    if status not in {"completed", "error"}:
        raise ValueError("invalid assessment status")
    evaluated_at = _time(value.get("evaluated_at"))
    if evaluated_at is None:
        raise ValueError("assessment requires evaluation time")
    raw_dimensions = value.get("dimensions")
    raw_findings = value.get("findings")
    if not isinstance(raw_dimensions, dict) or not isinstance(raw_findings, list) or len(raw_findings) > 100:
        raise ValueError("invalid assessment results")
    definitions = {d["name"]: d for d in rubric["dimensions"]}
    structured = None
    if status == "error":
        if raw_dimensions or raw_findings or value.get("structured") is not None:
            raise ValueError("failed assessment cannot contain results")
        error = value.get("error", "judge_unavailable")
        if error not in {"judge_unavailable", "invalid_judge_output", "target_unavailable"}:
            raise ValueError("invalid assessment error code")
        dimensions, findings = {}, []
    else:
        if value.get("error") not in (None, ""):
            raise ValueError("completed assessment must cover every rubric dimension")
        error = None
        if rubric.get("kind") == "element_scoring_v1":
            from verdict.structured_rubrics import score_output
            if raw_findings or value.get("structured") is None:
                raise ValueError("structured assessment requires element findings")
            structured, derived = score_output(
                rubric, value["structured"], conversation["messages"],
                conversation.get("enabled_phases"),
            )
            if raw_dimensions not in ({}, derived):
                raise ValueError("structured scores contradict element findings")
            raw_dimensions = derived
        elif value.get("structured") is not None:
            raise ValueError("simple assessment cannot contain structured findings")
        if set(raw_dimensions) != set(definitions):
            raise ValueError("completed assessment must cover every rubric dimension")
        dimensions = {}
        for name, definition in definitions.items():
            item = raw_dimensions[name]
            if not isinstance(item, dict) or set(item) - {"state", "score", "reason"}:
                raise ValueError("invalid dimension result")
            state, score = item.get("state"), item.get("score")
            if definition["type"] == "binary":
                if state not in {"pass", "fail", "unclear"} or score is not None:
                    raise ValueError("invalid binary dimension result")
            else:
                if score is not None and (
                    not _finite_number(score)
                    or not definition["min"] <= score <= definition["max"]
                ):
                    raise ValueError("numeric score outside rubric range")
                threshold = definition.get("passThreshold")
                expected = "unclear"
                if score is not None and threshold is not None:
                    passing = score >= threshold if definition["direction"] == "higher_is_better" else score <= threshold
                    expected = "pass" if passing else "fail"
                if state != expected:
                    raise ValueError("numeric state contradicts rubric threshold")
            dimensions[name] = {"state": state, "score": score, "reason": _text(item.get("reason", ""), 4_000, empty=True)}
        findings = []
        allowed_messages = conversation["messages"] if target is None else conversation["messages"][: target + 1]
        for item in raw_findings:
            if not isinstance(item, dict) or set(item) - {"dimension", "message_position", "quote", "reason", "issue"}:
                raise ValueError("invalid assessment finding")
            dimension = _name(item.get("dimension"))
            if dimension not in definitions:
                raise ValueError("finding dimension absent from rubric")
            position, quote = item.get("message_position"), item.get("quote")
            if quote is not None:
                if (type(position) is not int or not 0 <= position < len(allowed_messages)
                        or not isinstance(quote, str) or not quote or redact(quote) != quote
                        or quote not in allowed_messages[position]["content"]):
                    raise ValueError("finding quote absent from exact evidence")
            elif position is not None:
                raise ValueError("finding position requires a quote")
            findings.append({
                "dimension": dimension,
                "message_position": position,
                "quote": quote,
                "reason": _text(item.get("reason", ""), 4_000, empty=True),
                "issue": _name(item.get("issue", dimension)),
            })
    stored_target = -1 if target is None else target
    result = {
        "id": _digest([conversation["tenant_id"], conversation["id"], conversation["revision"], fingerprint, stored_target]),
        "tenant_id": conversation["tenant_id"],
        "conversation_id": conversation["id"],
        "revision": conversation["revision"],
        "target_position": target,
        "rubric": rubric,
        "evaluator": identity,
        "evaluator_fingerprint": fingerprint,
        "status": status,
        "dimensions": dimensions,
        "findings": findings,
        "evaluated_at": evaluated_at,
        "error": error,
    }
    if rubric.get("kind") == "element_scoring_v1" and structured is not None:
        result["structured"] = structured
    if value.get("id") not in (None, result["id"]):
        raise ValueError("assessment id mismatch")
    if len(_json(result).encode("utf-8")) > MAX_ASSESSMENT_BYTES:
        raise ValueError("assessment exceeds stored byte limit")
    return result


def assessment_coverage(
    conversation: dict, rubric: dict, evaluator_fingerprint: str, assessments: list[dict]
) -> dict:
    targets, reason = evaluation_targets(conversation, rubric)
    target_set = set(targets)
    relevant: dict[int | None, dict] = {}
    for row in assessments:
        target = row.get("target_position")
        if (row.get("tenant_id") != conversation["tenant_id"]
                or row.get("conversation_id") != conversation["id"]
                or row.get("revision") != conversation["revision"]
                or row.get("evaluator_fingerprint") != evaluator_fingerprint
                or target not in target_set):
            continue
        previous = relevant.get(target)
        if previous is None or (previous["status"] != "completed" and row["status"] == "completed"):
            relevant[target] = row
    completed = sum(row["status"] == "completed" for row in relevant.values())
    errors = sum(row["status"] == "error" for row in relevant.values())
    dimensions = {d["name"]: {"pass": 0, "fail": 0, "unclear": 0} for d in rubric["dimensions"]}
    for row in relevant.values():
        if row["status"] != "completed":
            continue
        for name, counts in dimensions.items():
            counts[row["dimensions"][name]["state"]] += 1
    return {
        "targets": len(targets),
        "completed": completed,
        "error": errors,
        "missing": len(targets) - len(relevant),
        "fullyGraded": bool(targets) and completed == len(targets),
        "ineligibleReason": reason,
        "dimensions": dimensions,
    }


def should_store_assessment(previous: dict | None, candidate: dict) -> bool:
    """A durable completion wins; errors remain retryable."""
    if previous is None:
        return True
    if previous["status"] == "completed":
        if candidate["status"] == "error":
            return False
        prior_value = {k: v for k, v in previous.items() if k != "evaluated_at"}
        candidate_value = {k: v for k, v in candidate.items() if k != "evaluated_at"}
        if prior_value != candidate_value:
            raise ValueError("conflicting completed assessment requires a new evaluator identity")
        return False
    return True


def validate_assessment_query(
    tenant_id: str, conversation_id: str, evaluator_fingerprint: str, limit: int
) -> None:
    validate_conversation_query(tenant_id, 1, conversation_id)
    if (not isinstance(evaluator_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", evaluator_fingerprint)):
        raise ValueError("invalid evaluator fingerprint")
    if type(limit) is not int or not 1 <= limit <= 1_000:
        raise ValueError("invalid assessment query limit")
