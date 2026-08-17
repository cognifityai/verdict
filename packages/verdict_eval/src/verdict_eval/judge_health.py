"""Judge-health monitoring against fixed, human-labeled sentinel examples.

Sentinel judgments are never production judgments. The resulting aggregate is
stored separately so a changing evaluator cannot masquerade as target-model
drift. Agreement is an anchor, not perfect detection: an unchanged score cannot
rule out provider-side behavior changes outside the sentinel set.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.schema import (
    EvaluatorHealthRecord,
    EvaluatorHealthStatus,
    Verdict,
)


@dataclass(frozen=True)
class SentinelExample:
    sentinel_id: str
    query: str
    response: str
    labels: dict[str, Verdict]
    context: str | None = None

    def __post_init__(self) -> None:
        if not self.sentinel_id.strip():
            raise ValueError("sentinel_id must not be empty")
        if not self.labels:
            raise ValueError(f"sentinel {self.sentinel_id!r} has no human labels")
        normalized: dict[str, Verdict] = {}
        for dimension, label in self.labels.items():
            verdict = label if isinstance(label, Verdict) else Verdict(str(label).lower())
            if verdict == Verdict.UNCLEAR:
                raise ValueError(
                    f"sentinel {self.sentinel_id!r} label {dimension!r} must be "
                    "PASS or FAIL, not UNCLEAR"
                )
            normalized[str(dimension)] = verdict
        object.__setattr__(self, "labels", normalized)


def load_sentinel_set(path: str | Path) -> tuple[str, list[SentinelExample]]:
    """Load newline-delimited JSON sentinel examples.

    The first optional metadata row is ``{"set_name": "support-v1"}``.
    Every other non-empty row must contain sentinel_id, query, response, and a
    labels mapping. Duplicate IDs are rejected because they make trend identity
    ambiguous.
    """
    source = Path(path)
    set_name = source.stem
    examples: list[SentinelExample] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(source.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid sentinel JSON at {source}:{line_number}: {exc.msg}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                f"sentinel row at {source}:{line_number} must be a JSON object"
            )
        if set(payload) == {"set_name"}:
            set_name = str(payload["set_name"]).strip()
            if not set_name:
                raise ValueError("sentinel set_name must not be empty")
            continue
        try:
            example = SentinelExample(
                sentinel_id=str(payload["sentinel_id"]),
                query=str(payload["query"]),
                response=str(payload["response"]),
                context=(
                    None if payload.get("context") is None
                    else str(payload["context"])
                ),
                labels=dict(payload["labels"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid sentinel at {source}:{line_number}: {exc}"
            ) from exc
        if example.sentinel_id in seen:
            raise ValueError(f"duplicate sentinel_id {example.sentinel_id!r}")
        seen.add(example.sentinel_id)
        examples.append(example)
    if not examples:
        raise ValueError(f"sentinel set {source} contains no examples")
    return set_name, examples


def sentinel_set_fingerprint(examples: list[SentinelExample]) -> str:
    payload: list[dict[str, Any]] = []
    for example in sorted(examples, key=lambda item: item.sentinel_id):
        payload.append({
            "sentinel_id": example.sentinel_id,
            "query": example.query,
            "response": example.response,
            "context": example.context,
            "labels": {
                name: verdict.value
                for name, verdict in sorted(example.labels.items())
            },
        })
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _wilson_interval(correct: float, total: int) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    z = 1.959963984540054
    observed = correct / total
    denominator = 1 + z * z / total
    center = (observed + z * z / (2 * total)) / denominator
    half_width = (
        z
        * math.sqrt(
            observed * (1 - observed) / total + z * z / (4 * total * total)
        )
        / denominator
    )
    return max(0.0, center - half_width), min(1.0, center + half_width)


def evaluate_judge_health(
    judge,
    examples: list[SentinelExample],
    *,
    set_name: str,
    minimum_examples: int = 30,
    agreement_threshold: float = 0.8,
) -> EvaluatorHealthRecord:
    """Evaluate a judge on a fixed anchor set without persisting raw examples."""
    if minimum_examples < 1:
        raise ValueError("minimum_examples must be at least 1")
    if not 0 <= agreement_threshold <= 1:
        raise ValueError("agreement_threshold must be between 0 and 1")
    if not examples:
        raise ValueError("judge health requires at least one sentinel example")

    identities = {
        judge.evaluator_identity(example.context)["evaluator_fingerprint"]
        for example in examples
    }
    if len(identities) != 1:
        raise ValueError(
            "sentinel examples resolve to multiple evaluator fingerprints; split "
            "the set by context-dependent rubric identity"
        )
    evaluator_fingerprint = next(iter(identities))

    correct = 0
    total = 0
    correct_examples = 0
    completed_examples = 0
    errors = 0
    for example in examples:
        try:
            judgment = judge.judge(
                query=example.query,
                response=example.response,
                context=example.context,
                trace_id=f"sentinel:{example.sentinel_id}",
            )
        except Exception:
            errors += 1
            continue
        completed_examples += 1
        total += len(example.labels)
        actual = {score.name: score.verdict for score in judgment.dimensions}
        example_correct = sum(
            actual.get(dimension) == expected
            for dimension, expected in example.labels.items()
        )
        correct += example_correct
        correct_examples += example_correct == len(example.labels)

    example_agreement = (
        correct_examples / completed_examples if completed_examples else 0.0
    )
    label_agreement = correct / total if total else 0.0
    low, high = _wilson_interval(correct_examples, completed_examples)
    if completed_examples < minimum_examples:
        status = EvaluatorHealthStatus.INSUFFICIENT_DATA
    elif errors:
        # Agreement among successful calls cannot certify a run whose sentinel
        # coverage was incomplete. Preserve INSUFFICIENT_DATA when errors leave
        # too few labels; otherwise report a measured but operationally degraded
        # evaluator rather than silently calling the partial sample healthy.
        status = EvaluatorHealthStatus.DEGRADED
    elif low is not None and low >= agreement_threshold:
        status = EvaluatorHealthStatus.HEALTHY
    else:
        status = EvaluatorHealthStatus.DEGRADED
    return EvaluatorHealthRecord(
        evaluator_fingerprint=evaluator_fingerprint,
        sentinel_set_name=set_name,
        sentinel_set_fingerprint=sentinel_set_fingerprint(examples),
        correct_examples=correct_examples,
        total_examples=completed_examples,
        example_agreement=example_agreement,
        example_confidence_low=low,
        example_confidence_high=high,
        correct_labels=correct,
        total_labels=total,
        label_agreement=label_agreement,
        status=status,
        error_count=errors,
        method_version="2",
    )
