"""Verdict — open-source LLM observability & drift detection.

Five-line install pattern:

    import verdict
    verdict.init(storage="sqlite:///./verdict.db")

    # ...your existing Anthropic/OpenAI code...
    # Verdict transparently captures supported provider SDK calls.
"""

from __future__ import annotations

from verdict.client import VerdictClient, init
from verdict.schema import DriftSignal, Judgment, Trace
from verdict.signals import record_user_signal
from verdict.trace import (
    clear_context,
    current_span,
    set_context,
    span,
    trace,
    trace_context,
)

__version__ = "0.1.0a3"

__all__ = [
    "DriftSignal",
    "Judgment",
    "Trace",
    "VerdictClient",
    "__version__",
    "clear_context",
    "current_span",
    "init",
    "record_user_signal",
    "set_context",
    "span",
    "trace",
    "trace_context",
]
