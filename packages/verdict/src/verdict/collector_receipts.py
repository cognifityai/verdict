"""Idempotent delivery receipts for the remote capture collector."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

INGEST_ACK_SCHEMA = "verdict-ingest-ack-v1"
MAX_ACK_BYTES = 256 * 1024
MAX_ACK_RESULTS = 500
_TRANSPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RECORD_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ERRORS = frozenset({"invalid_record", "unsupported_record_kind", "evidence_conflict"})


class ReceiptConflict(ValueError):
    """A batch identity was reused with different immutable inputs."""


class ReceiptBusy(RuntimeError):
    """A bounded receipt or pool lock could not be acquired."""


class ReceiptCorrupt(RuntimeError):
    """A stored receipt violates its persistence contract."""


@dataclass(frozen=True)
class CollectorReceipt:
    tenant_id: str
    batch_id: str
    producer_id: str
    payload_sha256: str
    state: Literal["pending", "completed"]
    first_received_at: datetime
    last_attempt_at: datetime
    attempt_count: int
    completed_at: datetime | None = None
    response_body: bytes | None = None


def validate_transport_id(value: str, *, field: str) -> None:
    if not isinstance(value, str) or _TRANSPORT_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded ASCII identifier")


def _require_identity(receipt: CollectorReceipt, producer_id: str, payload_sha256: str) -> None:
    if receipt.producer_id != producer_id or receipt.payload_sha256 != payload_sha256:
        raise ReceiptConflict("collector batch identity conflict")


def _validate_receipt(receipt: CollectorReceipt) -> None:
    try:
        validate_transport_id(receipt.batch_id, field="batch_id")
        validate_transport_id(receipt.producer_id, field="producer_id")
    except ValueError as exc:
        raise ReceiptCorrupt("collector receipt identity is invalid") from exc
    if not receipt.tenant_id or len(receipt.tenant_id.encode("utf-8")) > 256:
        raise ReceiptCorrupt("collector receipt tenant is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", receipt.payload_sha256):
        raise ReceiptCorrupt("collector receipt digest is invalid")
    if receipt.attempt_count < 0 or (receipt.state == "completed" and receipt.attempt_count == 0):
        raise ReceiptCorrupt("collector receipt attempt count is invalid")
    if (
        receipt.first_received_at.tzinfo is None
        or receipt.last_attempt_at.tzinfo is None
        or receipt.first_received_at > receipt.last_attempt_at
    ):
        raise ReceiptCorrupt("collector receipt timestamps are invalid")
    completed = receipt.state == "completed"
    if completed != (receipt.completed_at is not None and receipt.response_body is not None):
        raise ReceiptCorrupt("collector receipt state is inconsistent")
    if receipt.state not in {"pending", "completed"}:
        raise ReceiptCorrupt("collector receipt state is invalid")
    if completed and receipt.completed_at != receipt.last_attempt_at:
        raise ReceiptCorrupt("collector receipt completion time is invalid")
    if receipt.response_body is not None and not 0 < len(receipt.response_body) <= MAX_ACK_BYTES:
        raise ReceiptCorrupt("collector receipt response is invalid")
    if receipt.response_body is not None:
        _validate_acknowledgement(receipt.response_body, receipt.batch_id)


def _validate_acknowledgement(response: bytes, batch_id: str) -> None:
    try:
        payload = json.loads(response)
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ReceiptCorrupt("collector acknowledgement is invalid") from exc
    fields = {"schema", "batchId", "accepted", "rejected", "results"}
    if canonical != response or not isinstance(payload, dict) or set(payload) != fields:
        raise ReceiptCorrupt("collector acknowledgement shape is invalid")
    accepted = payload["accepted"]
    rejected = payload["rejected"]
    results = payload["results"]
    if (
        payload["schema"] != INGEST_ACK_SCHEMA
        or payload["batchId"] != batch_id
        or type(accepted) is not int
        or accepted < 0
        or type(rejected) is not int
        or rejected < 0
        or not isinstance(results, list)
        or not 1 <= len(results) <= MAX_ACK_RESULTS
        or accepted + rejected != len(results)
    ):
        raise ReceiptCorrupt("collector acknowledgement values are invalid")
    if not all(_valid_result(result, index) for index, result in enumerate(results)):
        raise ReceiptCorrupt("collector acknowledgement result is invalid")
    if sum(result["status"] == "accepted" for result in results) != accepted:
        raise ReceiptCorrupt("collector acknowledgement counts are invalid")


def _valid_result(result: object, index: int) -> bool:
    fields = {"index", "recordDigest", "status", "error"}
    if not isinstance(result, dict) or set(result) != fields:
        return False
    digest, status, error = result["recordDigest"], result["status"], result["error"]
    return (
        type(result["index"]) is int
        and result["index"] == index
        and isinstance(digest, str)
        and _RECORD_DIGEST.fullmatch(digest) is not None
        and isinstance(status, str)
        and status in {"accepted", "rejected"}
        and (
            error is None
            if status == "accepted"
            else isinstance(error, str) and error in _RECORD_ERRORS
        )
    )


class ReceiptStore(Protocol):
    def process(
        self,
        tenant_id: str,
        batch_id: str,
        producer_id: str,
        payload_sha256: str,
        processor: Callable[[], bytes],
    ) -> bytes: ...

    def get(self, tenant_id: str, batch_id: str) -> CollectorReceipt | None: ...

    def ready(self) -> bool: ...

    def close(self) -> None: ...


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS collector_receipts (
    tenant_id TEXT NOT NULL CHECK (
        octet_length(tenant_id) BETWEEN 1 AND 256
    ),
    batch_id TEXT NOT NULL CHECK (
        batch_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$'
    ),
    producer_id TEXT NOT NULL CHECK (
        producer_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$'
    ),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{{64}}$'),
    state TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
    first_received_at TIMESTAMPTZ NOT NULL,
    last_attempt_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    attempt_count BIGINT NOT NULL CHECK (attempt_count >= 0),
    response_body BYTEA CHECK (
        response_body IS NULL OR octet_length(response_body) BETWEEN 1 AND {MAX_ACK_BYTES}
    ),
    PRIMARY KEY (tenant_id, batch_id),
    CHECK (
        (state = 'pending' AND completed_at IS NULL AND response_body IS NULL)
        OR
        (state = 'completed' AND completed_at IS NOT NULL AND response_body IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_collector_receipts_producer_attempt
    ON collector_receipts(tenant_id, producer_id, last_attempt_at DESC);
"""


class PostgresReceiptStore:
    def __init__(
        self,
        dsn: str,
        *,
        min_pool: int = 1,
        max_pool: int = 4,
        pool_timeout_seconds: float = 5.0,
    ) -> None:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise ImportError(
                'PostgresReceiptStore requires `pip install "psycopg[binary,pool]"`'
            ) from exc
        if not 1 <= min_pool <= max_pool <= 32 or not 0 < pool_timeout_seconds <= 30:
            raise ValueError("collector receipt pool limits are invalid")
        self._pool_timeout = pool_timeout_seconds
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_pool,
            max_size=max_pool,
            timeout=pool_timeout_seconds,
            max_waiting=16,
            kwargs={"autocommit": True},
            open=True,
        )
        try:
            with self._connection() as conn, conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(current_database() || ':verdict-collector-schema', 0))"
                )
                cur.execute(_SCHEMA)
        except BaseException:
            self._pool.close()
            raise

    def _connection(self):
        return self._pool.connection(timeout=self._pool_timeout)

    def process(
        self,
        tenant_id: str,
        batch_id: str,
        producer_id: str,
        payload_sha256: str,
        processor: Callable[[], bytes],
    ) -> bytes:
        self._ensure_pending(tenant_id, batch_id, producer_id, payload_sha256)
        failure: BaseException | None = None
        response: bytes | None = None
        try:
            with self._connection() as conn, conn.transaction(), conn.cursor() as cur:
                try:
                    cur.execute(
                        """SELECT tenant_id,batch_id,producer_id,payload_sha256,state,
                                  first_received_at,last_attempt_at,attempt_count,
                                  completed_at,response_body
                           FROM collector_receipts
                           WHERE tenant_id=%s AND batch_id=%s
                           FOR UPDATE NOWAIT""",
                        (tenant_id, batch_id),
                    )
                except self._busy_errors() as exc:
                    raise ReceiptBusy("collector receipt is busy") from exc
                row = cur.fetchone()
                if row is None:
                    raise ReceiptCorrupt("collector receipt disappeared")
                receipt = self._from_row(row)
                _require_identity(receipt, producer_id, payload_sha256)
                if receipt.state == "completed":
                    assert receipt.response_body is not None
                    return receipt.response_body
                attempt = receipt.attempt_count + 1
                cur.execute(
                    """UPDATE collector_receipts
                       SET last_attempt_at=now(),attempt_count=%s
                       WHERE tenant_id=%s AND batch_id=%s""",
                    (attempt, tenant_id, batch_id),
                )
                try:
                    candidate = processor()
                    _validate_acknowledgement(candidate, batch_id)
                    response = bytes(candidate)
                except BaseException as exc:
                    failure = exc
                if failure is None:
                    cur.execute(
                        """UPDATE collector_receipts
                           SET state='completed',last_attempt_at=now(),completed_at=now(),
                               response_body=%s
                           WHERE tenant_id=%s AND batch_id=%s""",
                        (response, tenant_id, batch_id),
                    )
        except self._pool_errors() as exc:
            raise ReceiptBusy("collector database is busy") from exc
        if failure is not None:
            raise failure
        assert response is not None
        return response

    def _ensure_pending(
        self,
        tenant_id: str,
        batch_id: str,
        producer_id: str,
        payload_sha256: str,
    ) -> None:
        try:
            with self._connection() as conn, conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM collector_receipts WHERE tenant_id=%s AND batch_id=%s",
                    (tenant_id, batch_id),
                )
                if cur.fetchone() is not None:
                    return
                cur.execute(
                    """INSERT INTO collector_receipts(
                           tenant_id,batch_id,producer_id,payload_sha256,state,
                           first_received_at,last_attempt_at,attempt_count
                       ) VALUES (%s,%s,%s,%s,'pending',now(),now(),0)
                       ON CONFLICT(tenant_id,batch_id) DO NOTHING""",
                    (tenant_id, batch_id, producer_id, payload_sha256),
                )
        except self._pool_errors() as exc:
            raise ReceiptBusy("collector database is busy") from exc

    @staticmethod
    def _busy_errors() -> tuple[type[BaseException], ...]:
        from psycopg import errors

        return (errors.LockNotAvailable, errors.QueryCanceled)

    @staticmethod
    def _pool_errors() -> tuple[type[BaseException], ...]:
        from psycopg import errors
        from psycopg_pool import PoolTimeout, TooManyRequests

        return (errors.LockNotAvailable, errors.QueryCanceled, PoolTimeout, TooManyRequests)

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> CollectorReceipt:
        response = row[9]
        receipt = CollectorReceipt(
            tenant_id=str(row[0]),
            batch_id=str(row[1]),
            producer_id=str(row[2]),
            payload_sha256=str(row[3]),
            state=str(row[4]),  # type: ignore[arg-type]
            first_received_at=row[5],  # type: ignore[arg-type]
            last_attempt_at=row[6],  # type: ignore[arg-type]
            attempt_count=int(row[7]),
            completed_at=row[8],  # type: ignore[arg-type]
            response_body=bytes(response) if response is not None else None,
        )
        _validate_receipt(receipt)
        return receipt

    def get(self, tenant_id: str, batch_id: str) -> CollectorReceipt | None:
        try:
            with self._connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT tenant_id,batch_id,producer_id,payload_sha256,state,
                              first_received_at,last_attempt_at,attempt_count,
                              completed_at,response_body
                       FROM collector_receipts
                       WHERE tenant_id=%s AND batch_id=%s""",
                    (tenant_id, batch_id),
                )
                row = cur.fetchone()
        except self._pool_errors() as exc:
            raise ReceiptBusy("collector database is busy") from exc
        return self._from_row(row) if row is not None else None

    def ready(self) -> bool:
        try:
            with self._connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() == (1,)
        except Exception:
            return False

    def close(self) -> None:
        self._pool.close()
