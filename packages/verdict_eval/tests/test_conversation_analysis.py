from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from verdict.schema import Trace
from verdict.storage.memory import InMemoryStorage
from verdict.storage.sqlite import SQLiteStorage
from verdict_eval.cli.pipeline import main as pipeline_main
from verdict_eval.cluster_registry import ClusterRegistryService
from verdict_eval.clustering_strategies import FitConfig
from verdict_eval.conversation_analysis import (
    ConversationAnalysisConfig,
    _metadata_size,
    plan_conversation_analysis,
    run_conversation_analysis,
)
from verdict_eval.judge import Judge, Rubric, RubricDimension
from verdict_eval.providers import FakeProvider


@pytest.fixture(params=["memory", "sqlite"])
def conversation_storage(request, tmp_path):
    storage = (
        InMemoryStorage()
        if request.param == "memory"
        else SQLiteStorage(str(tmp_path / "conversation.db"))
    )
    yield storage
    storage.close()


def _config(run_id: str = "run-1") -> ConversationAnalysisConfig:
    return ConversationAnalysisConfig(
        tenant_id="tenant-a",
        run_id=run_id,
        target_workload="agent",
        baseline_start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        baseline_end=datetime(2026, 8, 2, tzinfo=timezone.utc),
        current_start=datetime(2026, 8, 3, tzinfo=timezone.utc),
        current_end=datetime(2026, 8, 4, tzinfo=timezone.utc),
        analysis_cutoff=datetime(2026, 8, 4, tzinfo=timezone.utc),
        actor="admin@example.test",
        target_per_cell=2,
        min_sample_size=2,
    )


def _activate_explicit_registry(
    storage, config: ConversationAnalysisConfig, *, count: int = 2, overlap: bool = False
) -> str:
    for window, start in (
        ("baseline", config.baseline_start),
        ("current", config.current_start),
    ):
        for index in range(count):
            storage.insert_trace(
                Trace(
                    trace_id=f"{window}-{index}",
                    tenant_id=config.tenant_id,
                    session_id=(
                        f"shared-session-{index}" if overlap else f"{window}-session-{index}"
                    ),
                    started_at=start + timedelta(minutes=index),
                    ended_at=start + timedelta(minutes=index, seconds=1),
                    prompt_redacted=f"prompt-{window}-{index}",
                    response_redacted=f"response-{window}-{index}",
                    raw_messages=[{"role": "user", "content": "billing"}],
                    tags={
                        "verdict.workload": "agent",
                        "verdict.intent_key": "billing",
                        "verdict.success_sampling": "full-v1",
                    },
                )
            )
    service = ClusterRegistryService(storage)
    version = service.fit(
        config.tenant_id,
        actor=config.actor,
        strategy="explicit",
        cutoff=config.analysis_cutoff,
        config=FitConfig(strategy="explicit", target_workload=config.target_workload),
    )
    service.assign(config.tenant_id, version.version_id, through_cutoff=config.analysis_cutoff)
    service.validate(config.tenant_id, version.version_id, actor=config.actor)
    service.activate(
        config.tenant_id,
        version.version_id,
        expected_generation=0,
        actor=config.actor,
    )
    return version.version_id


def test_plan_selects_one_bounded_representative_per_session_and_cell(
    conversation_storage,
) -> None:
    config = _config()
    version_id = _activate_explicit_registry(conversation_storage, config)

    plan = plan_conversation_analysis(conversation_storage, version_id, config)

    assert len(plan.source_rows) == 4
    assert len(plan.representatives.selected) == 4
    assert set(plan.contents) == {item.trace_id for item in plan.representatives.selected}
    assert plan.exclusions == {}


def test_metadata_cap_accepts_exact_size_and_rejects_one_byte_more(
    conversation_storage,
) -> None:
    config = _config("metadata-boundary")
    version_id = _activate_explicit_registry(conversation_storage, config)
    plan = plan_conversation_analysis(conversation_storage, version_id, config)

    exact = replace(config, max_candidate_metadata_bytes=plan.metadata_bytes)
    assert plan_conversation_analysis(conversation_storage, version_id, exact).metadata_bytes == (
        plan.metadata_bytes
    )

    longer = list(plan.source_rows)
    row = longer[0]
    longer[0] = replace(
        row,
        session_id=(row.session_id or "") + "x",
        session_utf8_bytes=(row.session_utf8_bytes or 0) + 1,
    )
    assert _metadata_size(longer, version_id, plan.metadata_bytes) == plan.metadata_bytes + 1
    with pytest.raises(ValueError, match="candidate_limit"):
        plan_conversation_analysis(
            conversation_storage,
            version_id,
            replace(config, max_candidate_metadata_bytes=plan.metadata_bytes - 1),
        )


def test_run_persists_completed_outcomes_and_ready_conversation_counts(
    conversation_storage,
) -> None:
    config = _config()
    version_id = _activate_explicit_registry(conversation_storage, config)
    rubric = Rubric("quality", "1", (RubricDimension("quality", "correct"),))
    payload = json.dumps({"quality": {"reasoning": "ok", "verdict": "PASS"}})
    judge = Judge(FakeProvider(payload), "fake-judge", rubric=rubric)

    result = run_conversation_analysis(
        conversation_storage,
        judge,
        config,
        assigner=lambda tenant, pinned, cutoff: (tenant, pinned, cutoff),
    )

    assert result.status == "ready"
    assert result.provider_calls == 4
    snapshot = conversation_storage.get_conversation_drift_snapshot(config.tenant_id, config.run_id)
    assert snapshot is not None
    run, samples, signals = snapshot
    assert run.registry_version == version_id
    assert run.sample_count == 4
    assert all(sample.outcomes_json == '{"quality":"PASS"}' for sample in samples)
    assert signals == []
    coverage = json.loads(run.coverage_json)
    assert coverage["cells"] == [[0, 0, 2, 2, 0, 2, 2, 2, 2, 2, 2, 2, 2, 0, 0, 0, 0, 3]]


def test_exact_run_replay_is_provider_free_and_changed_policy_conflicts(
    conversation_storage,
) -> None:
    config = _config("idempotent-run")
    _activate_explicit_registry(conversation_storage, config)
    rubric = Rubric("quality", "1", (RubricDimension("quality", "correct"),))
    payload = json.dumps({"quality": {"reasoning": "ok", "verdict": "PASS"}})
    calls = 0

    def response(_request):
        nonlocal calls
        calls += 1
        return payload

    judge = Judge(FakeProvider(response), "fake-judge", rubric=rubric)
    first = run_conversation_analysis(conversation_storage, judge, config)
    second = run_conversation_analysis(conversation_storage, judge, config)

    assert first.provider_calls == 4
    assert second.provider_calls == 0
    assert calls == 4
    with pytest.raises(ValueError, match="immutable conversation run conflict"):
        run_conversation_analysis(conversation_storage, judge, replace(config, seed=1))
    assert calls == 4


def test_candidate_cap_preflight_runs_before_assignment_or_provider(
    conversation_storage,
) -> None:
    config = replace(_config("candidate-preflight"), max_candidate_rows=1)
    _activate_explicit_registry(conversation_storage, config)
    rubric = Rubric("quality", "1", (RubricDimension("quality", "correct"),))

    def must_not_assign(*_args):
        raise AssertionError("assignment must not run after an over-cap preflight")

    result = run_conversation_analysis(
        conversation_storage,
        Judge(FakeProvider(lambda _request: pytest.fail("provider called")), "fake", rubric=rubric),
        config,
        assigner=must_not_assign,
        judge_factory=lambda: pytest.fail("judge constructor called"),
    )

    assert result.status == "unavailable"
    assert result.reason == "candidate_limit"
    assert result.provider_calls == 0


def test_control_character_dimension_is_rejected_before_provider_work(
    conversation_storage,
) -> None:
    config = _config("invalid-dimension")
    _activate_explicit_registry(conversation_storage, config)
    rubric = Rubric("quality", "1", (RubricDimension("quality\n", "invalid"),))

    with pytest.raises(ValueError, match="evaluator_definition"):
        run_conversation_analysis(
            conversation_storage,
            Judge(FakeProvider(lambda _request: pytest.fail("provider called")), "fake", rubric=rubric),
            config,
        )


def test_run_is_provider_free_when_assignment_coverage_is_missing(
    conversation_storage,
) -> None:
    config = _config()
    _activate_explicit_registry(conversation_storage, config)
    late = Trace(
        trace_id="late",
        tenant_id=config.tenant_id,
        session_id="late-session",
        started_at=config.current_start + timedelta(hours=1),
        ended_at=config.current_start + timedelta(hours=1, seconds=1),
        prompt_redacted="prompt",
        response_redacted="response",
        tags={
            "verdict.workload": "agent",
            "verdict.intent_key": "billing",
            "verdict.success_sampling": "full-v1",
        },
    )
    conversation_storage.insert_trace(late)
    judge = Judge(FakeProvider("{}"), "must-not-run")

    result = run_conversation_analysis(
        conversation_storage,
        judge,
        config,
        assigner=lambda tenant, pinned, cutoff: (tenant, pinned, cutoff),
        judge_factory=lambda: pytest.fail("judge constructor called"),
    )

    assert result.status == "unavailable"
    assert result.reason == "assignment_coverage"
    assert result.provider_calls == 0


def test_cli_runs_conversation_method_without_legacy_trace_readiness_gate(tmp_path, capsys) -> None:
    path = tmp_path / "cli.db"
    config = _config("cli-run")
    storage = SQLiteStorage(str(path))
    _activate_explicit_registry(storage, config)
    storage.close()

    result = pipeline_main(
        [
            "--storage",
            f"sqlite:///{path}",
            "--method",
            "conversation-v1",
            "--registry-mode",
            "active",
            "--tenant-id",
            config.tenant_id,
            "--run-id",
            config.run_id,
            "--as-of",
            config.analysis_cutoff.isoformat(),
            "--current-hours",
            "24",
            "--baseline-days",
            "1",
            "--baseline-lag-hours",
            "48",
            "--min-sample-size",
            "2",
            "--target-per-cluster",
            "2",
            "--max-candidate-rows",
            "5000",
            "--max-candidate-metadata-bytes",
            "8388608",
            "--max-judge-calls",
            "800",
            "--max-selected-content-bytes",
            "16777216",
            "--max-estimated-input-tokens",
            "2000000",
            "--judge-concurrency",
            "8",
            "--judge-attempt-timeout",
            "30",
        ]
    )

    assert result == 0
    assert "ready" in capsys.readouterr().out
    storage = SQLiteStorage(str(path))
    snapshot = storage.get_conversation_drift_snapshot(config.tenant_id, config.run_id)
    assert snapshot is not None
    policy = json.loads(snapshot[0].analysis_policy_json)
    assert policy["max_candidate_rows"] == 5000
    assert policy["max_judge_calls"] == 800
    assert policy["judge_concurrency"] == 8
    assert policy["judge_attempt_timeout"] == 30
    storage.close()


def test_cli_checks_registry_before_constructing_judge(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "no-registry.db"
    SQLiteStorage(str(path)).close()

    def must_not_construct(_args):
        raise AssertionError("judge constructor must not run")

    monkeypatch.setattr("verdict_eval.cli.pipeline._build_judge", must_not_construct)
    result = pipeline_main(
        [
            "--storage",
            f"sqlite:///{path}",
            "--method",
            "conversation-v1",
            "--registry-mode",
            "active",
            "--tenant-id",
            "tenant-a",
        ]
    )

    assert result == 2
    assert "unavailable:registry" in capsys.readouterr().out


def test_conversation_config_rejects_execution_bounds_above_alpha_maximum() -> None:
    with pytest.raises(ValueError, match="resource configuration"):
        replace(_config(), judge_concurrency=65)
    with pytest.raises(ValueError, match="resource configuration"):
        replace(_config(), judge_attempt_timeout=301)


def test_cross_window_sessions_are_provider_free_and_explicitly_insufficient(
    conversation_storage,
) -> None:
    config = _config("cross-window")
    _activate_explicit_registry(conversation_storage, config, overlap=True)
    rubric = Rubric("quality", "1", (RubricDimension("quality", "correct"),))
    payload = json.dumps({"quality": {"reasoning": "ok", "verdict": "PASS"}})

    result = run_conversation_analysis(
        conversation_storage,
        Judge(FakeProvider(payload), "fake-judge", rubric=rubric),
        config,
        assigner=lambda tenant, pinned, cutoff: (tenant, pinned, cutoff),
        judge_factory=lambda: pytest.fail("judge constructor called"),
    )

    assert result.status == "insufficient"
    assert result.provider_calls == 0
    snapshot = conversation_storage.get_conversation_drift_snapshot(config.tenant_id, config.run_id)
    assert snapshot is not None
    assert json.loads(snapshot[0].coverage_json)["cells"][0][-1] == 1


def test_judge_budget_is_checked_before_provider_construction(conversation_storage) -> None:
    config = replace(_config("judge-budget"), max_judge_calls=1)
    _activate_explicit_registry(conversation_storage, config)
    rubric = Rubric("quality", "1", (RubricDimension("quality", "correct"),))

    result = run_conversation_analysis(
        conversation_storage,
        Judge(FakeProvider("{}"), "fake-judge", rubric=rubric),
        config,
        judge_factory=lambda: pytest.fail("judge constructor called"),
    )

    assert result.status == "unavailable"
    assert result.reason == "judge_budget"
    assert result.provider_calls == 0


def test_terminal_errors_do_not_demote_a_cell_that_meets_the_scored_floor(
    conversation_storage,
) -> None:
    config = replace(_config("errors-with-floor"), target_per_cell=3)
    _activate_explicit_registry(conversation_storage, config, count=3)
    rubric = Rubric("quality", "1", (RubricDimension("quality", "correct"),))
    payload = json.dumps({"quality": {"reasoning": "ok", "verdict": "PASS"}})

    def response(request):
        if "-2" in request.messages[-1]["content"]:
            raise TimeoutError
        return payload

    result = run_conversation_analysis(
        conversation_storage,
        Judge(FakeProvider(response), "fake-judge", rubric=rubric),
        config,
        assigner=lambda tenant, pinned, cutoff: (tenant, pinned, cutoff),
    )

    assert result.status == "ready"
    snapshot = conversation_storage.get_conversation_drift_snapshot(config.tenant_id, config.run_id)
    assert snapshot is not None
    cell = json.loads(snapshot[0].coverage_json)["cells"][0]
    assert cell[15:18] == [1, 1, 3]


def test_attempt_timeout_freezes_errors_and_stops_scheduling_new_waves(
    conversation_storage,
) -> None:
    config = replace(
        _config("attempt-timeout"),
        target_per_cell=2,
        judge_concurrency=2,
        judge_attempt_timeout=1,
    )
    _activate_explicit_registry(conversation_storage, config)
    rubric = Rubric("quality", "1", (RubricDimension("quality", "correct"),))
    payload = json.dumps({"quality": {"reasoning": "ok", "verdict": "PASS"}})
    calls = 0
    finished = False

    def response(_request):
        nonlocal calls, finished
        calls += 1
        if calls == 1:
            time.sleep(1.1)
            finished = True
        return payload

    result = run_conversation_analysis(
        conversation_storage,
        Judge(FakeProvider(response), "fake-judge", rubric=rubric),
        config,
        assigner=lambda tenant, pinned, cutoff: (tenant, pinned, cutoff),
    )

    assert result.provider_calls == 2
    assert finished is True
    snapshot = conversation_storage.get_conversation_drift_snapshot(config.tenant_id, config.run_id)
    assert snapshot is not None
    assert sum(sample.error_category == "timeout" for sample in snapshot[1]) == 3


def test_external_judge_without_native_single_attempt_timeout_is_rejected_before_work(
    conversation_storage,
) -> None:
    config = _config("missing-provider-timeout")
    _activate_explicit_registry(conversation_storage, config)
    rubric = Rubric("quality", "1", (RubricDimension("quality", "correct"),))

    class MissingTimeoutProvider:
        name = "external"

        def complete(self, _request):
            raise AssertionError("provider must not be called")

    with pytest.raises(ValueError, match="evaluator_definition"):
        run_conversation_analysis(
            conversation_storage,
            Judge(MissingTimeoutProvider(), "external-judge", rubric=rubric),
            config,
        )
