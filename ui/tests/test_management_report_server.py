from datetime import datetime, timedelta, timezone

import pytest
import verdict.dashboard.app as dashboard_app
from verdict.client import VerdictClient
from verdict.dashboard.app import build_bundle, create_app
from verdict.schema import DimensionScore, Judgment, Trace, Verdict
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
        "judgedCalls": 0,
        "judgeErrorCalls": 0,
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
    assert report["applications"]["rows"][1]["environment"] == "staging"
    assert report["applications"]["rows"][1]["attributed"] is True
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
    assert report["applications"]["rows"][0]["environment"] == "Unspecified"
    assert report["applications"]["rows"][0]["attributed"] is False
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

        report = build_bundle(path, report_days=7 if name == "empty" else 0)[
            "managementReport"
        ]
        assert report["scope"]["calls"] == 0
        assert report["period"]["days"] == (7 if name == "empty" else 0)
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


def test_management_report_normalizes_default_service_without_name_collision(tmp_path):
    path = tmp_path / "service-identity.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 9, 11, tzinfo=timezone.utc)
    for trace_id, service_name in (
        ("default", VerdictClient(storage=None).service_name),
        ("blank", "  "),
        ("literal", "Unattributed"),
    ):
        storage.insert_trace(_trace(
            trace_id, started_at, service_name=service_name, environment="production"
        ))
    storage.close()

    report = build_bundle(path)["managementReport"]

    assert report["scope"]["identifiedApplications"] == 1
    assert report["scope"]["unattributedCalls"] == 2
    rows = [
        (row["name"], row["attributed"], row["calls"])
        for row in report["applications"]["rows"]
    ]
    assert rows == [("Unattributed", False, 2), ("Unattributed", True, 1)]


def test_management_report_bounds_high_cardinality_dimensions(tmp_path):
    path = tmp_path / "high-cardinality.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 9, 11, tzinfo=timezone.utc)
    for index in range(10_050):
        storage.insert_trace(_trace(
            f"trace-{index}",
            started_at + timedelta(microseconds=index),
            environment=f"environment-{index}",
            provider="custom-provider",
            model=f"model-{index}",
        ))
    storage.close()

    report = build_bundle(path)["managementReport"]

    assert report["scope"]["latencyKnownCalls"] == 10_050
    assert report["scope"]["latencySampledCalls"] == 10_000
    for table in ("applications", "models"):
        assert report[table]["availableRows"] == 10_050
        assert report[table]["shownRows"] == 20
    forbidden = {"environments", "providers", "models"}
    assert all(set(row).isdisjoint(forbidden) for row in report["applications"]["rows"])


def test_management_report_period_filters_every_aggregate(tmp_path, monkeypatch):
    path = tmp_path / "period.db"
    storage = SQLiteStorage(str(path))
    for trace_id, started_at, service in (
        ("before", datetime(2026, 8, 13, 23, 59, tzinfo=timezone.utc), "old-api"),
        ("first", datetime(2026, 8, 14, tzinfo=timezone.utc), "orders-api"),
        ("latest", datetime(2026, 9, 12, 23, 59, tzinfo=timezone.utc), "orders-api"),
        ("future", datetime(2026, 9, 13, tzinfo=timezone.utc), "future-api"),
    ):
        storage.insert_trace(_trace(trace_id, started_at, service_name=service))
    for trace_id in ("before", "first"):
        storage.insert_judgment(Judgment(
            trace_id=trace_id,
            dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
        ))
    storage.close()
    monkeypatch.setattr(
        dashboard_app, "_now_utc",
        lambda: datetime(2026, 9, 12, 12, tzinfo=timezone.utc),
    )

    report = build_bundle(path, report_days=30)["managementReport"]

    assert report["period"] == {
        "days": 30, "startDate": "2026-08-14", "endDate": "2026-09-12",
    }
    assert report["scope"]["calls"] == 2
    assert report["scope"]["firstCapturedAt"] == "2026-08-14T00:00:00+00:00"
    assert report["scope"]["latestCapturedAt"] == "2026-09-12T23:59:00+00:00"
    assert [row["date"] for row in report["timeline"]["rows"]] == [
        "2026-08-14", "2026-09-12",
    ]
    assert [(row["name"], row["calls"]) for row in report["applications"]["rows"]] == [
        ("orders-api", 2),
    ]
    assert report["models"]["rows"][0]["calls"] == 2
    assert report["scope"]["judgedCalls"] == 1
    assert report["scope"]["judgeErrorCalls"] == 0

    monkeypatch.setattr(
        dashboard_app, "_now_utc",
        lambda: datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
    )
    empty = build_bundle(path, report_days=7)["managementReport"]
    assert empty["scope"]["calls"] == 0
    assert empty["timeline"]["rows"] == []
    assert empty["applications"]["rows"] == []
    assert empty["models"]["rows"] == []


@pytest.mark.parametrize("report_days", [-1, 1, 31, 365, True, None])
def test_management_report_rejects_unsupported_periods(tmp_path, report_days):
    with pytest.raises(ValueError, match="report_days"):
        build_bundle(tmp_path / "unused.db", report_days=report_days)


def test_management_report_api_defaults_to_30_days_and_validates_selection(
    tmp_path, monkeypatch,
):
    import asyncio

    import httpx

    path = tmp_path / "api-period.db"
    storage = SQLiteStorage(str(path))
    for index, started_at in enumerate((
        datetime(2026, 8, 14, tzinfo=timezone.utc),
        datetime(2026, 9, 5, tzinfo=timezone.utc),
        datetime(2026, 9, 12, tzinfo=timezone.utc),
    )):
        storage.insert_trace(_trace(f"trace-{index}", started_at))
    storage.close()
    monkeypatch.setattr(
        dashboard_app, "_now_utc",
        lambda: datetime(2026, 9, 12, 12, tzinfo=timezone.utc),
    )
    app = create_app(storage=str(path))

    async def requests():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await asyncio.gather(
                client.get("/api/data"),
                client.get("/api/data?report_days=7"),
                client.get("/api/data?report_days=0"),
                client.get("/api/data?report_days=31"),
            )

    default, seven_days, all_time, invalid = asyncio.run(requests())
    assert default.json()["managementReport"]["scope"]["calls"] == 3
    assert default.json()["managementReport"]["period"]["days"] == 30
    assert seven_days.json()["managementReport"]["scope"]["calls"] == 1
    assert all_time.json()["managementReport"]["scope"]["calls"] == 3
    assert invalid.status_code == 422
