"""Jev through the dashboard's approved evaluator workflow."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import httpx2
import pytest
import typesafe_sdk
from verdict.dashboard.app import create_app
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
from verdict.storage import InMemoryStorage, SQLiteStorage


def _config() -> dict:
    return {
        "provider": "jev", "model": "jev-1.13.0", "maxCalls": 1,
        "maxOutputTokens": 512,
        "rubric": {"name": "quality", "version": "1", "dimensions": [
            {"name": "relevance", "description": "Answers the request."},
            {"name": "completeness", "description": "Covers the request."},
        ]},
    }


def _wire_sdk(monkeypatch, *, omit: str | None = None, response_model="jev-1.13.0"):
    requests = []
    client_type = typesafe_sdk.TypeSafeClient

    def respond(request):
        body = json.loads(request.content)
        body["__request_url__"] = str(request.url)
        requests.append(body)
        answers = {name: {
            "type": "choice", "choice": "pass", "confidence": 0.8,
            "probabilities": {"pass": 0.8, "fail": 0.1, "unclear": 0.1},
        } for name in body["questions"] if name != omit}
        return httpx2.Response(200, json={
            "model": response_model, "answers": answers,
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


def test_another_versioned_jev_model_has_separate_preview_and_persisted_identity(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    requests = _wire_sdk(monkeypatch, response_model="jev-1.14.0")
    storage = InMemoryStorage()
    storage.insert_trace(_trace())
    default_config = _config()
    selected_config = {**default_config, "model": "jev-1.14.0"}
    old_preview = preview_evaluation(storage, tenant_id="local", config=default_config)
    preview = preview_evaluation(storage, tenant_id="local", config=selected_config)
    assert preview["planFingerprint"] != old_preview["planFingerprint"]
    with pytest.raises(ValueError, match="approved preview"):
        execute_evaluation(storage, tenant_id="local", confirm_external_egress=True,
                           config={**selected_config, "planFingerprint": old_preview["planFingerprint"],
                                   "plannedTraces": old_preview["plannedTraces"]})
    assert requests == []
    result = execute_evaluation(
        storage, tenant_id="local", confirm_external_egress=True,
        config={**selected_config, "planFingerprint": preview["planFingerprint"],
                "plannedTraces": preview["plannedTraces"]},
    )
    assert result["completed"] == 1
    assert requests[0]["model"] == "jev-1.14.0"
    [saved] = storage.list_judgments_for_trace("trace-1")
    assert saved.judge_models == ["jev-1.14.0"]
    assert saved.evaluator_fingerprint == result["evaluatorFingerprint"]
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        '{"sentinel_id":"one","query":"q","response":"a",'
        '"labels":{"relevance":"pass","completeness":"pass"}}\n'
    )
    calibration_preview = preview_calibration(path=labels, config=selected_config)
    health = execute_calibration(
        storage, path=labels,
        config={**selected_config, "planFingerprint": calibration_preview["planFingerprint"]},
        confirm_external_egress=True, minimum_examples=1,
    )
    assert health["evaluatorFingerprint"] == saved.evaluator_fingerprint
    assert requests[1]["model"] == "jev-1.14.0"


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
    storage = InMemoryStorage()
    storage.insert_trace(_trace())
    production = preview_evaluation(storage, tenant_id="local", config=_config())
    judged = execute_evaluation(
        storage, tenant_id="local", confirm_external_egress=True,
        config={**_config(), "planFingerprint": production["planFingerprint"],
                "plannedTraces": production["plannedTraces"]},
    )
    assert result["evaluatorFingerprint"] == judged["evaluatorFingerprint"]


def test_jev_calibration_uses_production_context_free_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    requests = _wire_sdk(monkeypatch)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        '{"sentinel_id":"one","query":"Contact alice@example.com",'
        '"response":"Answered alice@example.com",'
        '"context":"Context with bob@example.com",'
        '"labels":{"relevance":"pass","completeness":"pass"}}\n'
    )
    storage = InMemoryStorage()
    result = execute_calibration(
        storage, path=labels, config=_config(),
        confirm_external_egress=True, minimum_examples=1,
    )
    assert result["errors"] == 0
    assert requests[0]["state"]["user_query"] == "Contact alice@example.com"
    assert requests[0]["state"]["assistant_response"] == "Answered alice@example.com"
    assert requests[0]["state"]["retrieved_context"] == ""
    assert "alice@example.com" not in str(storage.list_evaluator_health(limit=10))


def test_jev_calibration_rejects_labels_for_dimensions_absent_from_production(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        '{"sentinel_id":"one","query":"q","response":"a",'
        '"context":"evidence","labels":{"groundedness":"pass"}}\n'
    )
    config = {**_config(), "rubric": {
        "name": "quality", "version": "1", "dimensions": [
            {"name": "groundedness", "description": "Supported by context.",
             "requiresContext": True},
            {"name": "relevance", "description": "Answers the request."},
        ],
    }}
    with pytest.raises(ValueError, match="production evaluator"):
        preview_calibration(path=labels, config=config)


def test_jev_endpoint_is_approved_identity_and_run_target(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://judge-a.example/api/")
    requests = _wire_sdk(monkeypatch)
    storage = InMemoryStorage()
    storage.insert_trace(_trace())
    config = _config()
    preview = preview_evaluation(storage, tenant_id="local", config=config)
    assert preview["destination"] == "https://judge-a.example/api"
    jev = next(item for item in evaluator_environment()["providers"]
               if item["provider"] == "jev")
    assert "destination" not in jev

    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://judge-b.example/api")
    other_preview = preview_evaluation(storage, tenant_id="local", config=config)
    assert other_preview["planFingerprint"] != preview["planFingerprint"]
    with pytest.raises(ValueError, match="approved preview"):
        execute_evaluation(
            storage, tenant_id="local", confirm_external_egress=True,
            config={**config, "planFingerprint": preview["planFingerprint"],
                    "plannedTraces": preview["plannedTraces"]},
        )
    assert requests == []

    execute_evaluation(
        storage, tenant_id="local", confirm_external_egress=True,
        config={**config, "planFingerprint": other_preview["planFingerprint"],
                "plannedTraces": other_preview["plannedTraces"]},
    )
    assert requests[0]["state"]["assistant_response"] == "Returns are allowed."
    assert requests[0]["__request_url__"].startswith("https://judge-b.example/api/")
    [saved] = storage.list_judgments_for_trace("trace-1")
    assert saved.evaluator_config["base_url"] == "https://judge-b.example/api"


def test_jev_calibration_rejects_changed_approved_file_or_destination(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://judge-a.example")
    requests = _wire_sdk(monkeypatch)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        '{"sentinel_id":"one","query":"q","response":"a",'
        '"labels":{"relevance":"pass","completeness":"pass"}}\n'
    )
    config = _config()
    preview = preview_calibration(path=labels, config=config)
    assert preview["destination"] == "https://judge-a.example"

    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://judge-b.example")
    with pytest.raises(ValueError, match="approved preview"):
        execute_calibration(
            InMemoryStorage(), path=labels,
            config={**config, "planFingerprint": preview["planFingerprint"]},
            confirm_external_egress=True, minimum_examples=1,
        )
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://judge-a.example")
    labels.write_text(labels.read_text().replace('"query":"q"', '"query":"changed"'))
    with pytest.raises(ValueError, match="approved preview"):
        execute_calibration(
            InMemoryStorage(), path=labels,
            config={**config, "planFingerprint": preview["planFingerprint"]},
            confirm_external_egress=True, minimum_examples=1,
        )
    assert requests == []


def test_dashboard_api_calibration_approval_and_health_match_production(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://judge-a.example")
    requests = _wire_sdk(monkeypatch)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        '{"sentinel_id":"one","query":"Contact alice@example.com",'
        '"response":"A reply",'
        '"labels":{"relevance":"pass","completeness":"pass"}}\n'
    )
    db = tmp_path / "dashboard.db"
    storage = SQLiteStorage(str(db))
    storage.insert_trace(_trace())
    storage.close()
    app = create_app(storage=f"sqlite:///{db}", tenant_id="local")
    config = _config()

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            preview = await client.post(
                "/api/evaluators/calibration/preview", headers=headers,
                json={**config, "labelSetPath": str(labels)},
            )
            assert preview.status_code == 200
            assert preview.json()["destination"] == "https://judge-a.example"
            monkeypatch.setenv("TYPESAFE_BASE_URL", "https://judge-b.example")
            stale = await client.post(
                "/api/evaluators/calibration/run", headers=headers,
                json={**config, "labelSetPath": str(labels),
                      "planFingerprint": preview.json()["planFingerprint"],
                      "confirmExternalEgress": True, "minimumExamples": 1},
            )
            assert stale.status_code == 400
            assert requests == []
            monkeypatch.setenv("TYPESAFE_BASE_URL", "https://judge-a.example")
            approved = await client.post(
                "/api/evaluators/calibration/run", headers=headers,
                json={**config, "labelSetPath": str(labels),
                      "planFingerprint": preview.json()["planFingerprint"],
                      "confirmExternalEgress": True, "minimumExamples": 1},
            )
            assert approved.status_code == 200
            production_preview = await client.post(
                "/api/evaluators/preview", headers=headers, json=config,
            )
            assert production_preview.status_code == 200
            judged = await client.post(
                "/api/evaluators/run", headers=headers,
                json={**config,
                      "planFingerprint": production_preview.json()["planFingerprint"],
                      "plannedTraces": production_preview.json()["plannedTraces"],
                      "confirmExternalEgress": True},
            )
            assert judged.status_code == 200
            data = await client.get("/api/data", headers=headers)
            return approved.json(), judged.json(), data.text

    health, judged, data = asyncio.run(run())
    assert health["evaluatorFingerprint"] == judged["evaluatorFingerprint"]
    assert requests[0]["state"]["user_query"] == "Contact alice@example.com"
    assert requests[0]["__request_url__"].startswith("https://judge-a.example/")
    assert "alice@example.com" not in data
    assert b"alice@example.com" not in db.read_bytes()


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
