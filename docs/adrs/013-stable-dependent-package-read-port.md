# ADR-013: Stable read port for dependent packages

**Status:** Proposed
**Date:** 2026-09-11
**Decider:** Cognifity AI

## Context

Optional packages need a small amount of Verdict evidence without becoming
coupled to Verdict's SQLite or PostgreSQL schema. Today they could import the
large internal `Storage` protocol, call dashboard query helpers, or issue SQL
against current tables. All three choices expose physical storage and make an
otherwise internal migration a breaking change for every dependent package.

The first proven consumer needs one selected Agent Run, the time and Trace
identity of its model calls, and deterministic finding facts. It does not need
prompts, responses, raw messages, arbitrary event attributes, judgments,
provider/model display names, agent/service/environment names, complete Trace
records, database cursors, or write access. It also cannot obtain a LiteLLM
deployment or GPU identity from Verdict unless a separate correlation source
supplies an explicit stable join.

## Contract and deduplicated finding matrix

| Finding or adjacent case | Governing contract | Last affected sink |
| --- | --- | --- |
| A dependent package reads Verdict tables or dashboard SQL | Only Verdict's adapter may touch `Storage`; consumers receive versioned immutable DTOs | Built private-package wheel import and real storage read |
| A Verdict migration renames or normalizes tables | The V1 port and DTOs do not change; only the adapter changes | SQLite and PostgreSQL contract tests |
| One run ID exists in several tenants | Every read requires a bounded non-empty tenant and run ID; no implicit/default tenant exists | Returned DTO or `None` |
| A run is absent | Exact lookup returns `None`, never an empty synthetic run | Public Python call |
| Storage fails or returns a malformed/mismatched bundle | The adapter raises a stable coded error without copying or chaining the underlying message | Dependent-package exception boundary |
| Evidence contains prompts, responses, model names, tool arguments/results, commands, errors, arbitrary tags, secrets, or PII | Those fields do not exist in the DTO; exclusion canaries stay absent from its `repr` and JSON | Public DTO and serializer |
| Included identifiers themselves contain operator or personal data | Tenant, run, event, and Trace IDs are treated as protected Verdict metadata, not declared non-sensitive | Trusted composition root, DTO, and JSON |
| A model call has no Trace link | The call remains present with `trace_id=None`; absence is not converted into a guessed join | Model-call DTO and correlation result |
| A run is open, failed, cancelled, timed out, or has no events/findings | Status and optional end time remain explicit; an absent run alone returns `None` | Agent Run DTO |
| Tuple order differs across storage adapters | The bundle is canonicalized before both analysis and projection | Memory, SQLite, buffered SQLite, and PostgreSQL parity fixture |
| A run has more model calls/findings than V1 returns | Pre-truncation totals and collection-level truncation flags are explicit | DTO and JSON |
| A finding cites tool/test/command evidence or a model call omitted by the model-call cap | Witness IDs remain truthful but do not create a correlation unless the exact returned model-call event ID matches | Private correlation state |
| A future finding code appears | The bounded code is retained; no enum map silently drops it | DTO and dependent fixture |
| A consumer infers gateway deployment, backend pool, or GPU from display names | The port exports no display-name identity; only exact returned event/Trace identity may enter a separate verified join | `unmapped` or `insufficient_evidence` result |
| A caller needs a network service, list scan, or write | V1 provides none; auth, pagination, fleet access, and mutations require separate contracts | Dependency and route inventory |
| An existing Verdict caller or stored row is upgraded | Existing imports, dataclass fields, `Storage` signatures, schemas, routes, and behavior remain compatible | Published wheel and old SQLite fixture |

## Decision

Verdict will expose one narrow public Python module, `verdict.read_port`. It
contains the following exact V1 contract. Field order is part of the consumer
fixture:

```python
@dataclass(frozen=True)
class ModelCallRead:
    event_id: str
    occurred_at: datetime
    status: str
    trace_id: str | None
    latency_ms: float | None

@dataclass(frozen=True)
class FindingRead:
    code: str
    severity: str
    witness_event_ids: tuple[str, ...]

@dataclass(frozen=True)
class AgentRunRead:
    schema_version: str
    analysis_version: str
    tenant_id: str
    run_id: str
    started_at: datetime
    ended_at: datetime | None
    status: str
    model_call_count: int
    model_calls: tuple[ModelCallRead, ...]
    model_calls_truncated: bool
    finding_count: int
    findings: tuple[FindingRead, ...]
    findings_truncated: bool

class VerdictReadPort(Protocol):
    def get_agent_run(
        self, *, tenant_id: str, run_id: str
    ) -> AgentRunRead | None: ...

class StorageVerdictReadPort:
    def __init__(self, storage: Storage) -> None: ...
    def get_agent_run(
        self, *, tenant_id: str, run_id: str
    ) -> AgentRunRead | None: ...

def agent_run_read_to_json(value: AgentRunRead) -> str: ...

class VerdictReadError(RuntimeError):
    code: str
    def __init__(self, code: str) -> None: ...
```

The exact schema version is `verdict.agent-run-read.v1`; the initial analysis
version is `verdict.agent-analysis.v1`. A V1 class or protocol does not gain
fields or methods. A future shape or operation gets V2 types, a V2 protocol,
and a V2 serializer. Finding semantics remain owned by the shared
`analyze_agent_run` implementation rather than being copied into this adapter.
Every semantic change to that analyzer must bump the independent
`analysis_version`, even when the DTO schema stays V1. A consumer maintains its
own explicit analysis-version allowlist and rejects an unsupported value; it
must not interpret every value carried by a schema-V1 DTO as equivalent.

`StorageVerdictReadPort` is the only default adapter. It calls the existing
exact `Storage.get_agent_run_bundle(tenant_id, run_id)` method. A dependent
package types only against `VerdictReadPort` and the three read DTOs; its
trusted host composition root constructs the concrete storage adapter. The
dependent package never imports a storage implementation, opens a Verdict
table, or calls a dashboard module.

### Validation, errors, and authorization

Public constructors reject invalid values with `ValueError`. The adapter and
serializer expose `VerdictReadError`, which inherits directly from
`RuntimeError`. Its constructor accepts only one supported code, stores that
exact string in public `.code`, calls `RuntimeError.__init__(code)`, and
therefore has `str(error) == code` and `error.args == (code,)`. Codes are:

- `invalid_query`: tenant/run input is invalid before storage access;
- `read_unavailable`: storage raised or could not complete the exact lookup;
- `invalid_read_model`: stored evidence, canonical analysis, or projection
  violates the V1 contract; or
- `response_limit_exceeded`: the exact encoded JSON exceeds 262,144 bytes.

Underlying exceptions are suppressed with `from None`; their types, messages,
paths, SQL, and credentials never enter the public error or logs owned by this
port. An absent, valid lookup returns `None` and is not an error.

All IDs are non-empty valid UTF-8, contain no NUL, and are at most 256 encoded
bytes. Finding code is at most 64 UTF-8 bytes. Status is exactly one of
`completed`, `failed`, `timed_out`, `cancelled`, and `unknown`; severity is
exactly `info`, `warning`, or `error`. Counts are non-Boolean integers from 0
through 2^63-1. Booleans are exact booleans. `latency_ms` is `None` or a finite
non-negative float. Datetimes are timezone-aware, normalized to UTC, and an
end time cannot precede the start time.

`schema_version` must equal `verdict.agent-run-read.v1`.
`analysis_version` must be a member of the Verdict release's explicit supported
analysis-version set, initially only `verdict.agent-analysis.v1`. Consumers
still enforce their own allowlists. Collection values must be exact tuples and
their members must be exact `ModelCallRead` or `FindingRead` instances, not
arbitrary lookalikes. Returned model-call event IDs are unique, as are their
non-null Trace IDs. Every witness tuple contains no more than 20 unique IDs.

The collection relations are exact:

- `len(model_calls) == min(model_call_count, 16)` and
  `model_calls_truncated is (model_call_count > 16)`; and
- `len(findings) == min(finding_count, 4)` and
  `findings_truncated is (finding_count > 4)`.

This rejects contradictory directly constructed DTOs instead of relying on the
adapter to be their only producer.

`tenant_id` scopes the storage lookup; it does not authenticate or authorize a
caller. The trusted composition root must authorize the tenant before invoking
the port and must protect the returned metadata like other Verdict evidence.
V1 deliberately does not contain an HTTP/authentication layer.

### Bounded canonical analysis and projection

Immediately after the exact storage lookup returns, and before copying,
canonicalizing, or analyzing its tuples, the adapter rejects a bundle with
more than 1,000 turns or 1,500 events as `invalid_read_model`. These constants
cover Verdict's existing bounded source-import path while limiting in-process
sort and analysis work. The existing storage method nevertheless materializes
the full exact run before the adapter can inspect tuple lengths. V1 therefore
bounds projection, analysis CPU/memory, and output, but does **not** claim a
hard pre-I/O backing-store bound. It is restricted to the trusted same-process
composition in this ADR. A hard database-read bound requires a separately
justified storage primitive and does not belong in this PR.

For an accepted-size bundle, the adapter validates it and constructs one
canonical `AgentRunBundle` before doing any analysis:

1. normalize every datetime to UTC;
2. sort turns by `(sequence, turn_id)` and assign their resulting rank;
3. sort events by `(occurred_at, turn_rank, sequence, event_id)`; and
4. retain the same validated session/run/evidence values in that order.

The adapter passes that exact canonical bundle to Verdict's judge-free
`analyze_agent_run` and projects model calls from the same bundle. Model calls
keep canonical event order. Findings are sorted by severity rank
`error, warning, info`, then `code` and `witness_event_ids`.
Counts are measured before collection truncation. No finding is re-run against
a differently ordered bundle. The port invokes no judge and therefore exports
no redundant `judge_used` field.

V1 returns at most 16 model calls and four findings. A finding's
`witness_event_ids` are the analyzer-selected, already bounded set of at most
20 IDs; they are not claimed to enumerate every causal event. If a future
analysis implementation violates that analysis-version bound, the adapter
returns `invalid_read_model` instead of silently truncating witness evidence.
A witness can name non-model evidence or a model call outside the returned
16-call collection. It is usable for correlation only when it exactly equals a
returned `ModelCallRead.event_id` whose `trace_id` is non-null.

### Canonical JSON and size proof

`agent_run_read_to_json` accepts exactly `AgentRunRead` and emits UTF-8 JSON
with `ensure_ascii=False`, `allow_nan=False`, sorted keys, and compact
separators. JSON keys are the snake-case dataclass field names shown above;
nested objects use the exact `ModelCallRead` and `FindingRead` keys. Tuples
serialize as arrays. Datetimes serialize in UTC RFC 3339 as
`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`. `None` serializes as JSON `null`; no field is
omitted. The serializer measures final encoded bytes and raises
`response_limit_exceeded` above 262,144 bytes.

The cap is reachable only after construction bounds are applied. JSON can
escape a one-byte control character as six bytes, so the conservative identity
envelope is:

- 16 calls x two 256-byte IDs x six = 49,152 bytes;
- four findings x 20 witness IDs x 256 x six = 122,880 bytes; and
- top-level IDs, four finding codes, timestamps, statuses, numeric values,
  keys, punctuation, and flags are allocated a further 32,768 bytes.

That totals at most 204,800 bytes, leaving 57,344 bytes below the hard limit.
The final byte check remains authoritative and protects against implementation
or encoding mistakes.

### Dependency and upgrade shape

```text
Customer/private composition root
        |-- public Verdict
        |      `-- StorageVerdictReadPort -- internal Storage -- Verdict store
        |
        `-- optional private package
               `-- depends only on VerdictReadPort + read DTOs
```

A Verdict schema migration updates the storage adapter and parity tests. The
optional package does not change unless it deliberately adopts a new public
read contract version. It pins a supported Verdict version range and upgrades
by replacing its own package version plus a compatible public Verdict wheel;
it never runs or owns a Verdict migration itself.

### Correlation boundary

This port supplies a selected run window, finding facts, model-call timestamps,
and linked Trace IDs. It does **not** prove which LiteLLM deployment served a
call or which GPU executed it. A later private correlation adapter must join an
exact Trace/request identity to gateway telemetry through an explicit
operator-owned mapping, propagated correlation ID, trace/log/exemplar source,
or report `unmapped`/`insufficient_evidence`. Provider and model display names
are intentionally absent and are never accepted as physical identity.

## Considered options

### Let dependent packages query Verdict tables read-only

Rejected. Read-only SQL still couples column names, migrations, tenancy rules,
redaction, and storage-engine differences to every consumer.

### Export the existing `Storage` protocol

Rejected. It exposes writes and internal records. It is not a consumer contract
and would make unrelated storage evolution a compatibility promise.

### Reuse dashboard JSON or dashboard query helpers

Rejected. That projection contains presentation-specific aggregates and direct
SQL helpers. It would couple headless consumers to UI evolution.

### Add an authenticated HTTP service now

Deferred. A network API requires authentication, authorization, tenant routing,
pagination, rate limits, deployment ownership, and a new attack surface. The
first optional packages compose in one trusted process. A future remote adapter
may reuse versioned DTO semantics after those requirements are justified.

## Verification contract

- Unit tests cover every status, absent/open/empty evidence, missing Trace and
  latency, exact constructor bounds, UTC normalization, and each stable error.
- Tests put canaries separately into excluded content/model/operator fields and
  included identifiers. Excluded canaries remain absent from DTO `repr` and
  JSON; included IDs remain exact, proving the privacy claim is neither broader
  nor narrower than the real contract.
- Consumer fixtures pin dataclass field order and annotations, protocol and
  serializer signatures, schema version, current analysis version, explicit
  unsupported-analysis rejection, exact JSON keys, timestamp formatting, and
  the rule that V1 does not grow additively.
- Constructor tests cover exact tuple/member types, count/length/truncation
  relations, unique event/Trace/witness IDs, and the exact
  `VerdictReadError` inheritance, `.code`, `str`, and `args` contract.
- Shuffled equivalent bundles produce byte-identical JSON and findings.
  Deliberate mutations prove canonicalization occurs before analysis, finding
  totals precede truncation, and witness IDs are never presented as exhaustive.
- Limits test exactly 1,000/1,001 source turns, 1,500/1,501 source events,
  16/17 returned model calls, 4/5 findings, 20/21 analyzer witnesses, the
  conservative worst-case envelope, and the 262,144-byte final rejection.
- A storage spy proves oversized source tuples are rejected before any
  canonical copy or analyzer call; documentation and tests do not claim the
  preceding exact database lookup itself is bounded.
- The same lookup, canonical DTO, and tenant-isolation contract runs through
  InMemory, SQLite, Buffered SQLite, and a live disposable PostgreSQL database.
  If live PostgreSQL cannot run, the PR is not merge-ready.
- An old supported SQLite fixture is opened through the candidate adapter.
- Storage failures and malformed/mismatched bundles prove stable error codes,
  no underlying exception chaining, and no raw-message leakage.
- Existing Verdict imports, behavior, schemas, routes, `Storage` signature, and
  serialization fixtures stay unchanged.
- The built wheel is installed into a clean environment and a tiny independent
  consumer imports only `verdict.read_port`, reads a real SQLite run, and
  serializes it.

## Consequences

- Schema changes remain private to Verdict adapters instead of rippling into
  private products.
- Optional packages gain a small testable dependency surface with no database
  or dashboard knowledge.
- Included IDs are useful for exact joins but remain protected metadata.
- V1 supports exact selected Agent Run correlation only. Trace scans, reporting
  aggregates, remote access, and writes remain absent until a consumer proves
  those contracts are needed.
- Request-to-GPU attribution remains unavailable without a separate explicit
  identity-bearing telemetry path; the read port exposes that limitation
  rather than guessing.
