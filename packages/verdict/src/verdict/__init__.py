"""Verdict — open-source LLM observability & drift detection.

Five-line install pattern:

    import verdict
    verdict.init(storage="sqlite:///./verdict.db")

    # ...your existing Anthropic/OpenAI code...
    # Verdict transparently captures every LLM call.
"""

from __future__ import annotations

from verdict.client import VerdictClient, init
from verdict.schema import DriftSignal, Judgment, Trace
from verdict.signals import record_user_signal
from verdict.trace import current_span, trace

__version__ = "0.1.0a2"

__all__ = [
    "DriftSignal",
    "Judgment",
    "Trace",
    "VerdictClient",
    "__version__",
    "current_span",
    "init",
    "record_user_signal",
    "trace",
]
