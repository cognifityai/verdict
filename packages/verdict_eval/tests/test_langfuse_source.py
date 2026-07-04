"""Tests for LangfuseSource._observation_to_trace.

These are pure-conversion tests: no network, no real `langfuse` SDK. The
adapter hard-imports `langfuse` in __post_init__, so we deliberately bypass
__init__/__post_init__ (via object.__new__) and set only the redaction config
the conversion path actually reads. The observation is a SimpleNamespace that
mimics a Langfuse "generation" observation.

The regression these guard: `_observation_to_trace` used to be a @staticmethod
whose body referenced `self.redaction_mode`, so every call raised NameError.
It is now a normal instance method that uses the instance's redaction config.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from verdict.schema import Operation, Trace
from verdict_eval.langfuse_source import LangfuseSource


def _make_source(redaction_mode: str = "redact", redaction_secret=None) -> LangfuseSource:
    """Build a LangfuseSource WITHOUT running __post_init__.

    __post_init__ imports and instantiates the real `langfuse` client, which we
    neither have nor want here. _observation_to_trace only reads the redaction
    config off the instance, so that's all we set.
    """
    src = object.__new__(LangfuseSource)  # type: ignore[call-overload]
    src.public_key = "pk-test"
    src.secret_key = "sk-test"
    src.host = "https://example.invalid"
    src.redaction_mode = redaction_mode
    src.redaction_secret = redaction_secret
    return src


def _fake_generation() -> SimpleNamespace:
    """A minimal Langfuse-generation-shaped observation."""
    return SimpleNamespace(
        id="obs-123",
        traceId="trace-abc",
        model="claude-haiku-4-5",
        usage={"input": 100, "output": 42},
        calculatedTotalCost=0.0012,
        input="What is the capital of France?",
        output="The capital of France is Paris.",
        start_time=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
        metadata={},
        userId="tenant-x",
        sessionId="sess-1",
    )


def test_observation_to_trace_basic_fields() -> None:
    src = _make_source()
    trace = src._observation_to_trace(_fake_generation())

    assert isinstance(trace, Trace)
    assert trace.trace_id == "obs-123"
    assert trace.operation == Operation.CHAT
    assert trace.provider == "anthropic"          # inferred from "claude-..." model
    assert trace.request_model == "claude-haiku-4-5"
    assert trace.response_model == "claude-haiku-4-5"
    assert trace.input_tokens == 100
    assert trace.output_tokens == 42
    assert trace.cost_usd == pytest.approx(0.0012)
    assert trace.latency_ms == pytest.approx(1000.0)
    assert trace.tenant_id == "tenant-x"
    assert trace.session_id == "sess-1"
    assert trace.tags == {"source": "langfuse"}


def test_observation_to_trace_uses_instance_redaction() -> None:
    """The method must use self.redaction_* — the bug that made it a
    staticmethod referencing self.redaction_mode raised NameError instead."""
    src = _make_source()
    obs = _fake_generation()
    obs.input = "Email me at alice@example.com please"
    trace = src._observation_to_trace(obs)
    # Default "redact" mode replaces the email with a placeholder.
    assert trace.prompt_redacted is not None
    assert "alice@example.com" not in trace.prompt_redacted
    assert "<EMAIL>" in trace.prompt_redacted


def test_observation_to_trace_hash_mode() -> None:
    """Hash-mode redaction is keyed off the instance's redaction_secret."""
    src = _make_source(redaction_mode="hash", redaction_secret="s3cr3t")
    obs = _fake_generation()
    obs.output = "Reach me: bob@example.com"
    trace = src._observation_to_trace(obs)
    assert trace.response_redacted is not None
    assert "bob@example.com" not in trace.response_redacted
    assert "<EMAIL:" in trace.response_redacted


def test_observation_to_trace_handles_dict_observation() -> None:
    """Dict-shaped observations (raw API payloads) convert too."""
    src = _make_source()
    obs = {
        "id": "d-1",
        "model": "gpt-4o-mini",
        "input": "hi",
        "output": "hello",
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    }
    trace = src._observation_to_trace(obs)
    assert trace.trace_id == "d-1"
    assert trace.provider == "openai"
    assert trace.input_tokens == 3
    assert trace.output_tokens == 5


def test_observation_to_trace_missing_content() -> None:
    """No prompt/response → redacted fields stay None, no crash."""
    src = _make_source()
    obs = SimpleNamespace(id="empty-1", model="", usage={})
    trace = src._observation_to_trace(obs)
    assert trace.trace_id == "empty-1"
    assert trace.prompt_redacted is None
    assert trace.response_redacted is None
