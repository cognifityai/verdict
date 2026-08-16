from __future__ import annotations

import sys
from datetime import datetime, timezone

from verdict_eval.probes import ProbeResult, ProbeRun

from scripts import run_probes


def _run(*results: ProbeResult) -> ProbeRun:
    return ProbeRun(
        suite_name="test",
        suite_version="1",
        target_model="target",
        judge_model="judge",
        started_at=datetime.now(timezone.utc),
        results=list(results),
    )


def test_probe_exit_code_distinguishes_quality_failure_and_execution_error():
    failed = ProbeResult("failed", "cat", "response", overall_passed=False)
    errored = ProbeResult(
        "error", "cat", "", overall_passed=False, error="provider unavailable"
    )
    passed = ProbeResult("passed", "cat", "response", overall_passed=True)

    assert run_probes.probe_run_exit_code(_run(failed), min_pass_rate=1.0) == 1
    assert run_probes.probe_run_exit_code(_run(errored), min_pass_rate=0.0) == 2
    assert run_probes.probe_run_exit_code(_run(passed), min_pass_rate=1.0) == 0


def test_main_returns_nonzero_when_every_probe_fails(tmp_path, monkeypatch):
    failed_run = _run(
        ProbeResult("a", "cat", "response", overall_passed=False),
        ProbeResult("b", "cat", "response", overall_passed=False),
    )

    class _Runner:
        def __init__(self, **_kwargs):
            pass

        def run_suite(self, _suite):
            return failed_run

    monkeypatch.setattr(run_probes, "ProbeRunner", _Runner)
    monkeypatch.setattr(run_probes, "_provider_for_model", lambda _model: (object(), "m"))
    monkeypatch.setattr(
        run_probes,
        "default_suite",
        lambda: type("Suite", (), {"name": "test", "version": "1", "probes": [1, 2]})(),
    )
    monkeypatch.setattr(sys, "argv", [
        "run_probes.py",
        "--target-model", "openai/target",
        "--judge-model", "anthropic/judge",
        "--out", str(tmp_path / "run.json"),
    ])

    assert run_probes.main() == 1
    assert (tmp_path / "run.json").is_file()
