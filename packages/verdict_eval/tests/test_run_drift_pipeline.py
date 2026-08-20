from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from verdict.schema import (
    DimensionScore,
    Judgment,
    JudgmentStatus,
    Trace,
    Verdict,
)
from verdict.storage.sqlite import SQLiteStorage
from verdict_eval.cli.pipeline import _storage_backend, build_parser, main
from verdict_eval.judge import DEFAULT_RUBRIC, Judge
from verdict_eval.providers import FakeProvider


def test_pipeline_defaults_to_semantic_clustering_and_discloses_effect_floor():
    args = build_parser().parse_args([])

    assert args.embedder == "sentence-transformer"
    assert args.clustering_version == "v2"
    assert args.cluster_threshold == 0.50
    assert args.effect_size_threshold == 0.147


def test_pipeline_reads_storage_from_environment_without_cli_echo(monkeypatch):
    monkeypatch.setenv("VERDICT_STORAGE", "postgresql://user:secret@db/verdict")

    assert build_parser().parse_args([]).storage.endswith("@db/verdict")


def test_pipeline_help_renders_percent_signs(capsys):
    try:
        build_parser().parse_args(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:  # pragma: no cover - argparse always exits after rendering help
        raise AssertionError("--help did not exit")

    output = capsys.readouterr().out
    assert "Minimum 95% Wilson-CI lower bound" in output


def test_pipeline_names_storage_without_exposing_credentials(monkeypatch, capsys):
    from verdict import client

    class EmptyStorage:
        def list_traces(self, *, limit):
            return []

    dsn = "postgresql://operator:secret-canary@db.example/verdict"
    monkeypatch.setattr(client, "_resolve_storage", lambda _storage: EmptyStorage())

    assert _storage_backend(dsn) == "postgresql"
    assert main(["--storage", dsn]) == 0
    output = capsys.readouterr().out
    assert "Storage backend: postgresql" in output
    assert "secret-canary" not in output


def test_real_pipeline_uses_trace_time_and_replaces_hourly_result(tmp_path, monkeypatch, capsys):
    """Exercise the actual CLI entrypoint repeatedly over one SQLite store.

    All judgments are created at the analysis time, while their traces belong
    to distinct historical/current periods. The old judgment-time splitter
    produced no baseline. A random signal ID also produced two persisted rows
    on the second run. A third, deliberately stricter run verifies that a
    signal which no longer clears the configured gate is removed.
    """
    db_path = tmp_path / "pipeline.db"
    analysis_time = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    evaluator_identity = Judge(
        provider=FakeProvider("{}"),
        model="precomputed-test-judge",
        rubric=DEFAULT_RUBRIC,
    ).evaluator_identity(context=None)
    storage = SQLiteStorage(str(db_path))
    for period, started_at, verdict in [
        ("baseline", analysis_time - timedelta(days=3), Verdict.PASS),
        ("current", analysis_time - timedelta(hours=2), Verdict.FAIL),
    ]:
        for i in range(40):
            trace = Trace(
                trace_id=f"{period}-{i}",
                started_at=started_at + timedelta(seconds=i),
                provider="test",
                request_model="test-model",
                response_model="test-model",
                prompt_redacted="Explain the refund policy.",
                response_redacted="A captured response.",
                cluster_id="c1",
                tenant_id="tenant-a",
            )
            storage.insert_trace(trace)
            storage.insert_judgment(Judgment(
                trace_id=trace.trace_id,
                created_at=analysis_time,
                dimensions=[DimensionScore(name="completeness", verdict=verdict)],
                **evaluator_identity,
            ))
    storage.close()

    argv = [
        "run_drift_pipeline.py",
        "--storage", f"sqlite:///{db_path}",
        "--judge-provider", "fake",
        "--judge-model", "precomputed-test-judge",
        "--embedder", "deterministic",
        "--trust-existing-clusters",
        "--as-of", analysis_time.isoformat(),
        "--min-sample-size", "30",
        "--target-per-cluster", "40",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    assert main() == 0
    first_output = capsys.readouterr().out
    assert "Current windows:  1  (total n = 40)" in first_output
    assert "Baseline windows: 1  (total n = 40)" in first_output
    assert "Detected 1 drift signal(s)." in first_output

    assert main() == 0
    second_output = capsys.readouterr().out
    assert "Persisted 0 completed judgment(s) and 0 error record(s)." in second_output
    assert "Detected 1 drift signal(s)." in second_output

    check = SQLiteStorage(str(db_path))
    signals = check.list_drift_signals(limit=20)
    judgments = check.list_judgments_for_cluster("c1", limit=1000)
    check.close()

    assert len(judgments) == 80
    assert len(signals) == 1
    assert signals[0].dimension == "completeness"
    assert signals[0].evaluator_fingerprint == evaluator_identity[
        "evaluator_fingerprint"
    ]
    assert signals[0].example_trace_ids
    assert all(trace_id.startswith("current-") for trace_id in signals[0].example_trace_ids)

    monkeypatch.setattr(sys, "argv", [*argv, "--effect-size-threshold", "1.1"])
    assert main() == 0
    third_output = capsys.readouterr().out
    assert "Detected 0 drift signal(s)." in third_output

    check = SQLiteStorage(str(db_path))
    snapshot = check.get_latest_drift_run_snapshot(
        evaluator_identity["evaluator_fingerprint"]
    )
    historical_signals = check.list_drift_signals(limit=20)
    check.close()

    assert snapshot is not None
    assert snapshot[0].signal_count == 0
    assert snapshot[1] == []
    assert len(historical_signals) == 1


def test_pipeline_rejects_metadata_only_capture(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "metadata-only.db"
    storage = SQLiteStorage(str(db_path))
    storage.insert_trace(Trace(provider="test", request_model="test-model"))
    storage.close()

    monkeypatch.setattr(sys, "argv", [
        "run_drift_pipeline.py",
        "--storage", f"sqlite:///{db_path}",
        "--judge-provider", "fake",
        "--embedder", "deterministic",
    ])

    assert main() == 2
    assert "capture_content=True" in capsys.readouterr().out


def test_pipeline_rejects_mixed_tenant_store(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "mixed-tenant.db"
    storage = SQLiteStorage(str(db_path))
    for tenant in ("tenant-a", "tenant-b"):
        storage.insert_trace(Trace(
            provider="test",
            request_model="test-model",
            prompt_redacted="A prompt",
            response_redacted="A response",
            tenant_id=tenant,
        ))
    storage.close()

    monkeypatch.setattr(sys, "argv", [
        "run_drift_pipeline.py",
        "--storage", f"sqlite:///{db_path}",
        "--judge-provider", "fake",
        "--embedder", "deterministic",
    ])

    assert main() == 2
    assert "multiple tenant scopes" in capsys.readouterr().out


def test_pipeline_keeps_judge_models_separate_and_uniform_reruns_do_not_duplicate(
    tmp_path, monkeypatch, capsys,
):
    db_path = tmp_path / "judge-isolation.db"
    analysis_time = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    storage = SQLiteStorage(str(db_path))
    trace = Trace(
        trace_id="current-1",
        started_at=analysis_time - timedelta(hours=1),
        provider="test",
        request_model="test-model",
        prompt_redacted="Explain the refund policy.",
        response_redacted="A captured response.",
        cluster_id="c1",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        judge_models=["old-judge"],
        dimensions=[
            DimensionScore(name=name, verdict=Verdict.FAIL)
            for name in (
                "groundedness",
                "relevance",
                "completeness",
                "safety",
                "instruction_following",
            )
        ],
    ))
    storage.close()

    argv = [
        "run_drift_pipeline.py",
        "--storage", f"sqlite:///{db_path}",
        "--judge-provider", "fake",
        "--judge-model", "new-judge",
        "--embedder", "deterministic",
        "--trust-existing-clusters",
        "--sampling", "uniform",
        "--sample-rate", "1.0",
        "--as-of", analysis_time.isoformat(),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    assert main() == 0
    first_output = capsys.readouterr().out
    assert "Persisted 1 completed judgment(s) and 0 error record(s)." in first_output
    assert "Current windows:  5  (total n = 5)" in first_output

    assert main() == 0
    second_output = capsys.readouterr().out
    assert "Persisted 0 completed judgment(s) and 0 error record(s)." in second_output
    assert "Current windows:  5  (total n = 5)" in second_output

    check = SQLiteStorage(str(db_path))
    judgments = check.list_judgments_for_cluster("c1", limit=100)
    check.close()
    assert len(judgments) == 2
    assert {tuple(j.judge_models) for j in judgments} == {
        ("old-judge",),
        ("new-judge",),
    }


def test_pipeline_retries_when_latest_attempt_for_current_evaluator_is_error(
    tmp_path, monkeypatch, capsys,
):
    db_path = tmp_path / "retry-latest-error.db"
    analysis_time = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    evaluator_identity = Judge(
        provider=FakeProvider("{}"),
        model="retry-judge",
        rubric=DEFAULT_RUBRIC,
    ).evaluator_identity(context=None)
    storage = SQLiteStorage(str(db_path))
    trace = Trace(
        trace_id="current-1",
        started_at=analysis_time - timedelta(hours=1),
        provider="test",
        request_model="test-model",
        prompt_redacted="Explain the refund policy.",
        response_redacted="A captured response.",
        cluster_id="c1",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        judgment_id="completed-old",
        trace_id=trace.trace_id,
        created_at=analysis_time - timedelta(minutes=2),
        dimensions=[
            DimensionScore(name="relevance", verdict=Verdict.PASS)
        ],
        **evaluator_identity,
    ))
    storage.insert_judgment(Judgment(
        judgment_id="error-new",
        trace_id=trace.trace_id,
        created_at=analysis_time - timedelta(minutes=1),
        dimensions=[],
        status=JudgmentStatus.ERROR,
        error="provider unavailable",
        **evaluator_identity,
    ))
    storage.close()

    monkeypatch.setattr(sys, "argv", [
        "run_drift_pipeline.py",
        "--storage", f"sqlite:///{db_path}",
        "--judge-provider", "fake",
        "--judge-model", "retry-judge",
        "--embedder", "deterministic",
        "--trust-existing-clusters",
        "--sampling", "uniform",
        "--sample-rate", "1.0",
        "--as-of", analysis_time.isoformat(),
    ])

    assert main() == 0
    assert "Persisted 1 completed judgment(s) and 0 error record(s)." in (
        capsys.readouterr().out
    )

    check = SQLiteStorage(str(db_path))
    try:
        judgments = check.list_judgments_for_cluster("c1", limit=100)
    finally:
        check.close()
    assert len(judgments) == 3


def test_pipeline_rejects_cluster_ids_from_an_unrelated_registry(
    tmp_path, monkeypatch, capsys,
):
    db_path = tmp_path / "incompatible-clusters.db"
    storage = SQLiteStorage(str(db_path))
    storage.insert_trace(Trace(
        provider="test",
        request_model="test-model",
        prompt_redacted="A prompt",
        response_redacted="A response",
        cluster_id="v1-000001",
    ))
    storage.close()

    monkeypatch.setattr(sys, "argv", [
        "run_drift_pipeline.py",
        "--storage", f"sqlite:///{db_path}",
        "--judge-provider", "fake",
        "--embedder", "deterministic",
        "--clustering-version", "v2",
    ])

    assert main() == 2
    output = capsys.readouterr().out
    assert "do not belong to registry 'v2'" in output
    assert "--recluster" in output


def test_trusted_external_clusters_require_every_judgeable_trace_to_be_assigned(
    tmp_path, monkeypatch, capsys,
):
    db_path = tmp_path / "missing-external-cluster.db"
    storage = SQLiteStorage(str(db_path))
    storage.insert_trace(Trace(
        provider="test",
        request_model="test-model",
        prompt_redacted="A prompt",
        response_redacted="A response",
    ))
    storage.close()

    monkeypatch.setattr(sys, "argv", [
        "run_drift_pipeline.py",
        "--storage", f"sqlite:///{db_path}",
        "--judge-provider", "fake",
        "--embedder", "deterministic",
        "--trust-existing-clusters",
    ])

    assert main() == 2
    assert "requires every judgeable trace" in capsys.readouterr().out


def test_pipeline_persists_sentinel_health_separately_from_judgments(
    tmp_path, monkeypatch, capsys,
):
    db_path = tmp_path / "judge-health.db"
    sentinel_path = tmp_path / "sentinels.jsonl"
    sentinel_rows = ['{"set_name":"support-v1"}']
    sentinel_rows.extend(
        '{"sentinel_id":"anchor-' + str(index) + '","query":"q","response":"r",'
        '"labels":{"groundedness":"pass","relevance":"pass",'
        '"completeness":"pass","safety":"pass",'
        '"instruction_following":"pass"}}'
        for index in range(30)
    )
    sentinel_path.write_text('\n'.join(sentinel_rows))
    storage = SQLiteStorage(str(db_path))
    storage.insert_trace(Trace(
        provider="test",
        request_model="test-model",
        prompt_redacted="A production prompt",
        response_redacted="A production response",
        cluster_id="external-cluster",
    ))
    storage.close()

    monkeypatch.setattr(sys, "argv", [
        "run_drift_pipeline.py",
        "--storage", f"sqlite:///{db_path}",
        "--judge-provider", "fake",
        "--judge-model", "health-judge",
        "--judge-sentinel-file", str(sentinel_path),
        "--embedder", "deterministic",
        "--trust-existing-clusters",
        "--sampling", "uniform",
    ])

    assert main() == 0
    output = capsys.readouterr().out
    assert "Judge health (support-v1): healthy" in output
    assert "monitored separately from production drift" in output

    check = SQLiteStorage(str(db_path))
    try:
        health = check.list_evaluator_health()
        judgments = check.list_judgments_for_cluster("external-cluster")
    finally:
        check.close()
    assert len(health) == 1
    assert health[0].total_labels == 150
    assert len(judgments) == 1
    assert judgments[0].trace_id != "sentinel:anchor-1"


def test_pipeline_stops_before_production_judging_when_sentinel_health_degraded(
    tmp_path, monkeypatch, capsys,
):
    db_path = tmp_path / "degraded-judge.db"
    sentinel_path = tmp_path / "degraded.jsonl"
    rows = ['{"set_name":"degraded"}']
    rows.extend(
        '{"sentinel_id":"anchor-' + str(index) + '","query":"q","response":"r",'
        '"labels":{"groundedness":"fail","relevance":"fail",'
        '"completeness":"fail","safety":"fail",'
        '"instruction_following":"fail"}}'
        for index in range(30)
    )
    sentinel_path.write_text('\n'.join(rows))
    storage = SQLiteStorage(str(db_path))
    storage.insert_trace(Trace(
        provider="test",
        request_model="test-model",
        prompt_redacted="A production prompt",
        response_redacted="A production response",
        cluster_id="external-cluster",
    ))
    storage.close()

    monkeypatch.setattr(sys, "argv", [
        "run_drift_pipeline.py",
        "--storage", f"sqlite:///{db_path}",
        "--judge-provider", "fake",
        "--judge-model", "unhealthy-judge",
        "--judge-sentinel-file", str(sentinel_path),
        "--embedder", "deterministic",
        "--trust-existing-clusters",
        "--sampling", "uniform",
    ])

    assert main() == 2
    output = capsys.readouterr().out
    assert "Judge health (degraded): degraded" in output
    assert "production judging and drift detection are blocked" in output

    check = SQLiteStorage(str(db_path))
    try:
        assert len(check.list_evaluator_health()) == 1
        assert check.list_judgments_for_cluster("external-cluster") == []
    finally:
        check.close()
