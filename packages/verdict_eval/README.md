# Verdict Eval

PyPI distribution: `cognifity-verdict-eval`. Python import: `verdict_eval`.

The Verdict eval engine. LLM-as-judge with binary rubric, intent clustering,
non-parametric drift detection per cluster per dimension (Fisher's exact test
for binary PASS/FAIL dimensions, Mann-Whitney U for continuous metrics),
Bradley-Terry pairwise comparator for cross-LLM evaluation, and a synthetic
regression injector for verifying the pipeline catches what it should.

## Pairwise result contract

`PairwiseJudge.compare()` separates preference from execution state. A usable
`PairwiseJudgment` has `status == PairwiseStatus.VALID`, `is_usable == True`,
and a verdict of `A_BETTER`, `B_BETTER`, `TIE`, or `INCONSISTENT`. Exactly one
complete `[[A]]`, `[[B]]`, or `[[C]]` marker is required in each position-swap
round. Missing, empty, truncated, repeated, or conflicting markers produce
`PairwiseStatus.INVALID`; provider failures produce `PairwiseStatus.ERROR`.
Both unusable states carry `verdict=None` and must not be converted to ties.

Ensembles preserve one component record per configured judge and vote using
only usable components. An aggregate can remain usable when at least one
component is usable, but failed components remain visible in
`component_judgments`. A total component failure is unusable. The alignment
harness reports pair and component coverage separately and fails its evidence
gate when either is incomplete.

This does not change captured traces, spans, or storage schemas. Existing
successful 0.1.0a3 positional construction retains its original field order;
the status fields were appended. Consumers should check `is_usable` before
reading `verdict`:

```python
from verdict_eval import PairwiseJudge, PairwiseStatus

judgment = PairwiseJudge(provider=provider, model=model).compare(
    query=query,
    response_a=response_a,
    response_b=response_b,
)
if judgment.status is not PairwiseStatus.VALID:
    raise RuntimeError("pairwise comparison was not usable")
winner = judgment.verdict
```

The repository pipeline uses a persisted cluster registry rather than
re-clustering the full dataset on every run. Local
`sentence-transformers/all-MiniLM-L6-v2` embeddings are the semantic default;
`0.50` cosine distance is the shipped starting threshold, not a universal
cutoff. The built-in hash embedder is an explicit lexical fallback. Change the
embedding model or threshold only with a new clustering version and a one-time
`--recluster` for existing traces. `--trust-existing-clusters` is reserved for
stable cluster IDs assigned outside Verdict.

Drift is a batch comparison over each captured trace's `started_at` time. The
runner defaults to a 24-hour current window and a 7-day baseline separated by a
24-hour gap, with at least 30 judgments per `(cluster, dimension)` window. A
signal must clear the BH-adjusted p-value gate and the Cliff's delta effect-size
gate. On binary PASS/FAIL data the default `0.147` delta is a 14.7 percentage-
point sensitivity floor. These defaults require workload-specific validation.

Pipeline reruns use the latest attempt per trace for one complete evaluator
identity: provider, model list, rubric name/version, behavior-relevant
configuration, expected dimensions, and effective prompt/rubric fingerprint. A
latest error is excluded from PASS/FAIL and can be retried. Other evaluator
definitions are retained but not pooled. Persisted drift signals carry the same
fingerprint. Each completed analysis atomically persists a `DriftRun` marker and
its exact signal set, including zero-signal runs; latest-run consumers exclude
legacy ungrouped signals. Optional fixed human-labeled sentinel runs store independent judge-
health aggregates. A healthy status requires both the independent-example floor
and the 95% Wilson-interval lower bound to clear the configured threshold. An
example passes only when every declared label matches; label agreement is a
separate diagnostic, not the gate's statistical unit. Legacy label-only records
remain unavailable for health gating. Any sentinel execution error prevents a
`healthy` result: too few usable examples remain `insufficient_data`;
otherwise the result is `degraded`. When a sentinel file is supplied,
the runner persists the health record and exits 2 before production judgments or
drift unless status is `healthy`.

Probe weights enter suite and category quality gates once per probe. A probe
passes only when every declared expectation passes. Weighted expectation
agreement and its per-dimension breakdown remain separate diagnostics; adding
expectations cannot make a failing probe count less in the quality gate.
The bundled weighted suite is version `2.1`; its direct prompt-injection probe
defines the quoted-text instruction precedence independently for both safety
and instruction-following judgments. New `ProbeRun` and `ProbeResult`
artifacts stamp metric-schema version `3` and one-dimension judge-method version
`2`; historical artifacts without those fields remain version `1` when loaded
through the dataclasses, so scheduled comparisons cannot silently cross the
methodology boundary. Each expectation is judged with a one-dimension rubric
and records its effective evaluator fingerprint. A caller-supplied `Judge` or
`JudgeEnsemble` is narrowed consistently while preserving its rubric, provider,
model, temperature, and token configuration. Probe expectation verdicts accept
only the exact labels `PASS` and `FAIL`; malformed programmatic or YAML suite
definitions fail during construction instead of being normalized into a scored
outcome. Target or follow-up execution errors emit an `ERROR` result for every
declared expectation, so outages remain in every dimension denominator. The
scheduled CLI requires a 100% weighted probe pass rate by default, exits
1 below the configured threshold, and exits 2 on provider/judge execution errors.
Non-positive, non-finite, or non-numeric weights in historical result JSON
contribute zero rather than crashing or corrupting an aggregate. Historical
dimension entries whose `passed` field is not a literal boolean fail closed.
Current artifacts with missing, unnamed, duplicate, non-dictionary, or
contradictory expectation rows cannot pass the probe gate. The user-signal correlator
reports usable sample size, Wilson raw-agreement bounds, and deterministic
bootstrap intervals for both Cohen's kappa and Gwet's coefficient. It refuses to
call low-data output calibrated, excludes `UNCLEAR` judge results from its binary
confusion matrix, and requires an explicit evaluator selection when identities
are mixed. Exact duplicate usable rows collapse per trace; contradictory usable
rows are excluded and counted rather than resolved by input order. Conditional
disagreement rates use the judge-PASS denominator for leniency and the
judge-FAIL denominator for strictness.
Probe JSON artifacts apply Verdict's best-effort pattern redaction to
captured target text, judge reasoning, and provider errors before returning the
serializable run result.

```python
from verdict_eval import (
    DEFAULT_RUBRIC,
    CorruptionInjector,
    DriftDetector,
    Judge,
    PairwiseJudge,
    PairwiseStatus,
)
```

See the [repository README](https://github.com/cognifityai/verdict#readme),
[ADR-002](https://github.com/cognifityai/verdict/blob/main/docs/adrs/002-judge-methodology.md),
and the [verification scripts](https://github.com/cognifityai/verdict/tree/main/scripts).

Apache 2.0.
