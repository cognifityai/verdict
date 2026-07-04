# verdict-eval — Eval engine

The Verdict eval engine. LLM-as-judge with binary rubric, intent clustering,
non-parametric drift detection per cluster per dimension (Fisher's exact test
for binary PASS/FAIL dimensions, Mann-Whitney U for continuous metrics),
Bradley-Terry pairwise comparator for cross-LLM evaluation, and a synthetic
regression injector for verifying the pipeline catches what it should.

```python
from verdict_eval import Judge, DEFAULT_RUBRIC, DriftDetector, CorruptionInjector
```

See the parent repo [README](../../README.md), ADRs in `../../docs/adrs/`
(especially ADR-002 on judge methodology), and verification scripts in
`../../scripts/`.

Apache 2.0.
