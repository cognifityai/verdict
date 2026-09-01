# Verdict Roadmap

This document describes the boundary between the current v0 implementation and
future agent-level observability work. It is a public roadmap, not a delivery
commitment.

## Current Scope

Verdict's established path focuses on the LLM-call layer inside LLM apps and
agents. It can capture supported provider calls or import existing telemetry,
store traces, evaluate responses with a rubric, cluster similar prompts, and
inspect quality or cost changes over time. The local Claude Code/Codex adapter
also stores typed, bounded source sessions, runs, turns, and observable events
without relabeling them as provider calls.

Today, Verdict can capture or import and evaluate calls such as:

- Planning prompts
- Tool-selection prompts
- Replanning prompts
- Final-response prompts
- Regular chat or completion calls made by an LLM application
- OTLP GenAI/OpenInference LLM spans
- Langfuse, LangSmith, Datadog LLM Observability, Phoenix, and Opik exports
- MLflow tracing files and bounded text-only voice conversation exports

Each captured or imported LLM call is still stored independently. The pipeline
then samples eligible stored calls for judging. Local history projection is a
first-class evidence view, but it is not a complete runtime graph and does not
invent links to genuine provider calls that the source cannot establish.

## Planned Agent-Level Work

The next layer is deeper agent-run observability: connecting genuine LLM calls,
framework events, authoritative artifacts, and outcomes into one execution
graph beyond the source-bounded local projection that now ships.

### Agent-Run Tracing

Planned capabilities beyond the current typed evidence record:

- Link genuine LLM calls and spans to the run that produced them when source
  provenance establishes the relationship.
- Track run-level metadata such as total steps, latency, token usage, cost, and
  terminal status.
- Support rollups by run, intent cluster, model, provider, and time window.

### Tool-Call Instrumentation

Planned capabilities:

- Extend current Claude Code/Codex history normalization to common agent
  frameworks and provider SDKs.
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

Verdict should remain easy to adopt alongside existing observability stacks.
Existing telemetry readers normalize allowlisted source fields directly into
the current Verdict `Trace` schema; they do not duplicate raw vendor envelopes
or alter downstream statistics. Planned outbound integration work is to export
Verdict traces, judgments, and drift signals through standard formats where
possible.

Areas under consideration:

- An authenticated remote-ingestion service so production SDKs do not require
  database credentials or one PostgreSQL connection pool per application
  process
- Durable batching, retry, backpressure, and idempotency between remote
  producers and storage
- OpenTelemetry and OpenInference-compatible export paths
- Additional source contracts through maintained OSS packages when they reduce
  format-specific maintenance
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
- The v0 drift runner supports one tenant scope per store and refuses to pool a
  mixed-tenant database.
- Cost values are estimates from a dated static base-price table; caching,
  long-context tiers, batch/priority modes, tools, regional uplifts, and
  negotiated discounts are not modeled.
- Content redaction is best-effort pattern redaction, not a compliance guarantee.
- Reversible encrypted redaction is not implemented.
- Production users should configure storage, retention, access control, and
  judge calibration for their own environment.
- Provider SDKs change over time; live capture checks should stay part of the
  release process.
- Hosted telemetry APIs also change over time. Synthetic contract tests prove
  local mapping and pagination behavior, not live compatibility with every
  hosted deployment; credentialed pre-release checks remain required.
- Judge calls run sequentially. Verdict does not yet retain judge token/cost
  usage or enforce an evaluation budget.
- Local tool-call sequences are first-class source evidence and support bounded
  deterministic findings. Manual spans and supported provider traces still do
  not automatically reconstruct an authoritative cross-source agent graph or
  prove task success.
- Cache-token accounting and cache-aware pricing are not modeled.
- Stable intent clusters have IDs and health diagnostics, but no automatic
  human-readable naming or fragmented-cluster merge operation.
- PostgreSQL capture currently connects from each instrumented process through
  a process-local driver pool. Standard remote PostgreSQL URLs are supported,
  but Verdict does not yet provide a durable remote-ingestion gateway; network
  persistence failures can therefore leave a trace uncaptured.

## Prioritized Product Follow-ups

These are product additions, not correctness fixes. They require a pilot need
and an approved design before implementation.

1. **Judge cost and budget visibility (medium effort, high value for paid
   recurring evals):** retain usage, estimate judge cost, persist run budgets,
   and define partial-run/stop semantics.
2. **Cache-token accounting (medium effort, high value for cache-heavy
   workloads):** add provider-normalized cache fields, migrations, and pricing
   reconciliation.
3. **Concurrent judging (medium effort, latency value):** bounded concurrency,
   provider rate-limit handling, cancellation, deterministic output, and load
   tests. This reduces wall time, not token spend.
4. **Remote ingestion gateway (large effort, high production-deployment
   value):** add an authenticated, horizontally scalable HTTPS/OTLP ingestion
   boundary with batching, durable retry/spooling, backpressure, idempotency,
   tenant isolation, and separate schema-migration credentials. Keep direct
   SQLite/PostgreSQL storage as the simple local and embedded option.
5. **Framework-wide agent outcomes (large effort, high agent-workload value):**
   extend the shipped typed sequence/privacy/storage/UI contract with genuine
   provider-call links, authoritative task outcomes, and framework adapters.
6. **Cluster naming (small/medium effort, usability value):** prefer explicit
   customer labels; any generated name needs versioning and privacy controls.
7. **Cluster fusion (large, high-risk effort):** requires offline quality
   evaluation, immutable aliases/history, migration, rollback, and continuity
   across drift windows. v0 keeps explicit health warnings/reclustering instead.
8. **Dependency/packaging cleanup (small/medium maintenance value):** handle as
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
clustering, drift inspection, dashboard tooling, and source-bounded local
agent-run/tool-sequence evidence. Cross-source execution graphs, authoritative
task-success tracking, and outcome-backed run metrics are planned work.
