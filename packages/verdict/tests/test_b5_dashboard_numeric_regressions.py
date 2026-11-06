"""Real storage and API regressions for bounded trace previews and numeric metrics."""

import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import verdict.dashboard.app as dashboard_app
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from fastapi.testclient import TestClient
from verdict.dashboard import create_app
from verdict.redaction import redact_structure
from verdict.schema import SpanRecord, Trace
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage


def test_dashboard_home_bounds_many_long_trace_prompts(tmp_path):
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    for index in range(30):
        storage.insert_trace(
            Trace(
                provider="synthetic",
                request_model="sample-model",
                prompt_redacted=f"Prompt {index}: " + "a" * 40_000,
                response_redacted="completed",
            )
        )
    storage.close()

    response = TestClient(create_app(storage=str(path))).get("/api/data")

    assert response.status_code == 200
    samples = response.json()["samples"]
    assert len(samples) == 30
    assert all(len(sample["prompt_redacted"]) <= 8_000 for sample in samples)
    assert all(sample["promptPreviewTruncated"] for sample in samples)


def test_long_trace_preview_is_deferred_until_exact_selection(tmp_path, monkeypatch):
    path = tmp_path / "deferred-preview.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(provider="synthetic", request_model="sample-model",
                  prompt_redacted="short", response_redacted="short")
    storage.insert_trace(trace)
    storage.close()
    canary = "sk-ant-" + "S" * 24
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE traces SET prompt_redacted=?,response_redacted=?,error=? WHERE trace_id=?",
            ("p" * 7_995 + canary + "q" * 40_000,
             "r" * 40_000, "e" * 40_000, trace.trace_id),
        )
    original = dashboard_app.redact_structure
    long_strings = []

    def tracked(value, *args, **kwargs):
        if isinstance(value, str) and len(value) > 8_000:
            long_strings.append(len(value))
        return original(value, *args, **kwargs)

    monkeypatch.setattr(dashboard_app, "redact_structure", tracked)
    client = TestClient(create_app(storage=str(path)))
    listed = client.get("/api/data")
    assert listed.status_code == 200
    assert canary not in listed.text
    sample = listed.json()["samples"][0]
    assert sample["promptPreviewDeferred"] is True
    assert sample["responsePreviewDeferred"] is True
    assert sample["errorPreviewDeferred"] is True
    assert not long_strings

    selected = client.get("/api/data", params={"trace_id": trace.trace_id})
    assert selected.status_code == 200
    assert canary not in selected.text
    assert "sk-ant-" not in selected.text
    exact = selected.json()["samples"][0]
    assert exact["prompt_redacted"].startswith("p" * 100)
    assert exact["promptPreviewDeferred"] is False
    assert long_strings


def test_dashboard_home_bounds_many_long_provider_errors(tmp_path):
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    canary = "sk-ant-" + "S" * 24
    for _ in range(30):
        storage.insert_trace(
            Trace(
                provider="synthetic",
                request_model="sample-model",
                prompt_redacted="short prompt",
                response_redacted=None,
                error="provider failed: " + "e" * 7_950 + " " + canary + " " + "x" * 32_000,
            )
        )
    stored = storage.list_traces(limit=1)[0]
    storage.close()

    response = TestClient(create_app(storage=str(path))).get("/api/data")

    assert response.status_code == 200
    samples = response.json()["samples"]
    assert len(samples) == 30
    assert all(len(sample["error"]) <= 8_000 for sample in samples)
    assert all(sample["errorPreviewTruncated"] for sample in samples)
    assert canary not in stored.error
    assert canary not in response.text


def test_dashboard_home_bounds_long_prompts_on_postgres():
    dsn, reason = validate_test_dsn(
        os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False
    )
    if dsn is None:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as scoped_dsn:
        storage = PostgresStorage(scoped_dsn, min_pool=1, max_pool=2)
        try:
            for index in range(30):
                storage.insert_trace(
                    Trace(
                        provider="synthetic",
                        request_model="sample-model",
                        prompt_redacted=f"Prompt {index}: " + "a" * 40_000,
                        response_redacted=None,
                        error="provider failed: " + "e" * 40_000,
                    )
                )
            storage.insert_span(
                SpanRecord(
                    span_id="postgres-numeric-metrics",
                    started_at=datetime.now(timezone.utc),
                    tenant_id="local",
                    attributes={"latency_ms": 0.4111111111111111, "pan": 4111111111111111.0},
                )
            )
            stored_span = storage.list_spans(span_id="postgres-numeric-metrics", tenant_id="local")[
                0
            ]
        finally:
            storage.close()

        response = TestClient(create_app(storage=scoped_dsn)).get("/api/data")

        assert stored_span.attributes["latency_ms"] == 0.4111111111111111
        assert "4111111111111111" not in str(stored_span.attributes["pan"])
        assert response.status_code == 200
        samples = response.json()["samples"]
        assert len(samples) == 30
        assert all(sample["promptPreviewTruncated"] for sample in samples)
        assert all(sample["errorPreviewTruncated"] for sample in samples)
        assert all(sample["promptPreviewDeferred"] for sample in samples)
        assert all(sample["errorPreviewDeferred"] for sample in samples)
        selected = TestClient(create_app(storage=scoped_dsn)).get(
            "/api/data", params={"trace_id": samples[0]["trace_id"]},
        )
        assert selected.status_code == 200
        exact = next(sample for sample in selected.json()["samples"]
                     if sample["trace_id"] == samples[0]["trace_id"])
        assert exact["promptPreviewDeferred"] is False
        assert exact["errorPreviewDeferred"] is False
        assert exact["prompt_redacted"].startswith("Prompt")


def test_selected_trace_outside_page_keeps_redacted_bounded_preview(tmp_path):
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    oldest_id = None
    canary = "sk-ant-" + "S" * 24
    now = datetime.now(timezone.utc)
    for index in range(35):
        trace = Trace(
            provider="synthetic",
            request_model="sample-model",
            started_at=now - timedelta(minutes=35 - index),
            prompt_redacted="a" * 7_980 + " " + canary + " " + "b" * 32_000,
            response_redacted="completed",
        )
        storage.insert_trace(trace)
        if index == 0:
            oldest_id = trace.trace_id
    stored = storage.get_trace(oldest_id)
    storage.close()

    response = TestClient(create_app(storage=str(path))).get(
        "/api/data", params={"trace_id": oldest_id}
    )

    assert response.status_code == 200
    payload = response.json()
    assert oldest_id not in {sample["trace_id"] for sample in payload["samples"]}
    selected = payload["selectedTrace"]
    assert selected["trace_id"] == oldest_id
    assert len(selected["prompt_redacted"]) == 8_000
    assert selected["promptPreviewTruncated"] is True
    assert canary not in stored.prompt_redacted
    assert canary not in response.text


def test_finite_float_metrics_keep_numeric_type_and_card_strings_are_redacted():
    value = {"latency_ms": 0.4111111111111111, "cost_usd": 0.0026, "card_text": "4111111111111111"}

    cleaned = redact_structure(value)

    assert cleaned["latency_ms"] == value["latency_ms"]
    assert isinstance(cleaned["latency_ms"], float)
    assert cleaned["cost_usd"] == value["cost_usd"]
    assert isinstance(cleaned["cost_usd"], float)
    assert "4111111111111111" not in cleaned["card_text"]
    assert "4111111111111111" not in str(redact_structure(4111111111111111.0))


@pytest.mark.parametrize("mode,secret", [("redact", None), ("hash", "synthetic-secret")])
def test_float_redaction_is_atomic_and_repeated_sanitization_is_stable(mode, secret):
    source = {
        "unknown": [0.4111111111111111, 4111111111111111.0, {"metric": -0.4111111111111111}],
        "text": "4111111111111111",
    }

    first = redact_structure(source, mode=mode, secret=secret)
    second = redact_structure(first, mode=mode, secret=secret)

    assert first == second
    assert first["unknown"][0] == source["unknown"][0]
    assert first["unknown"][2]["metric"] == source["unknown"][2]["metric"]
    assert "4111111111111111" not in first["unknown"][1]
    assert "4111111111111111" not in first["text"]
    assert isinstance(first["unknown"][1], str)
    assert redact_structure(float("inf"), mode=mode, secret=secret) == "<REDACTED>"
    assert redact_structure(float("nan"), mode=mode, secret=secret) == "<REDACTED>"
    assert math.isfinite(first["unknown"][0])


def test_numeric_metrics_survive_sqlite_storage_and_dashboard_api(tmp_path):
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="synthetic",
        request_model="sample-model",
        prompt_redacted="Check cost",
        response_redacted="completed",
        cost_usd=0.4111111111111111,
    )
    storage.insert_trace(trace)
    span = SpanRecord(
        span_id="numeric-metrics",
        started_at=datetime.now(timezone.utc),
        tenant_id="local",
        attributes={
            "latency_ms": 0.4111111111111111,
            "unknown": {"cost_usd": 0.0026, "pan": 4111111111111111.0},
        },
    )
    storage.insert_span(span)
    stored_span = storage.list_spans(span_id=span.span_id, tenant_id="local")[0]
    storage.close()

    response = TestClient(create_app(storage=str(path))).get("/api/data")

    assert stored_span.attributes["latency_ms"] == 0.4111111111111111
    assert stored_span.attributes["unknown"]["cost_usd"] == 0.0026
    assert "4111111111111111" not in str(stored_span.attributes["unknown"]["pan"])
    assert response.status_code == 200
    sample = next(item for item in response.json()["samples"] if item["trace_id"] == trace.trace_id)
    assert sample["cost_usd"] == 0.4111111111111111
