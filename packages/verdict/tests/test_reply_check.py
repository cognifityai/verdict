"""verdict-reply-check: judge replies by the next message under one spend cap."""

from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from verdict import reply_check
from verdict.agent_judgment import AgentTurnJudgment, turn_evidence_fingerprint
from verdict.dashboard import evaluator_lab
from verdict.dashboard.evaluator_lab import _IdentityOnlyProvider
from verdict.evidence import (
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    SourceSession,
)
from verdict.monitor_inputs import LOCAL_TENANT
from verdict.pricing import compute_cost_usd
from verdict.schema import DimensionScore, JudgmentStatus, Verdict
from verdict.storage import InMemoryStorage
from verdict.storage.sqlite import SQLiteStorage
from verdict_eval.providers import CompletionResponse

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)
MODEL = "gpt-5.4-mini"


class Judge:
    """Fails the correction question only when the next message corrects."""

    name = "openai"

    def __init__(self):
        identity = _IdentityOnlyProvider("openai")
        self.supports_temperature = identity.supports_temperature
        self.temperature_supported = identity.temperature_supported
        self.calls = 0
        self.output_tokens = 200

    def complete(self, request):
        self.calls += 1
        prompt = "\n".join(message["content"] for message in request.messages)
        next_message = prompt[prompt.find("USER'S NEXT MESSAGE"):prompt.find("RUBRIC DIMENSIONS")]
        corrected = "broke" in next_message
        verdicts = {name: "PASS" for name in reply_check.FAIL_MEANS}
        if corrected:
            verdicts["no_correction_needed"] = "FAIL"
        return CompletionResponse(
            text=json.dumps({name: {"reasoning": "next message", "verdict": verdict}
                             for name, verdict in verdicts.items()}),
            input_tokens=1_000, output_tokens=self.output_tokens,
        )


def _bundle(run_id, requests, *, minutes, tenant=LOCAL_TENANT, **run_fields):
    started = NOW + timedelta(minutes=minutes)
    return AgentRunBundle(
        session=SourceSession(
            source_session_id=f"source-{run_id}", tenant_id=tenant, source_kind="pydantic-ai",
            source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest(),
            started_at=started, observed_at=started,
        ),
        run=AgentRun(
            run_id=run_id, source_session_id=f"source-{run_id}", tenant_id=tenant,
            started_at=started, ended_at=started, status=ExecutionStatus.COMPLETED,
            **run_fields,
        ),
        turns=tuple(AgentTurn(
            turn_id=f"{run_id}-{index}", run_id=run_id, sequence=index,
            started_at=started + timedelta(seconds=index),
            ended_at=started + timedelta(seconds=index),
            status=ExecutionStatus.COMPLETED, user_request_redacted=request,
            final_response_redacted=f"Reply {index}: done.",
            request_state=EvidenceState.PRESENT, response_state=EvidenceState.PRESENT,
        ) for index, request in enumerate(requests)),
    )


def _store(path):
    storage = SQLiteStorage(str(path))
    # A request-per-run application: each user message opens a new run.
    storage.replace_agent_run_bundle(_bundle("r1", ["Add a retry."], minutes=0, session_id="s"))
    storage.replace_agent_run_bundle(_bundle(
        "c1", ["Coordinator: research retries."], minutes=1, session_id="s",
        parent_run_id="r1"))
    storage.replace_agent_run_bundle(_bundle(
        "r2", ["That broke uploads, revert it."], minutes=2, session_id="s"))
    storage.replace_agent_run_bundle(_bundle(
        "r3", ["Thanks.", "Now add a test."], minutes=3, session_id="s"))
    return storage


@pytest.fixture
def judge(monkeypatch):
    provider = Judge()
    monkeypatch.setattr(evaluator_lab, "_provider", lambda name: provider)
    return provider


def _run(path, *args, answer=""):
    out = io.StringIO()
    code = reply_check.main(["--storage", str(path), *args], stdin=io.StringIO(answer), out=out)
    return code, out.getvalue()


def test_judges_every_reply_with_a_next_message_and_reports_rates(tmp_path, judge):
    _store(tmp_path / "v.db").close()

    code, text = _run(tmp_path / "v.db", answer="y\n")

    assert code == 0
    assert "3 replies to judge, 0 already judged." in text
    assert "skipped 1: sub-agent reply" in text
    assert "skipped 1: no next message" in text
    assert "Each request, reply and next message (already redacted) is sent to" in text
    # r1 -> r2 crosses runs; r2 -> r3 crosses runs; r3's first Turn -> its second.
    assert judge.calls == 3
    assert "33.3%  user had to correct the agent  (1 of 3;" in text

    code, text = _run(tmp_path / "v.db", "--json", "--yes")
    result = json.loads(text)
    assert code == 0 and judge.calls == 3
    assert result["judged"] == 3
    assert result["rates"]["no_correction_needed"]["fail"] == 1
    assert result["rates"]["understood_reply"]["fail"] == 0


def test_declining_sends_nothing(tmp_path, judge):
    _store(tmp_path / "v.db").close()

    code, text = _run(tmp_path / "v.db", answer="n\n")

    assert code == 1 and judge.calls == 0
    assert "Nothing sent." in text


def test_measured_spend_never_passes_the_budget_and_a_rerun_resumes(tmp_path, judge):
    storage = SQLiteStorage(str(tmp_path / "v.db"))
    storage.replace_agent_run_bundle(_bundle(
        "long", [f"Request {index}" for index in range(30)], minutes=0, session_id="s"))
    # Expensive calls, so the measured cap binds after the statically bounded first batch.
    judge.output_tokens = 60_000
    call = compute_cost_usd(MODEL, 1_000, judge.output_tokens)
    budget = call * 12.5
    try:
        first = reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai",
                                  model=MODEL, budget_usd=budget)
        assert first["stoppedForBudget"]
        assert first["spentUsd"] <= budget
        assert 1 <= first["completed"] < 29
        assert first["completed"] == judge.calls

        second = reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai",
                                   model=MODEL, budget_usd=1_000)
        assert not second["stoppedForBudget"]
        assert first["completed"] + second["completed"] == 29
        assert judge.calls == 29
        assert reply_check.summarize(
            storage, tenant_id=LOCAL_TENANT,
            fingerprint=reply_check.evaluator_fingerprint("openai", MODEL),
        )["judged"] == 29
    finally:
        storage.close()


def test_budget_below_one_call_sends_nothing(tmp_path, judge):
    storage = _store(tmp_path / "v.db")
    try:
        result = reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai", model=MODEL,
                                   budget_usd=1e-9)
        assert result["stoppedForBudget"] and judge.calls == 0
    finally:
        storage.close()


def test_unpriced_model_is_refused_before_any_call(tmp_path, judge):
    storage = _store(tmp_path / "v.db")
    try:
        with pytest.raises(ValueError, match="spend cap cannot be enforced"):
            reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai",
                              model="unpriced-model", budget_usd=5)
        assert judge.calls == 0
    finally:
        storage.close()


def test_summary_drops_results_whose_evidence_changed(tmp_path, judge):
    storage = _store(tmp_path / "v.db")
    try:
        reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai", model=MODEL,
                          budget_usd=5)
        fingerprint = reply_check.evaluator_fingerprint("openai", MODEL)
        assert reply_check.summarize(storage, tenant_id=LOCAL_TENANT,
                                     fingerprint=fingerprint)["judged"] == 3
        original = storage.get_agent_run_bundle(LOCAL_TENANT, "r1")
        storage.replace_agent_run_bundle(replace(original, turns=(replace(
            original.turns[0], final_response_redacted="Reply 0: done. Later extension."),)))

        summary = reply_check.summarize(storage, tenant_id=LOCAL_TENANT, fingerprint=fingerprint)
        assert summary["judged"] == 2
    finally:
        storage.close()


def _judgment(turn, tenant, fingerprint):
    return AgentTurnJudgment(
        tenant_id=tenant, run_id=turn.run_id, turn_id=turn.turn_id,
        evaluator_fingerprint=fingerprint, evidence_fingerprint=turn_evidence_fingerprint(turn),
        evaluator_provider="openai", evaluator_config={},
        judge_models=["m"], expected_dimensions=["no_correction_needed"],
        rubric_name="reply_check", rubric_version="1",
        dimensions=[DimensionScore(name="no_correction_needed", verdict=Verdict.PASS,
                                   reasoning="ok", judge_model="m")],
        status=JudgmentStatus.COMPLETED,
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite", "postgres"])
def test_turn_judgments_page_by_run_and_turn(tmp_path, backend):
    if backend == "memory":
        storage, tenant = InMemoryStorage(), "local"
    elif backend == "sqlite":
        storage, tenant = SQLiteStorage(str(tmp_path / "p.db")), "local"
    else:
        dsn = os.environ.get("VERDICT_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("disposable Postgres required")
        from verdict.storage.postgres import PostgresStorage

        storage, tenant = PostgresStorage(dsn), f"reply-check-{uuid4().hex}"
    wanted, other = "a" * 64, "b" * 64
    try:
        bundles = {
            (tenant, "r1"): _bundle("r1", ["a", "b", "c"], minutes=0, tenant=tenant),
            (tenant, "r2"): _bundle("r2", ["a"], minutes=1, tenant=tenant),
            (f"{tenant}-other", "r1"): _bundle("r1", ["a"], minutes=0, tenant=f"{tenant}-other"),
        }
        for bundle in bundles.values():
            storage.replace_agent_run_bundle(bundle)
        for scope, run_id, index, fingerprint in (
            (tenant, "r1", 1, wanted), (tenant, "r1", 0, wanted), (tenant, "r2", 0, wanted),
            (tenant, "r1", 2, other), (f"{tenant}-other", "r1", 0, wanted),
        ):
            turn = bundles[(scope, run_id)].turns[index]
            assert storage.save_agent_turn_judgment_if_current(
                _judgment(turn, scope, fingerprint)) == "saved"

        first, after = storage.list_agent_turn_judgments(tenant, wanted, limit=2)
        assert [(item.run_id, item.turn_id) for item in first] == [("r1", "r1-0"), ("r1", "r1-1")]
        assert after == ("r1", "r1-1")
        rest, after = storage.list_agent_turn_judgments(tenant, wanted, limit=2, after=after)
        assert [(item.run_id, item.turn_id) for item in rest] == [("r2", "r2-0")]
        assert after is None
        assert storage.list_agent_turn_judgments(tenant, wanted, after=("r2", "r2-0")) == ([], None)
        with pytest.raises(ValueError):
            storage.list_agent_turn_judgments(tenant, wanted, limit=1_001)
        with pytest.raises(ValueError):
            storage.list_agent_turn_judgments(tenant, wanted, after=("r1",))
    finally:
        storage.close()


class SizedJudge(Judge):
    """Reports usage proportional to prompt size, like a real provider."""

    def complete(self, request):
        response = super().complete(request)
        prompt = "\n".join(message["content"] for message in request.messages)
        return CompletionResponse(text=response.text, input_tokens=len(prompt) // 4,
                                  output_tokens=200)


def test_cheap_newest_replies_cannot_hide_expensive_older_ones(tmp_path, monkeypatch):
    provider = SizedJudge()
    monkeypatch.setattr(evaluator_lab, "_provider", lambda name: provider)
    storage = SQLiteStorage(str(tmp_path / "v.db"))
    try:
        storage.replace_agent_run_bundle(_bundle(
            "old", ["x " * 20_000 + str(index) for index in range(25)], minutes=0))
        storage.replace_agent_run_bundle(_bundle(
            "new", [f"short {index}" for index in range(7)], minutes=10))

        run = reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai",
                                model=MODEL, budget_usd=0.10)

        assert run["stopped"] == "budget"
        assert 0 < run["spentUsd"] <= 0.10
    finally:
        storage.close()


def test_unreported_usage_stops_the_run(tmp_path, monkeypatch):
    class Unmetered(Judge):
        def complete(self, request):
            response = super().complete(request)
            return CompletionResponse(text=response.text)

    provider = Unmetered()
    monkeypatch.setattr(evaluator_lab, "_provider", lambda name: provider)
    storage = SQLiteStorage(str(tmp_path / "v.db"))
    try:
        storage.replace_agent_run_bundle(_bundle(
            "long", [f"Request {index}" for index in range(30)], minutes=0))
        run = reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai",
                                model=MODEL, budget_usd=100)
        assert run["stopped"] == "usage_unreported"
        assert provider.calls == 1
    finally:
        storage.close()


def test_a_session_that_grows_mid_run_is_previewed_again(tmp_path, monkeypatch):
    storage = SQLiteStorage(str(tmp_path / "v.db"))
    storage.replace_agent_run_bundle(_bundle("live", ["One.", "Two."], minutes=0))

    provider = Judge()
    monkeypatch.setattr(evaluator_lab, "_provider", lambda name: provider)
    original = evaluator_lab.execute_evaluation
    raced = []

    def execute(*args, **kwargs):
        if not raced:
            raced.append(True)
            storage.replace_agent_run_bundle(_bundle(
                "live", ["One.", "Two.", "Three."], minutes=0))
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluator_lab, "execute_evaluation", execute)
    try:
        run = reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai",
                                model=MODEL, budget_usd=5)
        assert run["completed"] == 2
    finally:
        storage.close()


def test_a_malformed_stored_result_does_not_end_the_summary_early(tmp_path, judge):
    storage = SQLiteStorage(str(tmp_path / "v.db"))
    try:
        storage.replace_agent_run_bundle(_bundle(
            "long", [f"Request {index}" for index in range(8)], minutes=0))
        reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai", model=MODEL,
                          budget_usd=5)
        fingerprint = reply_check.evaluator_fingerprint("openai", MODEL)
        with storage._lock:
            storage._conn.execute(
                "UPDATE agent_turn_judgments SET result_json='{' WHERE turn_id='long-0'")
            storage._conn.commit()

        page, after = storage.list_agent_turn_judgments(LOCAL_TENANT, fingerprint, limit=3)
        assert len(page) == 2 and after == ("long", "long-2")
        assert reply_check.summarize(
            storage, tenant_id=LOCAL_TENANT, fingerprint=fingerprint)["judged"] == 6
    finally:
        storage.close()


class _UsageStopsAfter(Judge):
    """Reports usage for the first ``metered`` calls, then fails the given way."""

    def __init__(self, metered, failure):
        super().__init__()
        self.metered = metered
        self.failure = failure

    def complete(self, request):
        response = super().complete(request)
        if self.calls <= self.metered:
            return response
        if self.failure == "timeout":
            raise TimeoutError("judge request timed out")
        return CompletionResponse(text=response.text)


@pytest.mark.parametrize(("failure", "stopped"), [
    ("no_usage", "usage_unreported"), ("timeout", "provider_error"),
])
def test_no_request_follows_a_call_whose_cost_is_unknown(tmp_path, monkeypatch,
                                                         failure, stopped):
    # Past the first batch, so the unknown cost lands inside a large batch.
    provider = _UsageStopsAfter(7, failure)
    monkeypatch.setattr(evaluator_lab, "_provider", lambda name: provider)
    storage = SQLiteStorage(str(tmp_path / "v.db"))
    try:
        storage.replace_agent_run_bundle(_bundle(
            "long", [f"Request {index}" for index in range(60)], minutes=0))
        run = reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai",
                                model=MODEL, budget_usd=100)
        assert run["stopped"] == stopped
        assert provider.calls == 8
        # Every attempt is reported, including the one that failed, and replies
        # that were never sent leave no error result behind.
        assert run["calls"] == 8
        assert run["errors"] == (1 if failure == "timeout" else 0)
    finally:
        storage.close()


def test_output_that_grows_mid_batch_stops_at_the_next_call(tmp_path, monkeypatch):
    class Growing(Judge):
        def complete(self, request):
            response = super().complete(request)
            tokens = 200 if self.calls <= 5 else 60_000
            return CompletionResponse(text=response.text, input_tokens=1_000,
                                      output_tokens=tokens)

    provider = Growing()
    monkeypatch.setattr(evaluator_lab, "_provider", lambda name: provider)
    storage = SQLiteStorage(str(tmp_path / "v.db"))
    large_call = compute_cost_usd(MODEL, 1_000, 60_000)
    budget = 5 * compute_cost_usd(MODEL, 1_000, 200) + 3.5 * large_call
    try:
        storage.replace_agent_run_bundle(_bundle(
            "long", [f"Request {index}" for index in range(60)], minutes=0))
        run = reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai",
                                model=MODEL, budget_usd=budget)
        assert run["stopped"] == "budget"
        # The cap is soft by at most the one call that first showed the larger size.
        assert run["spentUsd"] <= budget + large_call
    finally:
        storage.close()


def test_a_rejected_request_is_recorded_and_the_run_continues(tmp_path, monkeypatch):
    class Rejected(Exception):
        status_code = 400

    class RejectsOne(Judge):
        def complete(self, request):
            response = super().complete(request)
            if self.calls == 2:
                # The provider refused it unprocessed, so it is not billed.
                raise Rejected("invalid request")
            return response

    provider = RejectsOne()
    monkeypatch.setattr(evaluator_lab, "_provider", lambda name: provider)
    storage = SQLiteStorage(str(tmp_path / "v.db"))
    try:
        storage.replace_agent_run_bundle(_bundle(
            "long", [f"Request {index}" for index in range(12)], minutes=0))
        run = reply_check.judge(storage, tenant_id=LOCAL_TENANT, provider="openai",
                                model=MODEL, budget_usd=100)
        # Eleven replies have a next message; the rejected one is retried later.
        assert run["stopped"] is None
        assert (run["completed"], run["errors"], run["calls"]) == (11, 1, 12)
    finally:
        storage.close()
