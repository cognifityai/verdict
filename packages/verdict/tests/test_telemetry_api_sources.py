from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from verdict.telemetry.http import JsonHttpClient, TelemetryHttpError
from verdict.telemetry.model import ImportContext
from verdict.telemetry.sources.datadog import DatadogApiSource
from verdict.telemetry.sources.langfuse import LangfuseApiSource
from verdict.telemetry.sources.langsmith import LangSmithApiSource
from verdict.telemetry.sources.opik import OpikApiSource
from verdict.telemetry.sources.phoenix import PhoenixApiSource

UTC = timezone.utc
START = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
END = datetime(2026, 8, 1, 13, 0, tzinfo=UTC)


class _ServerState:
    def __init__(self, responses: list[tuple[int, dict[str, str], object]]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()


@contextmanager
def _json_server(
    responses: list[tuple[int, dict[str, str], object]],
) -> Iterator[tuple[str, _ServerState]]:
    state = _ServerState(responses)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            return

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            with state.lock:
                state.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(body) if body else None,
                    }
                )
                if not state.responses:
                    status, headers, payload = 500, {}, {"error": "unexpected request"}
                else:
                    status, headers, payload = state.responses.pop(0)
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _context(adapter: str) -> ImportContext:
    return ImportContext(adapter=adapter, source_scope="api-project", tenant_id="tenant-a")


def _langfuse_record(identifier: str) -> dict[str, object]:
    return {
        "id": identifier,
        "traceId": f"trace-{identifier}",
        "type": "GENERATION",
        "startTime": "2026-08-01T12:01:00Z",
        "endTime": "2026-08-01T12:01:00.200Z",
        "providedModelName": "gpt-4o-mini",
        "input": "question",
        "output": "answer",
        "usageDetails": {"input": 3, "output": 2},
    }


def test_langfuse_v2_api_uses_basic_auth_fields_and_cursor() -> None:
    responses = [
        (200, {}, {"data": [_langfuse_record("one")], "meta": {"cursor": "next-1"}}),
        (200, {}, {"data": [_langfuse_record("two")], "meta": {"cursor": None}}),
    ]
    with _json_server(responses) as (base_url, state):
        source = LangfuseApiSource(
            client=JsonHttpClient(),
            context=_context("langfuse"),
            base_url=base_url,
            public_key="public-test",
            secret_key="secret-test",
            start_time=START,
            end_time=END,
            page_size=10,
        )

        results = list(source)

    assert len(results) == 2
    assert all(item.trace is not None for item in results)
    assert len(state.requests) == 2
    assert "fields=basic%2Ctime%2Cio%2Cmodel%2Cusage%2Cmetrics" in state.requests[0]["path"]
    assert "cursor=next-1" in state.requests[1]["path"]
    assert state.requests[0]["headers"]["Authorization"].startswith("Basic ")
    assert "secret-test" not in state.requests[0]["path"]


def test_langsmith_query_api_posts_llm_filter_and_cursor() -> None:
    record = {
        "id": "run-1",
        "trace_id": "trace-1",
        "run_type": "llm",
        "start_time": "2026-08-01T12:01:00Z",
        "end_time": "2026-08-01T12:01:00.200Z",
        "inputs": {"prompt": "question"},
        "outputs": {"text": "answer"},
        "extra": {"metadata": {"ls_model_name": "gpt-4o-mini"}},
    }
    responses = [
        (200, {}, {"runs": [record], "cursors": {"next": "cursor-1"}}),
        (200, {}, {"runs": [], "cursors": {"next": None}}),
    ]
    with _json_server(responses) as (base_url, state):
        results = list(
            LangSmithApiSource(
                client=JsonHttpClient(),
                context=_context("langsmith"),
                base_url=base_url,
                api_key="langsmith-test-key",
                project_name="support",
                start_time=START,
                end_time=END,
            )
        )

    assert len(results) == 1
    assert state.requests[0]["method"] == "POST"
    assert state.requests[0]["path"] == "/runs/query"
    assert state.requests[0]["body"]["run_type"] == "llm"
    assert state.requests[1]["body"]["cursor"] == "cursor-1"
    assert state.requests[0]["headers"]["X-Api-Key"] == "langsmith-test-key"


def test_datadog_export_api_uses_span_kind_headers_and_page_cursor() -> None:
    record = {
        "id": "event-1",
        "attributes": {
            "trace_id": "trace-1",
            "span_id": "span-1",
            "timestamp": "2026-08-01T12:01:00Z",
            "duration": 100_000_000,
            "meta": {
                "span": {"kind": "llm"},
                "model_name": "gpt-4o-mini",
                "input": {"value": "question"},
                "output": {"value": "answer"},
            },
            "metrics": {"input_tokens": 3, "output_tokens": 2},
        },
    }
    responses = [
        (200, {}, {"data": [record], "meta": {"page": {"after": "dd-next"}}}),
        (200, {}, {"data": [], "meta": {"page": {}}}),
    ]
    with _json_server(responses) as (base_url, state):
        results = list(
            DatadogApiSource(
                client=JsonHttpClient(),
                context=_context("datadog"),
                base_url=base_url,
                api_key="dd-api-test",
                app_key="dd-app-test",
                start_time=START,
                end_time=END,
            )
        )

    assert len(results) == 1
    assert "filter%5Bspan_kind%5D=llm" in state.requests[0]["path"]
    assert "page%5Bcursor%5D=dd-next" in state.requests[1]["path"]
    assert state.requests[0]["headers"]["Dd-Api-Key"] == "dd-api-test"
    assert state.requests[0]["headers"]["Dd-Application-Key"] == "dd-app-test"


def test_phoenix_trace_api_flattens_spans_and_paginates() -> None:
    span = {
        "context": {"trace_id": "trace-1", "span_id": "span-1"},
        "span_kind": "LLM",
        "start_time": "2026-08-01T12:01:00Z",
        "end_time": "2026-08-01T12:01:00.100Z",
        "attributes": {
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4o-mini",
            "input.value": "question",
            "output.value": "answer",
        },
    }
    responses = [
        (
            200,
            {},
            {
                "data": [{"trace_id": "trace-1", "spans": [span]}],
                "meta": {"next_cursor": "px-next"},
            },
        ),
        (200, {}, {"data": [], "meta": {}}),
    ]
    with _json_server(responses) as (base_url, state):
        results = list(
            PhoenixApiSource(
                client=JsonHttpClient(),
                context=_context("phoenix"),
                base_url=base_url,
                project="support project",
                api_key="phoenix-test-key",
                start_time=START,
                end_time=END,
            )
        )

    assert len(results) == 1
    assert state.requests[0]["path"].startswith("/v1/projects/support%20project/traces?")
    assert "cursor=px-next" in state.requests[1]["path"]
    assert state.requests[0]["headers"]["Authorization"] == "Bearer phoenix-test-key"


def test_opik_search_api_posts_workspace_and_last_id() -> None:
    record = {
        "id": "span-1",
        "trace_id": "trace-1",
        "type": "llm",
        "start_time": "2026-08-01T12:01:00Z",
        "end_time": "2026-08-01T12:01:00.100Z",
        "model": "gpt-4o-mini",
        "input": "question",
        "output": "answer",
    }
    responses = [
        (200, {}, (json.dumps(record) + "\r\n").encode()),
        (200, {}, b""),
    ]
    with _json_server(responses) as (base_url, state):
        results = list(
            OpikApiSource(
                client=JsonHttpClient(),
                context=_context("opik"),
                base_url=base_url,
                project="support",
                api_key="opik-test-key",
                workspace="workspace-test",
                start_time=START,
                end_time=END,
                page_size=1,
            )
        )

    assert len(results) == 1
    assert state.requests[0]["path"] == "/v1/private/spans/search"
    assert state.requests[0]["body"]["truncate"] is False
    assert state.requests[0]["body"]["from_time"] == START.isoformat()
    assert state.requests[1]["body"]["last_retrieved_id"] == "span-1"
    assert state.requests[0]["headers"]["Comet-Workspace"] == "workspace-test"
    assert state.requests[0]["headers"]["Authorization"] == "opik-test-key"


def test_cursor_cycle_fails_instead_of_looping_forever() -> None:
    responses = [
        (200, {}, {"data": [], "meta": {"cursor": "same"}}),
        (200, {}, {"data": [], "meta": {"cursor": "same"}}),
    ]
    with _json_server(responses) as (base_url, _state):
        source = LangfuseApiSource(
            client=JsonHttpClient(),
            context=_context("langfuse"),
            base_url=base_url,
            public_key="public-test",
            secret_key="secret-test",
            start_time=START,
            end_time=END,
        )

        with pytest.raises(ValueError, match="cursor repeated"):
            list(source)


def test_http_transport_rejects_redirect_without_forwarding_authorization() -> None:
    responses = [(302, {"Location": "/credential-sink"}, {})]
    with _json_server(responses) as (base_url, state):
        with pytest.raises(TelemetryHttpError) as raised:
            JsonHttpClient().request_json(
                "GET",
                f"{base_url}/redirect",
                headers={"Authorization": "Bearer redirect-canary"},
            )

    assert len(state.requests) == 1
    assert state.requests[0]["path"] == "/redirect"
    assert "redirect-canary" not in str(raised.value)


def test_http_transport_bounds_response_and_does_not_echo_body() -> None:
    responses = [
        (200, {}, b"{" + b'"secret":"response-canary",' + b'"pad":"' + b"x" * 1100 + b'"}')
    ]
    with _json_server(responses) as (base_url, _state):
        with pytest.raises(TelemetryHttpError) as raised:
            JsonHttpClient(max_response_bytes=1024).request_json("GET", base_url)

    assert "response-canary" not in str(raised.value)
    assert "response too large" in str(raised.value)


def test_http_transport_rejects_plaintext_remote_and_url_credentials() -> None:
    client = JsonHttpClient()

    with pytest.raises(ValueError, match="must use HTTPS"):
        client.request_json("GET", "http://telemetry.invalid/api")
    with pytest.raises(ValueError, match="must not include credentials"):
        client.request_json("GET", "https://user:secret@telemetry.invalid/api")
