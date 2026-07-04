# ADR-002 — LLM-as-Judge Methodology

**Status:** Accepted
**Date:** 2026-05-12

## Context

Verdict's drift signal depends on judging response quality. LLM judges can be
biased by response order, verbosity, style, and model family. The methodology
therefore needs explicit choices about rubric shape, bias controls, and
calibration.

## Decision

Verdict uses configurable judge providers behind an `LLMProvider` interface. The
default rubric is binary PASS/FAIL per dimension and covers groundedness,
relevance, completeness, safety, and instruction following.

Binary dimensions are preferred over broad numeric scales because they are easier
to calibrate, easier for humans to label consistently, and better suited to
pass-rate drift detection.

Bias mitigations:

1. **Position swap** for pairwise judgment. If order changes the result, treat
   the comparison cautiously or as a tie.
2. **Cross-family judging** for provider comparisons when practical.
3. **Length-aware review** when ranking responses so verbosity does not dominate
   preference.
4. **Per-dimension scoring** so one broad quality score does not hide the reason
   for a change.

## Calibration Guidance

Users should calibrate a judge on their own workload before relying on quality
alerts or model rankings:

1. Sample recent traces.
2. Label PASS/FAIL judgments for the dimensions they care about.
3. Run `scripts/verify_rubric_alignment.py` against the labeled set.
4. Review per-dimension agreement and confidence intervals.
5. Treat low-agreement dimensions as review-only until the rubric, judge model,
   or label set improves.

Pairwise model ranking is a different task from binary rubric drift. It should be
validated separately with `scripts/verify_judge_alignment.py` or a customer-owned
labeled comparison set.

## Consequences

- Verdict does not assume a judge is universally correct.
- Calibration data is workload-specific.
- Rubric versions should be tracked when alerts or reports depend on them.
- Deterministic checks should be used where they are more reliable than a judge,
  especially for schema validity, exact math, and executable code behavior.

## References

- Zheng et al. 2023, "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena,"
  arXiv:2306.05685
- Wang et al. 2023, "Large Language Models are not Fair Evaluators,"
  arXiv:2305.17926
- Panickssery et al. 2024, "LLM Evaluators Recognize and Favor Their Own
  Generations," arXiv:2404.13076
- Verga et al. 2024, "Replacing Judges with Juries," arXiv:2404.18796
- Tan et al. 2024, "JudgeBench," arXiv:2410.12784
