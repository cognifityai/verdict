"""Core domain schemas — vendor-neutral, no I/O imports.

These types are the lingua franca of Verdict. Every adapter speaks them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

# ---------------------------------------------------------------------------
# OpenTelemetry GenAI semconv attribute name constants
# (gated experimental as of mid-2026 — track open-telemetry/semantic-conventions)
# ---------------------------------------------------------------------------

class GenAIAttr:
    """Stable string constants for OTel GenAI span attributes.

    Pinned here so any rename upstream lands in one place.
    """
    SYSTEM = "gen_ai.system"
    OPERATION_NAME = "gen_ai.operation.name"
    REQUEST_MODEL = "gen_ai.request.model"
    RESPONSE_MODEL = "gen_ai.response.model"
    REQUEST_TEMPERATURE = "gen_ai.request.temperature"
    REQUEST_TOP_P = "gen_ai.request.top_p"
    REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
    RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
    RESPONSE_ID = "gen_ai.response.id"
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"


# ---------------------------------------------------------------------------
# Domain entities
# ---------------------------------------------------------------------------

class Operation(str, Enum):
    """The kind of LLM call captured in a trace."""
    CHAT = "chat"
    TEXT_COMPLETION = "text_completion"
    EMBEDDING = "embeddings"


_OPERATION_ALIASES = {
    "messages.create": Operation.CHAT,
    "chat.completions.create": Operation.CHAT,
    "responses.create": Operation.CHAT,
    "generate_content": Operation.CHAT,
    "models.generate_content": Operation.CHAT,
    "completions.create": Operation.TEXT_COMPLETION,
    "embeddings.create": Operation.EMBEDDING,
    "embed_content": Operation.EMBEDDING,
    "models.embed_content": Operation.EMBEDDING,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid4().hex


@dataclass
class Trace:
    """A single captured LLM request/response pair plus metadata.

    Vendor-neutral. The same Trace shape comes from any provider.
    """

    trace_id: str = field(default_factory=_id)
    started_at: datetime = field(default_factory=_now)
    ended_at: datetime | None = None

    # The minimum useful fields, all provider-neutral
    provider: str = ""                    # "anthropic", "openai", "google", ...
    operation: Operation = Operation.CHAT
    request_model: str = ""
    response_model: str = ""              # often differs after fallback

    # Token accounting (None when streaming hasn't finished accumulating)
    input_tokens: int | None = None
    output_tokens: int | None = None

    # Tunables visible at request time
    temperature: float | None = None
    max_tokens: int | None = None

    # Outcome
    finish_reason: str | None = None
    error: str | None = None
    latency_ms: float | None = None

    # Content (off by default; controlled by client configuration)
    prompt_redacted: str | None = None
    response_redacted: str | None = None
    raw_messages: list[dict[str, Any]] | None = None  # only retained if capture_content=True

    # Routing / observability metadata
    tenant_id: str | None = None
    session_id: str | None = None
    user_id_hash: str | None = None       # HMAC, never raw
    cluster_id: str | None = None         # assigned later by intent clusterer
    tags: dict[str, str] = field(default_factory=dict)

    # Cost (computed by verdict.pricing static pricing table)
    cost_usd: float | None = None

    # The innermost manual span active when this provider call began. Kept last
    # to preserve the positional constructor order of historical Trace fields.
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        """Normalize enum values supplied by manual instrumentation."""
        if isinstance(self.operation, Operation):
            return
        try:
            self.operation = Operation(self.operation)
        except (TypeError, ValueError) as exc:
            alias = _OPERATION_ALIASES.get(str(self.operation).lower())
            if alias is not None:
                self.operation = alias
                return
            supported = ", ".join(op.value for op in Operation)
            raise ValueError(
                f"Unsupported operation {self.operation!r}; expected one of: {supported}"
            ) from exc


# ---------------------------------------------------------------------------
# Judgments — output of the eval engine
# ---------------------------------------------------------------------------

class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNCLEAR = "unclear"


class JudgmentStatus(str, Enum):
    COMPLETED = "completed"
    ERROR = "error"


class EvaluatorHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass
class DimensionScore:
    """Per-dimension binary verdict from a judge."""
    name: str                              # "groundedness", "relevance", ...
    verdict: Verdict
    reasoning: str = ""
    judge_model: str = ""                  # which judge produced this

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, Verdict):
            self.verdict = Verdict(self.verdict)


@dataclass
class Judgment:
    """A complete eval of a single trace by a judge (or judge ensemble)."""

    judgment_id: str = field(default_factory=_id)
    trace_id: str = ""
    rubric_name: str = "default"
    rubric_version: str = "1"
    created_at: datetime = field(default_factory=_now)

    judge_models: list[str] = field(default_factory=list)   # ensemble support
    dimensions: list[DimensionScore] = field(default_factory=list)

    # Keep this in its published 0.1.0a3 positional slot. New fields must be
    # appended after it so existing positional callers cannot silently bind a
    # boolean to evaluator_provider.
    position_swap_consistent: bool | None = None

    # Complete evaluator identity. Historical rows created before these fields
    # were introduced load with empty values and remain explicitly incomplete.
    evaluator_provider: str = ""
    evaluator_config: dict[str, Any] = field(default_factory=dict)
    evaluator_fingerprint: str = ""
    expected_dimensions: list[str] = field(default_factory=list)
    status: JudgmentStatus = JudgmentStatus.COMPLETED
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, JudgmentStatus):
            self.status = JudgmentStatus(self.status)

    @property
    def pass_count(self) -> int:
        return sum(1 for d in self.dimensions if d.verdict == Verdict.PASS)

    @property
    def fail_count(self) -> int:
        return sum(1 for d in self.dimensions if d.verdict == Verdict.FAIL)

    @property
    def pass_rate(self) -> float | None:
        from verdict.metrics import count_scores

        return count_scores(d.verdict for d in self.dimensions).pass_rate

    @property
    def evaluator_identity_complete(self) -> bool:
        return bool(
            self.evaluator_provider
            and self.judge_models
            and self.evaluator_fingerprint
            and self.expected_dimensions
        )


@dataclass
class EvaluatorHealthRecord:
    """Agreement of one evaluator fingerprint against a fixed human anchor set.

    These records are intentionally separate from production judgments and drift
    signals: target-model behavior must never be pooled with judge-health evidence.
    """

    health_id: str = field(default_factory=_id)
    evaluated_at: datetime = field(default_factory=_now)
    evaluator_fingerprint: str = ""
    sentinel_set_name: str = ""
    sentinel_set_fingerprint: str = ""
    correct_examples: int = 0
    total_examples: int = 0
    example_agreement: float | None = None
    example_confidence_low: float | None = None
    example_confidence_high: float | None = None
    correct_labels: int = 0
    total_labels: int = 0
    label_agreement: float | None = None
    status: EvaluatorHealthStatus = EvaluatorHealthStatus.INSUFFICIENT_DATA
    error_count: int = 0
    method_version: str = "2"

    def __post_init__(self) -> None:
        if not isinstance(self.status, EvaluatorHealthStatus):
            self.status = EvaluatorHealthStatus(self.status)


# ---------------------------------------------------------------------------
# Spans — sub-operations within a trace (tool calls, retrieval steps, etc.)
# ---------------------------------------------------------------------------

@dataclass
class SpanRecord:
    """A timed sub-operation captured under a trace.

    Vendor-neutral. Spans nest by parent_name within a trace_id.
    """

    span_id: str = field(default_factory=_id)
    name: str = ""
    trace_id: str | None = None
    parent_name: str | None = None
    started_at: datetime = field(default_factory=_now)
    ended_at: datetime | None = None
    duration_ms: float | None = None
    attributes: dict = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# User signals — implicit/explicit feedback events tied to a trace
# ---------------------------------------------------------------------------

@dataclass
class UserSignalRecord:
    """A single user-feedback event attributed to a trace."""

    signal_id: str = field(default_factory=_id)
    trace_id: str = ""
    kind: str = ""            # thumbs_up, thumbs_down, regenerate, retry, abandon, copy, accept, ...
    created_at: datetime = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Drift signals — output of the drift detector
# ---------------------------------------------------------------------------

class DriftDirection(str, Enum):
    REGRESSION = "regression"
    IMPROVEMENT = "improvement"
    CHANGE = "change"          # statistically different but direction unclear


@dataclass
class DriftRun:
    """One completed, immutable drift-analysis snapshot.

    A run record exists even when no signal clears the gates. That explicit
    zero-signal snapshot is what lets consumers distinguish "no current drift"
    from "the pipeline has not run" and from legacy ungrouped signals.
    """

    run_id: str = field(default_factory=_id)
    analysis_time: datetime = field(default_factory=_now)
    completed_at: datetime = field(default_factory=_now)
    evaluator_fingerprint: str = ""
    signal_count: int = 0

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.evaluator_fingerprint.strip():
            raise ValueError("evaluator_fingerprint must not be empty")
        if self.signal_count < 0:
            raise ValueError("signal_count must not be negative")


@dataclass
class DriftSignal:
    """A statistically significant deviation between current window and baseline.

    One emitted per (cluster, dimension) when the test crosses threshold.
    """

    signal_id: str = field(default_factory=_id)
    detected_at: datetime = field(default_factory=_now)

    cluster_id: str = ""
    dimension: str = ""
    direction: DriftDirection = DriftDirection.CHANGE

    # Statistical details
    statistic_name: str = "mann_whitney_u"
    statistic_value: float = 0.0
    p_value: float = 1.0
    p_value_adjusted: float = 1.0          # Benjamini-Hochberg adjusted

    # Effect size measures — Cliff's delta is the primary (non-parametric, pairs
    # correctly with Mann-Whitney U). Cohen's d kept for backward compatibility
    # but its normality assumption is violated by our binary PASS/FAIL data.
    effect_size_cliffs_delta: float = 0.0   # in [-1, 1], primary effect size
    effect_size_cohens_d: float = 0.0       # legacy; reported for reference

    # Distributional drift signals from complementary methods
    wasserstein_distance: float = 0.0       # Wasserstein-1 / Earth Mover's; > 0
    psi: float = 0.0                        # Population Stability Index

    sample_size_current: int = 0
    sample_size_baseline: int = 0

    # Layer attribution — which eval layer triggered
    contributing_layers: list[str] = field(default_factory=list)

    # User-facing
    example_trace_ids: list[str] = field(default_factory=list)  # 3-5 worst examples
    recommended_action: str = ""

    # The measuring instrument that produced this signal. Empty means a
    # historical signal whose evaluator cannot be attributed safely. This is
    # appended after every field published in 0.1.0a3 to preserve positional
    # constructor compatibility.
    evaluator_fingerprint: str = ""

    # Completed run snapshot that owns this signal. Empty identifies a legacy
    # signal that cannot be presented as current evidence.
    run_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.direction, DriftDirection):
            self.direction = DriftDirection(self.direction)
