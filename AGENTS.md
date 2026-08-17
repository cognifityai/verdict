# Verdict Contributor Guide

This file gives humans and coding agents enough context to work in the repo
without re-learning the architecture each session.

## What Verdict Is

Verdict is an open-source LLM observability and drift-detection toolkit. It
captures supported provider calls, stores traces, evaluates responses with a
rubric, and helps inspect quality, cost, latency, and drift across similar
workloads.

Current scope: v0 operates at the individual LLM-call layer. Agent-run graphs,
tool-call sequence analysis, plan adherence, and task-success metrics are planned
work and should not be described as current functionality.

## Vocabulary

- **Trace**: one captured LLM request/response pair plus metadata.
- **Span**: a manually recorded non-LLM unit of work.
- **Judge**: a model that scores responses using a rubric.
- **Rubric**: PASS/FAIL dimensions such as groundedness, relevance,
  completeness, safety, and instruction following.
- **Cluster**: a group of similar prompts used for like-with-like comparison.
- **Reference baseline**: the historical distribution used for drift comparison.
- **Drift signal**: a statistically meaningful change for a cluster/dimension.
- **Probe**: a configured external evaluation run.
- **Shadow traffic**: sampled traffic mirrored to another model for comparison.

## Architecture Principles

1. Keep core logic separate from provider SDKs, storage engines, and UI code.
2. Put external systems behind interfaces and keep in-memory adapters for tests.
3. Capture through supported SDK instrumentation; do not require users to rewrite
   their application around Verdict-specific clients.
4. Keep content capture opt-in and redact before persistence when it is enabled.
5. Keep provider names and model IDs data-driven where possible.
6. Make validation workflows reproducible with scripts and tests.
7. Prefer maintained OSS libraries when they reduce provider, parser, or
   statistical edge-case maintenance.

## Current Boundaries

- The SDK stores Verdict's own `Trace`, `SpanRecord`, `Judgment`, and
  `DriftSignal` schemas. It does not currently emit OpenTelemetry or
  OpenInference spans.
- SQLite is the default local store. Postgres is available for shared
  environments.
- Redaction is best-effort pattern matching plus Luhn validation for payment-card
  candidates. It is not a compliance guarantee.
- `encrypt` redaction mode is rejected at init time; reversible encryption is not
  implemented.
- The local dashboard is intended for localhost or trusted-network use.

## Working Conventions

- Maintain ADRs in `docs/adrs/` for major architecture decisions.
- Do not import provider SDKs in core evaluation or storage modules.
- Keep tests next to the package they cover.
- Use the in-memory storage/provider adapters in unit tests where practical.
- Run the test suite before claiming behavior is fixed.
- Keep docs explicit about what ships today versus what is planned.

## Verification And Release Discipline

A green suite is evidence only for the cases it exercises. Before changing
code, identify the affected user workflow, security/privacy boundary, data
semantics, extension points, cross-layer consumers, failure modes, and public
claims. Do not infer a contract from a comment, docstring, or test name.

Every non-trivial change must include the applicable cases below:

1. The real user entry point and data flow, not a proxy or reimplementation.
2. Errors, malformed and partial data, nulls, empty values, retries,
   cancellation, re-initialization, and shutdown.
3. A non-default or unknown provider, model, dimension, adapter, backend, or
   enum-like value when the code claims extensibility.
4. Isolation across tenants, sessions, evaluators, rubric versions, models, and
   concurrent users where applicable.
5. Cross-layer parity among capture, storage, evaluation, drift analysis, API,
   and UI for identity, filters, windows, denominators, and unknown states.
6. Realistic nested provider structures, including tool calls, tool results,
   partial streams, and metadata at trust boundaries.
7. Browser interaction with real API data at desktop and mobile sizes for UI
   changes; seed data or static rendering alone is insufficient.

Add a counterexample that fails before the fix and passes afterward. Do not mock
the logic under test. For critical behavior, deliberately mutate the behavior
or use mutation testing to prove the test can detect the defect. Search the
entire repository for duplicate instances of the failed assumption.

### Mandatory Defect-Class Closure Protocol

Before the first product-code edit in every session, apply the workspace
`ENGINEERING_STANDARDS.md` defect-class protocol and write one deduplicated
finding/contract matrix. A reviewer example is a seed for adjacent cases, not
the complete test boundary.

For Verdict, the matrix must include these cases whenever the subsystem changes:

- **Redaction:** alternate valid representations and token boundaries (including
  mapped/scoped IP forms), key-collision/cardinality behavior, insertion-order
  determinism, cycles/shared graphs, and serialized/storage size budgets. Run
  canaries through the final storage, API/export, and browser-visible boundary.
- **Trace/storage linkage:** explicit context, inherited context, provider
  adoption, nested spans, concurrent tasks/tenants, sampling, synchronous and
  delayed buffered failure, backpressure, late stream completion, and shutdown.
  An enqueued write is not a durable write and may not authorize a persisted
  foreign link. Automatic provider correlation is one-way through
  `Trace.parent_span_id`; `SpanRecord.trace_id` is reserved for validated
  explicit context. Do not reintroduce an automatic reverse span link without a
  new schema and deletion/retention ownership design for the one-to-many case.
  Trace deletion and retention pruning must be atomic across every affected
  record/table, serialized against concurrent trace writers, adapter-parity
  tested, and linear rather than a spans-times-traces scan.
- **Evaluation:** permute duplicate/conflicting/unusable signals; cover empty,
  missing, error, outage, UNCLEAR, and zero-denominator states; assert one
  contract across persisted artifacts, scripts, API, and UI. Malformed stored
  booleans must fail closed without disappearing from one aggregate denominator.
- **Compatibility:** compare every affected public dataclass field order and
  callable signature with the latest published wheel, then exercise old
  positional construction and stored fixtures against the built candidate.
- **Runtime evidence:** source-code and generated-text substring assertions do
  not prove behavior. Storage claims require adapter execution; Postgres changes
  require a live disposable Postgres round trip before merge or an explicit
  blocked release claim.

Keep remediation slices reviewable. Do not add adjacent features or broad
cleanup while closing a reviewer finding. Any change after an approval or
readiness report resets that report for the affected candidate.

### Complexity Stop — Simplify Before Patching Again

The workspace `ENGINEERING_STANDARDS.md` Mandatory Complexity Stop And Redesign
Gate is binding here. A repeated defect in one stateful subsystem is evidence
that the design must be reconsidered, not permission to add another state or
callback.

Stop product-code edits and obtain an independent design review before the next
fix if a correction would add a state/enum value, boolean, callback, deferred
repair, registry, retry branch, or adapter-specific reconciliation path; if a
prior fix caused or exposed another defect in the same subsystem; or if
correctness depends on a later callback finding objects it does not own.

Before implementation, the design review must:

1. define the invariants and terminal outcome for every created record;
2. cover objects created before, during, and after asynchronous persistence,
   plus success, failure, sampling, cancellation, retry, timeout, and shutdown;
3. compare the proposed patch with a design that removes or collapses existing
   machinery, counting states, transitions, callbacks, writes, adapter branches,
   and files added and removed;
4. choose the design that makes loss, duplication, dangling links, and permanent
   pending states structurally impossible;
5. add deterministic and order-randomized real-path tests before changing the
   implementation; and
6. remove superseded mechanisms instead of leaving old and new paths running in
   parallel.

For trace/span persistence specifically, every ended span must be durably
accounted for exactly once regardless of buffer timing. Automatic correlation
is one-way: the provider trace may record `parent_span_id`, while a manual span
receives `trace_id` only from validated explicit context. Tests must create and
close descendants before, during, and after provider-trace persistence and run
the same lifecycle contract against memory, SQLite, buffered storage, and live
Postgres. Do not add an acknowledgement mechanism, reverse-link repair, or
another trace-link state without completing and approving the redesign gate.

### Verdict-Specific Regression Matrix

The following cases are mandatory whenever their area changes:

- Redaction canaries inside plain text, nested tool arguments, nested tool
  results, metadata, errors, exports, storage records, and API/UI payloads.
- Multiple judge models and rubric versions in one store remain isolated.
- PASS/FAIL/UNCLEAR/missing/error denominators agree between the detector, API,
  dashboard summaries, and charts.
- Custom dimensions and unknown provider/model names render without crashing or
  silently disappearing.
- Instrumentor changes run against the current real provider SDK through the
  cold initialization and streaming/error paths. Without credentials, mark the
  paid-call portion explicitly unverified.
- Stored records from supported earlier schemas still load or fail with a clear
  migration error.
- Judge-health gates count exact-match sentinel examples, never correlated
  labels. Keep label agreement as a separately named diagnostic and test
  unequal label counts per example.
- Probe quality gates count each probe weight once; expectation agreement is a
  separately named diagnostic. Test malformed, contradictory, and historical
  artifacts fail closed without changing published constructor positions.
- Drift consumers read one atomic latest `DriftRun` snapshot, including an
  explicit zero-signal run. Test rollback, concurrent replacement, legacy
  signals without run identity, and deletion without partial snapshots.
- Dashboard request tests must execute reordered and failed fetches plus stale
  trace/drift selection. JSX substring checks are not evidence for state
  behavior.
- Buffered storage lifecycle tests must cover accepted writes at shutdown,
  concurrent close/read/write/flush, post-close behavior, and a real adapter.

Before merge or release, run focused regressions, the complete Python and UI
test suites, the applicable smoke/end-to-end paths, `git diff --check`, a review
of every file being committed, and a repository-wide stale-documentation
search. The completion report must list exact commands, real paths exercised,
adversarial cases, untested areas, documentation changes, and residual risks.
Do not call Verdict or a feature production-ready from unit tests alone.

## Documentation Definition Of Done

Documentation updates are required in the same change whenever code modifies
public behavior, defaults, configuration, schemas, installation, CLI or UI
workflows, architecture, security/privacy boundaries, limitations, or validation
claims. Do not defer them to a later cleanup pass.

Before declaring a coding task complete:

1. Identify the affected documentation before or while implementing the change.
2. Search the entire repository for stale descriptions of the old behavior,
   including package READMEs, onboarding and architecture docs, ADRs, examples,
   command help/docstrings, screenshots, and diagrams.
3. Update every affected document and validate links, commands, generated assets,
   and diagrams where applicable.
4. In the final report, list the documentation files changed. If none changed,
   state explicitly why the code change has no documentation impact.

The task is not complete while any known code/documentation mismatch remains.

## Useful Commands

```bash
python scripts/smoke_test.py
python -m pytest
python scripts/live_capture_check.py
pnpm --dir ui install --frozen-lockfile
pnpm --dir ui build
python -m pytest -q ui/tests
```
