"""Real SQLite and HTTP checks for the local agent usage drilldown."""

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import verdict.dashboard.usage as usage_module
from verdict.dashboard.app import build_agent_session_bundle, build_agent_usage_bundle, create_app
from verdict.dashboard.recommendations import run_recommendations
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
)
from verdict.storage import SQLiteStorage


def _dt(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def _bundle(tenant: str, run_id: str, turns, events=(), *, session_id=None):
    started = min(turn.started_at for turn in turns)
    return AgentRunBundle(
        SourceSession(f"source-{tenant}-{run_id}", tenant, "codex", "a" * 64, started, started),
        AgentRun(run_id, f"source-{tenant}-{run_id}", tenant, started,
                 ExecutionStatus.UNKNOWN, session_id=session_id),
        tuple(turns), tuple(events),
    )


def _turn(run_id: str, turn_id: str, sequence: int, started: datetime, total,
          *, input_tokens=None, output_tokens=None, ended=None):
    return AgentTurn(
        turn_id, run_id, sequence, started, ExecutionStatus.COMPLETED,
        ended or started, input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=total,
        token_usage_basis="codex_turn_delta" if total is not None else None,
    )


def _model_event(turn_id: str, event_id: str, model: str, sequence=0):
    return AgentEvent(
        event_id, turn_id, sequence, _dt(8, 1), AgentEventType.CONTEXT,
        ExecutionStatus.COMPLETED, "test", {"name": "model_configuration", "source": model},
        PrivacyClassification.METADATA,
    )


def test_usage_groups_turns_by_utc_day_and_drills_into_session_without_cross_tenant_data(tmp_path):
    path = tmp_path / "usage.db"
    storage = SQLiteStorage(str(path))
    first = _turn("run-a", "a-1", 0, _dt(7, 23), 100,
                  input_tokens=80, output_tokens=20, ended=_dt(8, 0))
    first = replace(first, user_request_redacted="hidden-canary-example",
                    request_state="present")
    second = _turn("run-a", "a-2", 1, _dt(8, 1), 300,
                   input_tokens=250, output_tokens=50, ended=_dt(8, 2))
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (first, second),
        (_model_event("a-1", "model-1", "gpt-6-luna"),
         _model_event("a-2", "model-2", "gpt-6-luna")),
        session_id="session-a",
    ))
    storage.replace_agent_run_bundle(_bundle(
        "other", "run-other", (_turn("run-other", "secret", 0, _dt(8, 1), 999),),
        session_id="session-other",
    ))
    storage.close()

    report = build_agent_usage_bundle(
        path, tenant="local", now=_dt(8, 12), day="2026-10-08", session_id="session-a",
    )
    by_day = {row["day"]: row for row in report["days"]}
    assert by_day["2026-10-07"]["totalTokens"] == 100
    assert by_day["2026-10-08"]["totalTokens"] == 300
    assert by_day["2026-10-08"]["turns"] == 1
    assert by_day["2026-10-08"]["pricedTurns"] == 1
    assert report["selectedSession"]["totalTokens"] == 400
    assert report["selectedSession"]["turns"][0]["turnId"] == "a-2"
    assert report["selectedSession"]["topTurns"][0]["turnId"] == "a-2"
    assert report["daySources"][0]["sourceKind"] == "codex"
    assert report["daySources"][0]["totalTokens"] == 300
    assert "secret" not in str(report)
    assert "hidden-canary-example" not in str(report)

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/usage?day=2026-10-08&session_id=session-a")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["selectedSession"]["totalTokens"] == 400


def test_ambiguous_session_id_reports_source_choice_and_exact_source_selects(tmp_path):
    path = tmp_path / "ambiguous-session.db"
    storage = SQLiteStorage(str(path))
    codex = _bundle("local", "run-codex", (
        _turn("run-codex", "turn-codex", 0, _dt(8, 1), 100),
    ), session_id="shared-session")
    claude = _bundle("local", "run-claude", (
        _turn("run-claude", "turn-claude", 0, _dt(8, 2), 200),
    ), session_id="shared-session")
    storage.replace_agent_run_bundle(codex)
    storage.replace_agent_run_bundle(replace(
        claude, session=replace(claude.session, source_kind="claude-code"),
    ))
    storage.close()

    async def request(source_kind=""):
        transport = httpx.ASGITransport(app=create_app(
            storage=f"sqlite:///{path}", tenant_id="local",
        ))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(
                "/api/usage?day=2026-10-08&session_id=shared-session" + source_kind,
            )

    ambiguous = asyncio.run(request())
    assert ambiguous.status_code == 200
    assert ambiguous.json()["selectedSession"] is None
    assert ambiguous.json()["selectionReason"] == "ambiguous_source"
    assert {item["sourceKind"] for item in ambiguous.json()["sessions"]} == {
        "codex", "claude-code",
    }
    selected = asyncio.run(request("&source_kind=claude-code"))
    assert selected.status_code == 200
    assert selected.json()["selectedSession"]["sourceKind"] == "claude-code"
    assert selected.json()["selectedSession"]["totalTokens"] == 200
    assert selected.json()["selectionReason"] is None


def test_session_report_combines_runs_and_isolates_source_and_tenant(tmp_path):
    path = tmp_path / "session-report.db"
    storage = SQLiteStorage(str(path))
    for run_id, total, source_kind, tenant in (
        ("run-one", 12, "codex", "local"),
        ("run-two", 30, "codex", "local"),
        ("other-source", 900, "claude-code", "local"),
        ("other-tenant", 800, "codex", "other"),
    ):
        turn = replace(_turn(run_id, f"{run_id}-turn", 0, _dt(8, 1), total),
                       user_request_redacted=f"task {run_id}",
                       final_response_redacted=f"result {run_id}",
                       request_state="present", response_state="present")
        bundle = _bundle(tenant, run_id, (turn,), session_id="shared")
        storage.replace_agent_run_bundle(replace(
            bundle, session=replace(bundle.session, source_kind=source_kind,
                                    source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest()),
        ))
    storage.close()
    report = build_agent_session_bundle(
        path, tenant="local", source_kind="codex", session_id="shared",
    )
    assert report["coverage"] == "complete"
    assert report["runIds"] == ["run-one", "run-two"]
    assert report["usage"]["totalTokens"] == 42
    assert len(report["turns"]) == 2
    assert "other-source" not in str(report)
    assert "other-tenant" not in str(report)

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/session?source_kind=codex&session_id=shared")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["runIds"] == report["runIds"]


def test_large_session_returns_bounded_partial_evidence_and_usage(tmp_path):
    path = tmp_path / "large-session.db"
    storage = SQLiteStorage(str(path))
    turn = _turn("run-a", "turn-a", 0, _dt(8, 1), 100,
                 input_tokens=80, output_tokens=20)
    events = tuple(AgentEvent(
        f"event-{index}", "turn-a", index, _dt(8, 1), AgentEventType.COMMAND,
        ExecutionStatus.COMPLETED, "test", {"command": "synthetic"},
        PrivacyClassification.REDACTED,
    ) for index in range(81))
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,), events, session_id="session-a",
    ))
    storage.close()
    report = build_agent_session_bundle(
        path, tenant="local", source_kind="codex", session_id="session-a",
    )
    assert report["coverage"] == "partial"
    assert report["unavailableReason"] == "event_limit"
    assert report["eventCount"] == 81
    assert len(report["events"]) == 80
    assert len(report["turns"]) == 1
    assert report["usage"]["totalTokens"] == 100
    assert report["usage"]["listPriceUsd"] is None
    assert report["findings"] == []


def test_usage_missing_and_mixed_model_turns_are_not_priced_as_zero(tmp_path):
    path = tmp_path / "partial.db"
    storage = SQLiteStorage(str(path))
    known = _turn("run-a", "known", 0, _dt(8, 1), 10, input_tokens=8, output_tokens=2)
    unknown = _turn("run-a", "unknown", 1, _dt(8, 2), None)
    mixed = _turn("run-a", "mixed", 2, _dt(8, 3), 20, input_tokens=10, output_tokens=10)
    events = (
        _model_event("known", "m1", "gpt-6-luna"),
        _model_event("mixed", "m2", "gpt-6-luna"),
        _model_event("mixed", "m3", "gpt-6-sol", sequence=1),
    )
    storage.replace_agent_run_bundle(_bundle("local", "run-a", (known, unknown, mixed), events))
    storage.close()

    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    day = next(row for row in report["days"] if row["day"] == "2026-10-08")
    assert day["totalTokens"] == 30
    assert day["tokenKnownTurns"] == 2
    assert day["turns"] == 3
    assert day["pricedTurns"] == 1
    assert day["listPriceUsd"] is not None
    assert day["unattributedTokens"] == 20


def test_priced_components_without_reported_total_keep_model_tokens_unknown(tmp_path):
    path = tmp_path / "missing-total.db"
    storage = SQLiteStorage(str(path))
    turn = replace(_turn("run-a", "turn-a", 0, _dt(8, 1), 100,
                         input_tokens=80, output_tokens=20), total_tokens=None)
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,),
        (_model_event("turn-a", "model-a", "gpt-6-luna"),),
        session_id="session-a",
    ))
    storage.close()
    report = build_agent_usage_bundle(
        path, tenant="local", now=_dt(8, 12), day="2026-10-08",
        session_id="session-a", source_kind="codex",
    )
    day = next(item for item in report["days"] if item["day"] == "2026-10-08")
    source = report["daySources"][0]
    model = report["dayModels"][0]
    session = report["selectedSession"]
    assert day["totalTokens"] is None
    assert source["totalTokens"] is None
    assert model["totalTokens"] is None
    assert session["totalTokens"] is None
    assert session["models"][0]["totalTokens"] is None
    assert day["listPriceUsd"] == source["listPriceUsd"] == model["listPriceUsd"] == session["listPriceUsd"]
    assert day["pricedTurns"] == source["pricedTurns"] == model["pricedTurns"] == 1


def test_usage_does_not_merge_equal_session_ids_from_different_sources(tmp_path):
    path = tmp_path / "source-session.db"
    storage = SQLiteStorage(str(path))
    local = _bundle("local", "run-a", (_turn("run-a", "a", 0, _dt(8, 1), 10),),
                    session_id="shared")
    other = _bundle("local", "run-b", (_turn("run-b", "b", 0, _dt(8, 2), 90),),
                    session_id="shared")
    storage.replace_agent_run_bundle(local)
    storage.replace_agent_run_bundle(replace(
        other, session=replace(other.session, source_kind="claude-code"),
    ))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    assert len(report["sessions"]) == 2
    assert {item["sourceKind"]: item["totalTokens"] for item in report["sessions"]} == {
        "codex": 10, "claude-code": 90,
    }
    assert build_agent_usage_bundle(
        path, tenant="local", now=_dt(8, 12), day="2026-10-08", session_id="shared",
    )["selectedSession"] is None
    selected = build_agent_usage_bundle(
        path, tenant="local", now=_dt(8, 12), day="2026-10-08",
        session_id="shared", source_kind="codex",
    )["selectedSession"]
    assert selected["totalTokens"] == 10


def test_usage_duplicate_model_evidence_does_not_duplicate_tokens_or_price(tmp_path):
    path = tmp_path / "duplicate.db"
    storage = SQLiteStorage(str(path))
    turn = _turn("run-a", "turn-a", 0, _dt(8, 1), 100,
                 input_tokens=80, output_tokens=20)
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,),
        (_model_event("turn-a", "event-a", "gpt-6-luna"),
         _model_event("turn-a", "event-b", "gpt-6-luna", sequence=1)),
    ))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    day = next(row for row in report["days"] if row["day"] == "2026-10-08")
    assert day["totalTokens"] == 100
    assert day["pricedTurns"] == 1
    assert report["dayModels"] == [{
        "model": "gpt-6-luna", "turns": 1, "tokenKnownTurns": 1,
        "totalTokens": 100, "pricedTurns": 1,
        "listPriceUsd": day["listPriceUsd"],
    }]


def test_run_report_prices_only_supported_models_and_links_evidence(tmp_path):
    path = tmp_path / "report.db"
    storage = SQLiteStorage(str(path))
    first = _turn("run-a", "a-1", 0, _dt(8, 1), 100,
                  input_tokens=80, output_tokens=20)
    second = _turn("run-a", "a-2", 1, _dt(8, 2), 300,
                   input_tokens=250, output_tokens=50)
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (first, second),
        (_model_event("a-1", "m1", "gpt-6-luna"),
         _model_event("a-2", "m2", "gpt-6-luna")),
    ))
    storage.close()

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs?run_id=run-a")

    response = asyncio.run(request())
    assert response.status_code == 200
    run = response.json()["runs"][0]
    assert run["sourceTokenUsage"]["pricedTurns"] == 2
    assert run["sourceTokenUsage"]["listPriceUsd"] is not None
    assert run["recommendations"] == []


def test_run_report_suggests_investigation_only_for_unresolved_recorded_failure(tmp_path):
    path = tmp_path / "failure-report.db"
    storage = SQLiteStorage(str(path))
    turns = (_turn("run-a", "a-1", 0, _dt(8, 1), None),)
    failure = AgentEvent(
        "failed-test", "a-1", 0, _dt(8, 1), AgentEventType.TEST_RESULT,
        ExecutionStatus.FAILED, "synthetic", {"failed": 1},
        PrivacyClassification.METADATA,
    )
    storage.replace_agent_run_bundle(_bundle("local", "run-a", turns, (failure,)))
    storage.close()

    async def read():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return (await client.get("/api/runs?run_id=run-a")).json()["runs"][0]

    report = asyncio.run(read())
    suggestion = next(item for item in report["recommendations"] if item["code"] == "review_last_failed_test")
    assert suggestion["status"] == "suggested"
    assert suggestion["evidenceEventIds"] == ["failed-test"]
    assert "rerun" in suggestion["action"].lower()

    storage = SQLiteStorage(str(path))
    passed = AgentEvent(
        "passed-test", "a-1", 1, _dt(8, 2), AgentEventType.TEST_RESULT,
        ExecutionStatus.COMPLETED, "synthetic", {"failed": 0},
        PrivacyClassification.METADATA,
    )
    storage.replace_agent_run_bundle(_bundle("local", "run-a", turns, (failure, passed)))
    storage.close()
    report = asyncio.run(read())
    assert not any(item["code"] == "review_last_failed_test" for item in report["recommendations"])


def test_token_advice_links_observed_growth_and_repeated_captured_result(tmp_path):
    path = tmp_path / "token-advice.db"
    storage = SQLiteStorage(str(path))
    turns = tuple(replace(
        _turn("run-a", f"turn-{index}", index, _dt(8, index + 1), inputs + 100,
              input_tokens=inputs, output_tokens=100),
        cached_input_tokens=cached,
    ) for index, (inputs, cached) in enumerate(((12000, 1000), (18000, 9000), (25000, 20000))))
    events = tuple(AgentEvent(
        f"result-{index}", f"turn-{index}", 0, _dt(8, index + 1),
        AgentEventType.TOOL_RESULT, ExecutionStatus.COMPLETED, "test",
        {"call_id": f"call-{index}", "tool_name": "read", "result": "A" * 2500},
        PrivacyClassification.REDACTED,
    ) for index in range(2))
    storage.replace_agent_run_bundle(_bundle("local", "run-a", turns, events))
    storage.close()

    async def read():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs?run_id=run-a")

    response = asyncio.run(read())
    assert response.status_code == 200
    recommendations = {item["code"]: item for item in response.json()["runs"][0]["recommendations"]}
    assert recommendations["review_input_growth"]["evidenceTurnIds"] == ["turn-0", "turn-2"]
    assert "cache" in recommendations["review_input_growth"]["tradeoff"].lower()
    assert recommendations["review_repeated_large_result"]["evidenceEventIds"] == ["result-0", "result-1"]
    assert all(item["status"] == "suggested" and "%" not in item["action"]
               for item in recommendations.values())


def test_token_advice_does_not_label_one_expensive_turn_or_small_repeated_output_as_waste():
    turns = (
        _turn("run-a", "first", 0, _dt(8, 1), 30000, input_tokens=29000, output_tokens=1000),
        _turn("run-a", "second", 1, _dt(8, 2), 20000, input_tokens=19000, output_tokens=1000),
    )
    events = tuple(AgentEvent(
        f"small-{index}", turn.turn_id, 0, _dt(8, index + 1),
        AgentEventType.TOOL_RESULT, ExecutionStatus.COMPLETED, "test",
        {"result": "same short output", "call_id": f"call-{index}"},
        PrivacyClassification.REDACTED,
    ) for index, turn in enumerate(turns))
    assert run_recommendations(_bundle("local", "run-a", turns, events)) == []


def test_usage_keeps_unknown_model_tokens_visible_without_a_price(tmp_path):
    path = tmp_path / "custom.db"
    storage = SQLiteStorage(str(path))
    turn = _turn("run-a", "turn-a", 0, _dt(8, 1), 50,
                 input_tokens=40, output_tokens=10)
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,),
        (_model_event("turn-a", "custom-model", "my-custom-model"),),
    ))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    assert report["dayModels"][0]["model"] == "my-custom-model"
    assert report["dayModels"][0]["totalTokens"] == 50
    assert report["dayModels"][0]["listPriceUsd"] is None


def test_unknown_model_call_with_known_configuration_withholds_whole_turn_price(tmp_path):
    path = tmp_path / "mixed-evidence.db"
    storage = SQLiteStorage(str(path))
    turn = _turn("run-a", "turn-a", 0, _dt(8, 1), 100,
                 input_tokens=80, output_tokens=20)
    missing_model = AgentEvent(
        "call-without-model", "turn-a", 1, _dt(8, 1), AgentEventType.MODEL_CALL,
        ExecutionStatus.COMPLETED, "test", {"request_model": None},
        PrivacyClassification.METADATA,
    )
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (turn,),
        (_model_event("turn-a", "known-model", "gpt-6-luna"), missing_model),
    ))
    storage.close()

    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    day = next(row for row in report["days"] if row["day"] == "2026-10-08")
    assert day["totalTokens"] == 100
    assert day["pricedTurns"] == 0
    assert day["unattributedTokens"] == 100
    assert report["dayModels"] == []

    async def read():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            runs = await client.get("/api/runs?run_id=run-a")
            session = await client.get("/api/session?source_kind=codex&session_id=source-local-run-a")
            return runs, session

    runs, session = asyncio.run(read())
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["sourceTokenUsage"]["pricedTurns"] == 0
    assert session.status_code == 200
    assert session.json()["turns"][0]["listPriceUsd"] is None


def test_model_evidence_attribution_cases_share_sql_and_bundle_rules(tmp_path):
    path = tmp_path / "attribution-cases.db"
    storage = SQLiteStorage(str(path))
    cases = {
        "known": ("gpt-6-luna",),
        "repeated": ("gpt-6-luna", "gpt-6-luna"),
        "two-models": ("gpt-6-luna", "gpt-6-sol"),
        "missing": ("gpt-6-luna", None),
        "redacted": ("gpt-6-luna", "<redacted>"),
        "unknown-only": (None,),
        "zero-notice": ("gpt-6-luna", None),
    }
    turns = tuple(_turn("run-a", name, index, _dt(8, index + 1), 100,
                        input_tokens=80, output_tokens=20)
                  for index, name in enumerate(cases))
    events = []
    for name, models in cases.items():
        for index, model in enumerate(models):
            events.append(AgentEvent(
                f"{name}-{index}", name, index, _dt(8, 1), AgentEventType.MODEL_CALL,
                ExecutionStatus.COMPLETED, "test", {
                    "request_model": model,
                    **({"input_tokens": 0, "output_tokens": 0}
                       if name == "zero-notice" and model is None else {}),
                },
                PrivacyClassification.METADATA,
            ))
    events.append(AgentEvent(
        "unrelated-context", "known", 1, _dt(8, 1), AgentEventType.CONTEXT,
        ExecutionStatus.COMPLETED, "test", {"name": "other"}, PrivacyClassification.METADATA,
    ))
    storage.replace_agent_run_bundle(_bundle("local", "run-a", turns, tuple(events)))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12), day="2026-10-08")
    day = next(row for row in report["days"] if row["day"] == "2026-10-08")
    assert day["totalTokens"] == 700
    assert day["pricedTurns"] == 3
    assert day["unattributedTokens"] == 400
    assert report["dayModels"][0]["model"] == "gpt-6-luna"
    assert report["dayModels"][0]["totalTokens"] == 300

    async def read():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/runs?run_id=run-a")

    response = asyncio.run(read())
    assert response.status_code == 200
    assert response.json()["runs"][0]["sourceTokenUsage"]["pricedTurns"] == 3


def test_usage_scan_limits_mark_partial_and_do_not_invent_prices(tmp_path, monkeypatch):
    path = tmp_path / "bounded.db"
    storage = SQLiteStorage(str(path))
    turns = tuple(_turn("run-a", f"turn-{index}", index, _dt(8, index + 1), 10,
                        input_tokens=8, output_tokens=2) for index in range(3))
    events = tuple(_model_event(turn.turn_id, f"event-{index}", "gpt-6-luna")
                   for index, turn in enumerate(turns))
    storage.replace_agent_run_bundle(_bundle("local", "run-a", turns, events))
    storage.close()
    monkeypatch.setattr(usage_module, "MAX_USAGE_TURNS", 2)
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12))
    assert report["window"]["turnsComplete"] is False
    assert report["window"]["scannedTurns"] == 2
    monkeypatch.setattr(usage_module, "MAX_USAGE_TURNS", 20_000)
    monkeypatch.setattr(usage_module, "MAX_MODEL_EVENTS", 1)
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12))
    day = next(row for row in report["days"] if row["day"] == "2026-10-08")
    assert report["window"]["modelEvidenceComplete"] is False
    assert day["totalTokens"] == 30
    assert day["pricedTurns"] == 0
    assert day["listPriceUsd"] is None


def test_usage_invalid_day_and_missing_session_do_not_expose_other_data(tmp_path):
    path = tmp_path / "invalid.db"
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle(
        "local", "run-a", (_turn("run-a", "turn-a", 0, _dt(8, 1), 10),),
    ))
    storage.close()
    report = build_agent_usage_bundle(path, tenant="local", now=_dt(8, 12),
                                      day="2026-10-08", session_id="other-session")
    assert report["selectedSession"] is None

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/usage?day=2026-02-30")

    assert asyncio.run(request()).status_code == 400


def test_usage_respects_dashboard_basic_auth(tmp_path, monkeypatch):
    path = tmp_path / "protected.db"
    SQLiteStorage(str(path)).close()
    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            denied = await client.get("/api/usage")
            allowed = await client.get("/api/usage", auth=("reviewer", "secret"))
            return denied, allowed

    denied, allowed = asyncio.run(request())
    assert denied.status_code == 401
    assert allowed.status_code == 200
