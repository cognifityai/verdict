"""Comet Opik LLM span normalization and REST search pagination."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from verdict.telemetry.http import JsonHttpClient
from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.normalize import as_mapping, coalesce, first, make_trace, parse_datetime


def map_opik_span(record: object, context: ImportContext) -> MappingResult:
    if not isinstance(record, dict):
        return MappingResult.skipped("malformed_record")
    kind = str(first(record, "type", "span_type") or "").lower()
    if kind not in {"llm", "generation"}:
        return MappingResult.skipped("non_llm_span")
    usage = as_mapping(first(record, "usage", "usage_details"))
    metadata = as_mapping(record.get("metadata"))
    output = as_mapping(record.get("output"))
    choices = output.get("choices")
    finish_reason = None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish_reason = first(choices[0], "finish_reason", "stop_reason")
    return make_trace(
        context=context,
        external_id=record.get("id"),
        external_trace_id=record.get("trace_id"),
        started_at=parse_datetime(record.get("start_time")),
        ended_at=parse_datetime(record.get("end_time")),
        provider=first(record, "provider") or first(metadata, "provider", "model_provider"),
        operation="chat",
        request_model=first(record, "model") or first(metadata, "model", "model_name"),
        response_model=first(record, "model") or first(metadata, "model", "model_name"),
        input_tokens=first(usage, "input_tokens", "prompt_tokens", "input"),
        output_tokens=first(usage, "output_tokens", "completion_tokens", "output"),
        temperature=first(metadata, "temperature", "model_temperature"),
        max_tokens=first(metadata, "max_tokens"),
        finish_reason=finish_reason or first(record, "finish_reason"),
        error=record.get("error_info") or record.get("error"),
        input_value=record.get("input"),
        output_value=record.get("output"),
        session_id=first(metadata, "session_id", "conversation_id", "thread_id"),
        cost_usd=coalesce(
            first(record, "total_estimated_cost", "total_cost"),
            first(usage, "total_cost"),
        ),
    )


@dataclass(frozen=True)
class OpikApiSource:
    client: JsonHttpClient
    context: ImportContext
    base_url: str
    project: str
    start_time: datetime
    end_time: datetime
    api_key: str | None = None
    workspace: str | None = None
    page_size: int = 500

    def __post_init__(self) -> None:
        if not self.project:
            raise ValueError("Opik project is required")
        if not 1 <= self.page_size <= 2000:
            raise ValueError("Opik page_size must be in [1,2000]")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")

    def __iter__(self) -> Iterator[MappingResult]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = self.api_key
        if self.workspace:
            headers["Comet-Workspace"] = self.workspace
        while True:
            body: dict[str, object] = {
                "project_name": self.project,
                "type": "llm",
                "from_time": self.start_time.isoformat(),
                "to_time": self.end_time.isoformat(),
                "limit": self.page_size,
                "truncate": False,
            }
            if cursor is not None:
                body["last_retrieved_id"] = cursor
            rows = self.client.request_json_lines(
                "POST",
                f"{self.base_url.rstrip('/')}/v1/private/spans/search",
                headers=headers,
                body=body,
            )
            for row in rows:
                if "id" not in row and any(key in row for key in ("code", "message", "error")):
                    raise ValueError("Opik stream returned an error record")
                yield map_opik_span(row, self.context)
            if len(rows) < self.page_size:
                return
            next_cursor = rows[-1].get("id") if rows else None
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ValueError("Opik full page ended without a stable span id")
            if next_cursor in seen_cursors:
                raise ValueError("Opik pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
