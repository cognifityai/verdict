"""Regression tests for the `_is_wrapped` double-wrap guard.

THE BUG THIS PINS: the guard used to check `hasattr(method, "__wrapped__")`.
Provider SDK methods (Anthropic `Messages.create`, OpenAI `Completions.create`)
carry a NATIVE `__wrapped__` from their own decorators, so on a fresh,
never-wrapped method the guard returned True and install SKIPPED wrapping —
silently breaking capture entirely on every current SDK. The fix: detect OUR
wrapt wrapper, not SDK-native decorators and not unrelated wrapt wrappers.
Current wrapt exposes class-level method patches as BoundFunctionWrapper.

Note: these test the guard LOGIC. The DEFINITIVE check that capture actually
works on a fresh install is `scripts/live_capture_check.py` with real keys.
"""

from __future__ import annotations

import pytest

wrapt = pytest.importorskip("wrapt")

from verdict.instrumentors import anthropic as anthropic_instr  # noqa: E402
from verdict.instrumentors import google as google_instr  # noqa: E402
from verdict.instrumentors import openai as openai_instr  # noqa: E402


def _fresh_module_with_native_wrapped():
    """A stand-in module whose method carries a NATIVE __wrapped__ (like the SDKs)
    but was never wrapped by wrapt."""

    def create(self=None):
        return "real"

    create.__wrapped__ = object()  # what Anthropic/OpenAI SDK methods have natively

    class Messages:
        pass

    Messages.create = create

    class Mod:
        pass

    Mod.Messages = Messages
    Mod.Completions = Messages
    return Mod


def test_native_dunder_wrapped_is_NOT_treated_as_wrapped_anthropic():
    mod = _fresh_module_with_native_wrapped()
    # The whole bug: this must be False so install actually wraps.
    assert anthropic_instr._is_wrapped(mod, "Messages", "create") is False


def test_native_dunder_wrapped_is_NOT_treated_as_wrapped_openai():
    mod = _fresh_module_with_native_wrapped()
    assert openai_instr._is_wrapped(mod, "Completions", "create") is False


def test_native_dunder_wrapped_is_NOT_treated_as_wrapped_google():
    mod = _fresh_module_with_native_wrapped()
    # google's _is_wrapped takes the class directly.
    assert google_instr._is_wrapped(mod.Messages, "create") is False


def test_wrapt_wrapped_method_IS_detected_anthropic():
    """After a real wrapt wrap, the guard must return True (so re-init doesn't
    double-wrap)."""
    from verdict.instrumentors.anthropic import AnthropicInstrumentor

    def create(self=None):
        return "real"

    class Messages:
        pass

    Messages.create = create

    class Mod:
        pass

    Mod.Messages = Messages

    instr = object.__new__(AnthropicInstrumentor)
    wrapt.wrap_function_wrapper(Mod.Messages, "create", instr._wrap_create_sync)
    assert anthropic_instr._is_wrapped(Mod, "Messages", "create") is True


def test_real_anthropic_install_wraps_and_restores_complete_supported_surface():
    pytest.importorskip("anthropic")
    from verdict.client import VerdictClient
    from verdict.instrumentors.anthropic import AnthropicInstrumentor
    from verdict.instrumentors.base import is_verdict_wrapt_wrapper
    from verdict.storage.memory import InMemoryStorage

    mod, _module_path = anthropic_instr._message_resource_module()
    surface = (
        ("Messages", "create"),
        ("Messages", "stream"),
        ("AsyncMessages", "create"),
        ("AsyncMessages", "stream"),
    )
    originals = {
        (cls_name, method): getattr(getattr(mod, cls_name), method) for cls_name, method in surface
    }
    instrumentor = AnthropicInstrumentor(VerdictClient(storage=InMemoryStorage()))

    instrumentor.install()
    instrumentor.install()
    try:
        for cls_name, method in surface:
            wrapped = getattr(getattr(mod, cls_name), method)
            assert is_verdict_wrapt_wrapper(wrapped, owner=instrumentor)
            assert wrapped.__wrapped__ is originals[(cls_name, method)]
    finally:
        instrumentor.uninstall()
        instrumentor.uninstall()

    for cls_name, method in surface:
        assert getattr(getattr(mod, cls_name), method) is originals[(cls_name, method)]


def test_real_openai_install_wraps_and_restores_complete_supported_surface():
    pytest.importorskip("openai")
    import openai.resources.chat.completions as chat_mod
    from verdict.client import VerdictClient
    from verdict.instrumentors.base import is_verdict_wrapt_wrapper
    from verdict.instrumentors.openai import OpenAIInstrumentor
    from verdict.storage.memory import InMemoryStorage

    resources = [
        (chat_mod, cls_name, method)
        for cls_name, method in (
            ("Completions", "create"),
            ("Completions", "stream"),
            ("AsyncCompletions", "create"),
            ("AsyncCompletions", "stream"),
        )
        if hasattr(getattr(chat_mod, cls_name, None), method)
    ]
    responses_resource = openai_instr._responses_resource_module()
    if responses_resource is not None:
        import openai._base_client as base_client_mod

        resources.extend(
            (base_client_mod, cls_name, "request")
            for cls_name in ("SyncAPIClient", "AsyncAPIClient")
            if hasattr(getattr(base_client_mod, cls_name, None), "request")
        )
        for http_module in openai_instr._response_http_modules(base_client_mod):
            resources.extend(
                (http_module, cls_name, "_send_single_request")
                for cls_name in ("Client", "AsyncClient")
                if hasattr(
                    getattr(http_module, cls_name, None),
                    "_send_single_request",
                )
            )
        responses_mod, _module_path = responses_resource
        resources.extend(
            (responses_mod, cls_name, method)
            for cls_name, method in (
                ("Responses", "create"),
                ("Responses", "parse"),
                ("Responses", "retrieve"),
                ("Responses", "stream"),
                ("AsyncResponses", "create"),
                ("AsyncResponses", "parse"),
                ("AsyncResponses", "retrieve"),
                ("AsyncResponses", "stream"),
            )
            if hasattr(getattr(responses_mod, cls_name, None), method)
        )
    originals = {
        (id(mod), cls_name, method): getattr(getattr(mod, cls_name), method)
        for mod, cls_name, method in resources
    }
    instrumentor = OpenAIInstrumentor(VerdictClient(storage=InMemoryStorage()))

    instrumentor.install()
    instrumentor.install()
    try:
        for mod, cls_name, method in resources:
            wrapped = getattr(getattr(mod, cls_name), method)
            assert is_verdict_wrapt_wrapper(wrapped, owner=instrumentor)
            assert wrapped.__wrapped__ is originals[(id(mod), cls_name, method)]
    finally:
        instrumentor.uninstall()
        instrumentor.uninstall()

    for mod, cls_name, method in resources:
        assert getattr(getattr(mod, cls_name), method) is originals[(id(mod), cls_name, method)]
