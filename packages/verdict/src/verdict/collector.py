"""Authenticated, bounded remote ingestion for full Agent capture records."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from verdict.agent_transport import MAX_RECORD_BYTES, StorageCaptureSink, decode_capture_record
from verdict.collector_receipts import (
    INGEST_ACK_SCHEMA,
    MAX_ACK_BYTES,
    MAX_ACK_RESULTS,
    CollectorReceipt,
    PostgresReceiptStore,
    ReceiptBusy,
    ReceiptConflict,
    ReceiptCorrupt,
    ReceiptStore,
    validate_transport_id,
)
from verdict.evidence import AgentCaptureBatch
from verdict.schema import Trace
from verdict.storage.base import Storage

MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_BATCH_RECORDS = MAX_ACK_RESULTS
MAX_BATCH_PROCESS_SECONDS = 60.0
DEFAULT_MAX_IN_FLIGHT = 16
_MAX_SECRET_BYTES = 512


class BatchBoundaryError(ValueError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class CollectorService:
    """Apply one delivery through receipts and the canonical Agent sink."""

    def __init__(
        self,
        storage: Storage,
        receipts: ReceiptStore,
        *,
        tenant_id: str,
    ) -> None:
        _validate_tenant_id(tenant_id)
        self._storage = storage
        self._receipts = receipts
        self._tenant_id = tenant_id
        self._sink = StorageCaptureSink(storage)
        self._closed = False

    def ingest(self, batch_id: str, producer_id: str, body: bytes) -> bytes:
        validate_transport_id(batch_id, field="batch_id")
        validate_transport_id(producer_id, field="producer_id")
        lines = _batch_lines(body)
        digest = hashlib.sha256(body).hexdigest()
        return self._receipts.process(
            self._tenant_id,
            batch_id,
            producer_id,
            digest,
            lambda: self._apply(batch_id, lines),
        )

    def _apply(self, batch_id: str, lines: tuple[bytes, ...]) -> bytes:
        deadline = time.monotonic() + MAX_BATCH_PROCESS_SECONDS
        results: list[dict[str, object]] = []
        accepted = 0
        for index, raw in enumerate(lines):
            _enforce_deadline(deadline)
            record_digest = hashlib.sha256(raw).hexdigest()
            error: str | None = None
            if not raw or len(raw) > MAX_RECORD_BYTES:
                error = "invalid_record"
            else:
                try:
                    record = decode_capture_record(raw)
                except (RecursionError, ValueError):
                    error = "invalid_record"
                else:
                    if record.kind != "agent":
                        error = "unsupported_record_kind"
                    else:
                        assert record.batch is not None
                        try:
                            batch, traces = _scope_agent_record(
                                record.batch,
                                record.traces,
                                self._tenant_id,
                            )
                            self._sink.capture_agent(batch, traces)
                        except ValueError:
                            error = "evidence_conflict"
            _enforce_deadline(deadline)
            if error is None:
                accepted += 1
            results.append(
                {
                    "index": index,
                    "recordDigest": record_digest,
                    "status": "accepted" if error is None else "rejected",
                    "error": error,
                }
            )
        payload = {
            "schema": INGEST_ACK_SCHEMA,
            "batchId": batch_id,
            "accepted": accepted,
            "rejected": len(results) - accepted,
            "results": results,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        _enforce_deadline(deadline)
        if len(encoded) > MAX_ACK_BYTES:
            raise ReceiptCorrupt("collector acknowledgement exceeds its bound")
        return encoded

    def get_receipt(self, batch_id: str) -> CollectorReceipt | None:
        validate_transport_id(batch_id, field="batch_id")
        return self._receipts.get(self._tenant_id, batch_id)

    def ready(self) -> bool:
        return self._receipts.ready()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._receipts.close()
        finally:
            self._storage.close()


def _batch_lines(body: bytes) -> tuple[bytes, ...]:
    if not isinstance(body, bytes) or not body:
        raise BatchBoundaryError("empty_batch")
    if len(body) > MAX_BATCH_BYTES:
        raise BatchBoundaryError("batch_too_large", status_code=413)
    lines = body.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    if not lines:
        raise BatchBoundaryError("empty_batch")
    if len(lines) > MAX_BATCH_RECORDS:
        raise BatchBoundaryError("too_many_records", status_code=413)
    return tuple(lines)


def _enforce_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("collector batch processing deadline exceeded")


def _scope_agent_record(
    batch: AgentCaptureBatch,
    traces: tuple[Trace, ...],
    tenant_id: str,
) -> tuple[AgentCaptureBatch, tuple[Trace, ...]]:
    scoped = replace(
        batch,
        session=replace(batch.session, tenant_id=tenant_id),
        run=replace(batch.run, tenant_id=tenant_id),
    )
    return scoped, tuple(replace(trace, tenant_id=tenant_id) for trace in traces)


class _AdmissionGate:
    def __init__(self, maximum: int) -> None:
        _validate_in_flight(maximum)
        self._maximum = maximum
        self._active = 0
        self._accepting = True
        self._condition = threading.Condition()

    def enter(self) -> bool:
        with self._condition:
            if not self._accepting or self._active >= self._maximum:
                return False
            self._active += 1
            return True

    def leave(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def close_and_wait(self) -> None:
        with self._condition:
            self._accepting = False
            while self._active:
                self._condition.wait()


def _validate_tenant_id(tenant_id: str) -> None:
    if (
        not isinstance(tenant_id, str)
        or not tenant_id
        or "\x00" in tenant_id
        or len(tenant_id.encode("utf-8")) > 256
    ):
        raise ValueError("collector tenant_id must be bounded text")


def _validate_in_flight(maximum: int) -> None:
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 256:
        raise ValueError("collector in-flight limit is invalid")


def _raw_header_values(request: Request, name: bytes) -> list[bytes]:
    wanted = name.lower()
    return [value for key, value in request.scope.get("headers", ()) if key.lower() == wanted]


def _one_header(request: Request, name: bytes, *, maximum: int = 512) -> str | None:
    values = _raw_header_values(request, name)
    if len(values) > 1:
        raise BatchBoundaryError("duplicate_header")
    if not values:
        return None
    if len(values[0]) > maximum:
        raise BatchBoundaryError("invalid_header")
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise BatchBoundaryError("invalid_header") from exc


def _valid_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or any(ord(char) < 33 or ord(char) > 126 for char in secret):
        raise ValueError("collector API key must be bounded printable ASCII")
    encoded = secret.encode("ascii")
    if not 16 <= len(encoded) <= _MAX_SECRET_BYTES:
        raise ValueError("collector API key must be 16-512 ASCII bytes")
    return encoded


def _authorized(request: Request, secret_digest: bytes) -> bool:
    values = _raw_header_values(request, b"authorization")
    if len(values) != 1:
        return False
    value = values[0]
    if not value.startswith(b"Bearer ") or value.count(b" ") != 1:
        return False
    candidate = value[7:]
    if not candidate or len(candidate) > _MAX_SECRET_BYTES:
        return False
    return hmac.compare_digest(hashlib.sha256(candidate).digest(), secret_digest)


def _media_type_allowed(value: str | None) -> bool:
    if value is None:
        return False
    parts = [part.strip().lower() for part in value.split(";")]
    return parts[0] == "application/x-ndjson" and (
        len(parts) == 1 or (len(parts) == 2 and parts[1] == "charset=utf-8")
    )


def _json_error(code: str, status_code: int, *, retryable: bool = False) -> JSONResponse:
    headers = {"Retry-After": "1"} if retryable else None
    return JSONResponse({"error": code}, status_code=status_code, headers=headers)


def create_collector_app(
    service: CollectorService,
    *,
    api_key: str,
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    close_on_shutdown: bool = False,
) -> FastAPI:
    """Create the collector ASGI app without coupling it to the dashboard."""
    secret_digest = hashlib.sha256(_valid_secret(api_key)).digest()
    admission = _AdmissionGate(max_in_flight)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if close_on_shutdown:
            await anyio.to_thread.run_sync(admission.close_and_wait)
            await anyio.to_thread.run_sync(service.close)

    app = FastAPI(title="Verdict Collector", version="1", lifespan=lifespan)

    @app.get("/healthz")
    async def health() -> Response:
        ready = await anyio.to_thread.run_sync(service.ready)
        if not ready:
            return _json_error("unavailable", 503, retryable=True)
        return JSONResponse({"status": "ready"})

    @app.post("/v1/ingest")
    async def ingest(request: Request) -> Response:
        if not _authorized(request, secret_digest):
            return _json_error("unauthorized", 401)
        try:
            content_type = _one_header(request, b"content-type")
            content_encoding = _one_header(request, b"content-encoding")
            batch_id = _one_header(request, b"x-verdict-batch-id")
            producer_id = _one_header(request, b"x-verdict-producer-id")
            content_length = _one_header(request, b"content-length", maximum=20)
        except BatchBoundaryError as exc:
            return _json_error(exc.code, exc.status_code)
        if not _media_type_allowed(content_type):
            return _json_error("unsupported_media_type", 415)
        if content_encoding is not None:
            content_encoding = content_encoding.strip().lower()
        if content_encoding not in {None, "identity"}:
            return _json_error("unsupported_content_encoding", 415)
        try:
            if batch_id is None or producer_id is None:
                return _json_error("invalid_identifier", 400)
            validate_transport_id(batch_id, field="batch_id")
            validate_transport_id(producer_id, field="producer_id")
        except ValueError:
            return _json_error("invalid_identifier", 400)
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdigit():
                return _json_error("invalid_content_length", 400)
            if int(content_length) > MAX_BATCH_BYTES:
                return _json_error("batch_too_large", 413)
        if not admission.enter():
            return _json_error("busy", 503, retryable=True)
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(chunk) > MAX_BATCH_BYTES - len(body):
                    return _json_error("batch_too_large", 413)
                body.extend(chunk)
            try:
                response = await anyio.to_thread.run_sync(
                    service.ingest,
                    batch_id,
                    producer_id,
                    bytes(body),
                )
            except BatchBoundaryError as exc:
                return _json_error(exc.code, exc.status_code)
            except ReceiptConflict:
                return _json_error("batch_conflict", 409)
            except ReceiptBusy:
                return _json_error("busy", 503, retryable=True)
            except ReceiptCorrupt:
                return _json_error("unavailable", 503, retryable=True)
            except Exception:
                return _json_error("unavailable", 503, retryable=True)
            return Response(content=response, media_type="application/json")
        finally:
            admission.leave()

    @app.get("/v1/ingest/{batch_id}")
    async def receipt(request: Request, batch_id: str) -> Response:
        if not _authorized(request, secret_digest):
            return _json_error("unauthorized", 401)
        try:
            item = await anyio.to_thread.run_sync(
                service.get_receipt,
                batch_id,
            )
        except ValueError:
            return _json_error("invalid_identifier", 400)
        except ReceiptBusy:
            return _json_error("busy", 503, retryable=True)
        except ReceiptCorrupt:
            return _json_error("unavailable", 503, retryable=True)
        except Exception:
            return _json_error("unavailable", 503, retryable=True)
        if item is None:
            return _json_error("not_found", 404)
        if item.state == "pending":
            return JSONResponse(
                {"schema": INGEST_ACK_SCHEMA, "batchId": batch_id, "state": "pending"},
                status_code=202,
                headers={"Retry-After": "1"},
            )
        assert item.response_body is not None
        return Response(content=item.response_body, media_type="application/json")

    return app


def _bounded_postgres_dsn(dsn: str) -> str:
    try:
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
    except ImportError as exc:
        raise ImportError(
            'Verdict collector requires `pip install "cognifity-verdict[postgres]"`'
        ) from exc
    values: dict[str, Any] = conninfo_to_dict(dsn)
    values.setdefault("connect_timeout", "5")
    existing = str(values.get("options", "")).strip()
    limits = "-c lock_timeout=5000 -c statement_timeout=60000"
    values["options"] = f"{existing} {limits}".strip()
    return make_conninfo(**values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the Verdict Agent collector.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--storage", default=os.environ.get("VERDICT_STORAGE"))
    parser.add_argument("--tenant-id", default=os.environ.get("VERDICT_TENANT_ID"))
    parser.add_argument("--api-key-env", default="VERDICT_COLLECTOR_API_KEY")
    parser.add_argument("--max-in-flight", type=int, default=DEFAULT_MAX_IN_FLIGHT)
    args = parser.parse_args(argv)
    if not isinstance(args.storage, str) or not args.storage.startswith(
        ("postgresql://", "postgres://")
    ):
        parser.error("--storage must be a PostgreSQL DSN")
    if not args.tenant_id:
        parser.error("--tenant-id or VERDICT_TENANT_ID is required")
    secret = os.environ.get(args.api_key_env)
    if secret is None:
        parser.error(f"collector API key environment variable {args.api_key_env!r} is missing")
    try:
        _validate_tenant_id(args.tenant_id)
        _valid_secret(secret)
        _validate_in_flight(args.max_in_flight)
    except ValueError as exc:
        parser.error(str(exc))

    import uvicorn

    from verdict.storage.postgres import PostgresStorage

    dsn = _bounded_postgres_dsn(args.storage)
    storage = PostgresStorage(dsn, min_pool=1, max_pool=8)
    receipts: PostgresReceiptStore | None = None
    service: CollectorService | None = None
    try:
        receipts = PostgresReceiptStore(dsn, min_pool=1, max_pool=4)
        service = CollectorService(
            storage,
            receipts,
            tenant_id=args.tenant_id,
        )
        app = create_collector_app(
            service,
            api_key=secret,
            max_in_flight=args.max_in_flight,
            close_on_shutdown=True,
        )
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        if service is not None:
            service.close()
        else:
            try:
                if receipts is not None:
                    receipts.close()
            finally:
                storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
