import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
from verdict.dashboard.analysis_service import run_analysis
from verdict.dashboard.app import (
    build_agent_insights_bundle,
    build_agent_run_detail,
    build_agent_runs_bundle,
    create_app,
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
from verdict.schema import Operation, Trace
from verdict.storage import SQLiteStorage


def _bundle(tenant: str, now: datetime, *, with_turn: bool = False) -> AgentRunBundle:
    turns = (
        AgentTurn(
            "turn", f"r-{tenant}", 0, now, ExecutionStatus.COMPLETED, now,
            "request", "response", EvidenceState.PRESENT, EvidenceState.PRESENT,
        ),
    ) if with_turn else ()
    events = (
        AgentEvent(
            "event-1", "turn", 0, now, AgentEventType.MODEL_CALL,
            ExecutionStatus.COMPLETED, "claude:assistant",
            {"provider": "anthropic", "request_model": "claude-test", "input_tokens": 7,
             "output_tokens": 11}, PrivacyClassification.METADATA, trace_id="trace-1",
        ),
        AgentEvent(
            "event-2", "turn", 1, now, AgentEventType.COMMAND,
            ExecutionStatus.FAILED, "claude:tool_result",
            {"command": "pytest", "exit_code": 1, "stdout": "failed"},
            PrivacyClassification.REDACTED,
        ),
    ) if with_turn else ()
    return AgentRunBundle(
        SourceSession(f"s-{tenant}", tenant, "codex", "a" * 64, now, now),
        AgentRun(f"r-{tenant}", f"s-{tenant}", tenant, now, ExecutionStatus.UNKNOWN),
        turns,
        events,
    )


def test_agent_runs_api_exposes_typed_analysis_without_raw_envelopes(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(
        _bundle("local", datetime(2026, 8, 31, tzinfo=timezone.utc), with_turn=True)
    )
    storage.close()
    direct = build_agent_runs_bundle(path, tenant="local")

    async def request_runs():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs?tenant=local")

    response = asyncio.run(request_runs())
    assert response.status_code == 200
    assert response.json() == direct
    assert direct["summary"] == {"available": 1, "shown": 1}
    assert "turns" not in direct["runs"][0]
    assert "payload_json" not in json.dumps(direct)


def test_agent_run_detail_exposes_ordered_bounded_events_and_trace_links(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    storage.replace_agent_run_bundle(_bundle("local", now, with_turn=True))
    storage.insert_trace(Trace(
        trace_id="trace-1", started_at=now, ended_at=now, provider="anthropic",
        request_model="claude-test", response_model="claude-test", input_tokens=7,
        output_tokens=11, prompt_redacted="request", response_redacted="I'm sorry, maybe.",
        tenant_id="local",
    ))
    storage.close()

    direct = build_agent_run_detail(path, tenant="local", run_id="r-local")

    async def request_detail():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return (
                await client.get("/api/runs/r-local?tenant=local&event_limit=1"),
                await client.get(
                    "/api/runs/r-local?tenant=local&event_limit=1&event_id=event-2"
                ),
            )

    response, focused = asyncio.run(request_detail())
    assert response.status_code == 200
    assert [event["sequence"] for event in direct["events"]] == [0, 1]
    assert direct["turns"][0]["request"] == "request"
    assert direct["events"][0]["traceId"] == "trace-1"
    assert direct["events"][1]["attributes"] == {
        "command": "pytest", "exit_code": 1, "stdout": "failed"
    }
    assert response.json()["events"] == direct["events"][:1]
    assert response.json()["page"] == {
        "available": 2, "shown": 1, "offset": 0, "limit": 1, "truncated": True
    }
    assert [event["eventId"] for event in focused.json()["events"]] == ["event-2"]
    assert focused.json()["focusEventId"] == "event-2"
    assert focused.json()["page"]["offset"] == 1
    assert "payload_json" not in response.text


def test_agent_run_detail_is_tenant_scoped_and_returns_not_found(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(
        _bundle("a", datetime(2026, 8, 31, tzinfo=timezone.utc), with_turn=True)
    )
    storage.close()

    async def request_detail():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs/r-a?tenant=b")

    response = asyncio.run(request_detail())
    assert response.status_code == 404


def test_agent_insights_reports_dataset_wide_evidence_and_findings(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    storage.replace_agent_run_bundle(_bundle("local", now, with_turn=True))
    storage.insert_trace(Trace(
        trace_id="trace-1", started_at=now, ended_at=now, provider="anthropic",
        request_model="claude-test", response_model="claude-test", input_tokens=7,
        output_tokens=11, prompt_redacted="prompt-evidence",
        response_redacted="I'm sorry, maybe.", tenant_id="local", cost_usd=0.001,
        tags={"verdict.agent_run_id": "r-local"},
        operation=Operation.CHAT, finish_reason="stop",
    ))
    storage.insert_trace(Trace(
        trace_id="trace-failed", started_at=now, ended_at=now, provider="openai",
        request_model="gpt-test", response_model="gpt-test",
        prompt_redacted="prompt", response_redacted=None, tenant_id="local",
        operation=Operation.TEXT_COMPLETION, error="provider unavailable",
        finish_reason="error",
    ))
    storage.close()

    report = build_agent_insights_bundle(path, tenant="local")

    async def request_insights():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            before = await client.get("/api/insights?tenant=local")
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            run = await client.post(
                "/api/insights/run?tenant=local",
                headers={"X-Verdict-Setup": token},
            )
            after = await client.get("/api/insights?tenant=local")
            return before, run, after

    before, response, after = asyncio.run(request_insights())
    assert before.json()["analysisState"]["status"] == "never_run"
    assert response.status_code == 200
    assert after.json() == response.json()
    persisted = response.json()
    assert persisted["analysisState"]["status"] == "completed"
    assert {key: value for key, value in persisted.items() if key != "analysisState"} == report
    assert report["schema"] == "agent-insights-v1"
    assert report["scope"] == {
        "availableRuns": 1, "analyzedRuns": 1, "complete": True,
        "traces": {"available": 2, "analyzed": 2, "complete": True},
    }
    assert report["dataHealth"]["counts"] == {"runs": 1, "turns": 1, "events": 2}
    assert report["dataHealth"]["eventTypes"] == {"command": 1, "model_call": 1}
    assert report["dataHealth"]["traceLinks"] == {
        "modelCalls": 1, "linked": 1, "unlinked": 0
    }
    assert report["reliability"]["commandFailures"] == 1
    assert report["reliability"]["traceOutcomes"] == {"failed": 1, "succeeded": 1}
    assert report["dataHealth"]["traceEvidence"] == {
        "promptPresent": 2,
        "responsePresent": 1,
        "judgeEligible": 1,
        "notEvaluable": 1,
        "notEvaluableReasons": {"provider_call_failed": 1},
    }
    assert report["dataHealth"]["traceOperations"] == {
        "chat": 1, "text_completion": 1,
    }
    assert report["dataHealth"]["traceFinishReasons"] == {"error": 1, "stop": 1}
    assert report["performance"]["modelCalls"] == 2
    assert report["performance"]["inputTokens"] == 7
    assert report["performance"]["outputTokens"] == 11
    assert report["behavior"]["capturedResponses"] == 1
    assert report["behavior"]["apologyStarts"] == 1
    assert report["behavior"]["hedges"] == 1
    assert report["comparisons"][0]["source"] == "codex"
    assert report["comparisons"][0]["costUsd"] == 0.001
    assert report["comparisons"][0]["runOutcomes"] == {"unknown": 1}
    assert report["comparisons"][0]["retryState"] == "not_captured"
    command_finding = next(
        finding for finding in report["findings"] if finding["code"] == "command_failed"
    )
    assert command_finding["runIds"] == ["r-local"]
    assert command_finding["runIdsTruncated"] is False
    assert "prompt-evidence" not in json.dumps(report)
    assert "I'm sorry" not in json.dumps(report)


def test_local_insights_include_tenantless_historical_imports(tmp_path):
    path = tmp_path / "historical.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    storage.insert_trace(Trace(
        trace_id="historical-trace", started_at=now, ended_at=now,
        provider="imported", request_model="historical-model",
        prompt_redacted="historical prompt", response_redacted="historical response",
        tenant_id=None,
    ))
    storage.close()

    local = build_agent_insights_bundle(path, tenant="__verdict_local__")
    unrelated = build_agent_insights_bundle(path, tenant="another-tenant")

    assert local["scope"]["traces"] == {
        "available": 1, "analyzed": 1, "complete": True,
    }
    assert local["behavior"]["capturedResponses"] == 1
    assert unrelated["scope"]["traces"] == {
        "available": 0, "analyzed": 0, "complete": True,
    }


def test_insights_marks_missing_responses_not_evaluable(tmp_path):
    path = tmp_path / "missing-response.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    storage.insert_trace(
        Trace(
            trace_id="tool-call-only",
            started_at=now,
            ended_at=now,
            provider="imported",
            prompt_redacted="use the tool",
            response_redacted=None,
            tenant_id="local",
        )
    )
    storage.close()

    async def request_insights():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/insights/run?tenant=local",
                headers={"X-Verdict-Setup": token},
            )

    evidence = asyncio.run(request_insights()).json()["dataHealth"]["traceEvidence"]

    assert evidence["judgeEligible"] == 0
    assert evidence["notEvaluableReasons"] == {"response_not_captured": 1}


def test_analysis_failure_is_persisted_and_returned_as_an_explicit_state(tmp_path):
    storage_url = f"sqlite:///{tmp_path / 'analysis-error.db'}"

    result = run_analysis(
        storage_url,
        tenant="__verdict_local__",
        build=lambda: (_ for _ in ()).throw(ValueError("secret source detail")),
    )

    assert result["analysisState"]["status"] == "error"
    assert result["error"]["code"] == "analysis_failed"
    assert result["error"]["causeType"] == "ValueError"
    assert "secret source detail" not in json.dumps(result)


def test_agent_runs_can_select_an_exact_tenant_scoped_run(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    storage.replace_agent_run_bundle(_bundle("local", now))
    storage.replace_agent_run_bundle(_bundle("other", now))
    storage.close()

    selected = build_agent_runs_bundle(path, tenant="local", run_id="r-local")

    assert selected["summary"] == {"available": 1, "shown": 1}
    assert [run["runId"] for run in selected["runs"]] == ["r-local"]
    assert build_agent_runs_bundle(
        path, tenant="local", run_id="r-other"
    )["runs"] == []


def test_agent_runs_can_filter_multiple_affected_runs_beyond_default_page(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    for index in range(35):
        tenant = f"local-{index}"
        bundle = _bundle(tenant, now)
        storage.replace_agent_run_bundle(AgentRunBundle(
            replace(bundle.session, tenant_id="local"),
            replace(bundle.run, tenant_id="local"),
            bundle.turns,
            bundle.events,
        ))
    storage.close()

    selected = build_agent_runs_bundle(
        path,
        tenant="local",
        run_ids=("r-local-2", "r-local-34"),
    )

    async def request_filtered_runs():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(
                "/api/runs?run_ids=r-local-2&run_ids=r-local-34&tenant=local"
            )

    response = asyncio.run(request_filtered_runs())

    assert {run["runId"] for run in selected["runs"]} == {
        "r-local-2", "r-local-34",
    }
    assert response.status_code == 200
    assert response.json() == selected
    assert selected["summary"] == {"available": 2, "shown": 2}
    assert selected["filter"] == {
        "requested": 2, "matched": 2, "complete": True,
    }


def test_agent_runs_api_is_tenant_scoped_and_bounded(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    for tenant in ("a", "b"):
        storage.replace_agent_run_bundle(_bundle(tenant, now))
    storage.close()

    async def request_runs():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return (
                await client.get("/api/runs?tenant=a&limit=1"),
                await client.get("/api/runs?tenant=a&limit=0"),
            )

    valid, invalid = asyncio.run(request_runs())
    assert [run["runId"] for run in valid.json()["runs"]] == ["r-a"]
    assert invalid.status_code == 422
