# ADR-002 — LLM-as-Judge Methodology

**Status:** Accepted

**Date:** 2026-05-12

## Context

Verdict's judge-derived comparisons depend on scoring response quality. LLM
judges can be biased by verbosity, style, and model family. The methodology
therefore needs explicit rubric, coverage, identity, and calibration rules.

## Decision

Verdict uses configurable providers behind an `LLMProvider` interface. The
default rubric is binary PASS/FAIL per dimension and covers groundedness,
relevance, completeness, safety, and instruction following. Binary dimensions
are easier to calibrate and compare than broad numeric scales.

PASS rate is `PASS / (PASS + FAIL)`. `UNCLEAR`, missing dimensions, malformed
output normalized to `UNCLEAR`, and judge errors are coverage states and do not
enter the denominator. No scored values means unavailable, not zero percent.

The measuring instrument is a complete evaluator identity: provider, model
list, rubric name/version, behavior-relevant configuration, expected dimensions,
and a SHA-256 fingerprint over the effective rubric and prompt templates.
Pipeline reuse, Monitor policies, dashboard summaries, and sampled judgments
must describe one identity. Historical rows without complete fields stay
labeled incomplete and are not combined with complete identities. The latest
attempt per trace wins; a latest error is coverage failure and can be retried.

Dimensions marked as requiring retrieved context are omitted only when the
caller explicitly enables context-dependent skipping. The effective rubric is
the rubric shown at approval, sent to the provider, fingerprinted, and
persisted. If no dimension remains, evaluation fails before provider egress.
Nonblank explicit context restores the full configured rubric; whitespace is
not evidence.

Bias mitigations include cross-family judging when practical, length-aware
human review, and per-dimension scoring so one broad score cannot hide the
reason for a change.

## Calibration Guidance

Users should calibrate a judge on their own workload before relying on quality
alerts:

1. Sample recent traces.
2. Label PASS/FAIL judgments for the relevant dimensions.
3. Run `scripts/verify_rubric_alignment.py` against the labeled set.
4. Review per-dimension agreement and confidence intervals.
5. Treat low-agreement dimensions as review-only until the rubric, judge model,
   or label set improves.

For recurring monitoring, a fixed human-labeled JSONL sentinel set can store
only its aggregate, evaluator fingerprint, and set fingerprint. `healthy`
requires both the configured independent-example floor and a 95% Wilson
confidence-interval lower bound at or above the threshold. An example is
correct only when every declared label matches; label agreement is a separate
diagnostic. Any sentinel execution error prevents a healthy result. This anchor
does not guarantee unchanged behavior outside the set.

## Consequences

- Verdict does not assume a judge is universally correct.
- Calibration data is workload-specific.
- Rubric versions must be tracked when alerts or reports depend on them.
- Provider/model names alone are insufficient evaluator identity.
- Deterministic checks should be preferred for schema validity, exact math, and
  executable behavior.

## References

- Wang et al. 2023, "Large Language Models are not Fair Evaluators,"
  arXiv:2305.17926
- Panickssery et al. 2024, "LLM Evaluators Recognize and Favor Their Own
  Generations," arXiv:2404.18796
- Tan et al. 2024, "JudgeBench," arXiv:2410.12784
