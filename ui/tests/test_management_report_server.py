from datetime import datetime, timedelta, timezone

from verdict.dashboard.app import build_bundle
from verdict.schema import Trace
from verdict.storage import SQLiteStorage


def _trace(
    trace_id: str,
    started_at: datetime,
    *,
    service_name: str = "orders-api",
    environment: str = "production",
    provider: str = "openai",
    model: str = "gpt-5-mini",
    error: str | None = None,
    workload: str | None = None,
) -> Trace:
    tags = {"verdict.workload": workload} if workload else {}
    return Trace(
        trace_id=trace_id,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=1),
        provider=provider,
        request_model=model,
        response_model=model,
        input_tokens=100,
        output_tokens=25,
        latency_ms=1000,
        cost_usd=0.001,
        error=error,
        service_name=service_name,
        environment=environment,
        tags=tags,
    )


def test_management_report_aggregates_daily_application_evidence(tmp_path):
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    start = datetime(2026, 9, 8, tzinfo=timezone.utc)

    for index in range(24):
        storage.insert_trace(_trace(f"flat-{index}", start + timedelta(minutes=30 * index)))
    for index in range(4):
        storage.insert_trace(
            _trace(
                f"burst-{index}",
                start + timedelta(days=2, minutes=index),
                service_name="billing-worker",
                environment="staging",
                provider="custom-provider",
                model="custom-model-v1",
                error="failed" if index == 0 else None,
            )
        )
    storage.insert_trace(
        _trace(
            "judge-call",
            start + timedelta(days=3),
            service_name="verdict-evaluator",
            workload="judge",
        )
    )
    storage.close()

    report = build_bundle(path)["managementReport"]

    assert report["schema"] == "management-report-v1"
    assert report["scope"] == {
        "firstCapturedAt": "2026-09-08T00:00:00+00:00",
        "latestCapturedAt": "2026-09-10T00:03:00+00:00",
        "calls": 28,
        "successfulCalls": 27,
        "failedCalls": 1,
        "successRatePct": 96.4,
        "inputTokens": 2800,
        "outputTokens": 700,
        "totalTokens": 3500,
        "tokenKnownCalls": 28,
        "costUsd": 0.028,
        "costKnownCalls": 28,
        "averageLatencyMs": 1000.0,
        "latencyKnownCalls": 28,
        "latencySampledCalls": 28,
        "p50LatencyMs": 1000.0,
        "p95LatencyMs": 1000.0,
        "identifiedApplications": 2,
        "unattributedCalls": 0,
    }
    assert report["timeline"] == {
        "availableDates": 2,
        "shownDates": 2,
        "rows": [
            {"date": "2026-09-08", "calls": 24, "totalTokens": 3000, "tokenKnownCalls": 24},
            {"date": "2026-09-10", "calls": 4, "totalTokens": 500, "tokenKnownCalls": 4},
        ],
    }
    assert [(row["name"], row["calls"]) for row in report["applications"]["rows"]] == [
        ("orders-api", 24),
        ("billing-worker", 4),
    ]
    assert report["applications"]["rows"][1]["environments"] == ["staging"]
    assert report["models"]["rows"][1]["provider"] == "custom-provider"
    assert report["models"]["rows"][1]["model"] == "custom-model-v1"


def test_management_report_keeps_unknown_and_partial_evidence_explicit(tmp_path):
    path = tmp_path / "partial.db"
    storage = SQLiteStorage(str(path))
    trace = _trace(
        "unattributed",
        datetime(2026, 9, 11, tzinfo=timezone.utc),
        service_name="",
        environment="",
        provider="",
        model="",
    )
    trace.input_tokens = None
    trace.output_tokens = None
    trace.latency_ms = None
    trace.cost_usd = None
    storage.insert_trace(trace)
    storage.close()

    report = build_bundle(path)["managementReport"]

    assert report["scope"]["identifiedApplications"] == 0
    assert report["scope"]["unattributedCalls"] == 1
    assert report["scope"]["tokenKnownCalls"] == 0
    assert report["scope"]["costKnownCalls"] == 0
    assert report["scope"]["latencyKnownCalls"] == 0
    assert report["scope"]["p50LatencyMs"] is None
    assert report["applications"]["rows"][0]["name"] == "Unattributed"
    assert report["models"]["rows"][0]["provider"] == "Unknown provider"
    assert report["models"]["rows"][0]["model"] == "Unknown model"


def test_management_report_keeps_empty_and_judge_only_stores_at_zero(tmp_path):
    for name, judge_calls in (("empty", 0), ("judge-only", 2)):
        path = tmp_path / f"{name}.db"
        storage = SQLiteStorage(str(path))
        for index in range(judge_calls):
            storage.insert_trace(
                _trace(
                    f"judge-{index}",
                    datetime(2026, 9, 11, index, tzinfo=timezone.utc),
                    service_name="verdict-evaluator",
                    workload="judge",
                )
            )
        storage.close()

        report = build_bundle(path)["managementReport"]
        assert report["scope"]["calls"] == 0
        assert report["timeline"]["rows"] == []
        assert report["applications"]["rows"] == []
        assert report["models"]["rows"] == []


def test_management_report_bounds_dates_and_uses_effective_response_model(tmp_path):
    path = tmp_path / "bounded.db"
    storage = SQLiteStorage(str(path))
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    for index in range(35):
        trace = _trace(f"day-{index}", start + timedelta(days=index))
        trace.request_model = "requested-model"
        trace.response_model = "effective-model"
        storage.insert_trace(trace)
    storage.insert_trace(_trace("judge-only", start + timedelta(days=40), workload="judge"))
    storage.close()

    report = build_bundle(path)["managementReport"]

    assert report["scope"]["calls"] == 35
    assert report["timeline"]["availableDates"] == 35
    assert report["timeline"]["shownDates"] == 31
    assert report["timeline"]["rows"][0]["date"] == "2026-07-05"
    assert report["timeline"]["rows"][-1]["date"] == "2026-08-04"
    assert report["models"]["rows"] == [
        {
            "provider": "openai",
            "model": "effective-model",
            "calls": 35,
            "successfulCalls": 35,
            "failedCalls": 0,
            "successRatePct": 100.0,
            "inputTokens": 3500,
            "outputTokens": 875,
            "totalTokens": 4375,
            "tokenKnownCalls": 35,
            "costUsd": 0.035,
            "costKnownCalls": 35,
            "averageLatencyMs": 1000.0,
            "latencyKnownCalls": 35,
        }
    ]
