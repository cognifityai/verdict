from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
from verdict.schema import (
    DimensionScore,
    DriftDirection,
    Judgment,
    Operation,
    Trace,
    Verdict,
)
from verdict.storage import SQLiteStorage
from verdict.storage.memory import InMemoryStorage
from verdict_eval.cli.monitor import main
from verdict_eval.count_monitor import AnalysisStatus, analyze_traces, plan_history
from verdict_eval.monitoring import create_series_from_history

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


class _SemanticEmbedder:
    dim = 2
    model_name = "test-semantic"
    model_revision = "v1"
    model_file_sha256 = "test"

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float64)


def _trace(
    index: int,
    *,
    session: str | None,
    when: datetime,
    response: str,
    trace_id: str | None = None,
    granularity: str = "llm-call",
) -> Trace:
    return Trace(
        trace_id=trace_id or f"trace-{index:03d}",
        started_at=when,
        ended_at=when + timedelta(seconds=1),
        provider="unknown-provider",
        operation=Operation.CHAT,
        request_model="unknown-model",
        response_model="unknown-model",
        prompt_redacted="summarize the incident report",
        response_redacted=response,
        latency_ms=100 + index,
        output_tokens=10 + index,
        session_id=session,
        tags={
            "verdict.workload": "agent",
            "capture.granularity": granularity,
        },
    )


def test_history_plan_orders_independent_sessions_by_event_time_and_never_splits_one() -> None:
    tied = NOW - timedelta(days=60)
    traces = [
        _trace(0, session="session-c", when=tied, response="ok", trace_id="c"),
        _trace(1, session="session-a", when=tied, response="ok", trace_id="a-1"),
        _trace(2, session="session-a", when=tied + timedelta(days=20), response="later"),
        _trace(3, session="session-b", when=tied, response="ok", trace_id="b"),
        _trace(4, session="session-d", when=tied, response="ok", trace_id="d"),
        _trace(5, session="session-e", when=tied, response="ok", trace_id="e"),
    ]

    plan = plan_history(reversed(traces))

    assert [unit.unit_id for unit in plan.baseline] == ["session-a", "session-b"]
    assert [unit.unit_id for unit in plan.current] == ["session-d", "session-e"]
    assert plan.excluded_middle.unit_id == "session-c"
    assert {trace.trace_id for unit in plan.baseline for trace in unit.traces} == {
        "a-1",
        "trace-002",
        "b",
    }
    assert not (
        {trace.trace_id for unit in plan.baseline for trace in unit.traces}
        & {trace.trace_id for unit in plan.current for trace in unit.traces}
    )


def test_bootstrap_detects_old_count_cohort_drift_with_fewer_than_thirty_sessions(
    tmp_path, capsys
) -> None:
    db = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(db))
    try:
        for index in range(8):
            storage.insert_trace(
                _trace(
                    index,
                    session=f"baseline-{index}",
                    when=NOW - timedelta(days=80 - index),
                    response="short useful answer",
                )
            )
        for index in range(8, 16):
            storage.insert_trace(
                _trace(
                    index,
                    session=f"current-{index}",
                    when=NOW - timedelta(days=40 - index),
                    response="I cannot comply. " + "verbose " * 120,
                )
            )
    finally:
        storage.close()

    exit_code = main(
        [
            "--storage",
            f"sqlite:///{db}",
            "bootstrap",
            "--activate",
            "--json",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "NaN" not in output
    payload = json.loads(output)
    assert payload["schema"] == "verdict-count-monitor-v1"
    assert len(payload["scopes"]) == 1
    report = payload["scopes"][0]
    assert report["baseline_units"] == 8
    assert report["current_units"] == 8
    assert report["status"] == AnalysisStatus.DRIFT_DETECTED.value
    storage = SQLiteStorage(str(db))
    try:
        active = storage.list_monitor_series()[0]
        assert active.target_units == 8
    finally:
        storage.close()
    assert any(
        result["metric"] == "response_words"
        and result["status"] == AnalysisStatus.DRIFT_DETECTED.value
        and result["baseline_n"] == 8
        and result["current_n"] == 8
        for result in report["results"]
    )

    assert (
        main(
            [
                "--storage",
                f"sqlite:///{db}",
                "bootstrap",
                "--from",
                (NOW - timedelta(days=76)).isoformat(),
                "--json",
            ]
        )
        == 0
    )
    sliced = json.loads(capsys.readouterr().out)
    assert sliced["scopes"][0]["baseline_units"] == 6
    assert sliced["scopes"][0]["current_units"] == 6


def test_monitor_cli_semantic_bootstrap_uses_versioned_registry(
    tmp_path, capsys, monkeypatch
) -> None:
    import verdict_eval.clustering as clustering

    db = tmp_path / "semantic.db"
    storage = SQLiteStorage(str(db))
    try:
        for index in range(8):
            trace = _trace(
                index,
                session=f"session-{index}",
                when=NOW + timedelta(hours=index),
                response="useful response",
            )
            trace.raw_messages = [{"role": "user", "content": trace.prompt_redacted}]
            storage.insert_trace(trace)
    finally:
        storage.close()
    monkeypatch.setattr(clustering, "FrozenMiniLMEmbedder", lambda _path: _SemanticEmbedder())

    assert (
        main(
            [
                "--storage",
                f"sqlite:///{db}",
                "--semantic-model-path",
                str(tmp_path),
                "bootstrap",
                "--activate",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["active_series_ids"]
    storage = SQLiteStorage(str(db))
    try:
        pointer = storage.get_active_cluster_registry("__verdict_local__")
        [series] = storage.list_monitor_series()
    finally:
        storage.close()
    assert pointer.version_id is not None
    assert "cluster-registry-reference-v1" in series.registry_json


def test_quality_cohort_uses_completed_judgments_without_pooling_evaluators() -> None:
    traces: list[Trace] = []
    judgments: list[Judgment] = []
    evaluator = "quality-evaluator-v1"
    for index in range(16):
        trace = _trace(
            index,
            session=f"session-{index:02d}",
            when=NOW + timedelta(hours=index),
            response="same structural response",
        )
        trace.latency_ms = 100
        trace.output_tokens = 10
        traces.append(trace)
        judgments.append(
            Judgment(
                judgment_id=f"judgment-{index:02d}",
                trace_id=trace.trace_id,
                created_at=trace.started_at + timedelta(minutes=1),
                judge_models=["judge-model"],
                dimensions=[
                    DimensionScore(
                        name="completeness",
                        verdict=Verdict.PASS if index < 8 else Verdict.FAIL,
                        judge_model="judge-model",
                    )
                ],
                evaluator_provider="test",
                evaluator_config={"temperature": 0},
                evaluator_fingerprint=evaluator,
                expected_dimensions=["completeness"],
            )
        )

    reports = analyze_traces(traces, judgments=judgments)

    quality = next(report for report in reports if report.scope.evidence_layer == "quality")
    assert quality.scope.evaluator_fingerprint == evaluator
    assert quality.baseline_units == quality.current_units == 8
    assert quality.tested_hypotheses == 1
    result = next(row for row in quality.results if row.metric == "quality.completeness.pass_rate")
    assert result.baseline_n == result.current_n == 8
    assert result.baseline_value == 1.0
    assert result.current_value == 0.0
    assert result.status is AnalysisStatus.DRIFT_DETECTED

    storage = InMemoryStorage()
    try:
        for trace in traces:
            storage.insert_trace(trace)
        for judgment in judgments:
            storage.insert_judgment(judgment)
        series = create_series_from_history(
            storage,
            traces,
            target_units=8,
            state="active",
            judgments=judgments,
        )
        assert len(series) == 2
        run, signals = storage.get_latest_drift_run_snapshot(evaluator) or (None, [])
    finally:
        storage.close()
    assert run is not None
    [signal] = [row for row in signals if row.dimension == "quality.completeness.pass_rate"]
    assert signal.direction is DriftDirection.REGRESSION
    assert signal.sample_size_baseline == signal.sample_size_current == 8


def test_constant_metrics_are_descriptive_but_not_counted_as_tested_hypotheses() -> None:
    traces = []
    for index in range(16):
        trace = _trace(
            index,
            session=f"constant-{index:02d}",
            when=NOW + timedelta(hours=index),
            response="identical response",
        )
        trace.latency_ms = 100
        trace.output_tokens = 10
        traces.append(trace)

    [report] = analyze_traces(traces)

    assert report.tested_hypotheses == 0
    assert report.status is AnalysisStatus.NOT_EVALUABLE
    assert report.results
    assert all(not result.tested for result in report.results)


def test_semantically_ineligible_units_are_not_compared_as_an_intent_cluster() -> None:
    traces = [
        _trace(
            index,
            session=f"session-{index}",
            when=NOW + timedelta(hours=index),
            response="same response",
        )
        for index in range(8)
    ]
    assignments = {trace.trace_id: "not_evaluable" for trace in traces}

    [report] = analyze_traces(traces, assignments=assignments)

    assert report.status is AnalysisStatus.NOT_EVALUABLE
    assert report.results == ()


def test_historical_new_intent_traffic_is_visible_instead_of_silently_dropped() -> None:
    traces = []
    for index in range(16):
        trace = _trace(
            index,
            session=f"intent-{index:02d}",
            when=NOW + timedelta(hours=index),
            response="identical response",
        )
        trace.latency_ms = 100
        trace.output_tokens = 10
        if index >= 8:
            trace.prompt_redacted = "design a watercolor garden irrigation controller"
        traces.append(trace)

    [report] = analyze_traces(traces)

    result = next(row for row in report.results if row.metric == "new_intent_traffic")
    assert result.cluster_id == "new_intent"
    assert result.baseline_n == 0
    assert result.current_n == 8
    assert result.current_value == 1.0
    assert not result.tested
    assert result.status is AnalysisStatus.DRIFT_DETECTED
    assert report.status is AnalysisStatus.DRIFT_DETECTED

    storage = InMemoryStorage()
    try:
        for trace in traces:
            storage.insert_trace(trace)
        create_series_from_history(storage, traces, target_units=8, state="active")
        snapshot = storage.get_latest_drift_run_snapshot("deterministic-structural-count-v1")
    finally:
        storage.close()
    assert snapshot is not None
    _, signals = snapshot
    [signal] = [row for row in signals if row.dimension == "new_intent_traffic"]
    assert signal.sample_size_baseline == 0
    assert signal.sample_size_current == 8
    assert signal.contributing_layers == ["cluster_coverage"]


def test_zero_tested_hypotheses_is_not_reported_as_no_drift(tmp_path, capsys) -> None:
    db = tmp_path / "empty.db"
    storage = SQLiteStorage(str(db))
    try:
        storage.insert_trace(_trace(0, session="only-one", when=NOW, response="one response"))
    finally:
        storage.close()

    assert main(["--storage", f"sqlite:///{db}", "bootstrap", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["scopes"][0]["status"] == AnalysisStatus.COLLECTING_BASELINE.value
    assert payload["scopes"][0]["tested_hypotheses"] == 0


def test_granularity_isolates_agent_turns_from_llm_calls(tmp_path, capsys) -> None:
    db = tmp_path / "mixed.db"
    storage = SQLiteStorage(str(db))
    try:
        for index, granularity in enumerate(("agent-turn", "agent-turn", "llm-call", "llm-call")):
            storage.insert_trace(
                _trace(
                    index,
                    session=f"session-{index}",
                    when=NOW + timedelta(minutes=index),
                    response="response",
                    granularity=granularity,
                )
            )
    finally:
        storage.close()

    assert main(["--storage", f"sqlite:///{db}", "bootstrap", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert {scope["scope"]["granularity"] for scope in payload["scopes"]} == {
        "agent-turn",
        "llm-call",
    }


def test_matched_poc_compares_same_prompt_ids_under_two_models(tmp_path, capsys) -> None:
    db = tmp_path / "matched.db"
    storage = SQLiteStorage(str(db))
    try:
        for index in range(10):
            baseline = _trace(
                index,
                session=f"baseline-{index}",
                when=NOW + timedelta(minutes=index),
                response="short useful answer",
                trace_id=f"baseline-{index}",
            )
            baseline.request_model = baseline.response_model = "model-a"
            baseline.tags["verdict.intent_key"] = f"prompt-{index}"
            storage.insert_trace(baseline)

            current = _trace(
                index + 10,
                session=f"current-{index}",
                when=NOW + timedelta(hours=1, minutes=index),
                response="I cannot comply. " + "verbose " * 120,
                trace_id=f"current-{index}",
            )
            current.request_model = current.response_model = "model-b"
            current.tags["verdict.intent_key"] = f"prompt-{index}"
            storage.insert_trace(current)
    finally:
        storage.close()

    assert (
        main(
            [
                "--storage",
                f"sqlite:///{db}",
                "matched",
                "--baseline-model",
                "model-a",
                "--current-model",
                "model-b",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "matched"
    assert payload["comparison"] == "controlled_comparison"
    assert payload["matched_pairs"] == 10
    assert payload["status"] == "drift_detected"
    assert any(
        result["metric"] == "response_words" and result["status"] == "drift_detected"
        for result in payload["results"]
    )
