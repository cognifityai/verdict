---
name: verdict-instrument-app
description: Inspect an existing LLM application and plan, implement, or audit a safe Verdict by Cognifity integration. Use for locating supported Python provider calls, placing process-level initialization, choosing SQLite or Postgres trace storage, configuring capture and retention, scheduling evaluation or probe jobs, launching the bundled dashboard, and verifying regression evidence. Do not use for generic observability work or for adding features to Verdict itself.
---

# Instrument an App with Verdict

Instrument only behavior supported by the released Verdict target, and distinguish
what was executed from what was merely proposed. Verdict `0.1.0a4` is a Python
public alpha, not a production-readiness claim.

## Resolve paths before doing anything

Set `<skill-root>` to the absolute directory containing this `SKILL.md`. Resolve
`<verdict-repo>` independently to the pinned Verdict source checkout that supplies
`scripts/run_drift_pipeline.py` and `ui/server.py`. If this skill is being read from
`<verdict-repo>/skills/verdict-instrument-app`, two directories above is a candidate,
not proof: verify the Git commit and both required files. A native skill installer may
copy the skill elsewhere, so never infer the source checkout from the installed skill
location. If no checkout exists, ask for its absolute path or approval to create a
pinned checkout. Resolve both paths once and use absolute paths in every command. The
customer repository is usually a third checkout and working directory; never assume
`scripts/` or `ui/` means the customer's current directory.

Verify the selected source checkout before using repository-local tools:

```bash
python3 <skill-root>/scripts/verify_verdict_checkout.py \
  /absolute/path/to/verdict --format json
```

Stop if it does not report `"ready": true`; do not silently substitute another
checkout or package version.

## Preserve the authority boundary

- Inspect the repository before asking questions. Batch only decisions that cannot
  be discovered safely.
- Present the proposed files, dependencies, commands, data flow, risks, tests, and
  rollback before editing customer code or installing anything.
- Obtain explicit approval before code edits, dependency installation, new storage,
  persistent/background services, schedules, or paid/live provider calls.
- Treat content capture as a separate opt-in decision. Never infer it from approval
  to collect metadata.
- Keep credentials in the customer's existing secret mechanism. Never print or
  persist secrets, raw production traces, or sensitive samples in generated docs.
- Use synthetic data and mocked provider boundaries for initial verification. Do not
  write to production systems during setup tests.

## Follow this workflow

### 1. Resolve the exact target

Read repository instructions and existing observability, privacy, deployment, and
scheduler conventions. Identify the released Verdict version to use. For the
compatibility target and its concrete limitations, read
[`references/verdict-0.1.0a4.md`](references/verdict-0.1.0a4.md).

Do not treat an unpublished local Verdict worktree as released functionality.

### 2. Discover the runtime paths

Run the read-only scanner from its resolved skill path:

```bash
python3 <skill-root>/scripts/scan_repository.py \
  /absolute/path/to/customer/repository --format json
```

Then validate every candidate against the actual code. Trace supported provider
calls backward to all process entry points and forward to the final storage owner.
Identify web, worker, CLI, serverless, and test processes separately. Follow
[`references/discovery-and-placement.md`](references/discovery-and-placement.md).

The scanner reports candidates, not proof. Do not instrument a comment, test fixture,
generated file, wrapper name, or documentation example as if it were a live call.

### 3. Classify support before proposing edits

Classify each live path as:

- `supported`: exact released provider and method, language, and lifecycle can be
  exercised through final storage;
- `supported-with-constraints`: the released path works only with an explicit
  storage, privacy, shutdown, or deployment condition;
- `unverified-adapter`: a custom/manual span could cover it, but no released
  instrumentor supports it; or
- `unsupported`: no safe released integration path.

Prefer released instrumentors. Custom adapters must be small, separately approved,
clearly marked unverified, and tested against a controlled provider response.

Separately identify whether the application already has a stable route, task, mode,
or human-owned intent taxonomy. That field can be more faithful than text clustering,
but only after a trace-for-trace correlation and provenance check. It does not turn
an unsupported provider call into a supported capture path.

### 4. Resolve material configuration

Ask one batched set of questions after discovery. Cover only unresolved choices:

- environments, processes, providers, and call sites in scope;
- metadata-only versus prompt/response content;
- trace storage owner and an explicit retention period;
- tenant isolation and stable pseudonymous context fields;
- analysis jobs, evaluation budget/provider, minimum useful detection delay;
- the regression question, any existing stable taxonomy, and independently labelled
  examples that can validate cluster meaning and the judge;
- existing scheduler and dashboard exposure boundary; and
- authorization to edit, install, launch, and schedule.

Recommend an absolute SQLite path for a local single-host trial and Postgres for
shared capture. State that the bundled dashboard reads SQLite only and that the
`0.1.0a4` drift runner echoes its `--storage` value; do not pass a credential-bearing
Postgres URL into that runner or its logs. Use
[`references/configuration-and-risk.md`](references/configuration-and-risk.md) to
generate the plan and risk register.

### 5. Get approval for the exact plan

The plan must name:

1. files and dependency changes;
2. one initialization owner per process, before the first supported provider call;
3. storage URI source, permissions, retention, dashboard data path, and
   service/environment isolation because those labels are not persisted on trace rows;
4. capture/redaction settings and known residual privacy risk;
5. each job's command, trigger, lock, timeout, logs, credentials, and rollback;
6. expected provider/judge volume and cost boundary; and
7. verification through the final stored trace and final regression sink.

The plan must also name which of these POC outcomes is in scope:

- `capture`: metadata reaches the approved store;
- `quality`: approved content, meaningful clusters, and a calibrated judge exist; or
- `regression`: independent baseline/current windows reach the required sample floor
  and produce a persisted latest run.

Do not describe a capture-only POC as a regression POC.

Do not implement until the user approves this plan.

### 6. Implement the smallest reversible slice

Start with one environment, one process, one provider path, and metadata-only
capture. Initialize Verdict once per process before provider calls. Default to:

```python
import verdict

verdict.init(
    service_name="<stable-service-name>",
    environment="<explicit-environment>",
    storage="sqlite:////absolute/path/to/verdict.db",
    capture_content=False,
    buffered_writes=False,
)
```

Preserve request/response semantics and existing exception handling. Do not add
per-request initialization. If buffered writes are later approved, import
`shutdown` from `verdict.client` and exercise `shutdown()` on every normal and
cancellation path. It is not exported as `verdict.shutdown` in `0.1.0a4`.

### 7. Verify the last affected sink

Exercise a real supported provider SDK against a local mock server, then query the
configured storage independently. Prove the expected sampled call creates exactly
one trace with correct provider/model/status fields. Test sync/async/stream variants
only when they exist in the customer path. Also test provider failure, storage
failure, sampling, repeated initialization, and shutdown behavior relevant to that
path.

If content capture is approved, use synthetic canaries to recursively test nested
prompt, response, tool, and metadata structures. Do not claim that released
redaction is a compliance control. Stop if required sensitive fields survive.

Before any live judging, validate the actual clustering feature on representative
captured prompts. Released chat instrumentors flatten captured messages into
`prompt_redacted`. A long repeated system prompt can dominate embeddings and collapse
distinct user intents; in other applications the varying system or task field may be
the only intent signal. Define the intended grouping before choosing a feature. Do
not treat cluster count, sample size, or a large cluster as proof of semantic quality.

Prefer an existing, customer-owned stable route/task taxonomy when it matches the
regression question. Correlate each application request to exactly one Verdict trace,
clone the source records into a separate analysis store, assign stable cluster IDs,
and validate counts, IDs, non-projection field parity, and a labelled sample. Then use
`--trust-existing-clusters`. Never use an application turn ID as a Verdict trace ID
without a verified lookup. If no trustworthy taxonomy exists, validate shipped text
clustering against independently labelled examples. A role-aware projection or
external assignment remains an `unverified-adapter`: preserve the capture store,
record provenance, obtain approval, and stop if validation fails.

External taxonomy repairs grouping only. It does not add task instructions, system
prompts, retrieved evidence, or tool context that the released instrumentor omitted.
Before judging, prove the captured or approved analysis record contains every input
the rubric requires. Otherwise stop. Continue only after a separately approved
analysis projection supplies the missing context with trace parity, provenance,
privacy tests, and held-out judge calibration, or after independent labels prove a
narrow rubric remains valid without that context.

Follow [`references/operations-and-verification.md`](references/operations-and-verification.md)
for drift, probe, retention, dashboard, and rollback verification.

### 8. Schedule jobs outside the dashboard

Discover and recommend analysis, probes, retention cleanup, and health checks
separately. Reuse the customer's existing cron, systemd, GitHub Actions, Kubernetes,
or cloud scheduler. Use a non-overlap lock. Base frequency on eligible trace volume,
elapsed detection delay, provider latency, and evaluation cost—not a universal cron.

Run one approved command manually through its final sink before enabling its
schedule. The bundled dashboard does not configure schedules.

Generate the command from `<verdict-repo>/scripts/run_drift_pipeline.py --help` and
test that exact command. Release `0.1.0a4` does not accept `--yes-spend` or
`--max-spend-usd`; enforce approval, call ceilings, credentials, timeouts, and budget
outside the runner. Do not schedule a helper script that is absent from the pinned
checkout.

### 9. Show regression evidence accurately

The current dashboard signal is the latest persisted `DriftRun` in SQLite. Launch it
locally by default and verify `/api/health`, `/api/data`, and the rendered regression
view. Postgres capture needs a separately designed, approved materialization path;
do not imply direct dashboard support. Probes produce JSON and exit codes and do not
automatically appear as dashboard alerts. Do not promise outbound notifications
unless the installed release actually supports them.

## Stop instead of guessing

Stop and request direction when:

- the live path is outside released Python instrumentors;
- a required privacy/retention owner or content-capture decision is missing;
- SQLite locality, Postgres sharing, and dashboard needs conflict;
- live evaluator credentials, calibration, or spend limits are unresolved;
- available volume cannot meet the requested detection delay;
- representative clustering collapses or fragments and no approved, validated
  analysis feature exists;
- a non-local dashboard lacks authentication and a trusted TLS boundary; or
- a real supported SDK call and final storage/dashboard sink cannot be tested.

## Hand off complete evidence

Generate customer documentation covering setup, data flow, collected fields,
redaction limits, storage and retention, job schedules, dashboard access, rollback,
tested paths, untested paths, and residual risk. Label evidence `verified`,
`synthetic`, `mocked`, or `unverified`; never collapse those labels into a generic
"working" claim.
