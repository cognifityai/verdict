"""Bounded, read-only Trace Explorer query model.

This module owns filtering, keyset pagination, and detail projection.  Storage
connections and HTTP behavior stay in :mod:`verdict.dashboard.app` so this
contract is testable across SQLite and PostgreSQL without a second service.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any, Protocol

from verdict.dashboard.presentation import (
    evaluator_identity,
    json_column,
    judgment_presentation,
    provider_key,
)

DEFAULT_TRACE_PAGE_SIZE = 50
MAX_TRACE_PAGE_SIZE = 100
MAX_TRACE_QUERY_LENGTH = 200
MAX_TRACE_PROVIDER_LENGTH = 256
MAX_TRACE_CURSOR_LENGTH = 4096
MAX_TRACE_ID_LENGTH = 2048
TRACE_PROMPT_PREVIEW_LENGTH = 240
TRACE_CONTENT_LENGTH = 100_000
TRACE_ERROR_LENGTH = 10_000
TRACE_RAW_MESSAGES_LENGTH = 100_000
TRACE_TAGS_LENGTH = 20_000
TRACE_JUDGMENT_REASONING_LENGTH = 10_000
TRACE_METADATA_LENGTH = 2048
TRACE_JUDGMENT_IDENTITY_LENGTH = 20_000
TRACE_JUDGMENT_DIMENSIONS_LENGTH = 250_000
MAX_TRACE_JUDGMENT_ROWS = 500
_CURSOR_VERSION = 1
_CAPTURE_VALUES = {"all", "captured", "metadata"}
_REGISTRY_TABLES = (
    "cluster_registries",
    "cluster_registry_versions",
    "cluster_registry_clusters",
    "active_cluster_registry",
    "trace_cluster_assignments",
    "cluster_identities",
    "cluster_registry_events",
)


class TraceQueryError(ValueError):
    """A public Trace Explorer query is malformed or inconsistent."""


class TraceNotFoundError(LookupError):
    """The requested application trace does not exist or is ineligible."""


class QueryResult(Protocol):
    def fetchone(self) -> Mapping[str, Any] | None: ...

    def __iter__(self) -> Iterator[Mapping[str, Any]]: ...


class TraceQuerySession(Protocol):
    def execute(self, query: str, params: tuple[Any, ...] = ()) -> QueryResult: ...

    def table_exists(self, table: str) -> bool: ...

    def columns(self, table: str) -> set[str]: ...

    def json_text_expression(self, column: str, key: str) -> str: ...

    def cast_text_expression(self, expression: str) -> str: ...


def _validated_text(
    value: str | None,
    *,
    field: str,
    maximum: int,
    strip: bool = True,
) -> str:
    normalized = (value or "").strip() if strip else (value or "")
    if len(normalized.encode("utf-8")) > maximum:
        raise TraceQueryError(f"invalid {field}")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise TraceQueryError(f"invalid {field}")
    return normalized


def _validated_filters(
    *,
    query: str | None,
    provider: str | None,
    capture: str,
) -> dict[str, str]:
    normalized_capture = (capture or "all").lower()
    if normalized_capture not in _CAPTURE_VALUES:
        raise TraceQueryError("invalid capture filter")
    return {
        "q": _validated_text(
            query,
            field="search query",
            maximum=MAX_TRACE_QUERY_LENGTH,
        ),
        "provider": _validated_text(
            provider,
            field="provider filter",
            maximum=MAX_TRACE_PROVIDER_LENGTH,
        ),
        "capture": normalized_capture,
    }


def _filter_scope(filters: Mapping[str, str]) -> str:
    encoded = json.dumps(filters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _encode_cursor(*, started_at: str, trace_id: str, scope: str) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "at": started_at,
            "id": trace_id,
            "scope": scope,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, *, scope: str) -> tuple[str, str] | None:
    if not cursor:
        return None
    if len(cursor) > MAX_TRACE_CURSOR_LENGTH:
        raise TraceQueryError("invalid cursor")
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TraceQueryError("invalid cursor") from exc
    if not isinstance(payload, dict) or set(payload) != {"v", "at", "id", "scope"}:
        raise TraceQueryError("invalid cursor")
    started_at = payload.get("at")
    trace_id = payload.get("id")
    if (
        payload.get("v") != _CURSOR_VERSION
        or payload.get("scope") != scope
        or not isinstance(started_at, str)
        or not isinstance(trace_id, str)
        or not trace_id
        or len(trace_id.encode("utf-8")) > MAX_TRACE_ID_LENGTH
    ):
        raise TraceQueryError("invalid cursor")
    try:
        datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TraceQueryError("invalid cursor") from exc
    return started_at, trace_id


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _column(columns: set[str], name: str, *, alias: str = "t") -> str:
    return f"{alias}.{name}" if name in columns else "NULL"


def _tags_column(columns: set[str]) -> str:
    if "tags_json" in columns:
        return "t.tags_json"
    if "tags" in columns:
        return "t.tags"
    return "NULL"


def _raw_messages_column(columns: set[str]) -> str:
    if "raw_messages_json" in columns:
        return "t.raw_messages_json"
    if "raw_messages" in columns:
        return "t.raw_messages"
    return "NULL"


def _registry_projection(
    session: TraceQuerySession,
    registry_tenant: str | None,
    trace_columns: set[str],
) -> tuple[str, str, str, tuple[Any, ...]]:
    legacy_cluster = _column(trace_columns, "cluster_id")
    if registry_tenant is None or any(
        not session.table_exists(table) for table in _REGISTRY_TABLES
    ):
        return "traces t", legacy_cluster, legacy_cluster, ()
    pointer = session.execute(
        "SELECT version_id FROM active_cluster_registry WHERE tenant_id=?",
        (registry_tenant,),
    ).fetchone()
    if pointer is None or pointer.get("version_id") is None:
        return "traces t", legacy_cluster, legacy_cluster, ()
    version_id = pointer["version_id"]
    from_sql = (
        "traces t LEFT JOIN trace_cluster_assignments a ON a.tenant_id=? "
        "AND a.version_id=? AND a.trace_id=t.trace_id AND a.status='assigned' "
        "AND (t.tenant_id=a.tenant_id OR (?='__verdict_local__' "
        "AND a.tenant_id='__verdict_local__' AND t.tenant_id IS NULL)) "
        "LEFT JOIN cluster_registry_clusters c ON c.tenant_id=a.tenant_id "
        "AND c.version_id=a.version_id AND c.cluster_id=a.cluster_id "
        "AND c.kind=a.cluster_kind "
        "LEFT JOIN cluster_identities i ON i.tenant_id=c.tenant_id "
        "AND i.cluster_id=c.cluster_id AND i.kind=c.kind"
    )
    return from_sql, "c.cluster_id", "i.display_name", (
        registry_tenant,
        version_id,
        registry_tenant,
    )


def _where_clause(
    session: TraceQuerySession,
    *,
    trace_columns: set[str],
    cluster_expression: str,
    cluster_label_expression: str,
    filters: Mapping[str, str],
    cursor: tuple[str, str] | None,
) -> tuple[str, tuple[Any, ...]]:
    tags = _tags_column(trace_columns)
    workload = (
        session.json_text_expression(tags, "verdict.workload")
        if tags != "NULL"
        else "NULL"
    )
    clauses = [f"COALESCE({workload},'')<>'judge'"]
    params: list[Any] = []
    if filters["provider"]:
        clauses.append("COALESCE(t.provider,'')=?")
        params.append(filters["provider"])
    if filters["capture"] == "captured":
        clauses.append("(t.prompt_redacted IS NOT NULL OR t.response_redacted IS NOT NULL)")
    elif filters["capture"] == "metadata":
        clauses.append("(t.prompt_redacted IS NULL AND t.response_redacted IS NULL)")
    if filters["q"]:
        searchable = (
            "t.trace_id",
            "t.prompt_redacted",
            "t.response_redacted",
            "t.provider",
            "t.request_model",
            _column(trace_columns, "response_model"),
            cluster_expression,
            cluster_label_expression,
            session.cast_text_expression(tags),
        )
        clauses.append(
            "(" + " OR ".join(
                f"LOWER(COALESCE({expression},'')) LIKE ? ESCAPE '\\'"
                for expression in searchable
            ) + ")"
        )
        pattern = f"%{_escape_like(filters['q'].lower())}%"
        params.extend(pattern for _ in searchable)
    if cursor is not None:
        clauses.append("(t.started_at<? OR (t.started_at=? AND t.trace_id<?))")
        params.extend((cursor[0], cursor[0], cursor[1]))
    return " AND ".join(clauses), tuple(params)


def _projected_column(
    columns: set[str],
    name: str,
    *,
    maximum: int,
) -> str:
    if name not in columns:
        return f"NULL AS {name}"
    return f"SUBSTR({name},1,{maximum + 1}) AS {name}"


def _projected_json_column(
    session: TraceQuerySession,
    columns: set[str],
    name: str,
    *,
    maximum: int,
) -> tuple[str, str]:
    physical = f"{name}_json" if f"{name}_json" in columns else name
    if physical not in columns:
        return f"NULL AS {name}", name
    expression = session.cast_text_expression(physical)
    return f"SUBSTR({expression},1,{maximum + 1}) AS {physical}", physical


def _latest_judgments(
    session: TraceQuerySession,
    trace_ids: list[str],
    evaluator_id: str | None,
    *,
    reasoning_limit: int,
) -> tuple[dict[str, tuple[dict[str, Any], bool]], bool]:
    if not trace_ids or not evaluator_id or not session.table_exists("judgments"):
        return {}, False
    columns = session.columns("judgments")
    json_projections: list[str] = []
    json_physical_names: list[str] = []
    for name in ("judge_models", "evaluator_config", "expected_dimensions"):
        projection, physical_name = _projected_json_column(
            session,
            columns,
            name,
            maximum=TRACE_JUDGMENT_IDENTITY_LENGTH,
        )
        json_projections.append(projection)
        json_physical_names.append(physical_name)
    scalar_projections = [
        _projected_column(
            columns,
            name,
            maximum=TRACE_METADATA_LENGTH,
        )
        for name in (
            "rubric_name",
            "rubric_version",
            "evaluator_provider",
            "evaluator_fingerprint",
            "status",
        )
    ]
    placeholders = ",".join("?" for _ in trace_ids)
    select = ",".join([
        "judgment_id",
        "trace_id",
        "created_at",
        *scalar_projections,
        *json_projections,
    ])
    rows = list(session.execute(
        f"SELECT {select} FROM judgments "  # nosec B608
        f"WHERE trace_id IN ({placeholders}) "
        "ORDER BY created_at DESC,judgment_id DESC LIMIT ?",
        (*trace_ids, MAX_TRACE_JUDGMENT_ROWS + 1),
    ))
    rows_truncated = len(rows) > MAX_TRACE_JUDGMENT_ROWS
    rows = rows[:MAX_TRACE_JUDGMENT_ROWS]
    identity_truncated = any(
        isinstance(row.get(name), str)
        and len(row[name]) > TRACE_JUDGMENT_IDENTITY_LENGTH
        for row in rows
        for name in json_physical_names
    )
    latest: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if evaluator_identity(row)["id"] != evaluator_id:
            continue
        trace_id = str(row["trace_id"])
        current = latest.get(trace_id)
        key = (row.get("created_at") or "", row.get("judgment_id") or "")
        current_key = (
            (current.get("created_at") or "", current.get("judgment_id") or "")
            if current is not None
            else None
        )
        if current_key is None or key > current_key:
            latest[trace_id] = row
    judgment_ids = [str(row["judgment_id"]) for row in latest.values()]
    dimensions_by_judgment: dict[str, object] = {}
    dimensions_truncated = False
    if judgment_ids:
        dimensions_projection, dimensions_name = _projected_json_column(
            session,
            columns,
            "dimensions",
            maximum=TRACE_JUDGMENT_DIMENSIONS_LENGTH,
        )
        judgment_placeholders = ",".join("?" for _ in judgment_ids)
        dimension_rows = session.execute(
            f"SELECT judgment_id,{dimensions_projection} FROM judgments "  # nosec B608
            f"WHERE judgment_id IN ({judgment_placeholders})",
            tuple(judgment_ids),
        )
        for row in dimension_rows:
            value = row.get(dimensions_name)
            if (
                isinstance(value, str)
                and len(value) > TRACE_JUDGMENT_DIMENSIONS_LENGTH
            ):
                dimensions_truncated = True
                continue
            dimensions_by_judgment[str(row["judgment_id"])] = value
    result: dict[str, tuple[dict[str, Any], bool]] = {}
    for trace_id, row in latest.items():
        row_with_dimensions = dict(row)
        dimensions_name = (
            "dimensions_json" if "dimensions_json" in columns else "dimensions"
        )
        row_with_dimensions[dimensions_name] = dimensions_by_judgment.get(
            str(row["judgment_id"])
        )
        presentation = judgment_presentation(
            row_with_dimensions,
            reasoning_limit=reasoning_limit,
        )
        if presentation is None:
            continue
        dimensions = json_column(row_with_dimensions, "dimensions", [])
        reasoning_truncated = isinstance(dimensions, list) and any(
            isinstance(dimension, dict)
            and len(str(dimension.get("reasoning", ""))) > reasoning_limit
            for dimension in dimensions
        )
        result[trace_id] = (presentation, reasoning_truncated)
    return result, rows_truncated or identity_truncated or dimensions_truncated


def _rounded_latency(value: object) -> int | None:
    try:
        return round(float(str(value))) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def read_trace_page(
    session: TraceQuerySession,
    *,
    limit: int = DEFAULT_TRACE_PAGE_SIZE,
    cursor: str | None = None,
    query: str | None = None,
    provider: str | None = None,
    capture: str = "all",
    evaluator_id: str | None = None,
    registry_tenant: str | None = None,
) -> dict[str, Any]:
    """Read one deterministic page of application-trace previews."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_TRACE_PAGE_SIZE:
        raise TraceQueryError("invalid page size")
    filters = _validated_filters(query=query, provider=provider, capture=capture)
    normalized_registry_tenant = _validated_text(
        registry_tenant,
        field="registry tenant",
        maximum=128,
        strip=False,
    ) or None
    normalized_evaluator_id = _validated_text(
        evaluator_id,
        field="evaluator",
        maximum=128,
        strip=False,
    ) or None
    scope = _filter_scope({
        **filters,
        "registryTenant": normalized_registry_tenant or "",
    })
    decoded_cursor = _decode_cursor(cursor, scope=scope)
    if not session.table_exists("traces"):
        return {
            "items": [],
            "total": 0,
            "limit": limit,
            "nextCursor": None,
            "judgmentsTruncated": False,
        }
    columns = session.columns("traces")
    from_sql, cluster, cluster_label, from_params = _registry_projection(
        session,
        normalized_registry_tenant,
        columns,
    )
    count_where, count_params = _where_clause(
        session,
        trace_columns=columns,
        cluster_expression=cluster,
        cluster_label_expression=cluster_label,
        filters=filters,
        cursor=None,
    )
    page_where, page_params = _where_clause(
        session,
        trace_columns=columns,
        cluster_expression=cluster,
        cluster_label_expression=cluster_label,
        filters=filters,
        cursor=decoded_cursor,
    )
    total_row = session.execute(
        f"SELECT COUNT(*) AS n FROM {from_sql} WHERE {count_where}",  # nosec B608
        from_params + count_params,
    ).fetchone()
    select = (
        "t.trace_id,t.started_at,SUBSTR(t.provider,1,256) AS provider,"
        "SUBSTR(t.request_model,1,256) AS request_model,"
        f"SUBSTR({cluster},1,256) AS cluster_id,"
        f"SUBSTR({cluster_label},1,256) AS cluster_label,"
        "t.input_tokens,t.output_tokens,t.latency_ms,t.cost_usd,"
        "SUBSTR(t.finish_reason,1,256) AS finish_reason,"
        "CASE WHEN t.error IS NOT NULL AND t.error<>'' THEN 1 ELSE 0 END AS has_error,"
        f"SUBSTR(t.prompt_redacted,1,{TRACE_PROMPT_PREVIEW_LENGTH}) AS prompt_redacted,"
        f"CASE WHEN LENGTH(t.prompt_redacted)>{TRACE_PROMPT_PREVIEW_LENGTH} "
        "THEN 1 ELSE 0 END AS prompt_truncated,"
        "CASE WHEN t.prompt_redacted IS NOT NULL OR t.response_redacted IS NOT NULL "
        "THEN 1 ELSE 0 END AS content_captured"
    )
    rows = list(session.execute(
        f"SELECT {select} FROM {from_sql} WHERE {page_where} "  # nosec B608
        "ORDER BY t.started_at DESC,t.trace_id DESC LIMIT ?",
        from_params + page_params + (limit + 1,),
    ))
    has_next = len(rows) > limit
    rows = rows[:limit]
    trace_ids = [str(row["trace_id"]) for row in rows]
    judgments, judgments_truncated = _latest_judgments(
        session,
        trace_ids,
        normalized_evaluator_id,
        reasoning_limit=0,
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        trace_id = str(row["trace_id"])
        item = {
            "trace_id": trace_id,
            "started_at": row["started_at"],
            "provider": row.get("provider"),
            "providerKey": provider_key(row.get("provider")),
            "request_model": row.get("request_model"),
            "cluster_id": row.get("cluster_id"),
            "cluster_label": row.get("cluster_label") or row.get("cluster_id"),
            "input_tokens": row.get("input_tokens"),
            "output_tokens": row.get("output_tokens"),
            "latency_ms": _rounded_latency(row.get("latency_ms")),
            "cost_usd": row.get("cost_usd"),
            "finish_reason": row.get("finish_reason"),
            "error": bool(row.get("has_error")),
            "prompt_redacted": row.get("prompt_redacted"),
            "promptTruncated": bool(row.get("prompt_truncated")),
            "contentCaptured": bool(row.get("content_captured")),
        }
        if trace_id in judgments:
            judgment, _reasoning_truncated = judgments[trace_id]
            item["judgment"] = {
                "judges": judgment["judges"],
                "summary": judgment["summary"],
            }
        items.append(item)
    next_cursor = None
    if has_next and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(
            started_at=str(last["started_at"]),
            trace_id=str(last["trace_id"]),
            scope=scope,
        )
    return {
        "items": items,
        "total": int(total_row["n"] if total_row else 0),
        "limit": limit,
        "nextCursor": next_cursor,
        "judgmentsTruncated": judgments_truncated,
    }


def _bounded_text(value: object, maximum: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text = str(value)
    return text[:maximum], len(text) > maximum


def _bounded_json(
    value: object,
    *,
    maximum: int = TRACE_RAW_MESSAGES_LENGTH,
) -> tuple[object, bool]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return None, True
    if decoded is None:
        return None, False
    try:
        serialized = json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None, True
    if len(serialized) > maximum:
        return None, True
    return decoded, False


def read_trace_detail(
    session: TraceQuerySession,
    *,
    trace_id: str,
    evaluator_id: str | None = None,
    registry_tenant: str | None = None,
) -> dict[str, Any]:
    """Read one bounded application trace and selected-evaluator judgment."""
    normalized_trace_id = _validated_text(
        trace_id,
        field="trace id",
        maximum=MAX_TRACE_ID_LENGTH,
        strip=False,
    )
    if not normalized_trace_id or not session.table_exists("traces"):
        raise TraceNotFoundError(normalized_trace_id)
    normalized_registry_tenant = _validated_text(
        registry_tenant,
        field="registry tenant",
        maximum=128,
        strip=False,
    ) or None
    normalized_evaluator_id = _validated_text(
        evaluator_id,
        field="evaluator",
        maximum=128,
        strip=False,
    ) or None
    columns = session.columns("traces")
    from_sql, cluster, cluster_label, from_params = _registry_projection(
        session,
        normalized_registry_tenant,
        columns,
    )
    tags = _tags_column(columns)
    workload = (
        session.json_text_expression(tags, "verdict.workload")
        if tags != "NULL"
        else "NULL"
    )
    raw_messages = session.cast_text_expression(_raw_messages_column(columns))
    tags_text = session.cast_text_expression(tags)
    select = (
        "t.trace_id,"
        f"SUBSTR(t.parent_span_id,1,{TRACE_METADATA_LENGTH}) AS parent_span_id,"
        "t.started_at,t.ended_at,"
        f"SUBSTR(t.provider,1,{TRACE_METADATA_LENGTH}) AS provider,"
        f"SUBSTR(t.operation,1,{TRACE_METADATA_LENGTH}) AS operation,"
        f"SUBSTR(t.request_model,1,{TRACE_METADATA_LENGTH}) AS request_model,"
        f"SUBSTR(t.response_model,1,{TRACE_METADATA_LENGTH}) AS response_model,"
        "t.input_tokens,t.output_tokens,t.temperature,t.max_tokens,"
        f"SUBSTR(t.finish_reason,1,{TRACE_METADATA_LENGTH}) AS finish_reason,"
        f"SUBSTR(t.error,1,{TRACE_ERROR_LENGTH + 1}) AS error,"
        "t.latency_ms,"
        f"SUBSTR(t.prompt_redacted,1,{TRACE_CONTENT_LENGTH + 1}) AS prompt_redacted,"
        f"SUBSTR(t.response_redacted,1,{TRACE_CONTENT_LENGTH + 1}) AS response_redacted,"
        f"SUBSTR(t.tenant_id,1,{TRACE_METADATA_LENGTH}) AS tenant_id,"
        f"SUBSTR(t.session_id,1,{TRACE_METADATA_LENGTH}) AS session_id,"
        f"SUBSTR(t.user_id_hash,1,{TRACE_METADATA_LENGTH}) AS user_id_hash,"
        "t.cost_usd,"
        f"SUBSTR({cluster},1,{TRACE_METADATA_LENGTH}) AS cluster_id,"
        f"SUBSTR({cluster_label},1,{TRACE_METADATA_LENGTH}) AS cluster_label,"
        f"SUBSTR({tags_text},1,{TRACE_TAGS_LENGTH + 1}) AS trace_tags,"
        f"SUBSTR({raw_messages},1,{TRACE_RAW_MESSAGES_LENGTH + 1}) AS raw_messages"
    )
    row = session.execute(
        f"SELECT {select} FROM {from_sql} WHERE t.trace_id=? "  # nosec B608
        f"AND COALESCE({workload},'')<>'judge'",
        (*from_params, normalized_trace_id),
    ).fetchone()
    if row is None:
        raise TraceNotFoundError(normalized_trace_id)
    prompt, prompt_truncated = _bounded_text(
        row.get("prompt_redacted"), TRACE_CONTENT_LENGTH
    )
    response, response_truncated = _bounded_text(
        row.get("response_redacted"), TRACE_CONTENT_LENGTH
    )
    error, error_truncated = _bounded_text(row.get("error"), TRACE_ERROR_LENGTH)
    raw_messages, raw_messages_truncated = _bounded_json(row.get("raw_messages"))
    tags_value, tags_truncated = _bounded_json(
        row.get("trace_tags"),
        maximum=TRACE_TAGS_LENGTH,
    )
    tags_value = tags_value if isinstance(tags_value, dict) else {}
    detail = {
        "trace_id": row["trace_id"],
        "parent_span_id": row.get("parent_span_id"),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "provider": row.get("provider"),
        "providerKey": provider_key(row.get("provider")),
        "operation": row.get("operation"),
        "request_model": row.get("request_model"),
        "response_model": row.get("response_model"),
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "temperature": row.get("temperature"),
        "max_tokens": row.get("max_tokens"),
        "finish_reason": row.get("finish_reason"),
        "error": error,
        "latency_ms": _rounded_latency(row.get("latency_ms")),
        "prompt_redacted": prompt,
        "response_redacted": response,
        "raw_messages": raw_messages,
        "tenant_id": row.get("tenant_id"),
        "session_id": row.get("session_id"),
        "user_id_hash": row.get("user_id_hash"),
        "cluster_id": row.get("cluster_id"),
        "cluster_label": row.get("cluster_label") or row.get("cluster_id"),
        "tags": tags_value,
        "cost_usd": row.get("cost_usd"),
        "contentCaptured": prompt is not None or response is not None,
        "truncation": {
            "prompt": prompt_truncated,
            "response": response_truncated,
            "error": error_truncated,
            "rawMessages": raw_messages_truncated,
            "tags": tags_truncated,
        },
    }
    judgments, judgments_truncated = _latest_judgments(
        session,
        [normalized_trace_id],
        normalized_evaluator_id,
        reasoning_limit=TRACE_JUDGMENT_REASONING_LENGTH,
    )
    judgment_result = judgments.get(normalized_trace_id)
    if judgment_result is not None:
        judgment, reasoning_truncated = judgment_result
        detail["judgment"] = judgment
        detail["truncation"]["judgmentReasoning"] = reasoning_truncated
    else:
        detail["truncation"]["judgmentReasoning"] = False
    detail["truncation"]["judgments"] = judgments_truncated
    return detail
