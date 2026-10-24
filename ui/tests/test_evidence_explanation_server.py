"""Selected evidence explanation through stored agent activity and one approved call."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from verdict.dashboard.app import create_app
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.evidence_explanation import (
    _cluster_draft,
    _rubric_draft,
    _setup_draft,
    preview_explanation,
    run_explanation,
)
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
)
from verdict.schema import SpanRecord, Trace
from verdict.storage import SQLiteStorage
from verdict_eval.cluster_registry import ClusterRegistryService
from verdict_eval.clustering_strategies import FitConfig
from verdict_eval.providers import CompletionResponse


def _store(path, *, tenant="local"):
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    session = SourceSession("source", tenant, "synthetic-agent", "a" * 64, now, now)
    run = AgentRun("run", "source", tenant, now, ExecutionStatus.COMPLETED, now,
                   agent_name="worker")
    turn = AgentTurn("turn", "run", 0, now, ExecutionStatus.COMPLETED, now,
                     "Find the answer", "Answer", EvidenceState.PRESENT, EvidenceState.PRESENT)
    event = AgentEvent("event", "turn", 0, now, AgentEventType.TOOL_CALL,
                       ExecutionStatus.FAILED, "test", {"tool_name": "lookup", "arguments":
                       {"nested": {"token": "sk-test-secret-canary"},
                        "payload": r'{"misc":"\u0073k-proj-AAAAAAAAAAAAAAAAAAAAAAAA","error":"schema mismatch"}'}},
                       PrivacyClassification.REDACTED)
    storage.replace_agent_run_bundle(AgentRunBundle(session, run, (turn,), (event,)))
    storage.insert_trace(Trace(trace_id="trace", tenant_id=tenant, provider="other-provider",
                               request_model="other-model", prompt_redacted="Find the answer",
                               response_redacted="Answer", started_at=now, ended_at=now,
                               input_tokens=10, output_tokens=5))
    return storage


class Provider:
    name = "anthropic"

    def __init__(self):
        self.calls = 0
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        return CompletionResponse(text="Observed: the selected tool failed. Suggested: inspect its input.",
                                  input_tokens=20, output_tokens=20, finish_reason="end_turn")


def test_turn_event_and_trace_previews_are_bounded_redacted_and_tenant_scoped(tmp_path):
    path = tmp_path / "explain.db"
    storage = _store(path)
    url = f"sqlite:///{path}"
    for selection in ({"scope": "turn", "runId": "run", "turnId": "turn"},
                      {"scope": "event", "runId": "run", "eventId": "event"},
                      {"scope": "trace", "traceId": "trace"}):
        preview = preview_explanation(storage, storage_url=url, tenant="local",
                                      config={**selection, "provider": "anthropic", "focus": "failure"})
        assert preview["plannedCalls"] == 1
        assert len(str(preview["evidencePreview"]).encode()) <= 8_000
        assert "sk-test-secret-canary" not in str(preview)
    with pytest.raises(KeyError):
        preview_explanation(storage, storage_url=url, tenant="other",
                            config={"scope": "trace", "traceId": "trace", "provider": "anthropic", "focus": "failure"})
    with pytest.raises(KeyError):
        preview_explanation(storage, storage_url=url, tenant="local",
                            config={"scope": "span", "traceId": "trace", "spanId": "unowned-span",
                                    "provider": "anthropic", "focus": "failure"})
    storage.close()


def test_selected_event_explanation_omits_internal_check_identity(tmp_path):
    path = tmp_path / "check-identity-explain.db"
    storage = _store(path)
    [bundle] = storage.list_agent_run_bundles("local")
    digest = hashlib.sha256(b"synthetic-check").hexdigest()
    event = AgentEvent("test-event", "turn", 1, bundle.events[0].occurred_at,
                       AgentEventType.TEST_RESULT, ExecutionStatus.FAILED,
                       "synthetic", {"failed": 1, "check_id_hash": digest})
    storage.replace_agent_run_bundle(replace(bundle, events=(*bundle.events, event)))
    preview = preview_explanation(storage, storage_url=f"sqlite:///{path}",
                                  tenant="local", config={"scope": "event",
                                  "runId": "run", "eventId": "test-event",
                                  "provider": "anthropic", "focus": "failure"})
    assert preview["plannedCalls"] == 1
    assert digest not in str(preview)
    assert "check_id_hash" not in str(preview)
    storage.close()


def test_explanation_preview_disallows_causes_from_redaction_or_terminal_status(tmp_path):
    path = tmp_path / "explain-instructions.db"
    storage = _store(path)
    preview = preview_explanation(
        storage, storage_url=f"sqlite:///{path}", tenant="local",
        config={"scope": "event", "runId": "run", "eventId": "event",
                "provider": "openai", "focus": "failure"},
    )
    system = preview["messages"][0]["content"].lower()
    assert preview["promptVersion"] == "selected-evidence-explanation-v3"
    assert "redacted" in system and "cause" in system
    assert "completed" in system and "success" in system
    assert "credential" not in system
    assert "sk-test-secret-canary" not in str(preview)
    storage.close()


def test_explanation_egress_omits_masked_fields_and_keeps_recorded_error(tmp_path):
    path = tmp_path / "projected-evidence.db"
    storage = _store(path)
    bundle = storage.get_agent_run_bundle("local", "run")
    event = replace(bundle.events[0], event_id="schema-event", sequence=1,
                    event_type=AgentEventType.TOOL_RESULT, attributes={
        "tool_name": "schema_validate", "is_error": True,
        "result": {"error": "schema version mismatch",
                   r"\u0073k-proj-GGGGGGGGGGGGGGGGGGGGGGGG": "x",
                   "api_key": "sk-test-secret-canary",
                   "arguments": json.dumps({"version": "v2", "nested": {"token": "sk-test-secret-canary"}}),
                   "escaped": r'{"misc":"\u0073k-proj-AAAAAAAAAAAAAAAAAAAAAAAA","mask":"\u003cSECRET\u003e","error":"schema mismatch"}',
                   "mixed": "diagnostic mixed with <SECRET>",
                   "items": ["safe", "<REDACTED>", {"key": "<SECRET:abcdef123456>", "code": "E_SCHEMA"}]},
    })
    storage.replace_agent_run_bundle(replace(bundle, events=(*bundle.events, event)))
    stored = storage.get_agent_run_bundle("local", "run")
    assert "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA" not in json.dumps(
        json.loads(stored.events[1].attributes["result"]["escaped"]))
    selection = {"scope": "event", "runId": "run", "eventId": "schema-event",
                 "provider": "anthropic", "focus": "failure"}
    preview = preview_explanation(storage, storage_url=f"sqlite:///{path}", tenant="local",
                                  config=selection)
    attributes = preview["evidencePreview"]["events"][1]["attributes"]["result"]
    assert preview["plannedCalls"] == 1
    assert attributes["error"] == "schema version mismatch"
    assert "api_key" not in attributes and "mixed" not in attributes
    assert not any("\\u0073k-proj" in key for key in attributes)
    assert json.loads(attributes["arguments"]) == {"version": "v2", "nested": {}}
    assert json.loads(attributes["escaped"]) == {"error": "schema mismatch"}
    assert attributes["items"] == ["safe", {"code": "E_SCHEMA"}]
    assert "<SECRET" not in str(preview) and "<REDACTED>" not in str(preview)
    assert "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA" not in json.dumps(preview)
    assert "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA" not in json.dumps(json.loads(attributes["escaped"]))
    provider = Provider()
    result = run_explanation(
        storage, storage_url=f"sqlite:///{path}", tenant="local",
        config={**selection, "planFingerprint": preview["planFingerprint"],
                "maxEstimatedUsd": 1}, confirm_external_egress=True,
        provider_adapter=provider,
    )
    assert result["status"] == "completed"
    assert provider.requests[0].messages == preview["messages"]
    assert "api_key" not in str(provider.requests[0].messages)
    assert "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA" not in json.dumps(provider.requests[0].messages)
    storage.close()


def test_explanation_does_not_call_ai_when_selected_diagnostic_is_only_masked_text(tmp_path):
    path = tmp_path / "masked-diagnostic.db"
    storage = _store(path)
    bundle = storage.get_agent_run_bundle("local", "run")
    event = replace(bundle.events[0], event_id="masked-result", sequence=1,
                    event_type=AgentEventType.TOOL_RESULT,
                    attributes={"tool_name": "lookup", "is_error": True,
                                "result": {"credential": "sk-test-secret-canary"}})
    storage.replace_agent_run_bundle(replace(bundle, events=(*bundle.events, event)))
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    storage.insert_trace(Trace(trace_id="masked-error", tenant_id="local", started_at=now,
                               ended_at=now, error="<SECRET>"))
    url = f"sqlite:///{path}"
    for selection in ({"scope": "event", "runId": "run", "eventId": "masked-result"},
                      {"scope": "trace", "traceId": "masked-error"}):
        preview = preview_explanation(storage, storage_url=url, tenant="local",
                                      config={**selection, "provider": "anthropic", "focus": "failure"})
        assert preview["plannedCalls"] == 0
        assert preview["evidencePreview"]["reason"] == "diagnostic_redacted"
    storage.close()


def test_explanation_does_not_treat_failure_flag_as_a_recorded_cause(tmp_path):
    path = tmp_path / "masked-error-with-flag.db"
    storage = _store(path)
    bundle = storage.get_agent_run_bundle("local", "run")
    event = replace(bundle.events[0], event_id="masked-error-result", sequence=1,
                    event_type=AgentEventType.TOOL_RESULT,
                    attributes={"tool_name": "lookup", "is_error": True,
                                "result": {"error": "api_key: <SECRET>", "ok": False}})
    storage.replace_agent_run_bundle(replace(bundle, events=(*bundle.events, event)))
    preview = preview_explanation(
        storage, storage_url=f"sqlite:///{path}", tenant="local",
        config={"scope": "event", "runId": "run", "eventId": "masked-error-result",
                "provider": "anthropic", "focus": "failure"},
    )
    assert preview["plannedCalls"] == 0
    assert preview["evidencePreview"]["reason"] == "diagnostic_redacted"
    storage.close()


def test_explanation_rechecks_cluster_and_session_evidence_after_masking(tmp_path, monkeypatch):
    storage = _store(tmp_path / "masked-draft.db")
    url = f"sqlite:///{tmp_path / 'masked-draft.db'}"
    monkeypatch.setattr("verdict.dashboard.app.build_registry_bundle", lambda *_args, **_kwargs: {
        "clusters": [{"clusterId": "masked", "kind": "semantic", "detailsAvailable": True,
                      "assignedCount": 12, "representatives": [
                          {"prompt": "<SECRET>"}, {"prompt": "<REDACTED>"}]}]})
    cluster = preview_explanation(storage, storage_url=url, tenant="local", config={
        "scope": "cluster", "versionId": "version", "clusterId": "masked",
        "provider": "anthropic", "focus": "cluster_description",
        "draftKind": "cluster_description"})
    assert cluster["plannedCalls"] == 0
    monkeypatch.setattr("verdict.dashboard.evidence_explanation._session_projection",
                        lambda *_args, **_kwargs: {"coverage": "complete", "query": "<SECRET>",
                                                  "response": "Synthetic response"})
    session = preview_explanation(storage, storage_url=url, tenant="local", config={
        "scope": "session", "sessionId": "source", "sourceKind": "synthetic-agent",
        "provider": "anthropic", "focus": "failure"})
    assert session["plannedCalls"] == 0
    storage.close()


def test_explanation_drops_embedded_unicode_escape_from_historical_trace(tmp_path):
    path = tmp_path / "historical-escaped.db"
    storage = _store(path)
    mixed = r'Here is data: {"misc":"\u0073k-proj-DDDDDDDDDDDDDDDDDDDDDDDD","error":"schema mismatch"}'
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    storage.insert_span(SpanRecord(span_id="old-span", trace_id="trace", tenant_id="local",
                                   started_at=now, ended_at=now, attributes={"safe": "x"}))
    with storage._conn:
        storage._conn.execute("UPDATE traces SET prompt_redacted=? WHERE trace_id=?", (mixed, "trace"))
        storage._conn.execute("UPDATE spans SET attributes_json=? WHERE span_id=?", (
            json.dumps({r"\u0073k-proj-GGGGGGGGGGGGGGGGGGGGGGGG": "x", "safe": "x"}),
            "old-span"))
    preview = preview_explanation(storage, storage_url=f"sqlite:///{path}", tenant="local",
                                  config={"scope": "trace", "traceId": "trace",
                                          "provider": "anthropic", "focus": "improvement"})
    assert "prompt" not in preview["evidencePreview"]["call"]
    assert "sk-proj-DDDDDDDDDDDDDDDDDDDDDDDD" not in json.dumps(preview["messages"])
    assert "\\u0073k-proj" not in json.dumps(preview["messages"])
    span_preview = preview_explanation(storage, storage_url=f"sqlite:///{path}", tenant="local",
                                       config={"scope": "span", "traceId": "trace", "spanId": "old-span",
                                               "provider": "anthropic", "focus": "improvement"})
    assert span_preview["evidencePreview"]["selectedSpan"]["attributes"] == {"safe": "x"}
    assert "\\u0073k-proj" not in json.dumps(span_preview["messages"])
    storage.close()


def test_explanation_result_fails_closed_on_encoded_provider_key(tmp_path):
    class EscapedReply:
        name = "anthropic"

        def complete(self, _request):
            return CompletionResponse(
                text=r'Possible issue: {"misc":"\u0073k-proj-EEEEEEEEEEEEEEEEEEEEEEEE"}',
                input_tokens=10, output_tokens=10, finish_reason="end_turn")

    path = tmp_path / "escaped-reply.db"
    storage = _store(path)
    url = f"sqlite:///{path}"
    selection = {"scope": "event", "runId": "run", "eventId": "event",
                 "provider": "anthropic", "focus": "failure"}
    preview = preview_explanation(storage, storage_url=url, tenant="local", config=selection)
    result = run_explanation(storage, storage_url=url, tenant="local",
                             config={**selection, "planFingerprint": preview["planFingerprint"],
                                     "maxEstimatedUsd": 1}, confirm_external_egress=True,
                             provider_adapter=EscapedReply())
    assert result["answer"] == "<REDACTED>"
    assert "sk-proj-EEEEEEEEEEEEEEEEEEEEEEEE" not in json.dumps(result)
    storage.close()


def test_event_explanation_marks_exact_selection_among_neighbors(tmp_path):
    path = tmp_path / "selected-event.db"
    storage = _store(path)
    bundle = storage.get_agent_run_bundle("local", "run")
    original = bundle.events[0]
    events = (original, *(replace(original, event_id=f"event-{index}", sequence=index)
                          for index in range(1, 5)))
    storage.replace_agent_run_bundle(replace(bundle, events=events))
    preview = preview_explanation(storage, storage_url=f"sqlite:///{path}", tenant="local",
                                  config={"scope": "event", "runId": "run", "eventId": "event-2",
                                          "provider": "anthropic", "focus": "failure"})
    rows = preview["evidencePreview"]["events"]
    assert len(rows) == 5
    assert [row["selected"] for row in rows] == [False, False, True, False, False]
    assert "event-2" not in str(preview["messages"])
    storage.close()


def test_setup_excludes_unfinished_internal_and_foreign_traces(tmp_path):
    path = tmp_path / "setup-eligibility.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    for trace_id, tenant, ended, workload in (
        ("judge", "local", now, "judge"),
        ("replay", "local", now, "paired_replay"),
        ("unfinished", "local", None, None),
        ("foreign", "other", now, None),
    ):
        storage.insert_trace(Trace(trace_id=trace_id, tenant_id=tenant,
                                   started_at=now, ended_at=ended,
                                   tags={"verdict.workload": workload} if workload else {}))
    config = {"scope": "setup", "provider": "anthropic", "focus": "setup"}
    url = f"sqlite:///{path}"
    preview = preview_explanation(storage, storage_url=url, tenant="local", config=config)
    assert preview["evidencePreview"]["eligibleChecks"] == []
    storage.insert_trace(Trace(trace_id="genuine", tenant_id="local", started_at=now, ended_at=now))
    refreshed = preview_explanation(storage, storage_url=url, tenant="local", config=config)
    assert refreshed["evidencePreview"]["eligibleChecks"] == ["model_calls"]
    assert refreshed["planFingerprint"] != preview["planFingerprint"]
    storage.close()


def test_partial_agent_evidence_and_unfinished_calls_cannot_trigger_ai_egress(tmp_path):
    path = tmp_path / "partial-explain.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    source = SourceSession("partial-source", "local", "synthetic-agent", "a" * 64, now, now)
    run = AgentRun("partial-run", "partial-source", "local", now, ExecutionStatus.UNKNOWN)
    turn = AgentTurn("partial-turn", "partial-run", 0, now, ExecutionStatus.UNKNOWN,
                     user_request_redacted="Synthetic request", request_state=EvidenceState.PRESENT)
    event = AgentEvent("partial-event", "partial-turn", 0, now, AgentEventType.TOOL_CALL,
                       ExecutionStatus.FAILED, "synthetic", {"tool_name": "lookup"},
                       PrivacyClassification.METADATA)
    storage.replace_agent_run_bundle(AgentRunBundle(source, run, (turn,), (event,)))
    storage.insert_trace(Trace(trace_id="unfinished", tenant_id="local", started_at=now,
                               prompt_redacted="Synthetic request", response_redacted="Synthetic response"))
    url = f"sqlite:///{path}"
    provider = Provider()
    for selection in ({"scope": "run", "runId": "partial-run"},
                      {"scope": "turn", "runId": "partial-run", "turnId": "partial-turn"},
                      {"scope": "event", "runId": "partial-run", "eventId": "partial-event"},
                      {"scope": "trace", "traceId": "unfinished"}):
        config = {**selection, "provider": "anthropic", "focus": "failure"}
        preview = preview_explanation(storage, storage_url=url, tenant="local", config=config)
        assert preview["plannedCalls"] == 0
        assert preview["evidencePreview"]["coverage"] == "partial"
        with pytest.raises(ValueError):
            run_explanation(storage, storage_url=url, tenant="local",
                            config={**config, "planFingerprint": preview["planFingerprint"],
                                    "maxEstimatedUsd": 1},
                            confirm_external_egress=True, provider_adapter=provider)
    assert provider.calls == 0
    storage.close()

def test_capture_limit_blocks_selected_event_explanation(tmp_path):
    path = tmp_path / "limited-explain.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    source = SourceSession("source", "local", "synthetic-agent", "a" * 64, now, now)
    run = AgentRun("run", "source", "local", now, ExecutionStatus.COMPLETED, now)
    turn = AgentTurn("turn", "run", 0, now, ExecutionStatus.COMPLETED, now,
                     "Synthetic request", "Synthetic answer", EvidenceState.PRESENT, EvidenceState.PRESENT)
    events = tuple(AgentEvent(f"event-{index}", "turn", index, now,
                              AgentEventType.CONTEXT, ExecutionStatus.COMPLETED,
                              "verdict:capture_limit" if index == 4 else "synthetic",
                              {"name": "synthetic"}, PrivacyClassification.METADATA)
                   for index in range(5))
    storage.replace_agent_run_bundle(AgentRunBundle(source, run, (turn,), events))
    preview = preview_explanation(storage, storage_url=f"sqlite:///{path}", tenant="local",
                                  config={"scope": "event", "runId": "run", "eventId": "event-0",
                                          "provider": "anthropic", "focus": "failure"})
    assert preview["plannedCalls"] == 0
    assert preview["evidencePreview"]["reason"] == "source_capture_partial"
    storage.close()


def test_capture_limit_on_prior_turn_blocks_later_selected_explanations(tmp_path):
    path = tmp_path / "later-turn-limited.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    source = SourceSession("source", "local", "synthetic-agent", "a" * 64, now, now)
    run = AgentRun("run", "source", "local", now, ExecutionStatus.COMPLETED, now)
    turns = tuple(AgentTurn(
        f"turn-{index}", "run", index, now + timedelta(seconds=index),
        ExecutionStatus.COMPLETED, now + timedelta(seconds=index + 1),
        "Synthetic request", "Synthetic answer", EvidenceState.PRESENT,
        EvidenceState.PRESENT,
    ) for index in range(3))
    events = (
        AgentEvent("early", "turn-0", 0, now, AgentEventType.CONTEXT,
                   ExecutionStatus.COMPLETED, "synthetic", {"name": "early"}),
        AgentEvent("limit", "turn-1", 0, now + timedelta(seconds=1),
                   AgentEventType.CONTEXT, ExecutionStatus.UNKNOWN,
                   "verdict:capture_limit", {"name": "source_events_omitted"}),
        AgentEvent("later", "turn-2", 0, now + timedelta(seconds=2),
                   AgentEventType.CONTEXT, ExecutionStatus.COMPLETED,
                   "synthetic", {"name": "later"}),
    )
    storage.replace_agent_run_bundle(AgentRunBundle(source, run, turns, events))
    url = f"sqlite:///{path}"
    base = {"runId": "run", "provider": "anthropic", "focus": "failure"}
    early = preview_explanation(storage, storage_url=url, tenant="local",
                                config={**base, "scope": "event", "eventId": "early"})
    assert early["plannedCalls"] == 1
    provider = Provider()
    for selection in ({"scope": "run"}, {"scope": "turn", "turnId": "turn-2"},
                      {"scope": "event", "eventId": "later"}):
        config = {**base, **selection}
        preview = preview_explanation(storage, storage_url=url, tenant="local", config=config)
        assert preview["plannedCalls"] == 0
        assert preview["evidencePreview"]["reason"] == "source_capture_partial"
        with pytest.raises(ValueError):
            run_explanation(storage, storage_url=url, tenant="local",
                            config={**config, "planFingerprint": preview["planFingerprint"],
                                    "maxEstimatedUsd": 1},
                            confirm_external_egress=True, provider_adapter=provider)
    assert provider.calls == 0
    storage.close()


def test_prior_turn_capture_limit_is_withheld_by_explanation_api(tmp_path):
    path = tmp_path / "prior-limit-api.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    source = SourceSession("source", "local", "synthetic-agent", "a" * 64, now, now)
    run = AgentRun("run", "source", "local", now, ExecutionStatus.COMPLETED, now)
    turns = tuple(AgentTurn(f"turn-{index}", "run", index, now,
                            ExecutionStatus.COMPLETED, now, "Request", "Answer",
                            EvidenceState.PRESENT, EvidenceState.PRESENT)
                  for index in range(2))
    marker = AgentEvent("limit", "turn-0", 0, now, AgentEventType.CONTEXT,
                        ExecutionStatus.UNKNOWN, "verdict:capture_limit",
                        {"name": "source_events_omitted"})
    storage.replace_agent_run_bundle(AgentRunBundle(source, run, turns, (marker,)))
    storage.close()
    async def preview_through_api():
        transport = httpx.ASGITransport(app=create_app(
            storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post("/api/explain/preview",
                                     headers={"X-Verdict-Setup": token},
                                     json={"scope": "turn", "runId": "run", "turnId": "turn-1",
                                           "provider": "anthropic", "focus": "failure"})

    response = asyncio.run(preview_through_api())
    assert response.status_code == 200
    assert response.json()["plannedCalls"] == 0
    assert response.json()["evidencePreview"]["reason"] == "source_capture_partial"


def test_explanation_api_requires_authorization_and_executes_exact_preview(tmp_path, monkeypatch):
    path = tmp_path / "api.db"
    storage = _store(path, tenant="__verdict_local__")
    storage.close()
    provider = Provider()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    monkeypatch.setattr("verdict_eval.providers.AnthropicAdapter", lambda **_kwargs: provider)
    app = create_app(storage=f"sqlite:///{path}")
    selection = {"scope": "event", "runId": "run", "eventId": "event",
                 "provider": "anthropic", "focus": "failure"}

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            denied = await client.post("/api/explain/preview", json=selection)
            assert denied.status_code == 403
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = await client.post("/api/explain/preview", headers=headers, json=selection)
            assert preview.status_code == 200
            assert preview.json()["plannedCalls"] == 1
            run = await client.post("/api/explain/run", headers=headers,
                                    json={**selection, "planFingerprint": preview.json()["planFingerprint"],
                                          "maxEstimatedUsd": 1, "confirmExternalEgress": True})
            assert run.status_code == 200
            assert run.json()["status"] == "completed"
            assert provider.calls == 1
            assert provider.requests[0].messages == preview.json()["messages"]
            assert "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA" not in json.dumps(provider.requests[0].messages)

    asyncio.run(exercise())


def test_explanation_requires_current_preview_budget_and_approval_then_replays(tmp_path):
    path = tmp_path / "approval.db"
    storage = _store(path)
    url = f"sqlite:///{path}"
    config = {"scope": "event", "runId": "run", "eventId": "event",
              "provider": "anthropic", "focus": "failure"}
    preview = preview_explanation(storage, storage_url=url, tenant="local", config=config)
    provider = Provider()
    approved = {**config, "planFingerprint": preview["planFingerprint"]}
    with pytest.raises(ValueError):
        run_explanation(storage, storage_url=url, tenant="local", config=approved,
                        confirm_external_egress=False, provider_adapter=provider)
    with pytest.raises(ValueError):
        run_explanation(storage, storage_url=url, tenant="local",
                        config={**approved, "maxEstimatedUsd": 0},
                        confirm_external_egress=True, provider_adapter=provider)
    assert provider.calls == 0
    approved["maxEstimatedUsd"] = 1
    first = run_explanation(storage, storage_url=url, tenant="local", config=approved,
                            confirm_external_egress=True, provider_adapter=provider)
    assert first["status"] == "completed"
    assert provider.calls == 1
    assert run_explanation(storage, storage_url=url, tenant="local", config=approved,
                           confirm_external_egress=True, provider_adapter=provider) == first
    assert provider.calls == 1
    assert ControlStore(url).history("local", "evidence_explanation", preview["planFingerprint"])
    storage.close()


def test_provider_failure_has_safe_actionable_result_and_remains_one_attempt(tmp_path):
    class RejectedProvider:
        name = "anthropic"
        calls = 0

        def complete(self, _request):
            self.calls += 1
            failure = RuntimeError("provider body with sk-test-secret-canary")
            failure.status_code = 400
            raise failure

    path = tmp_path / "rejected.db"
    storage = _store(path)
    url = f"sqlite:///{path}"
    config = {"scope": "trace", "traceId": "trace", "provider": "anthropic", "focus": "failure"}
    preview = preview_explanation(storage, storage_url=url, tenant="local", config=config)
    approved = {**config, "planFingerprint": preview["planFingerprint"], "maxEstimatedUsd": 1}
    provider = RejectedProvider()
    first = run_explanation(storage, storage_url=url, tenant="local", config=approved,
                            confirm_external_egress=True, provider_adapter=provider)
    assert first["status"] == "error"
    assert "HTTP 400" in first["error"] and "account usage limits" in first["error"]
    assert "sk-test-secret-canary" not in str(first)
    second = run_explanation(storage, storage_url=url, tenant="local", config=approved,
                             confirm_external_egress=True, provider_adapter=provider)
    assert second == first
    assert provider.calls == 1
    assert "sk-test-secret-canary" not in str(ControlStore(url).history(
        "local", "evidence_explanation", preview["planFingerprint"]))
    storage.close()


def test_provider_error_categories_never_persist_raw_messages():
    from verdict.dashboard.evidence_explanation import _safe_call_error

    for status, expected in ((401, "key"), (403, "permissions"),
                             (429, "rate or usage limit"), (503, "service failed")):
        failure = RuntimeError("sk-test-secret-canary")
        failure.status_code = status
        message = _safe_call_error(failure)
        assert expected in message
        assert "sk-test-secret-canary" not in message
    assert "sk-test-secret-canary" not in _safe_call_error(RuntimeError("sk-test-secret-canary"))


def test_setup_draft_uses_aggregate_eligibility_and_rejects_ineligible_choice(tmp_path):
    storage = _store(tmp_path / "setup.db")
    url = f"sqlite:///{tmp_path / 'setup.db'}"
    selection = {"scope": "setup", "provider": "anthropic", "focus": "setup"}
    preview = preview_explanation(storage, storage_url=url, tenant="local", config=selection)
    assert preview["evidencePreview"]["eligibleChecks"] == ["agent_runs", "model_calls"]
    assert "Find the answer" not in str(preview)
    assert "sk-test-secret-canary" not in str(preview)

    class DraftProvider(Provider):
        def complete(self, request):
            self.calls += 1
            return CompletionResponse(
                text='{"check":"model_calls","grouping":"none","intervalHours":24,"why":"Review captured calls."}',
                input_tokens=20, output_tokens=20, finish_reason="end_turn")

    provider = DraftProvider()
    result = run_explanation(storage, storage_url=url, tenant="local",
                             config={**selection, "planFingerprint": preview["planFingerprint"],
                                     "maxEstimatedUsd": 1},
                             confirm_external_egress=True, provider_adapter=provider)
    assert result["draft"]["check"] == "model_calls"
    assert provider.calls == 1
    storage.close()

    empty_path = tmp_path / "empty.db"
    empty = SQLiteStorage(str(empty_path))
    empty_url = f"sqlite:///{empty_path}"
    empty_preview = preview_explanation(empty, storage_url=empty_url, tenant="local", config=selection)
    assert empty_preview["evidencePreview"]["eligibleChecks"] == []
    rejected = run_explanation(empty, storage_url=empty_url, tenant="local",
                               config={**selection, "planFingerprint": empty_preview["planFingerprint"],
                                       "maxEstimatedUsd": 1},
                               confirm_external_egress=True, provider_adapter=DraftProvider())
    assert rejected["status"] == "error"
    empty.close()


def test_cohort_draft_requires_genuine_calls_and_validates_reviewed_thresholds(tmp_path):
    path = tmp_path / "cohort.db"
    storage = _store(path)
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    for index in range(1, 60):
        storage.insert_trace(Trace(trace_id=f"cohort-{index}", tenant_id="local",
                                   started_at=now, ended_at=now))
    url = f"sqlite:///{path}"
    config = {"scope": "setup", "provider": "anthropic", "focus": "setup", "draftKind": "cohort"}
    preview = preview_explanation(storage, storage_url=url, tenant="local", config=config)
    assert preview["plannedCalls"] == 1
    assert preview["evidencePreview"]["eligibleCallCount"] == 60
    assert "Find the answer" not in str(preview)

    class CohortProvider(Provider):
        def complete(self, request):
            self.calls += 1
            return CompletionResponse(
                text='{"grouping":"none","referenceShare":50,"minimumReference":30,'
                     '"minimumCurrent":30,"prospectiveTarget":30,"why":"Start broadly."}',
                input_tokens=20, output_tokens=20, finish_reason="end_turn")

    provider = CohortProvider()
    result = run_explanation(storage, storage_url=url, tenant="local",
                             config={**config, "planFingerprint": preview["planFingerprint"],
                                     "maxEstimatedUsd": 1},
                             confirm_external_egress=True, provider_adapter=provider)
    assert result["draft"] == {"grouping": "none", "referenceShare": 50,
                               "minimumReference": 30, "minimumCurrent": 30,
                               "prospectiveTarget": 30, "why": "Start broadly."}
    assert provider.calls == 1
    storage.close()

    empty_path = tmp_path / "no-calls.db"
    empty = SQLiteStorage(str(empty_path))
    unavailable = preview_explanation(empty, storage_url=f"sqlite:///{empty_path}",
                                      tenant="local", config=config)
    assert unavailable["plannedCalls"] == 0
    with pytest.raises(ValueError):
        run_explanation(empty, storage_url=f"sqlite:///{empty_path}", tenant="local",
                        config={**config, "planFingerprint": unavailable["planFingerprint"],
                                "maxEstimatedUsd": 1},
                        confirm_external_egress=True, provider_adapter=CohortProvider())
    empty.close()


def test_cohort_draft_rejects_unready_split_and_unsupported_grouping():
    evidence = {"eligibleCallCount": 60}
    valid = {"grouping": "none", "referenceShare": 50, "minimumReference": 30,
             "minimumCurrent": 30, "prospectiveTarget": 30, "why": "Start broadly."}
    assert _setup_draft(json.dumps(valid), evidence, "cohort") == valid
    for candidate in ({**valid, "referenceShare": 80},
                      {**valid, "grouping": "provider_model"},
                      {**valid, "minimumCurrent": 31},
                      {**valid, "referenceShare": True}):
        with pytest.raises(ValueError):
            _setup_draft(json.dumps(candidate), evidence, "cohort")


def test_rubric_draft_uses_selected_bounded_evidence_and_validates_one_dimension(tmp_path):
    path = tmp_path / "rubric.db"
    storage = _store(path)
    config = {"scope": "turn", "runId": "run", "turnId": "turn", "provider": "anthropic",
              "focus": "rubric", "draftKind": "rubric"}
    url = f"sqlite:///{path}"
    preview = preview_explanation(storage, storage_url=url, tenant="local", config=config)
    assert preview["plannedCalls"] == 1
    assert "Find the answer" in str(preview["messages"])
    assert "sk-test-secret-canary" not in str(preview)
    assert "Do not infer hidden reasoning" in preview["messages"][0]["content"]
    with pytest.raises(KeyError):
        preview_explanation(storage, storage_url=url, tenant="other", config=config)
    with pytest.raises(ValueError):
        preview_explanation(storage, storage_url=url, tenant="local",
                            config={**config, "scope": "setup", "focus": "setup"})
    valid = {"name": "response_relevance", "description": "The response addresses the request.",
             "why": "This is visible in the selected turn."}
    assert _rubric_draft(json.dumps(valid)) == valid
    for candidate in ({**valid, "name": "Response Relevance"},
                      {**valid, "description": " "},
                      {**valid, "extra": "ignored"}):
        with pytest.raises(ValueError):
            _rubric_draft(json.dumps(candidate))

    class RubricProvider(Provider):
        def complete(self, request):
            self.calls += 1
            return CompletionResponse(text=json.dumps(valid), input_tokens=20,
                                      output_tokens=20, finish_reason="end_turn")

    provider = RubricProvider()
    result = run_explanation(storage, storage_url=url, tenant="local",
                             config={**config, "planFingerprint": preview["planFingerprint"],
                                     "maxEstimatedUsd": 1},
                             confirm_external_egress=True, provider_adapter=provider)
    assert result["draft"] == valid
    assert provider.calls == 1
    storage.close()


def test_cluster_draft_is_bound_to_tenant_version_and_bounded_representatives(tmp_path):
    path = tmp_path / "cluster.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    for index in range(10):
        storage.insert_trace(Trace(trace_id=f"billing-{index}", tenant_id="local",
                                   started_at=now - timedelta(minutes=2),
                                   ended_at=now - timedelta(minutes=1),
                                   prompt_redacted="Review a safe billing request",
                                   tags={"verdict.workload": "agent", "verdict.intent_key": "billing"}))
    service = ClusterRegistryService(storage)
    version = service.fit("local", actor="test", strategy="explicit", cutoff=now,
                          config=FitConfig(strategy="explicit", target_workload="agent"))
    service.assign("local", version.version_id, through_cutoff=now)
    from verdict.dashboard.app import build_registry_bundle
    registry = build_registry_bundle(f"sqlite:///{path}", tenant="local", version_id=version.version_id)
    cluster_id = registry["clusters"][0]["clusterId"]
    config = {"scope": "cluster", "versionId": version.version_id, "clusterId": cluster_id,
              "provider": "anthropic", "focus": "cluster_description", "draftKind": "cluster_description"}
    url = f"sqlite:///{path}"
    preview = preview_explanation(storage, storage_url=url, tenant="local", config=config)
    assert preview["plannedCalls"] == 1
    assert len(preview["evidencePreview"]["representativePrompts"]) <= 3
    assert "Review a safe billing request" in str(preview["messages"])
    with pytest.raises(KeyError):
        preview_explanation(storage, storage_url=url, tenant="other", config=config)
    with pytest.raises(KeyError):
        preview_explanation(storage, storage_url=url, tenant="local",
                            config={**config, "clusterId": "different"})
    valid = {"displayName": "Billing questions", "description": "Requests about billing.",
             "why": "Observed in three representatives."}
    assert _cluster_draft(json.dumps(valid)) == valid
    for candidate in ({**valid, "displayName": " "}, {**valid, "description": "x" * 241},
                      {**valid, "extra": "ignore"}):
        with pytest.raises(ValueError):
            _cluster_draft(json.dumps(candidate))
    storage.close()


def test_span_list_and_explanation_require_explicit_matching_tenant(tmp_path):
    path = tmp_path / "spans.db"
    storage = _store(path)
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    storage.insert_trace(replace(storage.get_trace("trace"), parent_span_id="parent"))
    storage.insert_span(SpanRecord(span_id="parent", name="automatic parent", trace_id=None,
                                   tenant_id="local", started_at=now, ended_at=now))
    for span_id, tenant in (("owned", "local"), ("forged", "other"), ("legacy", None)):
        storage.insert_span(SpanRecord(span_id=span_id, name="synthetic step", trace_id="trace",
                                       tenant_id=tenant, started_at=now, ended_at=now,
                                       attributes={"token": "sk-test-secret-canary"}))
    config = {"scope": "span", "traceId": "trace", "spanId": "owned",
              "provider": "anthropic", "focus": "failure"}
    url = f"sqlite:///{path}"
    preview = preview_explanation(storage, storage_url=url, tenant="local", config=config)
    assert preview["plannedCalls"] == 1
    assert preview["evidencePreview"]["selectedSpan"]["name"] == "synthetic step"
    assert "sk-test-secret-canary" not in str(preview)
    parent_preview = preview_explanation(storage, storage_url=url, tenant="local",
                                         config={**config, "spanId": "parent"})
    assert parent_preview["evidencePreview"]["selectedSpan"]["name"] == "automatic parent"
    for span_id in ("forged", "legacy"):
        with pytest.raises(KeyError):
            preview_explanation(storage, storage_url=url, tenant="local",
                                config={**config, "spanId": span_id})
    storage.close()

    async def exercise():
        app = create_app(storage=url, tenant_id="local")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            assert (await client.get("/api/explain/spans/trace")).status_code == 403
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            response = await client.get("/api/explain/spans/trace",
                                        headers={"X-Verdict-Setup": token})
            assert response.status_code == 200
            assert [span["spanId"] for span in response.json()["spans"]] == ["parent", "owned"]
            assert "sk-test-secret-canary" not in str(response.json())

    asyncio.run(exercise())


def test_local_trace_explanation_accepts_empty_tenant_legacy_trace(tmp_path):
    tenant = "__verdict_local__"
    path = tmp_path / "legacy-empty-tenant-trace.db"
    storage = _store(path, tenant=tenant)
    storage.insert_trace(replace(storage.get_trace("trace"), trace_id="legacy", tenant_id=""))
    preview = preview_explanation(
        storage, storage_url=f"sqlite:///{path}", tenant=tenant,
        config={"scope": "trace", "traceId": "legacy", "provider": "anthropic", "focus": "failure"},
    )
    assert preview["plannedCalls"] == 1
    storage.close()

    async def exercise():
        app = create_app(storage=f"sqlite:///{path}", tenant_id=tenant)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.get("/api/explain/spans/legacy",
                                    headers={"X-Verdict-Setup": token})

    response = asyncio.run(exercise())
    assert response.status_code == 200
