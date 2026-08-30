from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
from verdict.schema import Trace
from verdict.storage.memory import InMemoryStorage
from verdict_eval.clustering_strategies import FitConfig
from verdict_eval.monitoring import create_series_from_history, run_scheduled
from verdict_eval.semantic_monitoring import (
    assign_active_semantic,
    fit_semantic_bootstrap,
    has_active_semantic_registry,
)


class _Embedder:
    dim = 2
    model_name = "test-semantic"
    model_revision = "v1"
    model_file_sha256 = "test"

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float64)


def _trace(index: int, when: datetime) -> Trace:
    prompt = f"Fix the failing test number {index}"
    return Trace(
        trace_id=f"trace-{index}",
        session_id=f"session-{index}",
        started_at=when,
        ended_at=when + timedelta(seconds=1),
        prompt_redacted=prompt,
        response_redacted="Done",
        raw_messages=[{"role": "user", "content": prompt}],
        tags={"verdict.workload": "local-agent", "capture.granularity": "agent-turn"},
    )


def test_semantic_bootstrap_and_scheduled_assignment_share_frozen_registry() -> None:
    storage = InMemoryStorage()
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    traces = [_trace(index, start + timedelta(hours=index)) for index in range(8)]
    for trace in traces:
        storage.insert_trace(trace)

    semantic = fit_semantic_bootstrap(
        storage,
        traces,
        embedder=_Embedder(),
        actor="test",
        config=FitConfig(strategy="semantic", min_cluster_size=2),
    )
    assert has_active_semantic_registry(storage, traces)
    [series] = create_series_from_history(
        storage,
        traces,
        target_units=2,
        state="active",
        assignments=semantic.assignments,
        registry_references=semantic.registry_references,
    )

    assert "cluster-registry-reference-v1" in series.registry_json
    assert set(semantic.assignments) == {trace.trace_id for trace in traces}
    assert all(cluster_id.startswith("clu_") for cluster_id in semantic.assignments.values())

    later = [_trace(index, start + timedelta(hours=index)) for index in range(8, 10)]
    for trace in later:
        storage.insert_trace(trace)
    all_traces = [*traces, *later]
    assigned = assign_active_semantic(storage, all_traces, embedder=_Embedder())
    result = run_scheduled(storage, all_traces, assignments=assigned.assignments)

    assert result["status"] == "updated"
    assert result["closed_cohorts"] == 1
    assert {assigned.assignments[trace.trace_id] for trace in later} == {
        next(iter(semantic.assignments.values()))
    }


def test_active_registry_recovers_before_monitor_series_is_created() -> None:
    storage = InMemoryStorage()
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    traces = [_trace(index, start + timedelta(hours=index)) for index in range(8)]
    for trace in traces:
        storage.insert_trace(trace)

    fitted = fit_semantic_bootstrap(
        storage,
        traces,
        embedder=_Embedder(),
        actor="test",
        config=FitConfig(strategy="semantic", min_cluster_size=2),
    )
    recovered = assign_active_semantic(storage, traces, embedder=_Embedder())

    assert recovered.version_ids == fitted.version_ids
    assert recovered.assignments == fitted.assignments
