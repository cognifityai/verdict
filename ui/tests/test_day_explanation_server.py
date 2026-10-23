"""Day explanation through real SQLite, preview, and one-attempt execution."""

import asyncio
import hashlib
from datetime import datetime, timezone

import httpx
import pytest
from verdict.dashboard.app import create_app
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.day_explanation import preview_day_explanation, run_day_explanation
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


def _seed(path, *, tenant="local", total=900, source_kind="synthetic-codex", model=None):
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    day = now.date().isoformat()
    storage = SQLiteStorage(str(path))
    run_id = "run-a"
    session = SourceSession("source-a", tenant, source_kind, hashlib.sha256(b"source").hexdigest(), now, now)
    run = AgentRun(run_id, session.source_session_id, tenant, now, ExecutionStatus.COMPLETED, now,
                   session_id="private-session-id")
    turn = AgentTurn("turn-a", run_id, 0, now, ExecutionStatus.COMPLETED, now,
                     total_tokens=total, input_tokens=total - 100, output_tokens=100,
                     token_usage_basis="codex_turn_delta",
                     user_request_redacted="private-canary-do-not-send",
                     request_state=EvidenceState.PRESENT)
    events = (AgentEvent("event-a", "turn-a", 0, now, AgentEventType.CONTEXT,
                         ExecutionStatus.COMPLETED, "test",
                         {"name": "model_configuration", "source": model},
                         PrivacyClassification.METADATA),) if model else ()
    storage.replace_agent_run_bundle(AgentRunBundle(session, run, (turn,), events))
    storage.close()
    return day


class RecordingProvider:
    name = "anthropic"

    def __init__(self, response="Session 1 contributed most of the reported tokens."):
        self.calls = 0
        self.response = response
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return CompletionResponse(text=self.response, input_tokens=20, output_tokens=20,
                                  finish_reason="end_turn")


def test_day_explanation_preview_and_run_are_bounded_replay_safe_and_tenant_scoped(tmp_path):
    path = tmp_path / "day.db"
    day = _seed(path, source_kind="private-source-canary", model="private-model-canary")
    url = f"sqlite:///{path}"
    config = {"day": day, "provider": "anthropic"}
    preview = preview_day_explanation(url, tenant="local", config=config)
    assert preview["plannedCalls"] == 1
    assert preview["upperStaticListPriceUsd"] is not None
    assert "private-canary" not in str(preview)
    assert "private-session-id" not in str(preview)
    assert "private-source-canary" not in str(preview)
    assert "private-model-canary" not in str(preview)
    assert preview["evidencePreview"]["topSessions"][0]["label"] == "Session 1"
    assert preview_day_explanation(url, tenant="other", config=config)["plannedCalls"] == 0
    provider = RecordingProvider()
    with pytest.raises(ValueError):
        run_day_explanation(url, tenant="local", config={**config, "planFingerprint": "stale"},
                            confirm_external_egress=True, provider_adapter=provider)
    with pytest.raises(ValueError):
        run_day_explanation(url, tenant="local", config={**config, "planFingerprint": preview["planFingerprint"]},
                            confirm_external_egress=False, provider_adapter=provider)
    assert provider.calls == 0
    approved = {**config, "planFingerprint": preview["planFingerprint"]}
    result = run_day_explanation(url, tenant="local", config=approved,
                                 confirm_external_egress=True, provider_adapter=provider)
    assert result["status"] == "completed"
    assert provider.calls == 1
    assert "private-canary" not in str(provider.requests[0].messages)
    assert "private-session-id" not in str(provider.requests[0].messages)
    assert run_day_explanation(url, tenant="local", config=approved,
                               confirm_external_egress=True, provider_adapter=provider) == result
    assert provider.calls == 1
    _seed(path, total=901, source_kind="private-source-canary", model="private-model-canary")
    with pytest.raises(ValueError, match="stale"):
        run_day_explanation(url, tenant="local", config=approved,
                            confirm_external_egress=True, provider_adapter=provider)
    assert provider.calls == 1


def test_day_explanation_errors_and_reserved_attempt_do_not_retry(tmp_path):
    path = tmp_path / "errors.db"
    day = _seed(path)
    url = f"sqlite:///{path}"
    config = {"day": day, "provider": "anthropic"}
    preview = preview_day_explanation(url, tenant="local", config=config)
    approved = {**config, "planFingerprint": preview["planFingerprint"]}
    provider = RecordingProvider(RuntimeError("private-provider-error"))
    result = run_day_explanation(url, tenant="local", config=approved,
                                 confirm_external_egress=True, provider_adapter=provider)
    assert result["status"] == "error"
    assert result["error"] == "RuntimeError"
    assert "private-provider-error" not in str(result)
    assert run_day_explanation(url, tenant="local", config=approved,
                               confirm_external_egress=True, provider_adapter=provider) == result
    assert provider.calls == 1

    class BrokenClose:
        def close(self):
            raise RuntimeError("cleanup failed after provider response")

    cleanup_path = tmp_path / "cleanup.db"
    cleanup_day = _seed(cleanup_path)
    cleanup_url = f"sqlite:///{cleanup_path}"
    cleanup_config = {"day": cleanup_day, "provider": "anthropic"}
    cleanup_preview = preview_day_explanation(cleanup_url, tenant="local", config=cleanup_config)
    cleanup_provider = RecordingProvider()
    cleanup_provider._client = BrokenClose()
    cleanup_approved = {**cleanup_config, "planFingerprint": cleanup_preview["planFingerprint"]}
    saved = run_day_explanation(cleanup_url, tenant="local", config=cleanup_approved,
                                confirm_external_egress=True, provider_adapter=cleanup_provider)
    assert saved["status"] == "completed"
    assert run_day_explanation(cleanup_url, tenant="local", config=cleanup_approved,
                               confirm_external_egress=True, provider_adapter=cleanup_provider) == saved
    assert cleanup_provider.calls == 1
    # A reserved attempt whose process died has an unknown outcome. It is not retried.
    reserved_path = tmp_path / "reserved.db"
    reserved_day = _seed(reserved_path)
    reserved_url = f"sqlite:///{reserved_path}"
    reserved_config = {"day": reserved_day, "provider": "anthropic"}
    reserved_preview = preview_day_explanation(reserved_url, tenant="local", config=reserved_config)
    ControlStore(reserved_url).append("local", kind="usage_explanation",
        document_id=reserved_preview["planFingerprint"], state="reserved",
        payload={"status": "outcome_unknown"}, expected_revision=None)
    withheld = run_day_explanation(reserved_url, tenant="local",
        config={**reserved_config, "planFingerprint": reserved_preview["planFingerprint"]},
        confirm_external_egress=True, provider_adapter=provider)
    assert withheld["status"] == "outcome_unknown"
    assert provider.calls == 1


def test_day_explanation_routes_require_setup_authorization(tmp_path, monkeypatch):
    path = tmp_path / "api.db"
    day = _seed(path, tenant="__verdict_local__")
    app = create_app(storage=f"sqlite:///{path}")
    provider = RecordingProvider()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-placeholder")
    monkeypatch.setattr("verdict.dashboard.day_explanation._provider_available", lambda _provider: True)
    monkeypatch.setattr("verdict_eval.providers.AnthropicAdapter", lambda **_kwargs: provider)

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            denied = await client.post("/api/usage/explain/preview", json={"day": day, "provider": "anthropic"})
            assert denied.status_code == 403
            setup = await client.get("/api/setup/token")
            token = setup.json()["setupToken"]
            preview = await client.post("/api/usage/explain/preview",
                headers={"X-Verdict-Setup": token}, json={"day": day, "provider": "anthropic"})
            assert preview.status_code == 200
            assert preview.json()["plannedCalls"] == 1
            unapproved = await client.post("/api/usage/explain/run",
                headers={"X-Verdict-Setup": token}, json={"day": day, "provider": "anthropic",
                    "planFingerprint": preview.json()["planFingerprint"]})
            assert unapproved.status_code == 400
            approved = {"day": day, "provider": "anthropic",
                        "planFingerprint": preview.json()["planFingerprint"],
                        "confirmExternalEgress": True}
            denied_run = await client.post("/api/usage/explain/run", json=approved)
            assert denied_run.status_code == 403
            monkeypatch.setattr("verdict.dashboard.day_explanation._provider_available", lambda _provider: False)
            unavailable = await client.post("/api/usage/explain/run",
                headers={"X-Verdict-Setup": token}, json=approved)
            assert unavailable.status_code == 503
            assert provider.calls == 0
            monkeypatch.setattr("verdict.dashboard.day_explanation._provider_available", lambda _provider: True)
            response = await client.post("/api/usage/explain/run",
                headers={"X-Verdict-Setup": token}, json=approved)
            assert response.status_code == 200
            assert response.json()["status"] == "completed"
            assert provider.calls == 1
            assert provider.requests[0].messages == preview.json()["messages"]
            replay = await client.post("/api/usage/explain/run",
                headers={"X-Verdict-Setup": token}, json=approved)
            assert replay.json() == response.json()
            assert provider.calls == 1

    asyncio.run(exercise())
