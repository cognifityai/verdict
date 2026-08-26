"""LangSmith LLM run normalization and query pagination."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from verdict.telemetry.http import JsonHttpClient
from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.normalize import as_mapping, first, make_trace, parse_datetime


def map_langsmith_run(record: object, context: ImportContext) -> MappingResult:
    if not isinstance(record, dict):
        return MappingResult.skipped("malformed_record")
    if str(record.get("run_type") or "").lower() not in {"llm", "chat_model"}:
        return MappingResult.skipped("non_llm_span")
    inputs = as_mapping(record.get("inputs"))
    outputs = as_mapping(record.get("outputs"))
    extra = as_mapping(record.get("extra"))
    metadata = as_mapping(extra.get("metadata"))
    llm_output = as_mapping(outputs.get("llm_output"))
    usage = as_mapping(
        first(
            llm_output,
            "token_usage",
            "usage",
            "usage_metadata",
        )
        or outputs.get("usage_metadata")
        or record.get("usage_metadata")
    )
    generations = outputs.get("generations")
    finish_reason = None
    if isinstance(generations, list) and generations:
        first_generation = generations[0]
        while isinstance(first_generation, list) and first_generation:
            first_generation = first_generation[0]
        if isinstance(first_generation, dict):
            finish_reason = first(
                as_mapping(first_generation.get("generation_info")), "finish_reason", "stop_reason"
            )
    model = first(metadata, "ls_model_name", "model_name", "model") or first(
        as_mapping(extra.get("invocation_params")), "model", "model_name"
    )
    return make_trace(
        context=context,
        external_id=record.get("id"),
        external_trace_id=record.get("trace_id"),
        started_at=parse_datetime(record.get("start_time")),
        ended_at=parse_datetime(record.get("end_time")),
        provider=first(metadata, "ls_provider", "provider"),
        operation="chat",
        request_model=model,
        response_model=model,
        input_tokens=first(usage, "input_tokens", "prompt_tokens", "input"),
        output_tokens=first(usage, "output_tokens", "completion_tokens", "output"),
        temperature=first(as_mapping(extra.get("invocation_params")), "temperature"),
        max_tokens=first(as_mapping(extra.get("invocation_params")), "max_tokens"),
        finish_reason=finish_reason,
        error=record.get("error"),
        input_value=first(inputs, "messages", "input", "prompt") or inputs,
        output_value=first(outputs, "generations", "messages", "output", "text") or outputs,
        session_id=first(metadata, "session_id", "conversation_id", "thread_id"),
        cost_usd=first(usage, "total_cost", "cost_usd"),
    )


@dataclass(frozen=True)
class LangSmithApiSource:
    client: JsonHttpClient
    context: ImportContext
    base_url: str
    api_key: str
    project_name: str
    start_time: datetime
    end_time: datetime
    page_size: int = 100

    def __post_init__(self) -> None:
        if not self.api_key or not self.project_name:
            raise ValueError("LangSmith API key and project are required")
        if not 1 <= self.page_size <= 100:
            raise ValueError("LangSmith page_size must be in [1,100]")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")

    def __iter__(self) -> Iterator[MappingResult]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            body: dict[str, object] = {
                "project_name": self.project_name,
                "run_type": "llm",
                "start_time": self.start_time.isoformat(),
                "end_time": self.end_time.isoformat(),
                "limit": self.page_size,
                "select": [
                    "id",
                    "trace_id",
                    "run_type",
                    "start_time",
                    "end_time",
                    "inputs",
                    "outputs",
                    "extra",
                    "error",
                ],
            }
            if cursor is not None:
                body["cursor"] = cursor
            payload = self.client.request_json(
                "POST",
                f"{self.base_url.rstrip('/')}/runs/query",
                headers={"x-api-key": self.api_key},
                body=body,
            )
            rows = payload.get("runs")
            if not isinstance(rows, list):
                rows = payload.get("data")
            if not isinstance(rows, list):
                raise ValueError("LangSmith response runs must be a list")
            for row in rows:
                yield map_langsmith_run(row, self.context)
            next_cursor = first(payload, "cursors", "cursor", "next_cursor")
            if isinstance(next_cursor, dict):
                next_cursor = first(next_cursor, "next", "next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return
            if next_cursor in seen_cursors:
                raise ValueError("LangSmith pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
