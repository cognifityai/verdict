"""Native Turn result race and index integration on disposable Postgres."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4

import pytest
import verdict.storage.postgres as postgres_module
from verdict.agent_judgment import (
    AgentTurnJudgment,
    TurnToolCounts,
    agent_turn_judgment_to_json,
    turn_evidence_fingerprint,
)
from verdict.dashboard.app import build_agent_run_detail
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    SourceSession,
)
from verdict.schema import DimensionScore, JudgmentStatus, Verdict
from verdict.storage.postgres import PostgresStorage


def _bundle(tenant):
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    return AgentRunBundle(
        SourceSession("session", tenant, "pydantic-ai", "a" * 64, now, now),
        AgentRun("run", "session", tenant, now, ExecutionStatus.COMPLETED, now),
        (AgentTurn("turn", "run", 0, now, ExecutionStatus.COMPLETED, now,
                   "question", "answer", EvidenceState.PRESENT, EvidenceState.PRESENT),),
    )


def _tool_events():
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    return (
        AgentEvent("call", "turn", 0, now, AgentEventType.TOOL_CALL,
                   ExecutionStatus.COMPLETED, "sdk",
                   {"tool_name": "private_tool", "call_id": "private_id",
                    "tool_origin": "mcp"}),
        AgentEvent("result", "turn", 1, now, AgentEventType.TOOL_RESULT,
                   ExecutionStatus.FAILED, "sdk",
                   {"tool_name": "private_tool", "call_id": "private_id",
                    "is_error": True}),
    )


@pytest.mark.skipif(not os.environ.get("VERDICT_TEST_POSTGRES_DSN"), reason="disposable Postgres required")
def test_postgres_bounded_tool_projection_reads_full_turn_page():
    storage = PostgresStorage(os.environ["VERDICT_TEST_POSTGRES_DSN"])
    tenant = f"turn-batch-{uuid4().hex}"
    try:
        bundle = _bundle(tenant)
        turns = tuple(replace(bundle.turns[0], turn_id=f"turn-{index}", sequence=index)
                      for index in range(100))
        now = bundle.turns[0].started_at
        events = tuple(AgentEvent(
            event_id=f"call-{index}", turn_id=turn.turn_id, sequence=0,
            occurred_at=now, event_type=AgentEventType.TOOL_CALL,
            status=ExecutionStatus.COMPLETED, provenance="sdk",
            attributes={"tool_name": "tool", "call_id": f"id-{index}"},
        ) for index, turn in enumerate(turns))
        storage.replace_agent_run_bundle(replace(bundle, turns=turns, events=events))
        rows, has_more = storage.list_agent_turn_evaluation_candidates(
            tenant, "c" * 64, limit=100, tool_evidence=True,
        )
        assert not has_more
        assert len(rows) == 100
        assert all(counts.calls == 1 and counts.results == 0 for _, _, counts in rows)
        assert storage.list_agent_turn_evaluation_candidates(
            "other", "c" * 64, limit=100, tool_evidence=True,
        )[0] == []
    finally:
        storage.close()


@pytest.mark.skipif(not os.environ.get("VERDICT_TEST_POSTGRES_DSN"), reason="disposable Postgres required")
def test_postgres_tool_projection_treats_invalid_present_origin_as_unusable():
    storage = PostgresStorage(os.environ["VERDICT_TEST_POSTGRES_DSN"])
    tenant = f"turn-origin-invalid-{uuid4().hex}"
    try:
        bundle = _bundle(tenant)
        call, _result = _tool_events()
        storage.replace_agent_run_bundle(replace(bundle, events=(call,)))
        with storage._pool.connection() as conn:
            conn.execute(
                "UPDATE agent_events SET attributes_json=jsonb_set("
                "attributes_json,'{tool_origin}',%s::jsonb) "
                "WHERE tenant_id=%s AND run_id='run' AND event_id='call'",
                ('{"private":"CANARY"}', tenant),
            )

        [(turn, status, counts)], more = storage.list_agent_turn_evaluation_candidates(
            tenant, "e" * 64, tool_evidence=True,
        )
        assert not more and status is None
        assert turn.turn_id == "turn"
        assert counts.origin_unusable_calls == 1
        assert counts.mcp_calls == counts.origin_not_captured_calls == 0
        assert "CANARY" not in counts.prompt_block()
    finally:
        storage.close()


@pytest.mark.skipif(not os.environ.get("VERDICT_TEST_POSTGRES_DSN"), reason="disposable Postgres required")
def test_postgres_tool_counts_bind_preview_storage_and_dashboard():
    storage = PostgresStorage(os.environ["VERDICT_TEST_POSTGRES_DSN"])
    tenant = f"turn-tools-{uuid4().hex}"
    try:
        bundle = _bundle(tenant)
        other_tenant = f"turn-tools-other-{uuid4().hex}"
        call, _result = _tool_events()
        with_call = replace(bundle, events=(call,))
        storage.replace_agent_run_bundle(with_call)
        storage.replace_agent_run_bundle(replace(_bundle(other_tenant), events=(call,)))
        [(turn, status, counts)], more = storage.list_agent_turn_evaluation_candidates(
            tenant, "c" * 64, tool_evidence=True,
        )
        assert not more and status is None
        assert (counts.calls, counts.results, counts.error_results) == (1, 0, 0)
        assert (counts.mcp_calls, counts.origin_not_captured_calls) == (1, 0)
        judgment = AgentTurnJudgment(
            tenant_id=tenant, run_id="run", turn_id="turn", evaluator_fingerprint="c" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(turn, counts),
            evaluator_provider="anthropic", evaluator_config={
                "tool_evidence_mode": "counts_v1",
                "tool_evidence_template": TurnToolCounts.PROMPT_TEMPLATE,
            },
            judge_models=["test"], expected_dimensions=["quality"],
            rubric_name="test", rubric_version="1",
            dimensions=[DimensionScore("quality", Verdict.PASS, "ok", "test")],
        )
        assert storage.save_agent_turn_judgment_if_current(judgment) == "saved"
        visible = build_agent_run_detail(os.environ["VERDICT_TEST_POSTGRES_DSN"],
                                         tenant=tenant, run_id="run")
        assert visible["turns"][0]["evaluation"]["toolEvidence"] == "counts_v1"
        assert visible["turns"][0]["evaluation"]["toolCounts"]["calls"] == 1
        assert visible["turns"][0]["evaluation"]["toolCounts"]["mcpCalls"] == 1
        other = build_agent_run_detail(os.environ["VERDICT_TEST_POSTGRES_DSN"],
                                       tenant=other_tenant, run_id="run")
        assert other["turns"][0]["evaluation"] is None

        with storage._pool.connection() as conn:
            conn.execute(
                "UPDATE agent_turn_judgments SET result_json=jsonb_set("
                "result_json::jsonb,'{evaluator_config,tool_evidence_mode}',"
                "'[]'::jsonb)::text WHERE tenant_id=%s AND run_id='run' AND turn_id='turn'",
                (tenant,),
            )
        rows, _ = storage.list_agent_turn_evaluation_candidates(
            tenant, "c" * 64, tool_evidence=True,
        )
        assert rows[0][1] is None
        assert build_agent_run_detail(os.environ["VERDICT_TEST_POSTGRES_DSN"],
                                      tenant=tenant, run_id="run")["turns"][0]["evaluation"] is None
        with storage._pool.connection() as conn:
            conn.execute(
                "UPDATE agent_turn_judgments SET result_json=%s "
                "WHERE tenant_id=%s AND run_id='run' AND turn_id='turn'",
                (agent_turn_judgment_to_json(judgment), tenant),
            )

        # Simulate an already-stored origin correction. Normalized Postgres
        # events are immutable through the public capture API.
        with storage._pool.connection() as conn:
            conn.execute(
                "UPDATE agent_events SET attributes_json=jsonb_set("
                "attributes_json,'{tool_origin}','\"application\"'::jsonb) "
                "WHERE tenant_id=%s AND run_id='run' AND event_id='call'",
                (tenant,),
            )
        assert storage.save_agent_turn_judgment_if_current(judgment) == "stale"
        rows, _ = storage.list_agent_turn_evaluation_candidates(
            tenant, "c" * 64, tool_evidence=True,
        )
        assert rows[0][1] is None
        assert build_agent_run_detail(os.environ["VERDICT_TEST_POSTGRES_DSN"],
                                      tenant=tenant, run_id="run")["turns"][0]["evaluation"] is None
    finally:
        storage.close()


@pytest.mark.skipif(not os.environ.get("VERDICT_TEST_POSTGRES_DSN"), reason="disposable Postgres required")
@pytest.mark.parametrize("first", ["capture", "save"])
def test_postgres_tool_count_cas_serializes_with_event_append(monkeypatch, first):
    dsn = os.environ["VERDICT_TEST_POSTGRES_DSN"]
    writer = PostgresStorage(dsn)
    second = PostgresStorage(dsn)
    tenant = f"turn-tool-race-{uuid4().hex}"
    entered = Event()
    follower_started = Event()
    release = Event()
    try:
        bundle = _bundle(tenant)
        call, result = _tool_events()
        writer.replace_agent_run_bundle(replace(bundle, events=(call,)))
        [(turn, _, counts)], _ = writer.list_agent_turn_evaluation_candidates(
            tenant, "d" * 64, tool_evidence=True,
        )
        old = AgentTurnJudgment(
            tenant_id=tenant, run_id="run", turn_id="turn", evaluator_fingerprint="d" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(turn, counts),
            evaluator_provider="anthropic", evaluator_config={
                "tool_evidence_mode": "counts_v1",
                "tool_evidence_template": TurnToolCounts.PROMPT_TEMPLATE,
            },
            judge_models=["test"], expected_dimensions=["quality"],
            rubric_name="test", rubric_version="1",
            dimensions=[DimensionScore("quality", Verdict.PASS, "ok", "test")],
        )
        changed = replace(bundle, events=(call, result))
        if first == "capture":
            original_write = second._write_normalized_bundle_cursor

            def paused_write(*args, **kwargs):
                original_write(*args, **kwargs)
                entered.set()
                assert release.wait(10)

            monkeypatch.setattr(second, "_write_normalized_bundle_cursor", paused_write)
        else:
            original_decision = postgres_module.turn_judgment_write_decision

            def paused_decision(*args, **kwargs):
                entered.set()
                assert release.wait(10)
                return original_decision(*args, **kwargs)

            monkeypatch.setattr(postgres_module, "turn_judgment_write_decision", paused_decision)
        with ThreadPoolExecutor(max_workers=2) as pool:
            def follow(action, argument):
                follower_started.set()
                return action(argument)

            if first == "capture":
                leading = pool.submit(second.replace_agent_run_bundle, changed)
                assert entered.wait(10)
                trailing = pool.submit(follow, writer.save_agent_turn_judgment_if_current, old)
            else:
                leading = pool.submit(writer.save_agent_turn_judgment_if_current, old)
                assert entered.wait(10)
                trailing = pool.submit(follow, second.replace_agent_run_bundle, changed)
            assert follower_started.wait(10)
            assert not trailing.done()
            release.set()
            assert leading.result(timeout=10) == ("saved" if first == "save" else None)
            assert trailing.result(timeout=10) == ("stale" if first == "capture" else None)
        rows, _ = writer.list_agent_turn_evaluation_candidates(
            tenant, "d" * 64, tool_evidence=True,
        )
        assert rows[0][1] is None
    finally:
        release.set()
        writer.close()
        second.close()


@pytest.mark.skipif(not os.environ.get("VERDICT_TEST_POSTGRES_DSN"), reason="disposable Postgres required")
def test_postgres_turn_judge_guard_scopes_identity_and_leaves_pool_available():
    dsn = os.environ["VERDICT_TEST_POSTGRES_DSN"]
    storage = PostgresStorage(dsn, max_pool=1)
    other = PostgresStorage(dsn, max_pool=1)
    tenant = f"turn-guard-{uuid4().hex}"
    try:
        storage.replace_agent_run_bundle(_bundle(tenant))
        with storage.agent_turn_judge_guard(tenant, "run", "turn", "a" * 64) as acquired:
            assert acquired
            assert storage.get_agent_run_bundle(tenant, "run") is not None
            with other.agent_turn_judge_guard(tenant, "run", "turn", "a" * 64) as busy:
                assert not busy
            with other.agent_turn_judge_guard(tenant, "run", "turn", "b" * 64) as distinct:
                assert distinct
            with other.agent_turn_judge_guard("other", "run", "turn", "a" * 64) as isolated:
                assert isolated
        with other.agent_turn_judge_guard(tenant, "run", "turn", "a" * 64) as released:
            assert released
    finally:
        storage.close()
        other.close()


@pytest.mark.skipif(not os.environ.get("VERDICT_TEST_POSTGRES_DSN"), reason="disposable Postgres required")
def test_postgres_turn_judgment_cas_with_two_connections_and_extension():
    dsn = os.environ["VERDICT_TEST_POSTGRES_DSN"]
    writer = PostgresStorage(dsn)
    second = PostgresStorage(dsn)
    tenant = f"turn-{uuid4().hex}"
    try:
        bundle = _bundle(tenant)
        writer.replace_agent_run_bundle(bundle)
        identity = dict(
            tenant_id=tenant, run_id="run", turn_id="turn",
            evaluator_fingerprint="a" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
            evaluator_provider="anthropic", evaluator_config={},
            judge_models=["test"], expected_dimensions=["quality"],
            rubric_name="test", rubric_version="1",
        )
        old = AgentTurnJudgment(**identity, dimensions=[
            DimensionScore("quality", Verdict.PASS, "ok", "test"),
        ])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda store: store.save_agent_turn_judgment_if_current(old),
                                    (writer, second)))
        assert sorted(results) == ["already_completed", "saved"]
        changed = replace(bundle, turns=(replace(bundle.turns[0],
                                                 final_response_redacted="answer extended"),))
        second.replace_agent_run_bundle(changed)
        new = replace(old, evidence_fingerprint=turn_evidence_fingerprint(changed.turns[0]))
        with ThreadPoolExecutor(max_workers=2) as pool:
            old_result = pool.submit(writer.save_agent_turn_judgment_if_current, old)
            new_result = pool.submit(second.save_agent_turn_judgment_if_current, new)
            assert old_result.result() == "stale"
            assert new_result.result() == "saved"
        rows, more = writer.list_agent_turn_evaluation_candidates(tenant, "a" * 64)
        assert not more and rows[0][1] is JudgmentStatus.COMPLETED
        assert writer._fetchone(
            "SELECT evidence_fingerprint FROM agent_turn_judgments "
            "WHERE tenant_id=%s AND run_id=%s AND turn_id=%s AND evaluator_fingerprint=%s",
            (tenant, "run", "turn", "a" * 64),
        )[0] == new.evidence_fingerprint
        assert writer.list_agent_turn_evaluation_candidates("other", "a" * 64)[0] == []
    finally:
        writer.close()
        second.close()


@pytest.mark.skipif(not os.environ.get("VERDICT_TEST_POSTGRES_DSN"), reason="disposable Postgres required")
@pytest.mark.parametrize("first", ["capture", "save"])
def test_postgres_turn_save_and_capture_interleave_under_row_lock(monkeypatch, first):
    writer = PostgresStorage(os.environ["VERDICT_TEST_POSTGRES_DSN"])
    second = PostgresStorage(os.environ["VERDICT_TEST_POSTGRES_DSN"])
    tenant = f"turn-race-{uuid4().hex}"
    entered = Event()
    follower_started = Event()
    release = Event()
    try:
        bundle = _bundle(tenant)
        writer.replace_agent_run_bundle(bundle)
        changed = replace(bundle, turns=(replace(bundle.turns[0],
                                                 final_response_redacted="answer extended"),))
        old = AgentTurnJudgment(
            tenant_id=tenant, run_id="run", turn_id="turn", evaluator_fingerprint="a" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
            evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
            expected_dimensions=["quality"], rubric_name="test", rubric_version="1",
            dimensions=[DimensionScore("quality", Verdict.PASS, "ok", "test")],
        )
        if first == "capture":
            original_write = second._write_normalized_bundle_cursor

            def paused_write(*args, **kwargs):
                original_write(*args, **kwargs)
                entered.set()  # The changed Turn is written but not committed; row lock is held.
                assert release.wait(10)

            monkeypatch.setattr(second, "_write_normalized_bundle_cursor", paused_write)
        else:
            original_decision = postgres_module.turn_judgment_write_decision

            def paused_decision(*args, **kwargs):
                entered.set()  # save already holds SELECT FOR UPDATE on the Turn.
                assert release.wait(10)
                return original_decision(*args, **kwargs)

            monkeypatch.setattr(postgres_module, "turn_judgment_write_decision", paused_decision)
        with ThreadPoolExecutor(max_workers=2) as pool:
            def follow(action, argument):
                follower_started.set()
                return action(argument)

            if first == "capture":
                leading = pool.submit(second.replace_agent_run_bundle, changed)
                assert entered.wait(10)
                trailing = pool.submit(follow, writer.save_agent_turn_judgment_if_current, old)
            else:
                leading = pool.submit(writer.save_agent_turn_judgment_if_current, old)
                assert entered.wait(10)
                trailing = pool.submit(follow, second.replace_agent_run_bundle, changed)
            assert follower_started.wait(10)
            assert not trailing.done()
            release.set()
            assert leading.result(timeout=10) == ("saved" if first == "save" else None)
            assert trailing.result(timeout=10) == ("stale" if first == "capture" else None)
        rows, _ = writer.list_agent_turn_evaluation_candidates(tenant, "a" * 64)
        assert rows[0][1] is None  # An old completed slot is never current after extension.
    finally:
        release.set()
        writer.close()
        second.close()


@pytest.mark.skipif(not os.environ.get("VERDICT_TEST_POSTGRES_DSN"), reason="disposable Postgres required")
def test_postgres_malformed_current_slot_is_not_judged_and_can_be_replaced():
    storage = PostgresStorage(os.environ["VERDICT_TEST_POSTGRES_DSN"])
    tenant = f"turn-corrupt-{uuid4().hex}"
    try:
        bundle = _bundle(tenant)
        storage.replace_agent_run_bundle(bundle)
        result = AgentTurnJudgment(
            tenant_id=tenant, run_id="run", turn_id="turn", evaluator_fingerprint="a" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
            evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
            expected_dimensions=["quality"], rubric_name="test", rubric_version="1",
            dimensions=[DimensionScore("quality", Verdict.PASS, "ok", "test")],
            evaluated_at=datetime(2026, 8, 31, 10, tzinfo=timezone(timedelta(hours=2))),
        )
        assert storage.save_agent_turn_judgment_if_current(result) == "saved"
        with storage._pool.connection() as conn:
            conn.execute(
                "UPDATE agent_turn_judgments SET result_json='[]' WHERE tenant_id=%s",
                (tenant,),
            )
        rows, _ = storage.list_agent_turn_evaluation_candidates(tenant, "a" * 64)
        assert rows[0][1] is None
        assert storage.save_agent_turn_judgment_if_current(result) == "saved"
        rows, _ = storage.list_agent_turn_evaluation_candidates(tenant, "a" * 64)
        assert rows[0][1] is JudgmentStatus.COMPLETED
        newer = replace(result, evaluator_fingerprint="b" * 64,
                        evaluated_at=datetime(2026, 8, 31, 9, tzinfo=timezone.utc))
        assert storage.save_agent_turn_judgment_if_current(newer) == "saved"
        latest = build_agent_run_detail(os.environ["VERDICT_TEST_POSTGRES_DSN"],
                                        tenant=tenant, run_id="run")
        assert latest["turns"][0]["evaluation"]["evaluatorFingerprint"] == "b" * 64
        with storage._pool.connection() as conn:
            conn.execute("UPDATE agent_turn_judgments SET result_json='[]' "
                         "WHERE tenant_id=%s AND evaluator_fingerprint=%s", (tenant, "b" * 64))
        detail = build_agent_run_detail(os.environ["VERDICT_TEST_POSTGRES_DSN"],
                                        tenant=tenant, run_id="run")
        assert detail["turns"][0]["evaluation"]["evaluatorFingerprint"] == "a" * 64
    finally:
        storage.close()
