# ADR-015: Bounded fleet read and authenticated Operations links

**Status:** Accepted
**Date:** 2026-09-14
**Decider:** Cognifity AI

## Context

Verdict's accepted Agent Run ReadPort V1 intentionally reads one selected run.
An optional same-process product now needs bounded metadata/numeric trace windows
for fleet history without importing `Storage`, querying Verdict tables, copying
content, or adding work to provider capture. The same optional product needs its
mounted same-origin full-page path authenticated and visible in primary
navigation, plus exact links back to Verdict's own authorized trace and Agent
Run detail. The existing `operations_url` is a legacy JSON-adapter URL consumed
inside Settings and remains unchanged. The new product does not need a plugin
framework, callback, write port, HTTP fleet service, or SQLite fleet scan.

The default Verdict package must remain independent of every optional consumer.
Existing capture DTOs, provider paths, storage writes, SQLite behavior,
dashboard routes, and the accepted selected-run ReadPort cannot change.

## Defect-class closure matrix

| Failure class | Governing invariant | Last affected sink |
|---|---|---|
| Fleet consumer couples to storage/content | Only closed metadata DTOs cross two allowlisted views | Built wheel, live PostgreSQL row, JSON |
| Dense, slow, malformed, or oversized window returns plausible partial data | Fixed interval/item/read/byte bounds yield complete success or one stable error | Public call and consumer gap |
| Tenant or agent reverse link crosses scope | Query and join byte-match the authorized tenant; no unmatched row is returned | Live role/query and DTO |
| Optional consumer changes capture/default installs | Separate lazy PostgreSQL module; views/index are additive; no capture callback | Python 3.10 base wheel, provider calls, old fixtures |
| Operations route is public or prefix-confused | Exact configured path-or-descendant auth predicate | Real mounted ASGI route |
| Deep link reflects arbitrary URLs or exposes content | Server constructs only fixed relative selection forms; Verdict authorizes detail | Browser address, existing detail API |
| Close/deadline leaves work or a late result | One read-only transaction/pool, no queue/retry/write, terminal deadline recheck | Caller and database session |
| A later change silently expands V1 | Field/signature/version fixtures require a new V2 | Published public API |

## Decision

Add one PostgreSQL-only public Fleet ReadPort V1, one fixed optional
`/operations` full-page navigation/path hook, and two exact dashboard selection
links. These are independent additive changes. Verdict core imports no private
package and performs no background ingestion.

### Public contract

The new `verdict.fleet_read_port` module exports:

```python
VERDICT_FLEET_READ_SCHEMA_VERSION = "verdict.trace-window-read.v1"

@dataclass(frozen=True)
class TraceContributionReadV1:
    tenant_id: str
    trace_id: str
    started_at: datetime
    ended_at: datetime | None
    request_status: str
    provider: str | None
    request_model: str | None
    response_model: str | None
    service_name: str | None
    environment: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_us: int | None
    cost_micro_usd: int | None
    parent_span_id: str | None
    agent_link_state: str
    agent_event_id: str | None
    agent_turn_id: str | None
    agent_run_id: str | None

@dataclass(frozen=True)
class TraceWindowReadV1:
    schema_version: str
    tenant_id: str
    window_start: datetime
    window_end: datetime
    read_started_at: datetime
    read_completed_at: datetime
    item_count: int
    items: tuple[TraceContributionReadV1, ...]

class VerdictFleetReadPortV1(Protocol):
    def read_trace_window(
        self, *, tenant_id: str, window_start: datetime, window_end: datetime
    ) -> TraceWindowReadV1: ...

class PostgresVerdictFleetReadPortV1:
    def __init__(self, database_url: str, *, tenant_id: str) -> None: ...
    def read_trace_window(...) -> TraceWindowReadV1: ...
    def close(self) -> None: ...

def trace_window_read_to_json(value: TraceWindowReadV1) -> str: ...

class VerdictFleetReadError(RuntimeError):
    code: str
```

V1 classes and signatures never grow fields or operations. A future shape gets
new V2 types and protocol. The existing `verdict.read_port` exports and analysis
version remain unchanged.

### Exact values and invariants

- The half-open UTC query is `[window_start,window_end)`, is nonempty, and is at
  most 900 seconds. The tenant is explicit non-local ASCII matching the existing
  Verdict pattern `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`.
- Trace/agent/span IDs are 1..256 UTF-8 bytes. Provider/service/environment are
  null or 1..128 bytes; model IDs are null or 1..256 bytes. Empty optional source
  strings normalize to null. NUL and invalid Unicode are rejected.
- Counts are exact non-Boolean integers in `0..2^63-1`. Latency is an integer in
  `0..86_400_000_000` microseconds. Cost is an integer in `0..10^15` micro-USD.
- `started_at` lies in the requested window. `ended_at` is null exactly for
  `in_progress`, otherwise it is UTC in `[started_at,started_at+24h]` and status
  is `succeeded` or `failed`. Failed/succeeded uses only stored error presence;
  error text never crosses the view or port.
- Source `latency_ms` becomes microseconds with
  `Decimal(str(value))*1000`, integer round-half-even. Finite nonnegative
  `cost_usd` becomes micro-USD with `Decimal(str(value))*1_000_000`, integer
  round-half-even. A database null remains null. Every non-null view scalar is
  converted and validated exactly; a malformed, non-finite, negative, or
  overflowing view row fails the complete read with `invalid_read_model` rather
  than becoming null or being clamped.
- `agent_link_state` is `exact` or `not_found`. Exact requires a link count of
  one and all event/turn/run IDs from that same-tenant
  `agent_events.trace_id` relationship; not-found clears them. Multiple links
  fail the complete read. No guessed reverse link is created.
- Items are exact DTO instances, unique and strictly ordered by
  `(started_at,trace_id)`. `item_count == len(items) <= 2_000`. The result tenant
  and every row tenant byte-match the authorized query.
- Constructors revalidate nested exact types before serialization. JSON uses the
  declared snake-case order, UTF-8, compact separators, RFC 3339 `Z`, no NaN,
  and no omitted fields. Exactly 2,097,152 encoded bytes is accepted; the next
  byte fails.

`VerdictFleetReadError` accepts only `invalid_query`, `unsupported_backend`,
`unsupported_version`, `read_unavailable`, `invalid_read_model`,
`window_too_dense`, and `response_limit_exceeded`. It has only `.code`, exact
`str/args`, and no chained storage/SQL/credential text. Invalid input fails
before pool access. SQLite and unsupported schemas return
`unsupported_backend`/`unsupported_version` without scanning.

### PostgreSQL read boundary

Verdict owns two versioned views in its configured schema:

- `verdict_fleet_traces_v1` exposes tenant, trace identity/time, error-presence,
  bounded routing names, token/latency/cost scalars, and parent span only;
- `verdict_fleet_agent_links_v1` exposes one row per tenant/trace with bounded
  `link_count` plus event, turn, and run IDs only when that count is one. Count
  zero is not-found; count one is exact; count greater than one or inconsistent
  nullable IDs fails the complete read as `invalid_read_model`.

They expose no prompt, response, raw message, error text, finish reason,
temperature, max-token request, user/session identity, tag, arbitrary attribute,
judgment, cluster payload, or agent payload. These objects are not added by
ordinary `PostgresStorage` startup or a default Verdict upgrade. An explicit
operator command, `VERDICT_DATABASE_URL=<owner-dsn> python -m
verdict.fleet_read_port prepare --reader-role <role> --tenant-id <tenant>`,
installs the two
versioned views/grants and the
`(tenant_id,started_at,trace_id)` read index only when fleet mode is enabled; it
changes no capture column or write statement.

The prepare command accepts only those two options, reads the owner credential
only from `VERDICT_DATABASE_URL`, never prints or persists it, and requires a
precreated dedicated LOGIN reader. It uses identifier-safe SQL composition, a
fixed advisory lock, two-second lock waits,
and a five-minute aggregate deadline. It creates the scope table and both views,
but no target-reader mapping or grant, in one transaction when absent, then
creates the index with `CREATE INDEX
CONCURRENTLY`. A failed concurrent build is detected as invalid, removed with
`DROP INDEX CONCURRENTLY`, and retried only on a later explicit invocation. The
command records the exact view/index/scope-table checksum, target scope mapping,
and both target grants in one final transaction only after every object/readback
is valid. If the process stops after the valid concurrent index is committed
but before the final transaction commits, the next invocation accepts that
exact unledgered, target-absent state, repeats the complete bounded readback,
and records the ledger without rebuilding the index. The Verdict schema owner
owns all three objects; the command grants the reader role
`SELECT` only on the two views and no scope-table, base-table, or Operations-
schema right.

Before mutation, one bounded catalog read classifies shared objects and the
target reader independently. It reads at most nine scope rows and nine grantees
per view; V1 permits at most eight mapped readers and exactly two fleet-view
grants per mapped role. The only shared-object states are: scope and both views
absent with the exact index absent; exact scope/views with the index absent; the
same state with the exact-named index present but invalid after an interrupted
concurrent build; exact valid objects/index with the fleet ledger absent; or
exact valid checksummed complete objects. In any present state, zero through eight existing
scope rows must have unique reader roles and tenants, every role must satisfy
the reader restrictions below, and each must have both and only the two view
grants. The target reader is either absent from scope and both ACLs or has its
one byte-exact tenant mapping and both grants. Thus complete shared objects with
other valid readers and an absent target are an accepted add-reader state; one
transaction adds only that target mapping and grants after object/ledger
validation. Eight existing readers makes a ninth prepare fail without mutation.

Any unknown definition, owner, grant, duplicate object, role property,
membership, ledger row, partial target state, orphan ACL member, excess
cardinality, or differently named/defined index fails closed. Fresh state
creates only the table/views transactionally; the two index intermediate states
may create or drop-and-recreate only the named concurrent index; after a valid
index readback, the final transaction may atomically add only the exact ledger
row, target mapping, and grants. Exact target-enabled complete state is a
no-op. A busy lock fails without changing capture.

The opt-in migration also owns
`verdict_fleet_reader_scope_v1(reader_role,tenant_id)`, with at most eight rows
and one immutable
tenant mapping for each dedicated LOGIN reader and no reader privilege on the
table. Both `security_barrier` views join this mapping using `session_user`, so a
reader sees only its mapped tenant before the query predicate is applied.
Enablement rejects superuser/BYPASSRLS/CREATEROLE/CREATEDB readers, role
memberships in either direction, a reused role, duplicate tenant mappings, or a
role/tenant identity change. V1 rotates the credential of the same dedicated
role. Changing the role or tenant requires
`VERDICT_DATABASE_URL=<owner-dsn> python -m verdict.fleet_read_port disable
--reader-role <role> --tenant-id <tenant>` followed by `prepare`; it is not an
in-place mutation. `disable` takes only those two options, acquires the same
lock, and accepts every exact object/index/ledger state that `prepare` can leave
before its final transaction as well as complete prepared state. All-absent or
target-absent state (no mapping and neither grant) is an idempotent no-op and
does not finish preparation. Exact target-enabled state revokes that role's two
view grants and deletes only that scope row in one transaction. A partial grant,
different mapping, unknown object, unknown ledger, or busy lock fails closed. It
never creates, repairs, or drops the shared views, index, scope table, or ledger.
Fleet-prepared Verdict with no enabled private consumer and no mapped
reader is therefore an explicit safe partial-install state; removing or
disabling the private package does not leave a reader authorized. The port binds
`tenant_id` at construction and
rejects any call tenant that does not byte-match it before pool access.

`PostgresVerdictFleetReadPortV1` is the only default adapter. It lazily imports
the existing PostgreSQL extra and owns one connection pool with min/max size one.
Startup opens the pool within two seconds, validates PostgreSQL 16, exact view
columns/version and grants, a read-only role/search path, zero memberships,
lack of direct protected-relation rights, and no visible tenant other than the
tenant bound at construction. The security-barrier views enforce the
session-user mapping; every runtime query also requires that bound tenant
byte-for-byte before pool access. Runtime pool
acquisition is at most 250 ms. One repeatable-read,
read-only transaction sets a 1,500 ms local statement timeout and performs one
parameterized joined query ordered by `(started_at,trace_id)` with `LIMIT 2001`.
The aggregated same-tenant agent-link view prevents row multiplication while
still surfacing duplicate relationships as invalid. Row 2,001 is
`window_too_dense`; no truncated success is returned.

The call owns a fixed two-second monotonic deadline from before pool acquisition
through DTO construction and final byte measurement. A late result is discarded
as `read_unavailable`; the transaction is read-only and cannot perform a late
write. The adapter has no retry, background thread, queue, or page cursor. Close
stops admission, gives an admitted read at most the same two-second call
deadline, then closes the sole pool within that same two-second close bound. A
late read is discarded, a database session is never returned as success after
close, and a post-close call fails with `read_unavailable`.

### Full-page navigation, authentication, and deep links

`create_app` adds the keyword-only `operations_page_url`, whose only accepted
non-null value is the exact relative path `/operations`. When configured,
`/api/config` adds `operationsPageUrl:"/operations"` and the core browser renders
one top-level `Operations` navigation control that explicitly assigns the
current browser location to that path; it never requests a new browsing
context. When null, the config key is omitted, no control appears, and no new
path comparison occurs. Verdict's existing
`operations_url` argument, `operationsUrl` config field, and Settings JSON
adapter remain byte-for-byte unchanged.

Basic Auth gates exactly `/operations` and descendants using
`request_path == path or request_path.startswith(path + "/")`; prefix lookalikes
are not gated as Operations and cannot become mounted Operations routes. The
private composition root may mount routes/assets after app creation; no
discovery, entry-point scan, callback registry, or generic router contract is
added.

Verdict dashboard accepts only these selection query shapes:

```text
/dashboard?view=traces&trace_id=<percent-encoded exact ID>
/dashboard?view=agent-runs&run_id=<percent-encoded exact ID>
/dashboard?view=agent-runs&run_id=<percent-encoded exact ID>&event_id=<encoded ID>
```

Decoded IDs obey the 256-byte identifier bound; duplicate/unknown selection
parameters produce the existing safe dashboard shell with a bounded invalid
selection state. The browser selects only a row returned by Verdict's existing
authorized APIs. Missing/deleted rows show authorized not-found. Operations
never proxies Verdict content.

### Authorization, lifecycle, and compatibility

The port scopes by tenant but does not authenticate an end user. A trusted
composition root authorizes one tenant before construction, passes that exact
tenant plus its dedicated reader credential, and never exposes the port to a
browser. The adapter rejects call/result tenant mismatch. The core dashboard
remains the authentication owner for mounted same-origin routes.

The first release containing this contract is planned as
`cognifity-verdict==0.1.0a19`, supports Python 3.10+, and preserves the current
default SQLite install. Importing the DTO/protocol adds no PostgreSQL import;
constructing the adapter without the PostgreSQL extra fails with one stable
installation instruction. Existing positional DTOs, public functions, capture
latency, provider behavior, storage writes, routes without selection parameters,
legacy `operations_url`, and `operations_page_url=None` remain byte-for-byte/
public-behavior compatible. Ordinary Verdict upgrade/startup performs no Fleet
ReadPort DDL; enablement is the explicit operator command above.

## Options considered

### Extend the selected Agent Run ReadPort

Rejected. Fleet windows have different privacy, density, backend, deadline, and
version semantics. Growing V1 would break its fixed contract.

### Export Storage or dashboard queries

Rejected. Both expose private tables/content and couple consumers to migrations.

### Add a generic extension/plugin API

Rejected. The current same-origin URL plus private composition root meets the
verified requirement with fewer concepts.

### Add a fleet HTTP API to core

Rejected. Integrated consumers are trusted same-process components. A network
service would add authentication, tenant routing, rate limiting, and another
public attack surface without a current requirement.

## Consequences

- Optional products can reconcile bounded metadata windows without SQL coupling.
- Fleet mode is deliberately PostgreSQL-only and requires a dedicated reader.
- Dense one-second workloads become an explicit consumer gap after bounded
  interval splitting; core never returns partial success.
- Default users receive no Fleet ReadPort DDL, capture work, worker, route,
  dependency, or UI. Explicit fleet enablement adds only the reviewed views,
  concurrent read index, reader grant, and optional fixed page. Existing legacy
  Operations adapters remain unchanged.
- Exact request-to-gateway/deployment/GPU meaning remains outside Verdict.

### Defect-class closure: event selection survives URL canonicalization

The exact Agent-event query route was accepted and rendered during the initial
dashboard load, but the loading-to-live canonicalization path serialized only
the selected run. That second state transition removed `event_id`, so the last
browser/API sink could silently broaden the selection from one event to its
whole run.

The route contract therefore carries a bounded `event_id` only when the active
destination is `explore/runs` and the referenced run is the selected run. Query
selection, hash serialization, hash parsing, history replacement, React props,
and the final run-detail request must preserve that exact pair. Empty,
oversized, NUL-containing, duplicate, wrong-section, or run-less event values
remain absent or fail closed; trace and ordinary dashboard routes remain
unchanged. This is a browser-state compatibility fix only and changes no public
Python signature, storage row, authentication boundary, or content policy.

## Verification contract

- Public signature/field-order/version fixtures; exact constructors, subclass
  rejection/revalidation, JSON order/UTC, 2 MiB boundary, and every stable error.
- Empty/exact/2,000/2,001 windows, one-second density, malformed rows, UTC and
  duration/number overflow, duplicate IDs, and update/delete/stream completion.
- Error-presence status without error content; prompt/response/error/session/
  user/tag/agent-payload canaries absent from view rows, DTO repr, JSON, logs,
  and built wheel.
- Same trace ID across tenant probes and malicious cross-tenant agent links
  return no foreign data; a raw reader-role SQL probe sees only its mapped
  tenant; agent exact/not-found paths are storage-backed.
- Dedicated live PostgreSQL 16 roles prove the reader sees only exact views,
  cannot read protected tables or either schema's writes, and the adapter uses
  one read-only transaction with timeouts and no late result after close.
- Prepare-command tests cover first install, exact repeat, every accepted
  intermediate state, concurrent-index timeout/invalid cleanup/retry,
  exact valid-index/absent-ledger/target-absent crash recovery, ambiguous final
  commit, complete shared objects with zero/seven/eight readers and target
  absent, exact nine-row/ACL rejection,
  advisory-lock contention, signal, wrong owner/grant/checksum/reader property/
  membership/scope mapping, unknown partial state, and a live capture running
  during index creation. A raw SQL probe as the reader proves the scope table
  and both tenants' protected base tables remain unreadable while its own
  mapped view rows remain readable.
- Disable-command tests cover all-absent, every target-absent prepare
  intermediate, enabled, exact repeat, wrong tenant/role, partial grants,
  signal/lock contention, and disable followed by reprepare. A raw SQL
  probe proves the disabled role can read neither fleet view; shared prepared
  objects and another tenant's independently mapped reader remain unchanged.
- InMemory/SQLite/default PostgreSQL capture, published fixtures, provider
  smoke tests, selected Agent Run ReadPort, and dashboard tests remain unchanged;
  SQLite fleet construction fails without scanning.
- Auth/navigation tests cover `operations_page_url=None`, the exact configured
  path, descendants, trailing/path-prefix lookalikes, assets, unauthorized
  requests, one real mounted ASGI route, conditional `operationsPageUrl` config
  and same-tab top-level navigation, and unchanged legacy `operations_url`
  Settings behavior.
- Browser tests cover exact trace/run/event selection across the
  loading-to-live canonicalization transition, duplicate/oversized/malformed
  values, wrong-section and run-less event state, not-found, stale/reordered
  fetches, desktop/mobile, and existing no-query navigation.
- A clean Python 3.10 wheel installs/imports without PostgreSQL/private-package
  dependencies; a clean PostgreSQL-extra wheel exercises the real adapter.
- Critical privacy, tenant, density, auth-prefix, deadline/close, and deep-link
  selection controls each have a deliberate mutation killed at the real sink.
