"""MLflow 2.x/3.x exported trace normalization."""

from __future__ import annotations

from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.normalize import (
    as_mapping,
    coalesce,
    end_from_duration,
    first,
    make_trace,
    parse_datetime,
    parse_json_value,
)


def map_mlflow_traces(record: object, context: ImportContext) -> list[MappingResult]:
    if not isinstance(record, dict):
        return [MappingResult.skipped("malformed_record")]
    info = as_mapping(record.get("info"))
    data = as_mapping(record.get("data"))
    trace_id = first(info, "trace_id", "request_id") or first(record, "trace_id", "request_id")
    metadata = as_mapping(
        parse_json_value(
            first(info, "trace_metadata", "tags") or first(record, "trace_metadata", "tags")
        )
    )
    spans = data.get("spans")
    if spans is None:
        spans = parse_json_value(record.get("spans"))
    if not isinstance(spans, list):
        return [MappingResult.skipped("missing_llm_span")]
    output: list[MappingResult] = []
    for span in spans:
        if not isinstance(span, dict):
            output.append(MappingResult.skipped("malformed_record"))
            continue
        attributes = as_mapping(parse_json_value(span.get("attributes")))
        span_type = str(
            parse_json_value(
                coalesce(
                    first(span, "span_type", "type"),
                    first(attributes, "mlflow.spanType"),
                )
            )
            or ""
        ).upper()
        if span_type not in {"LLM", "CHAT_MODEL"}:
            output.append(MappingResult.skipped("non_llm_span"))
            continue
        usage = as_mapping(
            parse_json_value(
                first(
                    attributes,
                    "usage",
                    "token_usage",
                    "usage_metadata",
                    "mlflow.chat.tokenUsage",
                )
            )
        )
        started = parse_datetime(first(span, "start_time", "startTime")) or parse_datetime(
            first(span, "start_time_unix_nano"), numeric_unit="ns"
        )
        request_time = coalesce(
            first(info, "request_time", "timestamp"),
            first(record, "request_time", "timestamp"),
        )
        started = (
            started
            or parse_datetime(request_time)
            or parse_datetime(request_time, numeric_unit="ms")
        )
        ended = parse_datetime(first(span, "end_time", "endTime")) or parse_datetime(
            first(span, "end_time_unix_nano"), numeric_unit="ns"
        )
        duration_ms = coalesce(
            first(info, "execution_duration"), first(record, "execution_duration")
        )
        explicit_latency_ms = None
        if (
            ended is None
            and isinstance(duration_ms, (int, float))
            and not isinstance(duration_ms, bool)
        ):
            ended = end_from_duration(started, float(duration_ms))
            explicit_latency_ms = duration_ms
        span_context = as_mapping(span.get("context"))
        source_id = coalesce(first(span, "span_id", "id"), first(span_context, "span_id"))
        if source_id is None and trace_id is not None:
            source_id = f"{trace_id}:llm:{len(output)}"
        status = as_mapping(span.get("status"))
        status_code = str(
            coalesce(first(span, "status_code", "statusCode"), status.get("code")) or ""
        ).upper()
        cost = as_mapping(parse_json_value(first(attributes, "mlflow.llm.cost")))
        input_value = parse_json_value(
            coalesce(first(span, "inputs", "input"), first(attributes, "mlflow.spanInputs"))
        )
        output_value = parse_json_value(
            coalesce(first(span, "outputs", "output"), first(attributes, "mlflow.spanOutputs"))
        )
        session_id = parse_json_value(
            first(
                metadata,
                "mlflow.trace.session",
                "mlflow.trace.session_id",
                "session_id",
            )
        )
        output.append(
            make_trace(
                context=context,
                external_id=source_id,
                external_trace_id=trace_id,
                started_at=started,
                ended_at=ended,
                explicit_latency_ms=explicit_latency_ms,
                provider=first(attributes, "provider", "model_provider", "mlflow.llm.provider"),
                operation="chat",
                request_model=first(attributes, "model", "model_name", "mlflow.llm.model"),
                response_model=first(attributes, "model", "model_name", "mlflow.llm.model"),
                input_tokens=first(usage, "input_tokens", "prompt_tokens", "input"),
                output_tokens=first(usage, "output_tokens", "completion_tokens", "output"),
                temperature=first(attributes, "temperature"),
                max_tokens=first(attributes, "max_tokens"),
                finish_reason=first(attributes, "finish_reason"),
                error=coalesce(
                    first(span, "status_message", "statusMessage"), status.get("message")
                )
                if status_code in {"ERROR", "STATUS_CODE_ERROR", "2"}
                else None,
                input_value=input_value,
                output_value=output_value,
                session_id=session_id,
                cost_usd=coalesce(
                    first(cost, "total_cost", "total"),
                    first(attributes, "total_cost", "cost_usd"),
                ),
            )
        )
    return output or [MappingResult.skipped("missing_llm_span")]


def map_mlflow_trace(record: object, context: ImportContext) -> MappingResult:
    """Compatibility helper returning the first result from an MLflow trace."""
    return map_mlflow_traces(record, context)[0]
