from __future__ import annotations

import runpy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from verdict.dashboard.app import build_bundle
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.files import iter_telemetry_file
from verdict.telemetry.model import ImportContext
from verdict.telemetry.runner import import_into_storage
from verdict_eval.cli.pipeline import main as pipeline_main

UTC = timezone.utc
ROOT = Path(__file__).parents[3]
_GENERATOR = runpy.run_path(str(ROOT / "scripts" / "generate_telemetry_samples.py"))
SOURCES = _GENERATOR["SOURCES"]
generate_samples = _GENERATOR["generate_samples"]


def test_every_generated_adapter_reaches_cluster_judge_drift_and_dashboard(
    tmp_path: Path,
) -> None:
    sample_dir = tmp_path / "samples"
    analysis_time = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    manifest = generate_samples(
        sample_dir,
        as_of=analysis_time,
        per_source_window=5,
    )
    database = tmp_path / "verdict.db"
    storage_url = f"sqlite:///{database}"
    storage = SQLiteStorage(str(database))
    stored = 0
    for source in SOURCES:
        summary = import_into_storage(
            iter_telemetry_file(
                sample_dir / manifest["files"][source],
                file_format=source,
                context=ImportContext(
                    adapter="file",
                    source_scope=f"pipeline-{source}",
                    tenant_id="tenant-e2e",
                ),
            ),
            storage,
        )
        assert summary.skipped == 0
        stored += summary.stored
    rows = storage.list_traces(tenant_id="tenant-e2e", limit=1000)
    storage.close()

    assert stored == manifest["expected"]["traces"] == 80
    assert len(rows) == 80
    assert sum(row.input_tokens or 0 for row in rows) == manifest["expected"]["input_tokens"]
    assert sum(row.output_tokens or 0 for row in rows) == manifest["expected"]["output_tokens"]
    for source, expected_latency in manifest["expected"]["latency_ms_by_source"].items():
        source_rows = [row for row in rows if row.tags["verdict.source"] == source]
        assert len(source_rows) == 10
        assert all(row.latency_ms == pytest.approx(expected_latency) for row in source_rows)
        assert all(row.prompt_redacted == "Where is my order?" for row in source_rows)
        assert all(row.response_redacted == "Your order arrives Friday." for row in source_rows)

    exit_code = pipeline_main(
        [
            "--storage",
            storage_url,
            "--judge-provider",
            "fake",
            "--judge-model",
            "fake-judge",
            "--embedder",
            "deterministic",
            "--clustering-version",
            "telemetry-import-e2e-v1",
            "--cluster-threshold",
            "0.1",
            "--as-of",
            manifest["as_of"],
            "--sampling",
            "stratified",
            "--target-per-cluster",
            "40",
            "--min-sample-size",
            "30",
            "--limit",
            "1000",
        ]
    )

    assert exit_code == 0
    verified = SQLiteStorage(str(database))
    clustered = verified.list_traces(tenant_id="tenant-e2e", limit=1000)
    cluster_ids = {row.cluster_id for row in clustered}
    assert None not in cluster_ids
    assert len(cluster_ids) == 1
    cluster_id = next(iter(cluster_ids))
    judgments = verified.list_judgments_for_cluster(cluster_id, limit=1000)
    assert len(judgments) == 80
    assert all(judgment.status.value == "completed" for judgment in judgments)
    assert all(
        dimension.verdict.value == "pass"
        for judgment in judgments
        for dimension in judgment.dimensions
    )
    snapshot = verified.get_latest_drift_run_snapshot(judgments[0].evaluator_fingerprint)
    assert snapshot is not None
    run, signals = snapshot
    assert run.signal_count == 0
    assert signals == []
    verified.close()

    dashboard = build_bundle(storage_url)
    assert dashboard["meta"]["totalTraces"] == 80
    assert dashboard["meta"]["totalJudged"] == 80
    assert dashboard["driftRun"]["signalCount"] == 0
    assert len(dashboard["samples"]) == 30
    assert all(sample["input_tokens"] is not None for sample in dashboard["samples"])
    assert all(sample["output_tokens"] is not None for sample in dashboard["samples"])
    assert all(sample["latency_ms"] is not None for sample in dashboard["samples"])
