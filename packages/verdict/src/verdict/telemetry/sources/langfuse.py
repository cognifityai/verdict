"""Langfuse observation normalization and v2 API pagination."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from verdict.telemetry.http import JsonHttpClient
from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.normalize import (
    as_mapping,
    coalesce,
    first,
    make_trace,
    parse_datetime,
    parse_json_value,
)


def map_langfuse_observation(record: object, context: ImportContext) -> MappingResult:
    if not isinstance(record, dict):
        return MappingResult.skipped("malformed_record")
    observation_type = str(first(record, "type", "observationType") or "").lower()
    if observation_type not in {"generation", "embedding"}:
        return MappingResult.skipped("non_llm_span")
    usage = as_mapping(parse_json_value(first(record, "usageDetails", "usage")))
    costs = as_mapping(parse_json_value(record.get("costDetails")))
    parameters = as_mapping(parse_json_value(record.get("modelParameters")))
    metadata = as_mapping(parse_json_value(record.get("metadata")))
    model = first(record, "providedModelName", "model", "internalModelId")
    latency_seconds = record.get("latency")
    explicit_latency_ms = None
    if isinstance(latency_seconds, (int, float)) and not isinstance(latency_seconds, bool):
        explicit_latency_ms = float(latency_seconds) * 1000.0
    level = str(record.get("level") or "").upper()
    error = record.get("statusMessage") if level in {"ERROR", "WARNING"} else None
    return make_trace(
        context=context,
        external_id=record.get("id"),
        external_trace_id=first(record, "traceId", "trace_id"),
        started_at=parse_datetime(first(record, "startTime", "start_time", "createdAt")),
        ended_at=parse_datetime(first(record, "endTime", "end_time")),
        explicit_latency_ms=explicit_latency_ms,
        provider=first(metadata, "ls_provider", "provider"),
        operation="embeddings" if observation_type == "embedding" else "chat",
        request_model=model,
        response_model=model,
        input_tokens=first(usage, "input", "input_tokens", "prompt_tokens", "inputTokens"),
        output_tokens=first(usage, "output", "output_tokens", "completion_tokens", "outputTokens"),
        temperature=first(parameters, "temperature", "model_temperature"),
        max_tokens=first(parameters, "max_tokens", "maxTokens"),
        finish_reason=first(record, "finishReason", "finish_reason"),
        error=error,
        input_value=first(record, "input", "prompt"),
        output_value=first(record, "output", "completion", "response"),
        session_id=first(record, "sessionId", "session_id"),
        cost_usd=coalesce(
            first(costs, "total", "total_cost"),
            first(record, "totalCost", "calculatedTotalCost", "cost_usd"),
        ),
    )


@dataclass(frozen=True)
class LangfuseApiSource:
    client: JsonHttpClient
    context: ImportContext
    base_url: str
    public_key: str
    secret_key: str
    start_time: datetime
    end_time: datetime
    page_size: int = 100
    user_id: str | None = None
    session_id: str | None = None
    max_records: int | None = None

    def __post_init__(self) -> None:
        if not self.public_key or not self.secret_key:
            raise ValueError("Langfuse public and secret keys are required")
        if not 1 <= self.page_size <= 1000:
            raise ValueError("Langfuse page_size must be in [1,1000]")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        if self.max_records is not None and self.max_records <= 0:
            raise ValueError("Langfuse max_records must be positive")

    def __iter__(self) -> Iterator[MappingResult]:
        for row in self.iter_observations():
            yield map_langfuse_observation(row, self.context)

    def iter_observations(self) -> Iterator[dict[str, Any]]:
        import base64

        auth = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        yielded = 0
        while True:
            query: dict[str, object] = {
                "fromStartTime": self.start_time.isoformat(),
                "toStartTime": self.end_time.isoformat(),
                "fields": "basic,time,io,model,usage,metrics",
                "limit": self.page_size,
            }
            if self.user_id is not None:
                query["userId"] = self.user_id
            if self.session_id is not None:
                query["sessionId"] = self.session_id
            if cursor is not None:
                query["cursor"] = cursor
            payload = self.client.request_json(
                "GET",
                f"{self.base_url.rstrip('/')}/api/public/v2/observations",
                headers={"Authorization": f"Basic {auth}"},
                query=query,
            )
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise ValueError("Langfuse response data must be a list")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Langfuse observation must be an object")
                yield row
                yielded += 1
                if self.max_records is not None and yielded >= self.max_records:
                    return
            meta = as_mapping(payload.get("meta"))
            next_cursor = first(meta, "cursor", "nextCursor", "next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return
            if next_cursor in seen_cursors:
                raise ValueError("Langfuse pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
