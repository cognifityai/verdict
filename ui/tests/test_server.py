import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import verdict.dashboard.app as server_module
from verdict.dashboard.app import (
    _CSP,
    _cluster_health,
    _signal_provider,
    build_bundle,
    create_app,
)
from verdict.schema import (
    DimensionScore,
    DriftDirection,
    DriftRun,
    DriftSignal,
    EvaluatorHealthRecord,
    Judgment,
    Trace,
    Verdict,
)
from verdict.storage import SQLiteStorage


def _persist_drift_snapshot(storage, *signals, run_id=None, analysis_time=None):
    assert signals
    fingerprints = {signal.evaluator_fingerprint for signal in signals}
    assert len(fingerprints) == 1 and "" not in fingerprints
    fingerprint = next(iter(fingerprints))
    run_id = run_id or f"test-run-{fingerprint}-{signals[0].signal_id}"
    analysis_time = analysis_time or max(signal.detected_at for signal in signals)
    for signal in signals:
        signal.run_id = run_id
    storage.replace_drift_run(
        DriftRun(
            run_id=run_id,
            analysis_time=analysis_time,
            completed_at=analysis_time + timedelta(seconds=1),
            evaluator_fingerprint=fingerprint,
            signal_count=len(signals),
        ),
        list(signals),
    )


def test_signal_provider_resolves_demo_alias():
    assert _signal_provider("haiku", {"anthropic"}, {}) == "anthropic"


def test_signal_provider_rejects_demo_alias_when_provider_is_absent():
    assert _signal_provider("haiku", {"openai"}, {}) is None


def test_signal_provider_prefers_factual_cluster_provider_over_demo_alias():
    clusters = {"haiku": {"openai"}}
    assert _signal_provider("haiku", {"anthropic", "openai"}, clusters) == "openai"


def test_signal_provider_accepts_direct_provider_key():
    assert _signal_provider("anthropic", {"anthropic", "openai"}, {}) == "anthropic"


def test_signal_provider_accepts_single_provider_cluster():
    clusters = {"billing": {"openai"}}
    assert _signal_provider("billing", {"anthropic", "openai"}, clusters) == "openai"


def test_signal_provider_rejects_mixed_provider_cluster():
    clusters = {"billing": {"anthropic", "openai"}}
    assert _signal_provider("billing", {"anthropic", "openai"}, clusters) is None


def test_cluster_health_exposes_fragmentation_to_dashboard():
    health = _cluster_health([f"c{i}" for i in range(8)])

    assert health["status"] == "fragmented"
    assert health["medianClusterSize"] == 1
    assert health["messages"]


def test_cluster_health_distinguishes_low_volume_from_fragmentation():
    health = _cluster_health(["a"] * 20 + ["b"] * 20)

    assert health["status"] == "underpowered"
    assert health["clustersMeetingSampleFloor"] == 0


def test_content_security_policy_allows_only_local_scripts():
    assert "script-src 'self'" in _CSP
    assert "'unsafe-eval'" not in _CSP
    assert "cdn.tailwindcss.com" not in _CSP
    assert "cdnjs.cloudflare.com" not in _CSP
    assert "unpkg.com" not in _CSP


def test_bundle_marks_unknown_cost_and_exposes_signal_examples(tmp_path):
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="anthropic",
        request_model="unknown-model",
        cluster_id="support",
        prompt_redacted="help",
        response_redacted="response",
        cost_usd=None,
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_config={"temperature": 0},
        evaluator_fingerprint="cost-test-evaluator",
        expected_dimensions=["relevance"],
        judge_models=["judge-model"],
        dimensions=[DimensionScore(name="relevance", verdict=Verdict.FAIL)],
    ))
    _persist_drift_snapshot(storage, DriftSignal(
        cluster_id="support",
        dimension="relevance",
        evaluator_fingerprint="cost-test-evaluator",
        example_trace_ids=[trace.trace_id],
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["meta"]["totalCost"] is None
    assert bundle["meta"]["totalCostStatus"] == "unavailable"
    assert bundle["driftSignals"][0]["exampleTraceIds"] == [trace.trace_id]


def test_dashboard_api_preserves_captured_empty_content(tmp_path):
    import httpx

    path = tmp_path / "empty-content.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(
        trace_id="captured-empty",
        provider="anthropic",
        request_model="claude-test",
        prompt_redacted="",
        response_redacted="",
    ))
    storage.close()

    [sample] = build_bundle(path)["samples"]
    assert sample["prompt_redacted"] == ""
    assert sample["response_redacted"] == ""

    async def request_data():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}"),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/api/data")

    response = asyncio.run(request_data())
    assert response.status_code == 200
    [api_sample] = response.json()["samples"]
    assert api_sample["prompt_redacted"] == ""
    assert api_sample["response_redacted"] == ""


def test_dashboard_api_paginates_application_traces_with_deterministic_ties(tmp_path):
    import httpx

    path = tmp_path / "trace-pages.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for index in range(65):
        storage.insert_trace(Trace(
            trace_id=f"trace-{index:03d}",
            started_at=started_at,
            prompt_redacted=f"prompt {index}",
            response_redacted=f"response {index}",
        ))
    storage.insert_trace(Trace(
        trace_id="trace-judge-newest",
        started_at=started_at + timedelta(days=1),
        tags={"verdict.workload": "judge"},
    ))
    storage.close()

    async def request_pages():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{path}"),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return (
                await client.get("/api/data"),
                await client.get("/api/data?trace_offset=30"),
                await client.get("/api/data?trace_offset=60"),
                await client.get("/api/data?trace_offset=-1"),
            )

    first, second, third, invalid = asyncio.run(request_pages())

    assert first.status_code == second.status_code == third.status_code == 200
    assert invalid.status_code == 422
    assert [sample["trace_id"] for sample in first.json()["samples"]] == [
        f"trace-{index:03d}" for index in range(64, 34, -1)
    ]
    assert [sample["trace_id"] for sample in second.json()["samples"]] == [
        f"trace-{index:03d}" for index in range(34, 4, -1)
    ]
    assert [sample["trace_id"] for sample in third.json()["samples"]] == [
        f"trace-{index:03d}" for index in range(4, -1, -1)
    ]
    assert all(
        page.json()["truncation"]["resources"]["traceSamples"]["available"] == 65
        for page in (first, second, third)
    )
    for field in (
        "meta",
        "providers",
        "clusters",
        "dimensionOverall",
        "driftSignals",
        "driftAnalysis",
        "scoreCoverage",
    ):
        assert first.json()[field] == second.json()[field] == third.json()[field]
    assert "trace-judge-newest" not in {
        sample["trace_id"]
        for page in (first, second, third)
        for sample in page.json()["samples"]
    }
    with pytest.raises(ValueError, match="non-negative integer"):
        build_bundle(path, trace_offset=0.5)


def test_bundle_keeps_agent_judge_and_unclassified_cost_provenance_separate(tmp_path):
    path = tmp_path / "cost-provenance.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(
        trace_id="agent-priced",
        tags={"verdict.workload": "agent"},
        cost_usd=0.10,
    ))
    storage.insert_trace(Trace(
        trace_id="judge-priced",
        tags={"verdict.workload": "judge"},
        cost_usd=0.20,
    ))
    storage.insert_trace(Trace(
        trace_id="unknown-unpriced",
        tags={"verdict.workload": "customer-specialist"},
        cost_usd=None,
    ))
    storage.insert_trace(Trace(
        trace_id="legacy-priced",
        cost_usd=0.30,
    ))
    storage.close()

    breakdown = build_bundle(path)["meta"]["costBreakdown"]

    assert breakdown["agent"] == {
        "traces": 1,
        "pricedTraces": 1,
        "cost": 0.1,
        "status": "complete",
    }
    assert breakdown["judge"] == {
        "traces": 1,
        "pricedTraces": 1,
        "cost": 0.2,
        "status": "complete",
    }
    assert breakdown["unclassified"] == {
        "traces": 2,
        "pricedTraces": 1,
        "cost": 0.3,
        "status": "partial",
    }


def test_installed_dashboard_accepts_a_sqlite_storage_url(tmp_path):
    path = tmp_path / "installed-dashboard.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(provider="custom-provider", request_model="custom-model"))
    storage.close()

    bundle = build_bundle(f"sqlite:///{path}")

    assert bundle["meta"]["totalTraces"] == 1
    assert bundle["providers"][0]["rawProvider"] == "custom-provider"


def test_trace_explorer_includes_metadata_only_capture(tmp_path):
    path = tmp_path / "metadata-only.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(
        trace_id="metadata-only-trace",
        provider="openai",
        request_model="gpt-test",
        input_tokens=12,
        output_tokens=8,
        prompt_redacted=None,
        response_redacted=None,
    ))
    storage.close()

    bundle = build_bundle(path)

    assert [sample["trace_id"] for sample in bundle["samples"]] == [
        "metadata-only-trace"
    ]
    assert bundle["samples"][0]["prompt_redacted"] is None
    assert bundle["truncation"]["resources"]["traceSamples"]["available"] == 1


def test_trace_explorer_excludes_judge_only_store_without_hiding_totals(tmp_path):
    path = tmp_path / "judge-only.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(
        trace_id="judge-trace",
        tags={"verdict.workload": "judge"},
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["samples"] == []
    assert bundle["meta"]["totalTraces"] == 1
    assert bundle["meta"]["costBreakdown"]["judge"]["traces"] == 1
    assert bundle["truncation"]["resources"]["traceSamples"] == {
        "available": 0,
        "shown": 0,
        "limit": 30,
    }


def test_trace_explorer_prioritizes_newest_traces_with_deterministic_ties(tmp_path):
    path = tmp_path / "newest-traces.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
    for index in range(31):
        storage.insert_trace(Trace(
            trace_id=f"trace-{index:02d}",
            started_at=started_at + timedelta(hours=index),
            prompt_redacted=None if index < 15 else f"captured prompt {index}",
            response_redacted=None if index < 15 else f"captured response {index}",
            error="historical failure" if index == 0 else None,
        ))
    for trace_id, workload in (
        ("tie-a", "agent"),
        ("tie-z", "customer-specialist"),
    ):
        storage.insert_trace(Trace(
            trace_id=trace_id,
            started_at=started_at + timedelta(hours=31),
            prompt_redacted="newest captured prompt",
            response_redacted="newest captured response",
            tags={"verdict.workload": workload},
        ))
    for index in range(30):
        storage.insert_trace(Trace(
            trace_id=f"judge-{index:02d}",
            started_at=started_at + timedelta(hours=100 + index),
            tags={"verdict.workload": "judge"},
        ))
    storage.close()

    bundle = build_bundle(path)
    sample_ids = [sample["trace_id"] for sample in bundle["samples"]]

    assert sample_ids[:3] == ["tie-z", "tie-a", "trace-30"]
    assert sample_ids[-1] == "trace-03"
    assert "trace-00" not in sample_ids
    assert bundle["samples"][0]["contentCaptured"] is True
    assert bundle["samples"][-1]["contentCaptured"] is False
    assert bundle["meta"]["totalTraces"] == 63
    assert bundle["meta"]["costBreakdown"]["judge"]["traces"] == 30
    assert bundle["truncation"]["resources"]["traceSamples"] == {
        "available": 33,
        "shown": 30,
        "limit": 30,
    }


def test_dashboard_reports_global_content_availability_without_claiming_readiness(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "content-readiness.db"
    storage = SQLiteStorage(str(path))
    analysis_time = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        server_module,
        "_now_utc",
        lambda: analysis_time,
        raising=False,
    )
    for index in range(29):
        storage.insert_trace(Trace(
            trace_id=f"current-{index:02d}",
            started_at=analysis_time - timedelta(hours=1),
            prompt_redacted="current prompt",
            response_redacted="current response",
            tags={"verdict.workload": "agent"},
        ))
    for index in range(30):
        storage.insert_trace(Trace(
            trace_id=f"baseline-{index:02d}",
            started_at=analysis_time - timedelta(days=2),
            prompt_redacted="baseline prompt",
            response_redacted="baseline response",
            tags={"verdict.workload": "agent"},
        ))
    storage.insert_trace(Trace(
        trace_id="current-metadata-only",
        started_at=analysis_time - timedelta(hours=1),
        tags={"verdict.workload": "agent"},
    ))
    storage.insert_trace(Trace(
        trace_id="current-failed-with-content",
        started_at=analysis_time - timedelta(hours=1),
        prompt_redacted="captured prompt",
        response_redacted="partial response",
        error="provider failed",
        tags={"verdict.workload": "agent"},
    ))
    storage.insert_trace(Trace(
        trace_id="current-empty-error",
        started_at=analysis_time - timedelta(hours=1),
        prompt_redacted="captured prompt",
        response_redacted="captured response",
        error="",
        tags={"verdict.workload": "agent"},
    ))
    python_whitespace = "".join(
        chr(codepoint)
        for codepoint in range(0x110000)
        if chr(codepoint).isspace()
    )
    for index, whitespace in enumerate(
        ("\t", "\n", "\N{NO-BREAK SPACE}", python_whitespace)
    ):
        storage.insert_trace(Trace(
            trace_id=f"current-whitespace-only-{index}",
            started_at=analysis_time - timedelta(hours=1),
            prompt_redacted=whitespace,
            response_redacted="captured response",
            tags={"verdict.workload": "agent"},
        ))
    storage.insert_trace(Trace(
        trace_id="current-judge-workload",
        started_at=analysis_time - timedelta(hours=1),
        prompt_redacted="judge prompt",
        response_redacted="judge response",
        tags={"verdict.workload": "judge"},
    ))
    storage.insert_trace(Trace(
        trace_id="outside-window",
        started_at=analysis_time - timedelta(days=9),
        prompt_redacted="old prompt",
        response_redacted="old response",
        tags={"verdict.workload": "agent"},
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["meta"]["workload"] == "agent"
    assert bundle["driftAnalysis"] == {
        "runStatus": "no_completed_run",
        "readinessStatus": "global_minimum_met",
        "current": 30,
        "baseline": 30,
        "minimum": 30,
        "currentHours": 24,
        "baselineLagHours": 24,
        "baselineDays": 7,
    }


def test_dashboard_uses_neutral_identity_for_mixed_or_secret_shaped_workloads(
    tmp_path,
):
    path = tmp_path / "neutral-workload.db"
    storage = SQLiteStorage(str(path))
    for trace_id, workload in (
        ("safe", "agent"),
        ("secret-shaped", "123-45-6789"),
    ):
        storage.insert_trace(Trace(
            trace_id=trace_id,
            tags={"verdict.workload": workload},
        ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["meta"]["workload"] is None
    assert "123-45-6789" not in str(bundle)


def test_dashboard_distinguishes_completed_zero_signal_and_signaling_runs(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "drift-run-states.db"
    storage = SQLiteStorage(str(path))
    analysis_time = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        server_module,
        "_now_utc",
        lambda: analysis_time,
        raising=False,
    )
    trace = Trace(
        trace_id="evaluated-trace",
        started_at=analysis_time - timedelta(hours=1),
        prompt_redacted="safe prompt",
        response_redacted="safe response",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="run-state-evaluator",
        expected_dimensions=["quality"],
        judge_models=["judge-model"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    zero_run = DriftRun(
        run_id="completed-zero",
        analysis_time=analysis_time,
        completed_at=analysis_time + timedelta(seconds=1),
        evaluator_fingerprint="run-state-evaluator",
        signal_count=0,
    )
    storage.replace_drift_run(zero_run, [])

    zero_bundle = build_bundle(path)
    assert zero_bundle["driftAnalysis"]["runStatus"] == "completed_no_signals"

    signal = DriftSignal(
        signal_id="completed-signal",
        run_id="completed-with-signal",
        detected_at=analysis_time + timedelta(hours=1),
        cluster_id="support",
        dimension="quality",
        evaluator_fingerprint="run-state-evaluator",
    )
    signal_run = DriftRun(
        run_id=signal.run_id,
        analysis_time=signal.detected_at,
        completed_at=signal.detected_at + timedelta(seconds=1),
        evaluator_fingerprint="run-state-evaluator",
        signal_count=1,
    )
    storage.replace_drift_run(signal_run, [signal])
    storage.close()

    signal_bundle = build_bundle(path)
    assert signal_bundle["driftAnalysis"]["runStatus"] == "completed_with_signals"


def test_dashboard_app_can_be_mounted_below_a_host_application(tmp_path):
    import httpx
    from fastapi import FastAPI

    path = tmp_path / "mounted-dashboard.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(provider="openai", request_model="mounted-model"))
    storage.close()

    host = FastAPI()
    host.mount(
        "/admin/verdict",
        create_app(storage=f"sqlite:///{path}"),
    )

    async def request_data():
        transport = httpx.ASGITransport(app=host)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/admin/verdict/api/data")

    response = asyncio.run(request_data())

    assert response.status_code == 200
    assert response.json()["meta"]["totalTraces"] == 1


def test_mounted_dashboard_exposes_only_a_same_origin_operations_path(tmp_path):
    import httpx
    from fastapi import FastAPI

    path = tmp_path / "mounted-operations.db"
    SQLiteStorage(str(path)).close()
    host = FastAPI()
    host.mount(
        "/admin/verdict",
        create_app(
            storage=f"sqlite:///{path}",
            operations_url="/api/admin/operations",
        ),
    )

    async def request_config():
        transport = httpx.ASGITransport(app=host)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/admin/verdict/api/config")

    response = asyncio.run(request_config())

    assert response.status_code == 200
    assert response.json() == {"operationsUrl": "/api/admin/operations"}


def test_dashboard_rejects_cross_origin_operations_urls(tmp_path):
    path = tmp_path / "unsafe-operations.db"
    SQLiteStorage(str(path)).close()

    with pytest.raises(ValueError, match="same-origin absolute path"):
        create_app(
            storage=f"sqlite:///{path}",
            operations_url="https://attacker.example/collect",
        )


def test_large_store_api_returns_a_bounded_truthful_bundle(monkeypatch, tmp_path):
    import httpx

    path = tmp_path / "large-dashboard.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    providers = ("anthropic", "openai", "google")
    for index in range(1000):
        storage.insert_trace(Trace(
            trace_id=f"large-trace-{index:04d}",
            started_at=started_at + timedelta(minutes=30 * index),
            provider=providers[index % len(providers)],
            request_model="known-model",
            cluster_id=f"cluster-{index % 20:02d}",
            prompt_redacted="safe prompt",
        ))
    storage.close()
    monkeypatch.setenv("VERDICT_DB", str(path))

    async def request_data():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/api/data")

    response = asyncio.run(request_data())
    payload = response.json()

    assert response.status_code == 200
    assert payload["meta"]["totalTraces"] == 1000
    assert payload["truncation"]["applied"] is True
    assert payload["truncation"]["resources"]["latencyPoints"] == {
        "available": 1000,
        "shown": 100,
        "limit": 100,
    }
    assert len(payload["tsRows"]) == 100
    assert len(json.dumps(payload).encode("utf-8")) < 500_000


def test_redaction_budget_failure_returns_an_explicit_service_error(
    monkeypatch,
    tmp_path,
):
    import httpx

    path = tmp_path / "budget-error.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(prompt_redacted="safe prompt"))
    storage.close()
    monkeypatch.setenv("VERDICT_DB", str(path))
    monkeypatch.setattr(server_module, "redact_structure", lambda _bundle: "<REDACTED>")

    async def request_data():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/api/data")

    response = asyncio.run(request_data())

    assert response.status_code == 503
    assert response.json() == {"error": "dashboard bundle limit exceeded"}


def test_bundle_reports_every_high_cardinality_presentation_axis(tmp_path):
    path = tmp_path / "high-cardinality-dashboard.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    dimensions = [f"dimension-{index:02d}" for index in range(15)]
    traces = []
    for index in range(100):
        trace = Trace(
            trace_id=f"cardinality-trace-{index:03d}",
            started_at=started_at + timedelta(minutes=30 * index),
            provider="anthropic" if index < 25 else f"custom-provider-{index:03d}",
            request_model=f"model-{index:03d}",
            cluster_id=f"cluster-{index:03d}",
            prompt_redacted="safe prompt",
        )
        traces.append(trace)
        storage.insert_trace(trace)

    storage.insert_judgment(Judgment(
        trace_id=traces[0].trace_id,
        evaluator_provider="fake",
        evaluator_config={"temperature": 0},
        evaluator_fingerprint="target-fingerprint",
        expected_dimensions=dimensions,
        judge_models=["a-target-judge"],
        dimensions=[
            DimensionScore(name=dimension, verdict=Verdict.PASS)
            for dimension in dimensions
        ],
    ))
    for index in range(1, 25):
        storage.insert_judgment(Judgment(
            trace_id=traces[index].trace_id,
            evaluator_provider="fake",
            evaluator_config={"temperature": index},
            evaluator_fingerprint=f"other-fingerprint-{index:02d}",
            expected_dimensions=["quality"],
            judge_models=[f"z-other-judge-{index:02d}"],
            dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
        ))
    _persist_drift_snapshot(storage, *[
        DriftSignal(
            signal_id=f"cardinality-signal-{index:03d}",
            cluster_id=f"cluster-{index:03d}",
            dimension=dimensions[index % len(dimensions)],
            evaluator_fingerprint="target-fingerprint",
        )
        for index in range(60)
    ])
    storage.close()

    unselected = build_bundle(path)
    target_identity = next(
        identity
        for identity in unselected["evaluation"]["availableIdentities"]
        if identity["fingerprint"] == "target-fingerprint"
    )
    bundle = build_bundle(path, evaluator_id=target_identity["id"])
    resources = bundle["truncation"]["resources"]

    assert bundle["truncation"]["applied"] is True
    assert resources["providers"] == {"available": 76, "shown": 8, "limit": 8}
    assert resources["providerModels"] == {"available": 25, "shown": 20, "limit": 20}
    assert resources["clusters"] == {"available": 100, "shown": 20, "limit": 20}
    assert resources["dimensions"] == {"available": 15, "shown": 12, "limit": 12}
    assert resources["evaluatorIdentities"] == {
        "available": 25,
        "shown": 20,
        "limit": 20,
    }
    assert resources["driftSignals"] == {"available": 60, "shown": 40, "limit": 40}
    assert resources["traceSamples"] == {"available": 100, "shown": 30, "limit": 30}
    assert bundle["meta"]["providers"] == 76
    assert bundle["meta"]["clusters"] == 100


def test_cluster_cap_excludes_unclustered_from_both_count_and_payload(tmp_path):
    path = tmp_path / "cluster-cap-boundary.db"
    storage = SQLiteStorage(str(path))
    expected_clusters = {f"cluster-{index:02d}" for index in range(20)}
    for cluster_id in sorted(expected_clusters):
        storage.insert_trace(Trace(cluster_id=cluster_id))
    # Make the non-intent bucket large enough to displace a real cluster under
    # the old ORDER BY ... LIMIT query.
    storage.insert_trace(Trace(cluster_id="unclustered"))
    storage.insert_trace(Trace(cluster_id="unclustered"))
    storage.close()

    bundle = build_bundle(path)
    shown_clusters = {row["cluster_id"] for row in bundle["clusters"]}

    assert shown_clusters == expected_clusters
    assert bundle["truncation"]["resources"]["clusters"] == {
        "available": 20,
        "shown": 20,
        "limit": 20,
    }
    assert bundle["truncation"]["applied"] is False


def _bundle_with_drift_effects(tmp_path, effects):
    path = tmp_path / "drift-priority.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(cluster_id="support")
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="drift-priority-evaluator",
        expected_dimensions=["quality"],
        judge_models=["judge-model"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    signals = []
    for index, (direction, effect) in enumerate(effects):
        signals.append(DriftSignal(
            signal_id=f"priority-signal-{index:03d}",
            cluster_id="support",
            dimension="quality",
            direction=direction,
            evaluator_fingerprint="drift-priority-evaluator",
            effect_size_cliffs_delta=effect,
        ))
    _persist_drift_snapshot(storage, *signals)
    storage.close()
    return build_bundle(path)


def test_drift_cap_keeps_strongest_improvements(tmp_path):
    effects = [
        (DriftDirection.IMPROVEMENT, index / 100)
        for index in range(1, 61)
    ]

    bundle = _bundle_with_drift_effects(tmp_path, effects)
    shown = [signal["cliffsDelta"] for signal in bundle["driftSignals"]]

    assert len(shown) == 40
    assert shown == [index / 100 for index in range(60, 20, -1)]


def test_drift_cap_keeps_strongest_regressions(tmp_path):
    effects = [
        (DriftDirection.REGRESSION, -(index / 100))
        for index in range(1, 61)
    ]

    bundle = _bundle_with_drift_effects(tmp_path, effects)
    shown = [signal["cliffsDelta"] for signal in bundle["driftSignals"]]

    assert shown == [-(index / 100) for index in range(60, 20, -1)]


def test_drift_cap_uses_effect_magnitude_for_mixed_directions(tmp_path):
    effects = [
        (DriftDirection.REGRESSION, -(index / 100))
        for index in range(1, 22)
    ] + [
        (DriftDirection.IMPROVEMENT, index / 100)
        for index in range(1, 22)
    ]

    bundle = _bundle_with_drift_effects(tmp_path, effects)
    shown = [signal["cliffsDelta"] for signal in bundle["driftSignals"]]

    assert len(shown) == 40
    assert all(abs(effect) >= 0.02 for effect in shown)
    assert {-0.21, 0.21}.issubset(shown)


def test_fragmented_single_provider_store_has_bounded_cluster_matrix(tmp_path):
    import time

    path = tmp_path / "fragmented-dashboard.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for index in range(3000):
        trace = Trace(
            trace_id=f"fragmented-trace-{index:04d}",
            started_at=started_at + timedelta(hours=3 * index),
            provider="openai",
            request_model="known-model",
            cluster_id=f"fragmented-cluster-{index:04d}",
            prompt_redacted="safe prompt",
        )
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            evaluator_provider="fake",
            evaluator_config={"temperature": 0},
            evaluator_fingerprint="fragmented-evaluator",
            expected_dimensions=["quality"],
            judge_models=["judge-model"],
            dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
        ))
    storage.close()

    start = time.perf_counter()
    bundle = build_bundle(path)
    elapsed = time.perf_counter() - start

    assert bundle["meta"]["clusters"] == 3000
    assert bundle["truncation"]["resources"]["clusters"] == {
        "available": 3000,
        "shown": 20,
        "limit": 20,
    }
    assert len(bundle["clusters"]) == 20
    assert len(bundle["clusterPassrate"]) == 100
    assert all(len(row) <= 21 for row in bundle["clusterPassrate"])
    assert len(json.dumps(bundle).encode("utf-8")) < 500_000
    assert elapsed < 5.0, f"bounded dashboard bundle took {elapsed:.2f}s"


def test_composite_presentation_limits_stay_below_redaction_budget(tmp_path):
    path = tmp_path / "composite-dashboard.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    dimensions = [f"dimension-{index:02d}" for index in range(12)]
    scores = [
        DimensionScore(name=dimension, verdict=Verdict.PASS)
        for dimension in dimensions
    ]
    for index in range(800):
        trace = Trace(
            trace_id=f"composite-trace-{index:04d}",
            started_at=started_at + timedelta(minutes=7.5 * index),
            provider=f"provider-{index % 8}",
            request_model="known-model",
            cluster_id=f"cluster-{index % 20:02d}",
            prompt_redacted="safe prompt",
            response_redacted="safe response",
        )
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            evaluator_provider="fake",
            evaluator_config={"temperature": 0},
            evaluator_fingerprint="composite-evaluator",
            expected_dimensions=dimensions,
            judge_models=["judge-model"],
            dimensions=scores,
        ))
    _persist_drift_snapshot(storage, *[
        DriftSignal(
            signal_id=f"composite-signal-{index:03d}",
            cluster_id=f"cluster-{index % 20:02d}",
            dimension=dimensions[index % len(dimensions)],
            evaluator_fingerprint="composite-evaluator",
        )
        for index in range(40)
    ])
    storage.close()

    bundle = build_bundle(path)

    def count_nodes(value):
        if isinstance(value, dict):
            return 1 + sum(count_nodes(child) for child in value.values())
        if isinstance(value, list):
            return 1 + sum(count_nodes(child) for child in value)
        return 1

    assert len(bundle["providers"]) == 8
    assert len(bundle["dimensionOverall"]) == 12
    assert len(bundle["tsRows"]) == 100
    assert len(bundle["passrate"]) == 100
    assert len(bundle["haikuDim"]) == 100
    assert len(bundle["driftSignals"]) == 40
    assert len(bundle["samples"]) == 30
    assert count_nodes(bundle) < 9000


def test_bundle_filters_drift_by_selected_evaluator_and_excludes_historical_rows(tmp_path):
    path = tmp_path / "mixed-drift-evaluators.db"
    storage = SQLiteStorage(str(path))
    for suffix, fingerprint, verdict in (
        ("a", "fingerprint-a", Verdict.PASS),
        ("b", "fingerprint-b", Verdict.FAIL),
    ):
        trace = Trace(
            trace_id=f"trace-{suffix}",
            started_at=datetime(2026, 8, 15, 10, tzinfo=timezone.utc),
            provider="openai",
            cluster_id="support",
            prompt_redacted="Prompt",
            response_redacted="Response",
        )
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            evaluator_provider="fake",
            evaluator_config={"temperature": 0},
            evaluator_fingerprint=fingerprint,
            expected_dimensions=["quality"],
            judge_models=[f"judge-{suffix}"],
            dimensions=[DimensionScore(name="quality", verdict=verdict)],
        ))
        _persist_drift_snapshot(storage, DriftSignal(
            signal_id=f"signal-{suffix}",
            cluster_id="support",
            dimension=f"quality-{suffix}",
            evaluator_fingerprint=fingerprint,
        ))
    storage.insert_drift_signal(DriftSignal(
        signal_id="historical-signal",
        cluster_id="support",
        dimension="historical",
    ))
    assert storage.prune_before("2026-08-16T00:00:00+00:00") == 2
    storage.insert_trace(Trace(
        trace_id="trace-after-multi-retention",
        started_at=datetime(2026, 8, 16, 13, tzinfo=timezone.utc),
        prompt_redacted="New prompt",
        response_redacted="New response",
    ))
    storage.close()

    ambiguous = build_bundle(path)
    assert ambiguous["evaluation"]["status"] == "selection_required"
    assert ambiguous["evaluation"]["driftStatus"] == "selection_required"
    assert ambiguous["driftAnalysis"]["runStatus"] == "selection_required"
    assert ambiguous["driftSignals"] == []
    assert all(
        identity["complete"] is False
        for identity in ambiguous["evaluation"]["availableIdentities"]
    )

    evaluator_a = next(
        identity for identity in ambiguous["evaluation"]["availableIdentities"]
        if identity["fingerprint"] == "fingerprint-a"
    )
    selected = build_bundle(path, evaluator_id=evaluator_a["id"])
    assert [signal["id"] for signal in selected["driftSignals"]] == ["signal-a"]
    assert selected["evaluation"]["driftStatus"] == "selected"
    assert selected["evaluation"]["unattributedDriftSignals"] == 1


def test_bundle_uses_latest_completed_drift_run_even_when_it_has_zero_signals(tmp_path):
    path = tmp_path / "latest-zero-drift.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        trace_id="trace-latest-run",
        started_at=datetime(2026, 8, 15, 10, tzinfo=timezone.utc),
        provider="openai",
        cluster_id="support",
        prompt_redacted="Prompt",
        response_redacted="Response",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_config={"temperature": 0},
        evaluator_fingerprint="latest-run-evaluator",
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    historical_signal = DriftSignal(
        signal_id="historical-drift",
        detected_at=datetime(2026, 8, 15, 12, tzinfo=timezone.utc),
        cluster_id="support",
        dimension="quality",
        evaluator_fingerprint="latest-run-evaluator",
    )
    _persist_drift_snapshot(
        storage,
        historical_signal,
        run_id="historical-run",
        analysis_time=historical_signal.detected_at,
    )
    latest_time = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
    storage.replace_drift_run(
        DriftRun(
            run_id="latest-zero-run",
            analysis_time=latest_time,
            completed_at=latest_time + timedelta(minutes=1),
            evaluator_fingerprint="latest-run-evaluator",
            signal_count=0,
        ),
        [],
    )
    assert storage.prune_before("2026-08-16T00:00:00+00:00") == 1
    storage.insert_trace(Trace(
        trace_id="trace-after-retention",
        started_at=datetime(2026, 8, 16, 13, tzinfo=timezone.utc),
        prompt_redacted="New prompt",
        response_redacted="New response",
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["driftSignals"] == []
    assert bundle["driftRun"]["id"] == "latest-zero-run"
    assert bundle["driftRun"]["signalCount"] == 0
    assert bundle["evaluation"]["driftStatus"] == "selected"
    assert bundle["evaluation"]["selectedIdentity"]["complete"] is False
    assert bundle["evaluation"]["selectedIdentity"]["fingerprint"] == (
        "latest-run-evaluator"
    )


def test_bundle_builds_independent_cluster_pass_rate_series(tmp_path):
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for hour, cluster, verdict in [
        (0, "refund", Verdict.PASS),
        (0, "shipping", Verdict.PASS),
        (1, "refund", Verdict.FAIL),
        (1, "shipping", Verdict.PASS),
    ]:
        trace = Trace(
            started_at=started + timedelta(hours=hour),
            provider="openai",
            request_model="gpt-4o-mini",
            cluster_id=cluster,
        )
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            dimensions=[DimensionScore(name="quality", verdict=verdict)],
        ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["clusterPassrate"] == [
        {"hour": 0, "refund": 100.0, "shipping": 100.0},
        {"hour": 1, "refund": 0.0, "shipping": 100.0},
    ]


def test_bundle_requires_one_evaluator_identity_instead_of_mixing_rows(tmp_path):
    """P0-2: incompatible judge/rubric rows must not be pooled or last-row-wins."""
    path = tmp_path / "mixed-evaluators.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="openai",
        request_model="gpt-4o-mini",
        cluster_id="refund",
        prompt_redacted="Can I get a refund?",
        response_redacted="Yes.",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        judgment_id="old-evaluator",
        trace_id=trace.trace_id,
        rubric_name="support-quality",
        rubric_version="1",
        judge_models=["judge-model-a"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    storage.insert_judgment(Judgment(
        judgment_id="new-evaluator",
        trace_id=trace.trace_id,
        rubric_name="support-quality",
        rubric_version="2",
        judge_models=["judge-model-b"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.UNCLEAR)],
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["evaluation"]["status"] == "selection_required"
    assert len(bundle["evaluation"]["availableIdentities"]) == 2
    assert bundle["dimensionOverall"] == []
    assert "judgment" not in bundle["samples"][0]

    old_identity = next(
        identity for identity in bundle["evaluation"]["availableIdentities"]
        if identity["models"] == ["judge-model-a"]
    )
    selected = build_bundle(path, evaluator_id=old_identity["id"])
    assert selected["evaluation"]["status"] == "selected"
    assert selected["dimensionOverall"][0]["passRate"] == 100.0
    assert selected["samples"][0]["judgment"]["judges"] == ["judge-model-a"]


def test_bundle_exposes_only_selected_evaluator_sentinel_health(tmp_path):
    path = tmp_path / "judge-health.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="openai",
        cluster_id="refund",
        prompt_redacted="Prompt",
        response_redacted="Response",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_config={"temperature": 0},
        evaluator_fingerprint="selected-fingerprint",
        expected_dimensions=["quality"],
        judge_models=["judge-model"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    storage.insert_evaluator_health(EvaluatorHealthRecord(
        evaluator_fingerprint="selected-fingerprint",
        sentinel_set_name="support-v1",
        sentinel_set_fingerprint="set-one",
        correct_examples=27,
        total_examples=30,
        example_agreement=0.9,
        example_confidence_low=0.74,
        example_confidence_high=0.97,
        correct_labels=27,
        total_labels=30,
        label_agreement=0.9,
        status="healthy",
    ))
    storage.insert_evaluator_health(EvaluatorHealthRecord(
        evaluator_fingerprint="other-fingerprint",
        sentinel_set_name="other-v1",
        sentinel_set_fingerprint="set-two",
        correct_examples=0,
        total_examples=30,
        example_agreement=0,
        example_confidence_low=0,
        example_confidence_high=0.11,
        correct_labels=0,
        total_labels=30,
        label_agreement=0,
        status="degraded",
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["evaluation"]["selectedIdentity"]["fingerprint"] == (
        "selected-fingerprint"
    )
    assert bundle["evaluatorHealth"] == [{
        "id": bundle["evaluatorHealth"][0]["id"],
        "evaluatedAt": bundle["evaluatorHealth"][0]["evaluatedAt"],
        "sentinelSetName": "support-v1",
        "sentinelSetFingerprint": "set-one",
        "correctExamples": 27,
        "totalExamples": 30,
        "exampleAgreement": 90.0,
        "exampleConfidenceLow": 74.0,
        "exampleConfidenceHigh": 97.0,
        "correctLabels": 27,
        "totalLabels": 30,
        "labelAgreement": 90.0,
        "status": "healthy",
        "errorCount": 0,
        "methodVersion": "2",
    }]


def test_bundle_excludes_unclear_from_every_pass_rate_denominator(tmp_path):
    """P0-3: PASS / (PASS + FAIL); UNCLEAR is coverage, not failure."""
    path = tmp_path / "unclear-denominators.db"
    storage = SQLiteStorage(str(path))
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, verdict in enumerate((Verdict.PASS, Verdict.FAIL, Verdict.UNCLEAR)):
        trace = Trace(
            trace_id=f"trace-{index}",
            started_at=started,
            provider="openai",
            request_model="gpt-4o-mini",
            cluster_id="refund",
            prompt_redacted=f"Prompt {index}",
            response_redacted=f"Response {index}",
        )
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            rubric_name="quality",
            rubric_version="1",
            judge_models=["judge-model"],
            dimensions=[
                DimensionScore(name="quality", verdict=verdict),
                DimensionScore(name="unclear_only", verdict=Verdict.UNCLEAR),
            ],
        ))
    storage.close()

    bundle = build_bundle(path)
    by_dimension = {row["dim"]: row for row in bundle["dimensionOverall"]}

    assert by_dimension["quality"] == {
        "dim": "quality",
        "passRate": 50.0,
        "pass": 1,
        "fail": 1,
        "unclear": 1,
        "tot": 3,
    }
    assert by_dimension["unclear_only"]["passRate"] is None
    assert bundle["providers"][0]["passRate"] == 50.0
    assert bundle["passrate"] == [{"hour": 0, "openai": 50.0}]
    assert bundle["clusterPassrate"] == [{"hour": 0, "refund": 50.0}]
    assert bundle["scoreCoverage"] == {
        "pass": 1, "fail": 1, "unclear": 4,
        "missing": 0, "error": 0, "evaluable": 2,
    }
    sample_by_id = {sample["trace_id"]: sample for sample in bundle["samples"]}
    assert sample_by_id["trace-1"]["judgment"]["summary"]["status"] == "fail"
    assert sample_by_id["trace-2"]["judgment"]["summary"]["status"] == "unclear"


def test_bundle_uses_latest_duplicate_independent_of_insertion_order(tmp_path):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    outputs = []
    for filename, order in (("forward.db", ("old", "new")), ("reverse.db", ("new", "old"))):
        path = tmp_path / filename
        storage = SQLiteStorage(str(path))
        trace = Trace(
            trace_id="trace-1",
            started_at=created,
            provider="openai",
            cluster_id="refund",
            prompt_redacted="Prompt",
            response_redacted="Response",
        )
        storage.insert_trace(trace)
        judgments = {
            "old": Judgment(
                judgment_id="old",
                trace_id=trace.trace_id,
                created_at=created,
                judge_models=["judge-model"],
                dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
            ),
            "new": Judgment(
                judgment_id="new",
                trace_id=trace.trace_id,
                created_at=created + timedelta(seconds=1),
                judge_models=["judge-model"],
                dimensions=[DimensionScore(name="quality", verdict=Verdict.FAIL)],
            ),
        }
        for name in order:
            storage.insert_judgment(judgments[name])
        storage.close()
        outputs.append(build_bundle(path))

    for bundle in outputs:
        assert bundle["meta"]["totalJudged"] == 1
        assert bundle["dimensionOverall"][0] == {
            "dim": "quality", "passRate": 0.0,
            "pass": 0, "fail": 1, "unclear": 0, "tot": 1,
        }
        assert bundle["samples"][0]["judgment"]["dims"][0]["verdict"] == "fail"
    assert outputs[0]["dimensionOverall"] == outputs[1]["dimensionOverall"]


def test_bundle_preserves_unknown_provider_and_model_values_without_crashing(tmp_path):
    path = tmp_path / "extension-values.db"
    storage = SQLiteStorage(str(path))
    values = [
        ("custom-gateway", "vendor/model:beta"),
        ("custom.with.dot", "model.one"),
        ("__provider_null__", "literal-marker"),
        ("", ""),
        (None, None),
        ("vendor/模型", "model with spaces"),
        ("x" * 300, "y" * 300),
    ]
    for index, (provider, model) in enumerate(values):
        storage.insert_trace(Trace(
            trace_id=f"trace-{index}",
            provider=provider,
            request_model=model,
            cluster_id="extensions",
            prompt_redacted=f"Prompt {index}",
            response_redacted=f"Response {index}",
        ))
    storage.close()

    bundle = build_bundle(path)

    raw_providers = [provider["rawProvider"] for provider in bundle["providers"]]
    assert {
        "custom-gateway", "custom.with.dot", "__provider_null__", "", None,
        "vendor/模型", "x" * 300,
    } == set(raw_providers)
    assert all(provider["key"] for provider in bundle["providers"])
    assert all(
        provider["key"] in {"anthropic", "openai", "google"}
        or (
            provider["key"].startswith("provider_")
            and len(provider["key"]) == len("provider_") + 16
        )
        for provider in bundle["providers"]
    )
    assert len({provider["key"] for provider in bundle["providers"]}) == len(values)
    assert all(sample["providerKey"] for sample in bundle["samples"])


def test_bundle_labels_provider_aggregate_with_multiple_models_explicitly(tmp_path):
    path = tmp_path / "multiple-models.db"
    storage = SQLiteStorage(str(path))
    for model in ("model-a", "model-b"):
        storage.insert_trace(Trace(
            provider="custom-gateway",
            request_model=model,
            prompt_redacted="Prompt",
        ))
    storage.close()

    provider = build_bundle(path)["providers"][0]

    assert provider["model"] == "multiple models (2)"
    assert provider["models"] == ["model-a", "model-b"]


def test_bundle_never_reemits_content_canaries_from_storage(tmp_path):
    import json

    path = tmp_path / "redaction-boundary.db"
    storage = SQLiteStorage(str(path))
    canary = "api-secret@example.com"
    trace = Trace(
        provider="custom-gateway",
        request_model="model",
        prompt_redacted=f"Prompt {canary}",
        response_redacted=f"Response {canary}",
        error=f"Error {canary}",
        raw_messages=[{
            "role": "assistant",
            "content": [{"type": "tool_result", "content": {"email": canary}}],
        }],
    )
    storage.insert_trace(trace)
    stored = storage.get_trace(trace.trace_id)
    storage.close()

    bundle = build_bundle(path)

    assert canary not in json.dumps(bundle, sort_keys=True)
    assert canary not in repr(stored)


def test_bundle_redacts_legacy_rows_that_bypassed_current_storage_boundary(tmp_path):
    import json
    import sqlite3

    path = tmp_path / "legacy-privacy.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        trace_id="legacy-trace",
        provider="openai",
        cluster_id="legacy",
        prompt_redacted="safe",
        response_redacted="safe",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        judgment_id="legacy-judgment",
        trace_id=trace.trace_id,
        judge_models=["legacy-judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.FAIL)],
    ))
    storage.close()

    canary = "legacy-leak@example.com"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE traces SET prompt_redacted=?, response_redacted=?, error=?",
            (canary, canary, canary),
        )
        connection.execute(
            "UPDATE judgments SET dimensions_json=?",
            (json.dumps([{
                "name": "quality",
                "verdict": "fail",
                "reasoning": canary,
                "judge_model": "legacy-judge",
            }]),),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert canary not in json.dumps(bundle, sort_keys=True)


def test_bundle_supports_database_without_historical_drift_table(tmp_path):
    path = tmp_path / "pre-drift-schema.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(provider="openai", prompt_redacted="Prompt"))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE drift_signals")
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert bundle["driftSignals"] == []
    assert bundle["evaluation"]["driftStatus"] == "empty"
    assert bundle["evaluation"]["unattributedDriftSignals"] == 0


def test_bundle_preserves_nullable_historical_drift_statistics(tmp_path):
    path = tmp_path / "nullable-drift.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="openai",
        cluster_id="support",
        prompt_redacted="Prompt",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="nullable-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    _persist_drift_snapshot(storage, DriftSignal(
        signal_id="legacy-null",
        cluster_id="support",
        dimension="quality",
        evaluator_fingerprint="nullable-evaluator",
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE drift_signals SET statistic_value=NULL, "
            "effect_size_cliffs_delta=NULL, effect_size_cohens_d=NULL "
            "WHERE signal_id='legacy-null'"
        )
        connection.commit()
    finally:
        connection.close()

    signal = build_bundle(path)["driftSignals"][0]

    assert signal["stat"] is None
    assert signal["cliffsDelta"] is None
    assert signal["cohensD"] is None


def test_bundle_preserves_unclear_coverage_statistic_precision(tmp_path):
    path = tmp_path / "coverage-drift.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(provider="openai", cluster_id="support", prompt_redacted="Prompt")
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="coverage-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    coverage_signals = [
        DriftSignal(
            signal_id=signal_id,
            cluster_id="support",
            dimension="quality",
            evaluator_fingerprint="coverage-evaluator",
            statistic_name="unclear_rate_increase",
            statistic_value=statistic,
        )
        for signal_id, statistic in (("coverage-42", 0.42), ("coverage-37", 0.37))
    ]
    _persist_drift_snapshot(storage, *coverage_signals)
    storage.close()

    statistics = {
        signal["id"]: signal["stat"] for signal in build_bundle(path)["driftSignals"]
    }

    assert statistics == {"coverage-42": 0.42, "coverage-37": 0.37}


def test_nameless_historical_dimension_remains_unclear_in_trace_summary(tmp_path):
    import json

    path = tmp_path / "nameless-dimension.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        trace_id="trace-nameless",
        provider="openai",
        cluster_id="support",
        prompt_redacted="Prompt",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        judgment_id="judgment-nameless",
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="nameless-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE judgments SET dimensions_json=? WHERE judgment_id=?",
            (json.dumps([
                {"name": "quality", "verdict": "pass"},
                {"name": "", "verdict": "pass"},
            ]), "judgment-nameless"),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)
    [sample] = bundle["samples"]

    assert bundle["scoreCoverage"]["unclear"] == 1
    assert sample["judgment"]["summary"] == {
        "status": "unclear",
        "pass": 1,
        "fail": 0,
        "unclear": 1,
        "missing": 0,
        "passRate": 100.0,
    }
    assert sample["judgment"]["dims"] == [{
        "name": "quality", "verdict": "pass", "reasoning": "",
    }]


def test_bundle_treats_malformed_historical_drift_numbers_as_unavailable(tmp_path):
    path = tmp_path / "malformed-drift.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(provider="openai", cluster_id="support", prompt_redacted="Prompt")
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="malformed-number-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    _persist_drift_snapshot(storage, DriftSignal(
        signal_id="legacy-malformed-number",
        cluster_id="support",
        dimension="quality",
        evaluator_fingerprint="malformed-number-evaluator",
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE drift_signals SET statistic_value='not-a-number', "
            "effect_size_cliffs_delta='not-a-number', "
            "effect_size_cohens_d='not-a-number' "
            "WHERE signal_id='legacy-malformed-number'"
        )
        connection.commit()
    finally:
        connection.close()

    signal = build_bundle(path)["driftSignals"][0]

    assert signal["stat"] is None
    assert signal["cliffsDelta"] is None
    assert signal["cohensD"] is None


def test_bundle_excludes_legacy_drift_table_missing_run_metadata(tmp_path):
    path = tmp_path / "pre-effect-columns.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="openai",
        cluster_id="support",
        prompt_redacted="Prompt",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="legacy-effects-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE drift_signals")
        connection.executescript(
            """
            CREATE TABLE drift_signals (
                signal_id TEXT PRIMARY KEY,
                detected_at TEXT NOT NULL,
                cluster_id TEXT,
                dimension TEXT,
                direction TEXT,
                evaluator_fingerprint TEXT,
                statistic_name TEXT,
                statistic_value REAL,
                p_value REAL,
                p_value_adjusted REAL,
                effect_size_cohens_d REAL,
                sample_size_current INTEGER,
                sample_size_baseline INTEGER,
                contributing_layers_json TEXT,
                example_trace_ids_json TEXT,
                recommended_action TEXT
            );
            """
        )
        connection.execute(
            """INSERT INTO drift_signals (
                signal_id, detected_at, cluster_id, dimension, direction,
                evaluator_fingerprint, statistic_name, statistic_value,
                p_value, p_value_adjusted, effect_size_cohens_d,
                sample_size_current, sample_size_baseline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "legacy-effects",
                "2026-08-01T00:00:00+00:00",
                "support",
                "quality",
                "regression",
                "legacy-effects-evaluator",
                "mann_whitney_u",
                8.0,
                0.01,
                0.02,
                -0.4,
                30,
                30,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert bundle["driftSignals"] == []
    assert bundle["driftRun"] is None
    assert bundle["evaluation"]["driftStatus"] == "historical_without_run"


def test_bundle_normalizes_mixed_naive_and_aware_historical_timestamps(tmp_path):
    path = tmp_path / "mixed-timestamps.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(
        trace_id="legacy-naive",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        provider="openai",
        prompt_redacted="Legacy",
    ))
    storage.insert_trace(Trace(
        trace_id="current-aware",
        started_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        provider="openai",
        prompt_redacted="Current",
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE traces SET started_at = ? WHERE trace_id = ?",
            ("2026-01-01T00:00:00", "legacy-naive"),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert bundle["meta"]["durationHours"] == 1
    assert {sample["trace_id"] for sample in bundle["samples"]} == {
        "legacy-naive", "current-aware",
    }


def test_bundle_treats_malformed_historical_latency_as_unavailable(tmp_path):
    path = tmp_path / "malformed-latency.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(
        trace_id="bad-latency",
        provider="openai",
        prompt_redacted="Prompt",
        latency_ms=12.5,
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE traces SET latency_ms = ? WHERE trace_id = ?",
            ("not-a-number", "bad-latency"),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert bundle["providers"][0]["avgLatency"] == 0.0
    assert bundle["samples"][0]["latency_ms"] is None


def test_bundle_handles_sqlite_file_without_verdict_tables(tmp_path):
    path = tmp_path / "empty-schema.db"
    sqlite3.connect(path).close()

    bundle = build_bundle(path)

    assert bundle["meta"]["totalTraces"] == 0
    assert bundle["evaluation"]["status"] == "empty"


def test_bundle_handles_historical_database_without_judgments_table(tmp_path):
    path = tmp_path / "traces-only.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """CREATE TABLE traces (
                trace_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                provider TEXT,
                request_model TEXT,
                cluster_id TEXT,
                latency_ms REAL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                error TEXT,
                prompt_redacted TEXT,
                response_redacted TEXT,
                finish_reason TEXT
            )"""
        )
        connection.execute(
            """INSERT INTO traces (
                trace_id, started_at, provider, request_model, prompt_redacted
            ) VALUES (?, ?, ?, ?, ?)""",
            ("legacy", "2026-01-01T00:00:00", "openai", "legacy-model", "Prompt"),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert bundle["meta"]["totalTraces"] == 1
    assert bundle["evaluation"]["status"] == "empty"


def test_mixed_provider_lead_signal_falls_back_to_real_chart_series(tmp_path):
    path = tmp_path / "mixed-provider-focus.db"
    storage = SQLiteStorage(str(path))
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, provider in enumerate(("anthropic", "openai")):
        trace = Trace(
            trace_id=f"trace-{provider}",
            started_at=started + timedelta(hours=index),
            provider=provider,
            cluster_id="mixed-support",
            prompt_redacted="Prompt",
        )
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            evaluator_provider="fake",
            evaluator_fingerprint="mixed-evaluator",
            evaluator_config={"temperature": 0},
            expected_dimensions=["relevance"],
            judge_models=["judge"],
            dimensions=[DimensionScore(name="relevance", verdict=Verdict.PASS)],
        ))
    _persist_drift_snapshot(storage, DriftSignal(
        cluster_id="mixed-support",
        dimension="relevance",
        evaluator_fingerprint="mixed-evaluator",
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["driftSignals"][0]["provider"] == ""
    assert bundle["focusProvider"] == "anthropic"
    assert bundle["focusProviderLabel"] == "anthropic"
    assert any(row["relevance"] == 100.0 for row in bundle["haikuDim"])


def test_custom_rubric_dimension_populates_focused_dimension_series(tmp_path):
    path = tmp_path / "custom-dimension-focus.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        trace_id="custom-dimension",
        provider="openai",
        cluster_id="agent",
        prompt_redacted="Prompt",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="custom-dimension-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["action_correctness"],
        judge_models=["judge"],
        dimensions=[DimensionScore(
            name="action_correctness",
            verdict=Verdict.PASS,
        )],
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["dimensionOverall"][0]["dim"] == "action_correctness"
    assert bundle["haikuDim"] == [{"hour": 0, "action_correctness": 100.0}]


def test_multi_provider_bundle_omits_unused_cluster_time_series(tmp_path):
    path = tmp_path / "multi-provider-clusters.db"
    storage = SQLiteStorage(str(path))
    for provider, cluster in (("openai", "refund"), ("anthropic", "shipping")):
        trace = Trace(provider=provider, cluster_id=cluster)
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
        ))
    storage.close()

    bundle = build_bundle(path)

    assert len(bundle["providers"]) == 2
    assert bundle["clusterPassrate"] == []


def test_time_series_size_is_bounded_by_observed_bins_not_elapsed_wall_time(tmp_path):
    path = tmp_path / "sparse-years.db"
    storage = SQLiteStorage(str(path))
    for trace_id, started_at, verdict in (
        ("old", datetime(2000, 1, 1, tzinfo=timezone.utc), Verdict.PASS),
        ("new", datetime(2026, 1, 1, tzinfo=timezone.utc), Verdict.FAIL),
    ):
        storage.insert_trace(Trace(
            trace_id=trace_id,
            started_at=started_at,
            provider="openai",
            cluster_id="support",
        ))
        storage.insert_judgment(Judgment(
            trace_id=trace_id,
            dimensions=[DimensionScore(name="quality", verdict=verdict)],
        ))
    storage.close()

    bundle = build_bundle(path)

    assert len(bundle["tsRows"]) == 2
    assert len(bundle["passrate"]) == 2
    assert len(bundle["clusterPassrate"]) == 2


def test_dashboard_reads_pre_cluster_schema_without_503_or_mutating_it(tmp_path):
    path = tmp_path / "legacy-dashboard.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
            provider TEXT, operation TEXT, request_model TEXT, response_model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, temperature REAL,
            max_tokens INTEGER, finish_reason TEXT, error TEXT, latency_ms REAL,
            prompt_redacted TEXT, response_redacted TEXT, raw_messages_json TEXT,
            tenant_id TEXT, session_id TEXT, user_id_hash TEXT,
            tags_json TEXT, cost_usd REAL
        );
        INSERT INTO traces (
            trace_id, started_at, provider, request_model, prompt_redacted
        ) VALUES (
            'legacy', '2026-01-01T00:00:00+00:00', 'openai', 'legacy-model', 'Prompt'
        );
        """
    )
    connection.close()

    before = path.read_bytes()
    bundle = build_bundle(path)

    assert bundle["meta"]["totalTraces"] == 1
    assert bundle["clusters"] == []
    assert bundle["samples"][0]["cluster_id"] is None
    assert path.read_bytes() == before


def test_fisher_odds_ratio_keeps_small_nonzero_value(tmp_path):
    path = tmp_path / "odds-ratio.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(provider="openai", cluster_id="support")
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.FAIL)],
    ))
    _persist_drift_snapshot(storage, DriftSignal(
        cluster_id="support",
        dimension="quality",
        evaluator_fingerprint="evaluator",
        statistic_name="fisher_exact",
        statistic_value=0.04,
    ))
    storage.close()

    bundle = build_bundle(path)
    assert bundle["driftSignals"][0]["stat"] == 0.04


def test_custom_provider_late_trace_is_selected_using_chart_safe_key(tmp_path):
    path = tmp_path / "custom-provider-late.db"
    storage = SQLiteStorage(str(path))
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(45):
        storage.insert_trace(Trace(
            trace_id=f"early-{index:02d}",
            started_at=started + timedelta(minutes=index),
            provider="openai",
            cluster_id="other",
            prompt_redacted="Early",
        ))
    late = Trace(
        trace_id="late-custom",
        started_at=started + timedelta(hours=5),
        provider="custom-gateway",
        cluster_id="custom-cluster",
        prompt_redacted="Late custom",
    )
    storage.insert_trace(late)
    storage.insert_judgment(Judgment(
        trace_id=late.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="custom-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["relevance"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="relevance", verdict=Verdict.FAIL)],
    ))
    _persist_drift_snapshot(storage, DriftSignal(
        cluster_id="custom-cluster",
        dimension="relevance",
        evaluator_fingerprint="custom-evaluator",
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["driftSignals"][0]["provider"].startswith("provider_")
    assert "late-custom" in {sample["trace_id"] for sample in bundle["samples"]}


def test_authenticated_cors_preflight_reaches_cors_middleware(monkeypatch):
    import httpx

    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")
    monkeypatch.setenv("VERDICT_CORS_ORIGINS", "https://review.example")
    async def request_preflight():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.options(
                "/api/data",
                headers={
                    "Origin": "https://review.example",
                    "Access-Control-Request-Method": "GET",
                },
            )

    response = asyncio.run(request_preflight())

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://review.example"


def test_basic_auth_gates_dashboard_shells_and_data_but_not_health(monkeypatch):
    import base64

    import httpx

    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")
    app = create_app()

    async def requests():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            token = base64.b64encode(b"reviewer:secret").decode()
            return (
                await client.get("/"),
                await client.get("/api/health"),
                await client.get("/dashboard"),
                await client.get("/api/data"),
                await client.get("/api/registry"),
                await client.get("/dashboard", headers={"Authorization": f"Basic {token}"}),
            )

    landing, health, dashboard, data, registry, authenticated = asyncio.run(requests())

    assert landing.status_code == 401
    assert health.status_code == 200
    assert dashboard.status_code == 401
    assert data.status_code == 401
    assert registry.status_code == 401
    assert dashboard.headers["www-authenticate"] == 'Basic realm="Verdict"'
    assert landing.headers["www-authenticate"] == 'Basic realm="Verdict"'
    assert authenticated.status_code == 200


def test_basic_auth_compares_both_credentials_without_username_short_circuit(monkeypatch):
    import base64
    import secrets

    import httpx

    calls = []
    real_compare = secrets.compare_digest

    def counting_compare(left, right):
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")
    monkeypatch.setattr(secrets, "compare_digest", counting_compare)
    app = create_app()

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            token = base64.b64encode(b"wrong:also-wrong").decode()
            return await client.get(
                "/dashboard", headers={"Authorization": f"Basic {token}"}
            )

    response = asyncio.run(request())

    assert response.status_code == 401
    assert calls == [("wrong", "reviewer"), ("also-wrong", "secret")]
