"""Evidence-aware, budget-bounded evaluator execution for the dashboard."""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from verdict.pricing import PRICING_LAST_VERIFIED, compute_cost_usd
from verdict.redaction import redact
from verdict.schema import Judgment, JudgmentStatus, Trace
from verdict.storage.base import Storage

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def evaluator_environment() -> dict[str, Any]:
    """Return only secret availability, never secret values."""
    try:
        import verdict_eval  # noqa: F401
        available = True
    except ImportError:
        available = False
    return {
        "evalPackageAvailable": available,
        "providers": [
            {
                "provider": provider,
                "secretReference": key,
                "configured": bool(os.environ.get(key)),
            }
            for provider, key in _PROVIDER_KEYS.items()
        ],
        "secretStorage": "environment_only",
        "pricingLastVerified": PRICING_LAST_VERIFIED.isoformat(),
    }


def _validated_config(config: dict[str, Any]):
    if not isinstance(config, dict):
        raise ValueError("evaluator config must be an object")
    provider = config.get("provider")
    model = config.get("model")
    if provider not in _PROVIDER_KEYS:
        raise ValueError("unsupported judge provider")
    if not isinstance(model, str) or not model or len(model.encode("utf-8")) > 256:
        raise ValueError("invalid judge model")
    max_calls = config.get("maxCalls", 10)
    max_output = config.get("maxOutputTokens", 512)
    if (
        isinstance(max_calls, bool) or not isinstance(max_calls, int) or not 1 <= max_calls <= 500
        or isinstance(max_output, bool) or not isinstance(max_output, int)
        or not 64 <= max_output <= 4096
    ):
        raise ValueError("invalid evaluator budget")
    rubric_payload = config.get("rubric")
    if not isinstance(rubric_payload, dict):
        raise ValueError("rubric is required")
    name = rubric_payload.get("name")
    version = rubric_payload.get("version")
    dimensions = rubric_payload.get("dimensions")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid rubric name")
    if not isinstance(version, str) or not version or len(version.encode("utf-8")) > 64:
        raise ValueError("invalid rubric version")
    if not isinstance(dimensions, list) or not 1 <= len(dimensions) <= 12:
        raise ValueError("rubric must contain 1-12 dimensions")
    from verdict_eval.judge import Rubric, RubricDimension

    built = []
    seen = set()
    for item in dimensions:
        if not isinstance(item, dict):
            raise ValueError("invalid rubric dimension")
        dim_name = item.get("name")
        description = item.get("description")
        if not isinstance(dim_name, str) or not _NAME.fullmatch(dim_name) or dim_name in seen:
            raise ValueError("invalid or duplicate rubric dimension name")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description.encode("utf-8")) > 2_000
        ):
            raise ValueError("invalid rubric dimension description")
        seen.add(dim_name)
        built.append(RubricDimension(
            dim_name,
            description.strip(),
            requires_context=item.get("requiresContext") is True,
        ))
    return provider, model, max_calls, max_output, Rubric(name, version, tuple(built))


def _eligibility(trace: Trace) -> str | None:
    if trace.error:
        return "provider_call_failed"
    if not (trace.prompt_redacted or "").strip():
        return "prompt_not_captured"
    if not (trace.response_redacted or "").strip():
        return "response_not_captured"
    return None


def _selected(storage: Storage, tenant_id: str, max_calls: int):
    traces = storage.list_traces(tenant_id=tenant_id, limit=10_000)
    reasons: Counter[str] = Counter()
    eligible = []
    for trace in traces:
        reason = _eligibility(trace)
        if reason:
            reasons[reason] += 1
        else:
            eligible.append(trace)
    # Storage returns newest first. Keep selection deterministic and bounded.
    return traces, eligible, reasons


def preview_evaluation(
    storage: Storage, *, tenant_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    provider, model, max_calls, max_output, rubric = _validated_config(config)
    traces, eligible, reasons = _selected(storage, tenant_id, max_calls)
    selected = eligible[:max_calls]
    input_estimate = 0
    for trace in selected:
        rubric_chars = sum(len(d.name) + len(d.description) for d in rubric.dimensions)
        input_estimate += math.ceil(
            (len(trace.prompt_redacted or "") + len(trace.response_redacted or "") + rubric_chars)
            / 4
        ) + 400
    output_maximum = max_output * len(selected)
    return {
        "provider": provider,
        "model": model,
        "rubric": {"name": rubric.name, "version": rubric.version,
                   "dimensions": [d.name for d in rubric.dimensions]},
        "availableTraces": len(traces),
        "eligible": len(traces) - sum(reasons.values()),
        "notEvaluable": sum(reasons.values()),
        "notEvaluableReasons": dict(sorted(reasons.items())),
        "plannedCalls": len(selected),
        "maximumCalls": max_calls,
        "estimatedInputTokens": input_estimate,
        "maximumOutputTokens": output_maximum,
        "estimatedMaximumCostUsd": compute_cost_usd(model, input_estimate, output_maximum),
        "costIsStaticEstimate": True,
        "externalEgressRequired": True,
    }


def _provider(name: str):
    if not os.environ.get(_PROVIDER_KEYS[name]):
        raise ValueError(f"{_PROVIDER_KEYS[name]} is not configured")
    if name == "anthropic":
        from verdict_eval.providers import AnthropicAdapter
        return AnthropicAdapter()
    if name == "openai":
        from verdict_eval.providers import OpenAIAdapter
        return OpenAIAdapter()
    from verdict_eval.providers import GoogleAdapter
    return GoogleAdapter()


def execute_evaluation(
    storage: Storage,
    *,
    tenant_id: str,
    config: dict[str, Any],
    confirm_external_egress: bool,
    provider=None,
) -> dict[str, Any]:
    if confirm_external_egress is not True:
        raise ValueError("external judge egress was not confirmed")
    provider_name, model, max_calls, _max_output, rubric = _validated_config(config)
    traces, eligible, reasons = _selected(storage, tenant_id, max_calls)
    from verdict_eval.judge import Judge

    judge = Judge(
        provider=provider or _provider(provider_name),
        model=model,
        rubric=rubric,
        skip_context_dependent_when_missing=True,
        max_tokens=_max_output,
    )
    identity = judge.evaluator_identity(context=None)
    selected = []
    already_judged = 0
    for trace in eligible:
        if any(
            judgment.status is JudgmentStatus.COMPLETED
            and judgment.evaluator_fingerprint == identity["evaluator_fingerprint"]
            for judgment in storage.list_judgments_for_trace(trace.trace_id, limit=100)
        ):
            already_judged += 1
            continue
        selected.append(trace)
        if len(selected) >= max_calls:
            break
    completed = 0
    errors = 0
    for trace in selected:
        try:
            storage.insert_judgment(judge.judge(
                query=trace.prompt_redacted or "",
                response=trace.response_redacted or "",
                trace_id=trace.trace_id,
            ))
            completed += 1
        except Exception as exc:
            storage.insert_judgment(Judgment(
                trace_id=trace.trace_id,
                status=JudgmentStatus.ERROR,
                error=redact(str(exc)) or "judge error",
                **identity,
            ))
            errors += 1
    return {
        "availableTraces": len(traces),
        "plannedCalls": len(selected),
        "alreadyJudged": already_judged,
        "completed": completed,
        "errors": errors,
        "notEvaluable": sum(reasons.values()),
        "notEvaluableReasons": dict(sorted(reasons.items())),
        "evaluatorFingerprint": identity["evaluator_fingerprint"],
        "rubric": {"name": rubric.name, "version": rubric.version,
                   "dimensions": identity["expected_dimensions"]},
    }


def _label_set(path: str | Path, rubric):
    source = Path(path).expanduser()
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("label set must be a bounded regular file")
    from verdict_eval.judge_health import load_sentinel_set
    set_name, examples = load_sentinel_set(source)
    if len(examples) > 500:
        raise ValueError("label set exceeds 500 examples")
    dimension_map = {dimension.name: dimension for dimension in rubric.dimensions}
    for example in examples:
        unknown = set(example.labels) - set(dimension_map)
        if unknown:
            raise ValueError("label set contains dimensions outside the rubric")
        for name in example.labels:
            if dimension_map[name].requires_context and not (example.context or "").strip():
                raise ValueError("context-required label has no context evidence")
    return set_name, examples


def preview_calibration(*, path: str | Path, config: dict[str, Any]) -> dict[str, Any]:
    _provider_name, model, _max_calls, max_output, rubric = _validated_config(config)
    set_name, examples = _label_set(path, rubric)
    label_counts: Counter[str] = Counter()
    estimated_input = 0
    for example in examples:
        label_counts.update(example.labels.keys())
        estimated_input += math.ceil(
            (len(example.query) + len(example.response) + len(example.context or "")) / 4
        ) + 400
    maximum_output = max_output * len(examples)
    return {
        "setName": set_name,
        "examples": len(examples),
        "labelCounts": dict(sorted(label_counts.items())),
        "plannedCalls": len(examples),
        "estimatedMaximumCostUsd": compute_cost_usd(model, estimated_input, maximum_output),
        "externalEgressRequired": True,
        "rawLabelsPersisted": False,
    }


def execute_calibration(
    storage: Storage,
    *,
    path: str | Path,
    config: dict[str, Any],
    confirm_external_egress: bool,
    minimum_examples: int = 30,
    agreement_threshold: float = 0.8,
    provider=None,
) -> dict[str, Any]:
    if confirm_external_egress is not True:
        raise ValueError("external judge egress was not confirmed")
    provider_name, model, _max_calls, max_output, rubric = _validated_config(config)
    set_name, examples = _label_set(path, rubric)
    from verdict_eval.judge import Judge
    from verdict_eval.judge_health import evaluate_judge_health
    judge = Judge(
        provider=provider or _provider(provider_name),
        model=model,
        rubric=rubric,
        max_tokens=max_output,
        skip_context_dependent_when_missing=False,
    )
    health = evaluate_judge_health(
        judge,
        examples,
        set_name=set_name,
        minimum_examples=minimum_examples,
        agreement_threshold=agreement_threshold,
    )
    storage.insert_evaluator_health(health)
    return {
        "setName": health.sentinel_set_name,
        "status": health.status.value,
        "correctExamples": health.correct_examples,
        "totalExamples": health.total_examples,
        "exampleAgreement": health.example_agreement,
        "exampleConfidenceLow": health.example_confidence_low,
        "exampleConfidenceHigh": health.example_confidence_high,
        "correctLabels": health.correct_labels,
        "totalLabels": health.total_labels,
        "labelAgreement": health.label_agreement,
        "errors": health.error_count,
        "evaluatorFingerprint": health.evaluator_fingerprint,
        "rawLabelsPersisted": False,
    }
