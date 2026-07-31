# Verdict Eval

PyPI distribution: `cognifity-verdict-eval`. Python import: `verdict_eval`.

The Verdict eval engine. LLM-as-judge with binary rubric, intent clustering,
non-parametric drift detection per cluster per dimension (Fisher's exact test
for binary PASS/FAIL dimensions, Mann-Whitney U for continuous metrics),
Bradley-Terry pairwise comparator for cross-LLM evaluation, and a synthetic
regression injector for verifying the pipeline catches what it should.

```python
from verdict_eval import Judge, DEFAULT_RUBRIC, DriftDetector, CorruptionInjector
```

See the [repository README](https://github.com/cognifityai/verdict#readme),
[ADR-002](https://github.com/cognifityai/verdict/blob/main/docs/adrs/002-judge-methodology.md),
and the [verification scripts](https://github.com/cognifityai/verdict/tree/main/scripts).

Apache 2.0.
