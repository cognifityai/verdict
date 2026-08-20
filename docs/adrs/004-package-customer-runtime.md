# ADR-004: Package the customer runtime

**Status:** Accepted
**Date:** 2026-08-20
**Decider:** Cognifity AI

## Context

The published SDK captures to SQLite or PostgreSQL, but the dashboard and
analysis runners are available only from a source checkout. The dashboard also
queries SQLite directly. That prevents an installed application from mounting
the UI supplied by its pinned Verdict version and makes PostgreSQL traces
invisible in the bundled dashboard.

## Contract and finding ledger

| Finding | Governing contract | Last affected sink | Decision |
| --- | --- | --- | --- |
| The wheel does not contain the dashboard | Normal product use must not require a Git clone | Installed browser UI | Fix in this change |
| Dashboard queries are SQLite-specific | Advertised storage backends must produce the same dashboard DTO | `/api/data` and browser | Fix in this change |
| The browser fetches `/api/data` from the origin root | An installed dashboard must work when mounted below an application path | Browser fetch | Fix in this change |
| A failed live fetch initially displays synthetic values | Live and synthetic evidence must never be confused | Browser charts | Fix in this change |
| Repository scripts assume a checkout | Customer operations must be versioned install surfaces | Console commands | Package in a separate reviewable change |

The compatibility contract is additive: existing SDK imports, `verdict.init`,
SQLite files, PostgreSQL tables, and `python ui/server.py --db ...` continue to
work. Dashboard reads never migrate or mutate the selected store.

Adjacent cases are empty and missing stores, malformed or partial historical
rows, unknown providers/models/dimensions, multiple evaluator identities,
explicit zero-signal runs, database errors, nested redaction canaries, mounted
paths, and unauthorized requests. SQLite and PostgreSQL must return equivalent
DTOs for the same records.

## Decision

Ship one canonical dashboard implementation inside `cognifity-verdict`:

- a small public application factory accepting a storage URL;
- read-only SQLite and PostgreSQL query sessions feeding one aggregation path;
- only the compiled dashboard HTML, JavaScript, and CSS required at runtime;
- optional dashboard/server dependencies; and
- a thin repository wrapper for the historical source-checkout command.

The explicit sample experience remains available in the development UI, but
the live dashboard starts empty and reports load failure instead of substituting
sample metrics.

Customer analysis commands belong to the package that owns their domain logic:
capture diagnostics in `cognifity-verdict`, evaluation and drift commands in
`cognifity-verdict-eval`, and export inspection in `cognifity-verdict-inspect`.
Research, release, and contributor tools remain repository-only.

## Alternatives considered

### Copy the dashboard into each host application

Rejected. It creates divergent UI and query implementations and requires host
code changes for every Verdict UI release.

### Run a separate dashboard service

Rejected for the current product. It adds a deployment, authentication path,
and version boundary without providing needed isolation for the local and
single-service use cases.

### Publish a fourth dashboard distribution

Deferred. It avoids a small static-asset addition to the core wheel but adds a
package and coordinated release surface. The optional dependency group keeps
capture-only installations lightweight without that operational cost.

## Consequences

- Installed applications can mount the dashboard supplied by their pinned
  Verdict version.
- PostgreSQL becomes a real dashboard backend without changing stored schemas.
- The wheel grows by the compressed dashboard runtime assets; server and
  PostgreSQL dependencies remain opt-in.
- A dashboard release now requires SQLite/PostgreSQL parity, cold-wheel, and
  mounted-browser verification.
