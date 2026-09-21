"""Native Turn result race and index integration on disposable Postgres."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from verdict.agent_judgment import AgentTurnJudgment, turn_evidence_fingerprint
from verdict.evidence import (
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    SourceSession,
)
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
        assert not more and rows[0][1].evidence_fingerprint == new.evidence_fingerprint
        assert writer.list_agent_turn_evaluation_candidates("other", "a" * 64)[0] == []
    finally:
        writer.close()
        second.close()
