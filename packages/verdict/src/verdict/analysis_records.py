"""Durable contracts for deterministic analysis and notification delivery.

Both records are terminal, immutable facts.  Analysis never persists a
``pending`` row, and notification attempts are appended only after an HTTP
attempt has a terminal outcome.  This keeps crash recovery honest: callers
may retry work, but storage never pretends unfinished work completed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from verdict.redaction import redact_structure

_MAX_RECORD_BYTES = 4 * 1024 * 1024


class AnalysisRunStatus(str, Enum):
    COMPLETED = "completed"
    ERROR = "error"


class DeliveryOutcome(str, Enum):
    DELIVERED = "delivered"
    FAILED = "failed"


def _bounded_text(name: str, value: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be bounded text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError(f"{name} must be bounded text") from exc
    if size > maximum:
        raise ValueError(f"{name} must be bounded text")


def _digest(name: str, value: str) -> None:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def _safe_mapping(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    safe = redact_structure(dict(value))
    if not isinstance(safe, dict):
        raise ValueError(f"{name} exceeds the supported structure bounds")
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > _MAX_RECORD_BYTES:
        raise ValueError(f"{name} exceeds the {_MAX_RECORD_BYTES}-byte limit")
    return safe


@dataclass(frozen=True)
class DeterministicAnalysisRun:
    analysis_id: str
    tenant_id: str
    scope_key: str
    cutoff: datetime
    completed_at: datetime
    status: AnalysisRunStatus
    analyzer_version: str
    input_fingerprint: str
    result: dict[str, Any]

    def __post_init__(self) -> None:
        _digest("analysis_id", self.analysis_id)
        _bounded_text("tenant_id", self.tenant_id, 256)
        _bounded_text("scope_key", self.scope_key, 512)
        _aware("cutoff", self.cutoff)
        _aware("completed_at", self.completed_at)
        if self.completed_at < self.cutoff:
            raise ValueError("completed_at cannot precede cutoff")
        if not isinstance(self.status, AnalysisRunStatus):
            raise ValueError("status must be an AnalysisRunStatus")
        _bounded_text("analyzer_version", self.analyzer_version, 128)
        _digest("input_fingerprint", self.input_fingerprint)
        object.__setattr__(self, "result", _safe_mapping("result", self.result))


@dataclass(frozen=True)
class NotificationDeliveryAttempt:
    attempt_id: str
    notification_id: str
    tenant_id: str
    source_kind: str
    source_id: str
    destination_fingerprint: str
    attempted_at: datetime
    outcome: DeliveryOutcome
    payload: dict[str, Any]
    http_status: int | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        _digest("attempt_id", self.attempt_id)
        _digest("notification_id", self.notification_id)
        _bounded_text("tenant_id", self.tenant_id, 256)
        if self.source_kind not in {"analysis", "monitor"}:
            raise ValueError("source_kind must be analysis or monitor")
        _bounded_text("source_id", self.source_id, 256)
        _digest("destination_fingerprint", self.destination_fingerprint)
        _aware("attempted_at", self.attempted_at)
        if not isinstance(self.outcome, DeliveryOutcome):
            raise ValueError("outcome must be a DeliveryOutcome")
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or not 100 <= self.http_status <= 599
        ):
            raise ValueError("http_status must be between 100 and 599")
        if self.error_code is not None:
            _bounded_text("error_code", self.error_code, 128)
        if self.outcome is DeliveryOutcome.DELIVERED and self.error_code is not None:
            raise ValueError("delivered attempts cannot carry error_code")
        if self.outcome is DeliveryOutcome.FAILED and self.error_code is None:
            raise ValueError("failed attempts require error_code")
        object.__setattr__(self, "payload", _safe_mapping("payload", self.payload))


def analysis_run_to_json(run: DeterministicAnalysisRun) -> str:
    raw = asdict(run)
    raw["cutoff"] = run.cutoff.astimezone(timezone.utc).isoformat()
    raw["completed_at"] = run.completed_at.astimezone(timezone.utc).isoformat()
    raw["status"] = run.status.value
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def analysis_run_from_json(payload: str) -> DeterministicAnalysisRun:
    raw = json.loads(payload)
    raw["cutoff"] = datetime.fromisoformat(raw["cutoff"])
    raw["completed_at"] = datetime.fromisoformat(raw["completed_at"])
    raw["status"] = AnalysisRunStatus(raw["status"])
    return DeterministicAnalysisRun(**raw)


def notification_attempt_to_json(attempt: NotificationDeliveryAttempt) -> str:
    raw = asdict(attempt)
    raw["attempted_at"] = attempt.attempted_at.astimezone(timezone.utc).isoformat()
    raw["outcome"] = attempt.outcome.value
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def notification_attempt_from_json(payload: str) -> NotificationDeliveryAttempt:
    raw = json.loads(payload)
    raw["attempted_at"] = datetime.fromisoformat(raw["attempted_at"])
    raw["outcome"] = DeliveryOutcome(raw["outcome"])
    return NotificationDeliveryAttempt(**raw)


def validate_delivery_query(notification_id: str, destination_fingerprint: str, limit: int) -> None:
    _digest("notification_id", notification_id)
    _digest("destination_fingerprint", destination_fingerprint)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
