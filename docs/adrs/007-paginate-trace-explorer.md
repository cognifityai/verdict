# ADR-007 — Paginate the Trace Explorer

**Status:** Accepted for implementation
**Date:** 2026-08-27
**Owner approval:** The task owner explicitly approved a review-only pull
request on 2026-08-27 and prohibited merge before review.

## Brief and non-negotiable scope

**Intent.** Let an operator browse and search every application trace stored in
Verdict instead of seeing only the newest 30 records.

**Context.** The packaged dashboard currently returns aggregates, charts, and
30 trace presentations in one bounded `/api/data` response. That bound protects
response size and redaction work, but the Trace Explorer incorrectly uses the
bounded presentation sample as its complete dataset. Telemetry import makes the
limitation immediately visible because a single import can add many legitimate
application traces.

**Constraints.** Preserve the bounded aggregate bundle and its published
response shape. Keep SQLite and PostgreSQL read behavior equivalent. Keep the
dashboard read-only and mountable below a host path. Do not alter storage
schemas, trace ownership, clustering, sampling, evaluator identity, judgment
semantics, drift statistics, or database-write paths. Keep list responses small
even when stored prompts and responses are large.

**Acceptance criteria.** The live Trace Explorer must query all eligible
application traces through bounded pages, search and filter at the database,
open one bounded redacted detail record, and preserve the currently selected
evaluator's judgment and active-registry cluster projection. Ordering must be
deterministic for equal timestamps and stable when newer traces arrive between
page requests. The real installed HTTP and browser paths must work at desktop
and mobile widths. The pull request remains unmerged until owner review.

**Must not change.** `/api/data` aggregate/statistical semantics; the legacy
`samples` and `truncation.resources.traceSamples` fields; trace persistence;
provider capture; imported trace behavior; cluster assignment; evaluator
selection; score denominators; drift calculations; package versions; or
existing public Python signatures.

## Decision

Keep `/api/data` as a bounded analytics snapshot and add a separate trace read
model:

```text
GET /api/data
  -> bounded aggregates and legacy 30-row compatibility sample

GET /api/traces?limit=50&cursor=...&q=...&provider=...&capture=...
  -> one bounded page of application-trace previews

GET /api/traces/{trace_id}?evaluator=...
  -> one bounded redacted trace detail
```

The list endpoint defaults to 50 rows and accepts 1–100 at the API boundary;
the UI offers 25, 50, and 100. It orders by `started_at DESC, trace_id DESC`
and requests one extra row to determine whether a next cursor exists. Cursors
are opaque, versioned, length-bounded, and bound to the active filter set. They
contain only the last ordering key and a filter fingerprint. A cursor from a
different search/filter set is rejected instead of silently skipping data.

The UI stores already-issued cursors to support Previous without inventing an
offset. Newer concurrent inserts sort ahead of the first page and therefore do
not duplicate or skip rows while an operator walks older pages. Deletion can
reduce a later page, which is an honest view of the current read-only store.

List rows contain bounded prompt previews and metadata only. Full redacted
prompt/response content and selected-evaluator reasoning are fetched only when
the operator opens one trace. Detail content remains bounded, and the response
states whether a field was truncated. The final response is recursively
redacted even though Verdict storage already applies redaction on writes; this
preserves the existing dashboard boundary for historical databases.

Judgment lookup is also bounded: one page considers at most 500 newest retained
identity rows, then fetches dimension payloads only for the selected latest rows.
If that safety bound is reached, trace browsing remains available and the API/UI
marks some row-level judgment status unavailable rather than hiding the page or
performing an unbounded history scan. This does not alter stored judgments,
aggregate evaluation, or drift results.

The initial query surface deliberately matches the existing explorer workflow:

- search across trace ID, prompt, response, provider, model, cluster, and
  serialized provenance tags;
- exact provider filtering; and
- content-captured versus metadata-only filtering.

Search terms and provider values are length-bounded. SQL wildcard characters
are escaped and all values are bound parameters. Additional date, numeric,
source, cluster, and verdict controls can be added behind this read-model
interface in separate reviewable changes; they are not required to remove the
30-row product defect.

Judge telemetry remains excluded from the explorer using the same
`tags["verdict.workload"] == "judge"` contract as the legacy sample. It remains
included in existing full-store cost/provider totals. The selected evaluator
continues to use the full canonical identity rather than model name or
fingerprint alone. The latest `(created_at, judgment_id)` row wins per trace,
matching the aggregate dashboard.

## Designs considered

### A. Raise `MAX_TRACE_SAMPLES` (rejected)

Increasing 30 to 1,000 delays the failure while making every dashboard refresh
transfer and redact up to 1,000 prompts, responses, and judgment payloads. It
does not provide real pagination or store-wide filters.

### B. Offset pagination inside `/api/data` (rejected)

Offsets duplicate or skip rows when new traces arrive and turn the aggregate
snapshot into a query-specific response that every chart refresh must reload.
Large offsets also become progressively more expensive.

### C. Separate keyset-paginated list and detail reads (selected)

This preserves the existing analytics contract, bounds every response, keeps
one canonical read-only database path, and lets the UI replace the query
implementation without changing storage or statistical consumers.

## Mandatory defect-class closure ledger

| Finding / adjacent cases | Governing contract | Last affected sink | Status |
|---|---|---|---|
| The 31st and later application traces are stored but cannot be inspected | every eligible stored application trace is reachable through pages | browser trace table/detail | accepted; fix in this change |
| Empty, one-row, exact-page, page-plus-one, and final-short-page datasets | shown/total/next-cursor remain exact | API page controls | accepted |
| Many traces can share one timestamp | `trace_id` is the deterministic secondary order | API and browser order | accepted |
| New traces can arrive between page requests | keyset cursor walks older keys without duplicates | browser page sequence | accepted |
| A cursor can be malformed, oversized, version-unknown, or reused with other filters | fail with bounded 400; never guess or expose internals | HTTP response/log | accepted |
| Search can contain `%`, `_`, backslash, Unicode, NUL, or excessive text | bound length; escape wildcards; reject invalid control input; bind parameters | SQL query and HTTP response | accepted |
| Provider/model/cluster/tag values can be unknown, null, empty, or malformed historical JSON | render unknown values; malformed tags remain unclassified application traces | list row/filter | accepted |
| Content can be absent, empty-but-captured, partial, oversized, or contain nested secret/PII canaries | preserve capture state; preview/detail caps; recursive final redaction | API and browser-visible detail | accepted |
| Judge traces can be newer than application traces | exclude workload `judge` from count, pages, search, and detail | trace explorer | accepted |
| Multiple evaluator identities or retries can exist for one trace | canonical full identity; latest `(created_at, judgment_id)` wins | status badge and detail reasoning | accepted |
| Active registry assignments can differ from legacy `traces.cluster_id` | use the host-authorized active projection for list and detail | cluster label | accepted |
| SQLite and PostgreSQL encode JSON/timestamps differently | one read-model contract and parity fixtures through both adapters | HTTP/browser DTO | accepted |
| A page/detail fetch can fail, finish out of order, or complete after selection changes | abort obsolete work and reject stale responses; preserve last confirmed page | browser state | accepted |
| Dashboard can be mounted below another application and protected by Basic Auth | derive same-origin URLs; gate both new endpoints | installed browser/HTTP | accepted |
| `/api/data` is a published alpha response consumed by existing hosts | retain legacy sample/truncation fields unchanged | existing dashboard consumers | accepted |

## Failure and privacy behavior

- Invalid filters and cursors return a generic bounded `400` response.
- A missing or ineligible trace detail returns `404` without exposing another
  row or storage path.
- Missing/corrupt/locked storage returns the same generic `503` posture as the
  aggregate dashboard; credential-bearing PostgreSQL DSNs and filesystem paths
  remain server-side only.
- Both endpoints are protected by the dashboard's existing Basic Auth gate and
  same-origin CORS default.
- Responses carry the existing no-store and browser security headers.
- Queries are read-only. SQLite uses one read transaction and PostgreSQL uses a
  read-only repeatable-read transaction for a coherent count/page response.

## Verification plan and evidence labels

- **Deterministic unit/contract:** cursor validation and filter binding;
  boundary sizes; equal timestamps; wildcard escaping; evaluator identity;
  redaction and truncation.
- **Real local integration:** FastAPI endpoints over real SQLite storage,
  mounted paths, authentication, page/detail failures, and compatibility with
  `/api/data`.
- **Live adapter parity:** the same page/detail contract over disposable live
  PostgreSQL in CI/local when available.
- **Real browser:** installed dashboard with more than one page of real API
  data; search, provider/content filters, next/previous, row detail, failed and
  reordered requests; desktop and mobile viewport checks with no console error
  or horizontal page overflow.
- **Mutation evidence:** removing the secondary order, cursor filter binding,
  judge-trace predicate, or stale-response guard must make focused tests fail.
- **Statistics:** unchanged by contract; existing full-suite detector and
  dashboard aggregate tests remain regression evidence only for their executed
  paths.
