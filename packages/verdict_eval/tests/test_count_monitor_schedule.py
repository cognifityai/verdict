from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from verdict.dashboard.app import build_bundle
from verdict.schema import Operation, Trace
from verdict.storage import SQLiteStorage
from verdict_eval.cli.monitor import main

START = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _insert(
    db: Path,
    *,
    prefix: str,
    start_index: int,
    count: int,
    when: datetime,
    response: str,
    prompt: str = "summarize the incident report",
) -> None:
    storage = SQLiteStorage(str(db))
    try:
        for offset in range(count):
            index = start_index + offset
            storage.insert_trace(
                Trace(
                    trace_id=f"{prefix}-{index}",
                    started_at=when + timedelta(minutes=offset),
                    ended_at=when + timedelta(minutes=offset, seconds=1),
                    provider="future-provider",
                    operation=Operation.CHAT,
                    request_model="future-model",
                    response_model="future-model",
                    prompt_redacted=prompt,
                    response_redacted=response,
                    latency_ms=100,
                    output_tokens=10,
                    session_id=f"{prefix}-session-{index}",
                    tags={"verdict.workload": "production"},
                )
            )
    finally:
        storage.close()


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_scheduled_monitor_is_idempotent_confirms_and_refits_atomically(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "verdict.db"
    _insert(
        db,
        prefix="history",
        start_index=0,
        count=16,
        when=START,
        response="short useful answer",
    )
    storage_url = f"sqlite:///{db}"

    assert (
        main(
            [
                "--storage",
                storage_url,
                "bootstrap",
                "--activate",
                "--target-units",
                "4",
                "--json",
            ]
        )
        == 0
    )
    bootstrap = _json(capsys)
    series_id = bootstrap["active_series_id"]
    assert bootstrap["scopes"][0]["status"] == "not_evaluable"
    assert bootstrap["scopes"][0]["tested_hypotheses"] == 0
    initial_dashboard = build_bundle(db)
    assert initial_dashboard["driftRun"] is not None
    assert initial_dashboard["driftAnalysis"]["runStatus"] == "completed_no_signals"

    _insert(
        db,
        prefix="candidate",
        start_index=0,
        count=4,
        when=START + timedelta(days=40),
        response="I cannot comply. " + "verbose " * 120,
    )
    assert main(["--storage", storage_url, "--limit", "4", "run", "--json"]) == 0
    first = _json(capsys)
    assert first["closed_cohorts"] == 1
    assert first["results"][0]["episode_status"] == "candidate"
    drift_dashboard = build_bundle(db)
    assert drift_dashboard["driftAnalysis"]["runStatus"] == "completed_with_signals"
    assert {signal["dimension"] for signal in drift_dashboard["driftSignals"]} >= {
        "response_words",
        "refusal_rate",
    }

    assert main(["--storage", storage_url, "run", "--json"]) == 0
    assert _json(capsys)["status"] == "no_op"

    _insert(
        db,
        prefix="confirmation",
        start_index=0,
        count=4,
        when=START + timedelta(days=41),
        response="I cannot comply. " + "verbose " * 120,
    )
    assert main(["--storage", storage_url, "run", "--json"]) == 0
    second = _json(capsys)
    assert second["results"][0]["episode_status"] == "confirmed"

    _insert(
        db,
        prefix="new-intent",
        start_index=0,
        count=4,
        when=START + timedelta(days=42),
        prompt="book a glacier safari on mars",
        response="new workload answer",
    )
    assert main(["--storage", storage_url, "run", "--json"]) == 0
    novelty = _json(capsys)
    assert novelty["results"][0]["episode_status"] == "new_intent"

    _insert(
        db,
        prefix="late",
        start_index=0,
        count=1,
        when=START + timedelta(days=1),
        response="late imported answer",
    )
    assert main(["--storage", storage_url, "run", "--json"]) == 0
    assert _json(capsys)["late_arrivals"] == 1
    assert main(["--storage", storage_url, "run", "--json"]) == 0
    assert _json(capsys)["late_arrivals"] == 0

    authoritative_run = build_bundle(db)["driftRun"]["id"]
    assert main(["--storage", storage_url, "refit", "--json"]) == 0
    candidate_id = _json(capsys)["candidate_series_id"]
    assert candidate_id != series_id
    assert build_bundle(db)["driftRun"]["id"] == authoritative_run
    assert main(["--storage", storage_url, "status", "--json"]) == 0
    before = _json(capsys)
    assert before["active_series_id"] == series_id

    assert (
        main(
            [
                "--storage",
                storage_url,
                "activate",
                "--series-id",
                candidate_id,
                "--expected-active",
                series_id,
                "--json",
            ]
        )
        == 0
    )
    activation = _json(capsys)
    assert activation["active_series_id"] == candidate_id
    assert build_bundle(db)["driftRun"]["id"] == activation["bootstrap_run_id"]
    assert main(["--storage", storage_url, "status", "--json"]) == 0
    after = _json(capsys)
    assert after["active_series_id"] == candidate_id
    assert {series["state"] for series in after["series"]} == {"active", "retired"}

    storage = SQLiteStorage(str(db))
    try:
        baseline_id = next(
            member.trace_id
            for member in storage.list_monitor_members(candidate_id)
            if member.role == "baseline"
        )
        storage.delete_trace(baseline_id)
    finally:
        storage.close()
    assert main(["--storage", storage_url, "run", "--json"]) == 0
    blocked = _json(capsys)
    assert blocked["status"] == "blocked"
    assert blocked["blocked_series"] == [
        {
            "series_id": candidate_id,
            "reason": "baseline_evidence_missing",
            "missing_traces": 1,
        }
    ]


def test_excluded_middle_session_is_not_reclassified_as_a_late_arrival(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "odd-history.db"
    _insert(
        db,
        prefix="odd",
        start_index=0,
        count=5,
        when=START,
        response="identical response",
    )
    storage_url = f"sqlite:///{db}"

    assert (
        main(
            [
                "--storage",
                storage_url,
                "bootstrap",
                "--activate",
                "--target-units",
                "2",
                "--json",
            ]
        )
        == 0
    )
    bootstrap = _json(capsys)
    [series_id] = bootstrap["active_series_ids"]

    assert main(["--storage", storage_url, "run", "--json"]) == 0
    scheduled = _json(capsys)
    assert scheduled["status"] == "no_op"
    assert scheduled["late_arrivals"] == 0

    storage = SQLiteStorage(str(db))
    try:
        excluded = [
            member
            for member in storage.list_monitor_members(series_id)
            if member.role == "excluded"
        ]
    finally:
        storage.close()
    assert len({member.unit_id for member in excluded}) == 1
