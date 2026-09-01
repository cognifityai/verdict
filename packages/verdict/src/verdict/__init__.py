"""Verdict — open-source LLM observability & drift detection.

Five-line install pattern:

    import verdict
    verdict.init(storage="sqlite:///./verdict.db")

    # ...your existing Anthropic/OpenAI code...
    # Verdict transparently captures supported provider SDK calls.
"""

from __future__ import annotations

from verdict.client import VerdictClient, init
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
    agent_run_bundle_from_json,
    agent_run_bundle_to_json,
    stable_evidence_id,
)
from verdict.schema import DriftRun, DriftSignal, Judgment, Trace
from verdict.signals import record_user_signal
from verdict.trace import (
    clear_context,
    current_span,
    intent_context,
    set_context,
    span,
    trace,
    trace_context,
    workload_context,
)

__version__ = "0.1.0a13"

__all__ = [
    "AgentEvent",
    "AgentEventType",
    "AgentRun",
    "AgentRunBundle",
    "AgentTurn",
    "DriftRun",
    "DriftSignal",
    "EvidenceState",
    "ExecutionStatus",
    "Judgment",
    "PrivacyClassification",
    "SourceSession",
    "Trace",
    "VerdictClient",
    "__version__",
    "agent_run_bundle_from_json",
    "agent_run_bundle_to_json",
    "clear_context",
    "current_span",
    "init",
    "intent_context",
    "record_user_signal",
    "set_context",
    "span",
    "stable_evidence_id",
    "trace",
    "trace_context",
    "workload_context",
]
