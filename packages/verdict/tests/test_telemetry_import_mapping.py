from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from verdict.schema import Operation
from verdict.telemetry.model import ImportContext
from verdict.telemetry.otlp import map_otlp_payload
from verdict.telemetry.sources.datadog import map_datadog_span
from verdict.telemetry.sources.langfuse import map_langfuse_observation
from verdict.telemetry.sources.langsmith import map_langsmith_run
from verdict.telemetry.sources.mlflow import map_mlflow_trace, map_mlflow_traces
from verdict.telemetry.sources.opik import map_opik_span
from verdict.telemetry.sources.phoenix import map_phoenix_span
from verdict.telemetry.sources.voice import map_voice_conversation

UTC = timezone.utc


def _context(adapter: str) -> ImportContext:
    return ImportContext(adapter=adapter, source_scope="project-a", tenant_id="tenant-a")


def _otel_attributes(values: dict[str, object]) -> list[dict[str, object]]:
    attributes = []
    for key, value in values.items():
        if isinstance(value, int):
            encoded = {"intValue": str(value)}
        elif isinstance(value, float):
            encoded = {"doubleValue": value}
        else:
            encoded = {"stringValue": str(value)}
        attributes.append({"key": key, "value": encoded})
    return attributes


def test_otlp_maps_genai_and_openinference_spans_without_copying_unknown_metadata() -> None:
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "checkout"}},
                        {"key": "secret.unknown", "value": {"stringValue": "canary-secret"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "1" * 16,
                                "name": "chat gpt-4o-mini",
                                "startTimeUnixNano": "1788264000000000000",
                                "endTimeUnixNano": "1788264001250000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.operation.name",
                                        "value": {"stringValue": "chat"},
                                    },
                                    {
                                        "key": "gen_ai.provider.name",
                                        "value": {"stringValue": "openai"},
                                    },
                                    {
                                        "key": "gen_ai.request.model",
                                        "value": {"stringValue": "gpt-4o-mini"},
                                    },
                                    {
                                        "key": "gen_ai.response.model",
                                        "value": {"stringValue": "gpt-4o-mini-2026-08-01"},
                                    },
                                    {
                                        "key": "gen_ai.usage.input_tokens",
                                        "value": {"intValue": "17"},
                                    },
                                    {
                                        "key": "gen_ai.usage.output_tokens",
                                        "value": {"intValue": "8"},
                                    },
                                    {
                                        "key": "gen_ai.input.messages",
                                        "value": {
                                            "stringValue": '[{"role":"user","content":"Where is order 7?"}]'
                                        },
                                    },
                                    {
                                        "key": "gen_ai.output.messages",
                                        "value": {
                                            "stringValue": '[{"role":"assistant","content":"It ships Tuesday."}]'
                                        },
                                    },
                                    {
                                        "key": "gen_ai.conversation.id",
                                        "value": {"stringValue": "session-7"},
                                    },
                                    {
                                        "key": "unknown.payload",
                                        "value": {"stringValue": "do-not-store"},
                                    },
                                ],
                                "status": {"code": "STATUS_CODE_OK"},
                            },
                            {
                                "traceId": "b" * 32,
                                "spanId": "2" * 16,
                                "name": "LLM",
                                "startTimeUnixNano": "1788264010000000000",
                                "endTimeUnixNano": "1788264010500000000",
                                "attributes": [
                                    {
                                        "key": "openinference.span.kind",
                                        "value": {"stringValue": "LLM"},
                                    },
                                    {"key": "llm.provider", "value": {"stringValue": "anthropic"}},
                                    {
                                        "key": "llm.model_name",
                                        "value": {"stringValue": "claude-haiku-4-5"},
                                    },
                                    {"key": "llm.token_count.prompt", "value": {"intValue": "21"}},
                                    {
                                        "key": "llm.token_count.completion",
                                        "value": {"intValue": "4"},
                                    },
                                    {
                                        "key": "llm.input_messages.0.message.role",
                                        "value": {"stringValue": "user"},
                                    },
                                    {
                                        "key": "llm.input_messages.0.message.content",
                                        "value": {"stringValue": "Cancel order 8"},
                                    },
                                    {
                                        "key": "llm.output_messages.0.message.role",
                                        "value": {"stringValue": "assistant"},
                                    },
                                    {
                                        "key": "llm.output_messages.0.message.content",
                                        "value": {"stringValue": "Cancelled."},
                                    },
                                    {"key": "session.id", "value": {"stringValue": "session-8"}},
                                ],
                            },
                        ]
                    }
                ],
            }
        ]
    }

    mapped = map_otlp_payload(payload, _context("otlp"))

    assert [item.skip_reason for item in mapped] == [None, None]
    first, second = [item.trace for item in mapped]
    assert first is not None and second is not None
    assert first.trace_id != second.trace_id
    assert first.started_at == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert first.latency_ms == pytest.approx(1250.0)
    assert first.input_tokens == 17
    assert first.output_tokens == 8
    assert first.request_model == "gpt-4o-mini"
    assert first.response_model == "gpt-4o-mini-2026-08-01"
    assert first.session_id == "session-7"
    assert first.raw_messages == [
        {"role": "user", "content": "Where is order 7?"},
        {"role": "assistant", "content": "It ships Tuesday."},
    ]
    assert "unknown.payload" not in first.tags
    assert "canary-secret" not in repr(first)
    assert second.provider == "anthropic"
    assert second.input_tokens == 21
    assert second.output_tokens == 4
    assert second.prompt_redacted == "Cancel order 8"
    assert second.response_redacted == "Cancelled."


def test_otlp_maps_text_from_message_parts_without_inventing_tool_content() -> None:
    payload = {
        "resourceSpans": [{"scopeSpans": [{"spans": [{
            "traceId": "c" * 32,
            "spanId": "3" * 16,
            "name": "chat model",
            "startTimeUnixNano": "1788264000000000000",
            "attributes": _otel_attributes({
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "model",
                "gen_ai.input.messages": json.dumps([{
                    "role": "user",
                    "parts": [{"type": "text", "content": "Use the evidence."}],
                }]),
                "gen_ai.output.messages": json.dumps([{
                    "role": "assistant",
                    "parts": [
                        {"type": "text", "content": "First line."},
                        {"type": "tool_call", "content": "must-not-become-response"},
                        {"type": "output_text", "text": "Second line."},
                    ],
                }]),
            }),
        }]}]}],
    }

    mapped = map_otlp_payload(payload, _context("otlp"))

    assert len(mapped) == 1
    trace = mapped[0].trace
    assert trace is not None
    assert trace.prompt_redacted == "Use the evidence."
    assert trace.response_redacted == "First line.\nSecond line."
    assert "must-not-become-response" not in repr(trace)


def test_otlp_does_not_treat_tool_call_placeholder_as_a_response() -> None:
    payload = {
        "resourceSpans": [{"scopeSpans": [{"spans": [{
            "traceId": "d" * 32,
            "spanId": "4" * 16,
            "name": "chat model",
            "startTimeUnixNano": "1788264000000000000",
            "attributes": _otel_attributes({
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "model",
                "gen_ai.input.messages": json.dumps([{
                    "role": "user", "content": "Find the order.",
                }]),
                "gen_ai.output.messages": json.dumps([{
                    "role": "assistant",
                    "parts": [
                        {"type": "text", "content": " (no content) "},
                        {"type": "tool_call", "name": "lookup_order"},
                    ],
                }]),
            }),
        }]}]}],
    }

    [mapped] = map_otlp_payload(payload, _context("otlp"))

    assert mapped.trace is not None
    assert mapped.trace.response_redacted is None


def test_otlp_keeps_real_response_text_beside_a_tool_call() -> None:
    payload = {
        "resourceSpans": [{"scopeSpans": [{"spans": [{
            "traceId": "e" * 32,
            "spanId": "5" * 16,
            "name": "chat model",
            "startTimeUnixNano": "1788264000000000000",
            "attributes": _otel_attributes({
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "model",
                "gen_ai.output.messages": json.dumps([{
                    "role": "assistant",
                    "parts": [
                        {"type": "text", "content": "I will look that up."},
                        {"type": "tool_call", "name": "lookup_order"},
                    ],
                }]),
            }),
        }]}]}],
    }

    [mapped] = map_otlp_payload(payload, _context("otlp"))

    assert mapped.trace is not None
    assert mapped.trace.response_redacted == "I will look that up."


def test_otlp_skips_non_llm_and_missing_identity_instead_of_inventing_records() -> None:
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "1" * 16,
                                "startTimeUnixNano": "1788264000000000000",
                                "attributes": [
                                    {"key": "db.system", "value": {"stringValue": "postgresql"}}
                                ],
                            },
                            {
                                "traceId": "b" * 32,
                                "startTimeUnixNano": "1788264000000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.operation.name",
                                        "value": {"stringValue": "chat"},
                                    }
                                ],
                            },
                        ]
                    }
                ]
            }
        ]
    }

    mapped = map_otlp_payload(payload, _context("otlp"))

    assert [item.skip_reason for item in mapped] == ["non_llm_span", "missing_source_id"]


def test_same_span_id_in_different_source_traces_never_overwrites() -> None:
    def source_span(trace_id: str) -> dict[str, object]:
        return {
            "traceId": trace_id,
            "spanId": "1" * 16,
            "startTimeUnixNano": "1788264000000000000",
            "endTimeUnixNano": "1788264000500000000",
            "attributes": _otel_attributes(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "gpt-4o-mini",
                    "gen_ai.input.messages": '[{"role":"user","content":"hello"}]',
                    "gen_ai.output.messages": '[{"role":"assistant","content":"hi"}]',
                }
            ),
        }

    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {"spans": [source_span("a" * 32), source_span("b" * 32)]}
                ]
            }
        ]
    }

    results = map_otlp_payload(payload, _context("otlp"))

    assert len(results) == 2
    first_trace, second_trace = (result.trace for result in results)
    assert first_trace is not None
    assert second_trace is not None
    assert first_trace.trace_id != second_trace.trace_id


def test_otlp_maps_semantic_kernel_vercel_and_openllmetry_dialects_once() -> None:
    def span(identifier: str, attributes: dict[str, object], **extra) -> dict[str, object]:
        return {
            "traceId": identifier * 32,
            "spanId": identifier * 16,
            "startTimeUnixNano": "1788264000000000000",
            "endTimeUnixNano": "1788264000500000000",
            "attributes": _otel_attributes(attributes),
            **extra,
        }

    semantic_kernel = span(
        "3",
        {
            "gen_ai.operation.name": "chat.completions",
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.response.prompt_tokens": 16,
            "gen_ai.response.completion_tokens": 29,
            "gen_ai.response.finish_reason": "stop",
        },
        events=[
            {
                "name": "gen_ai.content.prompt",
                "attributes": _otel_attributes(
                    {"gen_ai.prompt": '[{"role":"user","content":"Why blue?"}]'}
                ),
            },
            {
                "name": "gen_ai.content.completion",
                "attributes": _otel_attributes(
                    {
                        "gen_ai.completion": (
                            '[{"role":"assistant","content":"Because scattering."}]'
                        )
                    }
                ),
            },
        ],
    )
    vercel_call = span(
        "4",
        {
            "ai.operationId": "ai.generateText.doGenerate",
            "ai.model.provider": "google.vertex.chat",
            "ai.model.id": "gemini-2.5-flash",
            "ai.response.model": "gemini-2.5-flash-002",
            "ai.prompt.messages": '[{"role":"user","content":"Hello"}]',
            "ai.response.text": "Hi there",
            "ai.usage.promptTokens": 2,
            "ai.usage.completionTokens": 5,
            "ai.response.finishReason": "stop",
        },
    )
    vercel_wrapper = span(
        "5",
        {"ai.operationId": "ai.generateText", "ai.prompt": "duplicate wrapper"},
    )
    openllmetry = span(
        "6",
        {
            "traceloop.span.kind": "LLM",
            "llm.request.type": "chat",
            "llm.vendor": "OpenAI",
            "llm.request.model": "gpt-4o-mini",
            "llm.response.model": "gpt-4o-mini-2026-08-01",
            "llm.prompts.0.role": "user",
            "llm.prompts.0.content": "Track it",
            "llm.completions.0.role": "assistant",
            "llm.completions.0.content": "It arrives Friday",
            "llm.usage.prompt_tokens": 9,
            "llm.usage.completion_tokens": 3,
        },
    )
    claude_code = span(
        "7",
        {
            "gen_ai.system": "anthropic",
            "gen_ai.request.model": "claude-opus-4-7",
            "input_tokens": 6,
            "output_tokens": 137,
            "session.id": "claude-session",
        },
        name="claude_code.llm_request",
    )
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            semantic_kernel,
                            vercel_call,
                            vercel_wrapper,
                            openllmetry,
                            claude_code,
                        ]
                    }
                ]
            }
        ]
    }

    results = map_otlp_payload(payload, _context("otlp"))

    assert [result.skip_reason for result in results] == [
        None,
        None,
        "non_llm_span",
        None,
        None,
    ]
    semantic_trace = results[0].trace
    vercel_trace = results[1].trace
    openllmetry_trace = results[3].trace
    assert semantic_trace is not None
    assert semantic_trace.prompt_redacted == "Why blue?"
    assert semantic_trace.response_redacted == "Because scattering."
    assert (semantic_trace.input_tokens, semantic_trace.output_tokens) == (16, 29)
    assert vercel_trace is not None
    assert vercel_trace.provider == "google.vertex.chat"
    assert vercel_trace.request_model == "gemini-2.5-flash"
    assert vercel_trace.response_model == "gemini-2.5-flash-002"
    assert vercel_trace.prompt_redacted == "Hello"
    assert vercel_trace.response_redacted == "Hi there"
    assert openllmetry_trace is not None
    assert openllmetry_trace.prompt_redacted == "Track it"
    assert openllmetry_trace.response_redacted == "It arrives Friday"
    assert (openllmetry_trace.input_tokens, openllmetry_trace.output_tokens) == (9, 3)
    claude_trace = results[4].trace
    assert claude_trace is not None
    assert claude_trace.provider == "anthropic"
    assert claude_trace.request_model == "claude-opus-4-7"
    assert (claude_trace.input_tokens, claude_trace.output_tokens) == (6, 137)
    assert claude_trace.session_id == "claude-session"
    assert claude_trace.prompt_redacted is None
    assert claude_trace.response_redacted is None


def test_langfuse_v2_generation_maps_current_fields() -> None:
    record = {
        "id": "obs-1",
        "traceId": "lf-trace-1",
        "type": "GENERATION",
        "startTime": "2026-08-01T10:00:00Z",
        "endTime": "2026-08-01T10:00:01.250Z",
        "providedModelName": "claude-haiku-4-5",
        "input": '[{"role":"user","content":"Refund order 9"}]',
        "output": '{"role":"assistant","content":"Refund started."}',
        "usageDetails": {"input": 31, "output": 9},
        "costDetails": {"input": 0.0002, "output": 0.0003, "total": 0.0005},
        "sessionId": "lf-session",
        "modelParameters": {"temperature": 0.2, "max_tokens": 200},
        "level": "DEFAULT",
    }

    mapped = map_langfuse_observation(record, _context("langfuse"))

    assert mapped.skip_reason is None
    trace = mapped.trace
    assert trace is not None
    assert trace.provider == "anthropic"
    assert trace.input_tokens == 31
    assert trace.output_tokens == 9
    assert trace.cost_usd == pytest.approx(0.0005)
    assert trace.latency_ms == pytest.approx(1250)
    assert trace.temperature == pytest.approx(0.2)
    assert trace.max_tokens == 200
    assert trace.session_id == "lf-session"


def test_langsmith_llm_run_maps_usage_and_thread_metadata() -> None:
    record = {
        "id": "run-1",
        "trace_id": "ls-trace-1",
        "run_type": "llm",
        "start_time": "2026-08-01T11:00:00Z",
        "end_time": "2026-08-01T11:00:00.800Z",
        "inputs": {"messages": [[{"type": "human", "content": "Track order 10"}]]},
        "outputs": {
            "generations": [
                [{"text": "It arrives Friday.", "generation_info": {"finish_reason": "stop"}}]
            ],
            "llm_output": {"token_usage": {"prompt_tokens": 14, "completion_tokens": 6}},
        },
        "extra": {
            "metadata": {
                "ls_provider": "openai",
                "ls_model_name": "gpt-4o-mini",
                "thread_id": "thread-10",
            }
        },
    }

    mapped = map_langsmith_run(record, _context("langsmith"))

    trace = mapped.trace
    assert mapped.skip_reason is None and trace is not None
    assert trace.provider == "openai"
    assert trace.request_model == "gpt-4o-mini"
    assert trace.input_tokens == 14
    assert trace.output_tokens == 6
    assert trace.session_id == "thread-10"
    assert trace.finish_reason == "stop"


def test_datadog_llm_span_maps_documented_meta_metrics_and_nanoseconds() -> None:
    record = {
        "id": "dd-event-1",
        "type": "span",
        "attributes": {
            "trace_id": "dd-trace-1",
            "span_id": "dd-span-1",
            "timestamp": "2026-08-01T12:00:00Z",
            "duration": 900_000_000,
            "status": "ok",
            "session_id": "dd-session",
            "meta": {
                "span": {"kind": "llm"},
                "model_name": "gemini-2.5-flash",
                "model_provider": "google",
                "input": {"messages": [{"role": "user", "content": "Change address"}]},
                "output": {"messages": [{"role": "assistant", "content": "Address changed"}]},
                "metadata": {"temperature": 0.1, "max_tokens": 128},
            },
            "metrics": {"input_tokens": 22, "output_tokens": 5, "total_cost": 0.0007},
        },
    }

    mapped = map_datadog_span(record, _context("datadog"))

    trace = mapped.trace
    assert mapped.skip_reason is None and trace is not None
    assert trace.provider == "google"
    assert trace.operation == Operation.CHAT
    assert trace.input_tokens == 22
    assert trace.output_tokens == 5
    assert trace.latency_ms == pytest.approx(900)
    assert trace.cost_usd == pytest.approx(0.0007)


def test_phoenix_openinference_span_uses_shared_attribute_semantics() -> None:
    record = {
        "context": {"trace_id": "px-trace-1", "span_id": "px-span-1"},
        "name": "LLM",
        "span_kind": "LLM",
        "start_time": "2026-08-01T13:00:00Z",
        "end_time": "2026-08-01T13:00:00.400Z",
        "attributes": {
            "openinference.span.kind": "LLM",
            "llm.provider": "openai",
            "llm.model_name": "gpt-4o-mini",
            "input.value": '{"messages":[{"role":"user","content":"Hello"}]}',
            "output.value": '{"role":"assistant","content":"Hi"}',
            "llm.token_count.prompt": 7,
            "llm.token_count.completion": 2,
            "session.id": "px-session",
        },
        "status_code": "OK",
    }

    mapped = map_phoenix_span(record, _context("phoenix"))

    trace = mapped.trace
    assert mapped.skip_reason is None and trace is not None
    assert trace.input_tokens == 7
    assert trace.output_tokens == 2
    assert trace.session_id == "px-session"
    assert trace.latency_ms == pytest.approx(400)


def test_opik_llm_span_maps_native_fields_without_requiring_trace_parent() -> None:
    record = {
        "id": "opik-span-1",
        "trace_id": "opik-trace-1",
        "type": "llm",
        "start_time": "2026-08-01T14:00:00Z",
        "end_time": "2026-08-01T14:00:00.650Z",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "input": {"messages": [{"role": "user", "content": "Invoice copy"}]},
        "output": {
            "choices": [
                {"message": {"role": "assistant", "content": "Sent"}, "finish_reason": "stop"}
            ]
        },
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        "metadata": {"session_id": "opik-session", "temperature": 0.0},
        "total_estimated_cost": 0.0004,
    }

    mapped = map_opik_span(record, _context("opik"))

    trace = mapped.trace
    assert mapped.skip_reason is None and trace is not None
    assert trace.input_tokens == 12
    assert trace.output_tokens == 3
    assert trace.finish_reason == "stop"
    assert trace.session_id == "opik-session"
    assert trace.cost_usd == pytest.approx(0.0004)


def test_mlflow_3_trace_selects_llm_span_and_preserves_request_time_duration() -> None:
    record = {
        "info": {
            "trace_id": "mlflow-trace-1",
            "request_time": "2026-08-01T15:00:00Z",
            "execution_duration": 700,
            "state": "OK",
            "trace_metadata": {"mlflow.trace.session": "ml-session"},
        },
        "data": {
            "spans": [
                {
                    "span_id": "mlflow-span-1",
                    "span_type": "LLM",
                    "start_time": "2026-08-01T15:00:00Z",
                    "end_time": "2026-08-01T15:00:00.700Z",
                    "inputs": {"messages": [{"role": "user", "content": "Reset password"}]},
                    "outputs": {
                        "choices": [{"message": {"role": "assistant", "content": "Link sent"}}]
                    },
                    "attributes": {
                        "model": "gpt-4o-mini",
                        "provider": "openai",
                        "usage": {"prompt_tokens": 19, "completion_tokens": 4},
                    },
                }
            ]
        },
    }

    mapped = map_mlflow_trace(record, _context("mlflow"))

    trace = mapped.trace
    assert mapped.skip_reason is None and trace is not None
    assert trace.input_tokens == 19
    assert trace.output_tokens == 4
    assert trace.latency_ms == pytest.approx(700)
    assert trace.session_id == "ml-session"


def test_mlflow_current_export_maps_otel_context_nanoseconds_and_json_attributes() -> None:
    record = {
        "info": {
            "trace_id": "tr-123",
            "request_time": 1_785_591_000_000,
            "execution_duration": 900,
            "trace_metadata": {"mlflow.trace.session": '"ml-current"'},
        },
        "data": {
            "spans": [
                {
                    "context": {"trace_id": "0xabc", "span_id": "0xdef"},
                    "start_time_unix_nano": 1_785_591_000_000_000_000,
                    "end_time_unix_nano": 1_785_591_000_250_000_000,
                    "status": {"code": "OK", "message": ""},
                    "attributes": {
                        "mlflow.spanType": '"CHAT_MODEL"',
                        "mlflow.spanInputs": '{"messages":[{"role":"user","content":"Hi"}]}',
                        "mlflow.spanOutputs": '{"role":"assistant","content":"Hello"}',
                        "mlflow.llm.provider": "openai",
                        "mlflow.llm.model": "gpt-4o-mini",
                        "mlflow.chat.tokenUsage": (
                            '{"input_tokens":11,"output_tokens":4,"total_tokens":15}'
                        ),
                        "mlflow.llm.cost": '{"total_cost":0.0003}',
                    },
                },
                {
                    "context": {"trace_id": "0xabc", "span_id": "0xaaa"},
                    "start_time_unix_nano": 1_785_591_000_300_000_000,
                    "end_time_unix_nano": 1_785_591_000_400_000_000,
                    "attributes": {"mlflow.spanType": '"TOOL"'},
                },
            ]
        },
    }

    results = map_mlflow_traces(record, _context("mlflow"))

    assert len(results) == 2
    assert results[1].skip_reason == "non_llm_span"
    trace = results[0].trace
    assert trace is not None
    assert trace.started_at == datetime(2026, 8, 1, 13, 30, tzinfo=UTC)
    assert trace.latency_ms == pytest.approx(250)
    assert trace.prompt_redacted == "Hi"
    assert trace.response_redacted == "Hello"
    assert trace.input_tokens == 11
    assert trace.output_tokens == 4
    assert trace.cost_usd == pytest.approx(0.0003)
    assert trace.session_id == "ml-current"


def test_zero_values_are_preserved_and_do_not_fall_through() -> None:
    langfuse = map_langfuse_observation(
        {
            "id": "zero-langfuse",
            "type": "GENERATION",
            "startTime": "2026-08-01T15:00:00Z",
            "latency": 0,
            "costDetails": {"total": 0},
            "totalCost": 99,
            "usageDetails": {"input": 0, "output": 0},
        },
        _context("langfuse"),
    ).trace
    opik = map_opik_span(
        {
            "id": "zero-opik",
            "type": "llm",
            "start_time": "2026-08-01T15:00:00Z",
            "end_time": "2026-08-01T15:00:00Z",
            "total_estimated_cost": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_cost": 99},
        },
        _context("opik"),
    ).trace
    mlflow = map_mlflow_trace(
        {
            "info": {
                "trace_id": "tr-zero",
                "request_time": 1_785_586_400_000,
                "execution_duration": 0,
            },
            "data": {
                "spans": [
                    {
                        "span_id": "span-zero",
                        "span_type": "LLM",
                        "attributes": {"usage": {"input_tokens": 0, "output_tokens": 0}},
                    }
                ]
            },
        },
        _context("mlflow"),
    ).trace

    assert langfuse is not None and opik is not None and mlflow is not None
    assert (langfuse.cost_usd, langfuse.latency_ms) == (0.0, 0.0)
    assert opik.cost_usd == 0.0
    assert mlflow.latency_ms == 0.0
    assert all(
        trace.input_tokens == 0 and trace.output_tokens == 0 for trace in (langfuse, opik, mlflow)
    )


def test_phoenix_top_level_error_status_is_preserved() -> None:
    mapped = map_phoenix_span(
        {
            "context": {"trace_id": "a" * 32, "span_id": "b" * 16},
            "span_kind": "LLM",
            "start_time": "2026-08-01T15:00:00Z",
            "attributes": {"openinference.span.kind": "LLM"},
            "status_code": "ERROR",
            "status_message": "provider timeout",
        },
        _context("phoenix"),
    )

    assert mapped.trace is not None
    assert mapped.trace.error == "provider timeout"


def test_sparse_trace_keeps_unknown_optional_values_unknown() -> None:
    mapped = map_langfuse_observation(
        {
            "id": "sparse",
            "type": "GENERATION",
            "startTime": "2026-08-01T15:00:00Z",
            "input": "Question",
            "output": "Answer",
        },
        _context("langfuse"),
    )

    assert mapped.trace is not None
    assert mapped.trace.input_tokens is None
    assert mapped.trace.output_tokens is None
    assert mapped.trace.latency_ms is None
    assert mapped.trace.cost_usd is None


def test_verbose_content_and_source_identifiers_are_bounded() -> None:
    mapped = map_langfuse_observation(
        {
            "id": "bounded",
            "type": "GENERATION",
            "startTime": "2026-08-01T15:00:00Z",
            "input": [{"role": "user", "content": "x" * 200_000}] * 2_000,
            "output": "y" * 200_000,
        },
        _context("langfuse"),
    )
    oversized_id = map_langfuse_observation(
        {
            "id": "z" * 1_025,
            "type": "GENERATION",
            "startTime": "2026-08-01T15:00:00Z",
        },
        _context("langfuse"),
    )
    invalid_unicode_id = map_langfuse_observation(
        {
            "id": "bad\ud800",
            "type": "GENERATION",
            "startTime": "2026-08-01T15:00:00Z",
        },
        _context("langfuse"),
    )

    assert mapped.trace is not None
    assert len(mapped.trace.prompt_redacted or "") == 100_000
    assert len(mapped.trace.response_redacted or "") == 100_000
    assert sum(len(item["content"]) for item in mapped.trace.raw_messages or []) == 200_000
    assert oversized_id.skip_reason == "invalid_source_id"
    assert invalid_unicode_id.skip_reason == "invalid_source_id"


def test_voice_conversation_emits_only_completed_assistant_turns_and_never_audio() -> None:
    record = {
        "conversation_id": "voice-conversation-1",
        "turns": [
            {
                "id": "v1",
                "speaker": "caller",
                "started_at": "2026-08-01T16:00:00Z",
                "ended_at": "2026-08-01T16:00:01Z",
                "text": "I need help",
                "audio_url": "https://secret.invalid/a.wav",
            },
            {
                "id": "v2",
                "speaker": "agent",
                "started_at": "2026-08-01T16:00:01Z",
                "ended_at": "2026-08-01T16:00:02Z",
                "text": "How can I help?",
                "status": "completed",
            },
            {
                "id": "v3",
                "speaker": "caller",
                "started_at": "2026-08-01T16:00:03Z",
                "ended_at": "2026-08-01T16:00:04Z",
                "text": "Cancel it",
            },
            {
                "id": "v4",
                "speaker": "agent",
                "started_at": "2026-08-01T16:00:04Z",
                "text": "One moment",
                "status": "interrupted",
                "audio_base64": "canary-audio",
            },
        ],
    }

    mapped = map_voice_conversation(record, _context("voice"))

    assert len(mapped) == 2
    assert mapped[0].skip_reason is None
    assert mapped[0].trace is not None
    assert mapped[0].trace.session_id == "voice-conversation-1"
    assert mapped[0].trace.raw_messages == [
        {"role": "user", "content": "I need help"},
        {"role": "assistant", "content": "How can I help?"},
    ]
    assert mapped[1].skip_reason == "incomplete_assistant_turn"
    assert "secret.invalid" not in repr(mapped)
    assert "canary-audio" not in repr(mapped)


def test_voice_conversation_reports_bounded_turn_limit() -> None:
    turns = [
        {
            "id": f"voice-{index}",
            "speaker": "caller",
            "started_at": "2026-08-01T16:00:00Z",
            "text": "hello",
        }
        for index in range(1_001)
    ]

    mapped = map_voice_conversation(
        {"conversation_id": "voice-bounded", "turns": turns}, _context("voice")
    )

    assert len(mapped) == 1
    assert mapped[0].skip_reason == "conversation_turn_limit"


@pytest.mark.parametrize(
    "adapter", ["otlp", "langfuse", "langsmith", "datadog", "phoenix", "opik", "mlflow", "voice"]
)
def test_import_context_identity_is_repeatable_and_source_scoped(adapter: str) -> None:
    context = _context(adapter)

    assert context.trace_id("external-1") == context.trace_id("external-1")
    assert context.trace_id("external-1") != _context("different").trace_id("external-1")
    assert context.trace_id("external-1", "trace-a") != context.trace_id(
        "external-1", "trace-b"
    )
    assert context.trace_id("b\0c", "a") != context.trace_id("c", "a\0b")
    other_tenant = ImportContext(adapter=adapter, source_scope="project-a", tenant_id="tenant-b")
    assert context.trace_id("external-1") != other_tenant.trace_id("external-1")
    assert len(context.trace_id("external-1")) == 32
