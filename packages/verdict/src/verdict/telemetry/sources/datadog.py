"""Datadog LLM Observability span normalization and export pagination."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from verdict.telemetry.http import JsonHttpClient
from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.normalize import (
    as_mapping,
    end_from_duration,
    first,
    make_trace,
    parse_datetime,
)


def map_datadog_span(record: object, context: ImportContext) -> MappingResult:
    if not isinstance(record, dict):
        return MappingResult.skipped("malformed_record")
    attributes = as_mapping(record.get("attributes")) or record
    meta = as_mapping(attributes.get("meta"))
    span_meta = as_mapping(meta.get("span"))
    kind = str(
        first(span_meta, "kind")
        or first(meta, "span.kind", "kind")
        or attributes.get("span_kind")
        or ""
    ).lower()
    if kind not in {"llm", "embedding"}:
        return MappingResult.skipped("non_llm_span")
    metrics = as_mapping(attributes.get("metrics"))
    metadata = as_mapping(meta.get("metadata"))
    input_container = as_mapping(meta.get("input"))
    output_container = as_mapping(meta.get("output"))
    timestamp = attributes.get("timestamp")
    started = parse_datetime(timestamp)
    if started is None and isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        started = parse_datetime(timestamp, numeric_unit="ms")
    duration_ns = first(attributes, "duration", "duration_ns")
    duration_ms = None
    if isinstance(duration_ns, (int, float)) and not isinstance(duration_ns, bool):
        duration_ms = float(duration_ns) / 1_000_000.0
    ended = parse_datetime(first(attributes, "end_time", "endTime")) or end_from_duration(
        started, duration_ms
    )
    status = str(attributes.get("status") or "").lower()
    error = first(meta, "error.message", "error_message") if status in {"error", "failed"} else None
    return make_trace(
        context=context,
        external_id=first(attributes, "span_id") or record.get("id"),
        external_trace_id=attributes.get("trace_id"),
        started_at=started,
        ended_at=ended,
        explicit_latency_ms=duration_ms,
        provider=first(meta, "model_provider"),
        operation="embeddings" if kind == "embedding" else "chat",
        request_model=first(meta, "model_name"),
        response_model=first(meta, "model_name"),
        input_tokens=first(metrics, "input_tokens", "prompt_tokens"),
        output_tokens=first(metrics, "output_tokens", "completion_tokens"),
        temperature=first(metadata, "temperature", "model_temperature"),
        max_tokens=first(metadata, "max_tokens"),
        finish_reason=first(metadata, "finish_reason", "finish_reasons"),
        error=error,
        input_value=first(input_container, "messages", "value"),
        output_value=first(output_container, "messages", "value"),
        session_id=first(attributes, "session_id") or first(meta, "session_id"),
        cost_usd=first(metrics, "total_cost", "cost_usd"),
    )


@dataclass(frozen=True)
class DatadogApiSource:
    client: JsonHttpClient
    context: ImportContext
    base_url: str
    api_key: str
    app_key: str
    start_time: datetime
    end_time: datetime
    page_size: int = 100

    def __post_init__(self) -> None:
        if not self.api_key or not self.app_key:
            raise ValueError("Datadog API and application keys are required")
        if urlparse(self.base_url).scheme != "https" and urlparse(self.base_url).hostname not in {
            "127.0.0.1",
            "localhost",
        }:
            raise ValueError("Datadog base URL must use HTTPS")
        if not 1 <= self.page_size <= 5000:
            raise ValueError("Datadog page_size must be in [1,5000]")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")

    def __iter__(self) -> Iterator[MappingResult]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            query: dict[str, object] = {
                "filter[from]": self.start_time.isoformat(),
                "filter[to]": self.end_time.isoformat(),
                "filter[span_kind]": "llm",
                "include_attachments": "true",
                "page[limit]": self.page_size,
                "sort": "timestamp",
            }
            if cursor is not None:
                query["page[cursor]"] = cursor
            payload = self.client.request_json(
                "GET",
                f"{self.base_url.rstrip('/')}/api/v2/llm-obs/v1/spans/events",
                headers={"DD-API-KEY": self.api_key, "DD-APPLICATION-KEY": self.app_key},
                query=query,
            )
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise ValueError("Datadog response data must be a list")
            for row in rows:
                yield map_datadog_span(row, self.context)
            page = as_mapping(as_mapping(payload.get("meta")).get("page"))
            next_cursor = first(page, "after", "cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return
            if next_cursor in seen_cursors:
                raise ValueError("Datadog pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
