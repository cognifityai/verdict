import builtins
import importlib.util
import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
import verdict
from verdict.agent_judgment import (
    AgentTurnJudgment,
    agent_turn_judgment_from_json,
    agent_turn_judgment_to_json,
    turn_evidence_fingerprint,
)
from verdict.dashboard.evaluator_lab import (
    _IdentityOnlyProvider,
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
from verdict.schema import DimensionScore, Judgment, JudgmentStatus, Trace, Verdict
from verdict.storage import InMemoryStorage
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage
from verdict_eval.providers import AnthropicAdapter, CompletionResponse


def test_evaluator_environment_reports_openai_compatible_endpoint_without_value(
    monkeypatch,
):
    endpoint = "http://127.0.0.1:11434/v1"
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    monkeypatch.setenv("OPENAI_API_KEY", "ollama")

    environment = evaluator_environment()
    openai = next(
        item for item in environment["providers"] if item["provider"] == "openai"
    )

    assert openai["configured"] is True
    assert openai["customEndpointConfigured"] is True
    assert endpoint not in str(environment)


class CountingProvider:
    name = "anthropic"

    def __init__(self):
        self.supports_temperature = _IdentityOnlyProvider("anthropic").supports_temperature
        self.calls = 0
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        return CompletionResponse(
            text=(
                '{"relevance":{"reasoning":"direct","verdict":"PASS"},'
                '"completeness":{"reasoning":"complete","verdict":"PASS"}}'
            ),
            input_tokens=10,
            output_tokens=8,
        )


def _trace(trace_id, *, prompt="question", response="answer"):
    return Trace(
        trace_id=trace_id,
        started_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        provider="anthropic",
        request_model="claude-test",
        response_model="claude-test",
        prompt_redacted=prompt,
        response_redacted=response,
        tenant_id="local",
        cluster_id="all",
    )


def _agent_turn_bundle(*, tenant="local"):
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    return AgentRunBundle(
        session=SourceSession(
            source_session_id="session-1", tenant_id=tenant,
            source_kind="pydantic-ai", source_locator_hash="a" * 64,
            started_at=now, observed_at=now,
        ),
        run=AgentRun(
            run_id="run-1", source_session_id="session-1", tenant_id=tenant,
            started_at=now, ended_at=now, status=ExecutionStatus.COMPLETED,
        ),
        turns=(AgentTurn(
            turn_id="turn-1", run_id="run-1", sequence=0, started_at=now,
            ended_at=now, status=ExecutionStatus.COMPLETED,
            user_request_redacted="What is the answer?",
            final_response_redacted="The answer is 42.",
            request_state=EvidenceState.PRESENT,
            response_state=EvidenceState.PRESENT,
        ),),
    )


def _stored_turn_result(storage, fingerprint):
    if isinstance(storage, SQLiteStorage):
        raw = storage._conn.execute(
            "SELECT result_json FROM agent_turn_judgments WHERE tenant_id=? AND run_id=? "
            "AND turn_id=? AND evaluator_fingerprint=?",
            ("local", "run-1", "turn-1", fingerprint),
        ).fetchone()[0]
    else:
        raw = storage._agent_turn_judgments[("local", "run-1", "turn-1", fingerprint)]
    return agent_turn_judgment_from_json(raw)


def _turn_judge_process(path, tenant, approved, observations, release):
    class WaitingProvider(CountingProvider):
        def complete(self, request):
            observations.put("provider_called")
            if not release.wait(10):
                raise TimeoutError("test provider wait expired")
            return super().complete(request)

    storage = (PostgresStorage(path, max_pool=1) if path.startswith("postgres")
               else SQLiteStorage(path))
    try:
        result = execute_evaluation(
            storage, tenant_id=tenant, config=approved,
            confirm_external_egress=True, provider=WaitingProvider(),
        )
        observations.put(("result", result["completed"], result["alreadyJudged"],
                          result["inProgress"]))
    finally:
        storage.close()


def _exit_while_holding_turn_guard(path, entered):
    storage = SQLiteStorage(path)
    with storage.agent_turn_judge_guard("local", "run-1", "turn-1", "a" * 64) as acquired:
        if acquired:
            entered.set()
            os._exit(0)
    os._exit(1)


def test_turn_judge_does_not_egress_twice_across_sqlite_processes(tmp_path):
    path = str(tmp_path / "cross-process.db")
    storage = SQLiteStorage(path)
    try:
        storage.replace_agent_run_bundle(_agent_turn_bundle())
        config = {**_config(), "unit": "agent_turn"}
        preview = preview_evaluation(storage, tenant_id="local", config=config)
        approved = {**config, "planFingerprint": preview["planFingerprint"],
                    "plannedTurns": preview["plannedTurns"]}
    finally:
        storage.close()

    ctx = multiprocessing.get_context("spawn")
    observations = ctx.Queue()
    release = ctx.Event()
    first = ctx.Process(target=_turn_judge_process, args=(path, "local", approved, observations, release))
    second = ctx.Process(target=_turn_judge_process, args=(path, "local", approved, observations, release))
    try:
        first.start()
        assert observations.get(timeout=10) == "provider_called"
        # The judge lock must not hold a transaction on the evidence DB.
        writer = SQLiteStorage(path)
        try:
            writer.replace_agent_run_bundle(_agent_turn_bundle(tenant="other"))
        finally:
            writer.close()
        second.start()
        # A second provider entry while the first is in flight is the defect.
        assert observations.get(timeout=10) == ("result", 0, 0, 1)
    finally:
        release.set()
        for process in (first, second):
            if process.pid:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
    assert first.exitcode == second.exitcode == 0
    assert observations.get(timeout=10) == ("result", 1, 0, 0)


def test_sqlite_turn_guard_resolves_aliases_and_releases_on_process_exit(tmp_path):
    path = tmp_path / "guard.db"
    alias = tmp_path / "alias.db"
    storage = SQLiteStorage(str(path))
    alias.symlink_to(path)
    aliased = SQLiteStorage(str(alias))
    try:
        with storage.agent_turn_judge_guard("local", "run-1", "turn-1", "a" * 64) as acquired:
            assert acquired
            with aliased.agent_turn_judge_guard("local", "run-1", "turn-1", "a" * 64) as busy:
                assert not busy
    finally:
        aliased.close()
        storage.close()

    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    process = ctx.Process(target=_exit_while_holding_turn_guard, args=(str(path), entered))
    process.start()
    try:
        assert entered.wait(10)
        process.join(timeout=10)
        assert process.exitcode == 0
        recovered = SQLiteStorage(str(path))
        try:
            with recovered.agent_turn_judge_guard("local", "run-1", "turn-1", "a" * 64) as acquired:
                assert acquired
        finally:
            recovered.close()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)


@pytest.mark.skipif(not os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                    reason="disposable Postgres required")
def test_turn_judge_does_not_egress_twice_across_postgres_processes():
    dsn = os.environ["VERDICT_TEST_POSTGRES_DSN"]
    tenant = f"turn-guard-{uuid4().hex}"
    storage = PostgresStorage(dsn, max_pool=1)
    try:
        storage.replace_agent_run_bundle(_agent_turn_bundle(tenant=tenant))
        config = {**_config(), "unit": "agent_turn"}
        preview = preview_evaluation(storage, tenant_id=tenant, config=config)
        approved = {**config, "planFingerprint": preview["planFingerprint"],
                    "plannedTurns": preview["plannedTurns"]}
    finally:
        storage.close()

    ctx = multiprocessing.get_context("spawn")
    observations = ctx.Queue()
    release = ctx.Event()
    first = ctx.Process(target=_turn_judge_process, args=(dsn, tenant, approved, observations, release))
    second = ctx.Process(target=_turn_judge_process, args=(dsn, tenant, approved, observations, release))
    try:
        first.start()
        assert observations.get(timeout=15) == "provider_called"
        second.start()
        assert observations.get(timeout=15) == ("result", 0, 0, 1)
    finally:
        release.set()
        for process in (first, second):
            if process.pid:
                process.join(timeout=15)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
    assert first.exitcode == second.exitcode == 0
    assert observations.get(timeout=10) == ("result", 1, 0, 0)


def test_agent_turn_preview_uses_final_output_without_relabeling_provider_trace():
    storage = InMemoryStorage()
    storage.replace_agent_run_bundle(_agent_turn_bundle())
    storage.insert_trace(_trace("provider-tool-call", response=""))

    preview = preview_evaluation(
        storage, tenant_id="local", config={**_config(), "unit": "agent_turn"},
    )

    assert preview["unit"] == "agent_turn"
    assert preview["availableTurns"] == 1
    assert preview["eligible"] == 1
    assert preview["plannedCalls"] == 1
    assert preview["plannedTurns"][0]["runId"] == "run-1"
    assert preview["plannedTurns"][0]["turnId"] == "turn-1"
    assert storage.list_judgments_for_trace("provider-tool-call") == []


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_native_turn_judgment_persists_only_current_tenant_evidence(tmp_path, backend):
    storage = InMemoryStorage() if backend == "memory" else SQLiteStorage(str(tmp_path / "turns.db"))
    try:
        storage.replace_agent_run_bundle(_agent_turn_bundle())
        storage.replace_agent_run_bundle(_agent_turn_bundle(tenant="other"))
        storage.insert_trace(_trace("provider-tool-call", response=""))
        config = {**_config(), "unit": "agent_turn"}
        preview = preview_evaluation(storage, tenant_id="local", config=config)
        provider = CountingProvider()
        result = execute_evaluation(
            storage, tenant_id="local",
            config={**config, "planFingerprint": preview["planFingerprint"],
                    "plannedTurns": preview["plannedTurns"]},
            provider=provider, confirm_external_egress=True,
        )
        assert result["completed"] == 1
        assert provider.calls == 1
        assert storage.list_judgments_for_trace("provider-tool-call") == []
        assert "The answer is 42." in str(provider.requests[0].messages)
        assert storage.list_agent_turn_evaluation_candidates("other", result["evaluatorFingerprint"])[0][0][1] is None
        [(_turn, status)] = storage.list_agent_turn_evaluation_candidates("local", result["evaluatorFingerprint"])[0]
        assert status is JudgmentStatus.COMPLETED
        judgment = _stored_turn_result(storage, result["evaluatorFingerprint"])
        assert [dim.name for dim in judgment.dimensions] == ["relevance", "completeness"]
        assert "The answer is 42." not in str(judgment)
        assert preview_evaluation(storage, tenant_id="local", config=config)["alreadyJudged"] == 1

        old = judgment
        bundle = _agent_turn_bundle()
        changed = replace(bundle, turns=(replace(
            bundle.turns[0], final_response_redacted="The answer is 42. Additional context.",
        ),))
        storage.replace_agent_run_bundle(changed)
        [(_turn, judgment)] = storage.list_agent_turn_evaluation_candidates("local", result["evaluatorFingerprint"])[0]
        assert judgment is None
        assert storage.save_agent_turn_judgment_if_current(old) == "stale"
        assert preview_evaluation(storage, tenant_id="local", config=config)["plannedCalls"] == 1
    finally:
        storage.close()


def test_turn_plan_rejects_tenant_replay_and_changed_evidence_without_provider_call():
    storage = InMemoryStorage()
    storage.replace_agent_run_bundle(_agent_turn_bundle())
    storage.replace_agent_run_bundle(_agent_turn_bundle(tenant="other"))
    config = {**_config(), "unit": "agent_turn"}
    preview = preview_evaluation(storage, tenant_id="local", config=config)
    approved = {**config, "planFingerprint": preview["planFingerprint"], "plannedTurns": preview["plannedTurns"]}
    provider = CountingProvider()
    with pytest.raises(ValueError, match="plan"):
        execute_evaluation(storage, tenant_id="other", config=approved,
                           provider=provider, confirm_external_egress=True)
    assert provider.calls == 0
    bundle = _agent_turn_bundle()
    storage.replace_agent_run_bundle(replace(bundle, turns=(replace(
        bundle.turns[0], final_response_redacted="The answer is 42. Revised.",
    ),)))
    with pytest.raises(ValueError, match="plan"):
        execute_evaluation(storage, tenant_id="local", config=approved,
                           provider=provider, confirm_external_egress=True)
    assert provider.calls == 0


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_turn_save_cas_late_error_and_concurrent_evidence_change(tmp_path, backend):
    storage = InMemoryStorage() if backend == "memory" else SQLiteStorage(str(tmp_path / "cas.db"))
    try:
        bundle = _agent_turn_bundle()
        storage.replace_agent_run_bundle(bundle)
        turn = bundle.turns[0]
        identity = {
            "tenant_id": "local", "run_id": "run-1", "turn_id": "turn-1",
            "evaluator_fingerprint": "a" * 64, "evidence_fingerprint": turn_evidence_fingerprint(turn),
            "evaluator_provider": "anthropic", "evaluator_config": {},
            "judge_models": ["test"], "expected_dimensions": ["relevance"],
            "rubric_name": "test", "rubric_version": "1",
        }
        error = AgentTurnJudgment(**identity, status="error", error="temporary")
        success = AgentTurnJudgment(**identity, dimensions=[
            DimensionScore("relevance", Verdict.PASS, "ok", "test"),
        ])
        assert storage.save_agent_turn_judgment_if_current(error) == "saved"
        assert storage.save_agent_turn_judgment_if_current(success) == "saved"
        assert storage.save_agent_turn_judgment_if_current(error) == "already_completed"
        assert storage.save_agent_turn_judgment_if_current(success) == "already_completed"
        changed = replace(bundle, turns=(replace(
            turn, final_response_redacted="The answer is 42. Revised.",
        ),))
        storage.replace_agent_run_bundle(changed)
        assert storage.save_agent_turn_judgment_if_current(success) == "stale"
        updated = replace(success, evidence_fingerprint=turn_evidence_fingerprint(changed.turns[0]))
        assert storage.save_agent_turn_judgment_if_current(updated) == "saved"
        assert storage.save_agent_turn_judgment_if_current(success) == "stale"
        rows, more = storage.list_agent_turn_evaluation_candidates("local", "a" * 64)
        assert more is False and rows[0][1] is JudgmentStatus.COMPLETED
        assert _stored_turn_result(storage, "a" * 64).evidence_fingerprint == updated.evidence_fingerprint
    finally:
        storage.close()


def test_turn_scan_counts_ineligible_candidates_and_keyset_pages():
    storage = InMemoryStorage()
    bundle = _agent_turn_bundle()
    first = replace(bundle.turns[0], turn_id="turn-0", sequence=0,
                    final_response_redacted=None, response_state=EvidenceState.MISSING)
    second = replace(bundle.turns[0], turn_id="turn-1", sequence=1)
    storage.replace_agent_run_bundle(replace(bundle, turns=(first, second)))
    config = {**_config(), "unit": "agent_turn", "scanLimit": 1}
    page = preview_evaluation(storage, tenant_id="local", config=config)
    assert page["availableTurns"] == 1 and page["hasMore"]
    assert page["plannedCalls"] == 1
    older = preview_evaluation(storage, tenant_id="local", config={**config, "before": page["nextCursor"]})
    assert older["plannedCalls"] == 0
    assert older["notEvaluableReasons"] == {"response_unavailable": 1}
    assert not older["hasMore"]


def test_turn_config_rejects_malformed_cursor_and_needs_egress_confirmation():
    storage = InMemoryStorage()
    storage.replace_agent_run_bundle(_agent_turn_bundle())
    config = {**_config(), "unit": "agent_turn"}
    with pytest.raises(ValueError, match="cursor"):
        preview_evaluation(storage, tenant_id="local", config={**config, "before": {"startedAt": "bad"}})
    with pytest.raises(ValueError, match="egress"):
        execute_evaluation(storage, tenant_id="local", config=config, confirm_external_egress=False)


def test_real_openai_sdk_tool_response_and_agent_sdk_final_turn_are_distinct():
    openai = pytest.importorskip("openai")
    storage = InMemoryStorage()
    verdict.shutdown()
    verdict.init(storage=storage, tenant_id="local", instrumentors=["openai"])
    try:
        def respond(_request):
            return httpx.Response(200, json={
                "id": "chatcmpl-local", "object": "chat.completion", "created": 1,
                "model": "gpt-4o-mini", "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "call-1", "type": "function", "function": {
                            "name": "final_result", "arguments": '{"answer":"42"}',
                        },
                    }],
                }, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
            })
        with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
            provider = openai.OpenAI(api_key="local-test-only", base_url="http://provider.test/v1",
                                    http_client=http_client)
            with verdict.agent_run(name="structured-agent") as run:
                with run.turn(user_input="What is the answer?") as turn:
                    provider.chat.completions.create(
                        model="gpt-4o-mini", messages=[{"role": "user", "content": "What is the answer?"}],
                    )
                    turn.set_output("The answer is 42.")
        [trace] = storage.list_traces(tenant_id="local")
        assert not (trace.response_redacted or "").strip()
        assert preview_evaluation(storage, tenant_id="local", config=_config())["eligible"] == 0
        preview = preview_evaluation(storage, tenant_id="local", config={**_config(), "unit": "agent_turn"})
        assert preview["eligible"] == 1 and preview["plannedCalls"] == 1
    finally:
        verdict.shutdown()


def test_turn_changes_during_judge_call_result_is_not_published():
    storage = InMemoryStorage()
    original = _agent_turn_bundle()
    storage.replace_agent_run_bundle(original)
    config = {**_config(), "unit": "agent_turn"}
    preview = preview_evaluation(storage, tenant_id="local", config=config)

    class ExtendingProvider(CountingProvider):
        def complete(self, request):
            storage.replace_agent_run_bundle(replace(original, turns=(replace(
                original.turns[0], final_response_redacted="The answer is 42. Later extension.",
            ),)))
            return super().complete(request)

    result = execute_evaluation(storage, tenant_id="local", provider=ExtendingProvider(),
                                config={**config, "planFingerprint": preview["planFingerprint"],
                                        "plannedTurns": preview["plannedTurns"]},
                                confirm_external_egress=True)
    assert result["completed"] == 0 and result["stale"] == 1
    assert preview_evaluation(storage, tenant_id="local", config=config)["plannedCalls"] == 1


def test_oversize_judge_reasoning_becomes_bounded_error_not_partial_result():
    storage = InMemoryStorage()
    storage.replace_agent_run_bundle(_agent_turn_bundle())
    config = {**_config(), "unit": "agent_turn"}
    preview = preview_evaluation(storage, tenant_id="local", config=config)

    class OversizeProvider(CountingProvider):
        def complete(self, request):
            result = super().complete(request)
            return replace(result, text='{"relevance":{"reasoning":"' + "x" * 70000 +
                           '","verdict":"PASS"},"completeness":{"reasoning":"ok","verdict":"PASS"}}')

    result = execute_evaluation(storage, tenant_id="local", provider=OversizeProvider(),
                                config={**config, "planFingerprint": preview["planFingerprint"],
                                        "plannedTurns": preview["plannedTurns"]},
                                confirm_external_egress=True)
    assert result["errors"] == 1 and result["completed"] == 0
    rows, _ = storage.list_agent_turn_evaluation_candidates("local", result["evaluatorFingerprint"])
    assert rows[0][1] is JudgmentStatus.ERROR


def test_turn_deleted_while_judge_runs_does_not_leave_orphan_score(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "deleted.db"))
    try:
        storage.replace_agent_run_bundle(_agent_turn_bundle())
        config = {**_config(), "unit": "agent_turn"}
        preview = preview_evaluation(storage, tenant_id="local", config=config)

        class DeletingProvider(CountingProvider):
            def complete(self, request):
                storage._conn.execute("DELETE FROM agent_runs WHERE tenant_id=? AND run_id=?",
                                      ("local", "run-1"))
                return super().complete(request)

        result = execute_evaluation(storage, tenant_id="local", provider=DeletingProvider(),
                                    config={**config, "planFingerprint": preview["planFingerprint"],
                                            "plannedTurns": preview["plannedTurns"]},
                                    confirm_external_egress=True)
        assert result["stale"] == 1 and result["completed"] == 0
        assert storage._conn.execute("SELECT count(*) FROM agent_turn_judgments").fetchone()[0] == 0
    finally:
        storage.close()


@pytest.mark.parametrize("failure", ["storage", "cancel"])
def test_provider_success_then_storage_failure_or_cancellation_cannot_claim_completion(failure):
    class FailingStorage(InMemoryStorage):
        def save_agent_turn_judgment_if_current(self, judgment):
            raise OSError("durable storage unavailable")

    storage = FailingStorage() if failure == "storage" else InMemoryStorage()
    storage.replace_agent_run_bundle(_agent_turn_bundle())
    config = {**_config(), "unit": "agent_turn"}
    preview = preview_evaluation(storage, tenant_id="local", config=config)
    provider = CountingProvider()
    if failure == "cancel":
        def cancel(_request):
            raise KeyboardInterrupt()
        provider.complete = cancel
    with pytest.raises(OSError if failure == "storage" else KeyboardInterrupt):
        execute_evaluation(storage, tenant_id="local", provider=provider,
                           config={**config, "planFingerprint": preview["planFingerprint"],
                                   "plannedTurns": preview["plannedTurns"]},
                           confirm_external_egress=True)
    assert preview_evaluation(storage, tenant_id="local", config=config)["plannedCalls"] == 1


def test_memory_close_clears_agent_evidence_and_native_results():
    storage = InMemoryStorage()
    bundle = _agent_turn_bundle()
    storage.replace_agent_run_bundle(bundle)
    storage.save_agent_turn_judgment_if_current(AgentTurnJudgment(
        tenant_id="local", run_id="run-1", turn_id="turn-1",
        evaluator_fingerprint="a" * 64,
        evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
        evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
        expected_dimensions=[], rubric_name="test", rubric_version="1",
        status=JudgmentStatus.ERROR, error="test failure",
    ))
    storage.close()
    assert storage.list_agent_turn_evaluation_candidates("local", "a" * 64)[0] == []
    assert storage.get_agent_run_bundle("local", "run-1") is None


def test_turn_judge_reasoning_redacted_before_disk_and_dashboard(tmp_path):
    from verdict.dashboard.app import build_agent_run_detail

    path = tmp_path / "privacy.db"
    storage = SQLiteStorage(str(path))
    bundle = _agent_turn_bundle()
    storage.replace_agent_run_bundle(bundle)
    result = AgentTurnJudgment(
        tenant_id="local", run_id="run-1", turn_id="turn-1",
        evaluator_fingerprint="a" * 64,
        evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
        evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
        expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
        dimensions=[DimensionScore("relevance", Verdict.PASS,
                                   "Evidence from alice@example.com", "test")],
    )
    assert storage.save_agent_turn_judgment_if_current(result) == "saved"
    raw = storage._conn.execute("SELECT result_json FROM agent_turn_judgments").fetchone()[0]
    assert "alice@example.com" not in raw and "<EMAIL>" in raw
    storage.close()
    visible = build_agent_run_detail(path, tenant="local", run_id="run-1")
    assert "alice@example.com" not in str(visible["turns"][0]["evaluation"])
    assert "Evidence from" not in str(visible["turns"][0]["evaluation"])


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_mutated_judge_result_is_resanitized_at_storage_boundary(tmp_path, backend):
    storage = InMemoryStorage() if backend == "memory" else SQLiteStorage(str(tmp_path / "mutated.db"))
    try:
        bundle = _agent_turn_bundle()
        storage.replace_agent_run_bundle(bundle)
        result = AgentTurnJudgment(
            tenant_id="local", run_id="run-1", turn_id="turn-1",
            evaluator_fingerprint="a" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
            evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
            expected_dimensions=["relevance"], rubric_name="test", rubric_version="1",
            dimensions=[DimensionScore("relevance", Verdict.PASS, "safe", "test")],
        )
        result.dimensions[0].reasoning = "alice@example.com"
        assert storage.save_agent_turn_judgment_if_current(result) == "saved"
        rows, _ = storage.list_agent_turn_evaluation_candidates("local", "a" * 64)
        assert rows[0][1] is JudgmentStatus.COMPLETED
        assert _stored_turn_result(storage, "a" * 64).dimensions[0].reasoning == "<EMAIL>"
        assert result.dimensions[0].reasoning == "alice@example.com"
    finally:
        storage.close()


def test_repeated_failed_turn_judge_calls_are_reported_as_errors():
    storage = InMemoryStorage()
    storage.replace_agent_run_bundle(_agent_turn_bundle())
    config = {**_config(), "unit": "agent_turn"}

    class FailingProvider(CountingProvider):
        def complete(self, request):
            self.calls += 1
            raise RuntimeError("provider failed")

    provider = FailingProvider()
    for expected_calls in (1, 2):
        preview = preview_evaluation(storage, tenant_id="local", config=config)
        result = execute_evaluation(storage, tenant_id="local", provider=provider,
                                    config={**config, "planFingerprint": preview["planFingerprint"],
                                            "plannedTurns": preview["plannedTurns"]},
                                    confirm_external_egress=True)
        assert provider.calls == expected_calls
        assert result["plannedCalls"] == 1
        assert result["errors"] == 1
        assert result["completed"] == 0
        assert result["stale"] == 0


def test_low_level_turn_result_rejects_sensitive_dimension_names_and_redacts_identity(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "identity-privacy.db"))
    try:
        bundle = _agent_turn_bundle()
        storage.replace_agent_run_bundle(bundle)
        values = dict(
            tenant_id="local", run_id="run-1", turn_id="turn-1",
            evaluator_fingerprint="a" * 64,
            evidence_fingerprint=turn_evidence_fingerprint(bundle.turns[0]),
            evaluator_provider="anthropic", evaluator_config={"memo": "alice@example.com"},
            judge_models=["judge-alice@example.com"], expected_dimensions=["quality"],
            rubric_name="test", rubric_version="1",
        )
        with pytest.raises(ValueError, match="expected dimensions"):
            AgentTurnJudgment(**{**values, "expected_dimensions": ["alice@example.com"]})
        with pytest.raises(ValueError, match="scored dimensions"):
            AgentTurnJudgment(**values, dimensions=[
                DimensionScore("alice@example.com", Verdict.PASS, "ok", "test"),
            ])
        assert storage.save_agent_turn_judgment_if_current(AgentTurnJudgment(
            **values, dimensions=[DimensionScore("quality", Verdict.PASS, "ok", "test")],
        )) == "saved"
        raw = storage._conn.execute("SELECT result_json FROM agent_turn_judgments").fetchone()[0]
        assert "alice@example.com" not in raw
        assert "<EMAIL>" in raw
    finally:
        storage.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("corruption", [
    "not_object", "invalid_model_list", "missing_score", "duplicate_score",
])
def test_corrupt_current_slot_is_retryable_and_repaired_after_explicit_approval(
    tmp_path, backend, corruption,
):
    storage = InMemoryStorage() if backend == "memory" else SQLiteStorage(str(tmp_path / "repair.db"))
    try:
        storage.replace_agent_run_bundle(_agent_turn_bundle())
        config = {**_config(), "unit": "agent_turn"}
        provider = CountingProvider()
        preview = preview_evaluation(storage, tenant_id="local", config=config)
        first = execute_evaluation(storage, tenant_id="local", provider=provider,
                                   config={**config, "planFingerprint": preview["planFingerprint"],
                                           "plannedTurns": preview["plannedTurns"]},
                                   confirm_external_egress=True)
        fingerprint = first["evaluatorFingerprint"]
        raw = _stored_turn_result(storage, fingerprint)
        payload = json.loads(agent_turn_judgment_to_json(raw))
        if corruption == "not_object":
            corrupt = "[]"
        else:
            if corruption == "invalid_model_list":
                payload["judge_models"] = "not a list"
            elif corruption == "missing_score":
                payload["dimensions"] = payload["dimensions"][:1]
            else:
                payload["dimensions"].append(payload["dimensions"][0])
            corrupt = json.dumps(payload)
        if isinstance(storage, SQLiteStorage):
            storage._conn.execute("UPDATE agent_turn_judgments SET result_json=?", (corrupt,))
        else:
            storage._agent_turn_judgments[("local", "run-1", "turn-1", fingerprint)] = corrupt
        retry = preview_evaluation(storage, tenant_id="local", config=config)
        assert retry["alreadyJudged"] == 0
        assert retry["plannedCalls"] == 1
        repaired = execute_evaluation(storage, tenant_id="local", provider=provider,
                                      config={**config, "planFingerprint": retry["planFingerprint"],
                                              "plannedTurns": retry["plannedTurns"]},
                                      confirm_external_egress=True)
        assert provider.calls == 2
        assert repaired["completed"] == 1
        assert preview_evaluation(storage, tenant_id="local", config=config)["alreadyJudged"] == 1
    finally:
        storage.close()


def test_completed_turn_result_requires_one_score_per_expected_dimension():
    values = dict(
        tenant_id="local", run_id="run-1", turn_id="turn-1",
        evaluator_fingerprint="a" * 64, evidence_fingerprint="b" * 64,
        evaluator_provider="anthropic", evaluator_config={}, judge_models=["test"],
        expected_dimensions=["quality"], rubric_name="test", rubric_version="1",
    )
    for dimensions in ([], [
        DimensionScore("quality", Verdict.PASS),
        DimensionScore("quality", Verdict.PASS),
    ]):
        with pytest.raises(ValueError, match="one score per expected dimension"):
            AgentTurnJudgment(**values, dimensions=dimensions)
    assert AgentTurnJudgment(**values, status=JudgmentStatus.ERROR).dimensions == []


def _config():
    return {
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "maxCalls": 10,
        "maxOutputTokens": 256,
        "rubric": {
            "name": "poc",
            "version": "1",
            "dimensions": [
                {"name": "relevance", "description": "Directly answers the question."},
                {"name": "completeness", "description": "Covers requested elements."},
            ],
        },
    }


def _mixed_context_config():
    return {
        **_config(),
        "rubric": {
            "name": "poc",
            "version": "1",
            "dimensions": [
                {
                    "name": "groundedness",
                    "description": "Every claim is supported by retrieved context.",
                    "requiresContext": True,
                },
                {"name": "relevance", "description": "Directly answers the question."},
            ],
        },
    }


def _approved_config(storage, config):
    preview = preview_evaluation(storage, tenant_id="local", config=config)
    return {
        **config,
        "planFingerprint": preview["planFingerprint"],
        "plannedTraces": preview["plannedTraces"],
    }


def test_preview_separates_evidence_eligibility_before_any_judge_call():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    storage.insert_trace(_trace("missing-response", response=None))

    preview = preview_evaluation(storage, tenant_id="local", config=_config())

    assert preview["eligible"] == 1
    assert preview["notEvaluable"] == 1
    assert preview["plannedCalls"] == 1
    assert preview["notEvaluableReasons"] == {"response_not_captured": 1}
    assert preview["estimatedMaximumCostUsd"] is not None
    assert preview["externalEgressRequired"] is True


def test_anthropic_preview_runs_with_the_installed_sdk_without_network_egress(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    # Anthropic 1.x uses httpx2; earlier SDKs use httpx. The transport and
    # response must come from the same HTTP package as the installed SDK.
    sdk_httpx = getattr(anthropic._base_client, "httpx2", None) or anthropic._base_client.httpx
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return sdk_httpx.Response(200, json={
            "id": "msg_local_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": (
                '{"relevance":{"reasoning":"direct","verdict":"PASS"},'
                '"completeness":{"reasoning":"complete","verdict":"PASS"}}'
            )}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 8},
        })

    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    config = _config()
    with monkeypatch.context() as no_egress:
        def reject_provider_call(self, request):
            raise AssertionError("preflight attempted a provider call")

        no_egress.setattr(AnthropicAdapter, "complete", reject_provider_call)
        preview = preview_evaluation(storage, tenant_id="local", config=config)
    assert requests == []

    with sdk_httpx.Client(transport=sdk_httpx.MockTransport(respond)) as client:
        provider = AnthropicAdapter(api_key="local-test", max_retries=1)
        provider._client.close()
        provider._client = anthropic.Anthropic(api_key="local-test", http_client=client)
        result = execute_evaluation(
            storage,
            tenant_id="local",
            config={
                **config,
                "planFingerprint": preview["planFingerprint"],
                "plannedTraces": preview["plannedTraces"],
            },
            provider=provider,
            confirm_external_egress=True,
        )

    assert result["completed"] == 1, storage.list_judgments_for_cluster("all", limit=10)
    assert result["errors"] == 0
    assert len(requests) == 1
    assert ("temperature" in requests[0]) is provider.supports_temperature
    assert requests[0].get("temperature") in (None, 0.0)
    [judgment] = storage.list_judgments_for_cluster("all", limit=10)
    assert judgment.evaluator_fingerprint == result["evaluatorFingerprint"]


def test_anthropic_preview_matches_an_adapter_without_temperature(monkeypatch):
    class OlderAdapter:
        supports_temperature = False

        def __init__(self, api_key):
            self._client = self

        def close(self):
            pass

    monkeypatch.setattr("verdict_eval.providers.AnthropicAdapter", OlderAdapter)
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    config = _approved_config(storage, _config())
    provider = CountingProvider()
    provider.supports_temperature = False

    result = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert result["completed"] == 1
    assert result["errors"] == 0


def test_anthropic_preview_remains_available_without_optional_sdk(monkeypatch):
    original_find_spec = importlib.util.find_spec
    original_import = builtins.__import__

    def import_without_anthropic(name, *args, **kwargs):
        if name == "anthropic" or name.startswith("anthropic."):
            raise ImportError("anthropic SDK unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "anthropic" else original_find_spec(name),
    )
    monkeypatch.setattr(builtins, "__import__", import_without_anthropic)
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))

    preview = preview_evaluation(storage, tenant_id="local", config=_config())

    assert preview["plannedCalls"] == 1
    assert preview["eligible"] == 1


def test_execute_judges_only_eligible_traces_and_persists_identity():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    storage.insert_trace(_trace("missing-prompt", prompt=None))
    provider = CountingProvider()
    config = _approved_config(storage, _config())

    result = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert provider.calls == 1
    assert result["completed"] == 1
    assert result["notEvaluable"] == 1
    assert len(result["evaluatorId"]) == 20
    [judgment] = storage.list_judgments_for_cluster("all", limit=10)
    assert judgment.trace_id == "eligible"
    assert judgment.evaluator_identity_complete is True
    assert [score.name for score in judgment.dimensions] == ["relevance", "completeness"]

    repeated = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )
    assert provider.calls == 1
    assert repeated["plannedCalls"] == 0
    assert repeated["alreadyJudged"] == 1

    repeated_preview = preview_evaluation(
        storage, tenant_id="local", config=_config()
    )
    assert repeated_preview["plannedCalls"] == 0
    assert repeated_preview["alreadyJudged"] == 1


def test_preview_execution_prompt_and_storage_share_the_effective_rubric():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    config = _mixed_context_config()

    preview = preview_evaluation(storage, tenant_id="local", config=config)
    context_free_preview = preview_evaluation(
        storage,
        tenant_id="local",
        config={
            **config,
            "rubric": {
                **config["rubric"],
                "dimensions": [config["rubric"]["dimensions"][1]],
            },
        },
    )
    provider = CountingProvider()
    result = execute_evaluation(
        storage,
        tenant_id="local",
        config={
            **config,
            "planFingerprint": preview["planFingerprint"],
            "plannedTraces": preview["plannedTraces"],
        },
        provider=provider,
        confirm_external_egress=True,
    )

    assert preview["rubric"] == {
        "name": "poc",
        "version": "1",
        "dimensions": ["relevance"],
        "skippedDimensions": ["groundedness"],
    }
    assert result["rubric"] == preview["rubric"]
    assert preview["estimatedInputTokens"] == context_free_preview["estimatedInputTokens"]
    assert "groundedness" not in provider.requests[0].messages[-1]["content"]
    assert "relevance" in provider.requests[0].messages[-1]["content"]
    [judgment] = storage.list_judgments_for_cluster("all", limit=10)
    assert judgment.expected_dimensions == ["relevance"]
    assert [dimension.name for dimension in judgment.dimensions] == ["relevance"]


def test_preview_rejects_a_rubric_with_no_dimension_evaluable_without_context():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    config = _mixed_context_config()
    config["rubric"]["dimensions"] = [config["rubric"]["dimensions"][0]]

    with pytest.raises(ValueError, match="no rubric dimensions are evaluable"):
        preview_evaluation(storage, tenant_id="local", config=config)


def test_matching_evaluation_is_not_repeated_when_newer_evaluators_exceed_page_limit():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    provider = CountingProvider()
    config = _approved_config(storage, _config())

    execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )
    for index in range(101):
        storage.insert_judgment(Judgment(
            judgment_id=f"newer-{index:03d}",
            trace_id="eligible",
            created_at=datetime(2030, 1, 1, 0, 0, index % 60, tzinfo=timezone.utc),
            evaluator_fingerprint=f"other-{index}",
        ))

    preview = preview_evaluation(storage, tenant_id="local", config=config)
    repeated = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert preview["plannedCalls"] == 0
    assert preview["alreadyJudged"] == 1
    assert repeated["plannedCalls"] == 0
    assert provider.calls == 1


def test_concurrent_equivalent_execution_makes_one_paid_call():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(CountingProvider):
        def complete(self, request):
            started.set()
            assert release.wait(timeout=5)
            return super().complete(request)

    provider = BlockingProvider()
    config = _approved_config(storage, _config())

    def run():
        return execute_evaluation(
            storage,
            tenant_id="local",
            config=config,
            provider=provider,
            confirm_external_egress=True,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run)
        assert started.wait(timeout=5)
        second = executor.submit(run)
        release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert provider.calls == 1
    assert sorted(result["completed"] for result in results) == [0, 1]
    assert sorted(result["alreadyJudged"] for result in results) == [0, 1]


def test_execute_requires_explicit_external_egress_confirmation():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))

    try:
        execute_evaluation(
            storage,
            tenant_id="local",
            config=_config(),
            provider=CountingProvider(),
            confirm_external_egress=False,
        )
    except ValueError as error:
        assert str(error) == "external judge egress was not confirmed"
    else:
        raise AssertionError("expected explicit egress confirmation")


def test_all_selection_previews_and_judges_every_eligible_trace_beyond_old_default():
    storage = InMemoryStorage()
    for index in range(12):
        storage.insert_trace(_trace(f"eligible-{index}"))
    storage.insert_trace(_trace("missing-response", response=None))
    config = {**_config(), "maxCalls": "all"}

    preview = preview_evaluation(storage, tenant_id="local", config=config)
    config = {
        **config,
        "planFingerprint": preview["planFingerprint"],
        "plannedTraces": preview["plannedTraces"],
    }

    assert preview["availableTraces"] == 13
    assert preview["eligible"] == 12
    assert preview["notEvaluable"] == 1
    assert preview["plannedCalls"] == 12
    assert preview["maximumCalls"] == "all"

    provider = CountingProvider()
    result = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert provider.calls == 12
    assert result["availableTraces"] == 13
    assert result["eligible"] == 12
    assert result["plannedCalls"] == 12
    assert result["completed"] == 12


def test_execution_is_pinned_to_the_approved_preview_plan():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("approved"))
    provider = CountingProvider()
    config = _approved_config(storage, {**_config(), "maxCalls": "all"})
    storage.insert_trace(_trace("arrived-after-preview-a"))
    storage.insert_trace(_trace("arrived-after-preview-b"))

    result = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert result["eligible"] == 3
    assert result["plannedCalls"] == 1
    assert result["completed"] == 1
    assert provider.calls == 1

    try:
        execute_evaluation(
            storage,
            tenant_id="local",
            config={**config, "maxCalls": 2},
            provider=provider,
            confirm_external_egress=True,
        )
    except ValueError as error:
        assert str(error) == "evaluator plan does not match the approved preview"
    else:
        raise AssertionError("expected changed evaluator plan rejection")

    changed = InMemoryStorage()
    changed.insert_trace(_trace("changed"))
    changed_config = _approved_config(changed, _config())
    changed.insert_trace(_trace("changed", response="different response"))
    try:
        execute_evaluation(
            changed,
            tenant_id="local",
            config=changed_config,
            provider=provider,
            confirm_external_egress=True,
        )
    except ValueError as error:
        assert str(error) == "evaluator preview plan is no longer current"
    else:
        raise AssertionError("expected changed trace evidence rejection")


def test_numeric_selection_cap_remains_available_and_invalid_modes_fail_closed():
    storage = InMemoryStorage()
    for index in range(12):
        storage.insert_trace(_trace(f"eligible-{index}"))

    preview = preview_evaluation(storage, tenant_id="local", config=_config())
    assert preview["plannedCalls"] == 10
    assert preview["maximumCalls"] == 10

    for invalid in ("everything", 0, 10_001, True):
        try:
            preview_evaluation(
                storage,
                tenant_id="local",
                config={**_config(), "maxCalls": invalid},
            )
        except ValueError as error:
            assert str(error) == "invalid evaluator budget"
        else:
            raise AssertionError(f"expected invalid maxCalls rejection: {invalid!r}")


def test_numeric_cap_reports_all_existing_evaluation_coverage():
    storage = InMemoryStorage()
    provider = CountingProvider()
    for index in range(12):
        storage.insert_trace(_trace(f"judged-{index}"))
    all_config = _approved_config(storage, {**_config(), "maxCalls": "all"})
    execute_evaluation(
        storage,
        tenant_id="local",
        config=all_config,
        provider=provider,
        confirm_external_egress=True,
    )
    storage.insert_trace(_trace("new-a"))
    storage.insert_trace(_trace("new-b"))
    config = {**_config(), "maxCalls": 2}

    preview = preview_evaluation(storage, tenant_id="local", config=config)
    config = {
        **config,
        "planFingerprint": preview["planFingerprint"],
        "plannedTraces": preview["plannedTraces"],
    }
    result = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert preview["eligible"] == 14
    assert preview["plannedCalls"] == 2
    assert preview["alreadyJudged"] == 12
    assert result["completed"] == 2
    assert result["alreadyJudged"] == 12
    assert provider.calls == 14


def test_customer_label_set_can_be_previewed_and_calibrated(tmp_path):
    label_set = tmp_path / "labels.jsonl"
    label_set.write_text(
        '{"set_name":"customer-v1"}\n'
        '{"sentinel_id":"one","query":"q","response":"a",'
        '"labels":{"relevance":"pass","completeness":"pass"}}\n'
    )
    preview = preview_calibration(path=label_set, config=_config())
    provider = CountingProvider()
    storage = InMemoryStorage()

    result = execute_calibration(
        storage,
        path=label_set,
        config=_config(),
        provider=provider,
        confirm_external_egress=True,
        minimum_examples=1,
        agreement_threshold=0.5,
    )

    assert preview["setName"] == "customer-v1"
    assert preview["examples"] == 1
    assert preview["labelCounts"] == {"completeness": 1, "relevance": 1}
    assert provider.calls == 1
    assert result["status"] == "degraded"  # Wilson lower bound is below .5 at n=1
    [health] = storage.list_evaluator_health(limit=10)
    assert health.sentinel_set_name == "customer-v1"
