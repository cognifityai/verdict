"""Arize Phoenix OpenInference span normalization and REST pagination."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from verdict.telemetry.http import JsonHttpClient
from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.normalize import as_mapping, first
from verdict.telemetry.otlp import attributes_to_dict, map_attribute_span


def map_phoenix_span(record: object, context: ImportContext) -> MappingResult:
    if not isinstance(record, dict):
        return MappingResult.skipped("malformed_record")
    span_context = as_mapping(record.get("context"))
    attributes = attributes_to_dict(record.get("attributes"))
    return map_attribute_span(
        span=record,
        attributes=attributes,
        context=context,
        external_id=first(span_context, "span_id", "spanId")
        or first(record, "span_id", "spanId", "id"),
        external_trace_id=first(span_context, "trace_id", "traceId")
        or first(record, "trace_id", "traceId"),
        started_at=first(record, "start_time", "startTime"),
        ended_at=first(record, "end_time", "endTime"),
        span_kind=first(record, "span_kind", "spanKind"),
    )


@dataclass(frozen=True)
class PhoenixApiSource:
    client: JsonHttpClient
    context: ImportContext
    base_url: str
    project: str
    start_time: datetime
    end_time: datetime
    api_key: str | None = None
    page_size: int = 100

    def __post_init__(self) -> None:
        if not self.project:
            raise ValueError("Phoenix project is required")
        if not 1 <= self.page_size <= 1000:
            raise ValueError("Phoenix page_size must be in [1,1000]")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")

    def __iter__(self) -> Iterator[MappingResult]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        while True:
            query: dict[str, object] = {
                "include_spans": "true",
                "start_time": self.start_time.isoformat(),
                "end_time": self.end_time.isoformat(),
                "limit": self.page_size,
            }
            if cursor is not None:
                query["cursor"] = cursor
            payload = self.client.request_json(
                "GET",
                f"{self.base_url.rstrip('/')}/v1/projects/{quote(self.project, safe='')}/traces",
                headers=headers,
                query=query,
            )
            rows = payload.get("data")
            if not isinstance(rows, list):
                rows = payload.get("traces")
            if not isinstance(rows, list):
                raise ValueError("Phoenix response traces must be a list")
            for trace in rows:
                trace_mapping = as_mapping(trace)
                spans = trace_mapping.get("spans")
                if not isinstance(spans, list):
                    spans = [trace_mapping]
                for span in spans:
                    yield map_phoenix_span(span, self.context)
            meta = as_mapping(payload.get("meta"))
            next_cursor = first(meta, "next_cursor", "cursor") or first(payload, "next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return
            if next_cursor in seen_cursors:
                raise ValueError("Phoenix pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
