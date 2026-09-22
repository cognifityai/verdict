import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import verdict
import verdict.dashboard.analysis_service as analysis_service
import verdict.dashboard.query as dashboard_query
from fastapi import FastAPI, Request
from verdict.agent_judgment import (
    AgentTurnJudgment,
    TurnToolCounts,
    agent_turn_judgment_to_json,
    turn_evidence_fingerprint,
)
from verdict.analysis_records import AnalysisRunStatus, DeterministicAnalysisRun
from verdict.capture import AgentCaptureService
from verdict.dashboard import agent_evidence_queries
from verdict.dashboard.analysis_service import read_latest_analysis, run_analysis
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
from verdict.schema import DimensionScore, Operation, Trace, Verdict
from verdict.storage import SQLiteStorage


def _bundle(
    tenant: str, now: datetime, *, with_turn: bool = False, trace_link: bool = False,
) -> AgentRunBundle:
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
             "output_tokens": 11}, PrivacyClassification.METADATA,
            trace_id="trace-1" if trace_link else None,
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


def test_run_detail_shows_current_native_turn_scores_without_cross_tenant_leak(tmp_path):
    path = tmp_path / "turn-scores.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    local = _bundle("local", now, with_turn=True)
    other = _bundle("other", now, with_turn=True)
    storage.replace_agent_run_bundle(local)
    storage.replace_agent_run_bundle(other)
    result = AgentTurnJudgment(
        tenant_id="local", run_id="r-local", turn_id="turn",
        evaluator_fingerprint="a" * 64,
        evidence_fingerprint=turn_evidence_fingerprint(local.turns[0]),
        evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
        expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
        dimensions=[DimensionScore("relevance", Verdict.PASS, "ok", "test")],
    )
    assert storage.save_agent_turn_judgment_if_current(result) == "saved"
    storage.close()
    visible = build_agent_run_detail(path, tenant="local", run_id="r-local")
    hidden = build_agent_run_detail(path, tenant="other", run_id="r-other")
    assert visible["turns"][0]["evaluation"]["status"] == "completed"
    assert hidden["turns"][0]["evaluation"] is None
    assert "request" not in json.dumps(visible["turns"][0]["evaluation"])
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(replace(local, turns=(replace(
        local.turns[0], final_response_redacted="response extended",
    ),)))
    storage.close()
    stale = build_agent_run_detail(path, tenant="local", run_id="r-local")
    assert stale["turns"][0]["evaluation"] is None


def test_run_detail_batches_tool_metadata_for_turn_page(tmp_path, monkeypatch):
    path = tmp_path / "detail-batch.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    bundle = _bundle("local", now, with_turn=True)
    turns = tuple(replace(bundle.turns[0], turn_id=f"turn-{index}", sequence=index)
                  for index in range(50))
    storage.replace_agent_run_bundle(replace(bundle, turns=turns, events=()))
    storage.close()

    event_queries = []
    original = dashboard_query.SQLiteSession.execute

    def counted(self, query, params=()):
        if "AS is_error" in query and "FROM agent_events" in query:
            event_queries.append(query)
        return original(self, query, params)

    monkeypatch.setattr(dashboard_query.SQLiteSession, "execute", counted)
    detail = build_agent_run_detail(path, tenant="local", run_id="r-local", turn_limit=50)
    assert len(detail["turns"]) == 50
    assert len(event_queries) == 1


def test_run_detail_shows_only_current_tool_count_judgment(tmp_path):
    path = tmp_path / "turn-tool-scores.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    bundle = _bundle("local", now, with_turn=True)
    call = AgentEvent(
        "call", "turn", 2, now, AgentEventType.TOOL_CALL,
        ExecutionStatus.COMPLETED, "sdk",
        {"tool_name": "secret_tool", "call_id": "secret_id", "arguments": "private"},
        PrivacyClassification.REDACTED,
    )
    with_call = replace(bundle, events=(*bundle.events, call))
    storage.replace_agent_run_bundle(with_call)
    [(turn, _status, counts)], _ = storage.list_agent_turn_evaluation_candidates(
        "local", "c" * 64, tool_evidence=True,
    )
    judgment = AgentTurnJudgment(
        tenant_id="local", run_id="r-local", turn_id="turn",
        evaluator_fingerprint="c" * 64,
        evidence_fingerprint=turn_evidence_fingerprint(turn, counts),
        evaluator_provider="anthropic", evaluator_config={
            "tool_evidence_mode": "counts_v1",
            "tool_evidence_template": TurnToolCounts.PROMPT_TEMPLATE,
        },
        judge_models=["test"], expected_dimensions=["relevance"],
        rubric_name="quality", rubric_version="1",
        dimensions=[DimensionScore("relevance", Verdict.PASS, "ok", "test")],
    )
    assert storage.save_agent_turn_judgment_if_current(judgment) == "saved"
    storage.close()
    visible = build_agent_run_detail(path, tenant="local", run_id="r-local")
    assert visible["turns"][0]["evaluation"]["toolEvidence"] == "counts_v1"
    assert visible["turns"][0]["evaluation"]["toolCounts"] == {
        "calls": 1, "results": 0, "errorResults": 0, "unknownResults": 0,
    }
    assert "secret_tool" not in json.dumps(visible["turns"][0]["evaluation"])
    corrupt = json.loads(agent_turn_judgment_to_json(judgment))
    corrupt["evaluator_config"]["tool_evidence_mode"] = []
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE agent_turn_judgments SET result_json=?",
                           (json.dumps(corrupt),))
    assert build_agent_run_detail(path, tenant="local", run_id="r-local")["turns"][0]["evaluation"] is None

    async def detail_after_corruption():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs/r-local")

    response = asyncio.run(detail_after_corruption())
    assert response.status_code == 200
    assert response.json()["turns"][0]["evaluation"] is None
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE agent_turn_judgments SET result_json=?",
                           (agent_turn_judgment_to_json(judgment),))
    storage = SQLiteStorage(str(path))
    result = AgentEvent(
        "result", "turn", 3, now, AgentEventType.TOOL_RESULT,
        ExecutionStatus.FAILED, "sdk",
        {"tool_name": "secret_tool", "call_id": "secret_id", "is_error": True,
         "result": "private"}, PrivacyClassification.REDACTED,
    )
    storage.replace_agent_run_bundle(replace(bundle, events=(*bundle.events, call, result)))
    storage.close()
    stale = build_agent_run_detail(path, tenant="local", run_id="r-local")
    assert stale["turns"][0]["evaluation"] is None


def test_run_detail_labels_latest_turn_evaluator_and_filters_exact_identity(tmp_path):
    path = tmp_path / "multi-evaluator.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    bundle = _bundle("local", now, with_turn=True)
    storage.replace_agent_run_bundle(bundle)
    for fingerprint, version, when in (("a" * 64, "one", now),
                                       ("b" * 64, "two", now + timedelta(seconds=1))):
        assert storage.save_agent_turn_judgment_if_current(AgentTurnJudgment(
            tenant_id="local", run_id="r-local", turn_id="turn",
            evaluator_fingerprint=fingerprint,
            evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
            evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
            expected_dimensions=["relevance"], rubric_name="quality",
            rubric_version=version, evaluated_at=when,
            dimensions=[DimensionScore("relevance", Verdict.PASS, "ok", "test")],
        )) == "saved"
    storage.close()
    latest = build_agent_run_detail(path, tenant="local", run_id="r-local")
    exact = build_agent_run_detail(path, tenant="local", run_id="r-local",
                                   turn_evaluator_fingerprint="a" * 64)
    assert latest["turnEvaluationScope"]["mode"] == "latest_valid_bounded"
    assert latest["turnEvaluationScope"]["maxEvaluatorsPerTurn"] == 8
    assert latest["turns"][0]["evaluation"]["evaluatorFingerprint"] == "b" * 64
    assert latest["turns"][0]["evaluation"]["rubricVersion"] == "two"
    assert exact["turnEvaluationScope"]["mode"] == "exact_evaluator"
    assert exact["turnEvaluationScope"]["maxEvaluatorsPerTurn"] == 1
    assert exact["turns"][0]["evaluation"]["evaluatorFingerprint"] == "a" * 64
    assert exact["turns"][0]["evaluation"]["rubricVersion"] == "one"
    assert build_agent_run_detail(path, tenant="local", run_id="r-local",
                                  turn_evaluator_fingerprint="c" * 64)["turns"][0]["evaluation"] is None

    async def request_exact():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            exact_response = await client.get("/api/runs/r-local", params={
                "turn_evaluator_fingerprint": "a" * 64,
            })
            bad_response = await client.get("/api/runs/r-local", params={
                "turn_evaluator_fingerprint": "not-a-digest",
            })
            return exact_response, bad_response

    response, bad = asyncio.run(request_exact())
    assert response.status_code == 200
    assert response.json()["turns"][0]["evaluation"]["evaluatorFingerprint"] == "a" * 64
    assert bad.status_code == 400
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE agent_turn_judgments SET result_json='[]' WHERE evaluator_fingerprint=?",
            ("b" * 64,),
        )
    fallback = build_agent_run_detail(path, tenant="local", run_id="r-local")
    assert fallback["turns"][0]["evaluation"]["evaluatorFingerprint"] == "a" * 64
    assert build_agent_run_detail(path, tenant="local", run_id="r-local",
                                  turn_evaluator_fingerprint="b" * 64)["turns"][0]["evaluation"] is None

    async def default_after_corruption():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs/r-local")

    api_fallback = asyncio.run(default_after_corruption())
    assert api_fallback.status_code == 200
    assert api_fallback.json()["turns"][0]["evaluation"]["evaluatorFingerprint"] == "a" * 64


def test_malformed_stored_turn_result_does_not_break_run_detail(tmp_path):
    path = tmp_path / "malformed-evaluator.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    bundle = _bundle("local", now, with_turn=True)
    storage.replace_agent_run_bundle(bundle)
    result = AgentTurnJudgment(
        tenant_id="local", run_id="r-local", turn_id="turn",
        evaluator_fingerprint="a" * 64,
        evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
        evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
        expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
        dimensions=[DimensionScore("relevance", Verdict.PASS, "ok", "test")],
    )
    storage.save_agent_turn_judgment_if_current(result)
    storage._conn.execute("UPDATE agent_turn_judgments SET result_json='[]'")
    detail = build_agent_run_detail(path, tenant="local", run_id="r-local")
    assert detail["turns"][0]["evaluation"] is None
    original = json.loads(agent_turn_judgment_to_json(result))
    for change in ("invalid_model_list", "missing_score", "duplicate_score"):
        payload = json.loads(json.dumps(original))
        if change == "invalid_model_list":
            payload["judge_models"] = "not a list"
        elif change == "missing_score":
            payload["dimensions"] = []
        else:
            payload["dimensions"].append(payload["dimensions"][0])
        storage._conn.execute("UPDATE agent_turn_judgments SET result_json=?", (json.dumps(payload),))
        detail = build_agent_run_detail(path, tenant="local", run_id="r-local")
        assert detail["turns"][0]["evaluation"] is None
    storage.close()


def test_latest_turn_result_checks_only_bounded_newest_evaluator_slots(tmp_path):
    path = tmp_path / "bounded-evaluators.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    bundle = _bundle("local", now, with_turn=True)
    storage.replace_agent_run_bundle(bundle)
    for index in range(1, 10):
        fingerprint = f"{index:064x}"
        storage.save_agent_turn_judgment_if_current(AgentTurnJudgment(
            tenant_id="local", run_id="r-local", turn_id="turn",
            evaluator_fingerprint=fingerprint,
            evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
            evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
            expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
            dimensions=[DimensionScore("relevance", Verdict.PASS, "ok", "test")],
            evaluated_at=now + timedelta(seconds=index),
        ))
        if index > 1:
            storage._conn.execute(
                "UPDATE agent_turn_judgments SET result_json='[]' WHERE evaluator_fingerprint=?",
                (fingerprint,),
            )
    storage.close()
    bounded = build_agent_run_detail(path, tenant="local", run_id="r-local")
    assert bounded["turnEvaluationScope"]["maxEvaluatorsPerTurn"] == 8
    assert bounded["turns"][0]["evaluation"] is None
    exact = build_agent_run_detail(path, tenant="local", run_id="r-local",
                                   turn_evaluator_fingerprint=f"{1:064x}")
    assert exact["turns"][0]["evaluation"]["evaluatorFingerprint"] == f"{1:064x}"


def test_turn_result_latest_order_normalizes_timezone_offsets_in_sqlite(tmp_path):
    path = tmp_path / "offset-order.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    bundle = _bundle("local", now, with_turn=True)
    storage.replace_agent_run_bundle(bundle)
    for fingerprint, when in (
        ("a" * 64, datetime(2026, 8, 31, 10, tzinfo=timezone(timedelta(hours=2)))),
        ("b" * 64, datetime(2026, 8, 31, 9, tzinfo=timezone.utc)),
    ):
        assert storage.save_agent_turn_judgment_if_current(AgentTurnJudgment(
            tenant_id="local", run_id="r-local", turn_id="turn",
            evaluator_fingerprint=fingerprint,
            evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
            evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
            expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
            dimensions=[DimensionScore("relevance", Verdict.PASS, "ok", "test")],
            evaluated_at=when,
        )) == "saved"
    stored = storage._conn.execute(
        "SELECT evaluated_at FROM agent_turn_judgments ORDER BY evaluated_at DESC"
    ).fetchall()
    assert stored[0][0] == "2026-08-31T09:00:00+00:00"
    storage.close()
    detail = build_agent_run_detail(path, tenant="local", run_id="r-local")
    assert detail["turns"][0]["evaluation"]["evaluatorFingerprint"] == "b" * 64

    async def read_api():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs/r-local")

    response = asyncio.run(read_api())
    assert response.status_code == 200
    assert response.json()["turns"][0]["evaluation"]["evaluatorFingerprint"] == "b" * 64


def test_agent_runs_api_exposes_typed_analysis_without_raw_envelopes(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(
        _bundle("local", datetime(2026, 8, 31, tzinfo=timezone.utc), with_turn=True)
    )
    storage.close()
    direct = build_agent_runs_bundle(path, tenant="local")

    async def request_runs():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}", tenant_id="local")
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs?tenant=local")

    response = asyncio.run(request_runs())
    assert response.status_code == 200
    assert response.json() == direct
    assert direct["summary"] == {"available": 1, "shown": 1}
    assert "turns" not in direct["runs"][0]
    assert "payload_json" not in json.dumps(direct)


def test_configured_dashboard_tenant_is_the_default_agent_run_scope(tmp_path):
    path = tmp_path / "configured-runs.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    storage.replace_agent_run_bundle(_bundle("customer-a", now, with_turn=True))
    storage.replace_agent_run_bundle(_bundle("customer-b", now, with_turn=True))
    storage.close()

    async def request_runs():
        app = create_app(storage=f"sqlite:///{path}", tenant_id="customer-a")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get("/api/data"), await client.get("/api/runs")

    dashboard, runs = asyncio.run(request_runs())

    assert dashboard.status_code == 200
    assert dashboard.json()["meta"]["totalAgentRuns"] == 1
    assert runs.json()["summary"] == {"available": 1, "shown": 1}
    assert [item["runId"] for item in runs.json()["runs"]] == ["r-customer-a"]


def test_request_tenant_parameters_cannot_override_configured_workspace(tmp_path):
    path = tmp_path / "configured-authority.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    storage.replace_agent_run_bundle(_bundle("customer-a", now, with_turn=True))
    storage.replace_agent_run_bundle(_bundle("customer-b", now, with_turn=True))
    storage.close()

    async def requests():
        app = create_app(storage=f"sqlite:///{path}", tenant_id="customer-a")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return (
                await client.get("/api/runs", params={"tenant": "customer-b"}),
                await client.get(
                    "/api/runs/r-customer-b", params={"tenant": "customer-b"}
                ),
                await client.get("/api/registry", params={"tenant": "customer-b"}),
                await client.get("/api/insights", params={"tenant": "customer-b"}),
                await client.post(
                    "/api/insights/run",
                    params={"tenant": "customer-b"},
                    headers={"X-Verdict-Setup": token},
                ),
            )

    runs, detail, registry, before, analysis = asyncio.run(requests())

    assert [item["runId"] for item in runs.json()["runs"]] == ["r-customer-a"]
    assert detail.status_code == 404
    assert registry.json()["tenant"] == "customer-a"
    assert before.json()["analysisState"]["status"] == "never_run"
    assert analysis.status_code == 200
    storage = SQLiteStorage(str(path))
    assert storage.get_latest_deterministic_analysis_run(
        "customer-a", "agent-and-trace"
    ) is not None
    assert storage.get_latest_deterministic_analysis_run(
        "customer-b", "agent-and-trace"
    ) is None
    storage.close()


def test_trusted_host_tenant_state_overrides_process_configuration(tmp_path):
    path = tmp_path / "host-authority.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    storage.replace_agent_run_bundle(_bundle("customer-a", now, with_turn=True))
    storage.replace_agent_run_bundle(_bundle("customer-b", now, with_turn=True))
    storage.close()

    host = FastAPI()

    @host.middleware("http")
    async def authorize_tenant(request: Request, call_next):
        request.state.verdict_registry_tenant = "customer-b"
        return await call_next(request)

    host.mount(
        "/verdict",
        create_app(storage=f"sqlite:///{path}", tenant_id="customer-a"),
    )

    async def request_runs():
        transport = httpx.ASGITransport(app=host)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(
                "/verdict/api/runs", params={"tenant": "customer-a"}
            )

    response = asyncio.run(request_runs())

    assert response.status_code == 200
    assert [item["runId"] for item in response.json()["runs"]] == ["r-customer-b"]


def test_invalid_trusted_host_tenant_is_rejected_consistently(tmp_path):
    path = tmp_path / "invalid-host-authority.db"
    SQLiteStorage(str(path)).close()
    host = FastAPI()

    @host.middleware("http")
    async def authorize_tenant(request: Request, call_next):
        request.state.verdict_registry_tenant = "a" * 129
        return await call_next(request)

    host.mount("/verdict", create_app(storage=f"sqlite:///{path}"))

    async def requests():
        transport = httpx.ASGITransport(app=host)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await asyncio.gather(
                client.get("/verdict/api/data"),
                client.get("/verdict/api/registry"),
                client.get("/verdict/api/runs"),
                client.get("/verdict/api/insights"),
            )

    responses = asyncio.run(requests())

    assert [response.status_code for response in responses] == [400, 400, 400, 400]


def test_agent_run_detail_exposes_ordered_bounded_events_and_trace_links(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    trace = Trace(
        trace_id="trace-1", started_at=now, ended_at=now, provider="anthropic",
        request_model="claude-test", response_model="claude-test", input_tokens=7,
        output_tokens=11, prompt_redacted="request", response_redacted="I'm sorry, maybe.",
        tenant_id="local",
    )
    bundle = _bundle("local", now, with_turn=True, trace_link=True)
    bundle = replace(
        bundle,
        turns=(replace(
            bundle.turns[0],
            input_tokens=7,
            cached_input_tokens=4,
            output_tokens=11,
            total_tokens=18,
            token_usage_basis="claude_provider_response_sum",
            response_truncated=True,
        ),),
        events=(
            replace(bundle.events[0], producer_id="agent", producer_sequence=0),
            replace(bundle.events[1], producer_id="shell", producer_sequence=0),
        ),
    )
    AgentCaptureService(storage).capture(
        bundle, traces=(trace,),
    )
    storage.close()

    direct = build_agent_run_detail(path, tenant="local", run_id="r-local")

    async def request_detail():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}", tenant_id="local")
        )
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
    assert direct["turns"][0]["tokenUsage"] == {
        "inputTokens": 7,
        "cachedInputTokens": 4,
        "cacheWriteInputTokens": None,
        "outputTokens": 11,
        "reasoningOutputTokens": None,
        "totalTokens": 18,
        "basis": "claude_provider_response_sum",
    }
    assert direct["turns"][0]["responseTruncated"] is True
    assert direct["events"][0]["traceId"] == "trace-1"
    assert direct["producerCount"] == 2
    assert {event["producerId"] for event in direct["events"]} == {"agent", "shell"}
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


def test_agent_run_detail_redacts_historical_semantic_and_plural_credentials(tmp_path):
    path = tmp_path / "historical-redaction.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    storage.replace_agent_run_bundle(_bundle("local", now, with_turn=True))
    storage.close()
    canary = "opaque-historical-agent-canary"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE agent_events SET event_type=?,attributes_json=? "
            "WHERE tenant_id=? AND run_id=? AND event_id=?",
            (
                "context",
                json.dumps(
                    {
                        "name": "OIDCIDTokens",
                        "value": canary,
                        "api_keys": [canary],
                        "AWSSECRETACCESSKEYS": [canary],
                        "XAPIKeys": [canary],
                        "source": "historical",
                    }
                ),
                "local",
                "r-local",
                "event-2",
            ),
        )

    async def request_detail():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}", tenant_id="local")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get("/api/runs/r-local")

    response = asyncio.run(request_detail())

    assert response.status_code == 200
    assert canary not in response.text
    event = next(item for item in response.json()["events"] if item["eventId"] == "event-2")
    assert event["attributes"] == {
        "AWSSECRETACCESSKEYS": "<SECRET>",
        "XAPIKeys": "<SECRET>",
        "api_keys": "<SECRET>",
        "name": "OIDCIDTokens",
        "source": "historical",
        "value": "<SECRET>",
    }


def test_agent_run_detail_serves_multiple_maximum_size_turn_previews(tmp_path):
    path = tmp_path / "large-turns.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    bundle = _bundle("local", now, with_turn=True)
    maximum_preview = "x" * 65_536
    bundle = replace(
        bundle,
        turns=tuple(
            replace(
                bundle.turns[0],
                turn_id=f"turn-{sequence}",
                sequence=sequence,
                started_at=now + timedelta(seconds=sequence),
                ended_at=now + timedelta(seconds=sequence),
                user_request_redacted=maximum_preview,
                final_response_redacted=maximum_preview,
            )
            for sequence in range(8)
        ),
        events=(),
    )
    storage.replace_agent_run_bundle(bundle)
    storage.close()

    async def request_detail():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}", tenant_id="local")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(
                "/api/runs/r-local?tenant=local&event_limit=1&turn_limit=20"
            )

    response = asyncio.run(request_detail())

    assert response.status_code == 200
    assert len(response.json()["turns"]) == 8
    assert all(len(turn["request"]) == 65_536 for turn in response.json()["turns"])
    assert all(len(turn["response"]) == 65_536 for turn in response.json()["turns"])


def test_agent_run_detail_reads_existing_schema_without_running_migrations(tmp_path):
    path = tmp_path / "existing.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(
        _bundle("local", datetime(2026, 8, 31, tzinfo=timezone.utc), with_turn=True)
    )
    storage.close()
    legacy_columns = (
        "tenant_id,turn_id,run_id,sequence,started_at,ended_at,status,"
        "user_request_redacted,final_response_redacted,request_state,response_state"
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """CREATE TABLE old_agent_turns (
                tenant_id TEXT NOT NULL, turn_id TEXT NOT NULL, run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
                status TEXT NOT NULL, user_request_redacted TEXT,
                final_response_redacted TEXT, request_state TEXT NOT NULL,
                response_state TEXT NOT NULL,
                PRIMARY KEY (tenant_id, run_id, turn_id),
                UNIQUE (tenant_id, run_id, sequence)
            )"""
        )
        connection.execute(
            f"INSERT INTO old_agent_turns ({legacy_columns}) "
            f"SELECT {legacy_columns} FROM agent_turns"
        )
        connection.execute("DROP TABLE agent_turns")
        connection.execute("ALTER TABLE old_agent_turns RENAME TO agent_turns")

    async def request_detail():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}", tenant_id="local")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get("/api/runs/r-local?tenant=local")

    response = asyncio.run(request_detail())

    assert response.status_code == 200
    [turn] = response.json()["turns"]
    assert turn["requestTruncated"] is False
    assert turn["responseTruncated"] is False
    assert turn["tokenUsage"] == {
        "inputTokens": None,
        "cachedInputTokens": None,
        "cacheWriteInputTokens": None,
        "outputTokens": None,
        "reasoningOutputTokens": None,
        "totalTokens": None,
        "basis": None,
    }


def test_agent_run_detail_does_not_reconstruct_a_whole_bundle(tmp_path, monkeypatch):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(
        _bundle("local", datetime(2026, 8, 31, tzinfo=timezone.utc), with_turn=True)
    )
    storage.close()
    monkeypatch.setattr(
        agent_evidence_queries,
        "bundle_from_normalized_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("whole run read")),
    )

    detail = build_agent_run_detail(path, tenant="local", run_id="r-local", event_limit=1)

    assert detail["page"]["shown"] == 1
    assert detail["page"]["available"] == 2


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
    trace = Trace(
        trace_id="trace-1", started_at=now, ended_at=now, provider="anthropic",
        request_model="claude-test", response_model="claude-test", input_tokens=7,
        output_tokens=11, prompt_redacted="prompt-evidence",
        response_redacted="I'm sorry, maybe.", tenant_id="local", cost_usd=0.001,
        tags={"verdict.agent_run_id": "r-local"},
        operation=Operation.CHAT, finish_reason="stop",
    )
    local_bundle = _bundle("local", now, with_turn=True, trace_link=True)
    local_bundle = replace(
        local_bundle,
        turns=(replace(
            local_bundle.turns[0],
            input_tokens=70,
            cached_input_tokens=50,
            output_tokens=30,
            total_tokens=100,
            token_usage_basis="codex_turn_delta",
        ),),
    )
    AgentCaptureService(storage).capture(local_bundle, traces=(trace,))
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
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}", tenant_id="local")
        )
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
    assert report["schema"] == "agent-insights-v2"
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
    assert report["sourceActivity"][0]["source"] == "codex"
    assert report["sourceActivity"][0]["runs"] == 1
    assert report["sourceActivity"][0]["turns"] == 1
    assert report["sourceActivity"][0]["finalResponses"] == 1
    assert report["sourceActivity"][0]["totalTokens"] == 100
    assert report["sourceActivity"][0]["tokenUsageState"] == "complete"
    assert report["sourceActivity"][0]["runOutcomes"] == {"unknown": 1}
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


def test_insights_reports_retries_captured_by_the_agent_sdk(tmp_path):
    path = tmp_path / "sdk.db"
    verdict.shutdown()
    verdict.init(
        storage=f"sqlite:///{path}",
        tenant_id="sdk-tenant",
        instrumentors=[],
    )
    try:
        with verdict.agent_run(name="support-agent") as run:
            with run.turn(user_input="retry the request") as turn:
                turn.record_retry(reason="rate limit", attempt=1)
                turn.record_test(command="pytest", exit_code=1, passed=2, failed=1)
                turn.set_output("done")
    finally:
        verdict.shutdown()

    report = build_agent_insights_bundle(path, tenant="sdk-tenant")

    [comparison] = report["sourceActivity"]
    assert comparison["source"] == "verdict_sdk"
    assert comparison["retries"] == 1
    assert comparison["retryState"] == "captured"
    assert comparison["testFailures"] == 1
    assert report["reliability"]["testFailures"] == 1


def test_trace_performance_never_falls_back_to_agent_event_usage(tmp_path):
    path = tmp_path / "source-only.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    storage.replace_agent_run_bundle(_bundle("local", now, with_turn=True))
    storage.close()

    report = build_agent_insights_bundle(path, tenant="local")

    assert report["scope"]["traces"]["available"] == 0
    assert report["performance"]["modelCalls"] == 0
    assert report["performance"]["inputTokens"] is None
    assert report["performance"]["outputTokens"] is None
    [activity] = report["sourceActivity"]
    assert activity["totalTokens"] is None
    assert activity["tokenUsageState"] == "not_captured"


def test_component_only_source_usage_is_partial_not_unavailable(tmp_path):
    path = tmp_path / "component-usage.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    bundle = _bundle("local", now, with_turn=True)
    bundle = replace(
        bundle,
        turns=(replace(
            bundle.turns[0],
            input_tokens=9,
            output_tokens=3,
            token_usage_basis="codex_turn_delta",
        ),),
    )
    storage.replace_agent_run_bundle(bundle)
    storage.close()

    runs = build_agent_runs_bundle(path, tenant="local")
    insights = build_agent_insights_bundle(path, tenant="local")

    assert runs["runs"][0]["sourceTokenUsage"] == {
        "totalTokens": None,
        "inputTokens": 9,
        "cachedInputTokens": None,
        "cacheWriteInputTokens": None,
        "outputTokens": 3,
        "reasoningOutputTokens": None,
        "turns": 1,
        "state": "partial",
    }
    [activity] = insights["sourceActivity"]
    assert activity["inputTokens"] == 9
    assert activity["outputTokens"] == 3
    assert activity["totalTokens"] is None
    assert activity["tokenUsageTurns"] == 1
    assert activity["tokenUsageState"] == "partial"


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
            app=create_app(storage=f"sqlite:///{path}", tenant_id="local")
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


def test_insights_surface_explicit_failed_business_outcomes(tmp_path):
    path = tmp_path / "failed-business-outcome.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    bundle = _bundle("local", now, with_turn=True)
    outcome = AgentEvent(
        "business-outcome",
        "turn",
        2,
        now,
        AgentEventType.OUTCOME,
        ExecutionStatus.FAILED,
        "verdict:sdk",
        {"name": "workflow_success", "value": False, "source": "application"},
        PrivacyClassification.REDACTED,
    )
    storage.replace_agent_run_bundle(replace(bundle, events=(*bundle.events, outcome)))
    storage.close()

    report = build_agent_insights_bundle(path, tenant="local")

    finding = next(item for item in report["findings"] if item["code"] == "business_outcome_failed")
    assert finding == {
        "code": "business_outcome_failed",
        "severity": "error",
        "message": "One or more explicit business outcomes reported failure.",
        "runs": 1,
        "runIds": ["r-local"],
        "runIdsTruncated": False,
    }


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


def test_analysis_build_finishes_before_persistent_storage_is_opened(monkeypatch):
    events = []

    class RecordingStorage:
        def get_latest_deterministic_analysis_run(self, *args, **kwargs):
            events.append("read")
            return None

        def save_deterministic_analysis_run(self, run):
            events.append("save")

        def close(self):
            events.append("close")

    def open_storage(_storage_url):
        events.append("storage_open")
        return RecordingStorage()

    def build():
        events.append("build")
        assert "storage_open" not in events
        return {
            "schema": "agent-insights-v2",
            "_analysisInputFingerprint": "a" * 64,
        }

    monkeypatch.setattr(analysis_service, "_storage", open_storage)

    result = run_analysis("unused", tenant="local", build=build)

    assert result["analysisState"]["status"] == "completed"
    assert events == ["build", "storage_open", "read", "save", "close"]


def test_incompatible_persisted_insights_are_not_served_as_current(tmp_path):
    storage_url = f"sqlite:///{tmp_path / 'old-analysis.db'}"
    storage = SQLiteStorage(str(tmp_path / "old-analysis.db"))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    tenant = "__verdict_local__"
    storage.save_deterministic_analysis_run(DeterministicAnalysisRun(
        analysis_id="a" * 64,
        tenant_id=tenant,
        scope_key="agent-and-trace",
        cutoff=now,
        completed_at=now,
        status=AnalysisRunStatus.COMPLETED,
        analyzer_version="agent-insights-v1",
        input_fingerprint="b" * 64,
        result={"schema": "agent-insights-v1", "comparisons": []},
    ))
    storage.insert_trace(Trace(
        trace_id="analysis-version-trace",
        tenant_id=tenant,
        started_at=now,
        ended_at=now,
        provider="anthropic",
        request_model="claude-test",
        prompt_redacted="request",
        response_redacted="response",
    ))
    storage.close()

    async def request_current_views():
        transport = httpx.ASGITransport(app=create_app(storage=storage_url))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(
                f"/api/insights?tenant={tenant}"
            ), await client.get("/api/data")

    insights_response, data_response = asyncio.run(request_current_views())
    result = insights_response.json()

    assert insights_response.status_code == 200
    assert data_response.status_code == 200
    assert result["analysisState"]["status"] == "never_run"
    assert result["analysisState"]["analyzerVersion"] == "agent-insights-v2"
    assert result["schema"] == "agent-insights-v2"
    assert "comparisons" not in result
    assert data_response.json()["coverage"]["deterministicAnalysis"] == {
        "status": "never_run",
        "analysisId": None,
        "completedAt": None,
        "availableRuns": 0,
        "analyzedRuns": 0,
        "availableTraces": 1,
        "analyzedTraces": 0,
        "complete": False,
    }


def test_current_analysis_is_reused_after_a_newer_rollback_version(tmp_path):
    path = tmp_path / "analysis-rollback.db"
    storage_url = f"sqlite:///{path}"
    fingerprint = "b" * 64

    def build():
        return {
            "schema": "agent-insights-v2",
            "sourceActivity": [],
            "_analysisInputFingerprint": fingerprint,
        }

    first = run_analysis(storage_url, tenant="local", build=build)
    rollback_time = datetime.now(timezone.utc) + timedelta(days=1)
    storage = SQLiteStorage(str(path))
    storage.save_deterministic_analysis_run(DeterministicAnalysisRun(
        analysis_id="a" * 64,
        tenant_id="local",
        scope_key="agent-and-trace",
        cutoff=rollback_time,
        completed_at=rollback_time,
        status=AnalysisRunStatus.COMPLETED,
        analyzer_version="agent-insights-v1",
        input_fingerprint=fingerprint,
        result={"schema": "agent-insights-v1", "comparisons": []},
    ))
    storage.close()

    second = run_analysis(storage_url, tenant="local", build=build)
    current = read_latest_analysis(
        storage_url,
        tenant="local",
        empty_result={"schema": "agent-insights-v2", "sourceActivity": []},
    )

    assert second == first
    assert current == first


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
            replace(
                bundle.session,
                tenant_id="local",
                source_locator_hash=f"{index:064x}",
            ),
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
            app=create_app(storage=f"sqlite:///{path}", tenant_id="local")
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


def test_agent_runs_pages_all_runs_in_stable_newest_first_order(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    for index in range(35):
        tenant = f"local-{index}"
        bundle = _bundle(tenant, now + timedelta(minutes=index))
        storage.replace_agent_run_bundle(AgentRunBundle(
            replace(
                bundle.session,
                tenant_id="local",
                source_locator_hash=f"{index:064x}",
            ),
            replace(bundle.run, tenant_id="local"),
            bundle.turns,
            bundle.events,
        ))
    storage.close()

    first = build_agent_runs_bundle(path, tenant="local", limit=10)
    second = build_agent_runs_bundle(path, tenant="local", limit=10, offset=10)
    last = build_agent_runs_bundle(path, tenant="local", limit=10, offset=30)

    async def request_second_page():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}", tenant_id="local")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get("/api/runs?tenant=local&limit=10&offset=10")

    response = asyncio.run(request_second_page())

    assert [run["runId"] for run in first["runs"]] == [
        f"r-local-{index}" for index in range(34, 24, -1)
    ]
    assert [run["runId"] for run in second["runs"]] == [
        f"r-local-{index}" for index in range(24, 14, -1)
    ]
    assert [run["runId"] for run in last["runs"]] == [
        f"r-local-{index}" for index in range(4, -1, -1)
    ]
    assert set(run["runId"] for run in first["runs"]).isdisjoint(
        run["runId"] for run in second["runs"]
    )
    assert first["page"] == {
        "available": 35, "shown": 10, "offset": 0, "limit": 10,
        "truncated": True,
    }
    assert second["page"] == {
        "available": 35, "shown": 10, "offset": 10, "limit": 10,
        "truncated": True,
    }
    assert last["page"] == {
        "available": 35, "shown": 5, "offset": 30, "limit": 10,
        "truncated": False,
    }
    assert response.status_code == 200
    assert response.json() == second


def test_agent_runs_reject_invalid_or_filtered_offsets(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(
        _bundle("local", datetime(2026, 8, 31, tzinfo=timezone.utc))
    )
    storage.close()

    for offset in (-1, True, 100_001):
        try:
            build_agent_runs_bundle(path, tenant="local", offset=offset)
        except ValueError as error:
            assert str(error) == "invalid offset"
        else:  # pragma: no cover - makes the failed contract explicit
            raise AssertionError(f"offset {offset!r} was accepted")
    try:
        build_agent_runs_bundle(
            path, tenant="local", run_ids=("r-local",), offset=1,
        )
    except ValueError as error:
        assert str(error) == "offset is not supported with selected runs"
    else:  # pragma: no cover - makes the failed contract explicit
        raise AssertionError("filtered offset was accepted")

    async def request_invalid_offsets():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return (
                await client.get("/api/runs?tenant=local&offset=-1"),
                await client.get("/api/runs?tenant=local&offset=100001"),
                await client.get(
                    "/api/runs?tenant=local&run_ids=r-local&offset=1"
                ),
            )

    negative, oversized, filtered = asyncio.run(request_invalid_offsets())
    assert negative.status_code == 422
    assert oversized.status_code == 422
    assert filtered.status_code == 400


def test_agent_runs_api_is_tenant_scoped_and_bounded(tmp_path):
    path = tmp_path / "runs.db"
    storage = SQLiteStorage(str(path))
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    for tenant in ("a", "b"):
        storage.replace_agent_run_bundle(_bundle(tenant, now))
    storage.close()

    async def request_runs():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}", tenant_id="a")
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return (
                await client.get("/api/runs?tenant=a&limit=1"),
                await client.get("/api/runs?tenant=a&limit=0"),
            )

    valid, invalid = asyncio.run(request_runs())
    assert [run["runId"] for run in valid.json()["runs"]] == ["r-a"]
    assert invalid.status_code == 422
