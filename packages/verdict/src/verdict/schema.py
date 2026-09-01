"""Core domain schemas — vendor-neutral, no I/O imports.

These types are the lingua franca of Verdict. Every adapter speaks them.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
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


# PostgreSQL ``INTEGER`` is the narrowest integer storage type used by Verdict.
# Keeping Trace token fields inside that common range prevents adapter-specific
# bind/overflow behavior.
_MAX_TRACE_INTEGER = 2**31 - 1


def normalize_optional_float(
    value: object,
    *,
    minimum: float | None = None,
) -> float | None:
    """Return a finite primitive float or ``None``.

    Provider SDKs use object sentinels for omitted values.  ``bool`` is also
    rejected even though Python considers it an ``int``: storing ``True`` as a
    temperature, latency, or cost would give it an accidental numeric meaning.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    if not math.isfinite(normalized):
        return None
    if minimum is not None and normalized < minimum:
        return None
    return normalized


def normalize_optional_integer(
    value: object,
    *,
    minimum: int = 0,
    maximum: int = _MAX_TRACE_INTEGER,
) -> int | None:
    """Return a database-portable primitive integer or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    normalized = int(value)
    if normalized < minimum or normalized > maximum:
        return None
    return normalized


@dataclass
class Trace:
    """A single captured LLM request/response pair plus metadata.

    Vendor-neutral. The same Trace shape comes from any provider.
    """

    trace_id: str = field(default_factory=_id)
    started_at: datetime = field(default_factory=_now)
    ended_at: datetime | None = None

    # The minimum useful fields, all provider-neutral
    provider: str = ""  # "anthropic", "openai", "google", ...
    operation: Operation = Operation.CHAT
    request_model: str = ""
    response_model: str = ""  # often differs after fallback

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

    # Content (on by default; disable explicitly for metadata-only capture)
    prompt_redacted: str | None = None
    response_redacted: str | None = None
    raw_messages: list[dict[str, Any]] | None = None  # only retained if capture_content=True

    # Routing / observability metadata
    tenant_id: str | None = None
    session_id: str | None = None
    user_id_hash: str | None = None  # HMAC, never raw
    cluster_id: str | None = None  # assigned later by intent clusterer
    tags: dict[str, str] = field(default_factory=dict)

    # Cost (computed by verdict.pricing static pricing table)
    cost_usd: float | None = None

    # The innermost manual span active when this provider call began. Kept last
    # to preserve the positional constructor order of historical Trace fields.
    parent_span_id: str | None = None

    # Additive analysis index fields. They are derived from the public source
    # fields and appended to preserve every published positional slot.
    analysis_started_at_us: int | None = None
    analysis_started_at_state: str = "pending"
    analysis_raw_messages_utf8_bytes: int | None = None
    analysis_raw_messages_state: str = "pending"

    def __post_init__(self) -> None:
        """Normalize enum and scalar values supplied at the public boundary."""
        self.normalize_scalars()
        if not isinstance(self.operation, Operation):
            try:
                self.operation = Operation(self.operation)
            except (TypeError, ValueError) as exc:
                alias = _OPERATION_ALIASES.get(str(self.operation).lower())
                if alias is not None:
                    self.operation = alias
                else:
                    supported = ", ".join(op.value for op in Operation)
                    raise ValueError(
                        f"Unsupported operation {self.operation!r}; expected one of: {supported}"
                    ) from exc

    def normalize_scalars(self) -> None:
        """Normalize mutable database scalar fields in place.

        Instrumentors call this again immediately before persistence because
        response usage, latency, and cost are populated after construction.
        """
        self.input_tokens = normalize_optional_integer(self.input_tokens)
        self.output_tokens = normalize_optional_integer(self.output_tokens)
        self.temperature = normalize_optional_float(self.temperature)
        self.max_tokens = normalize_optional_integer(self.max_tokens)
        self.latency_ms = normalize_optional_float(self.latency_ms, minimum=0.0)
        self.cost_usd = normalize_optional_float(self.cost_usd, minimum=0.0)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_ANALYSIS_RAW_MESSAGES_BYTES = 67_108_864


def datetime_to_utc_us(value: datetime) -> int:
    """Convert one datetime to UTC microseconds without float timestamp math."""
    if not isinstance(value, datetime):
        raise TypeError("value must be a datetime")
    current = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    delta = current - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def populate_trace_analysis_fields(trace: Trace) -> None:
    """Populate portable derived fields used by bounded Task 5/6 queries."""
    try:
        trace.analysis_started_at_us = datetime_to_utc_us(trace.started_at)
        trace.analysis_started_at_state = "valid"
    except (ArithmeticError, OverflowError, TypeError, ValueError):
        trace.analysis_started_at_us = None
        trace.analysis_started_at_state = "invalid"

    if trace.raw_messages is None:
        trace.analysis_raw_messages_utf8_bytes = None
        trace.analysis_raw_messages_state = "missing"
        return
    try:
        encoded = json.dumps(
            trace.raw_messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        trace.analysis_raw_messages_utf8_bytes = None
        trace.analysis_raw_messages_state = "malformed"
        return
    trace.analysis_raw_messages_utf8_bytes = len(encoded)
    trace.analysis_raw_messages_state = (
        "valid" if len(encoded) <= _MAX_ANALYSIS_RAW_MESSAGES_BYTES else "oversize"
    )


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

    name: str  # "groundedness", "relevance", ...
    verdict: Verdict
    reasoning: str = ""
    judge_model: str = ""  # which judge produced this

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

    judge_models: list[str] = field(default_factory=list)  # ensemble support
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
    kind: str = ""  # thumbs_up, thumbs_down, regenerate, retry, abandon, copy, accept, ...
    created_at: datetime = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Drift signals — output of the drift detector
# ---------------------------------------------------------------------------


class DriftDirection(str, Enum):
    REGRESSION = "regression"
    IMPROVEMENT = "improvement"
    CHANGE = "change"  # statistically different but direction unclear


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
    p_value_adjusted: float = 1.0  # Benjamini-Hochberg adjusted

    # Effect size measures — Cliff's delta is the primary (non-parametric, pairs
    # correctly with Mann-Whitney U). Cohen's d kept for backward compatibility
    # but its normality assumption is violated by our binary PASS/FAIL data.
    effect_size_cliffs_delta: float = 0.0  # in [-1, 1], primary effect size
    effect_size_cohens_d: float = 0.0  # legacy; reported for reference

    # Distributional drift signals from complementary methods
    wasserstein_distance: float = 0.0  # Wasserstein-1 / Earth Mover's; > 0
    psi: float = 0.0  # Population Stability Index

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


# ---------------------------------------------------------------------------
# Versioned cluster registry
# ---------------------------------------------------------------------------


@dataclass
class ClusterIdentity:
    tenant_id: str
    cluster_id: str = field(default_factory=lambda: f"clu_{_id()}")
    kind: str = "semantic"
    lifecycle: str = "provisional"
    explicit_key: str | None = None
    display_name: str = "Cluster"
    last_model_fingerprint: str | None = None
    last_centroid: list[float] | None = None
    last_version_id: str | None = None
    created_at: datetime = field(default_factory=_now)
    created_by: str = ""
    updated_at: datetime = field(default_factory=_now)
    updated_by: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"explicit", "semantic"}:
            raise ValueError("cluster identity kind must be explicit or semantic")
        if self.lifecycle not in {"provisional", "active"}:
            raise ValueError("cluster identity lifecycle is invalid")
        if (self.kind == "explicit") != (self.explicit_key is not None):
            raise ValueError("explicit identity requires exactly one explicit key")
        normalized = unicodedata.normalize("NFC", self.display_name)
        try:
            display_bytes = len(normalized.encode("utf-8"))
        except UnicodeError:
            display_bytes = 257
        if (
            normalized != self.display_name
            or not normalized
            or len(normalized) > 80
            or display_bytes > 256
            or any(unicodedata.category(char).startswith("C") for char in normalized)
        ):
            raise ValueError("cluster display name is invalid")
        if self.last_centroid is not None:
            norm = math.sqrt(sum(value * value for value in self.last_centroid))
            if (
                not self.last_centroid
                or any(not math.isfinite(value) for value in self.last_centroid)
                or not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-9)
            ):
                raise ValueError("cluster identity prototype must be finite and unit norm")


@dataclass(frozen=True)
class ClusterRegistryVersion:
    tenant_id: str
    version_id: str = field(default_factory=lambda: f"crv_{_id()}")
    parent_version_id: str | None = None
    strategy: str = "semantic"
    cutoff: datetime = field(default_factory=_now)
    lookback_days: int = 90
    fit_definition_json: str = "{}"
    fit_definition_fingerprint: str = ""
    preview_report_json: str = "{}"
    created_at: datetime = field(default_factory=_now)
    created_by: str = ""

    def __post_init__(self) -> None:
        if self.strategy not in {"explicit", "semantic", "hybrid"}:
            raise ValueError("cluster registry strategy is invalid")
        if self.lookback_days <= 0:
            raise ValueError("cluster registry lookback must be positive")


@dataclass(frozen=True)
class ClusterRegistryCluster:
    tenant_id: str
    version_id: str
    cluster_id: str
    kind: str
    centroid: list[float] | None = None
    radius: float | None = None
    member_count: int = 0
    outlier_count: int = 0

    def __post_init__(self) -> None:
        if self.kind == "explicit":
            if self.centroid is not None or self.radius is not None:
                raise ValueError("explicit cluster cannot contain semantic geometry")
        elif self.kind == "semantic":
            if not self.centroid or self.radius is None:
                raise ValueError("semantic cluster requires centroid and radius")
            if not math.isfinite(self.radius) or not 0 <= self.radius <= 2:
                raise ValueError("semantic cluster radius must be finite and in [0,2]")
            if any(not math.isfinite(value) for value in self.centroid):
                raise ValueError("semantic centroid must be finite")
            norm = math.sqrt(sum(value * value for value in self.centroid))
            if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
                raise ValueError("semantic centroid must have unit norm")
        else:
            raise ValueError("registry cluster kind is invalid")
        if self.member_count < 0 or self.outlier_count < 0:
            raise ValueError("cluster counts must be nonnegative")


_OUTLIER_REASONS = {
    "distance",
    "explicit_key_not_in_version",
    "semantic_fit_too_small",
}
_INELIGIBLE_REASONS = {
    "invalid_workload",
    "unsafe_workload",
    "missing_intent_key",
    "invalid_intent_key",
    "unsafe_intent_key",
    "content_not_captured",
    "raw_messages_oversize",
    "malformed_messages",
    "no_supported_user_text",
    "text_too_short",
    "text_too_long",
    "redaction_error",
}


@dataclass(frozen=True)
class TraceClusterAssignment:
    tenant_id: str
    version_id: str
    trace_id: str
    origin: str
    status: str
    cluster_id: str | None = None
    cluster_kind: str | None = None
    reason: str | None = None
    distance: float | None = None
    assigned_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.origin not in {"fit", "incremental"}:
            raise ValueError("assignment origin is invalid")
        if self.status == "assigned":
            if self.cluster_id is None or self.cluster_kind not in {"explicit", "semantic"}:
                raise ValueError("assigned result requires cluster identity and kind")
            if self.reason is not None:
                raise ValueError("assigned result cannot contain a reason")
            if self.cluster_kind == "explicit" and self.distance is not None:
                raise ValueError("explicit assignment distance must be null")
            if self.cluster_kind == "semantic" and (
                self.distance is None
                or not math.isfinite(self.distance)
                or not 0 <= self.distance <= 2
            ):
                raise ValueError("semantic assignment requires a finite distance in [0,2]")
        elif self.status == "outlier":
            if self.cluster_id is not None or self.cluster_kind is not None:
                raise ValueError("outlier result cannot name a cluster")
            if self.reason not in _OUTLIER_REASONS:
                raise ValueError("outlier assignment reason is invalid")
            if self.reason == "distance" and (
                self.distance is None
                or not math.isfinite(self.distance)
                or not 0 <= self.distance <= 2
            ):
                raise ValueError("outlier distance is invalid")
            if self.reason != "distance" and self.distance is not None:
                raise ValueError("outlier distance is invalid")
        elif self.status == "ineligible":
            if self.cluster_id is not None or self.cluster_kind is not None:
                raise ValueError("ineligible result cannot name a cluster")
            if self.reason not in _INELIGIBLE_REASONS or self.distance is not None:
                raise ValueError("ineligible assignment reason is invalid")
        else:
            raise ValueError("assignment status is invalid")


@dataclass(frozen=True)
class ActiveClusterRegistry:
    tenant_id: str
    version_id: str | None = None
    generation: int = 0
    activated_at: datetime | None = None
    activated_by: str | None = None

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("cluster registry generation must be nonnegative")


@dataclass(frozen=True)
class ClusterRegistryEvent:
    tenant_id: str
    event_id: str = field(default_factory=lambda: f"cre_{_id()}")
    action: str = "validated"
    from_version_id: str | None = None
    to_version_id: str | None = None
    pointer_generation: int | None = None
    created_at: datetime = field(default_factory=_now)
    actor: str = ""
    details_json: str = "{}"

    def __post_init__(self) -> None:
        if self.action not in {
            "validated",
            "validation_failed",
            "activated",
            "rolled_back",
            "renamed",
        }:
            raise ValueError("cluster registry event action is invalid")


@dataclass(frozen=True)
class ClusterTraceCandidate:
    trace_id_utf8_bytes: int
    trace_id: str | None
    tenant_id: str
    started_at_us: int
    workload_json_type: str
    workload_utf8_bytes: int | None
    workload: str | None
    intent_key_json_type: str
    intent_key_utf8_bytes: int | None
    intent_key: str | None
    raw_messages_state: str
    raw_messages_utf8_bytes: int | None


def cluster_candidate_digest(trace_ids: list[str]) -> str:
    """Canonical bounded identity of one activation coverage population."""
    digest = hashlib.sha256(b"cluster-coverage-v1")
    for encoded in sorted(value.encode("utf-8", "surrogatepass") for value in trace_ids):
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()
