"""Installable read-only Verdict dashboard for SQLite and PostgreSQL stores.

The application factory can be mounted inside another ASGI application. Every
request reads one database snapshot and never migrates or mutates the store.

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
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

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


def _evaluator_identity(row: Mapping[str, Any]) -> dict:
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


class _Result:
    """Normalize driver rows without copying query or aggregation logic."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        values = dict(row)
        return {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in values.items()
        }

    def fetchone(self) -> dict[str, Any] | None:
        return self._row(self._cursor.fetchone())

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for row in self._cursor:
            normalized = self._row(row)
            if normalized is not None:
                yield normalized


class _QuerySession(Protocol):
    def execute(self, query: str, params: tuple[Any, ...] = ()) -> _Result: ...

    def table_exists(self, table: str) -> bool: ...

    def columns(self, table: str) -> set[str]: ...


class _SQLiteSession:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> _Result:
        return _Result(self._connection.execute(query, params))

    def table_exists(self, table: str) -> bool:
        return bool(self.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone())

    def columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.execute(f"PRAGMA table_info({table})")}


class _PostgresSession:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> _Result:
        return _Result(self._connection.execute(query.replace("?", "%s"), params))

    def table_exists(self, table: str) -> bool:
        return bool(self.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchone())

    def columns(self, table: str) -> set[str]:
        return {
            row["column_name"]
            for row in self.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ?",
                (table,),
            )
        }


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
    return storage.startswith(("postgres://", "postgresql://")) or (
        "=" in storage and "://" not in storage
    )


# --------------------------------------------------------------------------- #
#  Aggregation — produces the exact shape the dashboard consumes
# --------------------------------------------------------------------------- #
def build_bundle(
    storage: str | os.PathLike[str],
    *,
    evaluator_id: str | None = None,
) -> dict:
    configured = str(storage)
    if _is_postgres(configured):
        return _build_from_postgres(configured, evaluator_id=evaluator_id)
    path = _sqlite_path(configured)
    try:
        return _build_from_connection(
            sqlite3.connect(f"file:{path}?mode=ro", uri=True),
            evaluator_id=evaluator_id,
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
        return _build_from_connection(con, evaluator_id=evaluator_id)


def _build_from_connection(
    con: sqlite3.Connection,
    *,
    evaluator_id: str | None = None,
) -> dict:
    con.row_factory = sqlite3.Row
    try:
        # Keep run metadata and its signals (and the rest of the bundle) on one
        # SQLite read snapshot while a pipeline process may replace a run.
        con.execute("BEGIN")
        bundle = _redacted_bundle(
            _SQLiteSession(con),
            evaluator_id=evaluator_id,
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
            )


def _redacted_bundle(
    session: _QuerySession,
    *,
    evaluator_id: str | None,
) -> dict:
    bundle = redact_structure(_build(session, evaluator_id=evaluator_id))
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
    from verdict_eval.stable_clustering import assess_cluster_health

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


def _empty_bundle() -> dict:
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
            "totalJudged": 0,
            "totalCost": None,
            "totalCostStatus": "unavailable",
            "costBreakdown": {
                name: _cost_summary(0, 0, 0.0)
                for name in ("agent", "judge", "unclassified")
            },
            "regressionHour": 0,
            "providers": 0,
            "clusters": 0,
        },
        "providers": [],
        "clusters": [],
        "driftSignals": [],
        "driftRun": None,
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


def _build(cur, *, evaluator_id: str | None = None) -> dict:
    if not _table_exists(cur, "traces"):
        return _empty_bundle()
    t0row = cur.execute("SELECT MIN(started_at) m, MAX(started_at) x FROM traces").fetchone()
    if not t0row or not t0row["m"]:
        return _empty_bundle()
    t0, tmax = _dt(t0row["m"]), _dt(t0row["x"])
    trace_columns = cur.columns("traces")
    cluster_select = (
        "cluster_id" if "cluster_id" in trace_columns else "NULL AS cluster_id"
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

    # trace_id -> (provider, started_at) and provider model
    tp, ttime, tcluster, model_of, models_of, raw_provider_of = (
        {}, {}, {}, {}, defaultdict(set), {}
    )
    provider_trace_counts = Counter()
    for r in cur.execute(
        # ``cluster_select`` is selected from the two literals above; it never
        # contains request data or a database value.
        "SELECT trace_id, provider, request_model, started_at, cost_usd, "
        f"{cluster_select}, {tags_column} AS workload_tags FROM traces"  # nosec B608
    ):
        provider_key = _provider_key(r["provider"])
        tp[r["trace_id"]] = provider_key
        ttime[r["trace_id"]] = r["started_at"]
        tcluster[r["trace_id"]] = r["cluster_id"]
        models_of[provider_key].add(r["request_model"])
        raw_provider_of.setdefault(provider_key, r["provider"])
        provider_trace_counts[provider_key] += 1
        tags = _json_value(r["workload_tags"], {})
        workload = tags.get("verdict.workload") if isinstance(tags, dict) else None
        group = workload if workload in {"agent", "judge"} else "unclassified"
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

    # Group persisted judgments by evaluator identity before calculating any
    # score. Multiple identities require an explicit API/UI selection.
    identity_rows: list[tuple[dict, Mapping[str, Any]]] = []
    identity_by_id: dict[str, dict] = {}
    judgment_rows = (
        cur.execute("SELECT * FROM judgments")
        if _table_exists(cur, "judgments")
        else ()
    )
    for row in judgment_rows:
        identity = _evaluator_identity(row)
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
    has_drift_table = _table_exists(cur, "drift_signals")
    has_drift_run_table = _table_exists(cur, "drift_runs")
    drift_columns = cur.columns("drift_signals") if has_drift_table else set()
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
    for trace_id, row in latest_row_by_trace.items():
        status = (_row_value(row, "status", "completed") or "completed").lower()
        if status == "error":
            score_coverage["error"] += 1
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
    if "cluster_id" in trace_columns:
        for r in cur.execute(
            """SELECT DISTINCT cluster_id, provider
                 FROM traces
                WHERE cluster_id IS NOT NULL AND cluster_id <> ''"""
        ):
            cluster_providers.setdefault(r["cluster_id"], set()).add(
                _provider_key(r["provider"])
            )

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
    from verdict_eval.stable_clustering import UNCLUSTERED_ID

    clusters = (
        [dict(cluster_id=r["cluster_id"], n=r["n"]) for r in cur.execute(
            """SELECT cluster_id, COUNT(*) n FROM traces
               WHERE cluster_id IS NOT NULL AND cluster_id <> ''
                 AND cluster_id <> ?
               GROUP BY cluster_id ORDER BY n DESC, cluster_id
               LIMIT ?""",
            (UNCLUSTERED_ID, MAX_DASHBOARD_CLUSTERS),
        )]
        if "cluster_id" in trace_columns
        else []
    )
    cluster_ids = (
        [r["cluster_id"] for r in cur.execute("SELECT cluster_id FROM traces")]
        if "cluster_id" in trace_columns
        else []
    )
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
            "dimension": s.get("dimension") or "unknown",
            "direction": s.get("direction") or "change",
            "provider": prov or "",
            "providerLabel": (_label_for(prov, model_of.get(prov, "")) if prov
                              else f"cluster {alias} (mixed providers)"),
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

    # ---- time series: 30-min bins ----
    BIN = 30 * 60
    bins = defaultdict(lambda: defaultdict(lambda: {"n": 0, "err": 0, "lat": []}))
    for r in cur.execute("SELECT provider, started_at, latency_ms, error FROM traces"):
        provider_key = _provider_key(r["provider"])
        if provider_key not in keys:
            continue
        b = int((_dt(r["started_at"]) - t0).total_seconds() // BIN)
        cell = bins[provider_key][b]
        cell["n"] += 1
        if r["error"]:
            cell["err"] += 1
        latency = _round_or_none(r["latency_ms"], 6)
        if latency is not None:
            cell["lat"].append(latency)
    all_observed_bins = sorted({b for p in bins for b in bins[p]})
    observed_bins = all_observed_bins[-MAX_SERIES_POINTS:]
    tsRows = []
    for b in observed_bins:
        row = {"hour": round(b * 0.5, 1)}
        for p in keys:
            cell = bins[p].get(b)
            if cell and cell["n"]:
                row[f"{p}_lat"] = round(sum(cell["lat"]) / len(cell["lat"]) / 1000, 2) if cell["lat"] else None
                row[f"{p}_err"] = round(100 * cell["err"] / cell["n"], 1)
                row[f"{p}_n"] = cell["n"]
            else:
                row[f"{p}_lat"] = None
                row[f"{p}_err"] = None
                row[f"{p}_n"] = 0
        tsRows.append(row)

    # ---- hourly pass rate by provider and cluster + per-dim drift focus ----
    HB = 60 * 60
    pr = defaultdict(lambda: defaultdict(Counter))
    cluster_pr = defaultdict(lambda: defaultdict(Counter))
    dim_hr = defaultdict(lambda: defaultdict(Counter))
    drift_focus = drift[0].get("provider") if drift else None
    focus = drift_focus if drift_focus in keys else (keys[0] if keys else None)
    focus_provider_label = (
        _label_for(
            str(raw_provider_of.get(focus) or ""),
            model_of.get(focus, ""),
        )
        if focus is not None
        else None
    )
    for tid, j in judg_by_trace.items():
        st = ttime.get(tid)
        if not st:
            continue
        prov = tp.get(tid)
        cluster = tcluster.get(tid)
        hb = int((_dt(st) - t0).total_seconds() // HB)
        for d in j["dims"]:
            verdict = d["verdict"]
            if prov in keys:
                pr[prov][hb][verdict] += 1
            if cluster and len(all_keys) == 1:
                cluster_pr[cluster][hb][verdict] += 1
            if prov == focus:
                dim_hr[d["name"]][hb][verdict] += 1
    all_observed_hours = sorted({h for p in pr for h in pr[p]})
    observed_hours = all_observed_hours[-MAX_SERIES_POINTS:]
    passrate = []
    for h in observed_hours:
        row = {"hour": h}
        for p in keys:
            cell = pr[p].get(h)
            row[p] = _score_rate(cell) if cell else None
        passrate.append(row)
    clusterPassrate = []
    if len(all_keys) == 1:
        cluster_keys = [cluster["cluster_id"] for cluster in clusters]
        for h in observed_hours:
            row = {"hour": h}
            for cluster in cluster_keys:
                cell = cluster_pr[cluster].get(h)
                row[cluster] = _score_rate(cell) if cell else None
            clusterPassrate.append(row)
    haikuDim = []
    for h in observed_hours:
        row = {"hour": h}
        for d in dims_present:
            cell = dim_hr[d].get(h)
            row[d] = _score_rate(cell) if cell else None
        haikuDim.append(row)

    # ---- sample traces for the explorer ----
    rows = [dict(r) for r in cur.execute(
        # ``cluster_select`` is the same closed-set schema compatibility
        # expression used above, not user-controlled SQL.
        "SELECT trace_id, provider, request_model, "
        f"{cluster_select}, input_tokens, output_tokens, "  # nosec B608
        "latency_ms, cost_usd, finish_reason, error, started_at, "
        "prompt_redacted, response_redacted FROM traces "
        "ORDER BY started_at"
    )]
    errs = [r for r in rows if r["error"]]
    late = [
        r for r in rows
        if _provider_key(r["provider"]) == focus
        and (_dt(r["started_at"]) - t0).total_seconds() > 4 * 3600
    ]
    pick, seen = [], set()
    for r in errs[:4] + late[:6]:
        if r["trace_id"] not in seen:
            pick.append(r)
            seen.add(r["trace_id"])
    for r in rows:
        if len(pick) >= MAX_TRACE_SAMPLES:
            break
        if r["trace_id"] not in seen:
            pick.append(r)
            seen.add(r["trace_id"])
    samples = []
    for r in pick:
        s = dict(r)
        for content_field in ("prompt_redacted", "response_redacted", "error"):
            s[content_field] = redact(s.get(content_field))
        s["providerKey"] = _provider_key(r["provider"])
        s["hour"] = round((_dt(r["started_at"]) - t0).total_seconds() / 3600, 2)
        latency = _round_or_none(r["latency_ms"], 0)
        s["latency_ms"] = int(latency) if latency is not None else None
        if s.get("response_redacted"):
            s["response_redacted"] = s["response_redacted"][:600]
        j = judg_by_trace.get(r["trace_id"])
        if j:
            s["judgment"] = j
        samples.append(s)

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
            len(all_observed_bins),
            len(observed_bins),
            MAX_SERIES_POINTS,
        ),
        "hourlyPoints": _resource_limit(
            len(all_observed_hours),
            len(observed_hours),
            MAX_SERIES_POINTS,
        ),
        "traceSamples": _resource_limit(
            len(rows),
            len(samples),
            MAX_TRACE_SAMPLES,
        ),
    })
    return {
        "meta": {
            "runStart": t0row["m"],
            "durationHours": max(1, round((tmax - t0).total_seconds() / 3600)),
            "totalTraces": total_traces,
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
            "regressionHour": int(os.environ.get("VERDICT_REGRESSION_HOUR", "4")),
            "providers": len(all_keys),
            "clusters": cluster_health["nClusters"],
        },
        "providers": providers,
        "clusters": clusters,
        "clusterHealth": cluster_health,
        "evaluation": evaluation,
        "evaluatorHealth": evaluator_health,
        "scoreCoverage": dict(score_coverage),
        "driftSignals": drift,
        "driftRun": drift_run,
        "dimensionOverall": dimensionOverall,
        "tsRows": tsRows,
        "passrate": passrate,
        "clusterPassrate": clusterPassrate,
        "haikuDim": haikuDim,
        "focusProvider": focus,
        "focusProviderLabel": focus_provider_label,
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
):
    import base64
    import secrets

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles

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
    app = FastAPI(title="Verdict Dashboard", version="0.1.0")
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
        return path in {"/", "/dashboard", "/api/config"} or path.startswith("/api/data")

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

    @app.get("/api/data")
    def data(evaluator: str | None = None):
        if not _is_postgres(configured_storage) and not _sqlite_path(
            configured_storage
        ).exists():
            # Do NOT leak the absolute DB path in the HTTP body — log it
            # server-side and return a generic 503 so the dashboard degrades
            # gracefully instead of crashing or exposing the filesystem layout.
            _log.warning("dashboard data unavailable: SQLite database not found")
            return JSONResponse({"error": "data unavailable"}, status_code=503)
        try:
            return build_bundle(configured_storage, evaluator_id=evaluator)
        except DashboardBundleLimitError:
            _log.exception("dashboard bundle exceeded its safety budget")
            return JSONResponse(
                {"error": "dashboard bundle limit exceeded"},
                status_code=503,
            )
        except Exception:  # pragma: no cover - defensive: corrupt/locked DB
            _log.exception("failed to build %s dashboard data bundle", backend)
            return JSONResponse({"error": "data unavailable"}, status_code=503)

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
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--storage", help="SQLite URL/path or PostgreSQL DSN")
    source.add_argument("--db", help="legacy SQLite path")
    args = parser.parse_args(argv)
    configured_storage = resolve_storage(args.storage or args.db)
    backend = "postgresql" if _is_postgres(configured_storage) else "sqlite"
    print(f"Verdict dashboard → http://{args.host}:{args.port}")
    print(f"Reading storage   → {backend}")

    # Loud warning if exposed beyond localhost without auth: /dashboard and the
    # full /api/data bundle would otherwise be world-readable.
    if args.host not in ("127.0.0.1", "localhost", "::1") and not (
        os.environ.get("VERDICT_USER") and os.environ.get("VERDICT_PASS")
    ):
        print("WARNING: binding to a non-localhost host without VERDICT_USER/VERDICT_PASS — "
              "the dashboard and /api/data will be publicly accessible. Set both to require auth.")

    import uvicorn
    uvicorn.run(
        create_app(storage=configured_storage),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
