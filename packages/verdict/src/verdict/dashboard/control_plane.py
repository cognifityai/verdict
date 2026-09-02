"""Small append-only control plane for dashboard configuration and approvals."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.dashboard.storage_url import is_postgres_storage

KINDS = {"settings", "schedule", "alert", "proposal", "review"}
STATES = {
    "settings": {"active"},
    "schedule": {"active", "disabled"},
    "alert": {"active", "disabled"},
    "proposal": {"pending", "approved", "rejected", "rolled_back"},
    "review": {"open", "resolved"},
}
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


def _safe_json(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("control payload must be an object")

    def inspect(value: object, depth: int = 0) -> None:
        if depth > 8:
            raise ValueError("control payload is too deep")
        if isinstance(value, dict):
            if len(value) > 64:
                raise ValueError("control payload has too many fields")
            for key, item in value.items():
                if not isinstance(key, str) or len(key.encode("utf-8")) > 128:
                    raise ValueError("control payload key is invalid")
                lowered = key.lower()
                if any(name in lowered for name in ("password", "apikey", "api_key", "secret", "token")):
                    raise ValueError("store an environment-variable reference, not a secret")
                inspect(item, depth + 1)
        elif isinstance(value, list):
            if len(value) > 128:
                raise ValueError("control payload list is too long")
            for item in value:
                inspect(item, depth + 1)
        elif isinstance(value, str):
            if len(value.encode("utf-8")) > 4096 or "\x00" in value:
                raise ValueError("control payload text is invalid")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ValueError("control payload value is invalid")

    inspect(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 65_536:
        raise ValueError("control payload is too large")
    return encoded


def _validate(kind: str, document_id: str, state: str, payload: dict[str, Any]) -> str:
    if kind not in KINDS or _IDENTITY.fullmatch(document_id) is None:
        raise ValueError("invalid control identity")
    if state not in STATES[kind]:
        raise ValueError("invalid control state")
    if kind == "schedule":
        interval = payload.get("intervalHours", 24)
        if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 168:
            raise ValueError("schedule interval must be 1-168 hours")
        for name in ("claudeRoot", "codexRoot"):
            value = payload.get(name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("invalid schedule source path")
    elif kind == "settings":
        retention = payload.get("retentionDays")
        if retention is not None and (
            isinstance(retention, bool) or not isinstance(retention, int) or not 1 <= retention <= 3650
        ):
            raise ValueError("retention days must be 1-3650")
        for name in payload.get("providerKeyEnvVars", []):
            if not isinstance(name, str) or _ENV_NAME.fullmatch(name) is None:
                raise ValueError("invalid provider environment-variable name")
    elif kind == "alert":
        if payload.get("destination") not in {"local_log", "webhook"}:
            raise ValueError("invalid alert destination")
        env_name = payload.get("webhookUrlEnvVar")
        if payload.get("destination") == "webhook" and (
            not isinstance(env_name, str) or _ENV_NAME.fullmatch(env_name) is None
        ):
            raise ValueError("invalid webhook environment-variable name")
    elif kind == "proposal":
        if payload.get("category") not in {"policy", "taxonomy", "evaluator", "baseline"}:
            raise ValueError("invalid proposal category")
        for name in ("title", "summary"):
            if not isinstance(payload.get(name), str) or not payload[name].strip():
                raise ValueError("proposal title and summary are required")
    elif kind == "review":
        if payload.get("label") not in {None, "pass", "fail", "unclear", "not_evaluable"}:
            raise ValueError("invalid review label")
    return _safe_json(payload)


class ControlStore:
    """Dialect-small store of immutable revisions with optimistic concurrency."""

    def __init__(self, storage: str) -> None:
        self.storage = storage
        self.postgres = is_postgres_storage(storage)

    def _connect(self):
        if self.postgres:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise ImportError("control plane requires the postgres extra") from exc
            return psycopg.connect(self.storage, autocommit=False, row_factory=dict_row)
        path = self.storage[len("sqlite:///"):] if self.storage.startswith("sqlite:///") else self.storage
        connection = sqlite3.connect(Path(path))
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _schema(postgres: bool) -> str:
        payload_type = "JSONB" if postgres else "TEXT"
        time_type = "TIMESTAMPTZ" if postgres else "TEXT"
        return f"""
        CREATE TABLE IF NOT EXISTS product_control_documents (
            tenant_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            document_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision > 0),
            state TEXT NOT NULL,
            payload_json {payload_type} NOT NULL,
            actor TEXT NOT NULL,
            created_at {time_type} NOT NULL,
            PRIMARY KEY (tenant_id, kind, document_id, revision)
        )
        """

    @staticmethod
    def _row(row: Any) -> dict[str, object]:
        payload = row["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        created = row["created_at"]
        return {
            "kind": row["kind"], "documentId": row["document_id"],
            "revision": row["revision"], "state": row["state"],
            "payload": payload, "actor": row["actor"],
            "createdAt": created.isoformat() if isinstance(created, datetime) else created,
        }

    def list_current(self, tenant: str) -> list[dict[str, object]]:
        placeholder = "%s" if self.postgres else "?"
        with self._connect() as connection:
            connection.execute(self._schema(self.postgres))
            rows = connection.execute(
                "SELECT d.* FROM product_control_documents d JOIN ("
                "SELECT tenant_id,kind,document_id,MAX(revision) AS revision "
                "FROM product_control_documents WHERE tenant_id=" + placeholder + " "
                "GROUP BY tenant_id,kind,document_id) latest "
                "ON latest.tenant_id=d.tenant_id AND latest.kind=d.kind "
                "AND latest.document_id=d.document_id AND latest.revision=d.revision "
                "ORDER BY d.kind,d.document_id",
                (tenant,),
            ).fetchall()
            connection.commit()
        return [self._row(row) for row in rows]

    def history(self, tenant: str, kind: str, document_id: str) -> list[dict[str, object]]:
        if kind not in KINDS or _IDENTITY.fullmatch(document_id) is None:
            raise ValueError("invalid control identity")
        placeholder = "%s" if self.postgres else "?"
        with self._connect() as connection:
            connection.execute(self._schema(self.postgres))
            rows = connection.execute(
                "SELECT * FROM product_control_documents WHERE tenant_id=" + placeholder +
                " AND kind=" + placeholder + " AND document_id=" + placeholder +
                " ORDER BY revision DESC LIMIT 100",
                (tenant, kind, document_id),
            ).fetchall()
            connection.commit()
        return [self._row(row) for row in rows]

    def append(
        self,
        tenant: str,
        *,
        kind: str,
        document_id: str,
        state: str,
        payload: dict[str, Any],
        expected_revision: int | None,
        actor: str = "dashboard-user",
    ) -> dict[str, object]:
        encoded = _validate(kind, document_id, state, payload)
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("invalid expected revision")
        placeholder = "%s" if self.postgres else "?"
        with self._connect() as connection:
            connection.execute(self._schema(self.postgres))
            if self.postgres:
                connection.execute("LOCK TABLE product_control_documents IN SHARE ROW EXCLUSIVE MODE")
            row = connection.execute(
                "SELECT MAX(revision) AS revision FROM product_control_documents "
                "WHERE tenant_id=" + placeholder + " AND kind=" + placeholder +
                " AND document_id=" + placeholder,
                (tenant, kind, document_id),
            ).fetchone()
            current = row["revision"] if row else None
            if current != expected_revision:
                connection.rollback()
                raise ValueError("control revision conflict")
            revision = 1 if current is None else int(current) + 1
            now = datetime.now(timezone.utc)
            connection.execute(
                "INSERT INTO product_control_documents "
                "(tenant_id,kind,document_id,revision,state,payload_json,actor,created_at) "
                f"VALUES ({','.join([placeholder] * 8)})",
                (tenant, kind, document_id, revision, state, encoded, actor,
                 now if self.postgres else now.isoformat()),
            )
            connection.commit()
        return self.history(tenant, kind, document_id)[0]

    def rollback(
        self, tenant: str, *, kind: str, document_id: str,
        target_revision: int, expected_revision: int,
    ) -> dict[str, object]:
        history = self.history(tenant, kind, document_id)
        target = next((item for item in history if item["revision"] == target_revision), None)
        if target is None:
            raise ValueError("unknown rollback revision")
        state = "rolled_back" if kind == "proposal" else str(target["state"])
        return self.append(
            tenant, kind=kind, document_id=document_id, state=state,
            payload=dict(target["payload"]), expected_revision=expected_revision,
        )
