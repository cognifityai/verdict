from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from verdict.schema import DimensionScore, Judgment, Trace, Verdict
from verdict.storage.sqlite import SQLiteStorage

from scripts.run_drift_pipeline import build_parser, main


def test_pipeline_defaults_to_semantic_clustering_and_discloses_effect_floor():
    args = build_parser().parse_args([])

    assert args.embedder == "sentence-transformer"
    assert args.clustering_version == "v2"
    assert args.cluster_threshold == 0.50
    assert args.effect_size_threshold == 0.147


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
                judge_models=["precomputed-test-judge"],
                dimensions=[DimensionScore(name="completeness", verdict=verdict)],
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
    assert "Persisted 0 judgments." in second_output
    assert "Detected 1 drift signal(s)." in second_output

    check = SQLiteStorage(str(db_path))
    signals = check.list_drift_signals(limit=20)
    judgments = check.list_judgments_for_cluster("c1", limit=1000)
    check.close()

    assert len(judgments) == 80
    assert len(signals) == 1
    assert signals[0].dimension == "completeness"
    assert signals[0].example_trace_ids
    assert all(trace_id.startswith("current-") for trace_id in signals[0].example_trace_ids)

    monkeypatch.setattr(sys, "argv", [*argv, "--effect-size-threshold", "1.1"])
    assert main() == 0
    third_output = capsys.readouterr().out
    assert "Detected 0 drift signal(s)." in third_output

    check = SQLiteStorage(str(db_path))
    assert check.list_drift_signals(limit=20) == []
    check.close()


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
    assert "Persisted 1 judgments." in first_output
    assert "Current windows:  5  (total n = 5)" in first_output

    assert main() == 0
    second_output = capsys.readouterr().out
    assert "Persisted 0 judgments." in second_output
    assert "Current windows:  5  (total n = 5)" in second_output

    check = SQLiteStorage(str(db_path))
    judgments = check.list_judgments_for_cluster("c1", limit=100)
    check.close()
    assert len(judgments) == 2
    assert {tuple(j.judge_models) for j in judgments} == {
        ("old-judge",),
        ("new-judge",),
    }


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
