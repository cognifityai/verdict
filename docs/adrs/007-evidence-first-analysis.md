# ADR-007: Evidence-first analysis architecture

**Status:** Current

## Context

Verdict stores genuine LLM request/response traces and typed observations from
agent executions. Analysis, monitoring, and evaluation need one evidence model
with explicit identity, provenance, time, and completeness. Semantic grouping
and model-based evaluation are optional capabilities rather than prerequisites
for ingesting or inspecting evidence.

## Architecture contract

1. A Trace represents one genuine LLM request/response. A run, turn, or session
   is never relabelled as a Trace.
2. Source files are read-only. Import is deterministic and idempotent.
3. Event time and processing time are separate. Each analysis freezes its input
   cutoff and membership.
4. Stored evidence is typed, bounded, recursively redacted, and records its
   provenance and any omission reason.
5. Analysis results are terminal and immutable. A completed analysis with no
   findings is stored explicitly.
6. PASS, FAIL, UNCLEAR, NOT_EVALUABLE, NOT_APPLICABLE, and ERROR have consistent
   denominator semantics across storage, API, and UI.
7. Evaluator evidence requirements are checked before a model call.
8. Only an active immutable monitoring policy can produce alerts.
9. One active reference owns each monitored comparison. A stale reference
   suspends the affected claim instead of silently rebasing it.
10. Candidate activation and rollback select immutable versions; historical
    results do not change meaning.

## Components

Verdict ships as one installable package with separable runtime roles and four
internal capabilities:

1. **Evidence intake** normalizes supported sources into Trace records and
   atomic AgentRun bundles.
2. **Finding analysis** derives deterministic evidence, reliability,
   performance, structural, and outcome findings without model calls.
3. **Policy execution** freezes analysis unit, reference/current membership,
   metrics, grouping, late-data rules, and schedule into immutable versions.
4. **Evaluator Lab** manages evidence requirements, labels, evaluator identity,
   grouped validation, and human-controlled activation.

The packaged CLI can run the loopback dashboard. Instrumented application
processes, the dashboard, and the scheduled worker may run separately against a
shared PostgreSQL store. SQLite remains the local default.

## Storage and monitoring lifecycle

- Evidence intake owns source snapshots and AgentRun-bundle writes.
- An analysis computes before atomically publishing one immutable `completed`
  or `error` record. A crash before publication leaves no authoritative result.
- The policy registry owns immutable candidate and active versions plus the
  active pointer.
- A scheduled worker performs idempotent rescans and prospective-monitor runs
  from durable policy state.
- Historical previews remain exploratory. Activation freezes their reference
  membership and starts an empty prospective current cohort.
- Repeated looks use bounded alpha spending; Benjamini-Hochberg correction
  remains within each look.
- Late-arriving units enter the next open cohort rather than being discarded.
- Snapshot payloads are bounded before storage.
- Manual source approval and saved scheduled-source configuration are separate
  explicit actions.

## Consequences

Deterministic findings, evidence coverage, and operational analysis are
available without enabling semantic clustering or an evaluator. Semantic
quality results require sufficient captured evidence and an explicitly
configured evaluator. Optional grouping remains versioned and reviewable.
