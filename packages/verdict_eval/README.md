# Verdict Eval

PyPI distribution: `cognifity-verdict-eval`. Python import: `verdict_eval`.

The Verdict eval engine. LLM-as-judge with binary rubric, intent clustering,
non-parametric drift detection per cluster per dimension (Fisher's exact test
for binary PASS/FAIL dimensions, Mann-Whitney U for continuous metrics),
Bradley-Terry pairwise comparator for cross-LLM evaluation, and a synthetic
regression injector for verifying the pipeline catches what it should.

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

Pipeline reruns deduplicate by trace for the selected judge model and rubric
version. Stored judgments from a different evaluator definition are retained but
are not pooled into the current drift windows.

```python
from verdict_eval import Judge, DEFAULT_RUBRIC, DriftDetector, CorruptionInjector
```

See the [repository README](https://github.com/cognifityai/verdict#readme),
[ADR-002](https://github.com/cognifityai/verdict/blob/main/docs/adrs/002-judge-methodology.md),
and the [verification scripts](https://github.com/cognifityai/verdict/tree/main/scripts).

Apache 2.0.
