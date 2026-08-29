# Verdict Eval

PyPI distribution: `cognifity-verdict-eval`. Python import: `verdict_eval`.

The Verdict eval engine. LLM-as-judge with binary rubric, intent clustering,
non-parametric drift detection per cluster per dimension (Fisher's exact test
for binary PASS/FAIL dimensions, Mann-Whitney U for continuous metrics),
Bradley-Terry pairwise comparator for cross-LLM evaluation, and a synthetic
regression injector for verifying the pipeline catches what it should.

## Count-cohort bootstrap and monitoring

`verdict-monitor` is the key-free path for fast historical and prospective
structural drift plus reuse of judgments already in the store. It never makes
a judge call. It groups traces by workload and capture granularity, keeps a
session together as one independent unit, orders by trace event time, and
compares equal older/newer count cohorts inside intent clusters. It does not
wait for a calendar window or require 30 observations per cluster.

For local Claude Code and Codex history, the installed `verdict-local` command
composes the core package's canonical source adapters with this monitor and the
packaged dashboard:

```bash
python -m pip install "cognifity-verdict[local]==0.1.0a13"
verdict-local
```

Run `verdict-agent-capture` instead when capture must be a standalone step.
Both commands write the same canonical Trace rows; `verdict-local` adds no
second ingestion or persistence implementation.

```bash
verdict-monitor --storage sqlite:///verdict.db bootstrap --activate --json
verdict-monitor --storage sqlite:///verdict.db run --json
verdict-monitor --storage sqlite:///verdict.db status --json
verdict-monitor --storage sqlite:///verdict.db refit --json
verdict-monitor --storage sqlite:///verdict.db activate \
  --series-id <candidate> --expected-active <active> --json
```

The prospective cohort size is derived from the historical baseline and capped
at 10 unless `--target-units` is supplied. `--from` and `--through` select an
explicit timezone-aware historical slice. Results report `low_power` or
`not_evaluable` rather than turning insufficient evidence into “no drift.” A
cohort that does not fit the frozen registry produces a separate
`new_intent_traffic` signal and does not contaminate known-intent comparisons.
Completed judgments enter separate scopes keyed by complete evaluator
fingerprint. PASS/FAIL supplies each dimension's pass-rate denominator;
UNCLEAR and missing dimensions remain separately visible. Constant columns are
descriptive, have `tested=false`, and do not enter multiplicity correction or
the `tested_hypotheses` count.

For a controlled POC, stamp the same `verdict.intent_key` around both model
variants and run:

```bash
verdict-monitor --storage sqlite:///verdict.db matched \
  --baseline-model model-a --current-model model-b --json
```

Historical, scheduled, and matched commands persist completed snapshots that
the packaged dashboard can display. The legacy judge pipeline below keeps its
calendar-window methodology for compatibility.

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

The versioned registry requires a deliberate `verdict-cluster fit --strategy`
choice; no registry strategy is silently selected. Exact-key `explicit`
clustering is supported. Automatic `semantic` clustering and the semantic
fallback inside `hybrid` are experimental opt-in alpha features. Their frozen
quality evaluation missed one preregistered fragmentation gate (largest
nonoutlier cluster `30.1047%`, maximum `30%`) and must not be described as
generally validated. `verdict-cluster inspect` reports the strategy and this
experimental status. Local semantic work uses the frozen
`sentence-transformers/all-MiniLM-L6-v2` model; runtime download is forbidden.
The legacy trace clustering pipeline remains a separate methodology.

### Supported explicit registry workflow

Stamp a bounded, redaction-safe routing key around the provider request that
owns the intent. The context is token-restoring and the raw key is stored only
as the existing trace routing tag:

```python
import verdict

with verdict.intent_context("billing.v1"):
    response = provider.messages.create(...)
```

Use the real tenant ID for tenant-owned traces. For tenantless Memory/SQLite
stores only, use the reserved local scope `__verdict_local__`; that literal is
not a customer tenant ID. Existing a7 databases start with pending derived
analysis fields, so normalize bounded pages until the JSON result says
`"complete": true`:

```bash
verdict-cluster --storage sqlite:///verdict.db --tenant tenant-a --actor ops \
  normalize --limit 1000
verdict-cluster --storage sqlite:///verdict.db --tenant tenant-a --actor ops \
  fit --strategy explicit --target-workload agent \
  --cutoff 2026-08-22T00:00:00Z
```

Take `version_id` from the fit result, then assign and validate the immutable
preview before activation:

```bash
verdict-cluster --storage sqlite:///verdict.db --tenant tenant-a --actor ops \
  assign --version "$VERSION" --through-cutoff 2026-08-22T00:00:00Z
verdict-cluster --storage sqlite:///verdict.db --tenant tenant-a --actor ops \
  validate --version "$VERSION"
verdict-cluster --storage sqlite:///verdict.db --tenant tenant-a --actor ops \
  activate --version "$VERSION" --expected-generation 0
verdict-pipeline --storage sqlite:///verdict.db --registry-mode active \
  --tenant-id tenant-a
```

Active mode is always pinned to the tenant's active pointer. `inspect` returns
bounded version, cluster, stable display-name, assignment, and event data plus
truncation flags; its `--*-limit` and `--*-offset` options page immutable detail
within hard output ceilings. `rename` changes only a stable display name; `rollback`
requires a previously activated version and the current expected generation.
CLI failures use a closed safe code such as `analysis_index_pending`,
`model_unavailable`, `validation_failed`, or `generation_conflict`; raw storage
and provider exception text is not printed. Semantic and hybrid commands also
require a reviewed local `--model-path` and remain experimental.

Registry shadow analysis is disabled pending the tenant-isolation correction in
[issue #24](https://github.com/cognifityai/verdict/issues/24). Validate an
inactive preview with `verdict-cluster validate`; do not analyze it through the
drift pipeline before activation.

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

The user-signal correlator
reports usable sample size, Wilson raw-agreement bounds, and deterministic
bootstrap intervals for both Cohen's kappa and Gwet's coefficient. It refuses to
call low-data output calibrated, excludes `UNCLEAR` judge results from its binary
confusion matrix, and requires an explicit evaluator selection when identities
are mixed. Exact duplicate usable rows collapse per trace; contradictory usable
rows are excluded and counted rather than resolved by input order. Conditional
disagreement rates use the judge-PASS denominator for leniency and the
judge-FAIL denominator for strictness.

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
