# ADR-006 — Import Existing Telemetry into Verdict Traces

**Status:** Accepted
**Date:** 2026-08-26

## Context

Many teams already collect LLM telemetry in OTLP or an observability product.
Requiring those teams to add Verdict-specific instrumentation before using
Verdict creates duplicate work.

Verdict already has a vendor-neutral `Trace` model, SQLite/PostgreSQL storage,
clustering, judge sampling, drift detection, and a trace explorer. Those
downstream paths need one stable dataset containing the LLM calls Verdict
actually analyzes.

## Decision

Verdict provides source adapters and the `verdict-import` CLI inside
`cognifity-verdict`. Adapters normalize supported LLM-generation records into
the existing `Trace` model and write them through the existing storage port.

Import is one-way and synchronous. It stores eligible normalized LLM-call
records, not complete vendor envelopes or unrelated telemetry. Deterministic,
source-scoped trace IDs make bounded retries idempotent. Missing optional fields
remain missing rather than being inferred.

The existing pipeline remains the only path for clustering, sampling, judging,
and drift calculations. Import does not create another schema, statistical
implementation, or dashboard data source. Supported formats, limits, field
mapping, privacy behavior, and commands are maintained in
[`examples/telemetry/README.md`](../../examples/telemetry/README.md) and the
[`verdict-import` documentation](../../README.md#import-telemetry-you-already-have).

## Consequences

- Teams can point Verdict at telemetry they already own without changing their
  application instrumentation.
- Imported evidence remains inspectable if the upstream system later expires
  it or is temporarily unavailable.
- Verdict stores a normalized copy of every eligible imported LLM call; the
  pipeline subsequently decides which stored traces to judge.
- Verdict does not preserve full vendor trace graphs or source-specific fields
  outside its allowlisted `Trace` contract.
- Source credentials, pagination, and schema differences stay at the adapter
  boundary instead of leaking into clustering, statistics, or the dashboard.
- New formats require an adapter into the same contract, not changes to every
  downstream consumer.

## Alternatives considered

- **Store a second canonical telemetry graph:** rejected because it would
  duplicate the customer's observability system and add schemas, migrations,
  reconciliation, queues, and checkpoint state without improving Verdict's
  statistical analysis.
- **Read vendors live during every analysis:** rejected because credentials,
  availability, pagination, and moving datasets would affect every consumer
  and could make the dashboard and a drift run analyze different snapshots.
