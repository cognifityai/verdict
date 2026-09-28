"""Jev through the dashboard's approved evaluator workflow."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx2
import pytest
import typesafe_sdk
from verdict.dashboard.evaluator_lab import (
    evaluator_environment,
    execute_calibration,
    execute_evaluation,
    preview_calibration,
    preview_evaluation,
)
from verdict.evidence import (
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    SourceSession,
)
from verdict.schema import JudgmentStatus, Trace, Verdict
from verdict.storage import InMemoryStorage


def _config() -> dict:
    return {
        "provider": "jev", "model": "jev-1.13.0", "maxCalls": 1,
        "maxOutputTokens": 512,
        "rubric": {"name": "quality", "version": "1", "dimensions": [
            {"name": "relevance", "description": "Answers the request."},
            {"name": "completeness", "description": "Covers the request."},
        ]},
    }


def _wire_sdk(monkeypatch, *, omit: str | None = None):
    requests = []
    client_type = typesafe_sdk.TypeSafeClient

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        answers = {name: {
            "type": "choice", "choice": "pass", "confidence": 0.8,
            "probabilities": {"pass": 0.8, "fail": 0.1, "unclear": 0.1},
        } for name in body["questions"] if name != omit}
        return httpx2.Response(200, json={
            "model": "jev-1.13.0", "answers": answers,
            "usage": {"input_tokens": 40, "output_tokens": 10},
        })

    def client(**kwargs):
        return client_type(api_key="ts_test_key", transport=httpx2.MockTransport(respond), **kwargs)

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", client)
    return requests


def _trace() -> Trace:
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return Trace(
        trace_id="trace-1", started_at=now, ended_at=now,
        provider="anthropic", request_model="source-model", response_model="source-model",
        prompt_redacted="Summarize the policy.", response_redacted="Returns are allowed.",
        tenant_id="local", cluster_id="all",
    )


def test_jev_preview_and_run_share_identity_and_persist_labels(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    requests = _wire_sdk(monkeypatch)
    storage = InMemoryStorage()
    storage.insert_trace(_trace())
    config = _config()

    preview = preview_evaluation(storage, tenant_id="local", config=config)
    assert preview["plannedCalls"] == 1
    assert preview["estimatedMaximumCostUsd"] is None
    assert requests == []

    result = execute_evaluation(
        storage, tenant_id="local", confirm_external_egress=True,
        config={**config, "planFingerprint": preview["planFingerprint"],
                "plannedTraces": preview["plannedTraces"]},
    )
    assert result["completed"] == 1
    assert result["errors"] == 0
    [saved] = storage.list_judgments_for_trace("trace-1")
    assert saved.evaluator_provider == "jev"
    assert saved.evaluator_fingerprint == result["evaluatorFingerprint"]
    assert [dimension.verdict for dimension in saved.dimensions] == [Verdict.PASS] * 2
    assert len(requests) == 1
    assert requests[0]["state"]["assistant_response"] == "Returns are allowed."


def test_jev_malformed_result_is_recorded_as_error(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    _wire_sdk(monkeypatch, omit="completeness")
    storage = InMemoryStorage()
    storage.insert_trace(_trace())
    config = _config()
    preview = preview_evaluation(storage, tenant_id="local", config=config)
    result = execute_evaluation(
        storage, tenant_id="local", confirm_external_egress=True,
        config={**config, "planFingerprint": preview["planFingerprint"],
                "plannedTraces": preview["plannedTraces"]},
    )
    assert result["completed"] == 0
    assert result["errors"] == 1
    [saved] = storage.list_judgments_for_trace("trace-1")
    assert saved.evaluator_provider == "jev"
    assert saved.dimensions == []


def test_jev_provider_failure_does_not_persist_provider_error_body(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")

    def failed_client(**_kwargs):
        raise RuntimeError("api-secret-canary-123 alice@example.com raw provider body")

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", failed_client)
    storage = InMemoryStorage()
    storage.insert_trace(_trace())
    config = _config()
    preview = preview_evaluation(storage, tenant_id="local", config=config)
    result = execute_evaluation(
        storage, tenant_id="local", confirm_external_egress=True,
        config={**config, "planFingerprint": preview["planFingerprint"],
                "plannedTraces": preview["plannedTraces"]},
    )
    assert result["errors"] == 1
    [saved] = storage.list_judgments_for_trace("trace-1")
    assert "api-secret-canary-123" not in str(saved)
    assert "alice@example.com" not in str(saved)


def test_jev_scores_agent_turn_final_text(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    requests = _wire_sdk(monkeypatch)
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    storage = InMemoryStorage()
    storage.replace_agent_run_bundle(AgentRunBundle(
        session=SourceSession(
            source_session_id="session-1", tenant_id="local",
            source_kind="pydantic-ai", source_locator_hash="a" * 64,
            started_at=now, observed_at=now,
        ),
        run=AgentRun(
            run_id="run-1", source_session_id="session-1", tenant_id="local",
            started_at=now, ended_at=now, status=ExecutionStatus.COMPLETED,
        ),
        turns=(AgentTurn(
            turn_id="turn-1", run_id="run-1", sequence=0,
            started_at=now, ended_at=now, status=ExecutionStatus.COMPLETED,
            user_request_redacted="Explain photosynthesis.",
            final_response_redacted="Plants turn sunlight into food.",
            request_state=EvidenceState.PRESENT,
            response_state=EvidenceState.PRESENT,
        ),),
    ))
    config = {**_config(), "unit": "agent_turn"}
    preview = preview_evaluation(storage, tenant_id="local", config=config)
    assert preview["plannedCalls"] == 1
    assert preview["estimatedMaximumCostUsd"] is None

    result = execute_evaluation(
        storage, tenant_id="local", confirm_external_egress=True,
        config={**config, "planFingerprint": preview["planFingerprint"],
                "plannedTurns": preview["plannedTurns"]},
    )
    assert result["completed"] == 1 and result["errors"] == 0
    rows, _ = storage.list_agent_turn_evaluation_candidates(
        "local", result["evaluatorFingerprint"],
    )
    assert rows[0][1] is JudgmentStatus.COMPLETED
    assert requests[0]["state"]["assistant_response"] == "Plants turn sunlight into food."


def test_jev_calibration_uses_same_judge(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    requests = _wire_sdk(monkeypatch)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        '{"set_name":"quality-v1"}\n'
        '{"sentinel_id":"one","query":"q","response":"a",'
        '"labels":{"relevance":"pass","completeness":"pass"}}\n'
    )
    preview = preview_calibration(path=labels, config=_config())
    assert preview["estimatedMaximumCostUsd"] is None
    result = execute_calibration(
        InMemoryStorage(), path=labels, config=_config(),
        confirm_external_egress=True, minimum_examples=1,
    )
    assert result["totalExamples"] == 1
    assert result["errors"] == 0
    assert len(requests) == 1


def test_jev_rejects_tool_counts_and_reports_secret_reference(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    environment = evaluator_environment()
    jev = next(item for item in environment["providers"] if item["provider"] == "jev")
    assert jev["configured"] is True
    assert jev["secretReference"] == "TYPESAFE_API_KEY"
    assert "ts_test_key" not in str(environment)
    with pytest.raises(ValueError, match="tool evidence"):
        preview_evaluation(InMemoryStorage(), tenant_id="local", config={
            **_config(), "unit": "agent_turn", "toolEvidence": "counts_v1",
        })
    with pytest.raises(ValueError, match="Jev model"):
        preview_evaluation(InMemoryStorage(), tenant_id="local", config={
            **_config(), "model": "unverified-model",
        })


def test_jev_run_requires_server_key_after_preview(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    storage = InMemoryStorage()
    storage.insert_trace(_trace())
    config = _config()
    preview = preview_evaluation(storage, tenant_id="local", config=config)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        execute_evaluation(
            storage, tenant_id="local", confirm_external_egress=True,
            config={**config, "planFingerprint": preview["planFingerprint"],
                    "plannedTraces": preview["plannedTraces"]},
        )
    assert storage.list_judgments_for_trace("trace-1") == []
