"""Small append-only control plane for dashboard configuration and approvals."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from verdict.dashboard.storage_url import is_postgres_storage
from verdict.storage.sqlite import sqlite_path

KINDS = {"settings", "schedule", "alert", "proposal", "review", "case_set", "experiment", "scoped_evaluation", "scheduled_evaluation", "usage_explanation", "evidence_explanation"}
STATES = {
    "settings": {"active"},
    "schedule": {"active", "disabled"},
    "alert": {"active", "disabled"},
    "proposal": {"pending", "approved", "rejected", "rolled_back"},
    "review": {"open", "resolved"},
    "case_set": {"active", "disabled"},
    "experiment": {"completed"},
    "scoped_evaluation": {"completed", "error"},
    "scheduled_evaluation": {"pending", "spent", "cancelled"},
    "usage_explanation": {"reserved", "completed", "error"},
    "evidence_explanation": {"reserved", "completed", "error"},
}
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


def normalize_source_root(value: object) -> str | None:
    """Treat an unfilled source field as absent, preserving real path spelling."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        raise ValueError("invalid schedule source path")
    return value


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
            normalize_source_root(payload.get(name))
        if payload.get("analyzeOnly") not in (None, True, False):
            raise ValueError("analyzeOnly must be a boolean")
        if "includeApprovedEvaluations" in payload and not isinstance(
            payload["includeApprovedEvaluations"], bool
        ):
            raise ValueError("includeApprovedEvaluations must be a boolean")
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
    elif kind == "case_set":
        if payload.get("reviewed") is not True:
            raise ValueError("case set requires reviewed inputs and expected behavior")
        cases = payload.get("cases")
        if not isinstance(cases, list) or not 1 <= len(cases) <= 10:
            raise ValueError("case set requires 1-10 cases")
        ids = set()
        for case in cases:
            if not isinstance(case, dict) or set(case) - {
                "id", "input", "context", "expectedBehavior", "checks",
            }:
                raise ValueError("invalid case")
            case_id = case.get("id")
            if not isinstance(case_id, str) or _IDENTITY.fullmatch(case_id) is None or case_id in ids:
                raise ValueError("invalid or duplicate case id")
            ids.add(case_id)
            for name in ("input", "expectedBehavior"):
                if not isinstance(case.get(name), str) or not case[name].strip():
                    raise ValueError(f"case {name} is required")
            if case.get("context") is not None and not isinstance(case["context"], str):
                raise ValueError("invalid case context")
            checks = case.get("checks")
            if not isinstance(checks, list) or not 1 <= len(checks) <= 5:
                raise ValueError("case requires 1-5 checks")
            for check in checks:
                if (not isinstance(check, dict) or set(check) != {"kind", "value"}
                        or check.get("kind") not in {"exact", "contains", "not_contains"}
                        or not isinstance(check.get("value"), str) or not check["value"]):
                    raise ValueError("invalid deterministic check")
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
        connection = sqlite3.connect(sqlite_path(self.storage))
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
            elif kind == "scheduled_evaluation":
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision,state,payload_json FROM product_control_documents "
                "WHERE tenant_id=" + placeholder + " AND kind=" + placeholder +
                " AND document_id=" + placeholder + " ORDER BY revision DESC LIMIT 1",
                (tenant, kind, document_id),
            ).fetchone()
            current = row["revision"] if row else None
            if current != expected_revision:
                connection.rollback()
                raise ValueError("control revision conflict")
            if kind == "scheduled_evaluation" and state == "pending":
                plan_fingerprint = payload.get("planFingerprint")
                if not isinstance(plan_fingerprint, str) or not plan_fingerprint:
                    connection.rollback()
                    raise ValueError("scheduled approval requires a plan fingerprint")
                active = connection.execute(
                    "SELECT d.payload_json FROM product_control_documents d JOIN ("
                    "SELECT document_id,MAX(revision) AS revision FROM product_control_documents "
                    "WHERE tenant_id=" + placeholder + " AND kind='scheduled_evaluation' "
                    "GROUP BY document_id) latest ON latest.document_id=d.document_id "
                    "AND latest.revision=d.revision WHERE d.tenant_id=" + placeholder + " "
                    "AND d.kind='scheduled_evaluation' AND d.state='pending'",
                    (tenant, tenant),
                )
                for pending in active:
                    saved = pending["payload_json"]
                    if isinstance(saved, str):
                        saved = json.loads(saved)
                    if saved.get("planFingerprint") == plan_fingerprint:
                        connection.rollback()
                        raise ValueError("scheduled evaluation approval already pending")
            current_payload = row["payload_json"] if row else None
            if isinstance(current_payload, str):
                current_payload = json.loads(current_payload)
            records_spent_outcome = (
                row is not None and row["state"] == "spent" and state == "spent"
                and current_payload.get("outcome") == "attempt_reserved"
                and payload.get("outcome") in {"completed", "error"}
                and isinstance(payload.get("resultId"), str)
                and re.fullmatch(r"[0-9a-f]{64}", payload["resultId"]) is not None
                and {key: value for key, value in payload.items()
                     if key not in {"outcome", "resultId", "error"}}
                    == {key: value for key, value in current_payload.items()
                        if key != "outcome"}
            ) if isinstance(current_payload, dict) else False
            if kind == "scheduled_evaluation" and not (
                (row is None and state == "pending")
                or (row is not None and row["state"] == "pending"
                    and state in {"spent", "cancelled"})
                or records_spent_outcome
            ):
                connection.rollback()
                raise ValueError("scheduled evaluation approval is terminal")
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
        if kind == "scheduled_evaluation":
            raise ValueError("spent external-evaluation approvals cannot be rearmed")
        history = self.history(tenant, kind, document_id)
        target = next((item for item in history if item["revision"] == target_revision), None)
        if target is None:
            raise ValueError("unknown rollback revision")
        state = "rolled_back" if kind == "proposal" else str(target["state"])
        return self.append(
            tenant, kind=kind, document_id=document_id, state=state,
            payload=dict(target["payload"]), expected_revision=expected_revision,
        )
