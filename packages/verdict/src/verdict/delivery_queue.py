"""Immutable worker notification obligations and their bounded delivery state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from verdict.analysis_records import _aware, _bounded_text, _digest, _safe_mapping


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def webhook_fingerprint(url: str) -> str | None:
    """Return a fingerprint only for one final, credential-free HTTPS endpoint."""
    if not isinstance(url, str) or not url or any(
        ch.isspace() or ch == "\\" or ord(ch) == 127 for ch in url
    ):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or not parts.netloc
                or not parts.netloc.isascii()
                or parts.username or parts.password or parts.fragment):
            return None
        _ = parts.port
    except ValueError:
        return None
    return hashlib.sha256(url.encode()).hexdigest()


LOCAL_LOG_FINGERPRINT = hashlib.sha256(b"local_log").hexdigest()


@dataclass(frozen=True)
class NotificationIntent:
    intent_id: str
    notification_id: str
    tenant_id: str
    source_kind: str
    source_id: str
    alert_revision: int
    destination: str
    webhook_url_env_var: str | None
    destination_fingerprint: str | None
    notification: dict[str, Any]
    created_at: datetime
    rebind_from_id: str | None = None

    def __post_init__(self) -> None:
        _digest("intent_id", self.intent_id)
        _digest("notification_id", self.notification_id)
        _bounded_text("tenant_id", self.tenant_id, 256)
        if self.source_kind not in {"analysis", "monitor"}:
            raise ValueError("invalid notification source kind")
        _bounded_text("source_id", self.source_id, 256)
        if type(self.alert_revision) is not int or self.alert_revision < 1:
            raise ValueError("invalid alert revision")
        if self.destination not in {"local_log", "webhook"}:
            raise ValueError("invalid notification destination")
        if self.destination == "webhook":
            _bounded_text("webhook_url_env_var", self.webhook_url_env_var, 128)
        elif self.webhook_url_env_var is not None:
            raise ValueError("local log cannot have a webhook environment variable")
        if self.destination_fingerprint is not None:
            _digest("destination_fingerprint", self.destination_fingerprint)
        if self.destination == "local_log" and self.destination_fingerprint != LOCAL_LOG_FINGERPRINT:
            raise ValueError("local log fingerprint mismatch")
        _aware("created_at", self.created_at)
        if self.rebind_from_id is not None:
            _digest("rebind_from_id", self.rebind_from_id)
        object.__setattr__(self, "notification", _safe_mapping("notification", self.notification))


@dataclass(frozen=True)
class DeliveryQueueItem:
    intent: NotificationIntent
    status: str
    attempt_count: int
    next_due_at: datetime | None
    next_probe_at: datetime | None
    last_error_code: str | None
    superseded_by_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {
            "pending", "unbound", "failed", "delivered", "blocked", "exhausted", "superseded",
        }:
            raise ValueError("invalid delivery queue status")
        if type(self.attempt_count) is not int or self.attempt_count < 0:
            raise ValueError("invalid delivery attempt count")
        for name in ("next_due_at", "next_probe_at"):
            value = getattr(self, name)
            if value is not None:
                _aware(name, value)
        if self.last_error_code is not None:
            _bounded_text("last_error_code", self.last_error_code, 128)
        if self.superseded_by_id is not None:
            _digest("superseded_by_id", self.superseded_by_id)


def validate_delivery_cursor(after: tuple[datetime, str]) -> tuple[datetime, str]:
    """Validate a position in the queue's immutable creation-time/ID order."""
    if not isinstance(after, tuple) or len(after) != 2:
        raise ValueError("invalid delivery cursor")
    created_at, intent_id = after
    _aware("cursor created_at", created_at)
    _digest("cursor intent_id", intent_id)
    return created_at.astimezone(timezone.utc), intent_id


def create_intent(
    alert: dict[str, object] | None,
    *,
    tenant_id: str,
    source_kind: str,
    source_id: str,
    notification: dict[str, Any],
    webhook_url: str | None,
    created_at: datetime | None = None,
    rebind_from_id: str | None = None,
) -> NotificationIntent | None:
    """Freeze a reviewed active alert revision without storing its URL."""
    if alert is None or alert.get("state") != "active":
        return None
    config = alert.get("payload")
    if not isinstance(config, dict):
        return None
    kind = notification.get("kind")
    if kind not in {"drift", "finding"} or config.get(
        "drift" if kind == "drift" else "findings"
    ) is not True:
        return None
    revision = alert.get("revision")
    destination = config.get("destination")
    env_name = config.get("webhookUrlEnvVar") if destination == "webhook" else None
    if type(revision) is not int or revision < 1 or destination not in {"webhook", "local_log"}:
        raise ValueError("invalid active alert configuration")
    fingerprint = (
        webhook_fingerprint(webhook_url or "") if destination == "webhook"
        else LOCAL_LOG_FINGERPRINT
    )
    safe_notification = _safe_mapping("notification", notification)
    # Keep the published beta notification identity so upgrade does not resend
    # a previously delivered notification under a new receiver key.
    notification_id = hashlib.sha256(json.dumps({
        **({"tenantId": tenant_id} if tenant_id != "__verdict_local__" else {}),
        "sourceKind": source_kind,
        "sourceId": source_id,
        "notification": safe_notification,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    intent_id = hashlib.sha256(_canonical({
        "notificationId": notification_id,
        "tenantId": tenant_id,
        "alertRevision": revision,
        "destination": destination,
        "webhookUrlEnvVar": env_name,
        **({"rebindFromId": rebind_from_id} if rebind_from_id else {}),
    })).hexdigest()
    return NotificationIntent(
        intent_id=intent_id,
        notification_id=notification_id,
        tenant_id=tenant_id,
        source_kind=source_kind,
        source_id=source_id,
        alert_revision=revision,
        destination=destination,
        webhook_url_env_var=env_name,
        destination_fingerprint=fingerprint,
        notification=safe_notification,
        created_at=created_at or datetime.now(timezone.utc),
        rebind_from_id=rebind_from_id,
    )


def intent_to_json(intent: NotificationIntent) -> str:
    raw = asdict(intent)
    raw["created_at"] = intent.created_at.astimezone(timezone.utc).isoformat()
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def intent_from_json(payload: str) -> NotificationIntent:
    raw = json.loads(payload)
    raw["created_at"] = datetime.fromisoformat(raw["created_at"])
    return NotificationIntent(**raw)
