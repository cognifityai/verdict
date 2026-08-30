from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from verdict.schema import Operation, Trace
from verdict.storage import SQLiteStorage
from verdict_eval.cli.monitor import main
from verdict_eval.count_monitor import AnalysisStatus, plan_history

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


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
    payload = json.loads(capsys.readouterr().out)
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
