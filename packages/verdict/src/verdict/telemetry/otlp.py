"""OTLP/JSON and OpenInference span normalization."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.normalize import (
    as_mapping,
    first,
    make_trace,
    parse_datetime,
    parse_json_value,
)

_MESSAGE_KEY = re.compile(r"llm\.(input|output)_messages\.(\d+)\.message\.(role|content)\Z")
_MESSAGE_CONTENT_KEY = re.compile(
    r"llm\.(input|output)_messages\.(\d+)\.message\.contents\.(\d+)\.message_content\.(text|type)\Z"
)
_LEGACY_MESSAGE_KEY = re.compile(r"llm\.(prompts|completions)\.(\d+)\.(role|content)\Z")
_LLM_OPERATIONS = {
    "chat",
    "chat.completion",
    "chat.completions",
    "chat_completion",
    "completion",
    "text_completion",
    "generate_content",
    "embeddings",
    "embedding",
}
_VERCEL_LLM_OPERATIONS = {
    "ai.generateObject.doGenerate",
    "ai.generateText.doGenerate",
    "ai.streamObject.doStream",
    "ai.streamText.doStream",
}
_VERCEL_EMBEDDING_OPERATIONS = {"ai.embed.doEmbed", "ai.embedMany.doEmbed"}
_MAX_ATTRIBUTES = 10_000
_MAX_ANYVALUE_DEPTH = 16


def _any_value(value: object, depth: int = 0) -> Any:
    if depth > _MAX_ANYVALUE_DEPTH or not isinstance(value, dict):
        return None
    for key in ("stringValue", "boolValue", "intValue", "doubleValue", "bytesValue"):
        if key in value:
            return value[key]
    array = value.get("arrayValue")
    if isinstance(array, dict) and isinstance(array.get("values"), list):
        return [_any_value(item, depth + 1) for item in array["values"][:_MAX_ATTRIBUTES]]
    kvlist = value.get("kvlistValue")
    if isinstance(kvlist, dict):
        return attributes_to_dict(kvlist.get("values"), depth=depth + 1)
    return None


def attributes_to_dict(value: object, *, depth: int = 0) -> dict[str, Any]:
    """Decode OTLP attribute lists or pass through bounded JSON mappings."""
    if depth > _MAX_ANYVALUE_DEPTH:
        return {}
    if isinstance(value, dict):
        return {str(key): item for key, item in list(value.items())[:_MAX_ATTRIBUTES]}
    if not isinstance(value, list):
        return {}
    output: dict[str, Any] = {}
    for item in value[:_MAX_ATTRIBUTES]:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        key = item["key"]
        if key in output:
            continue
        output[key] = _any_value(item.get("value"), depth + 1)
    return output


def _indexed_messages(attributes: dict[str, Any], direction: str) -> list[dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    blocks: dict[tuple[int, int], dict[str, str]] = {}
    for key, value in attributes.items():
        match = _MESSAGE_KEY.fullmatch(key)
        if match and match.group(1) == direction and isinstance(value, str):
            rows.setdefault(int(match.group(2)), {})[match.group(3)] = value
            continue
        content_match = _MESSAGE_CONTENT_KEY.fullmatch(key)
        if content_match and content_match.group(1) == direction and isinstance(value, str):
            blocks.setdefault((int(content_match.group(2)), int(content_match.group(3))), {})[
                content_match.group(4)
            ] = value
            continue
        legacy_match = _LEGACY_MESSAGE_KEY.fullmatch(key)
        expected_group = "prompts" if direction == "input" else "completions"
        if legacy_match and legacy_match.group(1) == expected_group and isinstance(value, str):
            rows.setdefault(int(legacy_match.group(2)), {})[legacy_match.group(3)] = value
    for (message_index, _), block in sorted(blocks.items()):
        if block.get("type", "text") in {"text", "input_text", "output_text"} and block.get("text"):
            previous = rows.setdefault(message_index, {}).get("content", "")
            rows[message_index]["content"] = "\n".join(
                piece for piece in (previous, block["text"]) if piece
            )
    default_role = "user" if direction == "input" else "assistant"
    return [
        {"role": row.get("role", default_role), "content": row["content"]}
        for _, row in sorted(rows.items())
        if isinstance(row.get("content"), str)
    ]


def _event_messages(span: dict[str, Any], direction: str) -> object:
    expected_name = "gen_ai.content.prompt" if direction == "input" else "gen_ai.content.completion"
    expected_key = "gen_ai.prompt" if direction == "input" else "gen_ai.completion"
    messages: list[object] = []
    events = span.get("events")
    for event in events[:_MAX_ATTRIBUTES] if isinstance(events, list) else []:
        event_mapping = as_mapping(event)
        if str(event_mapping.get("name") or "") != expected_name:
            continue
        event_attributes = attributes_to_dict(event_mapping.get("attributes"))
        value = parse_json_value(first(event_attributes, expected_key, "gen_ai.event.content"))
        if isinstance(value, list):
            messages.extend(value)
        elif value is not None:
            messages.append(value)
    return messages or None


def _messages(span: dict[str, Any], attributes: dict[str, Any], direction: str) -> object:
    if direction == "input":
        direct = first(
            attributes,
            "gen_ai.input.messages",
            "gen_ai.prompt",
            "ai.prompt.messages",
            "ai.prompt",
            "input.value",
        )
    else:
        direct = first(
            attributes,
            "gen_ai.output.messages",
            "gen_ai.completion",
            "ai.response.text",
            "output.value",
        )
    if direct is not None:
        return parse_json_value(direct)
    indexed = _indexed_messages(attributes, direction)
    return indexed or _event_messages(span, direction)


def _finish_reason(value: object) -> str | None:
    parsed = parse_json_value(value)
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
        return parsed[0]
    if isinstance(parsed, str):
        return parsed
    return None


def _error_from(span: dict[str, Any], attributes: dict[str, Any]) -> str | None:
    status = as_mapping(span.get("status"))
    code = str(first(status, "code") or first(span, "status_code", "statusCode") or "").upper()
    if code in {"2", "STATUS_CODE_ERROR", "ERROR"}:
        message = first(status, "message", "description") or first(
            span, "status_message", "statusMessage"
        )
        return (
            str(message)
            if message
            else str(first(attributes, "error.message", "exception.message") or "source span error")
        )
    for event in span.get("events", []) if isinstance(span.get("events"), list) else []:
        event_mapping = as_mapping(event)
        if str(event_mapping.get("name") or "").lower() == "exception":
            event_attributes = attributes_to_dict(event_mapping.get("attributes"))
            message = first(event_attributes, "exception.message", "exception.type")
            if isinstance(message, str):
                return message
    return None


def is_llm_attributes(
    attributes: dict[str, Any], span_kind: object = None, span_name: object = None
) -> bool:
    operation = str(attributes.get("gen_ai.operation.name") or "").lower()
    inference_kind = str(attributes.get("openinference.span.kind") or span_kind or "").upper()
    langfuse_kind = str(attributes.get("langfuse.observation.type") or "").lower()
    traceloop_kind = str(attributes.get("traceloop.span.kind") or "").upper()
    legacy_operation = str(attributes.get("llm.request.type") or "").lower()
    vercel_operation = str(attributes.get("ai.operationId") or "")
    return (
        operation in _LLM_OPERATIONS
        or inference_kind in {"LLM", "EMBEDDING"}
        or langfuse_kind in {"generation", "embedding"}
        or traceloop_kind in {"LLM", "EMBEDDING"}
        or legacy_operation in _LLM_OPERATIONS
        or vercel_operation in _VERCEL_LLM_OPERATIONS | _VERCEL_EMBEDDING_OPERATIONS
        or span_name == "claude_code.llm_request"
    )


def map_attribute_span(
    *,
    span: dict[str, Any],
    attributes: dict[str, Any],
    context: ImportContext,
    external_id: object,
    external_trace_id: object,
    started_at: object,
    ended_at: object,
    time_unit: str | None = None,
    span_kind: object = None,
) -> MappingResult:
    """Map one OTel/OpenInference-shaped span shared by OTLP and Phoenix."""
    if not is_llm_attributes(attributes, span_kind, span.get("name")):
        return MappingResult.skipped("non_llm_span")
    vercel_operation = str(attributes.get("ai.operationId") or "")
    operation = first(attributes, "gen_ai.operation.name", "llm.operation", "llm.request.type")
    if operation is None and str(span_kind or "").upper() == "EMBEDDING":
        operation = "embeddings"
    if vercel_operation in _VERCEL_EMBEDDING_OPERATIONS:
        operation = "embeddings"
    request_model = first(
        attributes,
        "gen_ai.request.model",
        "ai.model.id",
        "llm.request.model",
        "llm.model_name",
        "langfuse.observation.model.name",
    )
    response_model = first(
        attributes,
        "gen_ai.response.model",
        "ai.response.model",
        "llm.response.model",
        "llm.model_name",
    )
    provider = first(
        attributes,
        "gen_ai.provider.name",
        "gen_ai.system",
        "ai.model.provider",
        "llm.vendor",
        "llm.provider",
        "llm.system",
        "langfuse.observation.metadata.ls_provider",
    )
    usage_details = parse_json_value(attributes.get("langfuse.observation.usage_details"))
    usage = as_mapping(usage_details)
    input_tokens = first(
        attributes,
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.prompt_tokens",
        "gen_ai.response.prompt_tokens",
        "ai.usage.promptTokens",
        "llm.usage.prompt_tokens",
        "llm.token_count.prompt",
        "input_tokens",
    )
    output_tokens = first(
        attributes,
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.completion_tokens",
        "gen_ai.response.completion_tokens",
        "ai.usage.completionTokens",
        "llm.usage.completion_tokens",
        "llm.token_count.completion",
        "output_tokens",
    )
    if input_tokens is None:
        input_tokens = first(usage, "input", "input_tokens", "prompt_tokens")
    if output_tokens is None:
        output_tokens = first(usage, "output", "output_tokens", "completion_tokens")
    session_id = first(
        attributes,
        "gen_ai.conversation.id",
        "session.id",
        "langfuse.session.id",
    )
    started = parse_datetime(started_at, numeric_unit=time_unit)
    ended = parse_datetime(ended_at, numeric_unit=time_unit) if ended_at is not None else None
    return make_trace(
        context=context,
        external_id=external_id,
        external_trace_id=external_trace_id,
        started_at=started,
        ended_at=ended,
        provider=provider,
        operation=operation,
        request_model=request_model,
        response_model=response_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        temperature=first(
            attributes,
            "gen_ai.request.temperature",
            "llm.request.temperature",
            "llm.invocation_parameters.temperature",
        ),
        max_tokens=first(
            attributes,
            "gen_ai.request.max_tokens",
            "ai.settings.maxOutputTokens",
            "llm.request.max_tokens",
            "llm.invocation_parameters.max_tokens",
        ),
        finish_reason=_finish_reason(
            first(
                attributes,
                "gen_ai.response.finish_reasons",
                "gen_ai.response.finish_reason",
                "ai.response.finishReason",
                "llm.response.finish_reason",
            )
        ),
        error=_error_from(span, attributes),
        input_value=_messages(span, attributes, "input"),
        output_value=_messages(span, attributes, "output"),
        session_id=session_id,
        cost_usd=first(attributes, "gen_ai.usage.cost", "llm.cost.total", "cost.total"),
    )


def _iter_otlp_spans(payload: dict[str, Any]) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    resource_spans = payload.get("resourceSpans")
    if not isinstance(resource_spans, list):
        return
    for resource_entry in resource_spans:
        resource_mapping = as_mapping(resource_entry)
        resource = as_mapping(resource_mapping.get("resource"))
        resource_attributes = attributes_to_dict(resource.get("attributes"))
        groups = resource_mapping.get("scopeSpans")
        if not isinstance(groups, list):
            groups = resource_mapping.get("instrumentationLibrarySpans")
        for group in groups if isinstance(groups, list) else []:
            group_mapping = as_mapping(group)
            spans = group_mapping.get("spans")
            for span in spans if isinstance(spans, list) else []:
                if isinstance(span, dict):
                    yield span, resource_attributes


def map_otlp_payload(payload: object, context: ImportContext) -> list[MappingResult]:
    if not isinstance(payload, dict):
        raise ValueError("OTLP payload must be a JSON object")
    results: list[MappingResult] = []
    for span, _resource_attributes in _iter_otlp_spans(payload):
        attributes = attributes_to_dict(span.get("attributes"))
        results.append(
            map_attribute_span(
                span=span,
                attributes=attributes,
                context=context,
                external_id=span.get("spanId"),
                external_trace_id=span.get("traceId"),
                started_at=span.get("startTimeUnixNano"),
                ended_at=span.get("endTimeUnixNano"),
                time_unit="ns",
            )
        )
    return results


def otlp_protobuf_to_json(payload: bytes) -> dict[str, Any]:
    """Decode OTLP protobuf lazily so ordinary file/API imports stay dependency-free."""
    try:
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
    except ImportError as exc:  # pragma: no cover - exercised in isolated install checks
        raise RuntimeError(
            "OTLP protobuf requires `pip install cognifity-verdict[telemetry]`"
        ) from exc
    request = ExportTraceServiceRequest()
    request.ParseFromString(payload)
    decoded = MessageToDict(request, preserving_proto_field_name=False)
    if not isinstance(decoded, dict):
        raise ValueError("decoded OTLP protobuf was not an object")
    return decoded


def decode_otlp_http(body: bytes, content_type: str) -> dict[str, Any]:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized in {"application/json", "application/otlp+json"}:
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("invalid OTLP JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("OTLP JSON must be an object")
        return decoded
    if normalized in {"application/x-protobuf", "application/protobuf"}:
        try:
            return otlp_protobuf_to_json(body)
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise ValueError("invalid OTLP protobuf") from exc
    raise ValueError("unsupported OTLP content type")
