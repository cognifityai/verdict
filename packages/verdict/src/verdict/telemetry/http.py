"""Bounded standard-library JSON HTTP transport for telemetry readers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class TelemetryHttpError(RuntimeError):
    """Safe HTTP failure that never includes headers, bodies, or query values."""

    def __init__(self, status: int | None, method: str, url: str, reason: str) -> None:
        parsed = urlsplit(url)
        safe_target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        self.status = status
        self.method = method
        self.target = safe_target
        super().__init__(
            f"telemetry HTTP {method} {safe_target} failed ({status or 'transport'}: {reason})"
        )


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True)
class JsonHttpClient:
    timeout_seconds: float = 30.0
    max_response_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be in (0,300]")
        if not 1024 <= self.max_response_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_response_bytes must be in [1024,64MiB]")

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        raw = self._request_bytes(
            method,
            url,
            headers=headers,
            query=query,
            body=body,
            accept="application/json",
        )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise TelemetryHttpError(None, method.upper(), url, "invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TelemetryHttpError(None, method.upper(), url, "JSON root is not an object")
        return payload

    def request_json_lines(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        """Read a bounded CRLF/NDJSON object stream such as Opik search."""
        raw = self._request_bytes(
            method,
            url,
            headers=headers,
            query=query,
            body=body,
            accept="application/octet-stream",
        )
        rows: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            if len(line) > 16 * 1024 * 1024:
                raise TelemetryHttpError(None, method.upper(), url, "stream item too large")
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise TelemetryHttpError(None, method.upper(), url, "invalid JSON stream") from exc
            if not isinstance(row, dict):
                raise TelemetryHttpError(None, method.upper(), url, "stream item is not an object")
            rows.append(row)
        return rows

    def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        query: dict[str, object] | None,
        body: dict[str, object] | None,
        accept: str,
    ) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("telemetry URL must be absolute HTTP(S)")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("telemetry URL must not include credentials")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("remote telemetry APIs must use HTTPS")
        query_text = urlencode(query or {}, doseq=True)
        target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query_text, ""))
        request_headers = {"Accept": accept, **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(target, data=data, headers=request_headers, method=method.upper())
        try:
            with build_opener(_NoRedirects()).open(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            raise TelemetryHttpError(exc.code, method.upper(), url, "HTTP error") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TelemetryHttpError(None, method.upper(), url, type(exc).__name__) from exc
        if len(raw) > self.max_response_bytes:
            raise TelemetryHttpError(None, method.upper(), url, "response too large")
        return raw
