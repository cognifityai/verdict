# ADR-006 — Import Existing Telemetry into Verdict Traces

**Status:** Accepted for implementation
**Date:** 2026-08-26
**Owner approval:** The task owner explicitly approved implementation of the
full source list on 2026-08-26 and required a pull request without merge.

## Brief and non-negotiable scope

**Intent.** Let a team point Verdict at telemetry it already owns and use the
existing Verdict trace explorer, clustering, sampling, judging, and drift
pipeline without adding provider-specific instrumentation to its application.

**Context.** Verdict already has one vendor-neutral `Trace`, SQLite/Postgres
storage ports, stable clustering, judge sampling, drift statistics, and a
dashboard. Today its primary trace producer is in-process SDK instrumentation.

**Constraints.** Keep one published `cognifity-verdict` package and the current
database schema. Use the existing storage privacy boundary. Add no vendor SDK
runtime dependencies. Do not change statistical definitions, judge
denominators, cluster identity, or dashboard query semantics. The pull request
must not be merged without human review.

**Acceptance criteria.** The supported source formats below must produce the
same stored `Trace` contract. Deterministic import IDs must make retries
idempotent. Official-shape synthetic fixtures must traverse file or real local
HTTP input, mapper, redaction, SQLite persistence, clustering, sampling, and a
key-free fake-judge pipeline. Tokens, timing, content, session, provider, model,
and source identity must survive when present and remain explicitly absent when
not present. Malformed or oversized untrusted inputs must fail safely.

**Must not change.** Published `Trace` field order, storage schema, storage URL
behavior, capture SDK behavior, cluster registry behavior, sampling rules,
judge semantics, drift mathematics, dashboard row limits, or existing CLI entry
points.

## Decision

Add a synchronous import subsystem inside `cognifity-verdict` and a
`verdict-import` CLI. Every source adapter has one responsibility: iterate
external LLM-generation records and map them to existing `Trace` objects. A
shared runner writes those objects through the existing `Storage.insert_trace`
port, which applies Verdict's recursive redaction boundary and existing UPSERT.

No external raw envelope, shadow trace graph, new canonical database, queue,
checkpoint table, or duplicate statistical path is introduced. Source-specific
fields that Verdict does not analyze are not copied. The bounded allowlist is:

- source record/trace/span identity (namespaced into deterministic Verdict IDs),
- event time and duration,
- provider, operation, request/response model, request controls,
- input/output tokens, finish reason, error, and reported cost,
- normalized supported user/assistant/system/tool text,
- tenant/session identifiers explicitly selected by the operator, and
- small non-sensitive provenance tags.

The first support surface is:

1. OTLP/HTTP JSON trace receiver and OTLP JSON files, including OpenTelemetry
   `gen_ai.*`, legacy GenAI event/attribute aliases used by Semantic Kernel,
   OpenInference, Vercel AI SDK call spans, Claude Code enhanced-telemetry LLM
   request spans, and OpenLLMetry aliases. Framework wrapper spans and tool
   spans remain non-LLM skips so one provider call does not become multiple
   Verdict traces. Claude Code content remains absent when its exporter does
   not provide response text.
2. Langfuse observations API v2. The deprecated `/api/public/traces` list is
   deliberately not used: it returns trace aggregates rather than one row per
   LLM generation, can omit child token data, and Langfuse directs bounded bulk
   reads to `/api/public/v2/observations`.
3. LangSmith run query API and LangSmith JSONL exports.
4. Datadog LLM Observability span event export API.
5. Arize Phoenix trace/span REST export (OpenInference attributes share the
   OTLP mapper).
6. Opik trace/span REST search export.
7. Local NDJSON/JSON readers for the supported native records, OTLP JSON dumps,
   and MLflow 2.x/3.x tracing exports.
8. A bounded generic voice-conversation JSON/NDJSON reader. Each completed
   assistant turn becomes one Verdict trace with the preceding conversation as
   normalized messages; audio blobs and recordings are never stored.

API imports require explicit time bounds. They are synchronous and page until
the source cursor ends. An import retry safely re-reads a bounded interval and
UPSERTs the same deterministic IDs. This deliberately removes checkpoint and
acknowledgement states. It trades extra source reads for a much smaller and more
auditable correctness surface.

File imports reject JSON files above 64 MiB and NDJSON records above 16 MiB.
Hosted API responses are capped at 64 MiB, and the OTLP receiver defaults to a
16 MiB compressed/decompressed request cap. A mapped Trace retains at most
1,000 normalized messages and 100,000 UTF-8 characters in each input/output
direction. These are safety limits, not sampling: additional source records are
still read and mapped independently.

`source_scope` is identity, not metadata. Operators should give each source
project/export stream a stable, non-secret value. The CLI defaults API scopes
to base URL plus project and file scopes to the absolute path. Moving a file
therefore changes its default deterministic IDs; pass `--source-scope` when a
portable/repeatable file identity is required.

## Designs considered

### A. New persistent canonical telemetry graph (rejected)

Store every vendor trace/span/event in new normalized tables and derive Verdict
traces asynchronously. This would preserve full graphs, but it duplicates the
customer's observability system, adds migrations and reconciliation, and makes
Verdict own two sources of truth. It also adds queue/checkpoint states before
the current product can run. Those costs do not improve Verdict's statistical
moat.

### B. Read remotely during every analysis (rejected)

Teach clustering, judging, dashboard, and drift code to query each vendor on
demand. This avoids local copies but propagates pagination, credentials,
availability, and source schema differences into every consumer. A dashboard
view and a drift run could then observe different moving datasets. It would
also fork existing storage-backed statistics.

### C. One-way adapters into existing `Trace` storage (selected)

This collapses all downstream states into the existing, tested persistence
boundary. Verdict stores the records it actually analyzes, so drift evidence
remains inspectable even if the source later expires. It does copy imported LLM
records into Verdict, but it does not copy unrelated telemetry or introduce a
second schema.

## Complexity budget

The implementation target is **3,500–4,500 new product-source lines**. A hard
design-review stop applies at **5,500 product-source lines**, excluding tests,
official-shape synthetic fixtures, documentation, and generated evidence. The
budget is not a target to fill. Each adapter module and its focused tests must
remain readable together in one review context. Crossing the stop requires an
approved scope split or a new design; it must not be hidden in helpers or
generated source.

## Lifecycle and terminal states

| Object | Owner | Success | Skip | Failure / timeout / retry | Final state |
|---|---|---|---|---|---|
| file record | file reader | decoded once | blank/non-LLM counted | malformed/oversized counted or strict failure | consumed or reported |
| API page | source adapter | every item yielded | none | bounded HTTP error; repeated cursor rejected; operator retries interval | exhausted or failed |
| source LLM record | mapper | one `Trace` | documented non-LLM/incomplete record reason | invalid required identity/time reported | mapped, skipped, or failed |
| imported `Trace` | import runner | durable `insert_trace` returns | duplicate becomes same UPSERT | storage exception terminates nonzero; retry uses same ID | durable or reported failed |
| OTLP request | receiver | entire valid bounded batch stored, response summary | non-LLM spans counted | invalid/oversized request rejected; shutdown stops accepting | responded or rejected |

The first version has no async acceptance, background write, durable cursor, or
repair worker. A successful CLI/HTTP result means synchronous storage calls
returned. For SQL storage, the existing adapter owns durability semantics.

## Mandatory defect-class closure ledger

There is no pre-existing importer defect to reproduce. The finding ledger is a
pre-implementation boundary ledger for the new trust surface:

| Finding / adjacent cases | Governing contract | Last affected sink | Status |
|---|---|---|---|
| Attributes may be a JSON object, OTLP key/value list, protobuf `AnyValue`, JSON-encoded string, or indexed OpenInference keys | one allowlisted typed value view; unknown encodings never guessed | stored scalar/content | accepted |
| Values may be missing, null, empty, duplicated, reordered, conflicting, non-finite, boolean-as-number, negative, or overflowed | documented precedence; invalid numeric values become unavailable; required identity/time failures are visible | SQL row and dashboard metrics | accepted |
| Timestamps may be ISO, epoch seconds/ms/us/ns, or OTLP nanosecond strings; end may be absent or precede start | explicit source rules; never infer unit by a floating threshold when source defines it; invalid intervals are rejected | analysis windows | accepted |
| Retries and overlapping API intervals can repeat records | UUIDv5 over adapter, tenant, non-secret source scope, source trace ID when present, and source record ID | trace primary key and later cluster assignment | accepted |
| Child span/turn IDs can repeat in different source traces or vendors/projects/tenants | deterministic namespace includes parent source trace ID, adapter, tenant, and operator-visible source scope | trace primary key | accepted |
| Pagination can be empty, repeat a cursor, omit a cursor, return 429, time out, or fail after earlier pages | bounded timeouts; cursor-cycle detection; nonzero failure with prior synchronous writes reported | import result / durable rows | accepted |
| One payload can be huge, deeply nested, or contain secrets in unknown metadata | byte/item/depth limits; no unknown metadata persistence; no credentials in messages/tags/logs | DB, CLI, HTTP response, logs | accepted |
| Message forms can be text, blocks, arrays, role objects, or nested vendor content | normalize only supported text/tool structures; preserve role order; never retain audio/image blobs | cluster input and judge prompt | accepted |
| Session/tenant fields may be absent or sensitive | optional explicit mapping; redaction-safe validation; no synthesized business identity | storage isolation and dashboard filter | accepted |
| Voice logs may contain partial transcripts, overlapping turns, audio URLs/blobs, interruption/cancellation, or speaker aliases | only completed assistant-turn text traces; no audio; stable turn IDs; explicit skip reasons | stored content and clustering | accepted |
| A storage write can fail after earlier records succeeded | synchronous per-record acknowledgement; exact imported/failed counts; idempotent rerun | SQL storage | accepted |
| Latest published a12 positional `Trace`, SQL rows, package install, and CLIs must keep working | additive modules/entry point only; no schema/signature change | wheel and old databases | accepted |
| SQLite and Postgres UPSERTs do not update every immutable source field on conflict | deterministic records are immutable for one source ID; mutable completion fields use current UPSERT contract; parity tests | SQL row | accepted with documented caveat |

Test generation must cover empty, duplicate, conflicting, reordered, and
unknown values; success, delayed completion representation, source failure,
retry, timeout, cursor cycle, and receiver shutdown; multi-tenant/session
isolation; canary secrets/PII through the final storage and API/log sinks; and
old a12 construction/storage behavior.

## Source semantics and precedence

The mapper prefers current standardized fields, then documented legacy aliases,
then vendor-native fields. It never merges contradictory token counts. Explicit
source values win over duration derived from end minus start. Missing tokens,
content, cost, session, or model remain `None`/empty as required by the existing
`Trace` schema; they are not estimated. A record lacking stable identity or a
valid start time cannot enter analysis windows and is skipped with a reason.

Cluster IDs are not imported by default. Existing Verdict clustering assigns
them from normalized user text or an explicitly mapped safe workload key.
Therefore this change does not alter clustering mathematics. Session IDs group
evidence for exploration but do not define statistical samples. Importing all
eligible source LLM records changes only the available population; the existing
pipeline still performs its own deterministic stratified sampling for judging.

## Verification and evidence labels

- **Deterministic:** parser, normalization, identity, pagination, limits,
  redaction, storage parity, and compatibility tests.
- **Synthetic contract:** official-shape vendor fixtures served through real
  local HTTP servers or files; full local SQLite import and fake-judge pipeline.
- **Live:** calls to hosted vendor APIs with scoped credentials. These are
  required before making a public live-compatibility claim and are
  `UNVERIFIED` when credentials are absent.
- **Judge quality:** the fake judge proves plumbing only. It provides no quality
  or drift-validity evidence. Real judge claims remain subject to existing
  held-out human-label calibration requirements.

## Primary specifications

- OpenTelemetry OTLP and GenAI semantic conventions:
  <https://opentelemetry.io/docs/specs/otlp/> and
  <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/>
- OpenInference semantic conventions:
  <https://arize-ai.github.io/openinference/spec/semantic_conventions.html>
- Langfuse Observations API and legacy-read migration:
  <https://langfuse.com/docs/api-and-data-platform/features/observations-api>
- LangSmith trace/query documentation:
  <https://docs.langchain.com/langsmith/export-traces>
- Datadog LLM Observability export API:
  <https://docs.datadoghq.com/llm_observability/api/>
- Phoenix REST API: <https://arize.com/docs/phoenix/sdk-api-reference/rest-api>
- Opik traces and spans API:
  <https://www.comet.com/docs/opik/reference/rest-api/overview>
- MLflow tracing search/export:
  <https://mlflow.org/docs/latest/genai/tracing/search-traces/>
