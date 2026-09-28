"""Native transcript snapshots and assessments in Verdict's normal storage.

A session is conversation evidence, not a provider call or an
agent execution. Revisions and assessments are immutable; source time can be
unknown. Numeric scores never become binary judgments implicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone

from verdict.redaction import redact

MAX_SESSION_BYTES = 512_000
MAX_SESSIONS = 5_000
MAX_RUBRIC_BYTES = 512_000
_KEY = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.:/-]{0,255}$")
CLOSED_STATUSES = {
    "complete",
    "premature",
    "user_stop",
    "technical_failure",
    "handoff",
}


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value) or redact(value) != value:
        raise ValueError("invalid bounded identifier")
    return value


def text(value, maximum=65_536, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or "\x00" in value:
        raise ValueError("invalid text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError("text exceeds UTF-8 byte limit")
    return redact(value) or ""


def event_time(value):
    if value is None:
        return None
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("source timestamp must have a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def validate_session(value):
    """Allowlist and redact a complete bounded transcript snapshot."""
    if not isinstance(value, dict):
        raise ValueError("session must be an object")
    messages = value.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 1000:
        raise ValueError("session requires 1-1000 messages")
    built = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in {
            "user",
            "assistant",
            "system",
        }:
            raise ValueError("invalid message role")
        status = message.get("status", "completed")
        if status not in {"completed", "interrupted", "unknown"}:
            raise ValueError("invalid message status")
        built.append(
            {
                "id": key(message.get("id", f"message-{index}")),
                "role": message["role"],
                "content": text(message.get("content")),
                "status": status,
                "event_at": event_time(message.get("event_at")),
            }
        )
    if len({m["id"] for m in built}) != len(built):
        raise ValueError("duplicate message identity")
    end_status = value.get("end_status", "unknown")
    if end_status not in CLOSED_STATUSES | {"unknown", "open"}:
        raise ValueError("invalid session ending status")
    result = {
        "id": key(value.get("id")),
        "tenant_id": key(value.get("tenant_id")),
        "source_scope": key(value.get("source_scope")),
        "messages": built,
        "event_at": event_time(value.get("event_at")),
        "end_status": end_status,
    }
    for name in ("language", "workflow", "provider", "model", "prompt_version"):
        result[name] = text(value[name], 256) if value.get(name) is not None else None
    issues = value.get("input_issues", [])
    if not isinstance(issues, list) or len(issues) > 32:
        raise ValueError("invalid input issues")
    result["input_issues"] = sorted({key(issue) for issue in issues})
    if len(canonical(result).encode()) > MAX_SESSION_BYTES:
        raise ValueError("session exceeds document byte limit")
    result["revision"] = digest(result)
    if value.get("revision") not in (None, result["revision"]):
        raise ValueError("session revision does not match evidence")
    return result


def validate_rubric(value):
    """Supported executable JSON: binary or bounded numeric dimensions."""
    if not isinstance(value, dict) or len(canonical(value).encode()) > MAX_RUBRIC_BYTES:
        raise ValueError("invalid rubric document")
    target = value.get("target", "response")
    if target not in {"response", "conversation"}:
        raise ValueError("invalid rubric target")
    result = {
        "name": key(value.get("name", value.get("id"))),
        "version": key(value.get("version")),
        "target": target,
        "instructions": text(value.get("instructions", ""), 400_000, True),
    }
    if value.get("profile") is not None or value.get("catalog") is not None:
        raise ValueError("unsupported rubric scoring profile")
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, list) or not 1 <= len(dimensions) <= 12:
        raise ValueError("rubric requires 1-12 dimensions")
    result["dimensions"] = []
    for entry in dimensions:
        if not isinstance(entry, dict):
            raise ValueError("rubric dimension must be an object")
        name, description = key(entry.get("name")), text(entry.get("description"), 8000)
        kind = entry.get("type", "binary")
        dim = {
            "name": name,
            "description": description,
            "type": kind,
            "requiresContext": entry.get("requiresContext") is True,
        }
        if kind == "number":
            low, high = entry.get("min"), entry.get("max")
            if (
                any(type(n) not in (float, int) or not math.isfinite(n) for n in (low, high))
                or low >= high
            ):
                raise ValueError("numeric dimension requires finite min < max")
            dim.update(min=low, max=high, direction=entry.get("direction", "higher_is_better"))
            if dim["direction"] not in {"higher_is_better", "lower_is_better"}:
                raise ValueError("invalid score direction")
            if "passThreshold" in entry:
                threshold = entry["passThreshold"]
                if type(threshold) not in (float, int) or not low <= threshold <= high:
                    raise ValueError("invalid explicit pass threshold")
                dim["passThreshold"] = threshold
        elif kind != "binary":
            raise ValueError("unsupported dimension type")
        result["dimensions"].append(dim)
    if len({d["name"] for d in result["dimensions"]}) != len(result["dimensions"]):
        raise ValueError("duplicate rubric dimension")
    result["fingerprint"] = digest(result)
    return result


def validate_assessment(value):
    """Validate stored assessment envelope; the evaluator validates each finding."""
    required = {
        "session_id",
        "tenant_id",
        "session_revision",
        "evaluator_fingerprint",
        "rubric",
        "evaluator",
        "evaluated_at",
        "status",
        "dimensions",
        "findings",
        "source",
    }
    if not isinstance(value, dict) or not required <= set(value):
        raise ValueError("incomplete session assessment")
    result = json.loads(canonical({name: value[name] for name in required}))
    for name in ("session_id", "tenant_id", "session_revision", "evaluator_fingerprint"):
        key(result[name])
    for name in ("session_revision", "evaluator_fingerprint"):
        if len(result[name]) != 64 or not re.fullmatch(r"[0-9a-f]{64}", result[name]):
            raise ValueError("invalid assessment fingerprint")
    result["rubric"] = validate_rubric(result["rubric"])
    target = value.get("target_message_id")
    if result["rubric"]["target"] == "response":
        result["target_message_id"] = key(target)
    elif target is not None:
        raise ValueError("whole-conversation assessment has no response target")
    from verdict.redaction import redact_structure

    identity = result["evaluator"]
    allowed_identity = {
        "provider",
        "model",
        "rubric_fingerprint",
        "prompt_version",
        "target",
        "source",
        "scorer_version",
        "max_output_tokens",
        "endpoint_fingerprint",
        "config",
    }
    if (
        not isinstance(identity, dict)
        or set(identity) - allowed_identity
        or len(canonical(identity).encode()) > 16_384
    ):
        raise ValueError("invalid bounded evaluator identity")
    for field in ("provider", "model", "prompt_version", "target", "source"):
        key(identity.get(field))
    if identity["target"] != result["rubric"]["target"] or identity["source"] != result["source"]:
        raise ValueError("evaluator target/source mismatch")
    if redact_structure(identity, mode="redact") != identity:
        raise ValueError("evaluator identity contains sensitive configuration")
    if digest(identity) != result["evaluator_fingerprint"]:
        raise ValueError("evaluator identity mismatch")
    if result["evaluator"].get("rubric_fingerprint") != result["rubric"]["fingerprint"]:
        raise ValueError("evaluator rubric identity mismatch")
    result["evaluated_at"] = event_time(result["evaluated_at"])
    if result["evaluated_at"] is None or result["status"] not in {"completed", "error"}:
        raise ValueError("invalid assessment state")
    if result["source"] not in {"judge", "imported"}:
        raise ValueError("invalid assessment source")
    dimensions = result["dimensions"]
    if not isinstance(dimensions, dict) or len(dimensions) > 12:
        raise ValueError("invalid assessment dimensions")
    for name, dim in dimensions.items():
        key(name)
        if not isinstance(dim, dict) or dim.get("state") not in {
            "pass",
            "fail",
            "unclear",
            "missing",
            "error",
        }:
            raise ValueError("invalid dimension state")
        if dim.get("score") is not None and (
            type(dim["score"]) not in (int, float) or not math.isfinite(dim["score"])
        ):
            raise ValueError("invalid numeric assessment")
        dimensions[name] = {
            "state": dim["state"],
            "score": dim.get("score"),
            "reason": text(dim.get("reason", ""), 8000, True),
        }
    result["error"] = text(value.get("error", ""), 2000, True)
    if value.get("summary") is not None:
        result["summary"] = value["summary"]
    if len(canonical(result).encode()) > MAX_SESSION_BYTES:
        raise ValueError("assessment exceeds byte limit")
    # The judge saw sanitized messages. Never turn a fabricated sensitive quote
    # into a matching placeholder before checking its exact evidence.
    if not isinstance(result["findings"], list) or len(result["findings"]) > 1200:
        raise ValueError("invalid assessment findings")
    for finding in result["findings"]:
        if isinstance(finding, dict) and finding.get("quote") is not None:
            quote = finding["quote"]
            if not isinstance(quote, str) or redact(quote) != quote:
                raise ValueError("evidence quote must match sanitized source text exactly")
    # All free-form judge/scorer output is redacted before persistence.
    result["findings"] = redact_structure(result["findings"], mode="redact")
    if "summary" in result:
        result["summary"] = redact_structure(result["summary"], mode="redact")
    identity = [
        result["tenant_id"],
        result["session_id"],
        result["session_revision"],
        result["evaluator_fingerprint"],
    ]
    if target is not None:
        identity.append(target)
    if result["status"] == "error":
        identity.append(result["evaluated_at"])
    result["id"] = digest(identity)
    if value.get("id") not in (None, result["id"]):
        raise ValueError("assessment identity mismatch")
    return result


def check_assessment_evidence(assessment, session):
    """Enforce the same result contract at the last durable write boundary."""
    if (
        assessment["session_id"] != session["id"]
        or assessment["tenant_id"] != session["tenant_id"]
        or assessment["session_revision"] != session["revision"]
    ):
        raise ValueError("assessment evidence scope mismatch")
    if assessment.get("target_message_id"):
        target = next(
            (m for m in session["messages"] if m["id"] == assessment["target_message_id"]), None
        )
        if target is None or target["role"] != "assistant" or target["status"] != "completed":
            raise ValueError("response target is unavailable")
    if assessment["status"] == "error":
        if assessment["dimensions"] or assessment["findings"]:
            raise ValueError("failed assessment cannot contain completed findings")
        return
    rubric = assessment["rubric"]
    declared = {d["name"]: d for d in rubric["dimensions"]}
    if set(assessment["dimensions"]) != set(declared):
        raise ValueError("assessment must contain every declared dimension exactly once")
    for name, dim in assessment["dimensions"].items():
        definition, state, score = declared[name], dim["state"], dim["score"]
        if state in {"error", "missing"} and score is not None:
            raise ValueError("unassessed dimension cannot contain a score")
        if definition["type"] == "binary":
            if score is not None:
                raise ValueError("binary dimension cannot contain numeric score")
        elif score is not None:
            if not definition["min"] <= score <= definition["max"]:
                raise ValueError("score outside rubric range")
            threshold = definition.get("passThreshold")
            if threshold is None and state != "unclear":
                raise ValueError("numeric pass/fail requires explicit rubric threshold")
            if threshold is not None:
                passes = (
                    score >= threshold
                    if definition.get("direction", "higher_is_better") == "higher_is_better"
                    else score <= threshold
                )
                if state != ("pass" if passes else "fail"):
                    raise ValueError("numeric state contradicts declared threshold")
        elif state in {"pass", "fail"}:
            raise ValueError("numeric pass/fail requires score")
    findings = assessment["findings"]
    if not isinstance(findings, list) or len(findings) > 1200:
        raise ValueError("invalid assessment findings")
    source_messages = session["messages"]
    if assessment.get("target_message_id"):
        index = next(
            (
                i
                for i, m in enumerate(source_messages)
                if m["id"] == assessment["target_message_id"]
                and m["role"] == "assistant"
                and m["status"] == "completed"
            ),
            None,
        )
        if index is None:
            raise ValueError("response target is unavailable")
        source_messages = source_messages[: index + 1]
    messages = {m["id"]: m["content"] for m in source_messages}
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("dimension") not in declared:
            raise ValueError("finding must reference declared dimension")
        text(finding.get("reason"), 8000)
        quote, message_id = finding.get("quote"), finding.get("message_id")
        if quote is not None:
            if (
                not isinstance(quote, str)
                or not quote
                or message_id not in messages
                or quote not in messages[message_id]
            ):
                raise ValueError("finding evidence not present in referenced message")
        elif message_id is not None:
            raise ValueError("message reference requires evidence quote")


def validate_query(tenant_id, limit=100):
    key(tenant_id)
    if type(limit) is not int or not 1 <= limit <= MAX_SESSIONS + 1:
        raise ValueError("invalid bounded session query")


def select_assessments(assessments):
    """Completed immutable evidence wins over failed attempts, in any order."""
    if len(assessments) > MAX_SESSIONS:
        raise ValueError("session assessment query exceeds bounded input limit")
    selected = {}
    for row in assessments:
        identity = (
            row["tenant_id"],
            row["session_id"],
            row["session_revision"],
            row["evaluator_fingerprint"],
            row.get("target_message_id"),
        )
        old = selected.get(identity)
        rank = (row["status"] == "completed", row["evaluated_at"], row["id"])
        if old is None or rank > (old["status"] == "completed", old["evaluated_at"], old["id"]):
            selected[identity] = row
    return list(selected.values())


def aggregate_reply_assessments(session, assessments, fingerprint, dimensions):
    """All-completed-replies v1: strict full-coverage conversation estimand."""
    targets = {
        m["id"]
        for m in session["messages"]
        if m["role"] == "assistant" and m["status"] == "completed"
    }
    rows = {
        a.get("target_message_id"): a
        for a in select_assessments(assessments)
        if a["tenant_id"] == session["tenant_id"]
        and a["session_id"] == session["id"]
        and a["session_revision"] == session["revision"]
        and a["evaluator_fingerprint"] == fingerprint
        and a["rubric"]["target"] == "response"
    }
    known_fail = any(
        a["status"] == "completed" and any(d["state"] == "fail" for d in a["dimensions"].values())
        for t, a in rows.items()
        if t in targets
    )
    assessed = sum(t in rows and rows[t]["status"] == "completed" for t in targets)
    result = {}
    for name in dimensions:
        states = [
            "missing"
            if t not in rows
            else "error"
            if rows[t]["status"] == "error"
            else rows[t]["dimensions"].get(name, {}).get("state", "missing")
            for t in sorted(targets)
        ] or ["missing"]
        state = next((s for s in ("missing", "error", "unclear", "fail") if s in states), "pass")
        result[name] = {
            "state": state,
            "score": None,
            "reason": "Response assessments aggregated over all completed replies.",
        }
    state = (
        "pending"
        if any(d["state"] == "missing" for d in result.values())
        else "error"
        if any(d["state"] == "error" for d in result.values())
        else "completed"
    )
    return (
        result,
        state,
        {
            "expectedReplies": len(targets),
            "assessedReplies": assessed,
            "partiallyAssessed": int(assessed < len(targets)),
            "knownFailuresInPartial": int(
                known_fail
                and any(d["state"] in {"missing", "error", "unclear"} for d in result.values())
            ),
            "excludedInterruptedReplies": sum(
                m["role"] == "assistant" and m["status"] != "completed" for m in session["messages"]
            ),
        },
    )


def session_monitor_units(sessions, assessments, policy):
    """Project eligible conversations once into existing Boolean Monitor."""
    from verdict.monitoring import AnalysisUnitRecord

    selected = {
        (a["session_id"], a["session_revision"]): a
        for a in select_assessments(assessments)
        if a["evaluator_fingerprint"] == policy.evaluator_fingerprint
        and a["rubric"]["target"] == "conversation"
    }
    coverage = {
        "captured": len(sessions),
        "eligible": 0,
        "unknownClosure": 0,
        "missingEventTime": 0,
        "incompleteEvidence": 0,
        "judged": 0,
        "expectedReplies": 0,
        "assessedReplies": 0,
        "partiallyAssessed": 0,
        "knownFailuresInPartial": 0,
        "excludedInterruptedReplies": 0,
    }
    units = []
    for session in sessions:
        if session["event_at"] is None:
            coverage["missingEventTime"] += 1
            continue
        if session["end_status"] not in CLOSED_STATUSES:
            coverage["unknownClosure"] += 1
            continue
        if session["input_issues"]:
            coverage["incompleteEvidence"] += 1
            continue
        coverage["eligible"] += 1
        metrics = {"session.completed": session["end_status"] == "complete"}
        states = {}
        numeric = {}
        assessment = selected.get((session["id"], session["revision"]))
        evaluator_state = "not_requested"
        evidence_digest = None
        if policy.evaluator_fingerprint:
            evidence_digest = session["revision"]
            evaluator_state = (
                "completed" if assessment and assessment["status"] == "completed" else "pending"
            )
            if assessment and assessment["status"] == "error":
                evaluator_state = "error"
            if policy.response_aggregation:
                dims, evaluator_state, reply_coverage = aggregate_reply_assessments(
                    session, assessments, policy.evaluator_fingerprint, policy.evaluator_dimensions
                )
                for name, count in reply_coverage.items():
                    coverage[name] += count
                source_result = next(
                    (
                        a
                        for a in assessments
                        if a["evaluator_fingerprint"] == policy.evaluator_fingerprint
                    ),
                    None,
                )
                assessment = (
                    {"dimensions": dims, "rubric": source_result["rubric"]}
                    if source_result
                    else None
                )
            if evaluator_state == "completed":
                coverage["judged"] += 1
            rubric = assessment["rubric"] if assessment else None
            # The selected immutable evaluator definition is supplied by the projection owner.
            definitions = {}
            identity_result = next(
                (
                    a
                    for a in assessments
                    if a["evaluator_fingerprint"] == policy.evaluator_fingerprint
                ),
                None,
            )
            if rubric is None and identity_result:
                rubric = identity_result["rubric"]
            if rubric:
                definitions = {d["name"]: d for d in rubric["dimensions"]}
            definitions = definitions or {}
            for name in policy.evaluator_dimensions:
                definition = definitions.get(name, {"type": "binary"})
                dim = assessment["dimensions"].get(name) if assessment else None
                state = (
                    dim["state"] if dim else ("error" if evaluator_state == "error" else "missing")
                )
                if definition["type"] == "number" and policy.numeric_method:
                    metric = f"score.{name}"
                    value = dim["score"] if dim else None
                    states[metric] = (
                        "pass"
                        if value is not None
                        else (state if state in {"missing", "error"} else "unclear")
                    )
                    numeric[metric] = (
                        value,
                        definition["min"],
                        definition["max"],
                        definition.get("direction", "higher_is_better"),
                    )
                else:
                    metric = f"judge.{name}.pass"
                    states[metric] = state
                    if state in {"pass", "fail"}:
                        metrics[metric] = state == "pass"
        group_id = group_label = None
        if policy.grouping_mode == "population":
            population = [session["language"] or "unknown", session["workflow"] or "unknown"]
            group_id = digest(population)
            group_label = " / ".join(population)
        elif policy.grouping_mode == "provider_model":
            population = [session["provider"] or "unknown", session["model"] or "unknown"]
            group_id = digest(population)
            group_label = " / ".join(population)
        elif policy.grouping_mode != "none":
            raise ValueError("conversation monitoring requires explicit populations")
        units.append(
            AnalysisUnitRecord(
                session["id"],
                datetime.fromisoformat(session["event_at"]),
                metrics,
                group_id,
                states,
                group_label,
                evaluator_state=evaluator_state,
                evaluator_evidence_digest=evidence_digest,
                source_revision=session["revision"],
                numeric_metrics=numeric,
            )
        )
    return units, coverage
