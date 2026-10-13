"""Opt-in evaluation of bounded agent session, run, or tool-action evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.evaluator_lab import (
    _destination,
    _IdentityOnlyProvider,
    _judge,
    _output_maximum,
    _provider,
    _rubric_summary,
    _validated_config,
)
from verdict.dashboard.setup_routes import SetupRoutes
from verdict.evidence import AgentEventType, EvidenceState, event_order_key
from verdict.pricing import compute_cost_usd
from verdict.redaction import _PLACEHOLDER, redact, redact_structure

_MAX_TURNS = 20
_MAX_EVENTS = 80
_MAX_EVIDENCE_CHARS = 8_000
_PROMPT_VERSION = "ordered-agent-evidence-v2"
_REF = re.compile(r"\[([a-z_]+):([^\]]+)\]")
_CITATION_REQUEST = ("For each scored dimension, cite at least one exact [turn:...] or "
                     "[event:...] reference from the recorded evidence in the reasoning. "
                     "If the evidence does not support a judgment, return UNCLEAR.\n")
_LOCK = threading.Lock()
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
            or config.get("runId") is not None or config.get("eventId") is not None):
        raise ValueError("invalid session evaluation selection")
    report = build_agent_session_bundle(
        storage_url, tenant=tenant, source_kind=source_kind, session_id=session_id,
    )
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


def _projection(storage, tenant: str, config: dict[str, Any],
                storage_url: str | None = None) -> dict[str, Any]:
    scope = config.get("scope")
    if scope == "session":
        if storage_url is None:
            raise ValueError("session evaluation requires storage URL")
        return _session_projection(storage_url, tenant, config)
    run_id = config.get("runId")
    event_id = config.get("eventId")
    if (scope not in {"run", "tool_action"} or not isinstance(run_id, str)
            or not run_id or len(run_id.encode()) > 256 or
            (scope == "tool_action" and (not isinstance(event_id, str) or not event_id
             or len(event_id.encode()) > 256)) or
            (scope == "run" and event_id is not None)):
        raise ValueError("invalid scoped evaluation selection")
    bundle = storage.get_agent_run_bundle(tenant, run_id)
    if bundle is None:
        raise KeyError(run_id)
    turns = sorted(bundle.turns, key=lambda turn: (turn.sequence, turn.turn_id))
    events = sorted(bundle.events, key=event_order_key)
    producers = {event.producer_id for event in events if event.producer_id}
    if scope == "run":
        reason = None
        if not turns:
            reason = "no_turns"
        elif len(turns) > _MAX_TURNS or len(events) > _MAX_EVENTS:
            reason = "evidence_limit"
        elif len(producers) > 1:
            reason = "event_order_uncertain"
        elif any(event.provenance == "verdict:capture_limit" for event in events):
            reason = "source_capture_partial"
        elif any(turn.request_state is not EvidenceState.PRESENT or
                 turn.response_state is not EvidenceState.PRESENT or
                 turn.request_truncated or turn.response_truncated or turn.ended_at is None
                 for turn in turns):
            reason = "turn_content_partial"
        if reason is not None:
            return _finish_evidence({
                "scope": scope, "runId": run_id, "eventId": None,
                "query": "", "response": "", "evidenceReferences": [],
                "coverage": "partial", "unavailableReason": reason,
            })
        requests = [{"reference": f"[turn:{turn.turn_id}]", "request": turn.user_request_redacted}
                    for turn in turns]
        timed = [
            (turn.ended_at or turn.started_at, 1, turn.sequence,
             {"reference": f"[turn:{turn.turn_id}]", "response": turn.final_response_redacted})
            for turn in turns
        ] + [
            (event.occurred_at, 0, event_order_key(event),
             {"reference": f"[event:{event.event_id}]", "type": event.event_type.value,
              "status": event.status.value, "attributes": event.attributes})
            for event in events
        ]
        timeline = [item for _, _, _, item in sorted(timed, key=lambda row: row[:3])]
        query = _CITATION_REQUEST + "Recorded task requests (ordered by turn):\n" + json.dumps(requests, ensure_ascii=False)
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
        if (len({event.producer_id for event in turn_events if event.producer_id}) > 1
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
        elif any(event.provenance == "verdict:capture_limit" for event in turn_events):
            reason = "source_capture_partial"
        query = _CITATION_REQUEST + "Task and available tool contract:\n" + json.dumps({
            "task": turn.user_request_redacted,
            "contract": [{"reference": f"[event:{item.event_id}]", "text": item.attributes.get("text")}
                         for item in contracts],
        }, ensure_ascii=False)
        response = "Actual tool action and recorded result:\n" + json.dumps({
            "call": {"reference": f"[event:{call.event_id}]", "attributes": call.attributes},
            "results": [{"reference": f"[event:{item.event_id}]", "status": item.status.value,
                         "attributes": item.attributes} for item in results],
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
                "query": safe["query"], "response": safe["response"],
                "evidenceReferences": refs, "coverage": "complete" if reason is None else "partial",
                "unavailableReason": reason}
    return _finish_evidence(evidence)


def _plan(storage, tenant: str, config: dict[str, Any],
          storage_url: str | None = None) -> tuple[dict[str, Any], Any]:
    provider, model, max_calls, max_output, rubric = _validated_config({
        **config, "unit": "trace", "maxCalls": 1,
    })
    if max_calls != 1 or config.get("toolEvidence") is not None:
        raise ValueError("scoped evaluation supports one bounded call")
    evidence = _projection(storage, tenant, config, storage_url)
    identity = _judge(_IdentityOnlyProvider(provider), model, rubric, max_output).evaluator_identity(None)
    destination, destination_binding = _destination(provider, identity)
    identity_fp = hashlib.sha256(_canonical([
        _PROMPT_VERSION, config["scope"], identity["evaluator_fingerprint"], destination_binding,
    ])).hexdigest()
    fingerprint = hashlib.sha256(_canonical([
        tenant, evidence["evidenceFingerprint"], identity_fp,
    ])).hexdigest()
    eligible = evidence["coverage"] == "complete"
    input_estimate = math.ceil((len(evidence["query"]) + len(evidence["response"]) +
                                sum(len(d.description) for d in rubric.dimensions)) / 4) + 400
    output_max = _output_maximum(provider, model, max_output, 1) if eligible else 0
    preview = {
        "scope": config["scope"], "provider": provider, "model": model,
        "rubric": _rubric_summary(rubric, identity), "evaluatorFingerprint": identity_fp,
        "promptVersion": _PROMPT_VERSION, "planFingerprint": fingerprint,
        "plannedCalls": 1 if eligible else 0, "coverage": evidence["coverage"],
        "unavailableReason": evidence["unavailableReason"],
        "evidencePreview": evidence, "estimatedInputTokens": input_estimate if eligible else 0,
        "maximumOutputTokens": output_max,
        "estimatedMaximumCostUsd": compute_cost_usd(model, input_estimate, output_max)
        if eligible and provider != "jev" else None,
        "destination": destination, "externalEgressRequired": True,
    }
    preview["scheduledUpperStaticListPriceUsd"] = (
        _scheduled_upper_cost(preview, rubric) if provider in {"anthropic", "openai"} else None
    )
    return preview, rubric


def preview_scoped(storage, *, tenant: str, config: dict[str, Any],
                   storage_url: str | None = None) -> dict[str, Any]:
    return _plan(storage, tenant, config, storage_url)[0]


def execute_scoped(storage, *, storage_url: str, tenant: str, config: dict[str, Any],
                   confirm_external_egress: bool, provider=None) -> dict[str, Any]:
    if confirm_external_egress is not True:
        raise ValueError("external judge egress was not confirmed")
    with _LOCK:
        preview, rubric = _plan(storage, tenant, config, storage_url)
        if preview["plannedCalls"] != 1 or config.get("planFingerprint") != preview["planFingerprint"]:
            raise ValueError("scoped preview is no longer current")
        provider_name = preview["provider"]
        judge = _judge(provider or _provider(provider_name), preview["model"], rubric,
                       config.get("maxOutputTokens", 512))
        base_identity = judge.evaluator_identity(None)
        expected = _judge(_IdentityOnlyProvider(provider_name), preview["model"], rubric,
                          config.get("maxOutputTokens", 512)).evaluator_identity(None)
        if base_identity["evaluator_fingerprint"] != expected["evaluator_fingerprint"]:
            raise ValueError("judge provider behavior changed after preview")
        evidence = preview["evidencePreview"]
        try:
            scores = judge.score(query=evidence["query"], response=evidence["response"])
            allowed_refs = set(evidence["evidenceReferences"])
            dimensions = []
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
                    "explanation": (redact(score.reasoning) or "")[:2_000],
                    "evidenceReferences": cited,
                    "citationStatus": "linked" if cited else "uncited_opinion",
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
            "scope": evidence["scope"], "runId": evidence["runId"],
            "sessionId": evidence.get("sessionId"),
            "sourceKind": evidence.get("sourceKind"),
            "runIds": evidence.get("runIds"),
            "eventId": evidence["eventId"], "coverage": evidence["coverage"],
            "evidenceFingerprint": evidence["evidenceFingerprint"],
            "evidenceReferences": evidence["evidenceReferences"],
            "evaluatorFingerprint": preview["evaluatorFingerprint"],
            "provider": provider_name, "model": preview["model"],
            "rubric": preview["rubric"], "promptVersion": _PROMPT_VERSION,
            "status": status, "error": error, "dimensions": dimensions,
        }
        safe = redact_structure(result)
        if not isinstance(safe, dict):
            raise ValueError("scoped result redaction failed")
        document_id = hashlib.sha256(_canonical([
            tenant, preview["planFingerprint"], status,
        ])).hexdigest()
        store = ControlStore(storage_url)
        existing = store.history(tenant, "scoped_evaluation", document_id)
        if not existing:
            store.append(tenant, kind="scoped_evaluation", document_id=document_id,
                         state=status, payload=safe, expected_revision=None)
        return {"resultId": document_id, **safe}


def _scheduled_upper_cost(preview: dict[str, Any], rubric) -> float | None:
    """Conservative static list-price planning bound, never an invoice guarantee."""
    if preview["plannedCalls"] != 1:
        return None
    from verdict_eval.judge import SYSTEM_PROMPT, _user_prompt

    evidence = preview["evidencePreview"]
    prompt = _user_prompt(evidence["query"], evidence["response"], None, rubric)
    # A byte may become at most one text token in supported provider tokenizers;
    # the extra envelope allowance covers message framing and SDK metadata.
    input_bound = len(SYSTEM_PROMPT.encode()) + len(prompt.encode()) + 1_024
    return compute_cost_usd(preview["model"], input_bound, preview["maximumOutputTokens"])


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
    if (isinstance(max_estimated_usd, bool) or not isinstance(max_estimated_usd, (int, float))
            or not math.isfinite(max_estimated_usd) or not 0 < max_estimated_usd <= 100):
        raise ValueError("invalid scheduled estimated-spend limit")
    preview, rubric = _plan(storage, tenant, config, storage_url)
    if (preview["provider"] not in {"anthropic", "openai"}
            or preview["plannedCalls"] != 1
            or config.get("planFingerprint") != preview["planFingerprint"]):
        raise ValueError("scheduled evaluation requires a current bounded preview")
    upper_cost = _scheduled_upper_cost(preview, rubric)
    if upper_cost is None or upper_cost > max_estimated_usd:
        raise ValueError("scheduled evaluation exceeds estimated-spend limit")
    stored_config = {key: config[key] for key in (
        "scope", "provider", "model", "rubric",
    )}
    for key in ("runId", "eventId", "sessionId", "sourceKind"):
        if key in config:
            stored_config[key] = config[key]
    stored_config["maxOutput"] = config.get("maxOutputTokens", 512)
    document_id = hashlib.sha256(_canonical([tenant, preview["planFingerprint"], "once"])).hexdigest()
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


def run_scheduled_once(storage_url: str, *, tenant: str, provider=None) -> dict[str, Any]:
    """Consume at most one pending approval before any network call."""
    store = ControlStore(storage_url)
    pending = next((item for item in store.list_current(tenant)
                    if item["kind"] == "scheduled_evaluation" and item["state"] == "pending"), None)
    if pending is None:
        return {"status": "idle"}
    job_id = str(pending["documentId"])
    payload = dict(pending["payload"])
    config = _scheduled_config(payload)
    storage = None
    try:
        from verdict.client import _resolve_storage

        storage = _resolve_storage(storage_url)
        preview, rubric = _plan(storage, tenant, config, storage_url)
        upper_cost = _scheduled_upper_cost(preview, rubric)
        valid = (
            preview["plannedCalls"] == 1
            and preview["planFingerprint"] == payload.get("planFingerprint")
            and preview["destination"] == payload.get("destination")
            and preview["provider"] in {"anthropic", "openai"}
            and payload.get("maxCalls") == 1
            and upper_cost is not None
            and upper_cost <= payload.get("maxEstimatedUsd", -1)
        )
        if not valid:
            raise ValueError("scheduled evidence or authorization is stale")
    except (KeyError, TypeError, ValueError, UnicodeError):
        cancelled = store.append(
            tenant, kind="scheduled_evaluation", document_id=job_id,
            state="cancelled", payload={**payload, "outcome": "stale_or_invalid"},
            expected_revision=pending["revision"],
        )
        return {"jobId": job_id, "status": cancelled["state"], "reason": "stale_or_invalid"}
    finally:
        if storage is not None:
            storage.close()
    if provider is None:
        key_name = "ANTHROPIC_API_KEY" if preview["provider"] == "anthropic" else "OPENAI_API_KEY"
        if not os.environ.get(key_name):
            return {"jobId": job_id, "status": "waiting_provider"}
    spent = store.append(
        tenant, kind="scheduled_evaluation", document_id=job_id,
        state="spent", payload={**payload, "outcome": "attempt_reserved"},
        expected_revision=pending["revision"],
    )
    adapter = provider
    storage = None
    try:
        if adapter is None:
            from verdict_eval.providers import AnthropicAdapter, OpenAIAdapter

            adapter = (AnthropicAdapter(max_retries=1, sdk_max_retries=0)
                       if preview["provider"] == "anthropic"
                       else OpenAIAdapter(max_retries=1, sdk_max_retries=0))
        from verdict.client import _resolve_storage

        storage = _resolve_storage(storage_url)
        result = execute_scoped(
            storage, storage_url=storage_url, tenant=tenant,
            config={**config, "planFingerprint": payload["planFingerprint"]},
            confirm_external_egress=True, provider=adapter,
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
        return {"resultId": result_id, **history[0]["payload"]}

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
            for item in ControlStore(setup.storage_url).list_current(setup.tenant_id)
            if item["kind"] == "scheduled_evaluation"
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
        except (KeyError, TypeError, ValueError, UnicodeError):
            return JSONResponse({"error": "invalid or stale scoped evaluation"}, status_code=400)
        finally:
            storage.close()
