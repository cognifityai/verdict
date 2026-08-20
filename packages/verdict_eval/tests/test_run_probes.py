from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from verdict_eval.cli import probes as run_probes
from verdict_eval.probes import ProbeResult, ProbeRun


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


def test_unknown_probe_model_is_an_operator_error(capsys):
    assert run_probes.main([
        "--target-model",
        "unknown/model",
        "--judge-model",
        "anthropic/judge",
    ]) == 2

    output = capsys.readouterr()
    assert output.out.startswith("Suite:")
    assert output.err.startswith("ERROR: Unknown model prefix")


def test_probe_exit_code_treats_judge_dimension_error_as_execution_error():
    judge_error = ProbeResult(
        "judge-error",
        "cat",
        "response",
        dimensions=[
            {
                "name": "relevance",
                "observed": "ERROR",
                "passed": False,
            }
        ],
        overall_passed=False,
    )

    assert run_probes.probe_run_exit_code(
        _run(judge_error),
        min_pass_rate=0.0,
    ) == 2


def test_probe_exit_code_gates_probe_rate_not_expectation_agreement():
    many_pass = ProbeResult(
        "many-pass",
        "cat",
        "response",
        dimensions=[{"name": f"d{index}", "passed": True} for index in range(10)],
        overall_passed=True,
    )
    one_fail = ProbeResult(
        "one-fail",
        "cat",
        "response",
        dimensions=[{"name": "safety", "passed": False}],
        overall_passed=False,
    )

    assert run_probes.probe_run_exit_code(
        _run(many_pass, one_fail),
        min_pass_rate=0.9,
    ) == 1


def test_probe_exit_code_fails_malformed_dimension_container_closed():
    malformed = ProbeResult(
        "malformed",
        "cat",
        "response",
        dimensions=None,  # type: ignore[arg-type] - corrupt persisted artifact
        overall_passed=True,
    )

    assert run_probes.probe_run_exit_code(
        _run(malformed),
        min_pass_rate=1.0,
    ) == 1


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

    output_path = tmp_path / "run.json"
    assert run_probes.main() == 1
    payload = json.loads(output_path.read_text())
    assert payload["summary"] == {
        "probe_pass_rate": 0.0,
        "probe_passed_weight": 0.0,
        "probe_total_weight": 2.0,
        "probe_pass_rate_by_category": {"cat": 0.0},
        "expectation_agreement": 0.0,
        "expectation_passed_weight": 0.0,
        "expectation_total_weight": 2.0,
        "expectation_agreement_by_dimension": {},
    }


def test_persisted_summary_keeps_probe_gate_separate_from_expectations():
    run = _run(
        ProbeResult(
            "many-pass",
            "cat",
            "response",
            dimensions=[
                {"name": f"d{index}", "passed": True}
                for index in range(10)
            ],
            overall_passed=True,
        ),
        ProbeResult(
            "one-fail",
            "cat",
            "response",
            dimensions=[{"name": "safety", "passed": False}],
            overall_passed=False,
        ),
    )

    summary = run_probes.probe_run_payload(run)["summary"]

    assert summary["probe_pass_rate"] == 0.5
    assert summary["expectation_agreement"] == 10 / 11
    assert summary["probe_total_weight"] == 2.0
    assert summary["expectation_total_weight"] == 11.0
