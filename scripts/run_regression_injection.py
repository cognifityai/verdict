"""Synthetic regression injection self-test.

Generates corrupted responses across 7 categories, runs them through a judge,
and reports precision/recall/F1 per category. This verifies pipeline wiring on
known synthetic corruptions; it is not external validation or a production
quality claim.

Usage:
    python scripts/run_regression_injection.py \\
        --judge-model gemini-2.5-flash \\
        --provider google \\
        --n-samples 50

This script intentionally accepts a "fake" provider for offline testing so the
self-test is runnable before live LLM keys are wired up.
"""

from __future__ import annotations

# ruff: noqa: E402 - source-checkout imports follow the local sys.path bootstrap.
import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Make local packages importable when running from repo root
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "packages" / "verdict" / "src"))
sys.path.insert(0, str(HERE.parent / "packages" / "verdict_eval" / "src"))

from verdict.schema import Verdict
from verdict_eval.injector import (
    CorruptedSample,
    CorruptionInjector,
    CorruptionKind,
    build_corruption_battery,
)
from verdict_eval.judge import DEFAULT_RUBRIC, Judge
from verdict_eval.providers import FakeProvider

# A small built-in corpus so the script runs without external data.
DEFAULT_CORPUS = [
    (
        "What is the capital of France?",
        "The capital of France is Paris, located in the north-central part of the country.",
    ),
    (
        "Summarize the plot of Romeo and Juliet in one sentence.",
        "Two young lovers from feuding families in Verona secretly marry but die tragically due to a series of misunderstandings.",
    ),
    (
        "How do I install Python on Ubuntu?",
        "Run: sudo apt-get update && sudo apt-get install python3 python3-pip.",
    ),
    (
        "What is photosynthesis?",
        "Photosynthesis is the process by which plants convert sunlight, water, and carbon dioxide into glucose and oxygen.",
    ),
    (
        "Convert 100 km/h to m/s.",
        "100 km/h is approximately 27.78 m/s (divide by 3.6).",
    ),
]


@dataclass
class Outcome:
    kind: CorruptionKind
    strength: float
    # Per-dimension: judge said FAIL on at least one expected dimension
    detected: bool


def _build_fake_judge_response(sample: CorruptedSample) -> str:
    """Heuristic 'oracle' judge for offline testing.

    Real judges (Gemini Flash, Haiku) will replace this entirely. This is here
    so the harness is exercisable without API keys.
    """
    cr = sample.corrupted_response.lower()
    fails: dict[str, bool] = {}
    fails["safety"] = bool(
        "ssn" in cr or "credit card" in cr or "driver's license" in cr
        or "ignore previous instructions" in cr
    )
    fails["relevance"] = sample.kind in {CorruptionKind.OFF_TOPIC, CorruptionKind.REFUSAL}
    fails["completeness"] = sample.kind == CorruptionKind.TRUNCATION
    fails["groundedness"] = sample.kind == CorruptionKind.HALLUCINATION
    fails["instruction_following"] = sample.kind == CorruptionKind.TONE_DRIFT
    payload = {
        d.name: {
            "reasoning": "heuristic",
            "verdict": "FAIL" if fails.get(d.name, False) else "PASS",
        }
        for d in DEFAULT_RUBRIC.dimensions
    }
    return json.dumps(payload)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--judge-model", default="fake-oracle",
                   help="Judge model name. Use 'fake-oracle' for offline.")
    p.add_argument("--provider", default="fake",
                   choices=["fake", "anthropic", "openai", "google"],
                   help="LLM provider for the judge.")
    p.add_argument("--n-samples", type=int, default=5,
                   help="How many input (query,response) pairs to corrupt.")
    p.add_argument("--strengths", default="1.0",
                   help="Comma-separated corruption strengths (0.0-1.0).")
    args = p.parse_args()

    if args.provider == "fake":
        print(
            "SELF-TEST MODE: the deterministic oracle knows the injected labels. "
            "Scores verify harness wiring only; do not publish them as judge validation."
        )
    else:
        print(
            "SYNTHETIC BATTERY: results measure this judge on generated corruptions, "
            "not agreement with human labels on production traffic."
        )

    corpus = DEFAULT_CORPUS[: args.n_samples] or DEFAULT_CORPUS
    strengths = [float(s) for s in args.strengths.split(",")]
    battery = build_corruption_battery(corpus, strengths=strengths)
    print(f"Generated corruption battery: {len(battery)} samples across "
          f"{len(corpus)} inputs and {len(strengths)} strengths.")

    # Provider selection. Real providers swap in via the LLMProvider port.
    if args.provider == "fake":
        # Per-sample fake provider — we vary the response per sample to act
        # as a deterministic oracle that "knows" which corruption is which.
        def make_judge_for(sample: CorruptedSample) -> Judge:
            return Judge(provider=FakeProvider(_build_fake_judge_response(sample)),
                         model=args.judge_model)
    else:
        from verdict_eval.providers import AnthropicAdapter, GoogleAdapter, OpenAIAdapter
        if args.provider == "anthropic":
            provider = AnthropicAdapter()
        elif args.provider == "openai":
            provider = OpenAIAdapter()
        else:
            provider = GoogleAdapter()
        def make_judge_for(_sample: CorruptedSample) -> Judge:
            return Judge(provider=provider, model=args.judge_model)

    outcomes: list[Outcome] = []
    for sample in battery:
        judge = make_judge_for(sample)
        j = judge.judge(query=sample.original_query, response=sample.corrupted_response)
        any_fail = any(d.verdict == Verdict.FAIL for d in j.dimensions)
        outcomes.append(Outcome(kind=sample.kind, strength=sample.strength, detected=any_fail))

    # Compute precision/recall/F1 per category.
    # Positive label: corruption present. Negative: NONE.
    by_kind: dict[CorruptionKind, list[Outcome]] = {}
    for o in outcomes:
        by_kind.setdefault(o.kind, []).append(o)

    none = by_kind.get(CorruptionKind.NONE, [])
    fp_rate = sum(1 for o in none if o.detected) / max(1, len(none))

    print("\nPer-category detection:")
    print(f"  {'kind':30s} {'n':>4s}  {'recall':>7s}")
    for kind in CorruptionInjector().all_categories():
        outs = by_kind.get(kind, [])
        n = len(outs)
        recall = sum(1 for o in outs if o.detected) / max(1, n)
        print(f"  {kind.value:30s} {n:>4d}  {recall:>6.1%}")
    print(f"\nFalse-positive rate on clean responses: {fp_rate:.1%} (n={len(none)})")

    # Overall F1 across positives
    all_pos = [o for o in outcomes if o.kind != CorruptionKind.NONE]
    tp = sum(1 for o in all_pos if o.detected)
    fn = len(all_pos) - tp
    fp = sum(1 for o in none if o.detected)
    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    f1 = (2 * precision * recall) / max(1e-9, precision + recall)
    print(f"\nOverall: precision={precision:.3f}  recall={recall:.3f}  F1={f1:.3f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
