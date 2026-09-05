"""Unit tests for the pure logic of the Verdict instrumentors.

These tests deliberately avoid importing any provider SDK (anthropic / openai /
google) so they run in environments where those packages aren't installed. They
exercise the shared, side-effect-free helpers and the error-vs-sampling control
flow rule directly.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest
from verdict.instrumentors.base import decide_persist, normalize_finish_reason

# ---------------------------------------------------------------------------
# normalize_finish_reason
# ---------------------------------------------------------------------------


class _FakeFinishReason:
    """Stand-in for an enum value whose str() is 'FinishReason.STOP'."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:  # mimic Enum.__str__
        return f"FinishReason.{self.name}"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("stop", "stop"),
        ("STOP", "stop"),
        ("end_turn", "end_turn"),
        ("FinishReason.STOP", "stop"),
        ("FinishReason.MAX_TOKENS", "max_tokens"),
        (_FakeFinishReason("STOP"), "stop"),
        (_FakeFinishReason("SAFETY"), "safety"),
    ],
)
def test_normalize_finish_reason(raw, expected):
    assert normalize_finish_reason(raw) == expected


def test_normalize_finish_reason_strips_only_class_prefix():
    # A dotted plain string that is not enum-shaped still takes the last segment.
    assert normalize_finish_reason("Foo.Bar.BAZ") == "baz"


# ---------------------------------------------------------------------------
# decide_persist truth table (error-vs-sampling control flow)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raised, should_sample, expected",
    [
        # On error: always persist, always marked as error, regardless of sampling.
        (True, True, (True, True)),
        (True, False, (True, True)),
        # On success: persist iff sampled, never an error.
        (False, True, (True, False)),
        (False, False, (False, False)),
    ],
)
def test_decide_persist_truth_table(raised, should_sample, expected):
    assert decide_persist(raised, should_sample) == expected


def test_errors_are_never_dropped_by_sampling():
    # The whole point of fix #1: even with sampling fully off, an error persists.
    should_persist, is_error = decide_persist(raised=True, should_sample=False)
    assert should_persist is True
    assert is_error is True


# ---------------------------------------------------------------------------
# Dedicated-RNG determinism (fix #2)
# ---------------------------------------------------------------------------


def _sample_decisions(rng: random.Random, rate: float, n: int) -> list[bool]:
    return [rng.random() < rate for _ in range(n)]


def test_dedicated_rng_is_deterministic_with_same_seed():
    a = random.Random(1234)
    b = random.Random(1234)
    assert _sample_decisions(a, 0.5, 50) == _sample_decisions(b, 0.5, 50)


def test_dedicated_rng_is_isolated_from_global_seed():
    # An app calling random.seed() must NOT change our dedicated RNG's stream.
    instr_rng = random.Random(42)
    before = _sample_decisions(instr_rng, 0.5, 25)

    # Reset and reseed the *global* RNG the way an unrelated app might.
    instr_rng2 = random.Random(42)
    random.seed(999_999)
    _ = [random.random() for _ in range(100)]  # perturb global state
    after = _sample_decisions(instr_rng2, 0.5, 25)

    assert before == after


def test_module_rngs_match_when_seeded_identically():
    # Two separate instrumentor modules each holding a Random() produce the same
    # decisions if seeded identically -> behavior is reproducible/testable.
    m1 = random.Random()
    m2 = random.Random()
    m1.seed(7)
    m2.seed(7)
    assert _sample_decisions(m1, 0.3, 40) == _sample_decisions(m2, 0.3, 40)


# ---------------------------------------------------------------------------
# OpenAI / Anthropic flatten helpers handle multimodal list content (fix #5)
# ---------------------------------------------------------------------------


def test_openai_flatten_handles_list_content():
    from verdict.instrumentors.openai import _flatten_content

    content = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "http://x"}},
        {"type": "text", "text": "world"},
    ]
    assert _flatten_content(content) == "hello\nworld"
    assert _flatten_content("plain") == "plain"
    assert _flatten_content(None) == ""


def test_openai_input_trace_recursively_redacts_tool_arguments_before_persistence():
    from verdict.client import VerdictClient
    from verdict.instrumentors.openai import OpenAIInstrumentor
    from verdict.storage.memory import InMemoryStorage

    canary = "tool-secret@example.com"
    client = VerdictClient(storage=InMemoryStorage(), capture_content=True)
    instrumentor = OpenAIInstrumentor(client)

    trace = instrumentor._build_input_trace(
        {
            "model": "custom-model",
            "messages": [
                {
                    "role": "assistant",
                    "content": "safe",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": f'{{"email": "{canary}"}}',
                            },
                        }
                    ],
                    "metadata": {"contact": canary},
                }
            ],
        }
    )

    assert canary not in repr(trace.raw_messages)
    assert trace.raw_messages[0]["tool_calls"][0]["function"]["name"] == "lookup"


def test_provider_exception_content_is_redacted_before_storage():
    from verdict.client import VerdictClient
    from verdict.instrumentors.anthropic import AnthropicInstrumentor
    from verdict.storage.memory import InMemoryStorage

    storage = InMemoryStorage()
    instrumentor = AnthropicInstrumentor(VerdictClient(storage=storage))
    canary = "provider-error@example.com"

    def raises(*args, **kwargs):
        raise RuntimeError(f"request failed for {canary}")

    with pytest.raises(RuntimeError, match="request failed"):
        instrumentor._wrap_create_sync(
            raises,
            None,
            (),
            {"model": "claude-test", "max_tokens": 8, "messages": []},
        )

    [trace] = storage.list_traces()
    assert canary not in trace.error
    assert "<EMAIL>" in trace.error


def test_anthropic_flatten_handles_list_content():
    from verdict.instrumentors.anthropic import _flatten_content

    content = [
        {"type": "text", "text": "a"},
        {"type": "image", "source": {}},
        {"type": "text", "text": "b"},
    ]
    assert _flatten_content(content) == "a\nb"


def test_google_contents_to_messages_wraps_user_message():
    from verdict.instrumentors.google import _genai_contents_to_messages

    msgs = _genai_contents_to_messages("hi there")
    assert msgs == [{"role": "user", "content": "hi there"}]
    assert _genai_contents_to_messages(None) == []
    assert _genai_contents_to_messages(["one", "two"]) == [{"role": "user", "content": "one\ntwo"}]


def test_google_contents_to_messages_preserves_roles_and_ordered_text_parts():
    from verdict.instrumentors.google import _genai_contents_to_messages

    class Part:
        def __init__(self, text=None, *, inline_data=None):
            self.text = text
            self.inline_data = inline_data

    class Content:
        def __init__(self, role, parts):
            self.role = role
            self.parts = parts

    messages = _genai_contents_to_messages(
        [
            Content("user", [Part("old"), Part(inline_data=b"ignored")]),
            {"role": "model", "parts": [{"text": "answer"}]},
            Content("user", [Part("new"), Part("question")]),
        ],
        system_instruction={"parts": [{"text": "system policy"}]},
    )

    assert messages == [
        {"role": "system", "content": [{"type": "text", "text": "system policy"}]},
        {"role": "user", "content": [{"type": "text", "text": "old"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "new"},
                {"type": "text", "text": "question"},
            ],
        },
    ]


def test_google_contents_to_messages_matches_sdk_part_grouping():
    from google.genai import types
    from verdict.instrumentors.google import _genai_contents_to_messages

    assert _genai_contents_to_messages([types.Part.from_text(text="hello")]) == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    ]

    messages = _genai_contents_to_messages(
        [
            "first",
            types.Part.from_text(text="question"),
            types.Content(
                role="model",
                parts=[types.Part.from_text(text="answer")],
            ),
            "follow up",
        ]
    )
    assert messages == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "question"},
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        {"role": "user", "content": [{"type": "text", "text": "follow up"}]},
    ]


def test_intent_context_is_token_restoring_and_rejects_redaction_changing_keys():
    from verdict import intent_context
    from verdict.client import get_context_intent_key

    assert get_context_intent_key() is None
    with intent_context("billing.v1"):
        assert get_context_intent_key() == "billing.v1"
        with intent_context("shipping"):
            assert get_context_intent_key() == "shipping"
        assert get_context_intent_key() == "billing.v1"
    assert get_context_intent_key() is None

    with pytest.raises(ValueError, match="redaction-safe"):
        with intent_context("192.168.1.1"):
            pass


# ---------------------------------------------------------------------------
# Wrapper guard regression: SDK-native __wrapped__ is NOT Verdict instrumentation
# ---------------------------------------------------------------------------


def _sdk_native_wrapped(fn):
    def sdk_wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    sdk_wrapper.__wrapped__ = fn
    return sdk_wrapper


class _FakeNativeSDKMethods:
    @_sdk_native_wrapped
    def create(self):
        return None


class _FakeNativeModule:
    Messages = _FakeNativeSDKMethods
    Completions = _FakeNativeSDKMethods


def test_native_sdk_wrapped_attribute_does_not_skip_install_guard():
    """Provider SDK decorators can set __wrapped__; that is not our wrapper."""
    from verdict.instrumentors.anthropic import _is_wrapped as anthropic_is_wrapped
    from verdict.instrumentors.google import _is_wrapped as google_is_wrapped
    from verdict.instrumentors.openai import _is_wrapped as openai_is_wrapped

    assert hasattr(_FakeNativeSDKMethods.create, "__wrapped__")
    assert not anthropic_is_wrapped(_FakeNativeModule, "Messages", "create")
    assert not openai_is_wrapped(_FakeNativeModule, "Completions", "create")
    assert not google_is_wrapped(_FakeNativeSDKMethods, "create")


def test_verdict_wrapt_wrapper_is_detected_for_double_wrap_guard():
    import wrapt
    from verdict.instrumentors.anthropic import AnthropicInstrumentor
    from verdict.instrumentors.anthropic import _is_wrapped as anthropic_is_wrapped
    from verdict.instrumentors.base import is_verdict_wrapt_wrapper
    from verdict.instrumentors.google import _is_wrapped as google_is_wrapped
    from verdict.instrumentors.openai import _is_wrapped as openai_is_wrapped

    class FakeWrappedSDKMethods:
        def create(self):
            return None

    instr = object.__new__(AnthropicInstrumentor)
    FakeWrappedSDKMethods.create = wrapt.FunctionWrapper(
        FakeWrappedSDKMethods.create,
        instr._wrap_create_sync,
    )

    class FakeWrappedModule:
        Messages = FakeWrappedSDKMethods
        Completions = FakeWrappedSDKMethods

    bound = FakeWrappedSDKMethods.create
    assert is_verdict_wrapt_wrapper(bound)
    assert is_verdict_wrapt_wrapper(bound, owner=instr)
    assert not is_verdict_wrapt_wrapper(bound, owner=object())
    assert anthropic_is_wrapped(FakeWrappedModule, "Messages", "create")
    assert openai_is_wrapped(FakeWrappedModule, "Completions", "create")
    assert google_is_wrapped(FakeWrappedSDKMethods, "create")


def test_anthropic_sync_exception_persists_error_trace():
    from verdict.client import VerdictClient
    from verdict.instrumentors.anthropic import AnthropicInstrumentor
    from verdict.storage.memory import InMemoryStorage

    storage = InMemoryStorage()
    instr = AnthropicInstrumentor(VerdictClient(storage=storage))

    def raises(*args, **kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        instr._wrap_create_sync(
            raises,
            None,
            (),
            {
                "model": "claude-test",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    traces = storage.list_traces(limit=1)
    assert len(traces) == 1
    assert traces[0].request_model == "claude-test"
    assert traces[0].error == "RuntimeError: boom"


def test_anthropic_unknown_opus_response_is_persisted_without_a_cost_estimate():
    from verdict.client import VerdictClient
    from verdict.instrumentors.anthropic import AnthropicInstrumentor
    from verdict.storage.memory import InMemoryStorage

    storage = InMemoryStorage()
    instrumentor = AnthropicInstrumentor(VerdictClient(storage=storage))
    response = SimpleNamespace(
        model="claude-opus-4-9-20260901",
        usage=SimpleNamespace(input_tokens=1000, output_tokens=1000),
        stop_reason="end_turn",
        content=[],
    )

    returned = instrumentor._wrap_create_sync(
        lambda *args, **kwargs: response,
        None,
        (),
        {
            "model": "claude-opus-4-9-20260901",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    [trace] = storage.list_traces()
    assert returned is response
    assert trace.request_model == "claude-opus-4-9-20260901"
    assert trace.input_tokens == 1000
    assert trace.output_tokens == 1000
    assert trace.cost_usd is None


def test_anthropic_known_opus_41_response_persists_verified_cost():
    from verdict.client import VerdictClient
    from verdict.instrumentors.anthropic import AnthropicInstrumentor
    from verdict.storage.memory import InMemoryStorage

    storage = InMemoryStorage()
    instrumentor = AnthropicInstrumentor(VerdictClient(storage=storage))
    response = SimpleNamespace(
        model="claude-opus-4-1-20250805",
        usage=SimpleNamespace(input_tokens=1000, output_tokens=1000),
        stop_reason="end_turn",
        content=[],
    )

    returned = instrumentor._wrap_create_sync(
        lambda *args, **kwargs: response,
        None,
        (),
        {
            "model": "claude-opus-4-1-20250805",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    [trace] = storage.list_traces()
    assert returned is response
    assert trace.request_model == "claude-opus-4-1-20250805"
    assert trace.input_tokens == 1000
    assert trace.output_tokens == 1000
    assert trace.cost_usd == pytest.approx(0.09)


# ---------------------------------------------------------------------------
# Integration-ish: control flow against a fake wrapped call + InMemoryStorage,
# without importing any provider SDK. Drives a tiny fake instrumentor that uses
# the same decide_persist rule.
# ---------------------------------------------------------------------------


def test_control_flow_with_inmemory_storage():
    from verdict.schema import Operation, Trace
    from verdict.storage.memory import InMemoryStorage

    storage = InMemoryStorage()

    def run_call(*, raises: bool, sampled: bool) -> None:
        trace = Trace(provider="fake", operation=Operation.CHAT, request_model="m")
        try:
            if raises:
                raise ValueError("boom")
        except Exception as e:  # error path: always persist
            trace.error = f"{type(e).__name__}: {e}"
            storage.insert_trace(trace)
            return
        should_persist, _ = decide_persist(False, sampled)
        if should_persist:
            storage.insert_trace(trace)

    # error -> persisted as error even though not sampled
    run_call(raises=True, sampled=False)
    # success sampled -> persisted, no error
    run_call(raises=False, sampled=True)
    # success not sampled -> dropped
    run_call(raises=False, sampled=False)

    traces = storage.list_traces()
    assert len(traces) == 2
    errors = [t for t in traces if t.error]
    successes = [t for t in traces if not t.error]
    assert len(errors) == 1
    assert len(successes) == 1
    assert errors[0].error == "ValueError: boom"


# ---------------------------------------------------------------------------
# sample_rate validation in init() (fix #1)
# ---------------------------------------------------------------------------


def test_init_rejects_out_of_range_sample_rate():
    import verdict.client as client_mod

    for bad in (-0.1, 1.5, 2.0, -1.0):
        with pytest.raises(ValueError):
            client_mod.init(storage="memory://", sample_rate=bad)
    # No client should have been left initialized by the failed calls.
    assert client_mod.get_client() is None


def test_init_accepts_boundary_sample_rates():
    import verdict.client as client_mod

    for good in (0.0, 0.5, 1.0):
        c = client_mod.init(storage="memory://", sample_rate=good)
        assert c.sample_rate == good
        client_mod.shutdown()


def test_init_skips_optional_sdk_when_availability_check_raises(monkeypatch, caplog):
    import logging

    import verdict.client as client_mod
    from verdict.instrumentors.anthropic import AnthropicInstrumentor

    def broken_availability(_instrumentor):
        raise RuntimeError("private dependency detail")

    client_mod.shutdown()
    monkeypatch.setattr(AnthropicInstrumentor, "available", broken_availability)
    try:
        with caplog.at_level(logging.WARNING, logger="verdict"):
            for _ in range(2):
                client = client_mod.init(
                    storage="memory://", instrumentors=["anthropic"]
                )
                client_mod.shutdown()

        assert client._instrumentors == []
        matching = [
            record for record in caplog.records
            if "anthropic" in record.getMessage().lower()
        ]
        assert len(matching) == 1
        assert "private dependency detail" not in matching[0].getMessage()
    finally:
        client_mod.shutdown()
        client_mod._warned_availability_failures.clear()


# ---------------------------------------------------------------------------
# Routing context: tenant_id / session_id / user_id_hash on a built trace
# (fix #6). Exercised through the shared apply_routing_context helper so we
# don't need a provider SDK.
# ---------------------------------------------------------------------------


def test_apply_routing_context_populates_fields():
    import hashlib
    import hmac

    import verdict.client as client_mod
    from verdict.instrumentors.base import apply_routing_context
    from verdict.schema import Trace

    client_mod.shutdown()  # ensure a clean singleton
    client = client_mod.init(
        storage="memory://",
        tenant_id="tenant-42",
        redaction_mode="hash",
        redaction_secret="s3cret",
    )
    try:
        client_mod.set_context(session_id="sess-1", user_id="user-99")
        trace = Trace()
        apply_routing_context(client, trace)

        assert trace.tenant_id == "tenant-42"
        assert trace.session_id == "sess-1"
        # user id must be HMAC-hashed with the redaction secret, never raw.
        expected = hmac.new(b"s3cret", b"user-99", hashlib.sha256).hexdigest()
        assert trace.user_id_hash == expected
        assert trace.user_id_hash != "user-99"
    finally:
        client_mod.clear_context()
        client_mod.shutdown()


def test_apply_routing_context_no_context_is_clean():
    import verdict.client as client_mod
    from verdict.instrumentors.base import apply_routing_context
    from verdict.schema import Trace

    client_mod.shutdown()
    client = client_mod.init(storage="memory://", tenant_id="t1")
    try:
        client_mod.clear_context()
        trace = Trace()
        apply_routing_context(client, trace)
        assert trace.tenant_id == "t1"
        assert trace.session_id is None
        assert trace.user_id_hash is None
    finally:
        client_mod.shutdown()
