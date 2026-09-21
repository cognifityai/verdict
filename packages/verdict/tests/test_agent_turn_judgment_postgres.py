"""Native Turn result race and index integration on disposable Postgres."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event
from uuid import uuid4

import pytest
import verdict.storage.postgres as postgres_module
from verdict.agent_judgment import AgentTurnJudgment, turn_evidence_fingerprint
from verdict.evidence import (
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    SourceSession,
)
from verdict.schema import JudgmentStatus
from verdict.storage.postgres import PostgresStorage


def _bundle(tenant):
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    return AgentRunBundle(
        SourceSession("session", tenant, "pydantic-ai", "a" * 64, now, now),
        AgentRun("run", "session", tenant, now, ExecutionStatus.COMPLETED, now),
        (AgentTurn("turn", "run", 0, now, ExecutionStatus.COMPLETED, now,
                   "question", "answer", EvidenceState.PRESENT, EvidenceState.PRESENT),),
    )


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
        old = AgentTurnJudgment(**identity)
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
    finally:
        storage.close()
