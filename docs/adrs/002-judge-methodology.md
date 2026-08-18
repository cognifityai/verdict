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

PASS rate is defined once as `PASS / (PASS + FAIL)`. `UNCLEAR`, missing
dimensions, malformed output normalized to `UNCLEAR`, and judge errors are
reported as coverage states and do not enter that denominator. No scored values
means unavailable, not zero percent.

The measuring instrument is a complete evaluator identity: provider, model
list, rubric name/version, behavior-relevant configuration, expected dimensions,
and a SHA-256 fingerprint over the effective rubric plus system/user prompt
templates. Pipeline reuse, drift windows, persisted drift signals, correlator
input, dashboard summaries, and sampled trace judgments must describe one such
identity. Historical rows without complete fields stay labeled incomplete and
are not combined with complete identities. The latest attempt per trace wins;
a latest error is coverage failure and may be retried later.

Bias mitigations:

1. **Position swap** for pairwise judgment. If order changes the result, treat
   the comparison cautiously or as a tie.
2. **Cross-family judging** for provider comparisons when practical.
3. **Length-aware review** when ranking responses so verbosity does not dominate
   preference.
4. **Per-dimension scoring** so one broad quality score does not hide the reason
   for a change.

Pairwise preference and pairwise execution status are separate axes. A valid
preference is `A_BETTER`, `B_BETTER`, `TIE`, or `INCONSISTENT`. Each position-
swap round must contain exactly one complete verdict marker. Missing, empty,
truncated, repeated, or conflicting markers are `INVALID`; provider execution
failures are `ERROR`. Invalid and error judgments have no preference verdict
and never enter a tie denominator.

An ensemble retains a component record for every configured judge and votes
only over usable components. Partial component failure remains visible even if
the remaining votes produce a usable aggregate; total failure produces no
verdict. Evidence workflows exclude unusable pairs from diagnostic agreement
metrics, report pair and component coverage, and fail their coverage gate when
any selected pair or configured component is unusable. Bradley-Terry input
accepts only the two participating model identifiers or the literal `tie` and
rejects every other winner value.

## Calibration Guidance

Users should calibrate a judge on their own workload before relying on quality
alerts or model rankings:

1. Sample recent traces.
2. Label PASS/FAIL judgments for the dimensions they care about.
3. Run `scripts/verify_rubric_alignment.py` against the labeled set.
4. Review per-dimension agreement and confidence intervals.
5. Treat low-agreement dimensions as review-only until the rubric, judge model,
   or label set improves.

For recurring monitoring, the runner can evaluate a fixed human-labeled JSONL
sentinel set. It stores only the aggregate, evaluator fingerprint, and sentinel-
set fingerprint in a separate `evaluator_health` record. `healthy` requires both
the configured minimum independently judged example count and a 95% Wilson
confidence-interval lower bound at or above the configured agreement threshold.
An example is correct only when every declared label matches. The interval and
health gate therefore use exact-match examples; label-level agreement is stored
and displayed separately as a diagnostic. Legacy label-only health rows remain
explicitly unavailable for health gating. When the caller supplies a sentinel set, a
non-healthy aggregate is persisted and blocks production judging and drift with
exit status 2. This is an anchor, not a guarantee: unchanged sentinel agreement
cannot exclude silent changes elsewhere in the provider's behavior.

Pairwise model ranking is a different task from binary rubric drift. It should be
validated separately with `scripts/verify_judge_alignment.py` or a customer-owned
labeled comparison set. A successful alignment artifact requires complete pair
and ensemble-component coverage in addition to clearing its agreement interval.

## Consequences

- Verdict does not assume a judge is universally correct.
- Calibration data is workload-specific.
- Rubric versions should be tracked when alerts or reports depend on them.
- Provider/model names alone are insufficient evaluator identity. Local prompt,
  rubric, and configuration changes must change the fingerprint; a fixed
  sentinel set is still needed to observe provider-side changes that happen
  without a local identity change.
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
