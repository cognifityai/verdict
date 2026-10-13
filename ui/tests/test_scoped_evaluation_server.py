"""Scoped run and tool evaluation via real SQLite/API with a mocked provider."""

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from verdict.dashboard.app import create_app
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.scoped_evaluation import (
    cancel_scheduled_once,
    execute_scoped,
    preview_scoped,
    run_scheduled_once,
    schedule_scoped_once,
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
from verdict.storage import SQLiteStorage
from verdict_eval.providers import CompletionResponse


def _config(scope="run"):
    return {"scope": scope, "runId": "run-a", "provider": "anthropic",
            "model": "claude-haiku-4-5", "maxOutputTokens": 256,
            "rubric": {"name": "task_review", "version": "1", "dimensions": [
                {"name": "completion", "description": "Did the recorded work meet the stated task?"},
            ]}, **({"eventId": "call"} if scope == "tool_action" else {})}


def _bundle(tenant="local"):
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    turn = AgentTurn("turn-a", "run-a", 0, now, ExecutionStatus.COMPLETED, now,
                     "Read the requested file", "I read the file.",
                     EvidenceState.PRESENT, EvidenceState.PRESENT)
    def event(event_id, sequence, kind, attributes, status=ExecutionStatus.COMPLETED,
              privacy=PrivacyClassification.REDACTED):
        return AgentEvent(event_id, "turn-a", sequence, now, kind, status,
                          "synthetic", attributes, privacy)
    return AgentRunBundle(
        SourceSession(f"source-{tenant}", tenant, "synthetic-agent", "a" * 64, now, now),
        AgentRun("run-a", f"source-{tenant}", tenant, now, ExecutionStatus.COMPLETED, now),
        (turn,), (
            event("contract", 0, AgentEventType.INSTRUCTION,
                  {"name": "tool_contract", "text": "read_file(path) reads one file"}),
            event("call", 1, AgentEventType.TOOL_CALL,
                  {"tool_name": "read_file", "call_id": "call-1",
                   "arguments": {"path": "requested.txt"}}),
            event("result", 2, AgentEventType.TOOL_RESULT,
                  {"tool_name": "read_file", "call_id": "call-1", "result": "contents",
                   "is_error": False}),
        ),
    )


class MockProvider:
    name = "anthropic"
    supports_temperature = True

    def __init__(self, citation="[event:call]"):
        self.calls = 0
        self.citation = citation

    def complete(self, request):
        self.calls += 1
        return CompletionResponse(text=(
            '{"completion":{"reasoning":"' + self.citation +
            ' shows the selected action","verdict":"PASS"}}'
        ), input_tokens=10, output_tokens=10)


@pytest.mark.parametrize("variant", ["missing_arguments", "blank_contract", "duplicate_call", "redacted_arguments"])
def test_tool_action_withholds_judging_when_captured_evidence_is_incomplete(tmp_path, variant):
    bundle = _bundle()
    contract, call, result = bundle.events
    if variant == "missing_arguments":
        call = replace(call, attributes={"tool_name": "read_file", "call_id": "call-1"})
    elif variant == "blank_contract":
        contract = replace(contract, attributes={"name": "tool_contract", "text": "  "})
    elif variant == "duplicate_call":
        duplicate = replace(call, event_id="duplicate-call", sequence=2)
        result = replace(result, sequence=3)
        bundle = replace(bundle, events=(contract, call, duplicate, result))
    elif variant == "redacted_arguments":
        call = replace(call, attributes={**call.attributes, "arguments": {"path": "<REDACTED>"}})
    if variant != "duplicate_call":
        bundle = replace(bundle, events=(contract, call, result))
    path = tmp_path / f"{variant}.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(bundle)
    config = _config("tool_action")
    preview = preview_scoped(storage, tenant="local", config=config)
    assert preview["coverage"] == "partial"
    assert preview["plannedCalls"] == 0
    provider = MockProvider()
    with pytest.raises(ValueError):
        execute_scoped(storage, storage_url=url, tenant="local",
                       config={**config, "planFingerprint": preview["planFingerprint"]},
                       confirm_external_egress=True, provider=provider)
    assert provider.calls == 0
    assert not [row for row in ControlStore(url).list_current("local")
                if row["kind"] == "scoped_evaluation"]
    storage.close()


def test_explicit_empty_tool_arguments_remain_eligible(tmp_path):
    bundle = _bundle()
    call = replace(bundle.events[1], attributes={**bundle.events[1].attributes, "arguments": {}})
    path = tmp_path / "empty-arguments.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(replace(bundle, events=(bundle.events[0], call, bundle.events[2])))
    preview = preview_scoped(storage, tenant="local", config=_config("tool_action"))
    assert preview["coverage"] == "complete"
    assert preview["plannedCalls"] == 1
    storage.close()


def test_tool_action_requires_prior_contract_and_exposes_wrong_arguments(tmp_path):
    path = tmp_path / "tool-context.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    bundle = _bundle()
    wrong = replace(bundle.events[1], attributes={
        "tool_name": "read_file", "call_id": "call-1", "arguments": {"path": "wrong.txt"},
    })
    storage.replace_agent_run_bundle(replace(bundle, events=(bundle.events[0], wrong, bundle.events[2])))
    config = _config("tool_action")
    preview = preview_scoped(storage, tenant="local", config=config)
    assert "wrong.txt" in preview["evidencePreview"]["response"]
    assert "requested.txt" not in preview["evidencePreview"]["response"]
    assert preview["plannedCalls"] == 1

    class RejectingProvider(MockProvider):
        def complete(self, request):
            self.calls += 1
            return CompletionResponse(text=(
                '{"completion":{"reasoning":"[event:call] used the wrong path",'
                '"verdict":"FAIL"}}'
            ))

    provider = RejectingProvider()
    try:
        execute_scoped(storage, storage_url=url, tenant="local",
                       config={**config, "planFingerprint": preview["planFingerprint"]},
                       confirm_external_egress=False, provider=provider)
    except ValueError as exc:
        assert "not confirmed" in str(exc)
    else:
        raise AssertionError("egress denial was ignored")
    assert provider.calls == 0
    result = execute_scoped(storage, storage_url=url, tenant="local",
                            config={**config, "planFingerprint": preview["planFingerprint"]},
                            confirm_external_egress=True, provider=provider)
    assert provider.calls == 1
    assert result["dimensions"][0]["verdict"] == "fail"
    storage.close()

    later = replace(bundle.events[0], sequence=2, event_id="late-contract")
    late_bundle = replace(bundle, events=(replace(wrong, sequence=0),
                                          replace(bundle.events[2], sequence=1), later))
    path2 = tmp_path / "late.db"
    second = SQLiteStorage(str(path2))
    second.replace_agent_run_bundle(late_bundle)
    late_preview = preview_scoped(second, tenant="local", config=config)
    assert late_preview["plannedCalls"] == 0
    assert late_preview["unavailableReason"] == "tool_contract_or_task_unavailable"
    second.close()


def test_large_and_partial_runs_are_not_sent_to_judge(tmp_path):
    path = tmp_path / "partial.db"
    storage = SQLiteStorage(str(path))
    bundle = _bundle()
    storage.replace_agent_run_bundle(replace(bundle, turns=(replace(
        bundle.turns[0], response_truncated=True,
    ),)))
    preview = preview_scoped(storage, tenant="local", config=_config())
    assert preview["plannedCalls"] == 0
    assert preview["unavailableReason"] == "turn_content_partial"
    storage.close()

    large_path = tmp_path / "large.db"
    large = SQLiteStorage(str(large_path))
    large.replace_agent_run_bundle(replace(bundle, turns=(replace(
        bundle.turns[0], final_response_redacted="x" * 9_000,
    ),)))
    too_large = preview_scoped(large, tenant="local", config=_config())
    assert too_large["plannedCalls"] == 0
    assert too_large["unavailableReason"] == "evidence_limit"
    large.close()

    session_path = tmp_path / "large-session.db"
    session_storage = SQLiteStorage(str(session_path))
    many = tuple(replace(bundle.turns[0], turn_id=f"turn-{index}", sequence=index)
                 for index in range(21))
    session_storage.replace_agent_run_bundle(replace(
        bundle, run=replace(bundle.run, session_id="large-session"),
        turns=many, events=(),
    ))
    session_config = {**_config(), "scope": "session", "sessionId": "large-session",
                      "sourceKind": "synthetic-agent"}
    session_config.pop("runId")
    preview = preview_scoped(session_storage, tenant="local", config=session_config,
                             storage_url=f"sqlite:///{session_path}")
    assert preview["plannedCalls"] == 0
    assert preview["unavailableReason"] == "turn_limit"
    session_storage.close()


def test_scoped_run_and_tool_previews_bind_ordered_evidence_and_store_results(tmp_path):
    path = tmp_path / "scoped.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle())
    for scope in ("run", "tool_action"):
        config = _config(scope)
        preview = preview_scoped(storage, tenant="local", config=config)
        assert preview["plannedCalls"] == 1
        assert "requested.txt" in preview["evidencePreview"]["response"]
        if scope == "run":
            transcript = preview["evidencePreview"]["response"]
            assert transcript.index("[event:contract]") < transcript.index("I read the file.")
        assert preview["evidencePreview"]["coverage"] == "complete"
        provider = MockProvider()
        result = execute_scoped(storage, storage_url=url, tenant="local",
                                config={**config, "planFingerprint": preview["planFingerprint"]},
                                confirm_external_egress=True, provider=provider)
        assert provider.calls == 1
        assert result["status"] == "completed"
        assert result["dimensions"][0]["evidenceReferences"] == ["event:call"]
        assert ControlStore(url).history("local", "scoped_evaluation", result["resultId"])
    storage.close()


def test_session_evaluation_covers_two_runs_and_rejects_stale_append(tmp_path):
    path = tmp_path / "session-evaluation.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    first = _bundle()
    first = replace(first, run=replace(first.run, session_id="shared"))
    storage.replace_agent_run_bundle(first)
    second = replace(
        first,
        session=replace(first.session, source_session_id="source-two",
                        source_locator_hash=hashlib.sha256(b"two").hexdigest()),
        run=replace(first.run, run_id="run-b", source_session_id="source-two"),
        turns=(replace(first.turns[0], run_id="run-b", turn_id="turn-b",
                       user_request_redacted="Check the second file"),),
        events=tuple(replace(event, event_id=f"b-{event.event_id}", turn_id="turn-b")
                     for event in first.events),
    )
    storage.replace_agent_run_bundle(second)
    config = {**_config(), "scope": "session", "sessionId": "shared",
              "sourceKind": "synthetic-agent"}
    config.pop("runId")
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    assert preview["plannedCalls"] == 1
    assert preview["evidencePreview"]["runIds"] == ["run-a", "run-b"]
    assert "[turn:run-b/turn-b]" in preview["evidencePreview"]["query"]
    session_timeline = preview["evidencePreview"]["response"]
    assert session_timeline.index("[event:run-a/contract]") < session_timeline.index("[event:run-a/call]")
    storage.replace_agent_run_bundle(replace(
        second, turns=(replace(second.turns[0], final_response_redacted="I read the file. More detail."),),
    ))
    provider = MockProvider("[event:run-a/call]")
    try:
        execute_scoped(storage, storage_url=url, tenant="local",
                       config={**config, "planFingerprint": preview["planFingerprint"]},
                       confirm_external_egress=True, provider=provider)
    except ValueError as exc:
        assert "no longer current" in str(exc)
    else:
        raise AssertionError("stale session preview was accepted")
    assert provider.calls == 0
    fresh = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    result = execute_scoped(storage, storage_url=url, tenant="local",
                            config={**config, "planFingerprint": fresh["planFingerprint"]},
                            confirm_external_egress=True, provider=provider)
    assert provider.calls == 1
    assert result["status"] == "completed"
    assert result["scope"] == "session"
    assert result["runIds"] == ["run-a", "run-b"]
    storage.close()


def test_scheduled_evaluation_consumes_one_approved_attempt_and_never_retries(tmp_path, monkeypatch):
    path = tmp_path / "scheduled-scoped.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle())
    config = _config()
    preview = preview_scoped(storage, tenant="local", config=config)
    assert preview["scheduledUpperStaticListPriceUsd"] is not None
    approved = {**config, "planFingerprint": preview["planFingerprint"]}
    try:
        schedule_scoped_once(storage, storage_url=url, tenant="local", config=approved,
                             max_estimated_usd=1, confirm_persistent_egress=False)
    except ValueError:
        pass
    else:
        raise AssertionError("persistent egress was scheduled without consent")
    try:
        schedule_scoped_once(storage, storage_url=url, tenant="local", config=approved,
                             max_estimated_usd=0.000001, confirm_persistent_egress=True)
    except ValueError:
        pass
    else:
        raise AssertionError("estimated-spend limit was ignored")
    job = schedule_scoped_once(storage, storage_url=url, tenant="local", config=approved,
                               max_estimated_usd=1, confirm_persistent_egress=True)
    assert job["state"] == "pending"
    try:
        schedule_scoped_once(storage, storage_url=url, tenant="local", config=approved,
                             max_estimated_usd=1, confirm_persistent_egress=True)
    except ValueError:
        pass
    else:
        raise AssertionError("same plan received a second approval")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert run_scheduled_once(url, tenant="local")["status"] == "waiting_provider"
    assert ControlStore(url).history("local", "scheduled_evaluation", job["jobId"])[0]["state"] == "pending"
    provider = MockProvider()
    result = run_scheduled_once(url, tenant="local", provider=provider)
    assert result["status"] == "completed"
    assert provider.calls == 1
    assert run_scheduled_once(url, tenant="local", provider=provider)["status"] == "idle"
    assert provider.calls == 1
    history = ControlStore(url).history("local", "scheduled_evaluation", job["jobId"])
    assert [item["state"] for item in history] == ["spent", "spent", "pending"]
    assert history[0]["payload"]["outcome"] == "completed"
    assert history[0]["payload"]["resultId"] == result["resultId"]
    async def read_saved():
        visible = httpx.ASGITransport(app=create_app(storage=url, tenant_id="local"))
        hidden = httpx.ASGITransport(app=create_app(storage=url, tenant_id="other"))
        async with httpx.AsyncClient(transport=visible, base_url="http://testserver") as client:
            job_list = await client.get("/api/scoped/scheduled")
            saved = await client.get(f"/api/scoped/result/{result['resultId']}")
        async with httpx.AsyncClient(transport=hidden, base_url="http://testserver") as client:
            other = await client.get(f"/api/scoped/result/{result['resultId']}")
        return job_list, saved, other

    job_list, saved, other = asyncio.run(read_saved())
    assert job_list.status_code == 200
    assert job_list.json()["jobs"][0]["resultId"] == result["resultId"]
    assert saved.status_code == 200
    assert saved.json()["dimensions"][0]["citationStatus"] == "linked"
    assert other.status_code == 404
    try:
        ControlStore(url).rollback("local", kind="scheduled_evaluation",
                                   document_id=job["jobId"], target_revision=1,
                                   expected_revision=3)
    except ValueError:
        pass
    else:
        raise AssertionError("spent authorization was rearmed")
    storage.close()


def test_scheduled_evaluation_stale_cancel_and_cross_tenant_are_call_free(tmp_path):
    path = tmp_path / "scheduled-stale.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    bundle = _bundle()
    storage.replace_agent_run_bundle(bundle)
    config = _config()
    preview = preview_scoped(storage, tenant="local", config=config)
    schedule_scoped_once(storage, storage_url=url, tenant="local",
                         config={**config, "planFingerprint": preview["planFingerprint"]},
                         max_estimated_usd=1, confirm_persistent_egress=True)
    provider = MockProvider()
    assert run_scheduled_once(url, tenant="other", provider=provider)["status"] == "idle"
    storage.replace_agent_run_bundle(replace(
        bundle, turns=(replace(bundle.turns[0], final_response_redacted="I read the file. Then checked it."),),
    ))
    result = run_scheduled_once(url, tenant="local", provider=provider)
    assert result["status"] == "cancelled"
    assert provider.calls == 0
    assert run_scheduled_once(url, tenant="local", provider=provider)["status"] == "idle"
    fresh = preview_scoped(storage, tenant="local", config=config)
    second = schedule_scoped_once(storage, storage_url=url, tenant="local",
                                  config={**config, "planFingerprint": fresh["planFingerprint"]},
                                  max_estimated_usd=1, confirm_persistent_egress=True)
    assert cancel_scheduled_once(url, tenant="local", job_id=second["jobId"],
                                 expected_revision=second["revision"])["state"] == "cancelled"
    assert run_scheduled_once(url, tenant="local", provider=provider)["status"] == "idle"
    assert provider.calls == 0
    storage.close()


def test_scheduled_evaluation_crash_after_reservation_is_not_retried(tmp_path):
    path = tmp_path / "scheduled-crash.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle())
    config = _config()
    preview = preview_scoped(storage, tenant="local", config=config)
    job = schedule_scoped_once(storage, storage_url=url, tenant="local",
                               config={**config, "planFingerprint": preview["planFingerprint"]},
                               max_estimated_usd=1, confirm_persistent_egress=True)

    class InterruptingProvider(MockProvider):
        def complete(self, request):
            self.calls += 1
            raise KeyboardInterrupt("simulated process interruption")

    provider = InterruptingProvider()
    try:
        run_scheduled_once(url, tenant="local", provider=provider)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("simulated interruption did not reach worker")
    assert provider.calls == 1
    assert ControlStore(url).history("local", "scheduled_evaluation", job["jobId"])[0]["state"] == "spent"
    assert run_scheduled_once(url, tenant="local", provider=provider)["status"] == "idle"
    assert provider.calls == 1
    storage.close()


def test_scheduled_provider_error_links_terminal_job_to_saved_error(tmp_path):
    path = tmp_path / "scheduled-error.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle())
    config = _config()
    preview = preview_scoped(storage, tenant="local", config=config)
    job = schedule_scoped_once(
        storage, storage_url=url, tenant="local",
        config={**config, "planFingerprint": preview["planFingerprint"]},
        max_estimated_usd=1, confirm_persistent_egress=True,
    )

    class FailingProvider(MockProvider):
        def complete(self, request):
            self.calls += 1
            raise RuntimeError("synthetic provider failure")

    provider = FailingProvider()
    result = run_scheduled_once(url, tenant="local", provider=provider)
    assert result["status"] == "error"
    assert provider.calls == 1
    latest = ControlStore(url).history("local", "scheduled_evaluation", job["jobId"])[0]
    assert latest["state"] == "spent"
    assert latest["payload"]["outcome"] == "error"
    assert latest["payload"]["resultId"] == result["resultId"]
    assert latest["payload"]["error"] == "RuntimeError"
    assert run_scheduled_once(url, tenant="local", provider=provider)["status"] == "idle"
    assert provider.calls == 1
    storage.close()


def test_scoped_evaluation_rejects_stale_and_fabricated_references(tmp_path):
    path = tmp_path / "stale.db"
    storage = SQLiteStorage(str(path))
    bundle = _bundle()
    storage.replace_agent_run_bundle(bundle)
    config = _config()
    preview = preview_scoped(storage, tenant="local", config=config)
    storage.replace_agent_run_bundle(replace(bundle, turns=(replace(
        bundle.turns[0], final_response_redacted="I read the file. More detail.",
    ),)))
    try:
        execute_scoped(storage, storage_url=f"sqlite:///{path}", tenant="local",
                       config={**config, "planFingerprint": preview["planFingerprint"]},
                       confirm_external_egress=True, provider=MockProvider())
    except ValueError as exc:
        assert "no longer current" in str(exc)
    else:
        raise AssertionError("stale preview was accepted")
    current = preview_scoped(storage, tenant="local", config=config)
    provider = MockProvider("[event:fabricated]")
    result = execute_scoped(storage, storage_url=f"sqlite:///{path}", tenant="local",
                            config={**config, "planFingerprint": current["planFingerprint"]},
                            confirm_external_egress=True, provider=provider)
    assert result["status"] == "error"
    assert result["dimensions"] == []
    storage.close()


@pytest.mark.parametrize("reasoning", ["No specific event cited", "[see: above]"])
def test_uncited_scoped_verdict_is_saved_as_uncited_opinion(tmp_path, reasoning):
    path = tmp_path / "uncited.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle())
    config = _config()
    preview = preview_scoped(storage, tenant="local", config=config)
    assert "cite at least one exact" in preview["evidencePreview"]["query"]
    result = execute_scoped(
        storage, storage_url=url, tenant="local",
        config={**config, "planFingerprint": preview["planFingerprint"]},
        confirm_external_egress=True, provider=MockProvider(reasoning),
    )
    assert result["status"] == "completed"
    assert result["dimensions"][0]["verdict"] == "pass"
    assert result["dimensions"][0]["citationStatus"] == "uncited_opinion"
    assert result["dimensions"][0]["evidenceReferences"] == []
    saved = ControlStore(url).history("local", "scoped_evaluation", result["resultId"])[0]
    assert saved["payload"]["dimensions"][0]["citationStatus"] == "uncited_opinion"
    storage.close()


def test_unknown_bracket_kind_is_prose_but_real_citation_is_linked(tmp_path):
    path = tmp_path / "unknown-bracket.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle())
    config = _config()
    preview = preview_scoped(storage, tenant="local", config=config)
    provider = MockProvider("[see: above] [event:call]")
    result = execute_scoped(
        storage, storage_url=url, tenant="local",
        config={**config, "planFingerprint": preview["planFingerprint"]},
        confirm_external_egress=True, provider=provider,
    )
    assert provider.calls == 1
    assert result["status"] == "completed"
    assert result["dimensions"][0]["citationStatus"] == "linked"
    assert result["dimensions"][0]["evidenceReferences"] == ["event:call"]
    saved = ControlStore(url).history("local", "scoped_evaluation", result["resultId"])[0]
    assert saved["payload"]["dimensions"][0]["evidenceReferences"] == ["event:call"]
    storage.close()


def test_destination_change_invalidates_consent_and_midcall_change_invalidates_result(tmp_path, monkeypatch):
    path = tmp_path / "destination.db"
    url = f"sqlite:///{path}"
    storage = SQLiteStorage(str(path))
    bundle = _bundle()
    storage.replace_agent_run_bundle(bundle)
    config = _config()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://user:secret@judge-a.example/v1?key=secret")
    first = preview_scoped(storage, tenant="local", config=config)
    assert first["destination"] == "https://judge-a.example/v1"
    assert "secret" not in str(first)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://user:secret@judge-a.example/v1?key=changed")
    with pytest.raises(ValueError, match="no longer current"):
        execute_scoped(storage, storage_url=url, tenant="local",
                       config={**config, "planFingerprint": first["planFingerprint"]},
                       confirm_external_egress=True, provider=MockProvider())
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://judge-b.example/v1")
    try:
        execute_scoped(storage, storage_url=url, tenant="local",
                       config={**config, "planFingerprint": first["planFingerprint"]},
                       confirm_external_egress=True, provider=MockProvider())
    except ValueError as exc:
        assert "no longer current" in str(exc)
    else:
        raise AssertionError("destination change was accepted")

    class MutatingProvider(MockProvider):
        def complete(self, request):
            try:
                storage.replace_agent_run_bundle(replace(bundle, events=(*bundle.events, replace(
                    bundle.events[-1], event_id="late-feedback", sequence=3,
                    event_type=AgentEventType.FEEDBACK,
                    attributes={"kind": "user_feedback", "value": "later evidence"},
                ))))
            except Exception as exc:
                self.mutation_error = repr(exc)
            return super().complete(request)

    current = preview_scoped(storage, tenant="local", config=config)
    provider = MutatingProvider()
    result = execute_scoped(storage, storage_url=url, tenant="local",
                            config={**config, "planFingerprint": current["planFingerprint"]},
                            confirm_external_egress=True, provider=provider)
    assert not getattr(provider, "mutation_error", None)
    assert result["status"] == "error"
    assert result["error"] == "evidence_changed_during_evaluation"
    assert not result["dimensions"]
    storage.close()


def test_scoped_api_requires_authorization_and_hides_other_tenant(tmp_path):
    path = tmp_path / "isolation.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle("other"))
    storage.close()

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            denied = await client.post("/api/scoped/preview", json=_config())
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            hidden = await client.post("/api/scoped/preview", json=_config(),
                                       headers={"X-Verdict-Setup": token})
            return denied, hidden

    denied, hidden = asyncio.run(request())
    assert denied.status_code == 403
    assert hidden.status_code == 400


def test_scheduled_api_requires_explicit_approval_and_can_cancel(tmp_path):
    path = tmp_path / "scheduled-api.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle())
    storage.close()

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = (await client.post("/api/scoped/preview", json=_config(),
                                         headers=headers)).json()
            payload = {**_config(), "planFingerprint": preview["planFingerprint"],
                       "maxEstimatedUsd": 1, "confirmPersistentExternalEgress": True}
            denied = await client.post("/api/scoped/schedule", json=payload)
            missing_consent = await client.post("/api/scoped/schedule", headers=headers,
                                                json={**payload, "confirmPersistentExternalEgress": False})
            queued = await client.post("/api/scoped/schedule", headers=headers, json=payload)
            blocked_generic = await client.post(
                f"/api/control/scheduled_evaluation/{queued.json()['jobId']}",
                headers=headers, json={"state": "pending", "payload": {}},
            )
            cancelled = await client.post(
                f"/api/scoped/scheduled/{queued.json()['jobId']}/cancel", headers=headers,
                json={"expectedRevision": queued.json()["revision"]},
            )
            return denied, missing_consent, queued, blocked_generic, cancelled

    denied, missing_consent, queued, blocked_generic, cancelled = asyncio.run(request())
    assert denied.status_code == 403
    assert missing_consent.status_code == 409
    assert queued.status_code == 200
    assert blocked_generic.status_code == 400
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
