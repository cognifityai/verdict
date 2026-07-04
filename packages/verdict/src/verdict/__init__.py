"""Verdict — open-source LLM observability & drift detection.

Five-line install pattern:

    import verdict
    verdict.init(api_key="vk_...", storage="sqlite:///./verdict.db")

    # ...your existing Anthropic/OpenAI code...
    # Verdict transparently captures every LLM call.
"""

from __future__ import annotations

from verdict.client import VerdictClient, init
from verdict.schema import Trace, Judgment, DriftSignal
from verdict.signals import record_user_signal
from verdict.trace import trace, current_span

__version__ = "0.0.1"

__all__ = [
    "init",
    "record_user_signal",
    "trace",
    "current_span",
    "VerdictClient",
    "Trace",
    "Judgment",
    "DriftSignal",
    "__version__",
]
