# ADR-005: Extend the dashboard through an optional operations API

**Status:** Accepted
**Date:** 2026-08-20
**Decider:** Cognifity AI

## Context

The packaged dashboard shows Verdict traces, judgments, and drift, but it
cannot show the host application's infrastructure, Verdict's runtime impact,
or host-owned analysis jobs. Cognifity Lab needs those views in the same UI for
dogfood and demos. Other customers may use a different observability system or
no external system at all.

## Contract and finding ledger

| Finding | Governing contract | Last affected sink | Decision |
| --- | --- | --- | --- |
| Infrastructure and job data are absent | A configured host can add operations evidence without copying the dashboard | Browser Operations view | Add an optional same-origin JSON API URL |
| GCP metrics are host-specific | The core package stays vendor-neutral and capture-only installs gain no cloud dependency | Published wheel dependency graph | Keep every GCP query in the host |
| Verdict overhead and adapter failures are not measurable | Measurements must come from the real capture boundary and must not raise into provider calls | Runtime snapshot and Operations charts | Add bounded, process-local runtime counters |
| Buffered queue state is invisible | Disabled, queued, fallback, and failed writes must be distinguishable | Runtime snapshot and browser | Expose a read-only buffer snapshot; never imply disabled means healthy zero |
| Application and judge costs are pooled | Cost classes require explicit provenance and unknown costs remain unknown | Dashboard cost comparison | Stamp an opt-in workload tag; leave historical rows unclassified |
| Job buttons can create a write surface | Standalone Verdict remains read-only and hosts own authorization, CSRF, execution, and persistence | Operations POST endpoint | Render actions from the host contract; do not add mutation routes to Verdict |

Compatibility is additive. Existing imports, `verdict.init`, dataclass
constructors, SQLite/PostgreSQL schemas, stored rows, dashboard routes, and
`create_app(storage=...)` calls retain their behavior. The new configuration is
an optional keyword-only argument. A dashboard without an operations URL makes
no external request and shows no Operations tab.

Adjacent cases are an absent source, partial metrics, empty series, null and
non-finite values, reordered responses, unknown metric groups/units/workload
tags, a disabled buffer, a full buffer, persistence failure, unavailable job
prerequisites, failed jobs, mounted paths, unauthorized host routes, stale
responses, and desktop/mobile rendering. Cost summaries preserve priced,
unpriced, and unclassified counts rather than converting missing data to zero.

## Decision

The dashboard server exposes a small read-only configuration response containing
an optional same-origin operations URL. The browser fetches that host endpoint
only when configured. The normalized response contains metric panels, runtime
health, cost provenance, links, recent sanitized job lifecycle records, and
the host's available actions.

Verdict owns only two generic producers:

- bounded runtime capture measurements attached to the active `VerdictClient`;
- an explicit workload context stamped into the existing trace tags.

The host owns collection, aggregation windows, authorization, CSRF, job
execution, durable job state, and external links. Runtime measurements are
explicitly process-local. Hosts that need fleet-wide values export or aggregate
them through their own observability system.

### Runtime lifecycle

| Event | Owner | Outcome |
| --- | --- | --- |
| Supported provider call reaches Verdict persistence | Instrumentor | Record one bounded overhead sample and success/failure counter |
| Synchronous persistence fails | Instrumentor | Preserve the provider result/exception, increment the adapter-failure counter, and emit the existing bounded warning |
| Buffered write is accepted | Buffered storage | Queue depth and accepted-write counters change; durability is not claimed |
| Buffered fallback or worker failure | Buffered storage | Existing fallback/error counters remain authoritative and appear in the snapshot |
| Client shutdown | Storage owner | Existing flush/close contract remains unchanged; metrics add no worker or persistence path |

The metrics object owns no asynchronous work and no durable state, so
cancellation, retry, timeout, and shutdown cannot strand a metric record or
affect trace persistence.

## Alternatives considered

### Put Cloud Monitoring and job execution into the dashboard server

Rejected. It adds cloud dependencies and privileged mutations to every hosted
dashboard, makes standalone security harder, and couples Verdict releases to a
single deployment platform.

### Build a separate Lab observability dashboard

Rejected. It duplicates navigation, chart behavior, and release ownership. Lab
would stop testing the UI customers receive.

### Persist a second operational telemetry schema in Verdict

Rejected for this release. It adds migrations and another retention lifecycle
when the immediate need is a bounded current-runtime view. Fleet history remains
the observability provider's responsibility.

## Consequences

- Lab can render product, runtime, infrastructure, cost, and job evidence in
  one versioned dashboard.
- Customers can omit the integration or provide another same-origin adapter.
- Existing installations receive no new cloud dependency or write endpoint.
- Runtime overhead and queue measurements are process-local unless a host
  exports them.
- Historical costs remain visibly unclassified; the feature does not rewrite
  old traces or invent provenance.
