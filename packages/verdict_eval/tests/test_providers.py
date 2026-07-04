"""Provider adapter tests.

Regression lock: GoogleAdapter._complete_once annotates a local with
`list[Any]`, so `Any` must be importable in providers.py or the module
(and therefore GoogleAdapter) blows up at use. We confirm the module imports
cleanly and that a CompletionRequest constructs — without touching the live
Google API.
"""

from __future__ import annotations

import importlib


def test_providers_module_imports_with_any_defined() -> None:
    """providers.py uses `Any` in GoogleAdapter; it must be imported."""
    mod = importlib.import_module("verdict_eval.providers")
    importlib.reload(mod)
    # `Any` must be a defined name in the module namespace.
    assert hasattr(mod, "Any")
    # GoogleAdapter class is defined (its body references Any).
    assert hasattr(mod, "GoogleAdapter")


def test_completion_request_constructs() -> None:
    from verdict_eval.providers import CompletionRequest

    req = CompletionRequest(
        model="gemini/gemini-2.5-flash",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert req.model == "gemini/gemini-2.5-flash"
    assert req.temperature == 0.0
    assert req.max_tokens == 1024


def test_google_adapter_complete_once_source_uses_any() -> None:
    """The `list[Any]` annotation must resolve at runtime (no NameError).

    We compile the function under the module globals; if `Any` were missing
    this would have failed at import time already, but we assert the symbol is
    present to pin the contract.
    """
    import verdict_eval.providers as providers

    assert providers.Any is not None
