"""Saved reply comparison on identical captured input; no replay or new store."""

from __future__ import annotations

from verdict.sessions import digest
from verdict.statistics import wilson_interval
from verdict.trace_facts import trace_conversation_history


def compare_saved_replies(traces, judgments, left, right):
    if left == right:
        raise ValueError("select two different models")
    if (
        not judgments
        or len({j.evaluator_fingerprint for j in judgments}) != 1
        or any(not j.evaluator_identity_complete for j in judgments)
    ):
        raise ValueError("matched comparison requires one complete evaluator identity")
    by_trace = {j.trace_id: j for j in judgments}
    slots = {}, {}
    coverage = {
        "captured": len(traces),
        "missingInput": 0,
        "repeatedInput": 0,
        "matchedInputs": 0,
        "excludedRepeatedConversation": 0,
    }
    for trace in sorted(traces, key=lambda t: (t.started_at, t.trace_id)):
        if trace.tags.get("verdict.workload") in {"judge", "paired_replay"}:
            continue
        model = f"{trace.provider}/{trace.response_model or trace.request_model}"
        if model not in (left, right):
            continue
        # A redacted query alone cannot establish equality of system/prior context.
        if (
            not isinstance(trace.raw_messages, list)
            or not trace.raw_messages
            or not trace.prompt_redacted
            or not trace.response_redacted
            or trace.error
        ):
            coverage["missingInput"] += 1
            continue
        if any(
            not isinstance(m, dict)
            or m.get("role") not in {"system", "user", "assistant", "tool"}
            or not isinstance(m.get("content"), str)
            for m in trace.raw_messages
        ):
            coverage["missingInput"] += 1
            continue
        if any(set(m) - {"role", "content"} for m in trace.raw_messages):
            coverage["missingInput"] += 1
            continue
        history = trace_conversation_history(trace)
        identity = digest(
            [
                trace.prompt_redacted,
                history,
                trace.tags.get("workflow"),
                trace.tags.get("language"),
                trace.tags.get("prompt_version"),
            ]
        )
        slot = slots[0 if model == left else 1]
        if identity in slot:
            coverage["repeatedInput"] += 1
            continue
        slot[identity] = trace
    pairs = []
    used = set()
    for identity in sorted(set(slots[0]) & set(slots[1])):
        a, b = slots[0][identity], slots[1][identity]
        session_a = a.tags.get("verdict.session_id") or a.session_id
        session_b = b.tags.get("verdict.session_id") or b.session_id
        session_keys = {s for s in (session_a, session_b) if s}
        if used & session_keys:
            coverage["excludedRepeatedConversation"] += 1
            continue
        used.update(session_keys)
        pairs.append((a, b))
    coverage["matchedInputs"] = len(pairs)
    names = sorted({d.name for j in judgments for d in j.dimensions})
    metrics = []
    for name in names:
        eligible = []
        missing = unclear = error = 0
        for a, b in pairs:
            ja, jb = by_trace.get(a.trace_id), by_trace.get(b.trace_id)
            if ja is None or jb is None:
                missing += 1
                continue
            if (
                str(getattr(ja.status, "value", ja.status)) != "completed"
                or str(getattr(jb.status, "value", jb.status)) != "completed"
            ):
                error += 1
                continue
            da = [d for d in ja.dimensions if d.name == name]
            db = [d for d in jb.dimensions if d.name == name]
            if len(da) != 1 or len(db) != 1:
                missing += 1
                continue
            va, vb = (
                str(getattr(da[0].verdict, "value", da[0].verdict)).lower(),
                str(getattr(db[0].verdict, "value", db[0].verdict)).lower(),
            )
            if va not in {"pass", "fail"} or vb not in {"pass", "fail"}:
                unclear += 1
                continue
            eligible.append((va == "pass", vb == "pass"))
        n = len(eligible)
        pa = sum(a for a, _ in eligible)
        pb = sum(b for _, b in eligible)
        metrics.append(
            {
                "dimension": name,
                "pairedEvaluable": n,
                "missing": missing,
                "unclear": unclear,
                "error": error,
                "leftPassRate": pa / n if n else None,
                "rightPassRate": pb / n if n else None,
                "leftCI": wilson_interval(pa, n) if n else None,
                "rightCI": wilson_interval(pb, n) if n else None,
                "leftOnlyPass": sum(a and not b for a, b in eligible),
                "rightOnlyPass": sum(b and not a for a, b in eligible),
            }
        )
    return {
        "left": left,
        "right": right,
        "coverage": coverage,
        "metrics": metrics,
        "examples": [
            {"leftTraceId": a.trace_id, "rightTraceId": b.trace_id} for a, b in pairs[:10]
        ],
        "status": "matched_captured_reply_inputs" if pairs else "no_matched_inputs",
        "limitation": "Captured redacted input equality only. Hidden retrieval and tools are unverified. No whole-conversation model replay or causal winner is inferred.",
    }
