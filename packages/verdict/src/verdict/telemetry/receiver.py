"""Loopback OTLP/HTTP receiver that writes through Verdict's Storage port."""

from __future__ import annotations

import gzip
import io
import json
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from verdict.storage.base import Storage
from verdict.telemetry.model import ImportContext
from verdict.telemetry.otlp import decode_otlp_http, map_otlp_payload
from verdict.telemetry.runner import ImportRunError, import_into_storage

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def _bounded_gzip_decompress(body: bytes, maximum: int) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            decoded = stream.read(maximum + 1)
    except (OSError, EOFError) as exc:
        raise ValueError("invalid gzip body") from exc
    if len(decoded) > maximum:
        raise ValueError("decompressed OTLP request exceeds size limit")
    return decoded


@dataclass
class OtlpHttpReceiver:
    storage: Storage
    context: ImportContext
    host: str = "127.0.0.1"
    port: int = 4318
    max_request_bytes: int = 16 * 1024 * 1024
    _server: ThreadingHTTPServer | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.host not in _LOOPBACK_HOSTS:
            raise ValueError(
                "the built-in OTLP receiver is loopback-only; use an authenticated TLS "
                "collector or reverse proxy for remote traffic"
            )
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be in [0,65535]")
        if not 1024 <= self.max_request_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_request_bytes must be in [1024,64MiB]")

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return self.port
        return int(self._server.server_address[1])

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "VerdictOTLP/1"
            sys_version = ""

            def log_message(self, format: str, *args: object) -> None:
                return

            def _respond(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _error(self, status: int, code: str) -> None:
                body = json.dumps({"error": code}, separators=(",", ":")).encode()
                self._respond(status, "application/json", body)

            def do_POST(self) -> None:
                if urlsplit(self.path).path != "/v1/traces":
                    self._error(404, "not_found")
                    return
                if self.headers.get("Transfer-Encoding"):
                    self._error(411, "content_length_required")
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._error(411, "content_length_required")
                    return
                if length < 0 or length > receiver.max_request_bytes:
                    self._error(413, "request_too_large")
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    self._error(400, "incomplete_request")
                    return
                encoding = self.headers.get("Content-Encoding", "identity").lower()
                try:
                    if encoding == "gzip":
                        body = _bounded_gzip_decompress(body, receiver.max_request_bytes)
                    elif encoding not in {"", "identity"}:
                        self._error(415, "unsupported_content_encoding")
                        return
                    content_type = self.headers.get("Content-Type", "")
                    payload = decode_otlp_http(body, content_type)
                    summary = import_into_storage(
                        map_otlp_payload(payload, receiver.context), receiver.storage
                    )
                except ImportRunError:
                    self._error(500, "storage_failure")
                    return
                except RuntimeError:
                    self._error(415, "protobuf_support_not_installed")
                    return
                except ValueError:
                    self._error(400, "invalid_otlp_request")
                    return
                rejected = summary.skipped - summary.skip_reasons.get("non_llm_span", 0)
                if content_type.split(";", 1)[0].strip().lower() in {
                    "application/x-protobuf",
                    "application/protobuf",
                }:
                    response_body = _protobuf_response(rejected)
                    response_type = "application/x-protobuf"
                else:
                    response: dict[str, Any] = {}
                    if rejected:
                        response["partialSuccess"] = {
                            "rejectedSpans": rejected,
                            "errorMessage": "one or more LLM spans lacked required identity or time",
                        }
                    response_body = json.dumps(response, separators=(",", ":")).encode()
                    response_type = "application/json"
                self._respond(200, response_type, response_body)

        return Handler

    def serve_forever(self) -> None:
        if self._server is not None:
            raise RuntimeError("OTLP receiver is already running")
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler_type())
        self._server.daemon_threads = True
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()
            self._server = None

    def start(self) -> None:
        """Bind without serving; tests and embedding callers may own the thread."""
        if self._server is not None:
            raise RuntimeError("OTLP receiver is already running")
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler_type())
        self._server.daemon_threads = True

    def run_started_server(self) -> None:
        if self._server is None:
            raise RuntimeError("OTLP receiver is not started")
        self._server.serve_forever()

    def shutdown(self) -> None:
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._server is server:
            self._server = None


def _protobuf_response(rejected: int) -> bytes:
    try:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTracePartialSuccess,
            ExportTraceServiceResponse,
        )
    except ImportError as exc:  # pragma: no cover - isolated install path
        raise RuntimeError("OTLP protobuf support is not installed") from exc
    response = ExportTraceServiceResponse()
    if rejected:
        response.partial_success.CopyFrom(
            ExportTracePartialSuccess(
                rejected_spans=rejected,
                error_message="one or more LLM spans lacked required identity or time",
            )
        )
    return response.SerializeToString()
