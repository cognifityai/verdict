#!/usr/bin/env python3
"""Generate deterministic, source-shaped telemetry for full local demos.

The output is synthetic and contains no customer data. Each source receives the
same number of baseline/current LLM calls so Verdict's existing stratified
sampler can reach its statistical floor without favoring one adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid5

SOURCES = (
    "otlp",
    "langfuse",
    "langsmith",
    "datadog",
    "phoenix",
    "opik",
    "mlflow",
    "voice",
)
PROMPT = "Where is my order?"
RESPONSE = "Your order arrives Friday."
_SAMPLE_NAMESPACE = UUID("756914d7-359e-5cab-8ea7-cad3bf981d70")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ns(value: datetime) -> str:
    return str(int(value.timestamp()) * 1_000_000_000 + value.microsecond * 1_000)


def _hex_id(identifier: str, length: int) -> str:
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:length]


def _uuid_id(identifier: str) -> str:
    return str(uuid5(_SAMPLE_NAMESPACE, identifier))


def _decimal_id(identifier: str) -> str:
    return str(int(_hex_id(identifier, 16), 16))


def _otlp(identifier: str, started: datetime, source_index: int) -> dict:
    ended = started + timedelta(milliseconds=500 + source_index * 10)
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": _hex_id(f"{identifier}:trace", 32),
                                "spanId": _hex_id(f"{identifier}:span", 16),
                                "name": "chat gpt-4o-mini",
                                "startTimeUnixNano": _ns(started),
                                "endTimeUnixNano": _ns(ended),
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
                                        "key": "gen_ai.usage.input_tokens",
                                        "value": {"intValue": str(20 + source_index)},
                                    },
                                    {
                                        "key": "gen_ai.usage.output_tokens",
                                        "value": {"intValue": str(8 + source_index)},
                                    },
                                    {
                                        "key": "gen_ai.input.messages",
                                        "value": {
                                            "stringValue": json.dumps(
                                                [{"role": "user", "content": PROMPT}]
                                            )
                                        },
                                    },
                                    {
                                        "key": "gen_ai.output.messages",
                                        "value": {
                                            "stringValue": json.dumps(
                                                [{"role": "assistant", "content": RESPONSE}]
                                            )
                                        },
                                    },
                                    {
                                        "key": "gen_ai.conversation.id",
                                        "value": {"stringValue": identifier},
                                    },
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


def _langfuse(identifier: str, started: datetime, source_index: int) -> dict:
    return {
        "id": _uuid_id(f"{identifier}:observation"),
        "traceId": _uuid_id(f"{identifier}:trace"),
        "type": "GENERATION",
        "startTime": _iso(started),
        "endTime": _iso(started + timedelta(milliseconds=500 + source_index * 10)),
        "providedModelName": "gpt-4o-mini",
        "input": [{"role": "user", "content": PROMPT}],
        "output": {"role": "assistant", "content": RESPONSE},
        "usageDetails": {"input": 20 + source_index, "output": 8 + source_index},
        "costDetails": {"total": round(0.0001 * (source_index + 1), 6)},
        "sessionId": identifier,
    }


def _langsmith(identifier: str, started: datetime, source_index: int) -> dict:
    return {
        "id": _uuid_id(f"{identifier}:run"),
        "trace_id": _uuid_id(f"{identifier}:trace"),
        "run_type": "llm",
        "start_time": _iso(started),
        "end_time": _iso(started + timedelta(milliseconds=500 + source_index * 10)),
        "inputs": {"messages": [[{"type": "human", "content": PROMPT}]]},
        "outputs": {
            "generations": [[{"text": RESPONSE, "generation_info": {"finish_reason": "stop"}}]],
            "llm_output": {
                "token_usage": {
                    "prompt_tokens": 20 + source_index,
                    "completion_tokens": 8 + source_index,
                }
            },
        },
        "extra": {
            "metadata": {
                "ls_provider": "openai",
                "ls_model_name": "gpt-4o-mini",
                "thread_id": identifier,
            }
        },
    }


def _datadog(identifier: str, started: datetime, source_index: int) -> dict:
    return {
        "id": f"event-{identifier}",
        "type": "span",
        "attributes": {
            "trace_id": _decimal_id(f"{identifier}:trace"),
            "span_id": _decimal_id(f"{identifier}:span"),
            "timestamp": _iso(started),
            "duration": (500 + source_index * 10) * 1_000_000,
            "status": "ok",
            "session_id": identifier,
            "meta": {
                "span": {"kind": "llm"},
                "model_name": "gpt-4o-mini",
                "model_provider": "openai",
                "input": {"messages": [{"role": "user", "content": PROMPT}]},
                "output": {"messages": [{"role": "assistant", "content": RESPONSE}]},
            },
            "metrics": {
                "input_tokens": 20 + source_index,
                "output_tokens": 8 + source_index,
                "total_cost": round(0.0001 * (source_index + 1), 6),
            },
        },
    }


def _phoenix(identifier: str, started: datetime, source_index: int) -> dict:
    return {
        "context": {
            "trace_id": _hex_id(f"{identifier}:trace", 32),
            "span_id": _hex_id(f"{identifier}:span", 16),
        },
        "name": "LLM",
        "span_kind": "LLM",
        "start_time": _iso(started),
        "end_time": _iso(started + timedelta(milliseconds=500 + source_index * 10)),
        "attributes": {
            "openinference.span.kind": "LLM",
            "llm.provider": "openai",
            "llm.model_name": "gpt-4o-mini",
            "input.value": {"messages": [{"role": "user", "content": PROMPT}]},
            "output.value": {"role": "assistant", "content": RESPONSE},
            "llm.token_count.prompt": 20 + source_index,
            "llm.token_count.completion": 8 + source_index,
            "session.id": identifier,
        },
        "status_code": "OK",
    }


def _opik(identifier: str, started: datetime, source_index: int) -> dict:
    return {
        "id": _uuid_id(f"{identifier}:span"),
        "trace_id": _uuid_id(f"{identifier}:trace"),
        "type": "llm",
        "start_time": _iso(started),
        "end_time": _iso(started + timedelta(milliseconds=500 + source_index * 10)),
        "provider": "openai",
        "model": "gpt-4o-mini",
        "input": {"messages": [{"role": "user", "content": PROMPT}]},
        "output": {
            "choices": [
                {"message": {"role": "assistant", "content": RESPONSE}, "finish_reason": "stop"}
            ]
        },
        "usage": {"prompt_tokens": 20 + source_index, "completion_tokens": 8 + source_index},
        "metadata": {"session_id": identifier},
        "total_estimated_cost": round(0.0001 * (source_index + 1), 6),
    }


def _mlflow(identifier: str, started: datetime, source_index: int) -> dict:
    duration = 500 + source_index * 10
    return {
        "info": {
            "trace_id": f"tr-{_hex_id(f'{identifier}:trace', 32)}",
            "request_time": int(started.timestamp() * 1000),
            "execution_duration": duration,
            "state": "OK",
            "trace_metadata": {"mlflow.trace.session": identifier},
        },
        "data": {
            "spans": [
                {
                    "context": {
                        "trace_id": f"0x{_hex_id(f'{identifier}:otel-trace', 32)}",
                        "span_id": f"0x{_hex_id(f'{identifier}:span', 16)}",
                    },
                    "start_time_unix_nano": int(_ns(started)),
                    "end_time_unix_nano": int(_ns(started + timedelta(milliseconds=duration))),
                    "status_code": "OK",
                    "attributes": {
                        "mlflow.spanType": '"LLM"',
                        "mlflow.spanInputs": json.dumps(
                            {"messages": [{"role": "user", "content": PROMPT}]}
                        ),
                        "mlflow.spanOutputs": json.dumps(
                            {"choices": [{"message": {"role": "assistant", "content": RESPONSE}}]}
                        ),
                        "mlflow.llm.provider": "openai",
                        "mlflow.llm.model": "gpt-4o-mini",
                        "mlflow.chat.tokenUsage": json.dumps(
                            {
                                "input_tokens": 20 + source_index,
                                "output_tokens": 8 + source_index,
                                "total_tokens": 28 + source_index * 2,
                            }
                        ),
                    },
                }
            ]
        },
    }


def _voice(identifier: str, started: datetime, source_index: int) -> dict:
    duration = 500 + source_index * 10
    return {
        "conversation_id": identifier,
        "provider": "voice-agent",
        "model": "realtime-model",
        "turns": [
            {
                "id": f"{identifier}-user",
                "speaker": "caller",
                "started_at": _iso(started - timedelta(seconds=1)),
                "ended_at": _iso(started),
                "text": PROMPT,
            },
            {
                "id": identifier,
                "speaker": "agent",
                "started_at": _iso(started),
                "ended_at": _iso(started + timedelta(milliseconds=duration)),
                "text": RESPONSE,
                "status": "completed",
                "input_tokens": 20 + source_index,
                "output_tokens": 8 + source_index,
                "cost_usd": round(0.0001 * (source_index + 1), 6),
            },
        ],
    }


BUILDERS: dict[str, Callable[[str, datetime, int], dict]] = {
    "otlp": _otlp,
    "langfuse": _langfuse,
    "langsmith": _langsmith,
    "datadog": _datadog,
    "phoenix": _phoenix,
    "opik": _opik,
    "mlflow": _mlflow,
    "voice": _voice,
}


def _atomic_jsonl(path: Path, rows: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    os.replace(temporary, path)


def generate_samples(output: Path, *, as_of: datetime, per_source_window: int) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    if not 1 <= per_source_window <= 10_000:
        raise ValueError("per_source_window must be in [1,10000]")
    output.mkdir(parents=True, exist_ok=True)
    expected_input = 0
    expected_output = 0
    expected_latency: dict[str, float] = {}
    files: dict[str, str] = {}
    for source_index, source in enumerate(SOURCES):
        rows: list[dict] = []
        for window, anchor in (
            ("baseline", as_of - timedelta(days=3)),
            ("current", as_of - timedelta(hours=12)),
        ):
            for index in range(per_source_window):
                identifier = f"{source}-{window}-{index:05d}"
                rows.append(
                    BUILDERS[source](identifier, anchor + timedelta(seconds=index), source_index)
                )
                expected_input += 20 + source_index
                expected_output += 8 + source_index
        filename = f"{source}.jsonl"
        _atomic_jsonl(output / filename, rows)
        files[source] = filename
        expected_latency[source] = 500 + source_index * 10
    manifest = {
        "evidence": "synthetic_contract",
        "as_of": _iso(as_of),
        "per_source_window": per_source_window,
        "files": files,
        "expected": {
            "traces": len(SOURCES) * per_source_window * 2,
            "input_tokens": expected_input,
            "output_tokens": expected_output,
            "latency_ms_by_source": expected_latency,
        },
    }
    temporary = output / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output / "manifest.json")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", default="2026-08-26T12:00:00Z")
    parser.add_argument("--per-source-window", type=int, default=5)
    args = parser.parse_args()
    try:
        as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        manifest = generate_samples(
            args.output,
            as_of=as_of,
            per_source_window=args.per_source_window,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
