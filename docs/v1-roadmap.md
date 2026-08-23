# Verdict Roadmap

This document describes the boundary between the current v0 implementation and
future agent-level observability work. It is a public roadmap, not a delivery
commitment.

## Current Scope

Verdict v0 focuses on the LLM-call layer inside LLM apps and agents. It can
capture supported provider calls, store traces, evaluate responses with a rubric,
cluster similar prompts, and inspect quality or cost changes over time.

Today, Verdict can capture and evaluate calls such as:

- Planning prompts
- Tool-selection prompts
- Replanning prompts
- Final-response prompts
- Regular chat or completion calls made by an LLM application

Each captured LLM call is stored and evaluated independently. Verdict v0 does not
yet reconstruct a full multi-step agent run as a first-class object.

## Planned Agent-Level Work

The next layer of Verdict is agent-run observability: connecting individual LLM
calls, tool calls, and outcomes into a single execution graph.

### Agent-Run Tracing

Planned capabilities:

- Represent an agent run as a first-class record.
- Link LLM calls, spans, and tool calls to the run that produced them.
- Track run-level metadata such as total steps, latency, token usage, cost, and
  terminal status.
- Support rollups by run, intent cluster, model, provider, and time window.

### Tool-Call Instrumentation

Planned capabilities:

- Capture tool or function calls emitted by common agent frameworks and provider
  SDKs.
- Store tool name, arguments, result metadata, latency, and success or failure.
- Inspect tool-use sequences within an agent run.
- Detect changes in retry rate, tool-selection patterns, and escalation paths.

### Task-Success Signals

Planned capabilities:

- Let applications attach explicit success or failure outcomes to an agent run.
- Support user feedback, business outcome webhooks, or application-defined
  success criteria.
- Compare task-success rates across model, prompt, release, provider, and
  workload segments.

### Plan-Adherence Scoring

Planned capabilities:

- Capture explicit plans when an agent produces them.
- Compare later steps against the stated plan.
- Track plan-deviation rate over time.

### Run-Level Cost And Reliability Metrics

Planned capabilities:

- Cost per successful run
- Average steps per run
- Retry and escalation rate
- Error rate by model, provider, cluster, and release
- Latency distribution by run type

## Integration Direction

Verdict should remain easy to adopt alongside existing observability stacks. The
planned integration work is to export Verdict traces, judgments, and drift
signals through standard formats where possible.

Areas under consideration:

- OpenTelemetry and OpenInference-compatible export paths
- Additional provider instrumentation through maintained OSS packages
- Optional model-provider abstraction for evaluation calls
- Optional specialized evaluators for domains such as RAG faithfulness

## Known Boundaries In v0

- Verdict v0 is LLM-call observability, not a full agent runtime.
- The local dashboard is intended for localhost or trusted-network use. Its v0
  JavaScript and CSS are pre-built local assets, so rendering does not require
  public CDN access. Put TLS and authentication in front of it before any
  non-local deployment.
- Quality judging and drift detection are periodic batch operations, not a
  streaming alert service.
- The legacy trace-level runner supports one tenant scope per store and refuses
  to pool a mixed-tenant database. Conversation-v1 is explicitly
  tenant/version scoped.
- Cost values are estimates from a dated static base-price table; caching,
  long-context tiers, batch/priority modes, tools, regional uplifts, and
  negotiated discounts are not modeled.
- Content redaction is best-effort pattern redaction, not a compliance guarantee.
- Reversible encrypted redaction is not implemented.
- Production users should configure storage, retention, access control, and
  judge calibration for their own environment.
- Provider SDKs change over time; live capture checks should stay part of the
  release process.
- Judge calls run sequentially. Verdict does not yet retain judge token/cost
  usage or enforce an evaluation budget.
- Tool-call sequences are not a first-class capture/evaluation unit. Manual
  spans and supported provider traces now carry parent linkage, but they do not
  reconstruct an agent-run graph or provide agent-run scoring.
- Cache-token accounting and cache-aware pricing are not modeled.
- Stable intent clusters have IDs and health diagnostics, but no automatic
  human-readable naming or fragmented-cluster merge operation.

## Prioritized Product Follow-ups

These are product additions, not correctness fixes. They require a pilot need
and an approved design before implementation.

1. **Judge cost and budget visibility (medium effort, high value for paid
   recurring evals):** retain usage, estimate judge cost, persist run budgets,
   and define partial-run/stop semantics.
2. **Cache-token accounting (medium effort, high value for cache-heavy
   workloads):** add provider-normalized cache fields, migrations, and pricing
   reconciliation.
3. **Legacy-runner concurrent judging (medium effort, latency value):** the
   conversation-v1 path has bounded concurrency, provider deadlines and
   deterministic terminal snapshots; extending that contract to the legacy
   trace-level runner still needs its own compatibility/load tests. This
   reduces wall time, not token spend.
4. **First-class tool-call/run evaluation (large effort, high agent-workload
   value):** requires a versioned sequence schema, privacy model, storage/UI,
   and task-level evaluation contract.
5. **Cluster naming (small/medium effort, usability value):** prefer explicit
   customer labels; any generated name needs versioning and privacy controls.
6. **Cluster fusion (large, high-risk effort):** requires offline quality
   evaluation, immutable aliases/history, migration, rollback, and continuity
   across drift windows. v0 keeps explicit health warnings/reclustering instead.
7. **Dependency/packaging cleanup (small/medium maintenance value):** handle as
   a standalone clean-install/release-artifact change rather than mixing it into
   behavioral remediation.

## Engineering Principles

- Keep the SDK lightweight.
- Prefer transparent local workflows before hosted assumptions.
- Use maintained OSS libraries when they reduce provider or format maintenance.
- Keep public claims tied to reproducible tests or scripts.
- Separate current functionality from planned functionality clearly.

## Current Feature Boundary

Verdict currently ships LLM-call capture, local storage, rubric evaluation,
clustering, drift inspection, and dashboard tooling. Agent-run graphs, tool-call
sequence analysis, task-success tracking, and run-level outcome metrics are
planned work.
