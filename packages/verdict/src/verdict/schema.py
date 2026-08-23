"""Core domain schemas — vendor-neutral, no I/O imports.

These types are the lingua franca of Verdict. Every adapter speaks them.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

    # Content (off by default; controlled by client configuration)
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


# ---------------------------------------------------------------------------
# Conversation-independent drift snapshots
# ---------------------------------------------------------------------------


def _canonical_conversation_json(payload_json: str, *, max_bytes: int) -> object:
    try:
        encoded = payload_json.encode("utf-8")
        payload = json.loads(payload_json)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("conversation snapshot JSON is invalid") from exc
    if len(encoded) > max_bytes:
        raise ValueError("conversation snapshot JSON exceeds its bound")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if canonical != payload_json:
        raise ValueError("conversation snapshot JSON must be canonical")
    return payload


def conversation_json_fingerprint(payload_json: str) -> str:
    """Fingerprint one already-canonical bounded conversation definition."""
    payload = _canonical_conversation_json(payload_json, max_bytes=16 * 1024 * 1024)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_utc_time(name: str, value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ConversationDriftRun:
    tenant_id: str
    run_id: str
    registry_version: str
    analysis_policy_json: str
    analysis_policy_fingerprint: str
    evaluator_definition_json: str
    evaluator_fingerprint: str
    target_workload: str
    baseline_start: datetime
    baseline_end: datetime
    current_start: datetime
    current_end: datetime
    analysis_cutoff: datetime
    status: str
    coverage_json: str
    signal_count: int
    sample_count: int
    started_at: datetime
    completed_at: datetime
    actor: str
    unavailable_reason: str | None = None
    method: str = "conversation-v1"

    def __post_init__(self) -> None:
        if self.method != "conversation-v1":
            raise ValueError("conversation drift method is invalid")
        for name, value, maximum in (
            ("tenant_id", self.tenant_id, 128),
            ("run_id", self.run_id, 256),
            ("registry_version", self.registry_version, 256),
            ("target_workload", self.target_workload, 64),
            ("actor", self.actor, 256),
        ):
            try:
                size = len(value.encode("utf-8"))
            except (AttributeError, UnicodeError) as exc:
                raise ValueError(f"{name} is invalid") from exc
            if not value or size > maximum or "\x00" in value:
                raise ValueError(f"{name} is invalid")
        policy = _canonical_conversation_json(self.analysis_policy_json, max_bytes=32 * 1024)
        evaluator = _canonical_conversation_json(
            self.evaluator_definition_json, max_bytes=64 * 1024
        )
        coverage = _canonical_conversation_json(self.coverage_json, max_bytes=16 * 1024 * 1024)
        if not isinstance(policy, dict) or policy.get("schema") != "analysis-policy-v1":
            raise ValueError("analysis policy is invalid")
        if not isinstance(evaluator, dict) or evaluator.get("schema") != "evaluator-definition-v1":
            raise ValueError("evaluator definition is invalid")
        if not isinstance(coverage, dict) or coverage.get("schema") != "coverage-v1":
            raise ValueError("coverage is invalid")
        if (
            conversation_json_fingerprint(self.analysis_policy_json)
            != self.analysis_policy_fingerprint
        ):
            raise ValueError("analysis policy fingerprint does not match")
        if (
            conversation_json_fingerprint(self.evaluator_definition_json)
            != self.evaluator_fingerprint
        ):
            raise ValueError("evaluator fingerprint does not match")
        dimensions = evaluator.get("dimensions")
        if not isinstance(dimensions, list) or not 1 <= len(dimensions) <= 64:
            raise ValueError("evaluator expected dimensions are invalid")
        normalized_dimensions: list[str] = []
        for dimension in dimensions:
            if not isinstance(dimension, str):
                raise ValueError("evaluator expected dimensions are invalid")
            normalized = unicodedata.normalize("NFC", dimension)
            try:
                size = len(normalized.encode("utf-8"))
            except UnicodeError as exc:
                raise ValueError("evaluator expected dimensions are invalid") from exc
            if (
                normalized != dimension
                or not 1 <= size <= 1024
                or "\x00" in dimension
                or any(unicodedata.category(char).startswith("C") for char in dimension)
            ):
                raise ValueError("evaluator expected dimensions are invalid")
            normalized_dimensions.append(normalized)
        if len(set(normalized_dimensions)) != len(normalized_dimensions):
            raise ValueError("evaluator expected dimensions are not unique")
        longest_map = json.dumps(
            {dimension: "UNCLEAR" for dimension in normalized_dimensions},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if len(longest_map.encode("utf-8")) > 32 * 1024:
            raise ValueError("evaluator expected dimensions exceed the outcome-map bound")

        baseline_start = _require_utc_time("baseline_start", self.baseline_start)
        baseline_end = _require_utc_time("baseline_end", self.baseline_end)
        current_start = _require_utc_time("current_start", self.current_start)
        current_end = _require_utc_time("current_end", self.current_end)
        cutoff = _require_utc_time("analysis_cutoff", self.analysis_cutoff)
        started = _require_utc_time("started_at", self.started_at)
        completed = _require_utc_time("completed_at", self.completed_at)
        for name, value in (
            ("baseline_start", baseline_start),
            ("baseline_end", baseline_end),
            ("current_start", current_start),
            ("current_end", current_end),
            ("analysis_cutoff", cutoff),
            ("started_at", started),
            ("completed_at", completed),
        ):
            object.__setattr__(self, name, value)
        if not baseline_start < baseline_end <= current_start < current_end <= cutoff:
            raise ValueError("conversation drift window ordering is invalid")
        if baseline_end - baseline_start > timedelta(
            days=90
        ) or current_end - current_start > timedelta(days=90):
            raise ValueError("conversation drift window duration is invalid")
        if current_end - baseline_start > timedelta(days=180):
            raise ValueError("conversation drift total window span is invalid")
        if started > completed:
            raise ValueError("conversation drift completion precedes its start")
        if self.status not in {"ineligible", "insufficient", "partial", "ready", "unavailable"}:
            raise ValueError("conversation drift run status is invalid")
        if self.status == "unavailable" and not self.unavailable_reason:
            raise ValueError("unavailable conversation run requires a reason")
        if self.status != "unavailable" and self.unavailable_reason is not None:
            raise ValueError("available conversation run cannot contain an unavailable reason")
        if not 0 <= self.signal_count <= 16_000 or not 0 <= self.sample_count <= 50_000:
            raise ValueError("conversation snapshot counts are invalid")


@dataclass(frozen=True)
class ConversationDriftSample:
    tenant_id: str
    run_id: str
    registry_version: str
    cluster_id: str
    session_ordinal: int
    window: str
    trace_id: str
    event_time: datetime
    attempt_terminal_at: datetime
    attempt_status: str
    legacy_write_status: str
    outcomes_json: str
    error_category: str | None = None
    judgment_id: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.session_ordinal <= 50_000:
            raise ValueError("conversation session ordinal is invalid")
        if self.window not in {"baseline", "current"}:
            raise ValueError("conversation sample window is invalid")
        object.__setattr__(self, "event_time", _require_utc_time("event_time", self.event_time))
        object.__setattr__(
            self,
            "attempt_terminal_at",
            _require_utc_time("attempt_terminal_at", self.attempt_terminal_at),
        )
        outcomes = _canonical_conversation_json(self.outcomes_json, max_bytes=32 * 1024)
        if not isinstance(outcomes, dict):
            raise ValueError("conversation sample outcomes are invalid")
        if self.attempt_status == "completed":
            if (
                self.error_category is not None
                or not outcomes
                or any(value not in {"PASS", "FAIL", "UNCLEAR"} for value in outcomes.values())
            ):
                raise ValueError("completed conversation sample is invalid")
        elif self.attempt_status == "error":
            if outcomes or self.error_category not in {
                "timeout",
                "rate_limited",
                "connection",
                "provider",
                "invalid_response",
                "internal",
            }:
                raise ValueError("error conversation sample is invalid")
        else:
            raise ValueError("conversation sample attempt status is invalid")
        if self.legacy_write_status not in {
            "written",
            "source_deleted",
            "storage_error",
            "not_attempted",
        }:
            raise ValueError("conversation sample legacy write status is invalid")


@dataclass(frozen=True)
class ConversationDriftSignal:
    tenant_id: str
    run_id: str
    signal_id: str
    registry_version: str
    cluster_id: str
    dimension: str
    direction: str
    statistic_name: str
    statistic_value: float
    p_value: float
    p_value_adjusted: float
    effect_size: float
    sample_size_current: int
    sample_size_baseline: int
    examples_json: str = "[]"
    recommended_action: str = ""

    def __post_init__(self) -> None:
        if self.direction not in {"regression", "improvement", "change"}:
            raise ValueError("conversation signal direction is invalid")
        if any(
            not math.isfinite(value)
            for value in (
                self.statistic_value,
                self.p_value,
                self.p_value_adjusted,
                self.effect_size,
            )
        ):
            raise ValueError("conversation signal statistics must be finite")
        if not 0 <= self.p_value <= 1 or not 0 <= self.p_value_adjusted <= 1:
            raise ValueError("conversation signal p-value is invalid")
        if not -1 <= self.effect_size <= 1:
            raise ValueError("conversation signal effect size is invalid")
        if (
            not 0 <= self.sample_size_current <= 50_000
            or not 0 <= self.sample_size_baseline <= 50_000
        ):
            raise ValueError("conversation signal sample count is invalid")
        examples = _canonical_conversation_json(self.examples_json, max_bytes=16 * 1024)
        if not isinstance(examples, list):
            raise ValueError("conversation signal examples are invalid")


@dataclass(frozen=True)
class ConversationTraceCandidate:
    """Bounded metadata for one Task 6 source trace."""

    trace_id_utf8_bytes: int
    trace_id: str | None
    tenant_id: str
    started_at_us: int
    workload_json_type: str
    workload_utf8_bytes: int | None
    workload: str | None
    session_state: str
    session_utf8_bytes: int | None
    session_id: str | None
    success_sampling_state: str
    success_sampling_utf8_bytes: int | None
    success_sampling: str | None
    stream_completion_state: str
    stream_completion_utf8_bytes: int | None
    stream_completion: str | None
    assignment_status: str | None
    assignment_reason: str | None
    cluster_id: str | None
    provider_success: bool
    prompt_present: bool
    prompt_utf8_valid: bool
    prompt_utf8_bytes: int | None
    response_present: bool
    response_utf8_valid: bool
    response_utf8_bytes: int | None


@dataclass(frozen=True)
class ConversationTraceContent:
    trace_id: str
    prompt_redacted: str
    response_redacted: str
