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

The first proven consumer needs one selected Agent Run, its metadata-only model
call references, and its deterministic finding facts. It does not need prompts,
responses, raw messages, arbitrary event attributes, judgments, complete Trace
records, database cursors, or write access. It also cannot obtain a LiteLLM
deployment or GPU identity from Verdict unless a separate correlation source
supplies an explicit stable join.

## Contract and deduplicated finding matrix

| Finding or adjacent case | Governing contract | Last affected sink |
| --- | --- | --- |
| A dependent package reads Verdict tables or dashboard SQL | Only Verdict's adapter may touch `Storage`; consumers receive versioned immutable DTOs | Built private-package wheel import and real storage read |
| A Verdict migration renames or normalizes tables | The public DTO and port signature remain unchanged; only the adapter changes | SQLite and PostgreSQL contract tests |
| One run ID exists in several tenants | Every read requires a bounded non-empty tenant and run ID; no implicit/default tenant exists | Returned DTO or `None` |
| A run is absent | Exact lookup returns `None`, never an empty synthetic run | Public Python call and JSON serializer |
| Storage fails or returns a malformed/mismatched bundle | The adapter raises one stable, non-sensitive read error without copying the underlying message | Dependent-package exception boundary |
| Evidence contains prompts, responses, tool arguments/results, commands, errors, arbitrary tags, secrets, or PII | None of those fields exists in the DTO; recursive canaries stay absent from dataclass representation and serialized JSON | Public DTO, `repr`, and JSON |
| A model call has an unknown provider/model or no Trace link | Bounded metadata remains representable; `trace_id` and model fields are nullable where source evidence is missing | Model-call reference DTO |
| A run is open, failed, cancelled, timed out, or has no events/findings | Status and optional end time remain explicit; missing evidence is not reported as a successful empty result | Agent Run DTO |
| Event order differs in storage | References are projected deterministically by occurrence time, turn sequence, event sequence, and event ID; the order is presentation order, not a causal claim | DTO and canonical JSON |
| A run exceeds consumer response limits | Total counts and truncation flags remain explicit; at most 100 model calls and 100 findings are returned | DTO/JSON size boundary |
| A finding refers to more evidence events than the DTO limit | At most 20 event IDs remain, matching deterministic analysis; truncation is explicit at the containing collection | Finding DTO |
| A future internal field or finding code appears | Known typed scalar fields project normally and unknown finding codes remain bounded text; no enum map silently drops them | DTO and dependent contract fixture |
| A consumer tries to infer gateway deployment, backend pool, or GPU from provider/model display names | The read port exposes no such inferred field; `trace_id` is only a possible input to a separately verified correlation source | Correlation consumer state (`unmapped` or `insufficient_evidence`) |
| A caller needs a network service, list scan, or write | This PR provides none; auth, pagination, fleet access, and mutations require separate use cases and contracts | Dependency and route inventory |
| An existing Verdict caller or stored row is upgraded | Existing top-level imports, dataclass field order, `Storage` signatures, schemas, routes, and behavior remain byte-for-byte compatible | Published-wheel compatibility and old SQLite fixture |

## Decision

Verdict will expose one narrow public Python read boundary in
`verdict.read_port`:

```python
class VerdictReadPort(Protocol):
    def get_agent_run(
        self, *, tenant_id: str, run_id: str
    ) -> AgentRunRead | None: ...

class StorageVerdictReadPort:
    def __init__(self, storage: Storage) -> None: ...
```

`StorageVerdictReadPort` is the only default adapter. It uses the existing
exact `Storage.get_agent_run_bundle(tenant_id, run_id)` method and projects the
result into immutable read DTOs. A dependent package types against
`VerdictReadPort`, `AgentRunRead`, `ModelCallRead`, and `FindingRead`; its host
composition root constructs the storage adapter. The dependent package never
imports a concrete storage adapter, opens a Verdict table, or calls a dashboard
module.

The initial DTO is deliberately metadata-only:

- `AgentRunRead`: schema and analysis versions, tenant/run/source identity,
  started/ended time, status, optional service/environment/agent identifiers,
  model-call/finding totals, bounded collections, and explicit truncation;
- `ModelCallRead`: event identity and time, status, optional linked Trace ID,
  and bounded provider/request-model/response-model metadata; and
- `FindingRead`: bounded code, severity, evidence event IDs, and whether a judge
  was used. The analyzer's prose is not exported.

The adapter reuses Verdict's deterministic `analyze_agent_run` function. It
does not persist a snapshot, modify the run, run a judge, read raw Trace
content, or create another analysis path. The read schema version identifies
the DTO shape; the analysis version identifies the meaning of finding facts.
Both are serialized explicitly.

All public text and identifiers have byte limits, datetimes are timezone-aware,
numbers are finite and bounded, collections are tuples, and canonical JSON is
bounded to 256 KiB. Invalid construction fails closed. Underlying adapter
exceptions become a stable `VerdictReadUnavailable` exception with no raw
database message or chained exception.

The initial port performs exact lookup only. It has no list method, offset,
cursor, time-range scan, or arbitrary field selection. New use cases may add
new methods and DTO versions additively after their pagination, authorization,
and response-size semantics are frozen.

### Dependency and upgrade shape

```text
Customer/private composition root
        |-- public Verdict
        |      `-- StorageVerdictReadPort -- internal Storage -- Verdict store
        |
        `-- optional private package
               `-- depends only on VerdictReadPort + read DTOs
```

A Verdict schema migration updates the storage adapter and its parity tests.
The optional package does not change unless the public read contract version it
uses changes. The optional package pins a supported Verdict version range and
upgrades by replacing its own package version plus a compatible public Verdict
wheel; it never runs or owns a Verdict migration itself.

### Correlation boundary

This port supplies a selected run window, finding facts, model-call timestamps,
and linked Trace IDs. It does **not** prove which LiteLLM deployment served a
call or which GPU executed it. A later private correlation adapter must join a
Trace/request identity to gateway telemetry through an explicit operator-owned
mapping, propagated correlation ID, trace/log/exemplar source, or report
`unmapped`/`insufficient_evidence`. Provider and model display names are not
accepted as physical-infrastructure identity.

## Considered options

### Let dependent packages query Verdict tables read-only

Rejected. Read-only SQL still couples column names, migrations, tenancy rules,
redaction, and storage-engine differences to every consumer.

### Export the existing `Storage` protocol

Rejected. It exposes dozens of write and internal read methods plus physical
domain records. It is not a consumer contract and would make unrelated storage
evolution a compatibility promise.

### Reuse dashboard JSON or dashboard query helpers

Rejected. The dashboard projection contains presentation-specific aggregates
and currently includes direct SQL helpers. It is not a stable package API and
would couple headless consumers to UI evolution.

### Add an authenticated HTTP service now

Deferred. A network API requires authentication, authorization, tenant routing,
pagination, rate limits, deployment ownership, and a new attack surface. The
first wrapper and optional packages can compose in one trusted process against
the same public Python port. A future remote deployment may adapt the same DTOs
after those operational requirements are independently justified.

## Verification contract

- Unit tests cover every status, missing/open/empty evidence, nullable and
  unknown model metadata, deterministic ordering, collection/byte limits,
  malformed adapter results, stable exception sanitization, and constructor
  validation.
- Recursive secret/PII canaries in prompts, responses, raw messages, tool
  arguments/results, command output, arbitrary tags, and stored error strings
  remain absent from DTOs, `repr`, and canonical JSON.
- Consumer-driven fixtures pin the exact public dataclass field order, protocol
  signature, schema version, analysis version, and JSON keys.
- The same exact lookup and tenant-isolation contract runs through InMemory,
  SQLite, Buffered SQLite, and live disposable PostgreSQL. If live PostgreSQL
  cannot run, the PR is not merge-ready.
- An old supported SQLite fixture is opened through the candidate adapter.
- Deliberate mutations prove tenant forwarding, result projection, content
  omission, truncation/count signals, deterministic ordering, analyzer use,
  exception sanitization, and JSON bounds.
- The built wheel is installed into a clean environment and a tiny independent
  consumer imports only `verdict.read_port`, reads a real SQLite run, and
  serializes the result.

## Consequences

- Schema changes remain private to Verdict adapters instead of rippling into
  private products.
- Optional packages gain a small, testable dependency surface with no database
  or dashboard knowledge.
- The first version supports exact selected Agent Run correlation only. Trace
  scans, drift findings, reporting aggregates, remote access, and writes remain
  intentionally absent until a consumer contract proves they are needed.
- Request-to-GPU attribution remains unavailable without a separate explicit
  identity-bearing telemetry path; the read port makes that limitation visible
  rather than guessing.
