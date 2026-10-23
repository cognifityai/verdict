"""Installable Verdict dashboard for SQLite and PostgreSQL stores.

The application factory can be mounted inside another ASGI application. Read
views use one storage snapshot per request. Explicit setup, registry,
evaluator, monitor, and control-plane actions are the only mutating endpoints;
they retain immutable source traces and append versioned product state.

Run:
    pip install "cognifity-verdict[dashboard]"
    verdict-dashboard --storage sqlite:///./verdict.db

The aggregation is shared across storage dialects so the browser sees one DTO.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import logging
import math
import os
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from verdict.analysis import analyze_agent_run, outcome_reported, recorded_event_issue
from verdict.analysis_records import analysis_run_from_json
from verdict.cluster_health import UNCLUSTERED_ID, assess_cluster_health
from verdict.dashboard import agent_evidence_queries
from verdict.dashboard.analysis_service import (
    ANALYZER_VERSION,
    SCOPE_KEY,
    read_latest_analysis,
    run_analysis,
)
from verdict.dashboard.local_history_page import (
    complete_local_bundle,
    verified_local_positions,
)
from verdict.dashboard.query import PostgresSession as _PostgresSession
from verdict.dashboard.query import QuerySession as _QuerySession
from verdict.dashboard.query import SQLiteSession as _SQLiteSession
from verdict.dashboard.recommendations import run_recommendations
from verdict.dashboard.registry import (
    RegistryNotFoundError,
    RegistryStateError,
    active_cluster_projection,
)
from verdict.dashboard.registry import (
    build_registry_bundle as _build_registry_bundle,
)
from verdict.dashboard.run_outcome_preview import build_run_outcome_preview
from verdict.dashboard.storage_url import is_postgres_storage
from verdict.dashboard.turn_analysis import MAX_TURN_ANALYSIS_EVENTS, analyze_turn_events
from verdict.dashboard.usage import build_usage_view
from verdict.dashboard.usage import models_by_turn as _models_by_turn
from verdict.dashboard.usage import turn_list_price_usd as _turn_list_price_usd
from verdict.evidence import (
    EvidenceState,
    ToolOrigin,
    _load_json_without_duplicate_tool_origin,
    disclosed_event_attributes,
    event_order_key,
)
from verdict.local_source_revision import local_scope_partial, verified_recent_findings
from verdict.metrics import ScoreCounts, verdict_label
from verdict.monitor_inputs import LOCAL_TENANT
from verdict.normalized_evidence import (
    agent_turn_from_row,
    normalized_bundle_digest,
)
from verdict.redaction import redact, redact_structure, sanitize_agent_event_attributes
from verdict.storage.sqlite import sqlite_path
from verdict.telemetry.model import safe_tenant_id
from verdict.trace_facts import deterministic_trace_facts, trace_evidence_state

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

_log = logging.getLogger("verdict.dashboard")

# Security headers applied to every HTTP response. Scripts and stylesheets are
# pre-built local assets. ``unsafe-inline`` remains style-only because the React
# views use style properties for data-driven colors and dimensions.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
PRETTY = {
    "claude-haiku-4-5": "Claude Haiku 4.5",
    "gpt-4o-mini": "GPT-4o-mini",
    "gemini-2.5-flash": "Gemini 2.5 Flash",
}
PROVIDER_ORDER = ["anthropic", "openai", "google"]
DIM_ORDER = ["groundedness", "relevance", "completeness", "safety", "instruction_following"]
MAX_SERIES_POINTS = 100
MAX_DASHBOARD_PROVIDERS = 8
MAX_DASHBOARD_CLUSTERS = 20
MAX_DASHBOARD_DIMENSIONS = 12
MAX_TURN_DETAIL_EVALUATORS = 8
MAX_DASHBOARD_EVALUATORS = 20
MAX_PROVIDER_MODELS = 20
MAX_MANAGEMENT_REPORT_ROWS = 20
MAX_MANAGEMENT_TIMELINE_DATES = 31
MAX_MANAGEMENT_LATENCY_SAMPLES = 10_000
REPORT_PERIOD_DAYS = {0, 7, 30, 90}
MAX_TRACE_SAMPLES = 30
# Run detail has stricter record/page bounds than the generic redaction API.
# These endpoint-only limits cover the maximum valid 50-turn/200-event page
# without weakening the default fail-closed budget used by capture paths.
MAX_AGENT_DETAIL_REDACTION_NODES = 50_000
MAX_AGENT_DETAIL_REDACTION_CHARACTERS = 16_000_000
MAX_INSIGHT_LOCAL_EXPANSION_ROWS = 5_000
_DISPLAY_WORKLOAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_FINDING_SUMMARIES = {
    "run_status_unknown": "The source does not expose a terminal status for these agent sessions.",
    "response_not_evaluable": "At least one response is unavailable, so response quality is not evaluable.",
    "event_capture_partial": "The source exceeded the bounded event limit; event analysis is partial.",
    "source_capture_partial": "The local source has evidence outside the captured prefix, an incomplete final record, or unverified coverage; whole-run analysis is partial.",
    "tool_outcomes_incomplete": "Recorded tool calls do not all have matching results; outcome coverage is incomplete.",
    "tool_error": "One or more captured tool results reported failure.",
    "command_failed": "One or more captured commands returned a non-zero status.",
    "business_outcome_failed": "One or more explicit business outcomes reported failure.",
    "possible_tool_loop": (
        "Identical tool calls with identical arguments repeated within a turn past the configured threshold."
    ),
    "required_step_missing": "A policy-required evidence type was not observed.",
    "prohibited_tool_used": "A policy-prohibited tool was called.",
    "response_schema_invalid": "A response did not satisfy the configured JSON requirement.",
}


class DashboardBundleLimitError(RuntimeError):
    """The bounded dashboard bundle still exceeded the redaction boundary."""


def _dt(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        parsed = ts
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _report_period(report_days: int, now: datetime) -> tuple[datetime | None, datetime, dict]:
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if report_days == 0:
        return None, end, {"days": 0, "startDate": None, "endDate": None}
    start = end - timedelta(days=report_days)
    return start, end, {
        "days": report_days,
        "startDate": start.date().isoformat(),
        "endDate": (end - timedelta(days=1)).date().isoformat(),
    }


def _label_for(provider: str, model: str) -> str:
    return PRETTY.get(model, model or provider)


def _provider_key(provider: object) -> str:
    """Return a chart-safe key without erasing the raw provider value."""
    if provider in PROVIDER_ORDER:
        return str(provider)
    encoded = json.dumps(
        {"type": type(provider).__name__, "value": provider},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return f"provider_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"


def _row_value(row: Mapping[str, Any], key: str, default=None):
    """Read a column from both current and pre-migration judgment rows."""
    return row[key] if key in row.keys() else default


def _json_value(raw: object, default):
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw) if isinstance(raw, str) else deepcopy(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _dashboard_agent_event_attributes(event_type: object, raw: object) -> dict[str, object]:
    """Return display-safe attributes without exposing malformed origin payloads."""
    try:
        decoded = (
            _load_json_without_duplicate_tool_origin(raw)
            if isinstance(raw, str)
            else deepcopy(raw)
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    attributes = disclosed_event_attributes(
        sanitize_agent_event_attributes(event_type, decoded))
    origin = attributes.pop("tool_origin", None)
    if (
        event_type == "tool_call"
        and isinstance(origin, str)
        and origin in {item.value for item in ToolOrigin}
    ):
        attributes["tool_origin"] = origin
    return attributes


def _dashboard_time(value: object) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _json_column(row: Mapping[str, Any], name: str, default):
    """Read one logical JSON field from either adapter's physical schema."""
    raw = _row_value(row, f"{name}_json", _row_value(row, name))
    return _json_value(raw, default)


def evaluator_identity(row: Mapping[str, Any]) -> dict:
    """Build a stable evaluator discriminator, including legacy rows.

    The August dashboard blocker can already separate model/rubric definitions.
    Newer schema fields are included whenever present so complete identities
    never collapse into historical incomplete identities.
    """
    models = _json_column(row, "judge_models", [])
    config = _json_column(row, "evaluator_config", {})
    expected_dimensions = _json_column(row, "expected_dimensions", [])
    provider = _row_value(row, "evaluator_provider", "") or ""
    fingerprint = _row_value(row, "evaluator_fingerprint", "") or ""
    canonical = {
        "provider": provider,
        "models": models if isinstance(models, list) else [],
        "rubricName": _row_value(row, "rubric_name", "default") or "default",
        "rubricVersion": _row_value(row, "rubric_version", "1") or "1",
        "config": config if isinstance(config, dict) else {},
        "expectedDimensions": (
            expected_dimensions if isinstance(expected_dimensions, list) else []
        ),
        "fingerprint": fingerprint,
    }
    complete = bool(
        provider
        and fingerprint
        and canonical["models"]
        and canonical["expectedDimensions"]
    )
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    identity_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
    model_label = "+".join(str(model) for model in canonical["models"]) or "unknown judge"
    rubric_label = f"{canonical['rubricName']} v{canonical['rubricVersion']}"
    identity_suffix = (
        f" · fp {fingerprint[:8]}"
        if complete
        else " · historical identity incomplete"
    )
    return {
        "id": identity_id,
        "provider": provider or None,
        "models": canonical["models"],
        "rubricName": canonical["rubricName"],
        "rubricVersion": canonical["rubricVersion"],
        "fingerprint": fingerprint or None,
        "config": canonical["config"],
        "expectedDimensions": canonical["expectedDimensions"],
        "complete": complete,
        "label": f"{model_label} · {rubric_label}{identity_suffix}",
    }


def _score_rate(counts: Counter) -> float | None:
    """Canonical PASS / (PASS + FAIL), excluding every unknown state."""
    rate = ScoreCounts(
        passed=counts.get("pass", 0),
        failed=counts.get("fail", 0),
        unclear=counts.get("unclear", 0),
    ).pass_rate
    return round(100 * rate, 1) if rate is not None else None


def _table_exists(cur: _QuerySession, table: str) -> bool:
    return cur.table_exists(table)


def _round_or_none(value: object, digits: int) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return round(numeric, digits) if math.isfinite(numeric) else None


def _cost_summary(traces: int, priced_traces: int, cost: float) -> dict[str, Any]:
    status = (
        "unavailable" if priced_traces == 0
        else "complete" if priced_traces == traces
        else "partial"
    )
    return {
        "traces": traces,
        "pricedTraces": priced_traces,
        "cost": round(cost, 6) if priced_traces else None,
        "status": status,
    }


def _resource_limit(available: int, shown: int, limit: int) -> dict[str, int]:
    return {"available": available, "shown": shown, "limit": limit}


def _truncation_metadata(resources: dict[str, dict[str, int]]) -> dict:
    return {
        "applied": any(
            resource["shown"] < resource["available"]
            for resource in resources.values()
        ),
        "resources": resources,
    }


def resolve_db() -> Path:
    env = os.environ.get("VERDICT_DB")
    if env:
        return Path(env).expanduser().resolve()
    for name in ("verdict_experiment.db", "verdict.db"):
        cand = Path.cwd() / name
        if cand.exists():
            return cand
    return Path.cwd() / "verdict.db"


def resolve_storage(storage: str | os.PathLike[str] | None = None) -> str:
    """Resolve the explicit or environment-supplied dashboard store."""
    if storage is not None:
        return str(storage)
    configured = os.environ.get("VERDICT_STORAGE")
    if configured:
        return configured
    return str(resolve_db())


def _sqlite_path(storage: str) -> Path:
    return sqlite_path(storage)


def _is_postgres(storage: str) -> bool:
    return is_postgres_storage(storage)


def _trace_tenant_scope(
    tenant: str | None,
    trace_columns: set[str],
    *,
    alias: str = "traces",
) -> tuple[str, tuple[str, ...]]:
    """Return the one SQL predicate used by every trace-derived dashboard view."""
    if tenant is None:
        return "1=1", ()
    if "tenant_id" not in trace_columns:
        return ("1=1", ()) if tenant == LOCAL_TENANT else ("1=0", ())
    if tenant == LOCAL_TENANT:
        return (
            f"({alias}.tenant_id=? OR {alias}.tenant_id IS NULL "
            f"OR {alias}.tenant_id='')",
            (tenant,),
        )
    return f"{alias}.tenant_id=?", (tenant,)


def _owned_trace_ids(
    session: _QuerySession, tenant: str, trace_ids: set[object],
) -> set[str]:
    """Resolve bounded Agent Event links through the tenant-owned Trace table."""
    identifiers = sorted(
        trace_id for trace_id in trace_ids
        if isinstance(trace_id, str) and trace_id and "\x00" not in trace_id
        and len(trace_id.encode("utf-8")) <= 256
    )
    if not identifiers or not session.table_exists("traces"):
        return set()
    scope, scope_params = _trace_tenant_scope(
        tenant, session.columns("traces"), alias="t",
    )
    owned: set[str] = set()
    for start in range(0, len(identifiers), 500):
        chunk = identifiers[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        owned.update(str(row["trace_id"]) for row in session.execute(
            "SELECT t.trace_id FROM traces t WHERE "
            f"t.trace_id IN ({placeholders}) AND {scope}",  # nosec B608 -- fixed predicate and placeholders
            (*chunk, *scope_params),
        ))
    return owned


# --------------------------------------------------------------------------- #
#  Aggregation — produces the exact shape the dashboard consumes
# --------------------------------------------------------------------------- #
def build_bundle(
    storage: str | os.PathLike[str],
    *,
    evaluator_id: str | None = None,
    registry_tenant: str | None = None,
    trace_offset: int = 0,
    trace_judge_status: str = "all",
    trace_id: str | None = None,
    trace_date: str | None = None,
    report_days: int = 0,
) -> dict:
    if type(report_days) is not int or report_days not in REPORT_PERIOD_DAYS:
        raise ValueError("report_days must be 0, 7, 30, or 90")
    if (
        not isinstance(trace_offset, int)
        or isinstance(trace_offset, bool)
        or trace_offset < 0
    ):
        raise ValueError("trace_offset must be a non-negative integer")
    if trace_judge_status not in {
        "all", "judged", "not_judged", "judge_error", "pass", "fail", "unclear",
    }:
        raise ValueError("invalid trace judge status")
    if trace_id is not None and (
        not isinstance(trace_id, str) or not trace_id or len(trace_id.encode("utf-8")) > 256
    ):
        raise ValueError("invalid trace_id")
    if trace_date is not None:
        if not isinstance(trace_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", trace_date):
            raise ValueError("invalid trace_date")
        try:
            if date.fromisoformat(trace_date).isoformat() != trace_date:
                raise ValueError("invalid trace_date")
        except ValueError as exc:
            raise ValueError("invalid trace_date") from exc
    configured = str(storage)
    if _is_postgres(configured):
        return _build_from_postgres(
            configured,
            evaluator_id=evaluator_id,
            registry_tenant=registry_tenant,
            trace_offset=trace_offset,
            trace_judge_status=trace_judge_status,
            trace_id=trace_id,
            trace_date=trace_date,
            report_days=report_days,
        )
    path = _sqlite_path(configured)
    try:
        return _build_from_connection(
            sqlite3.connect(f"file:{path}?mode=ro", uri=True),
            evaluator_id=evaluator_id,
            registry_tenant=registry_tenant,
            trace_offset=trace_offset,
            trace_judge_status=trace_judge_status,
            trace_id=trace_id,
            trace_date=trace_date,
            report_days=report_days,
        )
    except sqlite3.OperationalError as exc:
        if "unable to open database file" not in str(exc).lower():
            raise
        # URI mode can fail on some filesystems/paths. Fall back to a plain
        # connection, but never let sqlite CREATE a missing db, and enforce
        # read-only at the connection level.
        if not path.is_file():
            raise FileNotFoundError(f"Verdict database not found: {path}") from exc
        _log.warning("read-only SQLite URI failed; retrying with query-only connection: %s", exc)
        con = sqlite3.connect(path)
        con.execute("PRAGMA query_only = ON")
        return _build_from_connection(
            con,
            evaluator_id=evaluator_id,
            registry_tenant=registry_tenant,
            trace_offset=trace_offset,
            trace_judge_status=trace_judge_status,
            trace_id=trace_id,
            trace_date=trace_date,
            report_days=report_days,
        )


def build_registry_bundle(
    storage: str | os.PathLike[str],
    *,
    tenant: str,
    version_id: str | None = None,
    assignment_limit: int = 50,
    assignment_offset: int = 0,
) -> dict:
    """Read one bounded tenant registry snapshot without migrating the store."""
    configured = str(storage)

    def builder(session: _QuerySession) -> dict:
        return _build_registry_bundle(
            session,
            tenant=tenant,
            version_id=version_id,
            assignment_limit=assignment_limit,
            assignment_offset=assignment_offset,
        )

    if _is_postgres(configured):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError(
                "PostgreSQL dashboard support requires "
                '`pip install "cognifity-verdict[postgres,dashboard]"`'
            ) from exc
        with psycopg.connect(configured, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                result = builder(_PostgresSession(connection))
    else:
        path = _sqlite_path(configured)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            result = builder(_SQLiteSession(connection))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    redacted = redact_structure(result)
    if not isinstance(redacted, dict):
        raise DashboardBundleLimitError(
            "bounded registry dashboard bundle exceeded the redaction budget"
        )
    return redacted


def build_agent_usage_bundle(
    storage: str | os.PathLike[str], *, tenant: str,
    now: datetime | None = None, day: str | None = None,
    session_id: str | None = None, source_kind: str | None = None,
) -> dict:
    """Read a bounded, tenant-scoped UTC usage report without store mutation."""
    if not isinstance(tenant, str) or not tenant or len(tenant.encode("utf-8")) > 256:
        raise ValueError("invalid tenant")
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id
        or len(session_id.encode("utf-8")) > 256
    ):
        raise ValueError("invalid session_id")
    if source_kind is not None and (
        session_id is None or not isinstance(source_kind, str) or not source_kind
        or len(source_kind.encode("utf-8")) > 256
    ):
        raise ValueError("invalid source_kind")
    if day is not None and (not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)):
        raise ValueError("invalid day")
    if now is not None and (not isinstance(now, datetime) or now.tzinfo is None):
        raise ValueError("invalid now")
    configured = str(storage)

    def builder(session: _QuerySession) -> dict:
        return build_usage_view(
            session, tenant=tenant, now=now or _now_utc(), day=day,
            session_id=session_id, source_kind=source_kind,
        )

    if _is_postgres(configured):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError("PostgreSQL dashboard support requires the postgres extra") from exc
        with psycopg.connect(configured, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                result = builder(_PostgresSession(connection))
    else:
        path = _sqlite_path(configured)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            result = builder(_SQLiteSession(connection))
            connection.commit()
        finally:
            connection.close()
    # Exact run/event IDs are dashboard navigation keys. Preserve only the
    # bounded selected-day issue examples after redacting descriptive fields;
    # changing an ID would make its evidence link resolve to a different key.
    issue_events = result.get("selectedDayIssues", {}).get("events", [])
    display = {**result, "selectedDayIssues": {
        **result["selectedDayIssues"],
        "events": [{**event, "runId": "", "eventId": ""} for event in issue_events],
    }}
    redacted = redact_structure(display)
    if not isinstance(redacted, dict):
        raise DashboardBundleLimitError("bounded agent usage bundle exceeded redaction budget")
    for event, exact in zip(redacted["selectedDayIssues"]["events"], issue_events, strict=True):
        event["runId"] = exact["runId"]
        event["eventId"] = exact["eventId"]
    return redacted


def build_agent_session_bundle(
    storage: str | os.PathLike[str], *, tenant: str, source_kind: str, session_id: str,
) -> dict:
    """Read one exact source-qualified session in a bounded storage snapshot."""
    for name, value in (("tenant", tenant), ("source_kind", source_kind),
                        ("session_id", session_id)):
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
            raise ValueError(f"invalid {name}")
    configured = str(storage)

    def builder(session: _QuerySession) -> dict:
        run_ids, available = agent_evidence_queries.session_run_ids(
            session, tenant, source_kind, session_id, limit=20,
        )
        if not run_ids:
            raise KeyError(session_id)
        counts = session.execute(
            "SELECT (SELECT COUNT(*) FROM agent_turns WHERE tenant_id=? AND run_id IN (" +
            ",".join("?" for _ in run_ids) + ")) AS turns," +
            "(SELECT COUNT(*) FROM agent_events WHERE tenant_id=? AND run_id IN (" +
            ",".join("?" for _ in run_ids) + ")) AS events",  # nosec B608 -- generated placeholders
            (tenant, *run_ids, tenant, *run_ids),
        ).fetchone()
        turn_count, event_count = int(counts["turns"]), int(counts["events"])
        reason = ("run_limit" if available > 20 else
                  "turn_limit" if turn_count > 20 else
                  "event_limit" if event_count > 80 else None)
        if reason is not None:
            placeholders = ",".join("?" for _ in run_ids)
            run_rows = session.execute(
                "SELECT run_id,status,started_at,agent_name FROM agent_runs "
                "WHERE tenant_id=? AND run_id IN (" + placeholders + ") "
                "ORDER BY started_at,run_id LIMIT 20",  # nosec B608 -- generated placeholders
                (tenant, *run_ids),
            )
            turn_rows = session.execute(
                "SELECT run_id,turn_id,sequence,started_at,ended_at,status,"
                "user_request_redacted,final_response_redacted,request_state,response_state,"
                "request_truncated,response_truncated,total_tokens "
                "FROM agent_turns WHERE tenant_id=? AND run_id IN (" + placeholders + ") "
                "ORDER BY started_at,run_id,sequence LIMIT 20",  # nosec B608 -- generated placeholders
                (tenant, *run_ids),
            )
            event_rows = session.execute(
                "SELECT run_id,turn_id,event_id,sequence,occurred_at,event_type,status,"
                "producer_id,producer_sequence,provenance FROM agent_events "
                "WHERE tenant_id=? AND run_id IN (" + placeholders + ") "
                "ORDER BY occurred_at,run_id,producer_id,"  # nosec B608 -- generated placeholders
                "COALESCE(producer_sequence,sequence),event_id LIMIT 80",
                (tenant, *run_ids),
            )
            usage_row = session.execute(
                "SELECT COUNT(total_tokens) AS known_turns,SUM(total_tokens) AS total_tokens "
                "FROM agent_turns WHERE tenant_id=? AND run_id IN (" + placeholders + ")",  # nosec B608
                (tenant, *run_ids),
            ).fetchone()
            def excerpt(value):
                if value is None:
                    return None
                return value if len(value) <= 512 else value[:512] + "… [excerpt; open run for full captured text]"

            return {
                "sessionId": session_id, "sourceKind": source_kind,
                "runIds": list(run_ids), "runCountAtLeast": available,
                "turnCount": turn_count, "eventCount": event_count,
                "coverage": "partial", "unavailableReason": reason,
                "runs": [{"runId": row["run_id"], "status": row["status"],
                          "startedAt": str(row["started_at"]), "agentName": row["agent_name"]}
                         for row in run_rows],
                "turns": [{"runId": row["run_id"], "turnId": row["turn_id"],
                           "sequence": row["sequence"], "startedAt": str(row["started_at"]),
                           "endedAt": str(row["ended_at"]) if row["ended_at"] else None,
                           "status": row["status"], "request": excerpt(row["user_request_redacted"]),
                           "response": excerpt(row["final_response_redacted"]),
                           "requestState": row["request_state"], "responseState": row["response_state"],
                           "requestTruncated": bool(row["request_truncated"]),
                           "responseTruncated": bool(row["response_truncated"]),
                           "totalTokens": row["total_tokens"], "listPriceUsd": None}
                          for row in turn_rows],
                "events": [{"runId": row["run_id"], "turnId": row["turn_id"],
                            "eventId": row["event_id"], "sequence": row["sequence"],
                            "occurredAt": str(row["occurred_at"]), "type": row["event_type"],
                            "status": row["status"], "producerId": row["producer_id"],
                            "producerSequence": row["producer_sequence"],
                            "provenance": row["provenance"], "attributes": {}}
                           for row in event_rows],
                "recommendations": [], "findings": [],
                "usage": {"totalTokens": usage_row["total_tokens"],
                          "tokenKnownTurns": usage_row["known_turns"],
                          "turnCount": turn_count, "listPriceUsd": None,
                          "pricedTurns": 0, "coverage": "partial"},
                "evaluations": [],
            }
        bundles = agent_evidence_queries.load_bundles(
            session, tenant, limit=len(run_ids), run_ids=run_ids, oldest_first=True,
        )
        local_revisions = agent_evidence_queries.local_revisions(
            session, tenant, tuple(bundle.run.run_id for bundle in bundles),
        )
        turns = []
        events = []
        runs = []
        findings = []
        recommendations = []
        total_tokens = 0
        known_tokens = 0
        list_price = 0.0
        priced_turns = 0
        session_source_partial = False
        for bundle in bundles:
            run_id = bundle.run.run_id
            local_revision = local_revisions.get(run_id)
            source_partial = local_scope_partial(
                bundle.session.source_kind, local_revision,
                max((turn.sequence for turn in bundle.turns), default=-1), None,
            )
            session_source_partial |= source_partial
            analysis = analyze_agent_run(
                bundle, include_tool_outcome_coverage=True,
                local_revision=local_revision,
            )
            runs.append({"runId": run_id, "status": bundle.run.status.value,
                         "startedAt": bundle.run.started_at.isoformat(),
                         "agentName": bundle.run.agent_name or None})
            for finding in analysis.findings:
                findings.append({"runId": run_id, "code": finding.code,
                                 "severity": finding.severity, "message": finding.message,
                                 "scope": "historical_prefix" if source_partial else "run",
                                 "evidenceEventIds": list(finding.evidence_event_ids)})
            for recommendation in run_recommendations(
                bundle,
                source_partial=source_partial,
            ):
                recommendations.append({"runId": run_id, **recommendation})
            models = _models_by_turn(bundle.events)
            for turn in bundle.turns:
                if turn.total_tokens is not None:
                    known_tokens += 1
                    total_tokens += turn.total_tokens
                price = _turn_list_price_usd(turn, models.get(turn.turn_id, frozenset()))
                if price is not None:
                    priced_turns += 1
                    list_price += price
                turns.append({"runId": run_id, "turnId": turn.turn_id,
                              "sequence": turn.sequence,
                              "startedAt": turn.started_at.isoformat(),
                              "endedAt": turn.ended_at.isoformat() if turn.ended_at else None,
                              "status": turn.status.value,
                              "request": turn.user_request_redacted,
                              "response": turn.final_response_redacted,
                              "requestState": turn.request_state.value,
                              "responseState": turn.response_state.value,
                              "requestTruncated": turn.request_truncated,
                              "responseTruncated": turn.response_truncated,
                              "totalTokens": turn.total_tokens,
                              "listPriceUsd": round(price, 8) if price is not None else None})
            for event in bundle.events:
                events.append({"runId": run_id, "turnId": event.turn_id,
                               "eventId": event.event_id, "sequence": event.sequence,
                               "occurredAt": event.occurred_at.isoformat(),
                               "type": event.event_type.value, "status": event.status.value,
                               "producerId": event.producer_id,
                               "producerSequence": event.producer_sequence,
                               "provenance": event.provenance,
                               "attributes": disclosed_event_attributes(event.attributes)})
        turns.sort(key=lambda item: (item["startedAt"], item["runId"], item["sequence"], item["turnId"]))
        events.sort(key=event_order_key)
        evaluations = []
        if session.table_exists("product_control_documents"):
            for row in session.execute(
                """SELECT document_id,state,payload_json,created_at FROM product_control_documents
                   WHERE tenant_id=? AND kind='scoped_evaluation'
                   ORDER BY created_at DESC,document_id DESC LIMIT 200""",
                (tenant,),
            ):
                payload = row["payload_json"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("runId") not in run_ids and not (
                    payload.get("scope") == "session" and
                    payload.get("sessionId") == session_id and
                    payload.get("sourceKind") == source_kind
                ):
                    continue
                evaluations.append({"resultId": row["document_id"],
                                    "scope": payload.get("scope"), "runId": payload.get("runId"),
                                    "status": row["state"],
                                    "evidenceFingerprint": payload.get("evidenceFingerprint"),
                                    "evaluatorFingerprint": payload.get("evaluatorFingerprint"),
                                    "dimensions": payload.get("dimensions", [])})
                if len(evaluations) >= 20:
                    break
        return {
            "sessionId": session_id, "sourceKind": source_kind,
            "runIds": list(run_ids), "runCountAtLeast": available,
            "turnCount": turn_count, "eventCount": event_count,
            "coverage": "partial" if session_source_partial else "complete",
            "unavailableReason": (
                "source_capture_partial" if session_source_partial else None
            ),
            "runs": runs, "turns": turns, "events": events,
            "findings": findings, "recommendations": recommendations,
            "usage": {"totalTokens": total_tokens if known_tokens else None,
                      "tokenKnownTurns": known_tokens, "turnCount": turn_count,
                      "listPriceUsd": round(list_price, 8) if priced_turns else None,
                      "pricedTurns": priced_turns},
            "evaluations": evaluations,
        }

    if _is_postgres(configured):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError("PostgreSQL dashboard support requires the postgres extra") from exc
        with psycopg.connect(configured, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                connection.execute("SET TRANSACTION READ ONLY")
                result = builder(_PostgresSession(connection))
    else:
        connection = sqlite3.connect(f"file:{_sqlite_path(configured)}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            result = builder(_SQLiteSession(connection))
            connection.commit()
        finally:
            connection.close()
    redacted = redact_structure(
        result, _max_nodes=MAX_AGENT_DETAIL_REDACTION_NODES,
        _max_characters=MAX_AGENT_DETAIL_REDACTION_CHARACTERS,
    )
    if not isinstance(redacted, dict):
        raise DashboardBundleLimitError("bounded session report exceeded redaction budget")
    from verdict.dashboard.scoped_evaluation import session_projection_from_report

    scoped_evidence = session_projection_from_report(
        redacted, session_id=session_id, source_kind=source_kind,
    )
    redacted["scopedEvaluationEligible"] = scoped_evidence["coverage"] == "complete"
    redacted["scopedEvaluationUnavailableReason"] = scoped_evidence["unavailableReason"]
    return redacted


def build_agent_runs_bundle(
    storage: str | os.PathLike[str], *, tenant: str, limit: int = 30,
    offset: int = 0,
    run_id: str | None = None, run_ids: tuple[str, ...] | None = None,
    evaluator_fingerprint: str | None = None,
    view: str = "full", selected_run_id: str | None = None,
    source_kind: str | None = None, agent_name: str | None = None,
) -> dict:
    """Read a bounded, analyzed agent-run view without migrating the store."""
    if not isinstance(tenant, str) or not tenant or len(tenant.encode("utf-8")) > 256:
        raise ValueError("invalid tenant")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("invalid limit")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= 100_000
    ):
        raise ValueError("invalid offset")
    if run_id is not None and (
        not isinstance(run_id, str) or not run_id or len(run_id.encode("utf-8")) > 256
    ):
        raise ValueError("invalid run_id")
    if run_id is not None and run_ids is not None:
        raise ValueError("run_id and run_ids are mutually exclusive")
    if offset and (run_id is not None or run_ids is not None):
        raise ValueError("offset is not supported with selected runs")
    if run_ids is not None:
        if not isinstance(run_ids, tuple) or not 1 <= len(run_ids) <= 50:
            raise ValueError("invalid run_ids")
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("run_ids must be unique")
        for requested_id in run_ids:
            if (
                not isinstance(requested_id, str)
                or not requested_id
                or len(requested_id.encode("utf-8")) > 256
            ):
                raise ValueError("invalid run_ids")
    if evaluator_fingerprint is not None and (
        len(evaluator_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in evaluator_fingerprint)
    ):
        raise ValueError("invalid evaluator fingerprint")
    if view not in ("full", "list"):
        raise ValueError("invalid runs view")
    if selected_run_id is not None and (
        view != "list" or not isinstance(selected_run_id, str)
        or not selected_run_id or len(selected_run_id.encode("utf-8")) > 256
    ):
        raise ValueError("invalid selected_run_id")
    group_values = [value for value in (source_kind, agent_name) if value is not None]
    if ((group_values and (view != "list" or run_id is not None or run_ids is not None))
        or any(not isinstance(value, str) or not value or "\x00" in value
               or len(value.encode("utf-8")) > 256 for value in group_values)):
        raise ValueError("invalid group filter")
    configured = str(storage)

    def builder(session: _QuerySession) -> dict:
        if not agent_evidence_queries.available(session):
            empty = {
                "summary": {"available": 0, "shown": 0},
                "page": {
                    "available": 0, "shown": 0, "offset": offset,
                    "limit": limit, "truncated": False,
                },
                "runs": [],
            }
            if view == "list":
                empty["selectedRun"] = None
            return empty
        selected_run_ids = run_ids or ((run_id,) if run_id is not None else None)
        if selected_run_ids is None:
            available_runs = agent_evidence_queries.count_runs(
                session, tenant, source_kind=source_kind, agent_name=agent_name,
            )
        else:
            placeholders = ",".join("?" for _ in selected_run_ids)
            count = session.execute(
                "SELECT COUNT(*) AS count FROM agent_runs "
                f"WHERE tenant_id=? AND run_id IN ({placeholders})",  # nosec B608
                (tenant, *selected_run_ids),
            ).fetchone()
            available_runs = int(count["count"] if count else 0)
        list_rows = agent_evidence_queries.list_run_rows(
            session, tenant, limit=limit, offset=offset, run_ids=selected_run_ids,
            source_kind=source_kind, agent_name=agent_name,
        ) if view == "list" else None
        listed_ids = {row["runId"] for row in list_rows} if list_rows is not None else set()
        target_id = (selected_run_id if selected_run_id is not None else
                     list_rows[0]["runId"] if list_rows else None)
        bundles = agent_evidence_queries.load_bundles(
            session, tenant,
            limit=1 if view == "list" else limit,
            offset=0 if view == "list" else offset,
            run_ids=(target_id,) if view == "list" and target_id in listed_ids
            else () if view == "list" else selected_run_ids,
        ) if view != "list" or target_id in listed_ids else []
        local_revisions = agent_evidence_queries.local_revisions(
            session, tenant, tuple(bundle.run.run_id for bundle in bundles),
        )
        runs = []
        linked_trace_ids: dict[str, set[str]] = defaultdict(set)
        from verdict.dashboard.scoped_evaluation import _projection

        for bundle in bundles:
            local_revision = local_revisions.get(bundle.run.run_id)
            scoped_evidence = _projection(
                None, tenant, {"scope": "run", "runId": bundle.run.run_id}, bundle=bundle,
                local_revision=local_revision,
            )
            analysis = analyze_agent_run(
                bundle, include_tool_outcome_coverage=True,
                local_revision=local_revision,
            )
            turn_models = _models_by_turn(bundle.events)
            turn_prices = [
                price for turn in bundle.turns
                if (price := _turn_list_price_usd(
                    turn, turn_models.get(turn.turn_id, frozenset()),
                )) is not None
            ]
            usage_turns = [
                turn for turn in bundle.turns if turn.token_usage_basis is not None
            ]
            total_usage_turns = [
                turn for turn in usage_turns if turn.total_tokens is not None
            ]
            source_components = {}
            for name in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            ):
                values = [
                    value for turn in usage_turns
                    if (value := getattr(turn, name)) is not None
                ]
                source_components[name] = sum(values) if values else None

            turn_outcomes = Counter(turn.status.value for turn in bundle.turns)
            finding_severity = Counter(finding.severity for finding in analysis.findings)
            runs.append({
                "runId": bundle.run.run_id,
                "sourceKind": bundle.session.source_kind,
                "sourcePartial": analysis.evidence_coverage["event_stream"] == "partial",
                "startedAt": bundle.run.started_at.isoformat(),
                "status": bundle.run.status.value,
                "sourceOutcome": bundle.run.status.value,
                "agentName": bundle.run.agent_name or None,
                "agentVersion": bundle.run.agent_version or None,
                "serviceName": bundle.run.service_name or None,
                "environment": bundle.run.environment or None,
                "instanceId": bundle.run.instance_id or None,
                "sessionId": bundle.run.session_id,
                "parentRunId": bundle.run.parent_run_id,
                "turnCount": len(bundle.turns),
                "eventCount": len(bundle.events),
                "sourceTokenUsage": {
                    "totalTokens": (
                        sum(turn.total_tokens or 0 for turn in total_usage_turns)
                        if total_usage_turns else None
                    ),
                    "inputTokens": source_components["input_tokens"],
                    "cachedInputTokens": source_components["cached_input_tokens"],
                    "cacheWriteInputTokens": source_components["cache_write_input_tokens"],
                    "outputTokens": source_components["output_tokens"],
                    "reasoningOutputTokens": source_components["reasoning_output_tokens"],
                    "turns": len(usage_turns),
                    "pricedTurns": len(turn_prices),
                    "listPriceUsd": round(sum(turn_prices), 8) if turn_prices else None,
                    "state": (
                        "complete"
                        if (bundle.turns and len(total_usage_turns) == len(bundle.turns)
                            and analysis.evidence_coverage["event_stream"] == "present")
                        else "partial" if usage_turns else "not_captured"
                    ),
                },
                "metrics": analysis.metrics,
                "evidenceCoverage": analysis.evidence_coverage,
                "turnOutcomes": dict(sorted(turn_outcomes.items())),
                "findingSeverity": dict(sorted(finding_severity.items())),
                "findings": [{
                    "code": finding.code, "severity": finding.severity,
                    "message": finding.message,
                    "scope": "historical_prefix" if analysis.evidence_coverage["event_stream"] == "partial" else "run",
                    "evidenceEventIds": list(finding.evidence_event_ids),
                    "judgeUsed": finding.judge_used,
                } for finding in analysis.findings],
                "recommendations": run_recommendations(
                    bundle,
                    source_partial=local_scope_partial(
                        bundle.session.source_kind, local_revision,
                        max((turn.sequence for turn in bundle.turns), default=-1), None,
                    ),
                ),
                "scopedEvaluationEligible": scoped_evidence["coverage"] == "complete",
                "scopedEvaluationUnavailableReason": scoped_evidence["unavailableReason"],
            })
            linked_trace_ids[bundle.run.run_id].update(
                event.trace_id for event in bundle.events if event.trace_id is not None
            )
        owned_links = _owned_trace_ids(
            session, tenant, set().union(*linked_trace_ids.values()) if linked_trace_ids else set(),
        )
        for links in linked_trace_ids.values():
            links.intersection_update(owned_links)
        runs_by_id = {run["runId"]: run for run in runs}
        latest_judgment_status: dict[str, tuple[tuple[str, str], str]] = {}
        if (evaluator_fingerprint is not None and runs
                and session.table_exists("judgments") and session.table_exists("traces")):
            judgment_source = (
                "current_judgments" if session.table_exists("current_judgments")
                else "judgments"
            )
            trace_scope, trace_scope_params = _trace_tenant_scope(
                tenant, session.columns("traces"), alias="t",
            )
            placeholders = ",".join("?" for _ in runs_by_id)
            for judgment in session.execute(
                f"""SELECT j.trace_id,j.status,j.created_at,j.judgment_id
                   FROM {judgment_source} j JOIN agent_events e ON e.trace_id=j.trace_id
                   JOIN traces t ON t.trace_id=e.trace_id
                   WHERE e.tenant_id=? AND e.run_id IN ("""
                + placeholders
                + ") AND j.evaluator_fingerprint=? AND "
                + trace_scope + " "
                "ORDER BY j.created_at,j.judgment_id",  # nosec B608
                (tenant, *runs_by_id, evaluator_fingerprint, *trace_scope_params),
            ):
                key = (judgment["created_at"] or "", judgment["judgment_id"] or "")
                current = latest_judgment_status.get(judgment["trace_id"])
                if current is None or key > current[0]:
                    latest_judgment_status[judgment["trace_id"]] = (
                        key, (judgment["status"] or "completed").lower(),
                    )
        for run in runs:
            linked = linked_trace_ids[run["runId"]]
            completed = sum(
                latest_judgment_status.get(trace_id, (("", ""), "not_judged"))[1]
                == "completed" for trace_id in linked
            )
            errors = sum(
                latest_judgment_status.get(trace_id, (("", ""), "not_judged"))[1]
                == "error" for trace_id in linked
            )
            run["evaluationCoverage"] = {
                "state": "selected" if evaluator_fingerprint is not None else "not_selected",
                "evaluatorFingerprint": evaluator_fingerprint,
                "linkedTraces": len(linked),
                "judged": completed,
                "judgeErrors": errors,
                "notJudged": len(linked) - completed - errors,
                "complete": True,
            }
        shown_runs = list_rows if list_rows is not None else runs
        page_offset = 0 if selected_run_ids is not None else offset
        result = {
            "summary": {"available": available_runs, "shown": len(shown_runs)},
            "page": {
                "available": available_runs,
                "shown": len(shown_runs),
                "offset": page_offset,
                "limit": len(selected_run_ids) if selected_run_ids is not None else limit,
                "truncated": (
                    selected_run_ids is None
                    and page_offset + len(shown_runs) < available_runs
                ),
            },
            "runs": shown_runs,
        }
        if view == "list":
            result["selectedRun"] = runs[0] if runs else None
        if selected_run_ids is not None:
            result["filter"] = {
                "requested": len(selected_run_ids),
                "matched": len(shown_runs),
                "complete": len(shown_runs) == len(selected_run_ids),
            }
        return result

    if _is_postgres(configured):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError("PostgreSQL dashboard support requires the postgres extra") from exc
        with psycopg.connect(configured, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                result = builder(_PostgresSession(connection))
    else:
        path = _sqlite_path(configured)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            result = builder(_SQLiteSession(connection))
            connection.commit()
        finally:
            connection.close()
    redacted = redact_structure(result)
    if not isinstance(redacted, dict):
        raise DashboardBundleLimitError("bounded agent-run bundle exceeded redaction budget")
    # These are dashboard navigation keys, not display content. Preserve the
    # exact stored identifiers after content redaction so a finding link can
    # resolve even when an opaque ID resembles PII.
    if (not isinstance(redacted.get("runs"), list)
            or len(redacted["runs"]) != len(result["runs"])
            or any(not isinstance(row, dict) for row in redacted["runs"])):
        raise DashboardBundleLimitError("agent-run navigation exceeded redaction budget")
    for original, safe in zip(result["runs"], redacted["runs"], strict=True):
        safe["runId"] = original["runId"]
        if "parentRunId" in original:
            safe["parentRunId"] = original["parentRunId"]
        for field in ("findings", "recommendations"):
            if field not in original:
                continue
            if (not isinstance(safe.get(field), list)
                    or len(safe[field]) != len(original[field])):
                raise DashboardBundleLimitError("agent-run navigation exceeded redaction budget")
            for source_item, safe_item in zip(
                original.get(field, ()), safe.get(field, ()), strict=True,
            ):
                if not isinstance(safe_item, dict):
                    raise DashboardBundleLimitError("agent-run navigation exceeded redaction budget")
                for key in ("evidenceEventIds", "evidenceTurnIds"):
                    if key in source_item:
                        safe_item[key] = source_item[key]
    if result.get("selectedRun") is not None:
        original = result["selectedRun"]
        safe = redacted.get("selectedRun")
        if not isinstance(safe, dict):
            raise DashboardBundleLimitError("agent-run navigation exceeded redaction budget")
        safe["runId"] = original["runId"]
        safe["parentRunId"] = original["parentRunId"]
        for field in ("findings", "recommendations"):
            if (not isinstance(safe.get(field), list)
                    or len(safe[field]) != len(original[field])):
                raise DashboardBundleLimitError("agent-run navigation exceeded redaction budget")
            for source_item, safe_item in zip(
                original[field], safe[field], strict=True,
            ):
                if not isinstance(safe_item, dict):
                    raise DashboardBundleLimitError("agent-run navigation exceeded redaction budget")
                for key in ("evidenceEventIds", "evidenceTurnIds"):
                    if key in source_item:
                        safe_item[key] = source_item[key]
    return redacted


def build_agent_run_detail(
    storage: str | os.PathLike[str],
    *,
    tenant: str,
    run_id: str,
    event_limit: int = 100,
    event_offset: int = 0,
    turn_limit: int = 20,
    turn_offset: int = 0,
    event_id: str | None = None,
    turn_evaluator_fingerprint: str | None = None,
) -> dict:
    """Read one tenant-scoped run with its canonical ordered evidence timeline."""
    for name, value in (("tenant", tenant), ("run_id", run_id)):
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
            raise ValueError(f"invalid {name}")
    if (
        isinstance(event_limit, bool)
        or not isinstance(event_limit, int)
        or not 1 <= event_limit <= 200
        or isinstance(event_offset, bool)
        or not isinstance(event_offset, int)
        or event_offset < 0
        or isinstance(turn_limit, bool)
        or not isinstance(turn_limit, int)
        or not 1 <= turn_limit <= 50
        or isinstance(turn_offset, bool)
        or not isinstance(turn_offset, int)
        or turn_offset < 0
    ):
        raise ValueError("invalid event page")
    if event_id is not None and (
        not isinstance(event_id, str) or not event_id or len(event_id.encode("utf-8")) > 256
    ):
        raise ValueError("invalid event_id")
    if turn_evaluator_fingerprint is not None and (
        not isinstance(turn_evaluator_fingerprint, str)
        or len(turn_evaluator_fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in turn_evaluator_fingerprint)
    ):
        raise ValueError("invalid Turn evaluator fingerprint")
    configured = str(storage)

    def builder(session: _QuerySession) -> dict:
        from verdict.agent_judgment import (
            TOOL_EVIDENCE_MODE,
            trusted_turn_judgment,
            turn_evidence_fingerprint,
            turn_evidence_reason,
        )
        from verdict.storage.turn_tool_evidence import read_turn_tool_counts_batch
        page = agent_evidence_queries.load_run_page(
            session, tenant, run_id,
            event_limit=event_limit, event_offset=event_offset,
            turn_limit=turn_limit, turn_offset=turn_offset, event_id=event_id,
        )
        if page is None:
            raise KeyError(run_id)
        run = page["run"]
        parent_available = None
        if run["parent_run_id"]:
            parent_available = next(iter(session.execute(
                "SELECT 1 FROM agent_runs WHERE tenant_id=? AND run_id=? LIMIT 1",
                (tenant, run["parent_run_id"]),
            )), None) is not None
        related_rows = list(session.execute(
            "SELECT run_id,agent_name,status,started_at FROM agent_runs "
            "WHERE tenant_id=? AND parent_run_id=? "
            "ORDER BY started_at,run_id LIMIT 31",
            (tenant, run_id),
        ))
        children = [{
            "runId": row["run_id"], "agentName": row["agent_name"] or None,
            "status": row["status"], "startedAt": _dashboard_time(row["started_at"]),
        } for row in related_rows[:30]]
        shown = page["events"]
        shown_turns = [agent_turn_from_row(turn) for turn in page["turns"]]
        turn_judgments = {}
        local_revision = agent_evidence_queries.local_revisions(
            session, tenant, (run_id,),
        ).get(run_id)
        last_turn = session.execute(
            "SELECT MAX(sequence) AS sequence FROM agent_turns "
            "WHERE tenant_id=? AND run_id=?", (tenant, run_id),
        ).fetchone()
        last_sequence = int(last_turn["sequence"]) if last_turn["sequence"] is not None else -1
        first_capture_limit = agent_evidence_queries.first_capture_limit_sequence(
            session, tenant, run_id,
        )
        eligible_turns = [
            turn for turn in shown_turns
            if turn_evidence_reason(turn) is None
            and (first_capture_limit is None or turn.sequence < first_capture_limit)
            and not local_scope_partial(
                run["source_kind"], local_revision, last_sequence, turn.sequence,
            )
        ]
        if eligible_turns and session.table_exists("agent_turn_judgments"):
            evidence_by_turn = {}
            counts_by_turn = {}
            conditions = []
            params = [tenant, run_id]
            evaluator_clause = "AND evaluator_fingerprint=? " if turn_evaluator_fingerprint else ""
            if turn_evaluator_fingerprint:
                params.append(turn_evaluator_fingerprint)
            all_counts = read_turn_tool_counts_batch(
                session, postgres=_is_postgres(configured), tenant_id=tenant,
                turn_keys=[(turn.run_id, turn.turn_id) for turn in eligible_turns],
                session=True,
            )
            for turn in eligible_turns:
                fingerprints = {None: turn_evidence_fingerprint(turn)}
                counts = all_counts[(turn.run_id, turn.turn_id)]
                if counts.unavailable_reason is None:
                    fingerprints[TOOL_EVIDENCE_MODE] = turn_evidence_fingerprint(turn, counts)
                    counts_by_turn[turn.turn_id] = counts
                evidence_by_turn[turn.turn_id] = fingerprints
                choices = list(fingerprints.values())
                conditions.append("(turn_id=? AND evidence_fingerprint IN (" +
                                  ",".join("?" for _ in choices) + "))")
                params.extend((turn.turn_id, *choices))
            params.extend((MAX_TURN_DETAIL_EVALUATORS, len(eligible_turns) * MAX_TURN_DETAIL_EVALUATORS))
            for row in session.execute(
                "SELECT turn_id,evaluator_fingerprint,evidence_fingerprint,status,result_json FROM ("
                "SELECT turn_id,evaluator_fingerprint,evidence_fingerprint,status,result_json,"
                "ROW_NUMBER() OVER (PARTITION BY turn_id ORDER BY evaluated_at DESC,"
                "evaluator_fingerprint DESC) AS rn FROM agent_turn_judgments "
                "WHERE tenant_id=? AND run_id=? " + evaluator_clause + "AND (" + " OR ".join(conditions) + ")"
                ") ranked WHERE rn<=? ORDER BY turn_id,rn LIMIT ?", tuple(params),
            ):
                if row["turn_id"] in turn_judgments:
                    continue
                result = trusted_turn_judgment(
                    row["result_json"], tenant_id=tenant, run_id=run_id,
                    turn_id=row["turn_id"], evaluator_fingerprint=row["evaluator_fingerprint"],
                    evidence_fingerprint=row["evidence_fingerprint"], status=row["status"],
                )
                if result is None:
                    continue
                mode = result.evaluator_config.get("tool_evidence_mode")
                if mode is not None and mode != TOOL_EVIDENCE_MODE:
                    continue
                if evidence_by_turn[row["turn_id"]].get(mode) != result.evidence_fingerprint:
                    continue
                turn_judgments[row["turn_id"]] = {
                    "evaluatorFingerprint": row["evaluator_fingerprint"],
                    "toolEvidence": TOOL_EVIDENCE_MODE if mode == TOOL_EVIDENCE_MODE else "none",
                    "toolCounts": (
                        {"calls": counts_by_turn[row["turn_id"]].calls,
                         "results": counts_by_turn[row["turn_id"]].results,
                         "errorResults": counts_by_turn[row["turn_id"]].error_results,
                         "unknownResults": counts_by_turn[row["turn_id"]].unknown_results,
                         "mcpCalls": counts_by_turn[row["turn_id"]].mcp_calls,
                         "applicationCalls": counts_by_turn[row["turn_id"]].application_calls,
                         "providerHostedCalls": counts_by_turn[row["turn_id"]].provider_hosted_calls,
                         "originNotCapturedCalls": counts_by_turn[row["turn_id"]].origin_not_captured_calls,
                         "originUnusableCalls": counts_by_turn[row["turn_id"]].origin_unusable_calls}
                        if mode == TOOL_EVIDENCE_MODE else None
                    ),
                    "rubricName": result.rubric_name,
                    "rubricVersion": result.rubric_version,
                    "judgeModels": result.judge_models,
                    "status": result.status.value,
                    "dimensions": [
                        {"name": item.name, "verdict": item.verdict.value}
                        for item in result.dimensions[:MAX_DASHBOARD_DIMENSIONS]
                    ],
                }
        available = page["eventCount"]
        available_turns = page["turnCount"]
        resolved_event_offset = page["eventOffset"]
        owned_links = _owned_trace_ids(
            session, tenant, {event["trace_id"] for event in shown if event["trace_id"]},
        )
        judgment_summaries: dict[str, dict[str, object]] = {}
        if session.table_exists("judgments"):
            judgment_source = (
                "current_judgments" if session.table_exists("current_judgments")
                else "judgments"
            )
            columns = session.columns("judgments")
            dimensions_column = (
                "dimensions_json" if "dimensions_json" in columns else "dimensions"
            )
            for trace_id in owned_links:
                judgment = session.execute(
                    f"SELECT judgment_id,evaluator_fingerprint,status,{dimensions_column} AS dimensions "  # nosec B608 -- schema-selected identifier
                    f"FROM {judgment_source} WHERE trace_id=? "
                    "ORDER BY created_at DESC,judgment_id DESC LIMIT 1",
                    (trace_id,),
                ).fetchone()
                if judgment is None:
                    continue
                dimensions = judgment["dimensions"]
                if isinstance(dimensions, str):
                    try:
                        dimensions = json.loads(dimensions)
                    except json.JSONDecodeError:
                        dimensions = []
                safe_dimensions = []
                for dimension in dimensions if isinstance(dimensions, list) else []:
                    if len(safe_dimensions) >= MAX_DASHBOARD_DIMENSIONS:
                        break
                    if not isinstance(dimension, dict):
                        continue
                    name = dimension.get("name")
                    verdict = dimension.get("verdict")
                    if isinstance(name, str) and verdict in {"pass", "fail", "unclear"}:
                        safe_dimensions.append({"name": name[:80], "verdict": verdict})
                judgment_summaries[trace_id] = {
                    "judgmentId": judgment["judgment_id"],
                    "evaluatorFingerprint": judgment["evaluator_fingerprint"],
                    "status": judgment["status"],
                    "dimensions": safe_dimensions,
                }
        detail = {
            "runId": run["run_id"],
            "turnEvaluationScope": {
                "mode": "exact_evaluator" if turn_evaluator_fingerprint else "latest_valid_bounded",
                "evaluatorFingerprint": turn_evaluator_fingerprint,
                "maxEvaluatorsPerTurn": 1 if turn_evaluator_fingerprint else MAX_TURN_DETAIL_EVALUATORS,
            },
            "focusEventId": event_id,
            "sourceKind": run["source_kind"],
            "startedAt": _dashboard_time(run["started_at"]),
            "endedAt": _dashboard_time(run["ended_at"]),
            "status": run["status"],
            "agentName": run["agent_name"] or None,
            "agentVersion": run["agent_version"] or None,
            "serviceName": run["service_name"] or None,
            "environment": run["environment"] or None,
            "instanceId": run["instance_id"] or None,
            "sessionId": run["session_id"],
            "parentRunId": run["parent_run_id"],
            "parentAvailable": parent_available,
            "children": children,
            "childrenTruncated": len(related_rows) > 30,
            "producerCount": page["producerCount"],
            "turns": [{
                "turnId": turn.turn_id, "sequence": turn.sequence,
                "startedAt": _dashboard_time(turn.started_at),
                "status": turn.status.value,
                "requestState": turn.request_state.value,
                "responseState": turn.response_state.value,
                "request": turn.user_request_redacted,
                "response": turn.final_response_redacted,
                "requestTruncated": turn.request_truncated,
                "responseTruncated": turn.response_truncated,
                "evaluation": turn_judgments.get(turn.turn_id),
                "tokenUsage": {
                    "inputTokens": turn.input_tokens,
                    "cachedInputTokens": turn.cached_input_tokens,
                    "cacheWriteInputTokens": turn.cache_write_input_tokens,
                    "outputTokens": turn.output_tokens,
                    "reasoningOutputTokens": turn.reasoning_output_tokens,
                    "totalTokens": turn.total_tokens,
                    "basis": turn.token_usage_basis,
                },
            } for turn in shown_turns],
            "turnPage": {
                "available": available_turns, "shown": len(shown_turns),
                "offset": turn_offset, "limit": turn_limit,
                "truncated": turn_offset + len(shown_turns) < available_turns,
            },
            "events": [{
                "eventId": event["event_id"],
                "turnId": event["turn_id"],
                "sequence": event["sequence"],
                "timelineIndex": resolved_event_offset + index,
                "occurredAt": _dashboard_time(event["occurred_at"]),
                "type": event["event_type"],
                "status": event["status"],
                "provenance": event["provenance"],
                "privacy": event["privacy_classification"],
                "omissionReason": event["omission_reason"],
                "traceId": event["trace_id"] if event["trace_id"] in owned_links else None,
                "producerId": event["producer_id"] or None,
                "producerSequence": event["producer_sequence"],
                "parentEventId": event["parent_event_id"],
                "judgment": judgment_summaries.get(event["trace_id"]),
                "attributes": _dashboard_agent_event_attributes(
                    event["event_type"], event["attributes_json"]
                ),
            } for index, event in enumerate(shown)],
            "page": {
                "available": available,
                "shown": len(shown),
                "offset": resolved_event_offset,
                "limit": event_limit,
                "truncated": resolved_event_offset + len(shown) < available,
            },
        }
        for event in detail["events"]:
            event["issueCode"] = recorded_event_issue(
                event["type"], event["status"], event["attributes"],
            )
        return detail

    if _is_postgres(configured):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError("PostgreSQL dashboard support requires the postgres extra") from exc
        with psycopg.connect(configured, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                connection.execute("SET TRANSACTION READ ONLY")
                result = builder(_PostgresSession(connection))
    else:
        path = _sqlite_path(configured)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            result = builder(_SQLiteSession(connection))
            connection.commit()
        finally:
            connection.close()
    redacted = redact_structure(
        result,
        _max_nodes=MAX_AGENT_DETAIL_REDACTION_NODES,
        _max_characters=MAX_AGENT_DETAIL_REDACTION_CHARACTERS,
    )
    if not isinstance(redacted, dict):
        raise DashboardBundleLimitError("bounded agent-run detail exceeded redaction budget")
    for key in ("runId", "focusEventId", "parentRunId"):
        redacted[key] = result[key]
    for field, keys in (
        ("children", ("runId",)),
        ("turns", ("turnId",)),
        ("events", ("eventId", "turnId", "traceId", "parentEventId")),
    ):
        if (not isinstance(redacted.get(field), list)
                or len(redacted[field]) != len(result[field])
                or any(not isinstance(row, dict) for row in redacted[field])):
            raise DashboardBundleLimitError("agent-run navigation exceeded redaction budget")
        for original, safe in zip(result[field], redacted[field], strict=True):
            for key in keys:
                safe[key] = original[key]
    return redacted


def build_full_local_event_page(
    storage: str | os.PathLike[str], *, tenant: str, run_id: str,
    revision: str, limit: int = 100, after_ordinal: int | None = None,
    after_event_id: str | None = None,
) -> dict:
    """Page retained recorded events only after one snapshot proves coverage."""
    if (not tenant or not run_id or len(tenant.encode()) > 256
            or len(run_id.encode()) > 256 or not re.fullmatch(r"[0-9a-f]{64}", revision)
            or type(limit) is not int or not 1 <= limit <= 100
            or ((after_ordinal is None) != (after_event_id is None))
            or (after_ordinal is not None and (type(after_ordinal) is not int
                                               or after_ordinal < 0
                                               or not isinstance(after_event_id, str)
                                               or len(after_event_id.encode()) > 256))):
        raise ValueError("invalid local event page request")
    configured = str(storage)

    def read(session: _QuerySession) -> dict:
        current_revision, positions = verified_local_positions(session, tenant, run_id)
        if current_revision != revision:
            raise ValueError("stale_revision")
        start = 0
        if after_ordinal is not None:
            start = next((index + 1 for index, position in enumerate(positions)
                          if position[0] == after_ordinal
                          and position[1] == after_event_id), -1)
            if start < 0:
                raise ValueError("stale_cursor")
        chosen = positions[start:start + limit]
        prefix_ids = [row[1] for row in chosen if not row[5]]
        fact_ids = [row[1] for row in chosen if row[5]]
        details: dict[str, dict] = {}
        for table, ids in (("agent_events", prefix_ids),
                           ("local_recent_event_facts", fact_ids)):
            if not ids:
                continue
            slots = ",".join("?" for _ in ids)
            for row in session.execute(
                "SELECT event_id,turn_id,sequence,occurred_at,event_type,status,"
                "provenance,attributes_json,trace_id"
                + (",first_seen_revision" if table == "local_recent_event_facts"
                   else ",NULL AS first_seen_revision")
                + f" FROM {table} WHERE tenant_id=? AND run_id=? "
                + f"AND event_id IN ({slots})", (tenant, run_id, *ids),
            ):
                details[row["event_id"]] = row
        if len(details) != len(chosen):
            raise RuntimeError("missing local event page fact")
        owned_links = _owned_trace_ids(session, tenant, {
            row["trace_id"] for row in details.values()
            if isinstance(row["trace_id"], str)
        })
        events = []
        for ordinal, event_id, turn_id, turn_sequence, event_sequence, _fact in chosen:
            row = details[event_id]
            if row["turn_id"] != turn_id or row["sequence"] != event_sequence:
                raise RuntimeError("changed local event page fact")
            attributes = _dashboard_agent_event_attributes(
                row["event_type"], row["attributes_json"],
            )
            events.append({
                "sourceOrdinal": ordinal, "eventId": event_id,
                "turnId": turn_id, "turnSequence": turn_sequence,
                "eventSequence": event_sequence, "occurredAt": row["occurred_at"],
                "type": row["event_type"], "status": row["status"],
                "provenance": row["provenance"], "attributes": attributes,
                "issueCode": recorded_event_issue(
                    row["event_type"], row["status"], attributes,
                ),
                "traceId": (row["trace_id"] if row["trace_id"] in owned_links else None),
                "firstSeenRevision": row["first_seen_revision"],
            })
        next_cursor = (
            {"sourceOrdinal": chosen[-1][0], "eventId": chosen[-1][1]}
            if chosen and start + len(chosen) < len(positions) else None
        )
        return {"sourceRevision": current_revision,
                "sourceEventCount": len(positions),
                "coverage": "complete_recorded", "events": events,
                "nextCursor": next_cursor}

    if _is_postgres(configured):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(configured, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                connection.execute("SET TRANSACTION READ ONLY")
                result = read(_PostgresSession(connection))
    else:
        connection = sqlite3.connect(f"file:{_sqlite_path(configured)}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            result = read(_SQLiteSession(connection))
            connection.commit()
        finally:
            connection.close()
    safe = redact_structure(result, _max_nodes=MAX_AGENT_DETAIL_REDACTION_NODES,
                            _max_characters=MAX_AGENT_DETAIL_REDACTION_CHARACTERS)
    if (not isinstance(safe, dict) or not isinstance(safe.get("events"), list)
            or len(safe["events"]) != len(result["events"])):
        raise DashboardBundleLimitError("local event page exceeded redaction budget")
    return safe


def build_full_local_run_analysis(
    storage: str | os.PathLike[str], *, tenant: str, run_id: str,
) -> dict:
    """Analyze one admitted local run from all recorded typed evidence."""
    if (not tenant or not run_id or len(tenant.encode()) > 256
            or len(run_id.encode()) > 256):
        raise ValueError("invalid local run analysis request")
    configured = str(storage)

    def read(session: _QuerySession) -> dict:
        revision, positions = verified_local_positions(session, tenant, run_id)
        bundles = agent_evidence_queries.load_bundles(
            session, tenant, limit=1, run_ids=(run_id,),
        )
        if len(bundles) != 1:
            raise KeyError(run_id)
        full = complete_local_bundle(session, tenant, bundles[0], positions)
        analysis = analyze_agent_run(full, include_tool_outcome_coverage=True)
        recommendations = run_recommendations(full)
        event_indexes = {position[1]: index for index, position in enumerate(positions)}
        def evidence_cursor(event_id: str | None):
            index = event_indexes.get(event_id) if event_id else None
            if index is None or index == 0:
                return None
            previous = positions[index - 1]
            return {"sourceOrdinal": previous[0], "eventId": previous[1]}
        return {
            "runId": run_id, "sourceRevision": revision,
            "coverage": "complete_recorded",
            "recordedTurns": len(full.turns), "recordedEvents": len(full.events),
            "metrics": analysis.metrics,
            "evidenceCoverage": analysis.evidence_coverage,
            "findings": [{
                "code": finding.code, "severity": finding.severity,
                "message": finding.message,
                "evidenceEventIds": list(finding.evidence_event_ids),
                "firstEvidenceCursor": evidence_cursor(
                    finding.evidence_event_ids[0] if finding.evidence_event_ids else None
                ),
            } for finding in analysis.findings[:20]],
            "findingsTruncated": len(analysis.findings) > 20,
            "recommendations": [{
                **item,
                "firstEvidenceCursor": evidence_cursor(
                    item["evidenceEventIds"][0] if item["evidenceEventIds"] else None
                ),
            } for item in recommendations],
        }

    if _is_postgres(configured):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(configured, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                connection.execute("SET TRANSACTION READ ONLY")
                result = read(_PostgresSession(connection))
    else:
        connection = sqlite3.connect(f"file:{_sqlite_path(configured)}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            result = read(_SQLiteSession(connection))
            connection.commit()
        finally:
            connection.close()
    safe = redact_structure(result, _max_nodes=MAX_AGENT_DETAIL_REDACTION_NODES,
                            _max_characters=MAX_AGENT_DETAIL_REDACTION_CHARACTERS)
    if (not isinstance(safe, dict) or not isinstance(safe.get("findings"), list)
            or len(safe["findings"]) != len(result["findings"])):
        raise DashboardBundleLimitError("local run analysis exceeded redaction budget")
    safe["runId"] = run_id
    for original, redacted in zip(result["findings"], safe["findings"], strict=True):
        redacted["evidenceEventIds"] = original["evidenceEventIds"]
    return safe


def build_recent_local_sample(
    storage: str | os.PathLike[str], *, tenant: str, run_id: str,
    revision: str | None = None, turn_id: str | None = None,
    event_id: str | None = None,
) -> dict:
    """Read the bounded recent window from the same source snapshot."""
    for name, value in (("tenant", tenant), ("run_id", run_id)):
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
            raise ValueError(f"invalid {name}")
    configured = str(storage)

    def verify_facts(session: _QuerySession, sample: dict) -> None:
        if sample.get("durableFacts") is not True:
            return  # Older inspection-only V2 rows have no durable fact contract.
        events = sample.get("events")
        prefix_count = sample.get("canonicalPrefixEvents")
        source_count = sample.get("sourceEventCount")
        if (not isinstance(events, list) or len(events) > 50
                or type(prefix_count) is not int or prefix_count < 0
                or type(source_count) is not int or source_count < prefix_count):
            raise ValueError("invalid durable recent local index")
        ids = [event.get("eventId") for event in events if isinstance(event, dict)]
        ordinals = [event.get("sourceOrdinal") for event in events if isinstance(event, dict)]
        if (len(ids) != len(events) or len(set(ids)) != len(ids)
                or any(not isinstance(value, str) or not value for value in ids)
                or len(ordinals) != len(events)
                or any(type(value) is not int or value < 0 for value in ordinals)
                or ordinals != sorted(set(ordinals))
                or any(value >= source_count for value in ordinals)
                or sample.get("firstOrdinal") != (ordinals[0] if ordinals else None)
                or sample.get("lastOrdinal") != (ordinals[-1] if ordinals else None)):
            raise ValueError("invalid durable recent local index")
        if not ids:
            return
        slots = ",".join("?" for _ in ids)
        facts = list(session.execute(
            "SELECT * FROM local_recent_event_facts WHERE tenant_id=? AND run_id=? "
            f"AND event_id IN ({slots})", (tenant, run_id, *ids),
        ))
        prefix = list(session.execute(
            "SELECT * FROM agent_events WHERE tenant_id=? AND run_id=? "
            f"AND event_id IN ({slots})", (tenant, run_id, *ids),
        ))
        prefix_order = list(session.execute(
            "SELECT e.event_id,t.sequence AS turn_sequence FROM agent_events e "
            "JOIN agent_turns t ON t.tenant_id=e.tenant_id AND t.run_id=e.run_id "
            "AND t.turn_id=e.turn_id WHERE e.tenant_id=? AND e.run_id=? "
            "AND e.provenance NOT IN ('verdict:capture_limit',"
            "'verdict:partial_source_line') ORDER BY t.sequence,e.sequence",
            (tenant, run_id),
        ))
        if len(prefix_order) != prefix_count:
            raise ValueError("invalid durable recent local prefix")
        prefix_position = {
            row["event_id"]: (ordinal, row["turn_sequence"])
            for ordinal, row in enumerate(prefix_order)
        }
        fact_by_id = {row["event_id"]: row for row in facts}
        prefix_by_id = {row["event_id"]: row for row in prefix}
        for item in events:
            ordinal = item.get("sourceOrdinal")
            turn_sequence = item.get("turnSequence")
            event_sequence = item.get("eventSequence")
            turn_id = item.get("turnId")
            if (type(turn_sequence) is not int or turn_sequence < 0
                    or type(event_sequence) is not int or event_sequence < 0
                    or not isinstance(turn_id, str) or not turn_id):
                raise ValueError("invalid durable recent local locator")
            stored = (prefix_by_id if ordinal < prefix_count else fact_by_id).get(
                item["eventId"]
            )
            if stored is None:
                raise ValueError("missing durable recent local event")
            if (stored["turn_id"] != item.get("turnId")
                    or stored["sequence"] != item.get("eventSequence")
                    or (ordinal < prefix_count and prefix_position.get(
                        item["eventId"]
                    ) != (ordinal, item.get("turnSequence")))
                    or (ordinal >= prefix_count and (
                        stored["source_ordinal"] != ordinal
                        or stored["turn_sequence"] != item.get("turnSequence")
                    ))):
                raise ValueError("recent local index differs from retained fact")
            event_id = item["eventId"]
            item.clear()
            item.update({
                "sourceOrdinal": ordinal, "eventId": event_id,
                "turnId": turn_id, "turnSequence": turn_sequence,
                "eventSequence": event_sequence,
                "occurredAt": str(stored["occurred_at"]),
                "type": stored["event_type"], "status": stored["status"],
                "provenance": stored["provenance"],
                "attributes": _json_value(stored["attributes_json"], {}),
                "privacy": stored["privacy_classification"],
                "omissionReason": stored["omission_reason"],
                "traceId": stored["trace_id"],
                "firstSeenRevision": (
                    stored["first_seen_revision"] if ordinal >= prefix_count else None
                ),
            })

    def read(session: _QuerySession) -> dict:
        if not session.table_exists("local_recent_events"):
            raise KeyError(run_id)
        row = session.execute(
            "SELECT sample.sample_json,sample.source_prefix_bytes,"
            "sample.source_prefix_sha256,source.local_full_bytes,"
            "source.local_full_sha256,source.local_prefix_bytes,"
            "source.local_prefix_sha256,source.local_source_turn_count,"
            "source.local_source_event_count,source.local_partial_line "
            "FROM local_recent_events sample "
            "JOIN agent_runs run ON run.tenant_id=sample.tenant_id "
            "AND run.run_id=sample.run_id "
            "JOIN import_sources source ON source.tenant_id=run.tenant_id "
            "AND source.source_session_id=run.source_session_id "
            "WHERE sample.tenant_id=? AND sample.run_id=?",
            (tenant, run_id),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        raw_sample = row["sample_json"]
        if not isinstance(raw_sample, str) or len(raw_sample.encode("utf-8")) > 1_048_576:
            raise ValueError("invalid stored recent local sample")
        try:
            sample = json.loads(raw_sample)
        except (ValueError, RecursionError) as exc:
            raise ValueError("invalid recent local sample") from exc
        if not isinstance(sample, dict) or sample.get("runId") != run_id:
            raise ValueError("invalid recent local sample")
        events = sample.get("events")
        if (not isinstance(events, list) or len(events) > 50
                or any(not isinstance(event, dict) for event in events)):
            raise ValueError("invalid bounded recent local events")
        if sample.get("schema") == "local-recent-window-v2":
            if (row["source_prefix_bytes"] != row["local_full_bytes"]
                    or row["source_prefix_sha256"] != row["local_full_sha256"]
                    or sample.get("sourceRevision") != row["local_full_sha256"]
                    or sample.get("sourceTurnCount") != row["local_source_turn_count"]
                    or sample.get("sourceEventCount") != row["local_source_event_count"]):
                raise KeyError(run_id)
            verify_facts(session, sample)
            sample["findings"] = verified_recent_findings(
                sample, partial_final_line=bool(row["local_partial_line"]),
            )
        elif sample.get("schema") == "local-recent-events-v1":
            if (row["source_prefix_bytes"] != row["local_prefix_bytes"]
                    or row["source_prefix_sha256"] != row["local_prefix_sha256"]):
                raise KeyError(run_id)
        else:
            raise ValueError("invalid recent local sample")
        current = None
        if revision is not None and event_id is not None:
            current = next((event for event in events
                            if event.get("eventId") == event_id
                            and event.get("turnId") == turn_id), None)
            if current is not None:
                proof = current.get("firstSeenRevision") or sample.get("sourceRevision")
                if revision != proof:
                    raise KeyError("stale_revision")
                sample["focusedEvent"] = deepcopy(current)
                sample["historicalFocusedEvent"] = False
        if (current is None and revision is not None and event_id is not None
                and sample.get("sourceRevision") != revision):
            retained = session.execute(
                "SELECT * FROM local_recent_event_facts WHERE tenant_id=? AND run_id=? "
                "AND turn_id=? AND event_id=? AND first_seen_revision=?",
                (tenant, run_id, turn_id, event_id, revision),
            ).fetchone()
            if retained is None:
                raise KeyError("stale_revision")
            sample["focusedEvent"] = {
                "sourceOrdinal": retained["source_ordinal"],
                "eventId": retained["event_id"], "turnId": retained["turn_id"],
                "turnSequence": retained["turn_sequence"],
                "eventSequence": retained["sequence"],
                "occurredAt": str(retained["occurred_at"]),
                "type": retained["event_type"], "status": retained["status"],
                "provenance": retained["provenance"],
                "attributes": _json_value(retained["attributes_json"], {}),
                "privacy": retained["privacy_classification"],
                "omissionReason": retained["omission_reason"],
                "traceId": retained["trace_id"],
                "firstSeenRevision": retained["first_seen_revision"],
            }
            sample["historicalFocusedEvent"] = True
        linked_events = list(events)
        focused = sample.get("focusedEvent")
        if focused is not None:
            if not isinstance(focused, dict):
                raise ValueError("invalid focused recent local event")
            linked_events.append(focused)
        owned_links = _owned_trace_ids(session, tenant, {
            trace_id for event in linked_events
            if isinstance(trace_id := event.get("traceId"), str)
        })
        for event in linked_events:
            trace_id = event.get("traceId")
            if "traceId" in event and (not isinstance(trace_id, str)
                                       or trace_id not in owned_links):
                event["traceId"] = None
        return sample

    if _is_postgres(configured):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(configured, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                connection.execute("SET TRANSACTION READ ONLY")
                sample = read(_PostgresSession(connection))
    else:
        path = _sqlite_path(configured)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            sample = read(_SQLiteSession(connection))
            connection.commit()
        finally:
            connection.close()
    redacted = redact_structure(
        sample, _max_nodes=MAX_AGENT_DETAIL_REDACTION_NODES,
        _max_characters=MAX_AGENT_DETAIL_REDACTION_CHARACTERS,
    )
    if (not isinstance(redacted, dict)
            or not isinstance(redacted.get("events"), list)
            or len(redacted["events"]) != len(sample["events"])
            or len(redacted["events"]) > 50):
        raise DashboardBundleLimitError("recent local sample exceeded redaction budget")
    for original, safe in zip(sample["events"], redacted["events"], strict=True):
        if (not isinstance(safe, dict)
                or safe.get("sourceOrdinal") != original.get("sourceOrdinal")):
            raise DashboardBundleLimitError("recent local event lost its structure")
        if "traceId" in original:
            safe["traceId"] = original["traceId"]
    if "focusedEvent" in sample:
        focused = redacted.get("focusedEvent")
        if (not isinstance(focused, dict)
                or focused.get("sourceOrdinal") != sample["focusedEvent"].get("sourceOrdinal")):
            raise DashboardBundleLimitError("focused recent local event lost its structure")
        if "traceId" in sample["focusedEvent"]:
            focused["traceId"] = sample["focusedEvent"]["traceId"]
    return redacted


def build_agent_turn_analysis(
    storage: str | os.PathLike[str], *, tenant: str, run_id: str, turn_id: str,
) -> dict:
    """Review one tenant-scoped turn without sending content to a judge."""
    for name, value in (("tenant", tenant), ("run_id", run_id), ("turn_id", turn_id)):
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
            raise ValueError(f"invalid {name}")
    configured = str(storage)

    def read(session: _QuerySession) -> dict:
        if not agent_evidence_queries.available(session):
            raise KeyError(turn_id)
        turn = session.execute(
            "SELECT t.request_state,t.response_state,t.sequence,s.source_kind "
            "FROM agent_turns t "
            "JOIN agent_runs r ON r.tenant_id=t.tenant_id AND r.run_id=t.run_id "
            "JOIN import_sources s ON s.tenant_id=r.tenant_id "
            "AND s.source_session_id=r.source_session_id "
            "WHERE t.tenant_id=? AND t.run_id=? AND t.turn_id=?",
            (tenant, run_id, turn_id),
        ).fetchone()
        if turn is None:
            if not session.table_exists("local_tail_turn_facts"):
                raise KeyError(turn_id)
            tail = session.execute(
                "SELECT f.payload_json FROM local_tail_turn_facts f "
                "JOIN agent_runs r ON r.tenant_id=f.tenant_id AND r.run_id=f.run_id "
                "WHERE f.tenant_id=? AND f.run_id=? AND f.turn_id=?",
                (tenant, run_id, turn_id),
            ).fetchone()
            if tail is None:
                raise KeyError(turn_id)
            try:
                _, positions = verified_local_positions(session, tenant, run_id)
            except ValueError as exc:
                raise KeyError(turn_id) from exc
            try:
                payload = json.loads(tail["payload_json"])
                request_state = EvidenceState(payload["request_state"]).value
                response_state = EvidenceState(payload["response_state"]).value
            except (TypeError, ValueError, KeyError) as exc:
                raise RuntimeError("invalid local tail turn") from exc
            available = sum(position[2] == turn_id for position in positions)
            rows = list(session.execute(
                "SELECT event_id,event_type,status,provenance,attributes_json "
                "FROM local_recent_event_facts WHERE tenant_id=? AND run_id=? "
                "AND turn_id=? ORDER BY sequence,event_id LIMIT ?",
                (tenant, run_id, turn_id, MAX_TURN_ANALYSIS_EVENTS + 1),
            ))
            if len(rows) != min(available, MAX_TURN_ANALYSIS_EVENTS + 1):
                raise RuntimeError("invalid local tail events")
            events = [{**row, "attributes": _dashboard_agent_event_attributes(
                row["event_type"], row["attributes_json"],
            )} for row in rows[:MAX_TURN_ANALYSIS_EVENTS]]
            return analyze_turn_events(
                run_id=run_id, turn_id=turn_id, rows=events,
                available=available, request_state=request_state,
                response_state=response_state,
            )
        last_turn = session.execute(
            "SELECT MAX(sequence) AS sequence FROM agent_turns "
            "WHERE tenant_id=? AND run_id=?", (tenant, run_id),
        ).fetchone()
        source_partial = local_scope_partial(
            turn["source_kind"],
            agent_evidence_queries.local_revisions(session, tenant, (run_id,)).get(run_id),
            int(last_turn["sequence"]), int(turn["sequence"]),
        )
        first_capture_limit = agent_evidence_queries.first_capture_limit_sequence(
            session, tenant, run_id,
        )
        source_limit = (first_capture_limit is not None
                        and first_capture_limit <= int(turn["sequence"]))
        count = session.execute(
            "SELECT COUNT(*) AS count,COUNT(DISTINCT producer_id) AS producers FROM agent_events "
            "WHERE tenant_id=? AND run_id=? AND turn_id=?",
            (tenant, run_id, turn_id),
        ).fetchone()
        # SQL equivalent of event_order_key; run_id is fixed by the WHERE clause.
        rows = list(session.execute(
            "SELECT event_id,event_type,status,provenance,attributes_json FROM agent_events "
            "WHERE tenant_id=? AND run_id=? AND turn_id=? "
            "ORDER BY occurred_at,producer_id,COALESCE(producer_sequence,sequence),event_id "
            "LIMIT ?",
            (tenant, run_id, turn_id, MAX_TURN_ANALYSIS_EVENTS),
        ))
        events = [{**row, "attributes": _dashboard_agent_event_attributes(
            row["event_type"], row["attributes_json"]
        )} for row in rows]
        return analyze_turn_events(
            run_id=run_id, turn_id=turn_id, rows=events,
            available=int(count["count"]), request_state=turn["request_state"],
            response_state=turn["response_state"],
            producer_count=int(count["producers"]),
            source_partial_from_run=source_limit or source_partial,
        )

    if _is_postgres(configured):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError("PostgreSQL dashboard support requires the postgres extra") from exc
        with psycopg.connect(configured, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                connection.execute("SET TRANSACTION READ ONLY")
                result = read(_PostgresSession(connection))
    else:
        path = _sqlite_path(configured)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            result = read(_SQLiteSession(connection))
            connection.commit()
        finally:
            connection.close()
    redacted = redact_structure(result)
    if not isinstance(redacted, dict):
        raise DashboardBundleLimitError("bounded turn analysis exceeded redaction budget")
    return redacted


def build_agent_insights_bundle(
    storage: str | os.PathLike[str], *, tenant: str, scan_limit: int = 10_000,
    _include_input_fingerprint: bool = False,
) -> dict:
    """Aggregate evidence coverage and judge-free findings across captured runs.

    The result never includes request, response, tool, or command content. When
    a tenant exceeds ``scan_limit``, the response is explicitly marked partial.
    """
    if not isinstance(tenant, str) or not tenant or len(tenant.encode("utf-8")) > 256:
        raise ValueError("invalid tenant")
    if (
        isinstance(scan_limit, bool)
        or not isinstance(scan_limit, int)
        or not 1 <= scan_limit <= 100_000
    ):
        raise ValueError("invalid scan limit")
    configured = str(storage)

    def builder(session: _QuerySession) -> dict:
        if not agent_evidence_queries.available(session):
            return _empty_agent_insights()
        evidence_revision = _evidence_revision(session, tenant)
        available = agent_evidence_queries.count_runs(session, tenant)
        input_hasher = hashlib.sha256()
        bundles = agent_evidence_queries.iter_capture_snapshots(
            session, tenant, limit=scan_limit,
        )
        event_types: Counter[str] = Counter()
        event_statuses: Counter[str] = Counter()
        run_outcomes: Counter[str] = Counter()
        turn_outcomes: Counter[str] = Counter()
        prompt_states: Counter[str] = Counter()
        response_states: Counter[str] = Counter()
        finding_counts: Counter[tuple[str, str]] = Counter()
        prefix_finding_counts: Counter[tuple[str, str]] = Counter()
        finding_messages: dict[tuple[str, str], str] = {}
        finding_run_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
        source_metrics: dict[str, Counter[str]] = defaultdict(Counter)
        source_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
        list_price_by_source: dict[str, float] = defaultdict(float)
        totals: Counter[str] = Counter()
        trace_scope = {"available": 0, "analyzed": 0, "complete": True}
        trace_metrics: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        behavior = Counter()
        trace_tokens = Counter()
        trace_latency_values: list[float] = []
        trace_cost_values: list[float] = []
        trace_outcomes: Counter[str] = Counter()
        trace_evidence: Counter[str] = Counter()
        trace_not_evaluable: Counter[str] = Counter()
        trace_operations: Counter[str] = Counter()
        trace_finish_reasons: Counter[str] = Counter()
        if session.table_exists("traces"):
            trace_tenant_predicate = (
                "(tenant_id=? OR tenant_id IS NULL OR tenant_id='')"
                if tenant == "__verdict_local__"
                else "tenant_id=?"
            )
            trace_count_row = session.execute(
                f"SELECT COUNT(*) AS count FROM traces WHERE {trace_tenant_predicate}",  # nosec B608 -- fixed predicate
                (tenant,),
            ).fetchone()
            trace_available = int(trace_count_row["count"] if trace_count_row else 0)
            trace_rows = list(session.execute(
                "SELECT provider, request_model, operation, finish_reason, input_tokens, "
                "output_tokens, latency_ms, cost_usd, error, prompt_redacted, "
                "response_redacted, ended_at "
                f"FROM traces WHERE {trace_tenant_predicate} "  # nosec B608 -- fixed predicate
                "ORDER BY started_at ASC, trace_id ASC LIMIT ?",
                (tenant, scan_limit),
            ))
            for trace in trace_rows:
                input_hasher.update(json.dumps(
                    {key: trace[key] for key in trace.keys()},
                    sort_keys=True, separators=(",", ":"), default=str,
                ).encode("utf-8"))
            trace_scope = {
                "available": trace_available,
                "analyzed": len(trace_rows),
                "complete": len(trace_rows) == trace_available,
            }
            for trace in trace_rows:
                provider = trace["provider"] or "unknown"
                model = trace["request_model"] or "unknown"
                metrics = trace_metrics[(provider, model)]
                metrics["traces"] += 1
                trace_operations[str(trace["operation"] or "unknown")] += 1
                trace_finish_reasons[str(trace["finish_reason"] or "unknown")] += 1
                for name in ("input_tokens", "output_tokens"):
                    value = trace[name]
                    if isinstance(value, int):
                        trace_tokens[name] += value
                        trace_tokens[f"{name}_known"] += 1
                        metrics[name] += value
                        metrics[f"{name}_known"] += 1
                state = trace_evidence_state(
                    error=trace["error"], ended_at=trace["ended_at"],
                )
                trace_outcomes[state] += 1
                if state == "error_recorded":
                    metrics["errors"] += 1
                latency = trace["latency_ms"]
                if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                    trace_latency_values.append(float(latency))
                    metrics["latency_known"] += 1
                    metrics["latency_ms"] += float(latency)
                cost = trace["cost_usd"]
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    trace_cost_values.append(float(cost))
                    metrics["cost_known"] += 1
                    metrics["cost_microusd"] += round(float(cost) * 1_000_000)
                prompt = trace["prompt_redacted"]
                response = trace["response_redacted"]
                facts = deterministic_trace_facts(
                    error=trace["error"], prompt=prompt, response=response,
                    ended_at=trace["ended_at"],
                )
                trace_evidence["prompt_present"] += int(facts["prompt_present"])
                trace_evidence["response_present"] += int(facts["response_present"])
                evidence_reason = facts["not_evaluable_reason"]
                if evidence_reason:
                    trace_not_evaluable[evidence_reason] += 1
                else:
                    trace_evidence["judge_eligible"] += 1
                if facts["response_present"]:
                    behavior["captured_responses"] += 1
                    behavior["response_characters"] += int(
                        facts["response_characters"] or 0
                    )
                    behavior["refusals"] += int(facts["refusal_signature"])
                    behavior["apology_starts"] += int(facts["apology_start"])
                    behavior["hedges"] += int(facts["hedge_phrases"] or 0)
                    behavior["valid_json"] += int(facts["valid_json"])
        def capture_pages_with_owned_links():
            expansion_remaining = MAX_INSIGHT_LOCAL_EXPANSION_ROWS
            while page := list(islice(bundles, 100)):
                expanded = []
                for bundle, revision in page:
                    complete = False
                    if (bundle.session.source_kind in {"codex", "claude-code"}
                            and revision is not None
                            and revision.source_turn_count is not None
                            and revision.source_event_count is not None
                            and revision.v1_turn_count is not None
                            and revision.v1_event_count is not None):
                        extra = (revision.source_turn_count - revision.v1_turn_count
                                 + revision.source_event_count - revision.v1_event_count)
                        if 0 < extra <= expansion_remaining:
                            try:
                                _, positions = verified_local_positions(
                                    session, tenant, bundle.run.run_id,
                                )
                            except ValueError as exc:
                                if str(exc) != "partial_projection":
                                    raise
                            else:
                                bundle = complete_local_bundle(session, tenant, bundle, positions)
                                expansion_remaining -= extra
                                complete = True
                    expanded.append((bundle, revision, complete))
                owned = _owned_trace_ids(session, tenant, {
                    event.trace_id for bundle, _revision, _complete in expanded
                    for event in bundle.events
                    if event.event_type.value == "model_call" and event.trace_id
                })
                for bundle, revision, complete in expanded:
                    yield bundle, revision, owned, complete

        analyzed = 0
        partial_runs = 0
        for bundle, local_revision, owned_links, complete_local in capture_pages_with_owned_links():
            input_hasher.update(normalized_bundle_digest(bundle).encode("ascii"))
            # Ownership changes the linked-call result even when the Agent
            # Event row is unchanged. Include the effective projection in the
            # immutable analysis identity so prior snapshots are not reused.
            input_hasher.update(json.dumps(sorted({
                event.trace_id for event in bundle.events
                if event.trace_id in owned_links
            }), separators=(",", ":")).encode("utf-8"))
            input_hasher.update(json.dumps(
                None if local_revision is None else [
                    local_revision.full_bytes, local_revision.full_sha256,
                    local_revision.complete_prefix_bytes,
                    local_revision.complete_prefix_sha256,
                    local_revision.partial_final_line,
                    local_revision.source_turn_count,
                    local_revision.source_event_count,
                    local_revision.v1_turn_count,
                    local_revision.v1_event_count,
                    local_revision.min_turn_at,
                    local_revision.max_turn_at,
                ], separators=(",", ":"),
            ).encode("ascii"))
            analyzed += 1
            analysis = analyze_agent_run(
                bundle, include_tool_outcome_coverage=True,
                **({} if complete_local else {"local_revision": local_revision}),
            )
            source = bundle.session.source_kind
            source_partial = analysis.evidence_coverage["event_stream"] == "partial"
            if source_partial:
                partial_runs += 1
                source_metrics[source]["partial_runs"] += 1
            else:
                run_outcomes[bundle.run.status.value] += 1
                source_outcomes[source][bundle.run.status.value] += 1
            source_metrics[source]["runs"] += 1
            source_metrics[source]["child_runs"] += int(bundle.run.parent_run_id is not None)
            turn_models = _models_by_turn(bundle.events)
            for turn in bundle.turns:
                turn_outcomes[turn.status.value] += 1
                list_price = _turn_list_price_usd(turn, turn_models.get(turn.turn_id, frozenset()))
                if list_price is None:
                    source_metrics[source]["unpriced_turns"] += 1
                else:
                    source_metrics[source]["priced_turns"] += 1
                    list_price_by_source[source] += list_price
                prompt_states[turn.request_state.value] += 1
                response_states[turn.response_state.value] += 1
                source_metrics[source]["turns"] += 1
                source_metrics[source]["final_responses"] += int(
                    turn.response_state is EvidenceState.PRESENT
                )
                source_metrics[source]["truncated_responses"] += int(
                    turn.response_truncated
                )
                if turn.token_usage_basis is not None:
                    source_metrics[source]["token_usage_turns"] += 1
                if turn.total_tokens is not None:
                    source_metrics[source]["total_token_turns"] += 1
                    source_metrics[source]["total_tokens"] += turn.total_tokens
                for name in (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                ):
                    value = getattr(turn, name)
                    if value is not None:
                        source_metrics[source][name] += value
                        source_metrics[source][f"{name}_known"] += 1
            totals["turns"] += len(bundle.turns)
            totals["events"] += len(bundle.events)
            for event in bundle.events:
                event_types[event.event_type.value] += 1
                event_statuses[event.status.value] += 1
                if event.event_type.value == "model_call":
                    totals["model_calls"] += 1
                    source_metrics[source]["model_calls"] += 1
                    if event.trace_id in owned_links:
                        totals["linked_model_calls"] += 1
                elif event.event_type.value == "tool_call":
                    totals["tool_calls"] += 1
                    source_metrics[source]["tool_calls"] += 1
                elif event.event_type.value == "retry":
                    totals["retries"] += 1
                    source_metrics[source]["retries"] += 1
                reported_metric = _OUTCOME_REPORTED_METRICS.get(event.event_type.value)
                if reported_metric is not None and outcome_reported(event):
                    totals[reported_metric] += 1
                    source_metrics[source][reported_metric] += 1
            for name in ("tool_errors", "command_failures", "test_failures"):
                value = analysis.metrics.get(name)
                if isinstance(value, int):
                    totals[name] += value
                    source_metrics[source][name] += value
            for finding in analysis.findings:
                key = (finding.code, finding.severity)
                finding_counts[key] += 1
                if source_partial:
                    prefix_finding_counts[key] += 1
                finding_messages.setdefault(key, finding.message)
                if len(finding_run_ids[key]) < 50:
                    finding_run_ids[key].append(bundle.run.run_id)
        findings = [
            {
                "code": code,
                "severity": severity,
                "message": _FINDING_SUMMARIES.get(code, finding_messages[(code, severity)]),
                "runs": count,
                "partialRuns": prefix_finding_counts[(code, severity)],
                "runIds": finding_run_ids[(code, severity)],
                "runIdsTruncated": count > len(finding_run_ids[(code, severity)]),
            }
            for (code, severity), count in sorted(
                finding_counts.items(), key=lambda item: (-item[1], item[0][0])
            )[:50]
        ]
        source_activity = []
        for source, metrics in sorted(source_metrics.items()):
            token_usage_turns = metrics["token_usage_turns"]
            total_token_turns = metrics["total_token_turns"]
            turns = metrics["turns"]
            source_activity.append({
                "source": source,
                "runs": metrics["runs"],
                "partialRuns": metrics["partial_runs"],
                "childRuns": metrics["child_runs"],
                "turns": turns,
                "finalResponses": metrics["final_responses"],
                "truncatedResponses": metrics["truncated_responses"],
                "toolCalls": metrics["tool_calls"],
                "toolErrors": metrics["tool_errors"],
                "commandFailures": metrics["command_failures"],
                "testFailures": metrics["test_failures"],
                "toolOutcomesReported": metrics["tool_outcomes_reported"],
                "commandOutcomesReported": metrics["command_outcomes_reported"],
                "testOutcomesReported": metrics["test_outcomes_reported"],
                "listPriceUsd": (
                    round(list_price_by_source[source], 6) if metrics["priced_turns"] else None
                ),
                "pricedTurns": metrics["priced_turns"],
                "inputTokens": (
                    metrics["input_tokens"] if metrics["input_tokens_known"] else None
                ),
                "cachedInputTokens": (
                    metrics["cached_input_tokens"]
                    if metrics["cached_input_tokens_known"] else None
                ),
                "cacheWriteInputTokens": (
                    metrics["cache_write_input_tokens"]
                    if metrics["cache_write_input_tokens_known"] else None
                ),
                "outputTokens": (
                    metrics["output_tokens"] if metrics["output_tokens_known"] else None
                ),
                "reasoningOutputTokens": (
                    metrics["reasoning_output_tokens"]
                    if metrics["reasoning_output_tokens_known"] else None
                ),
                "totalTokens": metrics["total_tokens"] if total_token_turns else None,
                "tokenUsageTurns": token_usage_turns,
                "tokenUsageState": (
                    "complete" if turns and total_token_turns == turns and not metrics["partial_runs"]
                    else "partial" if token_usage_turns else "not_captured"
                ),
                "runOutcomes": dict(sorted(source_outcomes[source].items())),
                "retries": metrics["retries"] if source == "verdict_sdk" else None,
                "retryState": "captured" if source == "verdict_sdk" else "not_captured",
            })
        model_comparisons = []
        for (provider, model), metrics in sorted(trace_metrics.items()):
            latency_known = metrics["latency_known"]
            cost_known = metrics["cost_known"]
            model_comparisons.append({
                "provider": provider,
                "model": model,
                "traces": metrics["traces"],
                "errors": metrics["errors"],
                "inputTokens": (
                    metrics["input_tokens"] if metrics["input_tokens_known"] else None
                ),
                "outputTokens": (
                    metrics["output_tokens"] if metrics["output_tokens_known"] else None
                ),
                "averageLatencyMs": (
                    round(metrics["latency_ms"] / latency_known, 2)
                    if latency_known else None
                ),
                "costUsd": (
                    round(metrics["cost_microusd"] / 1_000_000, 8)
                    if cost_known else None
                ),
                "costKnownTraces": cost_known,
            })
        result = {
            "schema": "agent-insights-v2",
            "scope": {
                "availableRuns": available,
                "analyzedRuns": analyzed,
                "partialRuns": partial_runs,
                "complete": analyzed == available and partial_runs == 0,
                "traces": trace_scope,
            },
            "findings": findings,
            "dataHealth": {
                "counts": {"runs": analyzed, "turns": totals["turns"], "events": totals["events"]},
                "eventTypes": dict(sorted(event_types.items())),
                "eventStatuses": dict(sorted(event_statuses.items())),
                "promptStates": dict(sorted(prompt_states.items())),
                "responseStates": dict(sorted(response_states.items())),
                "traceLinks": {
                    "modelCalls": totals["model_calls"],
                    "linked": totals["linked_model_calls"],
                    "unlinked": (
                        totals["model_calls"] - totals["linked_model_calls"]
                    ),
                },
                "traceEvidence": {
                    "promptPresent": trace_evidence["prompt_present"],
                    "responsePresent": trace_evidence["response_present"],
                    "judgeEligible": trace_evidence["judge_eligible"],
                    "notEvaluable": sum(trace_not_evaluable.values()),
                    "notEvaluableReasons": dict(sorted(trace_not_evaluable.items())),
                },
                "traceOperations": dict(sorted(trace_operations.items())),
                "traceFinishReasons": dict(sorted(trace_finish_reasons.items())),
            },
            "reliability": {
                "runOutcomes": dict(sorted(run_outcomes.items())),
                "turnOutcomes": dict(sorted(turn_outcomes.items())),
                "traceOutcomes": dict(sorted(trace_outcomes.items())),
                "toolErrors": totals["tool_errors"],
                "commandFailures": totals["command_failures"],
                "testFailures": totals["test_failures"],
                "toolOutcomesReported": totals["tool_outcomes_reported"],
                "commandOutcomesReported": totals["command_outcomes_reported"],
                "testOutcomesReported": totals["test_outcomes_reported"],
                "retries": totals["retries"],
            },
            "performance": {
                "modelCalls": trace_scope["analyzed"],
                "toolCalls": totals["tool_calls"],
                "inputTokens": (
                    trace_tokens["input_tokens"]
                    if trace_tokens["input_tokens_known"] else None
                ),
                "outputTokens": (
                    trace_tokens["output_tokens"]
                    if trace_tokens["output_tokens_known"] else None
                ),
                "averageModelLatencyMs": (
                    round(sum(trace_latency_values) / len(trace_latency_values), 2)
                    if trace_latency_values else None
                ),
                "latencyKnownCalls": len(trace_latency_values),
                "costUsd": round(sum(trace_cost_values), 8) if trace_cost_values else None,
                "agentTurnListPriceUsd": (
                    round(sum(list_price_by_source.values()), 6)
                    if any(metrics["priced_turns"] for metrics in source_metrics.values())
                    else None
                ),
                "agentTurnsPriced": sum(metrics["priced_turns"] for metrics in source_metrics.values()),
                "agentTurnsUnpriced": sum(
                    metrics["unpriced_turns"] for metrics in source_metrics.values()
                ),
                "costState": "complete" if trace_scope["analyzed"] and len(trace_cost_values) == trace_scope["analyzed"] else "partial" if trace_cost_values else "not_captured",
            },
            "behavior": {
                "findingRuns": sum(finding_counts.values()),
                "findingTypes": len({code for code, _ in finding_counts}),
                "capturedResponses": behavior["captured_responses"],
                "averageResponseCharacters": (
                    round(behavior["response_characters"] / behavior["captured_responses"], 1)
                    if behavior["captured_responses"] else None
                ),
                "refusals": behavior["refusals"],
                "apologyStarts": behavior["apology_starts"],
                "hedges": behavior["hedges"],
                "validJsonResponses": behavior["valid_json"],
            },
            "sourceActivity": source_activity,
            "modelComparisons": model_comparisons,
        }
        # A bounded scan can retain the same sampled rows while new evidence
        # changes only coverage. Its immutable result must get a new identity.
        input_hasher.update(b"bounded-scope-v2\0")
        input_hasher.update(json.dumps(
            {"scanLimit": scan_limit, "scope": result["scope"]},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
        result["_analysisInputFingerprint"] = input_hasher.hexdigest()
        result["_analysisEvidenceRevision"] = evidence_revision
        return result

    if _is_postgres(configured):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError("PostgreSQL dashboard support requires the postgres extra") from exc
        with psycopg.connect(configured, autocommit=False, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                result = builder(_PostgresSession(connection))
    else:
        path = _sqlite_path(configured)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            result = builder(_SQLiteSession(connection))
            connection.commit()
        finally:
            connection.close()
    redacted = redact_structure(result)
    if not isinstance(redacted, dict):
        raise DashboardBundleLimitError("bounded agent insights exceeded redaction budget")
    if not _include_input_fingerprint:
        redacted.pop("_analysisInputFingerprint", None)
        redacted.pop("_analysisEvidenceRevision", None)
    return redacted


# Failure counts are only meaningful when the source reported outcomes.
_OUTCOME_REPORTED_METRICS = {
    "tool_result": "tool_outcomes_reported",
    "command": "command_outcomes_reported",
    "test_result": "test_outcomes_reported",
}


def _empty_agent_insights() -> dict:
    return {
        "schema": "agent-insights-v2",
        "scope": {"availableRuns": 0, "analyzedRuns": 0, "partialRuns": 0, "complete": True,
                  "traces": {"available": 0, "analyzed": 0, "complete": True}},
        "findings": [],
        "dataHealth": {
            "counts": {"runs": 0, "turns": 0, "events": 0},
            "eventTypes": {}, "eventStatuses": {}, "promptStates": {}, "responseStates": {},
            "traceLinks": {"modelCalls": 0, "linked": 0, "unlinked": 0},
            "traceEvidence": {
                "promptPresent": 0, "responsePresent": 0, "judgeEligible": 0,
                "notEvaluable": 0, "notEvaluableReasons": {},
            },
            "traceOperations": {}, "traceFinishReasons": {},
        },
        "reliability": {
            "runOutcomes": {}, "turnOutcomes": {}, "traceOutcomes": {},
            "toolErrors": 0, "commandFailures": 0, "testFailures": 0, "retries": 0,
            "toolOutcomesReported": 0, "commandOutcomesReported": 0,
            "testOutcomesReported": 0,
        },
        "performance": {
            "modelCalls": 0, "toolCalls": 0, "inputTokens": None, "outputTokens": None,
            "averageModelLatencyMs": None, "latencyKnownCalls": 0,
            "costUsd": None, "costState": "not_captured",
            "agentTurnListPriceUsd": None, "agentTurnsPriced": 0, "agentTurnsUnpriced": 0,
        },
        "behavior": {
            "findingRuns": 0, "findingTypes": 0, "capturedResponses": 0,
            "averageResponseCharacters": None, "refusals": 0,
            "apologyStarts": 0, "hedges": 0, "validJsonResponses": 0,
        },
        "sourceActivity": [],
        "modelComparisons": [],
    }


def _build_from_connection(
    con: sqlite3.Connection,
    *,
    evaluator_id: str | None = None,
    registry_tenant: str | None = None,
    trace_offset: int = 0,
    trace_judge_status: str = "all",
    trace_id: str | None = None,
    trace_date: str | None = None,
    report_days: int = 0,
) -> dict:
    con.row_factory = sqlite3.Row
    try:
        # Keep run metadata and its signals (and the rest of the bundle) on one
        # SQLite read snapshot while a pipeline process may replace a run.
        con.execute("BEGIN")
        bundle = _redacted_bundle(
            _SQLiteSession(con),
            evaluator_id=evaluator_id,
            registry_tenant=registry_tenant,
            trace_offset=trace_offset,
            trace_judge_status=trace_judge_status,
            trace_id=trace_id,
            trace_date=trace_date,
            report_days=report_days,
        )
        if not isinstance(bundle, dict):
            raise DashboardBundleLimitError(
                "bounded dashboard bundle exceeded the redaction structure budget"
            )
        con.commit()
        return bundle
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _build_from_postgres(
    dsn: str,
    *,
    evaluator_id: str | None = None,
    registry_tenant: str | None = None,
    trace_offset: int = 0,
    trace_judge_status: str = "all",
    trace_id: str | None = None,
    trace_date: str | None = None,
    report_days: int = 0,
) -> dict:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ImportError(
            "PostgreSQL dashboard support requires "
            "`pip install \"cognifity-verdict[postgres,dashboard]\"`"
        ) from exc

    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return _redacted_bundle(
                _PostgresSession(connection),
                evaluator_id=evaluator_id,
                registry_tenant=registry_tenant,
                trace_offset=trace_offset,
                trace_judge_status=trace_judge_status,
                trace_id=trace_id,
                trace_date=trace_date,
                report_days=report_days,
            )


def _redacted_bundle(
    session: _QuerySession,
    *,
    evaluator_id: str | None,
    registry_tenant: str | None,
    trace_offset: int,
    trace_judge_status: str,
    trace_id: str | None,
    trace_date: str | None,
    report_days: int,
) -> dict:
    raw = _build(
        session,
        evaluator_id=evaluator_id,
        registry_tenant=registry_tenant,
        trace_offset=trace_offset,
        trace_judge_status=trace_judge_status,
        trace_id=trace_id,
        trace_date=trace_date,
        report_days=report_days,
    )
    bundle = redact_structure(raw)
    if not isinstance(bundle, dict):
        raise DashboardBundleLimitError(
            "bounded dashboard bundle exceeded the redaction structure budget"
        )
    # Trace IDs are authenticated navigation keys, not captured content. Keep
    # only these known structural fields exact after recursively redacting the
    # bundle; an email-shaped ID must still resolve to its persisted Trace.
    samples = bundle.get("samples")
    original_samples = raw.get("samples")
    if not isinstance(samples, list) or not isinstance(original_samples, list) or len(samples) != len(original_samples):
        raise DashboardBundleLimitError("dashboard trace sample structure changed during redaction")
    for sample, original in zip(samples, original_samples, strict=True):
        if not isinstance(sample, dict) or not isinstance(original, dict):
            raise DashboardBundleLimitError("dashboard trace sample structure changed during redaction")
        sample["trace_id"] = original["trace_id"]
    if raw.get("selectedTrace") is not None:
        selected = bundle.get("selectedTrace")
        if not isinstance(selected, dict):
            raise DashboardBundleLimitError("dashboard selected trace structure changed during redaction")
        selected["trace_id"] = raw["selectedTrace"]["trace_id"]
    return bundle


def _provider_order(keys) -> list[str]:
    ordered = [p for p in PROVIDER_ORDER if p in keys]
    ordered += sorted(k for k in keys if k not in PROVIDER_ORDER)
    return ordered


def _cluster_health(cluster_ids: list[str | None], min_sample_size: int = 30) -> dict:
    health = assess_cluster_health(cluster_ids, min_sample_size=min_sample_size)
    status = "fragmented" if health.is_fragmented else (
        "underpowered" if health.clusters_meeting_sample_floor < health.n_clusters else "ready"
    )
    if health.n_clusters == 0:
        status = "empty"
    return {
        "status": status,
        "nTraces": health.n_traces,
        "nClusters": health.n_clusters,
        "medianClusterSize": health.median_cluster_size,
        "clustersMeetingSampleFloor": health.clusters_meeting_sample_floor,
        "minSampleSize": health.min_sample_size,
        "messages": list(health.messages),
    }


def _agent_run_metadata(cur: _QuerySession, tenant: str) -> dict[str, Any]:
    return agent_evidence_queries.source_metadata(cur, tenant)


def _evidence_revision(cur: _QuerySession, tenant: str) -> int | None:
    if not cur.table_exists("evidence_revisions"):
        return None
    row = cur.execute(
        "SELECT revision FROM evidence_revisions WHERE tenant_id=?", (tenant,)
    ).fetchone()
    return int(row["revision"]) if row is not None else 0


def _empty_bundle(
    *, agent_runs: dict[str, Any] | None = None, report_days: int = 0,
    analysis_time: datetime | None = None,
    coverage: dict[str, Any] | None = None,
    evidence_revision: int | None = None,
) -> dict:
    run_metadata = agent_runs or {
        "available": 0, "sources": [], "sourcesTruncated": False,
        "agents": [], "agentsTruncated": False,
        "lastCapturedAt": None, "lastStoredAt": None,
    }
    period = _report_period(report_days, analysis_time or _now_utc())[2]
    truncation = _truncation_metadata({
        "providers": _resource_limit(0, 0, MAX_DASHBOARD_PROVIDERS),
        "providerModels": _resource_limit(0, 0, MAX_PROVIDER_MODELS),
        "clusters": _resource_limit(0, 0, MAX_DASHBOARD_CLUSTERS),
        "dimensions": _resource_limit(0, 0, MAX_DASHBOARD_DIMENSIONS),
        "evaluatorIdentities": _resource_limit(0, 0, MAX_DASHBOARD_EVALUATORS),
        "latencyPoints": _resource_limit(0, 0, MAX_SERIES_POINTS),
        "hourlyPoints": _resource_limit(0, 0, MAX_SERIES_POINTS),
        "traceSamples": _resource_limit(0, 0, MAX_TRACE_SAMPLES),
    })
    return {
        "meta": {
            "runStart": None,
            "runEnd": None,
            "durationHours": 0,
            "totalTraces": 0,
            "evidenceRevision": evidence_revision,
            "monitorEligibleCalls": 0,
            "totalAgentRuns": run_metadata["available"],
            "agentRunSources": run_metadata["sources"],
            "agentRunSourcesTruncated": run_metadata["sourcesTruncated"],
            "agentRunAgents": run_metadata["agents"],
            "agentRunAgentsTruncated": run_metadata["agentsTruncated"],
            "lastAgentCaptureAt": run_metadata["lastCapturedAt"],
            "lastAgentStoredAt": run_metadata.get("lastStoredAt"),
            "totalJudged": 0,
            "totalCost": None,
            "totalCostStatus": "unavailable",
            "costBreakdown": {
                name: _cost_summary(0, 0, 0.0)
                for name in ("agent", "judge", "unclassified")
            },
            "regressionHour": None,
            "providers": 0,
            "clusters": 0,
            "workload": None,
        },
        "providers": [],
        "clusters": [],
        "clusterHealth": _cluster_health([]),
        "evaluation": {
            "status": "empty",
            "selectedId": None,
            "availableIdentities": [],
        },
        "evaluatorHealth": [],
        "scoreCoverage": {
            "pass": 0,  # nosec B105
            "fail": 0,
            "unclear": 0,
            "missing": 0,
            "error": 0,
            "evaluable": 0,
        },
        **({"coverage": coverage} if coverage is not None else {}),
        "dimensionOverall": [],
        "tsRows": [],
        "passrate": [],
        "clusterPassrate": [],
        "haikuDim": [],
        "focusProvider": None,
        "focusProviderLabel": None,
        "samples": [],
        "selectedTrace": None,
        "providerDimension": [],
        "managementReport": {
            "schema": "management-report-v2",
            "period": period,
            "scope": {
                **_finish_management_metric(_empty_management_metric()),
                "firstCapturedAt": None,
                "latestCapturedAt": None,
                "latencySampledCalls": 0,
                "p50LatencyMs": None,
                "p95LatencyMs": None,
                "identifiedApplications": 0,
                "unattributedCalls": 0,
                "judgedCalls": 0,
                "judgeErrorCalls": 0,
            },
            "timeline": {"availableDates": 0, "shownDates": 0, "rows": []},
            "applications": {"availableRows": 0, "shownRows": 0, "rows": []},
            "models": {"availableRows": 0, "shownRows": 0, "rows": []},
        },
        "truncation": truncation,
    }


def _trace_samples(
    cur: _QuerySession,
    *,
    requested_trace_id: str | None,
    trace_judge_status: str,
    trace_offset: int,
    trace_date: str | None,
    explorer_trace_ids: list[str],
    day_explorer_trace_ids: list[str],
    judgment_status_by_trace: dict[str, str],
    ttime: dict[str, str],
    tcluster: dict[str, str | None],
    cluster_labels: dict[str, str],
    cluster_select: str,
    operation_select: str,
    ended_at_select: str,
    judg_by_trace: dict[str, dict[str, Any]],
    t0: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str]]:
    """Build one evaluator-aware, bounded Trace Explorer page."""
    filtered_trace_ids = explorer_trace_ids
    if trace_date is not None:
        chart_trace_ids = set(day_explorer_trace_ids)
        filtered_trace_ids = [
            candidate for candidate in filtered_trace_ids
            if candidate in chart_trace_ids
            and _dt(ttime[candidate]).date().isoformat() == trace_date
        ]
    if trace_judge_status != "all":
        def matches_judge_status(candidate: str) -> bool:
            status = judgment_status_by_trace.get(candidate, "not_judged")
            if trace_judge_status == "judged":
                return status in {"pass", "fail", "unclear"}
            return status == trace_judge_status

        filtered_trace_ids = [
            candidate for candidate in filtered_trace_ids
            if matches_judge_status(candidate)
        ]
    ordered_trace_ids = sorted(
        filtered_trace_ids,
        key=lambda candidate: (_dt(ttime[candidate]), candidate),
        reverse=True,
    )
    page_trace_ids = ordered_trace_ids[
        trace_offset:trace_offset + MAX_TRACE_SAMPLES
    ]
    selected_outside_page = (
        requested_trace_id in filtered_trace_ids
        and requested_trace_id not in page_trace_ids
    )
    sample_trace_ids = [
        *page_trace_ids, *([requested_trace_id] if selected_outside_page else []),
    ]
    placeholders = ",".join("?" for _ in sample_trace_ids) or "NULL"
    sample_rows = [dict(row) for row in cur.execute(
        # The select expressions are closed-set schema compatibility values.
        "SELECT trace_id, provider, request_model, "
        f"{cluster_select}, {operation_select}, "  # nosec B608
        "input_tokens, output_tokens, "
        "latency_ms, cost_usd, finish_reason, error, started_at, "
        f"{ended_at_select}, "
        f"prompt_redacted, response_redacted FROM traces WHERE trace_id IN ({placeholders})",
        tuple(sample_trace_ids),
    )]
    rows_by_trace_id = {row["trace_id"]: row for row in sample_rows}
    samples: list[dict[str, Any]] = []
    for row in (rows_by_trace_id[trace_id] for trace_id in sample_trace_ids):
        sample = dict(row)
        sample["cluster_id"] = tcluster.get(row["trace_id"])
        sample["cluster_label"] = cluster_labels.get(
            sample["cluster_id"], sample["cluster_id"]
        )
        for content_field in ("prompt_redacted", "response_redacted", "error"):
            sample[content_field] = redact_structure(sample.get(content_field))
        sample["providerKey"] = _provider_key(row["provider"])
        sample["contentCaptured"] = (
            row["prompt_redacted"] is not None or row["response_redacted"] is not None
        )
        started_at = _dt(row["started_at"])
        sample["started_at"] = started_at.isoformat()
        sample["hour"] = round((started_at - t0).total_seconds() / 3600, 2)
        latency = _round_or_none(row["latency_ms"], 0)
        sample["latency_ms"] = int(latency) if latency is not None else None
        if sample.get("response_redacted"):
            sample["response_redacted"] = sample["response_redacted"][:600]
        judgment = judg_by_trace.get(row["trace_id"])
        if judgment:
            sample["judgment"] = judgment
        sample["providerStatus"] = (
            "provider_error" if sample.get("error") is not None else "no_error_recorded"
        )
        sample["traceState"] = trace_evidence_state(
            error=row["error"], ended_at=row["ended_at"],
        )
        sample["judgeStatus"] = judgment_status_by_trace.get(
            row["trace_id"], "not_judged"
        )
        facts = deterministic_trace_facts(
            error=row["error"],
            prompt=row["prompt_redacted"],
            response=row["response_redacted"],
            ended_at=row["ended_at"],
        )
        sample["deterministicFacts"] = {
            "providerOutcome": facts["provider_outcome"],
            "promptPresent": facts["prompt_present"],
            "responsePresent": facts["response_present"],
            "judgeEligible": facts["judge_eligible"],
            "notEvaluableReason": facts["not_evaluable_reason"],
            "responseCharacters": facts["response_characters"],
            "validJson": facts["valid_json"],
            "refusalSignature": facts["refusal_signature"],
            "apologyStart": facts["apology_start"],
            "hedgePhrases": facts["hedge_phrases"],
        }
        samples.append(sample)
    selected_trace = samples.pop() if selected_outside_page else None
    return samples, selected_trace, filtered_trace_ids


def _empty_management_metric() -> dict[str, Any]:
    return {
        "calls": 0,
        "errorRecordedCalls": 0,
        "endedNoErrorRecordedCalls": 0,
        "unfinishedCalls": 0,
        "inputTokens": 0,
        "outputTokens": 0,
        "tokenKnownCalls": 0,
        "cachedInputTokens": 0,
        "uncachedInputTokens": 0,
        "inputBreakdownKnownCalls": 0,
        "costUsd": 0.0,
        "costKnownCalls": 0,
        "latencyTotalMs": 0.0,
        "latencyKnownCalls": 0,
    }


def _nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _cached_input_tokens(tags: object, input_tokens: float | None) -> int | None:
    if input_tokens is None or not isinstance(tags, dict):
        return None
    value = tags.get("verdict.cached_input_tokens")
    if not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", value) is None:
        return None
    cached = int(value)
    return cached if cached <= input_tokens else None


def _record_management_metric(
    metric: dict[str, Any], row: Mapping[str, Any], tags: object
) -> None:
    metric["calls"] += 1
    state = trace_evidence_state(error=row["error"], ended_at=row["ended_at"])
    metric[{
        "error_recorded": "errorRecordedCalls",
        "ended_no_error_recorded": "endedNoErrorRecordedCalls",
        "unfinished": "unfinishedCalls",
    }[state]] += 1
    input_tokens = _nonnegative_number(row["input_tokens"])
    output_tokens = _nonnegative_number(row["output_tokens"])
    if input_tokens is not None:
        metric["inputTokens"] += int(input_tokens)
    if output_tokens is not None:
        metric["outputTokens"] += int(output_tokens)
    if input_tokens is not None and output_tokens is not None:
        metric["tokenKnownCalls"] += 1
    cached_input_tokens = _cached_input_tokens(tags, input_tokens)
    if cached_input_tokens is not None and input_tokens is not None:
        metric["cachedInputTokens"] += cached_input_tokens
        metric["uncachedInputTokens"] += int(input_tokens) - cached_input_tokens
        metric["inputBreakdownKnownCalls"] += 1
    cost = _nonnegative_number(row["cost_usd"])
    if cost is not None:
        metric["costUsd"] += cost
        metric["costKnownCalls"] += 1
    latency = _nonnegative_number(row["latency_ms"])
    if latency is not None:
        metric["latencyTotalMs"] += latency
        metric["latencyKnownCalls"] += 1


def _finish_management_metric(metric: Mapping[str, Any]) -> dict[str, Any]:
    calls = int(metric["calls"])
    known_latency = int(metric["latencyKnownCalls"])
    return {
        "calls": calls,
        "errorRecordedCalls": int(metric["errorRecordedCalls"]),
        "endedNoErrorRecordedCalls": int(metric["endedNoErrorRecordedCalls"]),
        "unfinishedCalls": int(metric["unfinishedCalls"]),
        "inputTokens": int(metric["inputTokens"]),
        "outputTokens": int(metric["outputTokens"]),
        "totalTokens": int(metric["inputTokens"] + metric["outputTokens"]),
        "tokenKnownCalls": int(metric["tokenKnownCalls"]),
        "cachedInputTokens": int(metric["cachedInputTokens"]),
        "uncachedInputTokens": int(metric["uncachedInputTokens"]),
        "inputBreakdownKnownCalls": int(metric["inputBreakdownKnownCalls"]),
        "costUsd": round(metric["costUsd"], 9) if metric["costKnownCalls"] else None,
        "costKnownCalls": int(metric["costKnownCalls"]),
        "averageLatencyMs": round(metric["latencyTotalMs"] / known_latency, 1)
        if known_latency else None,
        "latencyKnownCalls": known_latency,
    }


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 1)


def _time_series_read_model(
    cur: _QuerySession,
    *,
    trace_scope: str,
    trace_scope_params: tuple[str, ...],
    keys: list[str],
    t0: datetime,
    judg_by_trace: dict[str, dict[str, Any]],
    ttime: dict[str, str],
    tp: dict[str, str],
    tcluster: dict[str, str | None],
    all_keys: list[str],
    clusters: list[dict[str, Any]],
    dims_present: list[str],
    raw_provider_of: dict[str, object],
    model_of: dict[str, str],
) -> dict[str, Any]:
    """Build bounded operational and evaluator time-series projections."""
    bin_seconds = 30 * 60
    bins = defaultdict(lambda: defaultdict(lambda: {"n": 0, "err": 0, "lat": []}))
    for row in cur.execute(
        "SELECT provider, started_at, latency_ms, error FROM traces "
        f"WHERE {trace_scope}",  # nosec B608 -- fixed tenant predicate
        trace_scope_params,
    ):
        provider_key = _provider_key(row["provider"])
        if provider_key not in keys:
            continue
        bucket = int((_dt(row["started_at"]) - t0).total_seconds() // bin_seconds)
        cell = bins[provider_key][bucket]
        cell["n"] += 1
        if row["error"] is not None:
            cell["err"] += 1
        latency = _round_or_none(row["latency_ms"], 6)
        if latency is not None:
            cell["lat"].append(latency)
    all_observed_bins = sorted({bucket for provider in bins for bucket in bins[provider]})
    observed_bins = all_observed_bins[-MAX_SERIES_POINTS:]
    latency_rows = []
    for bucket in observed_bins:
        projected = {"hour": round(bucket * 0.5, 1)}
        for provider in keys:
            cell = bins[provider].get(bucket)
            if cell and cell["n"]:
                projected[f"{provider}_lat"] = (
                    round(sum(cell["lat"]) / len(cell["lat"]) / 1000, 6)
                    if cell["lat"] else None
                )
                projected[f"{provider}_err"] = round(
                    100 * cell["err"] / cell["n"], 1
                )
                projected[f"{provider}_n"] = cell["n"]
            else:
                projected[f"{provider}_lat"] = None
                projected[f"{provider}_err"] = None
                projected[f"{provider}_n"] = 0
        latency_rows.append(projected)

    hour_seconds = 60 * 60
    provider_rates = defaultdict(lambda: defaultdict(Counter))
    cluster_rates = defaultdict(lambda: defaultdict(Counter))
    dimension_rates = defaultdict(lambda: defaultdict(Counter))
    focus = keys[0] if keys else None
    focus_provider_label = (
        _label_for(str(raw_provider_of.get(focus) or ""), model_of.get(focus, ""))
        if focus is not None else None
    )
    for trace_id, judgment in judg_by_trace.items():
        started_at = ttime.get(trace_id)
        if not started_at:
            continue
        provider = tp.get(trace_id)
        cluster = tcluster.get(trace_id)
        hour = int((_dt(started_at) - t0).total_seconds() // hour_seconds)
        for dimension in judgment["dims"]:
            verdict = dimension["verdict"]
            if provider in keys:
                provider_rates[provider][hour][verdict] += 1
            if cluster and len(all_keys) == 1:
                cluster_rates[cluster][hour][verdict] += 1
            if provider == focus:
                dimension_rates[dimension["name"]][hour][verdict] += 1
    all_observed_hours = sorted({hour for provider in provider_rates for hour in provider_rates[provider]})
    observed_hours = all_observed_hours[-MAX_SERIES_POINTS:]
    passrate = []
    for hour in observed_hours:
        projected = {"hour": hour}
        for provider in keys:
            cell = provider_rates[provider].get(hour)
            projected[provider] = _score_rate(cell) if cell else None
        passrate.append(projected)
    cluster_passrate = []
    if len(all_keys) == 1:
        cluster_keys = [cluster["cluster_id"] for cluster in clusters]
        for hour in observed_hours:
            projected = {"hour": hour}
            for cluster in cluster_keys:
                cell = cluster_rates[cluster].get(hour)
                projected[cluster] = _score_rate(cell) if cell else None
            cluster_passrate.append(projected)
    dimension_passrate = []
    for hour in observed_hours:
        projected = {"hour": hour}
        for dimension in dims_present:
            cell = dimension_rates[dimension].get(hour)
            projected[dimension] = _score_rate(cell) if cell else None
        dimension_passrate.append(projected)
    return {
        "latencyRows": latency_rows,
        "passrate": passrate,
        "clusterPassrate": cluster_passrate,
        "dimensionPassrate": dimension_passrate,
        "focusProvider": focus,
        "focusProviderLabel": focus_provider_label,
        "availableLatencyPoints": len(all_observed_bins),
        "shownLatencyPoints": len(observed_bins),
        "availableHourlyPoints": len(all_observed_hours),
        "shownHourlyPoints": len(observed_hours),
    }


def _analysis_coverage(
    cur: _QuerySession,
    *,
    tenant: str,
    evidence_revision: int | None,
    total_traces: int,
    agent_runs: dict[str, Any],
    explorer_trace_ids: list[str],
    judgment_status_by_trace: dict[str, str],
    selected_evaluator_id: str | None,
) -> dict[str, Any]:
    """Project persisted deterministic and selected-evaluator coverage."""
    deterministic = {
        "status": "never_run",
        "analysisId": None,
        "completedAt": None,
        "availableRuns": agent_runs["available"],
        "analyzedRuns": 0,
        "availableTraces": len(explorer_trace_ids),
        "analyzedTraces": 0,
        "complete": False,
        "needsRefresh": False,
    }
    if _table_exists(cur, "deterministic_analysis_runs"):
        revision_select = (
            "r.verified_revision" if "verified_revision" in cur.columns(
                "deterministic_analysis_runs"
            ) else "NULL AS verified_revision"
        )
        row = None
        if _table_exists(cur, "deterministic_analysis_current"):
            row = cur.execute(
                f"""SELECT r.payload_json, {revision_select} FROM deterministic_analysis_current c
                   JOIN deterministic_analysis_runs r ON r.analysis_id=c.analysis_id
                   WHERE c.tenant_id=? AND c.scope_key=? AND c.analyzer_version=?""",  # nosec B608 -- fixed projection from schema introspection
                (tenant, SCOPE_KEY, ANALYZER_VERSION),
            ).fetchone()
        if row is None:
            # Pre-pointer records retain their original completion-time order.
            row = cur.execute(
                f"""SELECT r.payload_json, {revision_select} FROM deterministic_analysis_runs r
                   WHERE tenant_id=? AND scope_key=? AND analyzer_version=?
                   ORDER BY completed_at DESC, analysis_id DESC LIMIT 1""",  # nosec B608 -- fixed projection from schema introspection
                (tenant, SCOPE_KEY, ANALYZER_VERSION),
            ).fetchone()
        if row is not None:
            payload = row["payload_json"]
            if isinstance(payload, dict):
                payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            persisted = analysis_run_from_json(payload)
            scope = persisted.result.get("scope", {})
            trace_scope = scope.get("traces", {}) if isinstance(scope, dict) else {}
            deterministic = {
                "status": persisted.status.value,
                "analysisId": persisted.analysis_id,
                "completedAt": persisted.completed_at.isoformat(),
                "availableRuns": scope.get("availableRuns", agent_runs["available"]),
                "analyzedRuns": scope.get("analyzedRuns", 0),
                "availableTraces": trace_scope.get(
                    "available", len(explorer_trace_ids)
                ),
                "analyzedTraces": trace_scope.get("analyzed", 0),
                "complete": bool(
                    scope.get("complete") and trace_scope.get("complete")
                ),
                "needsRefresh": persisted.status.value == "completed" and (
                    row["verified_revision"] is None
                    or row["verified_revision"] != evidence_revision
                    or scope.get("availableRuns") != agent_runs["available"]
                    or trace_scope.get("available") != total_traces
                ),
            }
    application_trace_ids = set(explorer_trace_ids)
    judged_trace_ids = {
        trace_id
        for trace_id, status in judgment_status_by_trace.items()
        if trace_id in application_trace_ids and status != "judge_error"
    }
    judge_error_trace_ids = {
        trace_id
        for trace_id, status in judgment_status_by_trace.items()
        if trace_id in application_trace_ids and status == "judge_error"
    }
    return {
        "deterministicAnalysis": deterministic,
        "evaluation": {
            "selectedEvaluator": selected_evaluator_id,
            "traces": len(application_trace_ids),
            "judged": len(judged_trace_ids),
            "judgeErrors": len(judge_error_trace_ids),
            "notJudged": len(
                application_trace_ids - judged_trace_ids - judge_error_trace_ids
            ),
            "completedCalls": len(judged_trace_ids),
            "errorCalls": len(judge_error_trace_ids),
        },
    }


def _build(
    cur,
    *,
    evaluator_id: str | None = None,
    registry_tenant: str | None = None,
    trace_offset: int = 0,
    trace_judge_status: str = "all",
    trace_id: str | None = None,
    trace_date: str | None = None,
    report_days: int = 0,
) -> dict:
    requested_trace_id = trace_id
    agent_runs = _agent_run_metadata(cur, registry_tenant or "__verdict_local__")
    evidence_revision = _evidence_revision(cur, registry_tenant or "__verdict_local__")
    analysis_time = _now_utc()
    report_start, report_end, report_period = _report_period(report_days, analysis_time)
    if not _table_exists(cur, "traces"):
        coverage = _analysis_coverage(cur, tenant=registry_tenant or "__verdict_local__",
                                      evidence_revision=evidence_revision, total_traces=0,
                                      agent_runs=agent_runs, explorer_trace_ids=[],
                                      judgment_status_by_trace={}, selected_evaluator_id=None)
        return _empty_bundle(
            agent_runs=agent_runs, report_days=report_days, analysis_time=analysis_time,
            coverage=coverage, evidence_revision=evidence_revision,
        )
    trace_columns = cur.columns("traces")
    trace_scope, trace_scope_params = _trace_tenant_scope(
        registry_tenant, trace_columns
    )
    t0row = cur.execute(
        "SELECT MIN(started_at) m, MAX(started_at) x FROM traces "
        f"WHERE {trace_scope}",  # nosec B608 -- fixed tenant predicate
        trace_scope_params,
    ).fetchone()
    if not t0row or not t0row["m"]:
        coverage = _analysis_coverage(cur, tenant=registry_tenant or "__verdict_local__",
                                      evidence_revision=evidence_revision, total_traces=0,
                                      agent_runs=agent_runs, explorer_trace_ids=[],
                                      judgment_status_by_trace={}, selected_evaluator_id=None)
        return _empty_bundle(
            agent_runs=agent_runs, report_days=report_days, analysis_time=analysis_time,
            coverage=coverage, evidence_revision=evidence_revision,
        )
    t0, tmax = _dt(t0row["m"]), _dt(t0row["x"])
    cluster_select = (
        "cluster_id" if "cluster_id" in trace_columns else "NULL AS cluster_id"
    )
    operation_select = (
        "operation" if "operation" in trace_columns else "NULL AS operation"
    )

    tags_column = "tags_json" if "tags_json" in trace_columns else (
        "tags" if "tags" in trace_columns else "NULL"
    )
    service_column = (
        "service_name" if "service_name" in trace_columns else "'' AS service_name"
    )
    response_model_column = (
        "response_model" if "response_model" in trace_columns else "'' AS response_model"
    )
    ended_at_column = "ended_at" if "ended_at" in trace_columns else "NULL AS ended_at"
    tenant_id_column = "tenant_id" if "tenant_id" in trace_columns else "NULL AS tenant_id"
    environment_column = (
        "environment" if "environment" in trace_columns else "'' AS environment"
    )
    management_scope = _empty_management_metric()
    management_days: dict[str, dict[str, Any]] = {}
    management_applications: dict[tuple[str | None, str], dict[str, Any]] = {}
    management_models: dict[tuple[str, str], dict[str, Any]] = {}
    management_services: set[str] = set()
    management_unattributed = 0
    management_first: datetime | None = None
    management_latest: datetime | None = None
    management_latencies: list[tuple[datetime, str, float]] = []
    management_trace_ids: list[str] = []
    cost_counts = {
        name: {"traces": 0, "priced": 0, "cost": 0.0}
        for name in ("agent", "judge", "unclassified")
    }
    total_cost = 0.0
    priced_traces = 0
    monitor_eligible_calls = 0

    display_workloads: set[str] = set()
    has_undisplayable_workload = False
    explorer_trace_ids: list[str] = []
    day_explorer_trace_ids: list[str] = []

    # trace_id -> (provider, started_at) and provider model
    tp, ttime, tcluster, model_of, models_of, raw_provider_of = (
        {}, {}, {}, {}, defaultdict(set), {}
    )
    provider_trace_counts = Counter()
    for r in cur.execute(
        # ``cluster_select`` is selected from the two literals above; it never
        # contains request data or a database value.
        "SELECT trace_id, provider, request_model, "
        f"{response_model_column}, started_at, {ended_at_column}, {tenant_id_column}, "  # nosec B608
        "input_tokens, output_tokens, error, latency_ms, cost_usd, "
        f"{cluster_select}, {tags_column} AS workload_tags, "  # nosec B608
        f"{service_column}, {environment_column} FROM traces "  # nosec B608
        f"WHERE {trace_scope}",  # nosec B608 -- fixed tenant predicate
        trace_scope_params,
    ):
        provider_key = _provider_key(r["provider"])
        tp[r["trace_id"]] = provider_key
        ttime[r["trace_id"]] = r["started_at"]
        tcluster[r["trace_id"]] = r["cluster_id"]
        models_of[provider_key].add(r["request_model"])
        raw_provider_of.setdefault(provider_key, r["provider"])
        provider_trace_counts[provider_key] += 1
        tags = _json_value(r["workload_tags"], {})
        workload_present = (
            isinstance(tags, dict) and "verdict.workload" in tags
        )
        workload = tags.get("verdict.workload") if isinstance(tags, dict) else None
        if (r["ended_at"] is not None and workload not in {"judge", "paired_replay"}
                and not (registry_tenant == LOCAL_TENANT and r["tenant_id"] == "")):
            monitor_eligible_calls += 1
        group = workload if workload in {"agent", "judge"} else "unclassified"
        if workload != "judge":
            explorer_trace_ids.append(r["trace_id"])
        if workload not in {"judge", "paired_replay"}:
            day_explorer_trace_ids.append(r["trace_id"])
        if workload not in {"judge", "paired_replay"} and (
            report_start is None or report_start <= _dt(r["started_at"]) < report_end
        ):
            management_trace_ids.append(r["trace_id"])
            _record_management_metric(management_scope, r, tags)
            started_at = _dt(r["started_at"])
            management_first = min(management_first, started_at) if management_first else started_at
            management_latest = max(management_latest, started_at) if management_latest else started_at
            service = r["service_name"] if isinstance(r["service_name"], str) else ""
            service = service.strip()
            if service == "unknown-service":
                service = ""
            if service:
                management_services.add(service)
            else:
                management_unattributed += 1
            provider = str(r["provider"] or "").strip() or "Unknown provider"
            model = str(r["response_model"] or r["request_model"] or "").strip()
            model = model or "Unknown model"
            environment = (
                r["environment"].strip()
                if isinstance(r["environment"], str) and r["environment"].strip()
                else "Unspecified"
            )
            application = management_applications.setdefault(
                (service or None, environment), _empty_management_metric()
            )
            _record_management_metric(application, r, tags)
            model_metric = management_models.setdefault(
                (provider, model), _empty_management_metric()
            )
            _record_management_metric(model_metric, r, tags)
            date = started_at.date().isoformat()
            day = management_days.setdefault(
                date, {"date": date, "calls": 0, "totalTokens": 0,
                       "tokenKnownCalls": 0}
            )
            day["calls"] += 1
            input_tokens = _nonnegative_number(r["input_tokens"])
            output_tokens = _nonnegative_number(r["output_tokens"])
            if input_tokens is not None and output_tokens is not None:
                day["totalTokens"] += int(input_tokens) + int(output_tokens)
                day["tokenKnownCalls"] += 1
            latency = _nonnegative_number(r["latency_ms"])
            if latency is not None:
                latency_entry = (started_at, str(r["trace_id"]), latency)
                if len(management_latencies) < MAX_MANAGEMENT_LATENCY_SAMPLES:
                    heapq.heappush(management_latencies, latency_entry)
                elif latency_entry[:2] > management_latencies[0][:2]:
                    heapq.heapreplace(management_latencies, latency_entry)
        displayable_workload = (
            isinstance(workload, str)
            and workload != "judge"
            and _DISPLAY_WORKLOAD.fullmatch(workload) is not None
            and redact(workload) == workload
        )
        if displayable_workload:
            display_workloads.add(workload)
        elif workload_present and workload != "judge":
            has_undisplayable_workload = True
        cost_counts[group]["traces"] += 1
        cost = _round_or_none(r["cost_usd"], 9)
        if cost is not None:
            cost_counts[group]["priced"] += 1
            cost_counts[group]["cost"] += cost
            priced_traces += 1
            total_cost += cost
    for provider_key, models in models_of.items():
        known_models = sorted(str(model) for model in models if model not in (None, ""))
        model_of[provider_key] = (
            known_models[0]
            if len(known_models) == 1
            else f"multiple models ({len(known_models)})"
            if known_models
            else ""
        )

    cluster_labels: dict[str, str] = {}
    if registry_tenant is not None:
        projection = active_cluster_projection(cur, registry_tenant)
        if projection is not None:
            assignments, cluster_labels = projection
            tcluster = {
                trace_id: assignments.get(trace_id)
                for trace_id in tcluster
            }

    # Group persisted judgments by evaluator identity before calculating any
    # score. Multiple identities require an explicit API/UI selection.
    identity_rows: list[tuple[dict, Mapping[str, Any]]] = []
    identity_by_id: dict[str, dict] = {}
    judgment_scope, judgment_scope_params = _trace_tenant_scope(
        registry_tenant,
        trace_columns,
        alias="t",
    )
    judgment_source = (
        "current_judgments" if _table_exists(cur, "current_judgments") else "judgments"
    )
    judgment_rows = (
        cur.execute(
            f"SELECT j.* FROM {judgment_source} AS j "
            "JOIN traces AS t ON t.trace_id=j.trace_id "
            f"WHERE {judgment_scope}",  # nosec B608 -- fixed tenant predicate
            judgment_scope_params,
        )
        if _table_exists(cur, "judgments")
        else ()
    )
    for row in judgment_rows:
        if row["trace_id"] not in ttime:
            continue
        identity = evaluator_identity(row)
        identity_by_id.setdefault(identity["id"], identity)
        identity_rows.append((identity, row))
    all_available_identities = sorted(
        identity_by_id.values(), key=lambda identity: (identity["label"], identity["id"])
    )

    selected_id = evaluator_id
    if selected_id is not None and selected_id not in identity_by_id:
        evaluation_status = "invalid_selection"
        selected_id = None
    elif selected_id is not None:
        evaluation_status = "selected"
    elif len(all_available_identities) == 1:
        selected_id = all_available_identities[0]["id"]
        evaluation_status = "selected"
    elif all_available_identities:
        evaluation_status = "selection_required"
    else:
        evaluation_status = "empty"
    available_identities = all_available_identities[:MAX_DASHBOARD_EVALUATORS]
    selected_identity = identity_by_id.get(selected_id)
    if (
        selected_identity is not None
        and selected_identity not in available_identities
        and available_identities
    ):
        available_identities[-1] = selected_identity
        available_identities.sort(
            key=lambda identity: (identity["label"], identity["id"])
        )
    evaluation = {
        "status": evaluation_status,
        "selectedId": selected_id,
        # Do not expose aliased mutable containers in two response locations.
        # Besides being surprising to clients, shared graphs are rejected by
        # the redaction boundary to prevent exponential serialization.
        "selectedIdentity": deepcopy(identity_by_id.get(selected_id)),
        "availableIdentities": available_identities,
    }

    # Fixed human-labeled sentinel agreement is independent judge-health
    # evidence. Historical databases may predate this table, so absence means
    # "not monitored", never zero agreement.
    evaluator_health = []
    selected_identity = identity_by_id.get(selected_id)
    selected_fingerprint = (
        selected_identity.get("fingerprint") if selected_identity else None
    )
    has_health_table = _table_exists(cur, "evaluator_health")
    if selected_fingerprint and has_health_table:
        health_columns = cur.columns("evaluator_health")
    else:
        health_columns = set()
    if selected_fingerprint and "tenant_id" in health_columns:
        for row in cur.execute(
            """SELECT * FROM evaluator_health
                 WHERE evaluator_fingerprint = ? AND tenant_id = ?
                 ORDER BY evaluated_at DESC LIMIT 30""",
            (selected_fingerprint, registry_tenant or LOCAL_TENANT),
        ):
            method_version = (
                row["method_version"]
                if "method_version" in health_columns else "1"
            ) or "1"
            legacy = method_version == "1"
            evaluator_health.append({
                "id": row["health_id"],
                "evaluatedAt": row["evaluated_at"],
                "sentinelSetName": row["sentinel_set_name"] or "",
                "sentinelSetFingerprint": row["sentinel_set_fingerprint"],
                "correctExamples": (
                    None if legacy else row["correct_examples"]
                ),
                "totalExamples": None if legacy else row["total_examples"],
                "exampleAgreement": (
                    None if legacy or row["example_agreement"] is None
                    else round(100 * row["example_agreement"], 1)
                ),
                "exampleConfidenceLow": (
                    None if legacy or row["example_confidence_low"] is None
                    else round(100 * row["example_confidence_low"], 1)
                ),
                "exampleConfidenceHigh": (
                    None if legacy or row["example_confidence_high"] is None
                    else round(100 * row["example_confidence_high"], 1)
                ),
                "correctLabels": row["correct_labels"],
                "totalLabels": row["total_labels"],
                "labelAgreement": (
                    round(
                        100 * (
                            row["agreement"] if legacy
                            else row["label_agreement"]
                        ),
                        1,
                    )
                    if (
                        (legacy and "agreement" in health_columns and row["agreement"] is not None)
                        or (
                            not legacy
                            and row["label_agreement"] is not None
                        )
                    ) else None
                ),
                "status": "insufficient_data" if legacy else row["status"],
                "errorCount": row["error_count"],
                "methodVersion": method_version,
            })

    # A trace can have retries or historical duplicate rows for one evaluator.
    # Latest (created_at, judgment_id) wins, independent of insertion/scan order.
    latest_row_by_trace: dict[str, Mapping[str, Any]] = {}
    if selected_id is not None:
        for identity, row in identity_rows:
            if identity["id"] != selected_id:
                continue
            previous = latest_row_by_trace.get(row["trace_id"])
            row_key = (row["created_at"] or "", row["judgment_id"] or "")
            if previous is None:
                latest_row_by_trace[row["trace_id"]] = row
                continue
            previous_key = (
                previous["created_at"] or "",
                previous["judgment_id"] or "",
            )
            if row_key > previous_key:
                latest_row_by_trace[row["trace_id"]] = row

    # Selected judgments -> provider/dimension tallies and per-trace detail.
    dim_overall = defaultdict(Counter)
    prov_dim = defaultdict(lambda: defaultdict(Counter))
    score_coverage = Counter({
        "pass": 0, "fail": 0, "unclear": 0,  # nosec B105
        "missing": 0, "error": 0, "evaluable": 0,
    })
    judg_by_trace = {}
    judgment_status_by_trace: dict[str, str] = {}
    for trace_id, row in latest_row_by_trace.items():
        status = (_row_value(row, "status", "completed") or "completed").lower()
        if status == "error":
            score_coverage["error"] += 1
            judgment_status_by_trace[trace_id] = "judge_error"
            continue
        dims_raw = _json_column(row, "dimensions", [])
        dims_raw = dims_raw if isinstance(dims_raw, list) else []
        expected = _json_column(row, "expected_dimensions", [])
        if not isinstance(expected, list) or not expected:
            expected = [d.get("name") for d in dims_raw if isinstance(d, dict) and d.get("name")]
        normalized_dims = []
        present_names = set()
        nameless_unclear = 0
        prov = tp.get(trace_id, "__provider_unknown__")
        for dimension in dims_raw:
            if not isinstance(dimension, dict) or not dimension.get("name"):
                score_coverage["unclear"] += 1
                nameless_unclear += 1
                continue
            name = str(dimension["name"])
            verdict = verdict_label(dimension.get("verdict", "unclear")).lower()
            present_names.add(name)
            normalized_dims.append({
                "name": name,
                "verdict": verdict,
                "reasoning": redact_structure(str(dimension.get("reasoning", ""))) or "",
            })
            dim_overall[name][verdict] += 1
            prov_dim[prov][name][verdict] += 1
            score_coverage[verdict] += 1
        score_coverage["missing"] += len(set(expected) - present_names)
        trace_counts = Counter(dimension["verdict"] for dimension in normalized_dims)
        trace_status = (
            "fail" if trace_counts["fail"]
            else "unclear" if trace_counts["unclear"] or nameless_unclear or set(expected) - present_names
            else "pass" if trace_counts["pass"]
            else "unavailable"
        )
        judg_by_trace[trace_id] = {
            "judges": _json_column(row, "judge_models", []),
            "dims": normalized_dims,
            "summary": {
                "status": trace_status,
                "pass": trace_counts["pass"],
                "fail": trace_counts["fail"],
                "unclear": trace_counts["unclear"] + nameless_unclear,
                "missing": len(set(expected) - present_names),
                "passRate": _score_rate(trace_counts),
            },
        }
        judgment_status_by_trace[trace_id] = trace_status
    score_coverage["evaluable"] = score_coverage["pass"] + score_coverage["fail"]

    all_keys = _provider_order(set(raw_provider_of))
    provider_rank = {provider: index for index, provider in enumerate(all_keys)}
    ranked_provider_keys = sorted(
        all_keys,
        key=lambda provider: (
            -provider_trace_counts[provider],
            provider_rank[provider],
        ),
    )
    keys = _provider_order(ranked_provider_keys[:MAX_DASHBOARD_PROVIDERS])
    # ---- providers ----
    providers = []
    for p in keys:
        raw_provider = raw_provider_of[p]
        provider_where = "provider IS NULL" if raw_provider is None else "provider = ?"
        provider_params = () if raw_provider is None else (raw_provider,)
        r = cur.execute(
            # ``provider_where`` is selected from two closed literals above.
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN error IS NOT NULL AND error<>'' THEN 1 ELSE 0 END) errors,
                      AVG(latency_ms) lat, SUM(input_tokens) it, SUM(output_tokens) ot,
                      SUM(cost_usd) cost,
                      SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) cost_unknown
               FROM traces WHERE """ + trace_scope + " AND " + provider_where,  # nosec B608
            (*trace_scope_params, *provider_params),
        ).fetchone()
        provider_counts = Counter()
        for c in prov_dim[p].values():
            for v, cnt in c.items():
                provider_counts[v] += cnt
        judged = provider_counts["pass"] + provider_counts["fail"]
        trace_count = int(r["n"] or 0)
        error_count = int(r["errors"] or 0)
        providers.append({
            "key": p, "rawProvider": raw_provider,
            "label": _label_for(str(raw_provider or ""), model_of.get(p, "")),
            "model": model_of.get(p, ""),
            "models": sorted(
                str(model) for model in models_of[p] if model not in (None, "")
            )[:MAX_PROVIDER_MODELS],
            "n": trace_count, "errors": error_count,
            "errorRate": round(100 * error_count / trace_count, 1) if trace_count else 0.0,
            "avgLatency": _round_or_none(float(r["lat"] or 0) / 1000, 6) or 0.0,
            "inTok": int(r["it"] or 0), "outTok": int(r["ot"] or 0),
            "cost": _round_or_none(r["cost"], 4),
            "costUnknown": int(r["cost_unknown"] or 0),
            "passRate": _score_rate(provider_counts), "judged": judged,
            "unclear": provider_counts["unclear"],
        })

    # ---- clusters ----
    cluster_counts = Counter(
        cluster_id
        for cluster_id in tcluster.values()
        if cluster_id and cluster_id != UNCLUSTERED_ID
    )
    clusters = [
        {
            "cluster_id": cluster_id,
            "display_name": cluster_labels.get(cluster_id, cluster_id),
            "n": count,
        }
        for cluster_id, count in sorted(
            cluster_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:MAX_DASHBOARD_CLUSTERS]
    ]
    cluster_ids = list(tcluster.values())
    cluster_health = _cluster_health(cluster_ids)

    # ---- dimension overall ----
    all_dims_present = [d for d in DIM_ORDER if d in dim_overall] + \
                       [d for d in dim_overall if d not in DIM_ORDER]
    dims_present = all_dims_present[:MAX_DASHBOARD_DIMENSIONS]
    shown_dimensions = set(dims_present)
    for judgment in judg_by_trace.values():
        judgment["dims"] = [
            dimension
            for dimension in judgment["dims"]
            if dimension["name"] in shown_dimensions
        ]
    dimensionOverall = []
    for d in dims_present:
        c = dim_overall[d]
        tot = sum(c.values())
        dimensionOverall.append({"dim": d, "passRate": _score_rate(c),
                                 "pass": c.get("pass", 0), "fail": c.get("fail", 0),
                                 "unclear": c.get("unclear", 0), "tot": tot})

    # ---- provider x dimension ----
    providerDimension = []
    for d in dims_present:
        row = {"dim": d}
        for p in keys:
            c = prov_dim[p].get(d, {})
            row[p] = _score_rate(c)
        providerDimension.append(row)

    series = _time_series_read_model(
        cur,
        trace_scope=trace_scope,
        trace_scope_params=trace_scope_params,
        keys=keys,
        t0=t0,
        judg_by_trace=judg_by_trace,
        ttime=ttime,
        tp=tp,
        tcluster=tcluster,
        all_keys=all_keys,
        clusters=clusters,
        dims_present=dims_present,
        raw_provider_of=raw_provider_of,
        model_of=model_of,
    )

    samples, selected_trace, filtered_explorer_trace_ids = _trace_samples(
        cur,
        requested_trace_id=requested_trace_id,
        trace_judge_status=trace_judge_status,
        trace_offset=trace_offset,
        trace_date=trace_date,
        explorer_trace_ids=explorer_trace_ids,
        day_explorer_trace_ids=day_explorer_trace_ids,
        judgment_status_by_trace=judgment_status_by_trace,
        ttime=ttime,
        tcluster=tcluster,
        cluster_labels=cluster_labels,
        cluster_select=cluster_select,
        operation_select=operation_select,
        ended_at_select=ended_at_column,
        judg_by_trace=judg_by_trace,
        t0=t0,
    )

    total_traces = sum(int(values["traces"]) for values in cost_counts.values())
    total_cost_status = (
        "unavailable" if priced_traces == 0
        else "complete" if priced_traces == total_traces
        else "partial"
    )
    provider_model_counts = [
        len({model for model in models_of[provider] if model not in (None, "")})
        for provider in keys
    ]
    available_provider_models = max(provider_model_counts, default=0)
    shown_provider_models = min(available_provider_models, MAX_PROVIDER_MODELS)
    truncation = _truncation_metadata({
        "providers": _resource_limit(
            len(all_keys), len(keys), MAX_DASHBOARD_PROVIDERS
        ),
        "providerModels": _resource_limit(
            available_provider_models,
            shown_provider_models,
            MAX_PROVIDER_MODELS,
        ),
        "clusters": _resource_limit(
            cluster_health["nClusters"],
            len(clusters),
            MAX_DASHBOARD_CLUSTERS,
        ),
        "dimensions": _resource_limit(
            len(all_dims_present),
            len(dims_present),
            MAX_DASHBOARD_DIMENSIONS,
        ),
        "evaluatorIdentities": _resource_limit(
            len(all_available_identities),
            len(available_identities),
            MAX_DASHBOARD_EVALUATORS,
        ),
        "latencyPoints": _resource_limit(
            series["availableLatencyPoints"],
            series["shownLatencyPoints"],
            MAX_SERIES_POINTS,
        ),
        "hourlyPoints": _resource_limit(
            series["availableHourlyPoints"],
            series["shownHourlyPoints"],
            MAX_SERIES_POINTS,
        ),
        "traceSamples": _resource_limit(
            len(filtered_explorer_trace_ids),
            len(samples),
            MAX_TRACE_SAMPLES,
        ),
    })
    coverage = _analysis_coverage(
        cur,
        tenant=registry_tenant or "__verdict_local__",
        evidence_revision=evidence_revision,
        total_traces=total_traces,
        agent_runs=agent_runs,
        explorer_trace_ids=explorer_trace_ids,
        judgment_status_by_trace=judgment_status_by_trace,
        selected_evaluator_id=selected_id,
    )
    application_rows = [
        {
            "name": service or "Unattributed",
            "environment": environment,
            "attributed": service is not None,
            **_finish_management_metric(metric),
        }
        for (service, environment), metric in management_applications.items()
    ]
    application_rows.sort(key=lambda row: (
        -row["calls"], row["name"].casefold(), row["environment"].casefold()
    ))
    model_rows = [
        {
            "provider": provider,
            "model": model,
            **_finish_management_metric(metric),
        }
        for (provider, model), metric in management_models.items()
    ]
    model_rows.sort(key=lambda row: (
        -row["calls"], row["provider"].casefold(), row["model"].casefold()
    ))
    all_dates = sorted(management_days)
    shown_dates = all_dates[-MAX_MANAGEMENT_TIMELINE_DATES:]
    scope = _finish_management_metric(management_scope)
    latency_values = [entry[2] for entry in management_latencies]
    scope.update({
        "firstCapturedAt": management_first.isoformat() if management_first else None,
        "latestCapturedAt": management_latest.isoformat() if management_latest else None,
        "latencySampledCalls": len(latency_values),
        "p50LatencyMs": _nearest_rank(latency_values, 0.50),
        "p95LatencyMs": _nearest_rank(latency_values, 0.95),
        "identifiedApplications": len(management_services),
        "unattributedCalls": management_unattributed,
        "judgedCalls": sum(
            judgment_status_by_trace.get(trace_id) not in {None, "judge_error"}
            for trace_id in management_trace_ids
        ),
        "judgeErrorCalls": sum(
            judgment_status_by_trace.get(trace_id) == "judge_error"
            for trace_id in management_trace_ids
        ),
    })
    if report_days == 0:
        report_period.update({
            "startDate": management_first.date().isoformat() if management_first else None,
            "endDate": management_latest.date().isoformat() if management_latest else None,
        })
    management_report = {
        "schema": "management-report-v2",
        "period": report_period,
        "scope": scope,
        "timeline": {
            "availableDates": len(all_dates),
            "shownDates": len(shown_dates),
            "rows": [management_days[date] for date in shown_dates],
        },
        "applications": {
            "availableRows": len(application_rows),
            "shownRows": min(len(application_rows), MAX_MANAGEMENT_REPORT_ROWS),
            "rows": application_rows[:MAX_MANAGEMENT_REPORT_ROWS],
        },
        "models": {
            "availableRows": len(model_rows),
            "shownRows": min(len(model_rows), MAX_MANAGEMENT_REPORT_ROWS),
            "rows": model_rows[:MAX_MANAGEMENT_REPORT_ROWS],
        },
    }
    return {
        "meta": {
            "runStart": t0.isoformat(),
            "runEnd": tmax.isoformat(),
            "durationHours": max(1, round((tmax - t0).total_seconds() / 3600)),
            "totalTraces": total_traces,
            "evidenceRevision": evidence_revision,
            "monitorEligibleCalls": monitor_eligible_calls,
            "totalAgentRuns": agent_runs["available"],
            "agentRunSources": agent_runs["sources"],
            "agentRunSourcesTruncated": agent_runs["sourcesTruncated"],
            "agentRunAgents": agent_runs["agents"],
            "agentRunAgentsTruncated": agent_runs["agentsTruncated"],
            "lastAgentCaptureAt": agent_runs["lastCapturedAt"],
            "lastAgentStoredAt": agent_runs.get("lastStoredAt"),
            "totalJudged": len(judg_by_trace),
            "totalCost": total_cost if priced_traces else None,
            "totalCostStatus": total_cost_status,
            "costBreakdown": {
                name: _cost_summary(
                    int(values["traces"]),
                    int(values["priced"]),
                    float(values["cost"]),
                )
                for name, values in cost_counts.items()
            },
            "regressionHour": None,
            "providers": len(all_keys),
            "clusters": cluster_health["nClusters"],
            "workload": (
                next(iter(display_workloads))
                if len(display_workloads) == 1 and not has_undisplayable_workload
                else None
            ),
        },
        "providers": providers,
        "clusters": clusters,
        "clusterHealth": cluster_health,
        "evaluation": evaluation,
        "evaluatorHealth": evaluator_health,
        "scoreCoverage": dict(score_coverage),
        "coverage": coverage,
        "dimensionOverall": dimensionOverall,
        "tsRows": series["latencyRows"],
        "passrate": series["passrate"],
        "clusterPassrate": series["clusterPassrate"],
        "haikuDim": series["dimensionPassrate"],
        "focusProvider": series["focusProvider"],
        "focusProviderLabel": series["focusProviderLabel"],
        "samples": samples,
        "selectedTrace": selected_trace,
        "providerDimension": providerDimension,
        "managementReport": management_report,
        "truncation": truncation,
    }


# --------------------------------------------------------------------------- #
#  FastAPI app
# --------------------------------------------------------------------------- #
def create_app(
    *,
    storage: str | os.PathLike[str] | None = None,
    operations_url: str | None = None,
    operations_page_url: str | None = None,
    allowed_hosts: list[str] | None = None,
    tenant_id: str = LOCAL_TENANT,
    synthetic_demo: bool = False,
):
    import base64
    import secrets

    from fastapi import FastAPI, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    configured_storage = resolve_storage(storage)
    if safe_tenant_id(tenant_id) is None:
        raise ValueError(
            "tenant_id must be a non-sensitive bounded routing identifier "
            "of at most 128 ASCII characters"
        )
    configured_tenant = tenant_id
    if operations_url is not None:
        parsed_operations = urlsplit(operations_url)
        if (
            not operations_url.startswith("/")
            or operations_url.startswith("//")
            or parsed_operations.scheme
            or parsed_operations.netloc
            or parsed_operations.query
            or parsed_operations.fragment
        ):
            raise ValueError("operations_url must be a same-origin absolute path")
    if operations_page_url is not None and operations_page_url != "/operations":
        raise ValueError("operations_page_url must be /operations")
    backend = "postgresql" if _is_postgres(configured_storage) else "sqlite"
    setup_token = secrets.token_urlsafe(32)
    app = FastAPI(title="Verdict Dashboard", version="0.1.0")
    app.state.verdict_tenant_id = configured_tenant
    configured_hosts = allowed_hosts or [
        host.strip()
        for host in os.environ.get(
            "VERDICT_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1],testserver"
        ).split(",")
        if host.strip()
    ]
    if not configured_hosts:
        raise ValueError("at least one trusted dashboard host is required")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=configured_hosts)
    app.mount(
        "/assets",
        StaticFiles(directory=STATIC / "assets", check_dir=False),
        name="assets",
    )
    # CORS is locked down by default (same-origin only). The dashboard is served
    # from this same origin, so it needs nothing. Set VERDICT_CORS_ORIGINS to a
    # comma-separated allowlist only if a separate frontend must read /api/data.
    _cors = [o.strip() for o in os.environ.get("VERDICT_CORS_ORIGINS", "").split(",") if o.strip()]
    if _cors:
        app.add_middleware(CORSMiddleware, allow_origins=_cors,
                           allow_methods=["GET"], allow_headers=["*"])

    # The dashboard shell and live data require the password. Health and static
    # assets contain no stored telemetry and remain public.
    def _is_gated(path: str) -> bool:
        core_path = path in {"/", "/dashboard"} or (
            path.startswith("/api/") and path != "/api/health"
        )
        operations_path = operations_page_url is not None and (
            path == operations_page_url or path.startswith(operations_page_url + "/")
        )
        return core_path or operations_path

    @app.middleware("http")
    async def basic_auth(request, call_next):
        """Gate the dashboard + live data behind HTTP Basic Auth when configured.

        Set VERDICT_USER and VERDICT_PASS to require a login. If either is unset
        (local dev), the gate is disabled. The dashboard shells at / and
        /dashboard and dashboard API routes are protected; /api/health stays public.
        """
        user = os.environ.get("VERDICT_USER")
        pw = os.environ.get("VERDICT_PASS")
        path = request.scope["path"]
        root_path = request.scope.get("root_path", "").rstrip("/")
        if root_path and (path == root_path or path.startswith(root_path + "/")):
            path = path[len(root_path):] or "/"
        if user and pw and request.method != "OPTIONS" and _is_gated(path):
            header = request.headers.get("authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    raw = base64.b64decode(header[6:]).decode("utf-8")
                    u, _, p = raw.partition(":")
                    user_ok = secrets.compare_digest(u, user)
                    password_ok = secrets.compare_digest(p, pw)
                    ok = user_ok & password_ok
                except Exception:
                    ok = False
            if not ok:
                resp = Response("Authentication required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Verdict"'})
                for k, v in _SECURITY_HEADERS.items():
                    resp.headers.setdefault(k, v)
                return resp
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request, call_next):
        """Apply baseline security headers to every response."""
        resp = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    def _serve(filename: str) -> HTMLResponse:
        html = STATIC / filename
        if not html.exists():
            return HTMLResponse(f"<h1>{filename} not found</h1>"
                                "<p>Run <code>pnpm --dir ui build</code> to generate it.</p>",
                                status_code=404)
        return HTMLResponse(html.read_text())

    @app.get("/api/health")
    def health():
        # Public endpoint — do not leak a filesystem path or credential-bearing DSN.
        configured = _is_postgres(configured_storage) or _sqlite_path(
            configured_storage
        ).exists()
        return {"status": "ok", "storage": backend, "configured": configured}

    @app.get("/api/config")
    def config():
        result = {"operationsUrl": operations_url, "tenantId": configured_tenant}
        if operations_page_url is not None:
            result["operationsPageUrl"] = operations_page_url
        return result

    from verdict.dashboard.setup_routes import SetupRoutes

    setup_routes = SetupRoutes(
        configured_storage, setup_token, tenant_id=configured_tenant
    )
    setup_routes.register(app)
    _setup_authorized = setup_routes.authorized

    from verdict.dashboard.control_routes import ControlRoutes
    from verdict.dashboard.day_explanation import register_day_explanation_routes
    from verdict.dashboard.evidence_explanation import register_explanation_routes
    from verdict.dashboard.experiments import register_experiment_routes
    from verdict.dashboard.lab_routes import register_lab_routes
    from verdict.dashboard.monitor_routes import MonitorRoutes
    from verdict.dashboard.scoped_evaluation import register_scoped_routes

    register_lab_routes(app, setup_routes)
    register_experiment_routes(app, setup_routes)
    register_scoped_routes(app, setup_routes)
    register_day_explanation_routes(app, setup_routes)
    register_explanation_routes(app, setup_routes)
    monitor_routes = MonitorRoutes(setup_routes)
    monitor_routes.register(app)
    ControlRoutes(configured_storage, setup_routes, monitor_routes).register(app)

    def _authorized_tenant(request: Request) -> str:
        host_tenant = getattr(request.state, "verdict_registry_tenant", None)
        authorized_tenant = (
            host_tenant if host_tenant is not None else configured_tenant
        )
        if safe_tenant_id(authorized_tenant) is None:
            raise ValueError("invalid tenant")
        return authorized_tenant

    def data(
        request: Request,
        evaluator: str | None = None,
        trace_offset: int = Query(default=0, ge=0),
        trace_judge_status: str = "all",
        trace_id: str | None = None,
        trace_date: str | None = None,
        report_days: int = 30,
    ):
        if report_days not in REPORT_PERIOD_DAYS:
            return JSONResponse({"error": "report_days must be 0, 7, 30, or 90"}, status_code=422)
        if not _is_postgres(configured_storage) and not _sqlite_path(
            configured_storage
        ).exists():
            # Do NOT leak the absolute DB path in the HTTP body — log it
            # server-side and return a generic 503 so the dashboard degrades
            # gracefully instead of crashing or exposing the filesystem layout.
            # The state lets the UI treat a store that setup has not created
            # yet as first run rather than as a failed read.
            _log.warning("dashboard data unavailable: SQLite database not found")
            return JSONResponse(
                {"error": "data unavailable", "state": "store_not_created"},
                status_code=503,
            )
        try:
            authorized_tenant = _authorized_tenant(request)
        except ValueError:
            return JSONResponse({"error": "invalid data request"}, status_code=400)
        try:
            bundle = build_bundle(
                configured_storage,
                evaluator_id=evaluator,
                registry_tenant=authorized_tenant,
                trace_offset=trace_offset,
                trace_judge_status=trace_judge_status,
                trace_id=trace_id,
                trace_date=trace_date,
                report_days=report_days,
            )
            bundle["meta"]["storageBackend"] = backend
            bundle["meta"]["dataProvenance"] = (
                "synthetic" if synthetic_demo else "unspecified"
            )
            bundle["monitor"] = monitor_routes.read_state(tenant_id=authorized_tenant)
            return bundle
        except ValueError:
            return JSONResponse({"error": "invalid data request"}, status_code=422)
        except DashboardBundleLimitError:
            _log.exception("dashboard bundle exceeded its safety budget")
            return JSONResponse(
                {"error": "dashboard bundle limit exceeded"},
                status_code=503,
            )
        except Exception:  # pragma: no cover - defensive: corrupt/locked DB
            _log.exception("failed to build %s dashboard data bundle", backend)
            return JSONResponse({"error": "data unavailable"}, status_code=503)

    data.__annotations__["request"] = Request
    app.get("/api/data")(data)

    def registry(
        request,
        version: str | None = None,
        assignment_limit: int = 50,
        assignment_offset: int = 0,
    ):
        try:
            authorized_tenant = _authorized_tenant(request)
            return build_registry_bundle(
                configured_storage,
                tenant=authorized_tenant,
                version_id=version,
                assignment_limit=assignment_limit,
                assignment_offset=assignment_offset,
            )
        except RegistryNotFoundError:
            return JSONResponse({"error": "registry version not found"}, status_code=404)
        except ValueError:
            return JSONResponse({"error": "invalid registry request"}, status_code=400)
        except (RegistryStateError, DashboardBundleLimitError):
            _log.exception("registry dashboard state is invalid")
            return JSONResponse({"error": "registry data unavailable"}, status_code=503)
        except Exception:  # pragma: no cover - defensive: corrupt/locked DB
            _log.exception("failed to build %s registry dashboard data", backend)
            return JSONResponse({"error": "registry data unavailable"}, status_code=503)

    registry.__annotations__["request"] = Request
    app.get("/api/registry")(registry)

    def agent_runs(
        request,
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100_000),
        run_id: str | None = None,
        evaluator_fingerprint: str | None = None,
        view: str = "full",
        selected_run_id: str | None = None,
        source_kind: str | None = None,
        agent_name: str | None = None,
    ):
        try:
            authorized_tenant = _authorized_tenant(request)
            requested_run_ids = request.query_params.getlist("run_ids")
            selected_run_ids = tuple(requested_run_ids) if requested_run_ids else None
            return build_agent_runs_bundle(
                configured_storage,
                tenant=authorized_tenant,
                limit=limit,
                offset=offset,
                run_id=run_id,
                run_ids=selected_run_ids,
                evaluator_fingerprint=evaluator_fingerprint,
                view=view,
                selected_run_id=selected_run_id,
                source_kind=source_kind,
                agent_name=agent_name,
            )
        except ValueError:
            return JSONResponse({"error": "invalid runs request"}, status_code=400)
        except (DashboardBundleLimitError, OSError, sqlite3.DatabaseError):
            _log.exception("failed to build %s agent runs bundle", backend)
            return JSONResponse({"error": "agent runs unavailable"}, status_code=503)

    agent_runs.__annotations__["request"] = Request
    app.get("/api/runs")(agent_runs)

    def agent_run_outcomes(request):
        try:
            return build_run_outcome_preview(
                configured_storage, tenant=_authorized_tenant(request),
            )
        except ValueError:
            return JSONResponse({"error": "invalid run outcome request"}, status_code=400)
        except (OSError, sqlite3.DatabaseError):
            _log.exception("failed to build %s run outcome preview", backend)
            return JSONResponse({"error": "run outcome preview unavailable"}, status_code=503)

    agent_run_outcomes.__annotations__["request"] = Request
    app.get("/api/agent-run-outcomes")(agent_run_outcomes)

    def agent_usage(request, day: str | None = None, session_id: str | None = None,
                    source_kind: str | None = None):
        try:
            return build_agent_usage_bundle(
                configured_storage, tenant=_authorized_tenant(request),
                day=day, session_id=session_id, source_kind=source_kind,
            )
        except ValueError:
            return JSONResponse({"error": "invalid usage request"}, status_code=400)
        except (DashboardBundleLimitError, OSError, sqlite3.DatabaseError):
            _log.exception("failed to build %s agent usage bundle", backend)
            return JSONResponse({"error": "agent usage unavailable"}, status_code=503)

    agent_usage.__annotations__["request"] = Request
    app.get("/api/usage")(agent_usage)

    def agent_session(request, session_id: str, source_kind: str):
        try:
            return build_agent_session_bundle(
                configured_storage, tenant=_authorized_tenant(request),
                source_kind=source_kind, session_id=session_id,
            )
        except KeyError:
            return JSONResponse({"error": "agent session not found"}, status_code=404)
        except ValueError:
            return JSONResponse({"error": "invalid agent session request"}, status_code=400)
        except (DashboardBundleLimitError, OSError, sqlite3.DatabaseError):
            _log.exception("failed to build %s agent session bundle", backend)
            return JSONResponse({"error": "agent session unavailable"}, status_code=503)

    agent_session.__annotations__["request"] = Request
    app.get("/api/session")(agent_session)

    def agent_run_detail(
        run_id: str,
        request,
        event_limit: int = Query(default=100, ge=1, le=200),
        event_offset: int = Query(default=0, ge=0),
        turn_limit: int = Query(default=20, ge=1, le=50),
        turn_offset: int = Query(default=0, ge=0),
        event_id: str | None = None,
        turn_evaluator_fingerprint: str | None = None,
    ):
        try:
            authorized_tenant = _authorized_tenant(request)
            return build_agent_run_detail(
                configured_storage,
                tenant=authorized_tenant,
                run_id=run_id,
                event_limit=event_limit,
                event_offset=event_offset,
                turn_limit=turn_limit,
                turn_offset=turn_offset,
                event_id=event_id,
                turn_evaluator_fingerprint=turn_evaluator_fingerprint,
            )
        except KeyError:
            return JSONResponse({"error": "agent run not found"}, status_code=404)
        except ValueError:
            return JSONResponse({"error": "invalid run detail request"}, status_code=400)
        except (DashboardBundleLimitError, OSError, sqlite3.DatabaseError):
            _log.exception("failed to build %s agent run detail", backend)
            return JSONResponse({"error": "agent run unavailable"}, status_code=503)

    agent_run_detail.__annotations__["request"] = Request
    app.get("/api/runs/{run_id}")(agent_run_detail)

    def recent_local_events(
        run_id: str, request, revision: str | None = None,
        turn_id: str | None = None, event_id: str | None = None,
    ):
        try:
            if revision is not None and (len(revision) != 64
                                         or any(char not in "0123456789abcdef" for char in revision)):
                return JSONResponse({"error": "invalid recent local sample request"}, status_code=400)
            if ((turn_id is None) != (event_id is None)
                    or (turn_id is not None and revision is None)
                    or any(not isinstance(value, str) or not value
                           or len(value.encode("utf-8")) > 256
                           for value in (turn_id, event_id) if value is not None)):
                return JSONResponse({"error": "invalid recent local sample request"}, status_code=400)
            sample = build_recent_local_sample(
                configured_storage, tenant=_authorized_tenant(request), run_id=run_id,
                revision=revision, turn_id=turn_id, event_id=event_id,
            )
            if (revision is not None and sample.get("sourceRevision") != revision
                    and sample.get("historicalFocusedEvent") is not True
                    and sample.get("focusedEvent") is None):
                return JSONResponse({"error": "stale_revision"}, status_code=409)
            if event_id is not None and sample.get("historicalFocusedEvent") is not True:
                if sample.get("schema") != "local-recent-window-v2":
                    return JSONResponse({"error": "recent event not found"}, status_code=404)
                selected = next((event for event in sample["events"]
                                 if event.get("eventId") == event_id
                                 and event.get("turnId") == turn_id), None)
                if selected is None:
                    return JSONResponse({"error": "recent event not found"}, status_code=404)
                sample["focusedEvent"] = selected
            return sample
        except KeyError:
            if revision is not None:
                current = build_agent_runs_bundle(
                    configured_storage, tenant=_authorized_tenant(request), run_id=run_id,
                )
                if current.get("runs"):
                    return JSONResponse({"error": "stale_revision"}, status_code=409)
            return JSONResponse({"error": "recent local sample not found"}, status_code=404)
        except ValueError:
            _log.exception("invalid stored %s recent local sample", backend)
            return JSONResponse({"error": "recent local sample unavailable"}, status_code=503)
        except (DashboardBundleLimitError, OSError, sqlite3.DatabaseError):
            _log.exception("failed to read %s recent local sample", backend)
            return JSONResponse({"error": "recent local sample unavailable"}, status_code=503)

    recent_local_events.__annotations__["request"] = Request
    app.get("/api/runs/{run_id}/recent-local-events")(recent_local_events)

    def full_local_events(
        run_id: str, request, revision: str, limit: int = 100,
        after_ordinal: int | None = None, after_event_id: str | None = None,
    ):
        try:
            return build_full_local_event_page(
                configured_storage, tenant=_authorized_tenant(request),
                run_id=run_id, revision=revision, limit=limit,
                after_ordinal=after_ordinal, after_event_id=after_event_id,
            )
        except KeyError:
            return JSONResponse({"error": "local history not found"}, status_code=404)
        except ValueError as exc:
            reason = str(exc)
            if reason in {"stale_revision", "stale_cursor", "partial_projection"}:
                return JSONResponse({"error": reason}, status_code=409)
            return JSONResponse({"error": "invalid local event page request"}, status_code=400)
        except (RuntimeError, DashboardBundleLimitError, OSError, sqlite3.DatabaseError):
            _log.exception("failed to read %s full local event page", backend)
            return JSONResponse({"error": "local event page unavailable"}, status_code=503)

    full_local_events.__annotations__["request"] = Request
    app.get("/api/runs/{run_id}/local-events")(full_local_events)

    def full_local_analysis(run_id: str, request):
        try:
            return build_full_local_run_analysis(
                configured_storage, tenant=_authorized_tenant(request), run_id=run_id,
            )
        except KeyError:
            return JSONResponse({"error": "local history not found"}, status_code=404)
        except ValueError as exc:
            if str(exc) == "partial_projection":
                return JSONResponse({"error": "partial_projection"}, status_code=409)
            return JSONResponse({"error": "invalid local run analysis request"}, status_code=400)
        except (RuntimeError, DashboardBundleLimitError, OSError, sqlite3.DatabaseError):
            _log.exception("failed to analyze %s full local run", backend)
            return JSONResponse({"error": "local run analysis unavailable"}, status_code=503)

    full_local_analysis.__annotations__["request"] = Request
    app.get("/api/runs/{run_id}/recorded-analysis")(full_local_analysis)

    def agent_turn_analysis(run_id: str, turn_id: str, request):
        try:
            return build_agent_turn_analysis(
                configured_storage, tenant=_authorized_tenant(request),
                run_id=run_id, turn_id=turn_id,
            )
        except KeyError:
            return JSONResponse({"error": "agent turn not found"}, status_code=404)
        except ValueError:
            return JSONResponse({"error": "invalid turn analysis request"}, status_code=400)
        except (DashboardBundleLimitError, RuntimeError, OSError, sqlite3.DatabaseError):
            _log.exception("failed to build %s agent turn analysis", backend)
            return JSONResponse({"error": "agent turn analysis unavailable"}, status_code=503)

    agent_turn_analysis.__annotations__["request"] = Request
    app.get("/api/runs/{run_id}/turns/{turn_id}/analysis")(agent_turn_analysis)

    def agent_insights(request):
        try:
            authorized_tenant = _authorized_tenant(request)
            return read_latest_analysis(
                configured_storage,
                tenant=authorized_tenant,
                empty_result=_empty_agent_insights(),
            )
        except ValueError:
            return JSONResponse({"error": "invalid insights request"}, status_code=400)
        except (DashboardBundleLimitError, OSError, sqlite3.DatabaseError):
            _log.exception("failed to build %s agent insights", backend)
            return JSONResponse({"error": "agent insights unavailable"}, status_code=503)

    agent_insights.__annotations__["request"] = Request
    app.get("/api/insights")(agent_insights)

    def run_agent_insights(request):
        if not _setup_authorized(request):
            return JSONResponse({"error": "analysis authorization required"}, status_code=403)
        try:
            authorized_tenant = _authorized_tenant(request)
            return run_analysis(
                configured_storage,
                tenant=authorized_tenant,
                build=lambda: build_agent_insights_bundle(
                    configured_storage,
                    tenant=authorized_tenant,
                    _include_input_fingerprint=True,
                ),
            )
        except ValueError:
            return JSONResponse({"error": "invalid insights request"}, status_code=400)
        except (DashboardBundleLimitError, OSError, sqlite3.DatabaseError, RuntimeError):
            _log.exception("failed to run %s agent insights", backend)
            return JSONResponse({"error": "agent insights failed"}, status_code=503)

    run_agent_insights.__annotations__["request"] = Request
    app.post("/api/insights/run")(run_agent_insights)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _serve("dashboard.html")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        # Gated by the middleware above.
        return _serve("dashboard.html")

    return app


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Serve the Verdict dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--tenant-id",
        default=os.environ.get("VERDICT_TENANT_ID", LOCAL_TENANT),
        help="Tenant scope to read and mutate (default: VERDICT_TENANT_ID or local)",
    )
    parser.add_argument("--open-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--synthetic-demo", action="store_true",
        help="Label the connected store as illustrative synthetic data",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--storage", help="SQLite URL/path or PostgreSQL DSN")
    source.add_argument("--db", help="legacy SQLite path")
    args = parser.parse_args(argv)
    if safe_tenant_id(args.tenant_id) is None:
        parser.error(
            "--tenant-id must be a non-sensitive bounded routing identifier "
            "of at most 128 ASCII characters"
        )
    configured_storage = resolve_storage(args.storage or args.db)
    backend = "postgresql" if _is_postgres(configured_storage) else "sqlite"
    print(f"Verdict dashboard → http://{args.host}:{args.port}")
    print(f"Reading storage   → {backend}")
    print(f"Tenant scope      → {args.tenant_id}")

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        if not (os.environ.get("VERDICT_USER") and os.environ.get("VERDICT_PASS")):
            parser.error(
                "non-loopback binding requires VERDICT_USER and VERDICT_PASS"
            )
        if not os.environ.get("VERDICT_ALLOWED_HOSTS"):
            parser.error(
                "non-loopback binding requires an explicit VERDICT_ALLOWED_HOSTS allowlist"
            )

    import uvicorn
    if args.open_browser:
        import threading
        import webbrowser
        threading.Timer(
            0.5, webbrowser.open, args=(f"http://{args.host}:{args.port}/dashboard",),
        ).start()
    uvicorn.run(
        create_app(
            storage=configured_storage, tenant_id=args.tenant_id,
            synthetic_demo=args.synthetic_demo,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
