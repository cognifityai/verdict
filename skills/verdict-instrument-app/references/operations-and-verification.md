# Operations and end-to-end verification

Verification must reach the last affected sink. A successful import or mocked helper
test is insufficient evidence of trace collection or a rendered regression.

## Verify package identity first

Record the interpreter and installed distributions. Confirm the imported `verdict`
module belongs to `cognifity-verdict`, not the unrelated package named `verdict`.
Pin the synchronized `0.1.0a12` distributions and verify the installed pipeline,
probe, and dashboard commands.

## Trace capture gate

Use a synthetic request through the customer's real supported provider SDK while a
local mock server replaces the paid third-party boundary. Then inspect the final
store independently.

Verify:

- exactly one stored trace for one sampled call;
- persisted provider, model, timing, status, finish reason, and token fields expected
  for the exercised path;
- active service/environment configuration separately, while stating that release
  `0.1.0a12` does not persist those two labels on `Trace` rows;
- request and response behavior is unchanged;
- supported sync, async, and streaming variants used by the customer;
- provider exception/cancellation semantics;
- `sample_rate=0` and the approved non-zero setting;
- repeated initialization and every process entry point;
- unwritable/unavailable storage behavior; and
- shutdown/flush behavior if buffering is enabled.

Do not assert exactly-once semantics beyond the paths directly tested. Provider or
application retries may create multiple logical attempts.

## Content and privacy gate

Keep content off unless separately approved. If approved, insert only synthetic
canaries across strings, message arrays, nested tool arguments, metadata, malformed
values, and provider response variants. Query the stored rows and dashboard response,
not merely the in-memory redaction helper.

Fail closed if a prohibited canary survives. Document pattern classes not covered by
the released redactor. Never use a real credential or a real person's data as a test
value.

## Calibrate each job separately

| Job | Purpose | Frequency basis | Final sink |
|---|---|---|---|
| Drift analysis | compare baseline/current quality | eligible volume, window length, delay, judge cost/latency | persisted latest `DriftRun` |
| Probes | deterministic behavioral gate | change events plus bounded periodic coverage | JSON artifact and exit code |
| Retention | remove expired rows | retention boundary and operational load | rows older than cutoff absent |
| Health check | confirm capture/dashboard dependencies | incident-detection need and noise budget | customer's existing monitor/log sink |

Estimate how long a useful sample takes:

```text
hours_to_target = target_judgments_per_window / eligible_traces_per_cluster_hour
```

The schedule cannot be faster than the data can support. Also account for baseline
and current window separation, evaluator rate limits, sequential judge latency, and
expected cost. Calibrate with a dry-run/count query before live judging.

## Build a safe schedule

Reuse the customer's existing scheduler. For each job define:

- pinned command and working directory;
- environment/tenant/store scope;
- secret source and least-privilege OS identity;
- non-overlap lock or scheduler concurrency policy;
- timeout, retry/backoff, and maximum attempts;
- stdout/stderr or structured log destination;
- exit-code interpretation and incident owner; and
- disable/rollback command.

Run the exact command manually first. Then run a one-shot scheduler invocation and
verify the final sink. Do not schedule overlapping analysis against the same store.
Do not place a credential-bearing storage URL in the command or logs. Supply it
through the customer's protected `VERDICT_STORAGE` environment. The installed
commands log only the selected backend name.

## Drift pipeline gate

### Versioned registry gate

For the supported exact-key path, first prove that every intended call stamps a
validated `verdict.intent_context(...)` value and that routing metadata remains
non-sensitive. On an upgraded store, run the tenant-scoped, resumable
`verdict-cluster normalize` command until no pending rows remain. Then execute
`fit --strategy explicit`, `assign`, `validate`, inspect the immutable preview,
and `activate` only after the operator accepts its coverage and labels. Record
the preview and active version IDs. Test rollback after incremental assignments.

Automatic `semantic` and `hybrid` strategies are experimental opt-in paths; the
frozen release evaluation missed its fragmentation maximum. Do not enable them
for a customer path without a separate workload-specific evaluation. Shadow
analysis is disabled; `active` mode must resolve the authorized tenant's active
pointer and must never accept a browser-supplied tenant as authorization.

Before a live run prove:

- content capture was explicitly approved and the required content exists;
- windows contain enough eligible traces;
- a store contains at most one tenant scope;
- cluster assignment and version are understood;
- evaluator provider/model/rubric remain stable for comparisons;
- the user approved credential, call count, spend, and rate-limit exposure; and
- both no-signal and regression outcomes persist an atomic latest snapshot.

### Validate the clustering feature, not only cluster size

For released chat instrumentors, `prompt_redacted` is a newline join of captured
message content. A shared system prompt, policy preamble, retrieved context, or tool
schema can therefore dominate the embedding. One large cluster may meet every sample
floor while containing unrelated user intents.

Before paid judging:

1. Run the shipped semantic embedder and exact threshold on representative captured
   prompts without invoking a judge.
2. Review cluster examples and window coverage. When independent intent labels exist,
   calculate purity plus a chance-adjusted metric such as ARI or NMI. Report the
   labelled sample size and uncertainty; do not tune and score on the same examples.
3. Stop on collapse, fragmentation, window-only separation, or cluster definitions
   that do not match the regression question. Do not repair collapse by blindly
   changing the distance threshold.
4. If intent should be based on a role-aware user message, retrieved query, or another
   field the released pipeline cannot select, classify that preprocessing as an
   `unverified-adapter`. Obtain separate approval, clone into a distinct analysis
   store, preserve the source prompt and provenance hashes, clear incompatible cluster
   registries/judgments/signals, and prove every non-projection field is unchanged.

If the application already has a stable route/task taxonomy that directly answers the
regression question, prefer it over an unvalidated text-clustering threshold:

1. create a unique correlation ID before the provider call and store it as a
   non-sensitive Verdict `session_id` plus the application's own request record;
   set it with `verdict.set_context(...)`, clear it in a `finally` block, and test
   concurrent-request isolation;
2. join that correlation ID back to the real Verdict `trace_id` and require one-to-one
   parity, including errors and sampled-out calls;
3. clone traces to a separate analysis store, assign stable external cluster IDs, and
   record source trace IDs and adapter version without changing captured content;
4. hand-check or independently label a sample and stop on missing, duplicate, unstable,
   or semantically mixed assignments; and
5. run the pipeline with `--trust-existing-clusters` only after those checks pass.

Do not overwrite the capture store. Do not use the application's request or turn ID as
the Verdict `trace_id`; feedback and evidence references require a verified lookup.
External taxonomy fixes grouping only. It does not restore instructions, retrieved
evidence, or tool context omitted from captured judge input. List every field the
rubric needs and prove it exists before any paid call. If required context is absent,
stop unless a separately approved analysis projection adds it with the same
privacy/parity/provenance checks and held-out judge calibration, or independently
labelled evidence validates a deliberately narrower rubric without that context.

The dashboard must point to the resulting analysis store if that is where judgments
and drift signals live. Keep the original capture store available for audit and
rollback. A projection validated on one chat shape does not generalize to multiple
user turns, tool-only calls, multimodal blocks, or another provider's message schema.

Use `--storage` or `VERDICT_STORAGE` with `verdict-pipeline` and
`verdict-dashboard`. Reject generated schedules that mix these contracts.
Release `0.1.0a12` still constructs the selected embedder when
`--trust-existing-clusters` is used. For that external-taxonomy path, deliberately
select an installed local embedder such as `--embedder hashing` to avoid an
unnecessary MiniLM download, then run the exact command manually. This is an
implementation quirk, not evidence that the external assignments were validated.

Calibrate the approved live judge against independently human-labelled customer
examples before trusting production judgments. Keep provider, model, rubric name and
version, configuration, and prompt fingerprint fixed. Report PASS/FAIL/UNCLEAR/error
counts, chance-corrected agreement, a 95% confidence interval, and sample size. A
point estimate alone does not clear the gate.

Use synthetic judgments to test drift detection first. Label this `synthetic`; it is
not live model-quality evidence. The fake judge proves wiring only.

## Dashboard gate

Point the dashboard at the exact SQLite or PostgreSQL store containing the tested `DriftRun`.
Bind loopback unless a secure remote boundary is approved. Verify:

1. `/api/health` reports the expected database state;
2. `/api/data` returns the latest run and signal count;
3. the browser renders the regression signal and relevant metadata; and
4. a later zero-signal run replaces stale dashboard evidence correctly.

If auth is enabled, test both denied and allowed requests. Browser rendering is
required when the user asks to see the dashboard; API success alone does not prove the
visible UI. State that dashboard signals are not outbound notifications and that the
UI does not configure schedules.

Basic authentication is enabled only when both `VERDICT_USER` and `VERDICT_PASS`
are non-empty. Fail readiness when exactly one is set. Treat `/` and `/api/health`
as public even when auth is enabled; only `/dashboard` and `/api/data` are gated.

## Probe and retention gates

For probes, exercise exit `0` (pass), exit `1` (gate failure), and exit `2`
(execution error), and preserve the JSON artifact in the customer's normal CI/job
logs. Do not claim probe results appear on the dashboard.

For retention, seed rows on both sides of a synthetic cutoff, call
`prune_before(cutoff_iso)`, and independently prove only expired rows were removed.
Test the scheduled command against a disposable store before production authorization.

## Rollback and completion

Test rollback in a disposable environment: disable the schedule, stop the dashboard,
remove or disable Verdict initialization, perform a provider call, and prove provider
behavior still works while no new trace is written. Preserve the database only per
the approved retention/deletion decision.

Report exact commands, versions, paths, and results with these labels:

- `verified`: executed through the named final sink;
- `synthetic`: real Verdict logic exercised with generated data;
- `mocked`: a third-party boundary was replaced;
- `unverified`: not directly executed.

List real paths tested, adversarial and isolation cases tested, what was not tested,
and residual risk. Passing tests only support the exercised paths.
