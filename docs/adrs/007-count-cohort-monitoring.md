# ADR-007: Count Cohorts for Historical Bootstrap and Monitoring

**Status:** accepted for implementation

## Decision

Verdict analyzes behavior with immutable, count-based cohorts ordered by trace
event time and stable trace ID. The same engine serves historical bootstrap,
matched POCs, and scheduled monitoring. Fixed calendar windows remain available
only as an explicit legacy mode during the alpha transition.

The first release keeps the model deliberately small:

- One trace remains Verdict's stored record. SDK traces represent one LLM call;
  local Agent Capture traces represent one agent turn. Granularity is part of
  every analysis scope, so these records cannot be mixed silently.
- The independent unit is a session when `session_id` exists, otherwise one
  trace. A session is assigned wholly to one cohort.
- Historical bootstrap sorts independent units by their earliest event time,
  splits them into equal non-overlapping older and newer cohorts, and excludes
  one middle unit when the count is odd.
- Deterministic refusal, response-length, latency, error, and token metrics can
  run without a judge or network call. Judge-derived dimensions are added only
  when one complete evaluator identity is available on both sides.
- There is no universal 30-sample gate. Results always report counts, effect
  size, uncertainty, and whether inference is evaluable or low-power.
- A scheduled monitor owns one frozen baseline and one open current cohort per
  cluster. Sparse clusters retain evidence and do not block ready clusters.
- Cluster refits create a candidate registry and candidate baseline. One atomic
  activation switches registry and baseline together. The previous generation
  remains queryable for rollback but is not a second authoritative alert source.

## State and ownership

The planner is pure and proposes membership. Storage atomically owns immutable
manifests, members, results, and the active monitor pointer. CLI/API/dashboard
consumers read persisted state; they do not recompute readiness independently.

An invocation with no new evidence or zero tested hypotheses is a no-op or
`not_evaluable`, never a completed zero-drift claim. Closed manifests never
accept late members. Late arrivals are counted and require an explicit replay.
The scheduler reloads frozen baseline traces even when they fall outside the
normal query limit. If retention has deleted baseline evidence, that series is
blocked explicitly until it is re-bootstrapped; it never closes a cohort from a
partial baseline.

## Defect-class closure matrix

| Finding | Governing contract | Last affected sink | Adjacent cases |
| --- | --- | --- | --- |
| Calendar windows age historical evidence out | Membership is immutable and event-time ordered | Dashboard result | empty/one/odd/even history, timestamp ties, reordered import |
| Thirty rows per cluster blocks first value | No global magic count | CLI/API/UI status | sparse/dominant/outlier-only clusters, low power, zero hypotheses |
| Turns in one session are correlated | Session is the independent unit | Statistical claim | missing session, one-turn and multi-turn sessions, sessions at split boundary |
| Bootstrap can leak current behavior into clusters | Fit on older cohort only | Cluster result | new intent, outlier, registry mismatch, refit candidate failure |
| Retry can duplicate or partially expose a run | Freeze membership and results atomically | Storage/API | failure before/after membership, retry, concurrent scheduler, shutdown |
| Late imports can rewrite conclusions | Closed manifests are immutable | Alert/dashboard | before/on/after tie boundary, replay, deleted trace |
| Evaluator or granularity changes can masquerade as drift | Both are required scope keys | Detector/dashboard | unknown provider/model/dimension, incomplete identity, mixed workload |
| Two baselines create conflicting authority | One active generation pointer | Alerts/UI | failed candidate, compare-and-swap loss, rollback, old history read |
| Local history can leak private source data | Adapter persists an allowlist and defaults offline | SQLite/API/browser/logs | malformed/oversized/nested source, secret/path canaries, unknown fields |

## 2026-08-30 local bootstrap correction

The first real local-history run falsified the original local bootstrap wiring.
The count monitor used lexical hashing, the separately supported registry path
was invoked with the explicit-key strategy even though uninstrumented Claude
and Codex history has no intent key, and the paid-judge pipeline still owned a
different calendar-window comparison.  Those paths cannot be combined as
evidence for the one-command local claim.

The corrective contract is:

- the bootstrap planner resolves and persists cohort membership once, before
  clustering or judging;
- uninstrumented local history fits the existing frozen MiniLM semantic
  strategy on the older cohort only; it never falls back silently to hashing;
- the current cohort is assigned to that frozen definition, and unmatched rows
  remain explicit new-intent/outlier evidence;
- one complete real evaluator identity is joined to both cohorts for quality
  analysis; fake judgments remain test-only evidence and are never presented
  as a live local result;
- paid judging performs a token/cost preflight, persists resumable completed
  judgments, and fails closed before the approved spend ceiling; and
- the count monitor, not the legacy calendar pipeline, owns the resulting
  comparison and persisted status.

### Corrective defect-class ledger

| Finding | Observed failure | Governing contract | Last affected sink | Adjacent cases and compatibility |
| --- | --- | --- | --- | --- |
| Local count bootstrap used `HashingEmbedder` | 356 lexical clusters from 1,036 real turns, including 258 singletons and one catch-all | Local uninstrumented history uses the pinned semantic strategy or stops `not_evaluable` | Registry and dashboard cluster views | empty/tiny fit, paraphrases, dominant cluster, outliers, missing model, no change to SDK capture |
| Explicit-key strategy was applied to uninstrumented history | Zero fitted clusters because every candidate lacked an intent key | Strategy selection follows evidence: explicit only for a valid explicit-key contract, semantic for local history | Cluster fit/validation output | missing/invalid/unsafe keys, hybrid input, named and tenantless scopes |
| Cluster fit and cohort planning could select different evidence | A timestamp cutoff cannot exactly express whole-session count membership when sessions span the boundary | The immutable bootstrap manifest is the sole membership authority for fit, assignment, judgment and detection | Persisted monitor members and statistical claim | odd/even units, long sessions, timestamp ties, reordered import, late arrivals |
| Real judging and count detection were separate paths | The real-judge pipeline used calendar windows while the count monitor ignored judgments | One count-cohort path joins complete evaluator identities without changing windows | Drift run, signals and dashboard judgments | PASS/FAIL/UNCLEAR/missing/error, duplicate judgments, multiple evaluator fingerprints, unknown dimensions |
| Fake judgments remained visible after a local run | All displayed dimensions were synthetic PASS and no real conclusion was possible | Fake provider is test-only unless the UI and run are explicitly labeled synthetic | Trace details, Judge Scores and drift summary | mixed fake/live rows, stale database, rerun, zero completed live judgments |
| Paid runner had no spend boundary | CLI could start an unbounded sequence of provider calls | Preflight plus a hard approved ceiling precedes every paid call; completed rows are resumable | Provider bill and CLI terminal status | price/model mismatch, retries, partial failure, interruption, usage missing, budget exhausted |
| Zero/constant hypotheses could masquerade as a successful comparison | Real-history falsification found non-finite p-values and inflated tested counts | Only finite non-constant hypotheses enter correction; zero tested hypotheses is `not_evaluable` | Stored result and dashboard status | NaN/inf, constant columns, empty cluster, sparse cluster, BH family membership |

### Alternatives considered

1. Add count-window flags to the legacy pipeline. This preserves two owners for
   membership, cluster assignment, judgment reuse and result persistence, so a
   later change can make the local and scheduled paths disagree again.
2. Keep the count monitor as the sole lifecycle owner and inject the existing
   semantic registry assignments plus complete judgments into its immutable
   cohort manifest. This reuses the canonical storage and evaluator contracts
   and removes hashing from the local default.

The second design is selected. It adds no new lifecycle state or database
table. The legacy calendar pipeline remains a compatibility command, not a
component of the count-bootstrap result.

## User-flow acceptance contract

1. A clean installed-wheel command imports Claude and Codex fixtures read-only,
   bootstraps historical cohorts, persists a deterministic result, and serves it
   in the dashboard without network access.
2. A supported 60--90 day telemetry export imported twice produces the same
   membership and immediate historical comparison without SDK instrumentation.
3. A newly instrumented sample app first reports `collecting_baseline`; a
   matched A/B run over repeated prompt IDs reports a controlled comparison.
4. Repeated scheduled invocations preserve the frozen baseline, close only
   ready cluster cohorts, survive crash/retry, classify late arrivals, confirm
   on a second independent cohort, and atomically cut over a validated refit.

SQLite, memory, and live PostgreSQL must satisfy the same storage contract.
Published-package upgrades must preserve existing traces and legacy drift runs.

## Consequences

This requires additive persisted analysis state and an upgrade migration. It
does not change public `Trace`, `Judgment`, `DriftRun`, or `DriftSignal`
constructor positions. The SDK capture hot path does not import clustering,
statistics, dashboard, or scheduler dependencies.
