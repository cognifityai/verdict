"""Bounded JSON/NDJSON readers for supported telemetry records."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.otlp import map_otlp_payload
from verdict.telemetry.sources.datadog import map_datadog_span
from verdict.telemetry.sources.langfuse import map_langfuse_observation
from verdict.telemetry.sources.langsmith import map_langsmith_run
from verdict.telemetry.sources.mlflow import map_mlflow_traces
from verdict.telemetry.sources.opik import map_opik_span
from verdict.telemetry.sources.phoenix import map_phoenix_span
from verdict.telemetry.sources.voice import map_voice_conversation

SUPPORTED_FORMATS = {
    "auto",
    "otlp",
    "langfuse",
    "langsmith",
    "datadog",
    "phoenix",
    "opik",
    "mlflow",
    "voice",
}
_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_NDJSON_LINE_BYTES = 16 * 1024 * 1024


def _detected_format(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    if isinstance(record.get("resourceSpans"), list):
        return "otlp"
    if "run_type" in record and ("inputs" in record or "outputs" in record):
        return "langsmith"
    if "info" in record and "data" in record:
        return "mlflow"
    if "turns" in record or (
        "conversation_id" in record and isinstance(record.get("messages"), list)
    ):
        return "voice"
    attributes = record.get("attributes")
    if isinstance(attributes, dict):
        meta = attributes.get("meta")
        if isinstance(meta, dict) and (
            "span" in meta or "model_provider" in meta or "model_name" in meta
        ):
            return "datadog"
    if "context" in record and "attributes" in record:
        return "phoenix"
    if str(record.get("type") or "").lower() in {"llm", "generation"} and "start_time" in record:
        return "opik"
    if "usageDetails" in record or "providedModelName" in record:
        return "langfuse"
    return None


def _map_record(record: object, file_format: str, context: ImportContext) -> list[MappingResult]:
    selected = _detected_format(record) if file_format == "auto" else file_format
    if selected is None:
        return [MappingResult.skipped("unsupported_record_format")]
    source_context = (
        ImportContext(
            adapter=selected,
            source_scope=context.source_scope,
            tenant_id=context.tenant_id,
        )
        if context.adapter == "file"
        else context
    )
    if selected == "otlp":
        return map_otlp_payload(record, source_context)
    if selected == "langfuse":
        return [map_langfuse_observation(record, source_context)]
    if selected == "langsmith":
        return [map_langsmith_run(record, source_context)]
    if selected == "datadog":
        return [map_datadog_span(record, source_context)]
    if selected == "phoenix":
        if isinstance(record, dict) and isinstance(record.get("spans"), list):
            return [map_phoenix_span(span, source_context) for span in record["spans"]]
        return [map_phoenix_span(record, source_context)]
    if selected == "opik":
        return [map_opik_span(record, source_context)]
    if selected == "mlflow":
        return map_mlflow_traces(record, source_context)
    if selected == "voice":
        return map_voice_conversation(record, source_context)
    return [MappingResult.skipped("unsupported_record_format")]


def _unwrap_json(payload: object, file_format: str) -> Iterable[object]:
    if file_format == "otlp" or (
        file_format == "auto" and isinstance(payload, dict) and "resourceSpans" in payload
    ):
        return [payload]
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return [payload]
    for key in ("data", "runs", "observations", "spans", "traces", "content"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return rows
    return [payload]


def iter_telemetry_file(
    path: str | Path,
    *,
    file_format: str,
    context: ImportContext,
) -> Iterator[MappingResult]:
    """Read one supported file with bounded memory and line sizes."""
    if file_format not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported telemetry format: {file_format}")
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix in {".ndjson", ".jsonl"}:
        with source_path.open("rb") as handle:
            line_number = 0
            while raw := handle.readline(_MAX_NDJSON_LINE_BYTES + 1):
                line_number += 1
                if len(raw) > _MAX_NDJSON_LINE_BYTES:
                    raise ValueError(f"NDJSON line {line_number} exceeds 16 MiB")
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                    raise ValueError(f"invalid NDJSON at line {line_number}") from exc
                yield from _map_record(record, file_format, context)
        return
    try:
        with source_path.open("rb") as handle:
            raw = handle.read(_MAX_JSON_BYTES + 1)
        if len(raw) > _MAX_JSON_BYTES:
            raise ValueError("JSON file exceeds 64 MiB; use NDJSON for streaming imports")
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid JSON file") from exc
    for record in _unwrap_json(payload, file_format):
        yield from _map_record(record, file_format, context)
