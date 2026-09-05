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
import json
import logging
import math
import os
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from verdict.analysis import analyze_agent_run
from verdict.analysis_records import analysis_run_from_json
from verdict.cluster_health import UNCLUSTERED_ID, assess_cluster_health
from verdict.dashboard.analysis_service import read_latest_analysis, run_analysis
from verdict.dashboard.query import PostgresSession as _PostgresSession
from verdict.dashboard.query import QuerySession as _QuerySession
from verdict.dashboard.query import SQLiteSession as _SQLiteSession
from verdict.dashboard.registry import (
    RegistryNotFoundError,
    RegistryStateError,
    active_cluster_projection,
)
from verdict.dashboard.registry import (
    build_registry_bundle as _build_registry_bundle,
)
from verdict.dashboard.storage_url import is_postgres_storage
from verdict.dashboard.trace_facts import deterministic_trace_facts
from verdict.evidence import agent_run_bundle_from_json
from verdict.metrics import ScoreCounts, verdict_label
from verdict.redaction import redact, redact_structure

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
# drift signals tag the regressed stream in cluster_id; map those aliases back
# to a provider key so the UI can attribute the signal.
PROVIDER_ALIAS = {"haiku": "anthropic", "gpt": "openai", "gpt-4o-mini": "openai",
                  "gemini": "google", "flash": "google"}
DIM_ORDER = ["groundedness", "relevance", "completeness", "safety", "instruction_following"]
MAX_SERIES_POINTS = 100
MAX_DASHBOARD_PROVIDERS = 8
MAX_DASHBOARD_CLUSTERS = 20
MAX_DASHBOARD_DIMENSIONS = 12
MAX_DASHBOARD_EVALUATORS = 20
MAX_DASHBOARD_DRIFT_SIGNALS = 40
MAX_PROVIDER_MODELS = 20
MAX_TRACE_SAMPLES = 30
DRIFT_CURRENT_HOURS = 24
DRIFT_BASELINE_LAG_HOURS = 24
DRIFT_BASELINE_DAYS = 7
DRIFT_MIN_SAMPLE_SIZE = 30
_DISPLAY_WORKLOAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_FINDING_SUMMARIES = {
    "run_status_unknown": "The source does not expose a terminal status for these agent sessions.",
    "response_not_evaluable": "At least one response is unavailable, so response quality is not evaluable.",
    "event_capture_partial": "The source exceeded the bounded event limit; event analysis is partial.",
    "tool_error": "One or more captured tool results reported failure.",
    "command_failed": "One or more captured commands returned a non-zero status.",
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


def _drift_analysis(
    *,
    current: int,
    baseline: int,
    run_status: str,
) -> dict[str, Any]:
    if current < DRIFT_MIN_SAMPLE_SIZE:
        readiness_status = "not_enough_current"
    elif baseline < DRIFT_MIN_SAMPLE_SIZE:
        readiness_status = "not_enough_baseline"
    else:
        readiness_status = "global_minimum_met"
    return {
        "runStatus": run_status,
        "readinessStatus": readiness_status,
        "current": current,
        "baseline": baseline,
        "minimum": DRIFT_MIN_SAMPLE_SIZE,
        "currentHours": DRIFT_CURRENT_HOURS,
        "baselineLagHours": DRIFT_BASELINE_LAG_HOURS,
        "baselineDays": DRIFT_BASELINE_DAYS,
    }


def _signal_provider(
    alias: str,
    provider_keys: set[str],
    cluster_providers: dict[str, set[str]],
) -> str | None:
    if alias in provider_keys:
        return alias

    providers = cluster_providers.get(alias, set())
    if providers:
        return next(iter(providers)) if len(providers) == 1 else None
    if alias in PROVIDER_ALIAS:
        mapped = PROVIDER_ALIAS[alias]
        return mapped if mapped in provider_keys else None
    return None


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
    if storage.startswith("sqlite:///"):
        storage = storage.removeprefix("sqlite:///")
    elif "://" in storage:
        raise ValueError("dashboard storage must use sqlite or postgresql")
    return Path(storage).expanduser().resolve()


def _is_postgres(storage: str) -> bool:
    return is_postgres_storage(storage)


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
) -> dict:
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
    configured = str(storage)
    if _is_postgres(configured):
        return _build_from_postgres(
            configured,
            evaluator_id=evaluator_id,
            registry_tenant=registry_tenant,
            trace_offset=trace_offset,
            trace_judge_status=trace_judge_status,
            trace_id=trace_id,
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


def build_agent_runs_bundle(
    storage: str | os.PathLike[str], *, tenant: str, limit: int = 30,
    run_id: str | None = None, run_ids: tuple[str, ...] | None = None,
    evaluator_fingerprint: str | None = None,
) -> dict:
    """Read a bounded, analyzed agent-run view without migrating the store."""
    if not isinstance(tenant, str) or not tenant or len(tenant.encode("utf-8")) > 256:
        raise ValueError("invalid tenant")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("invalid limit")
    if run_id is not None and (
        not isinstance(run_id, str) or not run_id or len(run_id.encode("utf-8")) > 256
    ):
        raise ValueError("invalid run_id")
    if run_id is not None and run_ids is not None:
        raise ValueError("run_id and run_ids are mutually exclusive")
    if run_ids is not None:
        if not isinstance(run_ids, tuple) or not 1 <= len(run_ids) <= 50:
            raise ValueError("invalid run_ids")
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("run_ids must be unique")
        for selected_run_id in run_ids:
            if (
                not isinstance(selected_run_id, str)
                or not selected_run_id
                or len(selected_run_id.encode("utf-8")) > 256
            ):
                raise ValueError("invalid run_ids")
    if evaluator_fingerprint is not None and (
        len(evaluator_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in evaluator_fingerprint)
    ):
        raise ValueError("invalid evaluator fingerprint")
    configured = str(storage)

    def builder(session: _QuerySession) -> dict:
        if not session.table_exists("agent_run_bundles"):
            return {"summary": {"available": 0, "shown": 0}, "runs": []}
        selected_run_ids = run_ids or ((run_id,) if run_id is not None else None)
        if selected_run_ids is None:
            count = session.execute(
                "SELECT COUNT(*) AS count FROM agent_run_bundles WHERE tenant_id=?", (tenant,)
            ).fetchone()
            rows = session.execute(
                "SELECT payload_json FROM agent_run_bundles WHERE tenant_id=? "
                "ORDER BY started_at DESC, run_id DESC LIMIT ?", (tenant, limit),
            )
        else:
            placeholders = ",".join("?" for _ in selected_run_ids)
            parameters = (tenant, *selected_run_ids)
            count = session.execute(
                "SELECT COUNT(*) AS count FROM agent_run_bundles "
                f"WHERE tenant_id=? AND run_id IN ({placeholders})",  # nosec B608
                parameters,
            ).fetchone()
            rows = session.execute(
                "SELECT payload_json FROM agent_run_bundles "
                f"WHERE tenant_id=? AND run_id IN ({placeholders}) "  # nosec B608
                "ORDER BY started_at DESC, run_id DESC",
                parameters,
            )
        runs = []
        for row in rows:
            raw = row["payload_json"]
            if isinstance(raw, dict):
                raw = json.dumps(raw, sort_keys=True, separators=(",", ":"))
            bundle = agent_run_bundle_from_json(raw)
            analysis = analyze_agent_run(bundle)
            turn_outcomes = Counter(turn.status.value for turn in bundle.turns)
            finding_severity = Counter(finding.severity for finding in analysis.findings)
            runs.append({
                "runId": bundle.run.run_id,
                "sourceKind": bundle.session.source_kind,
                "startedAt": bundle.run.started_at.isoformat(),
                "status": bundle.run.status.value,
                "sourceOutcome": bundle.run.status.value,
                "agentVersion": bundle.run.agent_version or None,
                "turnCount": len(bundle.turns),
                "eventCount": len(bundle.events),
                "metrics": analysis.metrics,
                "evidenceCoverage": analysis.evidence_coverage,
                "turnOutcomes": dict(sorted(turn_outcomes.items())),
                "findingSeverity": dict(sorted(finding_severity.items())),
                "findings": [{
                    "code": finding.code, "severity": finding.severity,
                    "message": finding.message,
                    "evidenceEventIds": list(finding.evidence_event_ids),
                    "judgeUsed": finding.judge_used,
                } for finding in analysis.findings],
            })
        runs_by_id = {run["runId"]: run for run in runs}
        linked_trace_ids: dict[str, set[str]] = defaultdict(set)
        trace_scan_complete = True
        trace_ids: set[str] = set()
        if runs and session.table_exists("traces"):
            columns = session.columns("traces")
            tags_column = "tags_json" if "tags_json" in columns else "tags" if "tags" in columns else None
            if tags_column is not None:
                trace_rows = list(session.execute(
                    f"SELECT trace_id,{tags_column} AS tags FROM traces "  # nosec B608
                    "WHERE tenant_id=? ORDER BY trace_id LIMIT 100001",
                    (tenant,),
                ))
                trace_scan_complete = len(trace_rows) <= 100_000
                for trace in trace_rows[:100_000]:
                    tags = _json_value(trace["tags"], {})
                    linked_run_id = tags.get("verdict.agent_run_id") if isinstance(tags, dict) else None
                    if linked_run_id in runs_by_id:
                        linked_trace_ids[linked_run_id].add(trace["trace_id"])
                        trace_ids.add(trace["trace_id"])
        latest_judgment_status: dict[str, tuple[tuple[str, str], str]] = {}
        if evaluator_fingerprint is not None and trace_ids and session.table_exists("judgments"):
            for judgment in session.execute(
                """SELECT trace_id,status,created_at,judgment_id FROM judgments
                   WHERE evaluator_fingerprint=? ORDER BY created_at,judgment_id""",
                (evaluator_fingerprint,),
            ):
                if judgment["trace_id"] not in trace_ids:
                    continue
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
                "complete": trace_scan_complete,
            }
        result = {"summary": {"available": int(count["count"] if count else 0),
                              "shown": len(runs)}, "runs": runs}
        if selected_run_ids is not None:
            result["filter"] = {
                "requested": len(selected_run_ids),
                "matched": len(runs),
                "complete": len(runs) == len(selected_run_ids),
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
    redacted = redact_structure(result)
    if not isinstance(redacted, dict):
        raise DashboardBundleLimitError("bounded agent-run bundle exceeded redaction budget")
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
    configured = str(storage)

    def builder(session: _QuerySession) -> dict:
        if not session.table_exists("agent_run_bundles"):
            raise KeyError(run_id)
        row = session.execute(
            "SELECT payload_json FROM agent_run_bundles WHERE tenant_id=? AND run_id=?",
            (tenant, run_id),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        raw = row["payload_json"]
        if isinstance(raw, dict):
            raw = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        bundle = agent_run_bundle_from_json(raw)
        available = len(bundle.events)
        resolved_event_offset = event_offset
        if event_id is not None:
            matching_index = next(
                (index for index, event in enumerate(bundle.events) if event.event_id == event_id),
                None,
            )
            if matching_index is None:
                raise KeyError(event_id)
            resolved_event_offset = (matching_index // event_limit) * event_limit
        shown = bundle.events[resolved_event_offset:resolved_event_offset + event_limit]
        shown_turns = bundle.turns[turn_offset:turn_offset + turn_limit]
        judgment_summaries: dict[str, dict[str, object]] = {}
        if session.table_exists("judgments"):
            columns = session.columns("judgments")
            dimensions_column = (
                "dimensions_json" if "dimensions_json" in columns else "dimensions"
            )
            for trace_id in {event.trace_id for event in shown if event.trace_id}:
                judgment = session.execute(
                    f"SELECT judgment_id,evaluator_fingerprint,status,{dimensions_column} AS dimensions "  # nosec B608 -- schema-selected identifier
                    "FROM judgments WHERE trace_id=? ORDER BY created_at DESC,judgment_id DESC LIMIT 1",
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
        return {
            "runId": bundle.run.run_id,
            "focusEventId": event_id,
            "sourceKind": bundle.session.source_kind,
            "startedAt": bundle.run.started_at.isoformat(),
            "endedAt": bundle.run.ended_at.isoformat() if bundle.run.ended_at else None,
            "status": bundle.run.status.value,
            "turns": [{
                "turnId": turn.turn_id, "sequence": turn.sequence,
                "startedAt": turn.started_at.isoformat(), "status": turn.status.value,
                "requestState": turn.request_state.value,
                "responseState": turn.response_state.value,
                "request": turn.user_request_redacted,
                "response": turn.final_response_redacted,
            } for turn in shown_turns],
            "turnPage": {
                "available": len(bundle.turns), "shown": len(shown_turns),
                "offset": turn_offset, "limit": turn_limit,
                "truncated": turn_offset + len(shown_turns) < len(bundle.turns),
            },
            "events": [{
                "eventId": event.event_id,
                "turnId": event.turn_id,
                "sequence": event.sequence,
                "timelineIndex": resolved_event_offset + index,
                "occurredAt": event.occurred_at.isoformat(),
                "type": event.event_type.value,
                "status": event.status.value,
                "provenance": event.provenance,
                "privacy": event.privacy_classification.value,
                "omissionReason": event.omission_reason,
                "traceId": event.trace_id,
                "judgment": judgment_summaries.get(event.trace_id),
                "attributes": event.attributes,
            } for index, event in enumerate(shown)],
            "page": {
                "available": available,
                "shown": len(shown),
                "offset": resolved_event_offset,
                "limit": event_limit,
                "truncated": resolved_event_offset + len(shown) < available,
            },
        }

    if _is_postgres(configured):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError("PostgreSQL dashboard support requires the postgres extra") from exc
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
        finally:
            connection.close()
    redacted = redact_structure(result)
    if not isinstance(redacted, dict):
        raise DashboardBundleLimitError("bounded agent-run detail exceeded redaction budget")
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
        if not session.table_exists("agent_run_bundles"):
            return _empty_agent_insights()
        count_row = session.execute(
            "SELECT COUNT(*) AS count FROM agent_run_bundles WHERE tenant_id=?", (tenant,)
        ).fetchone()
        available = int(count_row["count"] if count_row else 0)
        input_hasher = hashlib.sha256()
        rows = session.execute(
            "SELECT payload_json FROM agent_run_bundles WHERE tenant_id=? "
            "ORDER BY started_at ASC, run_id ASC LIMIT ?",
            (tenant, scan_limit),
        )
        event_types: Counter[str] = Counter()
        event_statuses: Counter[str] = Counter()
        run_outcomes: Counter[str] = Counter()
        turn_outcomes: Counter[str] = Counter()
        prompt_states: Counter[str] = Counter()
        response_states: Counter[str] = Counter()
        finding_counts: Counter[tuple[str, str]] = Counter()
        finding_messages: dict[tuple[str, str], str] = {}
        finding_run_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
        source_metrics: dict[str, Counter[str]] = defaultdict(Counter)
        source_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
        run_trace_metrics: dict[str, Counter[str]] = defaultdict(Counter)
        totals: Counter[str] = Counter()
        latency_values: list[float] = []
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
            trace_columns = session.columns("traces")
            trace_tags = (
                "tags_json" if "tags_json" in trace_columns
                else "tags" if "tags" in trace_columns else "NULL"
            )
            trace_count_row = session.execute(
                f"SELECT COUNT(*) AS count FROM traces WHERE {trace_tenant_predicate}",  # nosec B608 -- fixed predicate
                (tenant,),
            ).fetchone()
            trace_available = int(trace_count_row["count"] if trace_count_row else 0)
            trace_rows = list(session.execute(
                "SELECT provider, request_model, operation, finish_reason, input_tokens, "
                "output_tokens, latency_ms, cost_usd, error, prompt_redacted, "
                f"response_redacted, {trace_tags} AS trace_tags "  # nosec B608 -- schema-selected identifier
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
                tags = _json_value(trace["trace_tags"], {})
                run_id = tags.get("verdict.agent_run_id") if isinstance(tags, dict) else None
                run_metrics = (
                    run_trace_metrics[run_id]
                    if isinstance(run_id, str) and run_id else None
                )
                if run_metrics is not None:
                    run_metrics["traces"] += 1
                for name in ("input_tokens", "output_tokens"):
                    value = trace[name]
                    if isinstance(value, int):
                        trace_tokens[name] += value
                        metrics[name] += value
                        if run_metrics is not None:
                            run_metrics[name] += value
                if trace["error"]:
                    metrics["errors"] += 1
                    trace_outcomes["failed"] += 1
                    if run_metrics is not None:
                        run_metrics["errors"] += 1
                else:
                    trace_outcomes["succeeded"] += 1
                latency = trace["latency_ms"]
                if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                    trace_latency_values.append(float(latency))
                    metrics["latency_known"] += 1
                    metrics["latency_ms"] += float(latency)
                    if run_metrics is not None:
                        run_metrics["latency_known"] += 1
                        run_metrics["latency_ms"] += float(latency)
                cost = trace["cost_usd"]
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    trace_cost_values.append(float(cost))
                    metrics["cost_known"] += 1
                    metrics["cost_microusd"] += round(float(cost) * 1_000_000)
                    if run_metrics is not None:
                        run_metrics["cost_known"] += 1
                        run_metrics["cost_microusd"] += round(float(cost) * 1_000_000)
                prompt = trace["prompt_redacted"]
                response = trace["response_redacted"]
                facts = deterministic_trace_facts(
                    error=trace["error"], prompt=prompt, response=response,
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
        analyzed = 0
        for row in rows:
            raw = row["payload_json"]
            if isinstance(raw, dict):
                raw = json.dumps(raw, sort_keys=True, separators=(",", ":"))
            input_hasher.update(raw.encode("utf-8"))
            bundle = agent_run_bundle_from_json(raw)
            analyzed += 1
            analysis = analyze_agent_run(bundle)
            source = bundle.session.source_kind
            run_outcomes[bundle.run.status.value] += 1
            source_outcomes[source][bundle.run.status.value] += 1
            source_metrics[source]["runs"] += 1
            linked = run_trace_metrics.get(bundle.run.run_id)
            if linked is not None:
                for name, value in linked.items():
                    source_metrics[source][f"trace_{name}"] += value
            for turn in bundle.turns:
                turn_outcomes[turn.status.value] += 1
                prompt_states[turn.request_state.value] += 1
                response_states[turn.response_state.value] += 1
            totals["turns"] += len(bundle.turns)
            totals["events"] += len(bundle.events)
            for event in bundle.events:
                event_types[event.event_type.value] += 1
                event_statuses[event.status.value] += 1
                if event.event_type.value == "model_call":
                    totals["model_calls"] += 1
                    source_metrics[source]["model_calls"] += 1
                    if event.trace_id:
                        totals["linked_model_calls"] += 1
                    for name in ("input_tokens", "output_tokens"):
                        value = event.attributes.get(name)
                        if isinstance(value, int):
                            totals[name] += value
                            source_metrics[source][name] += value
                    latency = event.attributes.get("latency_ms")
                    if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                        latency_values.append(float(latency))
                        source_metrics[source]["latency_known"] += 1
                        source_metrics[source]["latency_ms"] += float(latency)
                elif event.event_type.value == "tool_call":
                    totals["tool_calls"] += 1
                    source_metrics[source]["tool_calls"] += 1
            for name in ("tool_errors", "command_failures"):
                value = analysis.metrics.get(name)
                if isinstance(value, int):
                    totals[name] += value
                    source_metrics[source][name] += value
            for finding in analysis.findings:
                key = (finding.code, finding.severity)
                finding_counts[key] += 1
                finding_messages.setdefault(key, finding.message)
                if len(finding_run_ids[key]) < 50:
                    finding_run_ids[key].append(bundle.run.run_id)
        model_calls = totals["model_calls"]
        findings = [
            {
                "code": code,
                "severity": severity,
                "message": _FINDING_SUMMARIES.get(code, finding_messages[(code, severity)]),
                "runs": count,
                "runIds": finding_run_ids[(code, severity)],
                "runIdsTruncated": count > len(finding_run_ids[(code, severity)]),
            }
            for (code, severity), count in sorted(
                finding_counts.items(), key=lambda item: (-item[1], item[0][0])
            )[:50]
        ]
        comparisons = []
        for source, metrics in sorted(source_metrics.items()):
            latency_known = metrics["trace_latency_known"] or metrics["latency_known"]
            latency_total = metrics["trace_latency_ms"] or metrics["latency_ms"]
            trace_calls = metrics["trace_traces"]
            cost_known = metrics["trace_cost_known"]
            comparisons.append({
                "source": source,
                "runs": metrics["runs"],
                "modelCalls": metrics["model_calls"],
                "toolCalls": metrics["tool_calls"],
                "toolErrors": metrics["tool_errors"],
                "commandFailures": metrics["command_failures"],
                "inputTokens": metrics["trace_input_tokens"] or metrics["input_tokens"],
                "outputTokens": metrics["trace_output_tokens"] or metrics["output_tokens"],
                "averageModelLatencyMs": (
                    round(latency_total / latency_known, 2)
                    if latency_known else None
                ),
                "latencyKnownCalls": latency_known,
                "costUsd": (
                    round(metrics["trace_cost_microusd"] / 1_000_000, 8)
                    if cost_known else None
                ),
                "costState": (
                    "complete" if trace_calls and cost_known == trace_calls
                    else "partial" if cost_known else "not_captured"
                ),
                "providerErrors": metrics["trace_errors"],
                "runOutcomes": dict(sorted(source_outcomes[source].items())),
                "retries": None,
                "retryState": "not_captured",
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
                "inputTokens": metrics["input_tokens"],
                "outputTokens": metrics["output_tokens"],
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
        input_tokens = trace_tokens["input_tokens"] or totals["input_tokens"]
        output_tokens = trace_tokens["output_tokens"] or totals["output_tokens"]
        known_latency = trace_latency_values or latency_values
        result = {
            "schema": "agent-insights-v1",
            "scope": {
                "availableRuns": available,
                "analyzedRuns": analyzed,
                "complete": analyzed == available,
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
                    "modelCalls": model_calls,
                    "linked": totals["linked_model_calls"],
                    "unlinked": model_calls - totals["linked_model_calls"],
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
            },
            "performance": {
                "modelCalls": trace_scope["analyzed"] or model_calls,
                "toolCalls": totals["tool_calls"],
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "averageModelLatencyMs": (
                    round(sum(known_latency) / len(known_latency), 2)
                    if known_latency else None
                ),
                "latencyKnownCalls": len(known_latency),
                "costUsd": round(sum(trace_cost_values), 8) if trace_cost_values else None,
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
            "comparisons": comparisons,
            "modelComparisons": model_comparisons,
        }
        result["_analysisInputFingerprint"] = input_hasher.hexdigest()
        return result

    if _is_postgres(configured):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError("PostgreSQL dashboard support requires the postgres extra") from exc
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
        finally:
            connection.close()
    redacted = redact_structure(result)
    if not isinstance(redacted, dict):
        raise DashboardBundleLimitError("bounded agent insights exceeded redaction budget")
    if not _include_input_fingerprint:
        redacted.pop("_analysisInputFingerprint", None)
    return redacted


def _empty_agent_insights() -> dict:
    return {
        "schema": "agent-insights-v1",
        "scope": {"availableRuns": 0, "analyzedRuns": 0, "complete": True,
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
            "toolErrors": 0, "commandFailures": 0,
        },
        "performance": {
            "modelCalls": 0, "toolCalls": 0, "inputTokens": 0, "outputTokens": 0,
            "averageModelLatencyMs": None, "latencyKnownCalls": 0,
            "costUsd": None, "costState": "not_captured",
        },
        "behavior": {
            "findingRuns": 0, "findingTypes": 0, "capturedResponses": 0,
            "averageResponseCharacters": None, "refusals": 0,
            "apologyStarts": 0, "hedges": 0, "validJsonResponses": 0,
        },
        "comparisons": [],
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
            connection.execute("SET TRANSACTION READ ONLY")
            return _redacted_bundle(
                _PostgresSession(connection),
                evaluator_id=evaluator_id,
                registry_tenant=registry_tenant,
                trace_offset=trace_offset,
                trace_judge_status=trace_judge_status,
                trace_id=trace_id,
            )


def _redacted_bundle(
    session: _QuerySession,
    *,
    evaluator_id: str | None,
    registry_tenant: str | None,
    trace_offset: int,
    trace_judge_status: str,
    trace_id: str | None,
) -> dict:
    bundle = redact_structure(
        _build(
            session,
            evaluator_id=evaluator_id,
            registry_tenant=registry_tenant,
            trace_offset=trace_offset,
            trace_judge_status=trace_judge_status,
            trace_id=trace_id,
        )
    )
    if not isinstance(bundle, dict):
        raise DashboardBundleLimitError(
            "bounded dashboard bundle exceeded the redaction structure budget"
        )
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
    if not cur.table_exists("agent_run_bundles"):
        return {
            "available": 0, "sources": [], "sourcesTruncated": False,
            "lastCapturedAt": None,
        }
    count = cur.execute(
        "SELECT COUNT(*) AS count FROM agent_run_bundles WHERE tenant_id=?", (tenant,)
    ).fetchone()
    rows = cur.execute(
        "SELECT source_kind, COUNT(*) AS count FROM agent_run_bundles "
        "WHERE tenant_id=? GROUP BY source_kind ORDER BY source_kind LIMIT 16",
        (tenant,),
    )
    sources = [
        {"sourceKind": row["source_kind"], "runs": int(row["count"])}
        for row in rows
    ]
    source_count = cur.execute(
        "SELECT COUNT(DISTINCT source_kind) AS count FROM agent_run_bundles "
        "WHERE tenant_id=?", (tenant,),
    ).fetchone()
    last = cur.execute(
        "SELECT MAX(started_at) AS newest_started_at FROM agent_run_bundles "
        "WHERE tenant_id=?",
        (tenant,),
    ).fetchone()
    newest_started_at = last["newest_started_at"] if last else None
    if isinstance(newest_started_at, datetime):
        newest_started_at = newest_started_at.isoformat()
    return {
        "available": int(count["count"] if count else 0),
        "sources": sources,
        "sourcesTruncated": int(source_count["count"] if source_count else 0) > len(sources),
        "lastCapturedAt": newest_started_at,
    }


def _empty_bundle(*, agent_runs: dict[str, Any] | None = None) -> dict:
    run_metadata = agent_runs or {
        "available": 0, "sources": [], "sourcesTruncated": False,
        "lastCapturedAt": None,
    }
    truncation = _truncation_metadata({
        "providers": _resource_limit(0, 0, MAX_DASHBOARD_PROVIDERS),
        "providerModels": _resource_limit(0, 0, MAX_PROVIDER_MODELS),
        "clusters": _resource_limit(0, 0, MAX_DASHBOARD_CLUSTERS),
        "dimensions": _resource_limit(0, 0, MAX_DASHBOARD_DIMENSIONS),
        "evaluatorIdentities": _resource_limit(0, 0, MAX_DASHBOARD_EVALUATORS),
        "driftSignals": _resource_limit(0, 0, MAX_DASHBOARD_DRIFT_SIGNALS),
        "latencyPoints": _resource_limit(0, 0, MAX_SERIES_POINTS),
        "hourlyPoints": _resource_limit(0, 0, MAX_SERIES_POINTS),
        "traceSamples": _resource_limit(0, 0, MAX_TRACE_SAMPLES),
    })
    return {
        "meta": {
            "runStart": None,
            "durationHours": 0,
            "totalTraces": 0,
            "totalAgentRuns": run_metadata["available"],
            "agentRunSources": run_metadata["sources"],
            "agentRunSourcesTruncated": run_metadata["sourcesTruncated"],
            "lastAgentCaptureAt": run_metadata["lastCapturedAt"],
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
        "driftSignals": [],
        "driftRun": None,
        "driftAnalysis": _drift_analysis(
            current=0,
            baseline=0,
            run_status="no_completed_run",
        ),
        "clusterHealth": _cluster_health([]),
        "evaluation": {
            "status": "empty",
            "selectedId": None,
            "availableIdentities": [],
            "driftStatus": "empty",
            "unattributedDriftSignals": 0,
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
        "dimensionOverall": [],
        "tsRows": [],
        "passrate": [],
        "clusterPassrate": [],
        "haikuDim": [],
        "focusProvider": None,
        "focusProviderLabel": None,
        "samples": [],
        "providerDimension": [],
        "truncation": truncation,
    }


def _trace_samples(
    cur: _QuerySession,
    *,
    requested_trace_id: str | None,
    trace_judge_status: str,
    trace_offset: int,
    explorer_trace_ids: list[str],
    judgment_status_by_trace: dict[str, str],
    ttime: dict[str, str],
    tcluster: dict[str, str | None],
    cluster_labels: dict[str, str],
    cluster_select: str,
    operation_select: str,
    judg_by_trace: dict[str, dict[str, Any]],
    t0: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build one evaluator-aware, bounded Trace Explorer page."""
    filtered_trace_ids = explorer_trace_ids
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
    sample_trace_ids = ordered_trace_ids[
        trace_offset:trace_offset + MAX_TRACE_SAMPLES
    ]
    if (
        requested_trace_id in filtered_trace_ids
        and requested_trace_id not in sample_trace_ids
    ):
        sample_trace_ids = [
            *sample_trace_ids[:MAX_TRACE_SAMPLES - 1], requested_trace_id,
        ]
    placeholders = ",".join("?" for _ in sample_trace_ids) or "NULL"
    sample_rows = [dict(row) for row in cur.execute(
        # The select expressions are closed-set schema compatibility values.
        "SELECT trace_id, provider, request_model, "
        f"{cluster_select}, {operation_select}, "  # nosec B608
        "input_tokens, output_tokens, "
        "latency_ms, cost_usd, finish_reason, error, started_at, "
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
            sample[content_field] = redact(sample.get(content_field))
        sample["providerKey"] = _provider_key(row["provider"])
        sample["contentCaptured"] = (
            row["prompt_redacted"] is not None or row["response_redacted"] is not None
        )
        sample["hour"] = round(
            (_dt(row["started_at"]) - t0).total_seconds() / 3600, 2
        )
        latency = _round_or_none(row["latency_ms"], 0)
        sample["latency_ms"] = int(latency) if latency is not None else None
        if sample.get("response_redacted"):
            sample["response_redacted"] = sample["response_redacted"][:600]
        judgment = judg_by_trace.get(row["trace_id"])
        if judgment:
            sample["judgment"] = judgment
        sample["providerStatus"] = (
            "provider_error" if sample.get("error") else "provider_succeeded"
        )
        sample["judgeStatus"] = judgment_status_by_trace.get(
            row["trace_id"], "not_judged"
        )
        facts = deterministic_trace_facts(
            error=row["error"],
            prompt=row["prompt_redacted"],
            response=row["response_redacted"],
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
    return samples, filtered_trace_ids


def _time_series_read_model(
    cur: _QuerySession,
    *,
    keys: list[str],
    t0: datetime,
    judg_by_trace: dict[str, dict[str, Any]],
    ttime: dict[str, str],
    tp: dict[str, str],
    tcluster: dict[str, str | None],
    all_keys: list[str],
    clusters: list[dict[str, Any]],
    dims_present: list[str],
    drift: list[dict[str, Any]],
    raw_provider_of: dict[str, object],
    model_of: dict[str, str],
) -> dict[str, Any]:
    """Build bounded operational and evaluator time-series projections."""
    bin_seconds = 30 * 60
    bins = defaultdict(lambda: defaultdict(lambda: {"n": 0, "err": 0, "lat": []}))
    for row in cur.execute("SELECT provider, started_at, latency_ms, error FROM traces"):
        provider_key = _provider_key(row["provider"])
        if provider_key not in keys:
            continue
        bucket = int((_dt(row["started_at"]) - t0).total_seconds() // bin_seconds)
        cell = bins[provider_key][bucket]
        cell["n"] += 1
        if row["error"]:
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
                    round(sum(cell["lat"]) / len(cell["lat"]) / 1000, 2)
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
    drift_focus = drift[0].get("provider") if drift else None
    focus = drift_focus if drift_focus in keys else (keys[0] if keys else None)
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
    }
    if _table_exists(cur, "deterministic_analysis_runs"):
        row = cur.execute(
            """SELECT payload_json FROM deterministic_analysis_runs
               WHERE tenant_id=? AND scope_key='agent-and-trace'
               ORDER BY completed_at DESC, analysis_id DESC LIMIT 1""",
            (tenant,),
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
) -> dict:
    requested_trace_id = trace_id
    agent_runs = _agent_run_metadata(cur, registry_tenant or "__verdict_local__")
    if not _table_exists(cur, "traces"):
        return _empty_bundle(agent_runs=agent_runs)
    t0row = cur.execute("SELECT MIN(started_at) m, MAX(started_at) x FROM traces").fetchone()
    if not t0row or not t0row["m"]:
        return _empty_bundle(agent_runs=agent_runs)
    t0, tmax = _dt(t0row["m"]), _dt(t0row["x"])
    trace_columns = cur.columns("traces")
    cluster_select = (
        "cluster_id" if "cluster_id" in trace_columns else "NULL AS cluster_id"
    )
    operation_select = (
        "operation" if "operation" in trace_columns else "NULL AS operation"
    )

    tags_column = "tags_json" if "tags_json" in trace_columns else (
        "tags" if "tags" in trace_columns else "NULL"
    )
    cost_counts = {
        name: {"traces": 0, "priced": 0, "cost": 0.0}
        for name in ("agent", "judge", "unclassified")
    }
    total_cost = 0.0
    priced_traces = 0

    analysis_time = _now_utc()
    current_start = analysis_time - timedelta(hours=DRIFT_CURRENT_HOURS)
    baseline_end = analysis_time - timedelta(hours=DRIFT_BASELINE_LAG_HOURS)
    baseline_start = baseline_end - timedelta(days=DRIFT_BASELINE_DAYS)
    current_content_traces = 0
    baseline_content_traces = 0
    display_workloads: set[str] = set()
    has_undisplayable_workload = False
    explorer_trace_ids: list[str] = []

    # trace_id -> (provider, started_at) and provider model
    tp, ttime, tcluster, model_of, models_of, raw_provider_of = (
        {}, {}, {}, {}, defaultdict(set), {}
    )
    provider_trace_counts = Counter()
    content_bearing_predicate = cur.content_bearing_predicate("traces")
    for r in cur.execute(
        # ``cluster_select`` is selected from the two literals above; it never
        # contains request data or a database value.
        "SELECT trace_id, provider, request_model, started_at, cost_usd, "
        f"{cluster_select}, {tags_column} AS workload_tags, "  # nosec B608
        f"CASE WHEN {content_bearing_predicate} "  # nosec B608
        "THEN 1 ELSE 0 END AS content_bearing FROM traces"
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
        group = workload if workload in {"agent", "judge"} else "unclassified"
        if workload != "judge":
            explorer_trace_ids.append(r["trace_id"])
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
        if workload != "judge" and r["content_bearing"]:
            started_at = _dt(r["started_at"])
            if current_start <= started_at <= analysis_time:
                current_content_traces += 1
            elif baseline_start <= started_at < baseline_end:
                baseline_content_traces += 1
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

    has_drift_table = _table_exists(cur, "drift_signals")
    has_drift_run_table = _table_exists(cur, "drift_runs")
    drift_columns = cur.columns("drift_signals") if has_drift_table else set()

    # Group persisted judgments by evaluator identity before calculating any
    # score. Multiple identities require an explicit API/UI selection. Retention
    # may remove the last judgment while intentionally preserving its completed
    # run; keep that fingerprint selectable as an explicitly incomplete identity.
    identity_rows: list[tuple[dict, Mapping[str, Any]]] = []
    identity_by_id: dict[str, dict] = {}
    judgment_rows = (
        cur.execute("SELECT * FROM judgments")
        if _table_exists(cur, "judgments")
        else ()
    )
    for row in judgment_rows:
        identity = evaluator_identity(row)
        identity_by_id.setdefault(identity["id"], identity)
        identity_rows.append((identity, row))
    known_fingerprints = {
        identity["fingerprint"]
        for identity in identity_by_id.values()
        if identity["fingerprint"]
    }
    retained_fingerprints = set()
    if has_drift_run_table:
        retained_fingerprints.update(
            row["evaluator_fingerprint"]
            for row in cur.execute(
                """SELECT DISTINCT evaluator_fingerprint FROM drift_runs
                     WHERE evaluator_fingerprint IS NOT NULL
                       AND evaluator_fingerprint != ''"""
            )
        )
    if has_drift_table and "evaluator_fingerprint" in drift_columns:
        retained_fingerprints.update(
            row["evaluator_fingerprint"]
            for row in cur.execute(
                """SELECT DISTINCT evaluator_fingerprint FROM drift_signals
                     WHERE evaluator_fingerprint IS NOT NULL
                       AND evaluator_fingerprint != ''"""
            )
        )
    for fingerprint in sorted(retained_fingerprints - known_fingerprints):
        identity = evaluator_identity({"evaluator_fingerprint": fingerprint})
        identity_by_id[identity["id"]] = identity
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
    drift_signal_count = (
        cur.execute("SELECT COUNT(*) AS n FROM drift_signals").fetchone()["n"]
        if has_drift_table
        else 0
    )
    if has_drift_table and "evaluator_fingerprint" in drift_columns:
        unattributed_drift = cur.execute(
            """SELECT COUNT(*) AS n FROM drift_signals
                 WHERE evaluator_fingerprint IS NULL OR evaluator_fingerprint = ''"""
        ).fetchone()["n"]
    else:
        unattributed_drift = drift_signal_count
    drift_run = None
    if selected_fingerprint and has_drift_run_table:
        latest_run = cur.execute(
            """SELECT * FROM drift_runs
                 WHERE evaluator_fingerprint = ?
                 ORDER BY analysis_time DESC, completed_at DESC, run_id DESC
                 LIMIT 1""",
            (selected_fingerprint,),
        ).fetchone()
        if latest_run is not None:
            drift_run = {
                "id": latest_run["run_id"],
                "analysisTime": latest_run["analysis_time"],
                "completedAt": latest_run["completed_at"],
                "signalCount": latest_run["signal_count"],
            }
    if selected_id is None and (drift_signal_count or has_drift_run_table):
        drift_status = evaluation_status
    elif not selected_fingerprint or "evaluator_fingerprint" not in drift_columns:
        drift_status = "historical_unattributed" if drift_signal_count else "empty"
    elif drift_run is not None:
        drift_status = "selected"
    elif cur.execute(
        "SELECT COUNT(*) AS n FROM drift_signals WHERE evaluator_fingerprint = ?",
        (selected_fingerprint,),
    ).fetchone()["n"]:
        drift_status = "historical_without_run"
    else:
        drift_status = "empty"
    evaluation.update({
        "driftStatus": drift_status,
        "unattributedDriftSignals": unattributed_drift,
    })
    has_health_table = _table_exists(cur, "evaluator_health")
    if selected_fingerprint and has_health_table:
        health_columns = cur.columns("evaluator_health")
        for row in cur.execute(
            """SELECT * FROM evaluator_health
                 WHERE evaluator_fingerprint = ?
                 ORDER BY evaluated_at DESC LIMIT 30""",
            (selected_fingerprint,),
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
                "reasoning": redact(str(dimension.get("reasoning", ""))) or "",
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
    # Providers present in each cluster. Used ONLY to attribute a drift signal
    # to a provider when the attribution is factual (cluster is single-provider).
    # Drift is detected per (cluster, dimension) and may span providers; the
    # detector does not establish a causal provider, so we never guess one.
    cluster_providers: dict[str, set] = {}
    for trace_id, cluster_id in tcluster.items():
        if cluster_id:
            cluster_providers.setdefault(cluster_id, set()).add(tp[trace_id])

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
               FROM traces WHERE """ + provider_where,  # nosec B608
            provider_params,
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
            "avgLatency": _round_or_none(float(r["lat"] or 0) / 1000, 2) or 0.0,
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

    # ---- drift signals ----
    drift = []
    drift_rows = []
    if drift_run is not None and "run_id" in drift_columns:
        drift_rows = list(cur.execute(
            """SELECT * FROM drift_signals
                 WHERE run_id = ? AND evaluator_fingerprint = ?
                 ORDER BY signal_id""",
            (drift_run["id"], selected_fingerprint),
        ))
        if len(drift_rows) != drift_run["signalCount"]:
            drift_rows = []
            evaluation["driftStatus"] = "inconsistent_run"
    for s in drift_rows:
        s = dict(s)
        alias = s.get("cluster_id") or "unknown cluster"
        # Attribute a provider only when it is a fact, not an inference:
        #   1. demo alias mapping (cluster IS a provider bucket), or
        #   2. cluster_id is itself a provider key, or
        #   3. every trace in the cluster comes from one provider.
        # Otherwise attribute to the cluster itself — the detector's real unit.
        prov = _signal_provider(alias, set(keys), cluster_providers)
        drift.append({
            "id": s.get("signal_id") or "unknown-signal", "clusterId": alias,
            "clusterLabel": cluster_labels.get(alias, alias),
            "dimension": s.get("dimension") or "unknown",
            "direction": s.get("direction") or "change",
            "provider": prov or "",
            "providerLabel": (_label_for(prov, model_of.get(prov, "")) if prov
                              else f"cluster {cluster_labels.get(alias, alias)} (mixed providers)"),
            "statName": s.get("statistic_name") or "unknown",
            "stat": _round_or_none(
                s.get("statistic_value"),
                6,
            ),
            "p": s.get("p_value"), "pAdj": s.get("p_value_adjusted"),
            "cliffsDelta": _round_or_none(s.get("effect_size_cliffs_delta"), 3),
            "cohensD": _round_or_none(s.get("effect_size_cohens_d"), 2),
            "nCur": s.get("sample_size_current"),
            "nBase": s.get("sample_size_baseline"),
            "layers": _json_column(s, "contributing_layers", []),
            "exampleTraceIds": _json_column(s, "example_trace_ids", []),
            "action": s.get("recommended_action") or "Review the affected traces.",
            "detectedAt": s.get("detected_at"),
        })
    # Keep the largest effects under the cap for both regressions and
    # improvements. The database's signal-id ordering breaks equal-magnitude
    # ties deterministically.
    drift.sort(key=lambda x: (
        -abs(
            x["cliffsDelta"]
            if x["cliffsDelta"] is not None
            else x["cohensD"]
            if x["cohensD"] is not None
            else 0.0
        )
    ))
    total_drift_signals = len(drift)
    drift = drift[:MAX_DASHBOARD_DRIFT_SIGNALS]

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
        keys=keys,
        t0=t0,
        judg_by_trace=judg_by_trace,
        ttime=ttime,
        tp=tp,
        tcluster=tcluster,
        all_keys=all_keys,
        clusters=clusters,
        dims_present=dims_present,
        drift=drift,
        raw_provider_of=raw_provider_of,
        model_of=model_of,
    )

    samples, filtered_explorer_trace_ids = _trace_samples(
        cur,
        requested_trace_id=requested_trace_id,
        trace_judge_status=trace_judge_status,
        trace_offset=trace_offset,
        explorer_trace_ids=explorer_trace_ids,
        judgment_status_by_trace=judgment_status_by_trace,
        ttime=ttime,
        tcluster=tcluster,
        cluster_labels=cluster_labels,
        cluster_select=cluster_select,
        operation_select=operation_select,
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
        "driftSignals": _resource_limit(
            total_drift_signals,
            len(drift),
            MAX_DASHBOARD_DRIFT_SIGNALS,
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
        agent_runs=agent_runs,
        explorer_trace_ids=explorer_trace_ids,
        judgment_status_by_trace=judgment_status_by_trace,
        selected_evaluator_id=selected_id,
    )
    return {
        "meta": {
            "runStart": t0row["m"],
            "durationHours": max(1, round((tmax - t0).total_seconds() / 3600)),
            "totalTraces": total_traces,
            "totalAgentRuns": agent_runs["available"],
            "agentRunSources": agent_runs["sources"],
            "agentRunSourcesTruncated": agent_runs["sourcesTruncated"],
            "lastAgentCaptureAt": agent_runs["lastCapturedAt"],
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
        "driftSignals": drift,
        "driftRun": drift_run,
        "driftAnalysis": _drift_analysis(
            current=current_content_traces,
            baseline=baseline_content_traces,
            run_status=(
                evaluation["status"]
                if evaluation["status"] in {"selection_required", "invalid_selection"}
                else "completed_with_signals"
                if evaluation["driftStatus"] == "selected" and total_drift_signals
                else "completed_no_signals"
                if evaluation["driftStatus"] == "selected" and drift_run is not None
                else "no_completed_run"
            ),
        ),
        "dimensionOverall": dimensionOverall,
        "tsRows": series["latencyRows"],
        "passrate": series["passrate"],
        "clusterPassrate": series["clusterPassrate"],
        "haikuDim": series["dimensionPassrate"],
        "focusProvider": series["focusProvider"],
        "focusProviderLabel": series["focusProviderLabel"],
        "samples": samples,
        "providerDimension": providerDimension,
        "truncation": truncation,
    }


# --------------------------------------------------------------------------- #
#  FastAPI app
# --------------------------------------------------------------------------- #
def create_app(
    *,
    storage: str | os.PathLike[str] | None = None,
    operations_url: str | None = None,
    allowed_hosts: list[str] | None = None,
):
    import base64
    import secrets

    from fastapi import FastAPI, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    configured_storage = resolve_storage(storage)
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
    backend = "postgresql" if _is_postgres(configured_storage) else "sqlite"
    setup_token = secrets.token_urlsafe(32)
    app = FastAPI(title="Verdict Dashboard", version="0.1.0")
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
        return path in {"/", "/dashboard", "/api/config"} or path.startswith(
            (
                "/api/data", "/api/registry", "/api/runs", "/api/insights",
                "/api/evaluators", "/api/setup", "/api/monitor", "/api/clusters",
                "/api/control",
            )
        )

    @app.middleware("http")
    async def basic_auth(request, call_next):
        """Gate the dashboard + live data behind HTTP Basic Auth when configured.

        Set VERDICT_USER and VERDICT_PASS to require a login. If either is unset
        (local dev), the gate is disabled. The dashboard shells at / and
        /dashboard plus /api/data are protected; /api/health stays public.
        """
        user = os.environ.get("VERDICT_USER")
        pw = os.environ.get("VERDICT_PASS")
        if user and pw and request.method != "OPTIONS" and _is_gated(request.url.path):
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
        return {"operationsUrl": operations_url}

    from verdict.dashboard.setup_routes import SetupRoutes

    setup_routes = SetupRoutes(configured_storage, setup_token)
    setup_routes.register(app)
    _setup_authorized = setup_routes.authorized

    from verdict.dashboard.control_routes import ControlRoutes
    from verdict.dashboard.lab_routes import register_lab_routes
    from verdict.dashboard.monitor_routes import MonitorRoutes

    register_lab_routes(app, setup_routes)
    monitor_routes = MonitorRoutes(setup_routes)
    monitor_routes.register(app)
    ControlRoutes(configured_storage, setup_routes, monitor_routes).register(app)

    def data(
        request: Request,
        evaluator: str | None = None,
        trace_offset: int = Query(default=0, ge=0),
        trace_judge_status: str = "all",
        trace_id: str | None = None,
    ):
        if not _is_postgres(configured_storage) and not _sqlite_path(
            configured_storage
        ).exists():
            # Do NOT leak the absolute DB path in the HTTP body — log it
            # server-side and return a generic 503 so the dashboard degrades
            # gracefully instead of crashing or exposing the filesystem layout.
            _log.warning("dashboard data unavailable: SQLite database not found")
            return JSONResponse({"error": "data unavailable"}, status_code=503)
        try:
            return build_bundle(
                configured_storage,
                evaluator_id=evaluator,
                registry_tenant=getattr(
                    request.state,
                    "verdict_registry_tenant",
                    None,
                ),
                trace_offset=trace_offset,
                trace_judge_status=trace_judge_status,
                trace_id=trace_id,
            )
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
        tenant: str | None = None,
        version: str | None = None,
        assignment_limit: int = 50,
        assignment_offset: int = 0,
    ):
        authorized_tenant = getattr(
            request.state,
            "verdict_registry_tenant",
            None,
        ) or tenant or "__verdict_local__"
        try:
            tenant_bytes = authorized_tenant.encode("utf-8")
        except (AttributeError, UnicodeError):
            return JSONResponse({"error": "invalid tenant"}, status_code=400)
        if not tenant_bytes or len(tenant_bytes) > 128:
            return JSONResponse({"error": "invalid tenant"}, status_code=400)
        try:
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
        tenant: str | None = None,
        limit: int = Query(default=30, ge=1, le=100),
        run_id: str | None = None,
        evaluator_fingerprint: str | None = None,
    ):
        authorized_tenant = getattr(
            request.state, "verdict_registry_tenant", None,
        ) or tenant or "__verdict_local__"
        try:
            requested_run_ids = request.query_params.getlist("run_ids")
            selected_run_ids = tuple(requested_run_ids) if requested_run_ids else None
            return build_agent_runs_bundle(
                configured_storage,
                tenant=authorized_tenant,
                limit=limit,
                run_id=run_id,
                run_ids=selected_run_ids,
                evaluator_fingerprint=evaluator_fingerprint,
            )
        except ValueError:
            return JSONResponse({"error": "invalid runs request"}, status_code=400)
        except (DashboardBundleLimitError, OSError, sqlite3.DatabaseError):
            _log.exception("failed to build %s agent runs bundle", backend)
            return JSONResponse({"error": "agent runs unavailable"}, status_code=503)

    agent_runs.__annotations__["request"] = Request
    app.get("/api/runs")(agent_runs)

    def agent_run_detail(
        run_id: str,
        request,
        tenant: str | None = None,
        event_limit: int = Query(default=100, ge=1, le=200),
        event_offset: int = Query(default=0, ge=0),
        turn_limit: int = Query(default=20, ge=1, le=50),
        turn_offset: int = Query(default=0, ge=0),
        event_id: str | None = None,
    ):
        authorized_tenant = getattr(
            request.state, "verdict_registry_tenant", None,
        ) or tenant or "__verdict_local__"
        try:
            return build_agent_run_detail(
                configured_storage,
                tenant=authorized_tenant,
                run_id=run_id,
                event_limit=event_limit,
                event_offset=event_offset,
                turn_limit=turn_limit,
                turn_offset=turn_offset,
                event_id=event_id,
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

    def agent_insights(request, tenant: str | None = None):
        authorized_tenant = getattr(
            request.state, "verdict_registry_tenant", None,
        ) or tenant or "__verdict_local__"
        try:
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

    def run_agent_insights(request, tenant: str | None = None):
        if not _setup_authorized(request):
            return JSONResponse({"error": "analysis authorization required"}, status_code=403)
        authorized_tenant = getattr(
            request.state, "verdict_registry_tenant", None,
        ) or tenant or "__verdict_local__"
        try:
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
    parser.add_argument("--open-browser", action="store_true", help=argparse.SUPPRESS)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--storage", help="SQLite URL/path or PostgreSQL DSN")
    source.add_argument("--db", help="legacy SQLite path")
    args = parser.parse_args(argv)
    configured_storage = resolve_storage(args.storage or args.db)
    backend = "postgresql" if _is_postgres(configured_storage) else "sqlite"
    print(f"Verdict dashboard → http://{args.host}:{args.port}")
    print(f"Reading storage   → {backend}")

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
        create_app(storage=configured_storage),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
