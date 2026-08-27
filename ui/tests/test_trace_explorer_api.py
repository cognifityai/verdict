"""Contract tests for the store-wide Trace Explorer read model."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from verdict.dashboard.app import create_app
from verdict.schema import DimensionScore, Judgment, Trace, Verdict
from verdict.storage import SQLiteStorage


def _request(app, path: str, *, headers: dict[str, str] | None = None):
    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get(path, headers=headers)

    return asyncio.run(send())


def _insert_numbered_traces(
    storage: SQLiteStorage,
    *,
    count: int,
    started_at: datetime,
) -> None:
    for index in range(count):
        storage.insert_trace(Trace(
            trace_id=f"trace-{index:03d}",
            started_at=started_at + timedelta(minutes=index),
            provider="openai" if index % 2 else "anthropic",
            request_model=f"model-{index % 3}",
            cluster_id=f"cluster-{index % 4}",
            prompt_redacted=f"Prompt {index}",
            response_redacted=f"Response {index}",
            input_tokens=index,
            output_tokens=index + 1,
            latency_ms=float(100 + index),
            tags={
                "verdict.workload": "agent",
                "telemetry.source": "fixture",
            },
        ))


def _page_database(path) -> None:
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    _insert_numbered_traces(storage, count=55, started_at=started_at)
    storage.insert_trace(Trace(
        trace_id="judge-newer-than-everything",
        started_at=started_at + timedelta(days=10),
        provider="openai",
        tags={"verdict.workload": "judge"},
    ))
    storage.close()


def test_trace_pages_reach_every_application_trace_without_changing_legacy_sample(
    tmp_path,
) -> None:
    path = tmp_path / "trace-pages.db"
    _page_database(path)
    app = create_app(storage=f"sqlite:///{path}")

    first_response = _request(app, "/api/traces?limit=25")
    first = first_response.json()
    second_response = _request(
        app,
        f"/api/traces?limit=25&cursor={quote(first['nextCursor'], safe='')}",
    )
    second = second_response.json()
    third_response = _request(
        app,
        f"/api/traces?limit=25&cursor={quote(second['nextCursor'], safe='')}",
    )
    third = third_response.json()
    legacy = _request(app, "/api/data").json()

    assert first_response.status_code == 200
    assert [item["trace_id"] for item in first["items"]] == [
        f"trace-{index:03d}" for index in range(54, 29, -1)
    ]
    assert [item["trace_id"] for item in second["items"]] == [
        f"trace-{index:03d}" for index in range(29, 4, -1)
    ]
    assert [item["trace_id"] for item in third["items"]] == [
        f"trace-{index:03d}" for index in range(4, -1, -1)
    ]
    assert first["total"] == second["total"] == third["total"] == 55
    assert first["limit"] == second["limit"] == third["limit"] == 25
    assert first["nextCursor"] and second["nextCursor"]
    assert third["nextCursor"] is None
    assert "response_redacted" not in first["items"][0]
    assert legacy["truncation"]["resources"]["traceSamples"] == {
        "available": 55,
        "shown": 30,
        "limit": 30,
    }
    assert len(legacy["samples"]) == 30


def _search_database(path) -> None:
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    _insert_numbered_traces(storage, count=35, started_at=started_at)
    storage.insert_trace(Trace(
        trace_id="old-target_%_trace",
        started_at=started_at - timedelta(days=1),
        provider="custom-gateway",
        request_model="private-model",
        response_model="response-model-search-target",
        cluster_id="billing",
        prompt_redacted="Find literal 100%_coverage please",
        response_redacted="The old target is searchable",
        tags={
            "verdict.workload": "customer-specialist",
            "telemetry.source": "langfuse",
        },
    ))
    storage.insert_trace(Trace(
        trace_id="metadata-target",
        started_at=started_at - timedelta(days=2),
        provider="custom-gateway",
        request_model="private-model",
        prompt_redacted=None,
        response_redacted=None,
        tags={"telemetry.source": "otlp"},
    ))
    storage.close()


def test_trace_search_and_filters_apply_to_the_whole_store_and_escape_wildcards(
    tmp_path,
) -> None:
    path = tmp_path / "trace-search.db"
    _search_database(path)
    app = create_app(storage=f"sqlite:///{path}")

    literal = _request(
        app,
        "/api/traces?q=100%25_coverage&provider=custom-gateway&capture=captured",
    )
    source = _request(app, "/api/traces?q=langfuse")
    response_model = _request(app, "/api/traces?q=response-model-search-target")
    metadata = _request(
        app,
        "/api/traces?provider=custom-gateway&capture=metadata",
    )

    assert [item["trace_id"] for item in literal.json()["items"]] == [
        "old-target_%_trace"
    ]
    assert [item["trace_id"] for item in source.json()["items"]] == [
        "old-target_%_trace"
    ]
    assert [item["trace_id"] for item in response_model.json()["items"]] == [
        "old-target_%_trace"
    ]
    assert [item["trace_id"] for item in metadata.json()["items"]] == [
        "metadata-target"
    ]


def test_trace_cursor_is_deterministic_bound_to_filters_and_rejects_bad_input(
    tmp_path,
) -> None:
    path = tmp_path / "trace-cursor.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    storage.insert_trace(Trace(trace_id="tie-a", started_at=started_at))
    storage.insert_trace(Trace(trace_id="tie-z", started_at=started_at))
    storage.insert_trace(Trace(
        trace_id="tie-judge",
        started_at=started_at,
        tags={"verdict.workload": "judge"},
    ))
    storage.close()
    app = create_app(storage=f"sqlite:///{path}")

    first = _request(app, "/api/traces?limit=1").json()
    second = _request(
        app,
        f"/api/traces?limit=1&cursor={quote(first['nextCursor'], safe='')}",
    )
    wrong_filter = _request(
        app,
        f"/api/traces?limit=1&provider=openai&cursor={quote(first['nextCursor'], safe='')}",
    )
    malformed = _request(app, "/api/traces?cursor=not-a-cursor")
    oversized = _request(app, f"/api/traces?cursor={'x' * 3000}")
    bad_limit = _request(app, "/api/traces?limit=101")

    assert [item["trace_id"] for item in first["items"]] == ["tie-z"]
    assert [item["trace_id"] for item in second.json()["items"]] == ["tie-a"]
    assert wrong_filter.status_code == 400
    assert malformed.status_code == 400
    assert oversized.status_code == 400
    assert bad_limit.status_code == 422


def test_trace_cursor_walks_older_rows_when_a_new_trace_arrives_between_pages(
    tmp_path,
) -> None:
    path = tmp_path / "trace-concurrent-insert.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    _insert_numbered_traces(storage, count=5, started_at=started_at)
    storage.close()
    app = create_app(storage=f"sqlite:///{path}")

    first = _request(app, "/api/traces?limit=2").json()
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(
        trace_id="new-arrival",
        started_at=started_at + timedelta(days=1),
    ))
    storage.close()
    second = _request(
        app,
        f"/api/traces?limit=2&cursor={quote(first['nextCursor'], safe='')}",
    ).json()

    assert [item["trace_id"] for item in first["items"]] == [
        "trace-004",
        "trace-003",
    ]
    assert [item["trace_id"] for item in second["items"]] == [
        "trace-002",
        "trace-001",
    ]
    assert second["total"] == 6


def _detail_database(path) -> tuple[str, str]:
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    trace_id = "trace / unicode λ"
    storage.insert_trace(Trace(
        trace_id=trace_id,
        started_at=started_at,
        provider="anthropic",
        request_model="claude-test",
        response_model="claude-final",
        prompt_redacted="Explain the invoice",
        response_redacted="Here is the invoice explanation",
        raw_messages=[{"role": "user", "content": "Explain the invoice"}],
        input_tokens=12,
        output_tokens=34,
        latency_ms=123.4,
        cost_usd=0.0012,
        tags={"verdict.workload": "agent"},
    ))
    identity = {
        "evaluator_provider": "fake",
        "evaluator_config": {"temperature": 0},
        "evaluator_fingerprint": "detail-evaluator",
        "expected_dimensions": ["relevance"],
        "judge_models": ["judge-model"],
    }
    storage.insert_judgment(Judgment(
        judgment_id="older",
        trace_id=trace_id,
        created_at=started_at + timedelta(minutes=1),
        dimensions=[DimensionScore(
            name="relevance",
            verdict=Verdict.FAIL,
            reasoning="Older result",
        )],
        **identity,
    ))
    storage.insert_judgment(Judgment(
        judgment_id="newer",
        trace_id=trace_id,
        created_at=started_at + timedelta(minutes=2),
        dimensions=[DimensionScore(
            name="relevance",
            verdict=Verdict.PASS,
            reasoning="Latest result",
        )],
        **identity,
    ))
    storage.close()
    app = create_app(storage=f"sqlite:///{path}")
    evaluator_id = _request(app, "/api/data").json()["evaluation"]["selectedId"]
    return trace_id, evaluator_id


def test_trace_detail_returns_full_selected_evaluator_result_for_arbitrary_id(
    tmp_path,
) -> None:
    path = tmp_path / "trace-detail.db"
    trace_id, evaluator_id = _detail_database(path)
    app = create_app(storage=f"sqlite:///{path}")

    response = _request(
        app,
        f"/api/traces/{quote(trace_id, safe='')}?evaluator={evaluator_id}",
    )
    detail = response.json()

    assert response.status_code == 200
    assert detail["trace_id"] == trace_id
    assert detail["prompt_redacted"] == "Explain the invoice"
    assert detail["response_redacted"] == "Here is the invoice explanation"
    assert detail["input_tokens"] == 12
    assert detail["output_tokens"] == 34
    assert detail["latency_ms"] == 123
    assert detail["judgment"]["summary"]["status"] == "pass"
    assert detail["judgment"]["dims"] == [{
        "name": "relevance",
        "verdict": "pass",
        "reasoning": "Latest result",
    }]
    assert detail["truncation"] == {
        "prompt": False,
        "response": False,
        "error": False,
        "rawMessages": False,
        "tags": False,
        "judgmentReasoning": False,
        "judgments": False,
    }


def test_trace_list_and_detail_bound_oversized_historical_fields(tmp_path) -> None:
    path = tmp_path / "trace-bounds.db"
    storage = SQLiteStorage(str(path))
    started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    storage.insert_trace(Trace(
        trace_id="oversized-historical",
        started_at=started_at,
        prompt_redacted="safe",
        response_redacted="safe",
    ))
    storage.insert_judgment(Judgment(
        trace_id="oversized-historical",
        evaluator_provider="fake",
        evaluator_config={"temperature": 0},
        evaluator_fingerprint="oversized-evaluator",
        expected_dimensions=["quality"],
        judge_models=["judge-model"],
        dimensions=[DimensionScore(
            name="quality",
            verdict=Verdict.FAIL,
            reasoning="safe",
        )],
    ))
    storage.close()
    import json
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE traces SET prompt_redacted=?,response_redacted=?,error=?,"
        "raw_messages_json=?,tags_json=? WHERE trace_id=?",
        (
            "p" * 120_000,
            "r" * 120_000,
            "e" * 20_000,
            json.dumps([{"role": "user", "content": "m" * 120_000}]),
            json.dumps({"oversized": "t" * 25_000}),
            "oversized-historical",
        ),
    )
    connection.execute(
        "UPDATE judgments SET dimensions_json=? WHERE trace_id=?",
        (
            json.dumps([{
                "name": "quality",
                "verdict": "fail",
                "reasoning": "j" * 20_000,
            }]),
            "oversized-historical",
        ),
    )
    connection.commit()
    connection.close()
    app = create_app(storage=f"sqlite:///{path}")
    evaluator_id = _request(app, "/api/data").json()["evaluation"]["selectedId"]

    listing = _request(
        app,
        f"/api/traces?evaluator={evaluator_id}",
    ).json()["items"][0]
    detail = _request(
        app,
        f"/api/traces/oversized-historical?evaluator={evaluator_id}",
    ).json()

    assert len(listing["prompt_redacted"]) == 240
    assert listing["promptTruncated"] is True
    assert listing["error"] is True
    assert "dims" not in listing["judgment"]
    assert len(detail["prompt_redacted"]) == 100_000
    assert len(detail["response_redacted"]) == 100_000
    assert len(detail["error"]) == 10_000
    assert detail["raw_messages"] is None
    assert detail["tags"] == {}
    assert len(detail["judgment"]["dims"][0]["reasoning"]) == 10_000
    assert detail["truncation"] == {
        "prompt": True,
        "response": True,
        "error": True,
        "rawMessages": True,
        "tags": True,
        "judgmentReasoning": True,
        "judgments": False,
    }


def test_trace_endpoints_redact_historical_canaries_and_exclude_judge_detail(
    tmp_path,
) -> None:
    path = tmp_path / "trace-redaction.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(
        trace_id="unsafe-historical",
        prompt_redacted="safe-at-write",
        response_redacted="safe-at-write",
    ))
    storage.insert_trace(Trace(
        trace_id="judge-private",
        tags={"verdict.workload": "judge"},
    ))
    storage.close()
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE traces SET prompt_redacted=?, response_redacted=? WHERE trace_id=?",
        (
            "Contact alice@example.com from 192.0.2.1",
            "Card 4111 1111 1111 1111",
            "unsafe-historical",
        ),
    )
    connection.commit()
    connection.close()
    app = create_app(storage=f"sqlite:///{path}")

    listing = _request(app, "/api/traces?q=alice%40example.com")
    detail = _request(app, "/api/traces/unsafe-historical")
    judge_detail = _request(app, "/api/traces/judge-private")
    serialized = listing.text + detail.text

    assert listing.status_code == 200
    assert detail.status_code == 200
    assert "alice@example.com" not in serialized
    assert "192.0.2.1" not in serialized
    assert "4111 1111 1111 1111" not in serialized
    assert judge_detail.status_code == 404


def test_trace_endpoints_are_protected_by_dashboard_basic_auth(monkeypatch, tmp_path) -> None:
    path = tmp_path / "trace-auth.db"
    _page_database(path)
    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")
    app = create_app(storage=f"sqlite:///{path}")
    token = base64.b64encode(b"reviewer:secret").decode()

    list_response = _request(app, "/api/traces")
    detail_response = _request(app, "/api/traces/trace-000")
    authenticated = _request(
        app,
        "/api/traces",
        headers={"Authorization": f"Basic {token}"},
    )

    assert list_response.status_code == 401
    assert detail_response.status_code == 401
    assert authenticated.status_code == 200
