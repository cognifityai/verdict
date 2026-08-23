# ADR-003 — Capture Pattern via wrapt Monkey-Patching

**Status:** Accepted
**Date:** 2026-05-12

## Context

We need to capture LLM traffic from user applications. Five capture patterns exist: wrap-the-client, monkey-patch (auto-instrumentation), gateway/proxy, native SDK callbacks, eBPF. Each has tradeoffs.

Users should be able to add observability without rewriting their application
around Verdict-specific clients.

## Decision

**Primary capture: monkey-patch via `wrapt`.** The user runs `verdict.init()`
once at app startup; we transparently intercept provider SDK calls (e.g.
`Anthropic.messages.create`, `Anthropic.messages.stream`, and
`OpenAI.chat.completions.create` plus the OpenAI Responses resource) by wrapping
the target functions with `wrapt`.
This is a common pattern for Python
auto-instrumentation libraries.

**We do NOT depend on OpenLLMetry / OpenTelemetry instrumentation packages.** Our
capture layer is our own `wrapt`-based instrumentors. (Earlier drafts of this ADR
proposed reusing OpenLLMetry's `opentelemetry-instrumentation-*` packages and
emitting OpenTelemetry spans. That was not built; this ADR has been corrected to
describe what the code actually does. See "Planned / not yet implemented" below
for the parts that remain aspirational.)

**Secondary: explicit `@verdict.trace` decorator / context manager** for non-LLM
work (retrieval, reranking, business logic) that auto-instrumentation can't see.
Manual spans are persisted to the `spans` table alongside captured traces. When
a supported instrumented provider call starts inside a manual span, its Trace
automatically stores the innermost `parent_span_id`. This Trace-to-span pointer
is the sole automatic direction. Multiple provider calls keep distinct Trace IDs
and can all point to the same parent span without selecting an arbitrary reverse
owner. `SpanRecord.trace_id` is reserved for explicit caller binding. For
manual-only work, callers can bind an existing stored LLM trace with
`trace_context(trace_id)` or `set_context(trace_id=...)`. The binding uses
`contextvars`, is inherited by nested work and child async tasks, and is restored
by the scoped context manager. A missing explicit trace ID degrades to an
unlinked span carrying `verdict.link_status=trace_not_found`. Without a provider
call or bound trace, a span remains intentionally standalone (`trace_id=None`).

**Tertiary (planned): OpenAI-compatible gateway proxy** for environments where
running code in-process is impossible.

**Not in v0: eBPF capture.** This is out of scope for the current SDK.

## Implementation

The SDK is a self-contained capture + storage layer with **no OpenTelemetry or
OpenLLMetry runtime dependency**. It:

1. Installs `wrapt` wrappers over supported provider SDK call sites at
   `verdict.init()` time.
2. Normalizes each intercepted call into a **vendor-neutral `Trace` record**
   (see `verdict/schema.py`) — provider, model, token counts, latency, estimated cost,
   finish reason, redacted prompt/response, plus tenant/session/cluster tags.
3. Provides the manual `@verdict.trace` decorator / context manager, which emits
   `SpanRecord`s into the same store.
4. Provides the configuration surface (`verdict.init()`).
5. Runs PII redaction on captured content **before it is persisted** (see "PII
   handling").
6. Persists everything through the `Storage` port (SQLite in v0; Postgres
   available behind the same interface).

Streaming responses are handled by wrapping the returned iterator with a
pass-through that yields each chunk immediately and collects a copy for
telemetry. Full consumption, iteration error, explicit `close()` / `aclose()`,
context-manager exit, and async cancellation finalize exactly once. A dropped,
never-iterated, unclosed stream is not a supported garbage-collection
finalization boundary. Anthropic's lazy `messages.stream(...)` manager begins
capture and binds routing context on each context entry, so constructing but
never entering it performs no provider request or trace. Telemetry gives the
SDK and capture independent views of a one-shot message iterable, so SDK
request construction cannot be starved by capture. Capture observes messages
only as the SDK consumes them, so an iterable failure remains at the provider
call boundary and produces an error trace. Its event, text, and final-message
accessors all consume through the same accumulator. Anthropic
stream traces use `verdict.stream_completion` in `Trace.tags` to identify
`complete`, `partial`, or `error` finalization without changing the storage
schema. When content capture is disabled the accumulator does not retain text;
when enabled, an empty string remains distinct from unavailable content through
storage and dashboard rendering.

OpenAI Responses capture follows the same one-request/one-trace boundary for
sync and async `responses.create(...)`, `responses.parse(...)`, and the lazy
`responses.stream(...)` helper. A helper for a new response delegates
to its nested create call; a helper for an existing response delegates to its
streaming retrieve call, so the manager itself never creates a duplicate trace.
Routing binds when that nested request starts. Every valid non-stream Response
status is retained; cancelled and failed responses are error traces. Terminal
completed, incomplete, and failed stream events retain their distinct outcome,
while explicit close, helper exit, application error, transport error, and
cancellation finalize once with `verdict.stream_completion` set to `complete`,
`partial`, or `error`. A partial stream also retains content carried only by a
valid output-text or refusal `done` event; its authoritative full value replaces
any observed suffix deltas for the same output/content identity. SDK-local
validation and request-hook failures before transport emit no trace. Each
Responses resource call owns its exact SDK request-options object, and only the
matching native HTTP transport invocation establishes the provider boundary, so
nested same-client requests cannot mark the outer call. Both OpenAI's legacy
`httpx` layout and its current `httpx2` layout are discovered from the installed
SDK; an explicitly injected supported legacy client is wrapped as well. Capture reads an
allowlisted view of the already-serialized outbound JSON. This preserves SDK
mapping/list semantics, applies actual `extra_body` precedence, and records the
wire snapshot rather than a later caller mutation without pre-traversing input.
Content fields are retained from that snapshot only when content capture is
enabled; disabled capture retains scalar request metadata only.
The
declared `openai>=1.56.2` minimum predates the Responses resource,
so installation feature-detects it and continues to capture Chat Completions on
older supported SDKs. This floor is the first tested patch after OpenAI's HTTPX
0.28 default-client incompatibility and avoids constraining HTTPX for the Google
extra; CI exercises that ordinary constructor as well as injected local
transports. OpenAI's
`beta.chat.completions` alias shares the stable Chat resource class and therefore
the same wrappers. OpenAI's `responses.with_streaming_response` raw-response
manager and the separate experimental `beta.responses` multi-agent resource are
outside this bounded support surface.

Provider request scalars cross one typed boundary before Trace construction and
are normalized again before persistence because response usage and latency are
filled later. SDK-specific unset objects, booleans, non-finite numbers, and
values outside the common SQLite/PostgreSQL representation become unavailable
rather than being handed to a database driver. Synchronous persistence remains
non-raising on the application path, but failures emit a bounded warning keyed
by provider, storage type, and exception type.

## Wire format / schema

There is **no OpenTelemetry/OpenInference span emission today.** Instead we use an
internal, **vendor-neutral schema** (`Trace`, `Judgment`, `SpanRecord`,
`DriftSignal`, `UserSignalRecord`) defined in `verdict/schema.py` and written to
storage. Field names are our own and are stable within v0.

**Planned:** an optional exporter that maps the internal
schema onto OpenTelemetry GenAI semantic conventions and/or OpenInference
formats. This is a future interop layer, not part of the current build.

## Performance

The capture wrapper is intended to add minimal overhead, but there is no
published latency benchmark yet. Performance figures should be measured in the
target deployment before relying on them.

## PII handling

- Content capture (prompts/completions) **off by default**.
- When enabled, content is redacted by `verdict/redaction.py` before assignment
  to the captured `Trace`; every storage adapter applies a second boundary so a
  manually constructed record cannot bypass it. Dashboard/export paths reapply
  redaction for historical rows written before that boundary existed.
- Provider message persistence uses an allowlist of supported top-level fields.
  Every string key and value in their JSON-compatible nested structures is
  sanitized recursively, including OpenAI tool arguments and Anthropic-style
  tool inputs/results. Traversal has node and character budgets. Cycles, repeated
  container references, excessive depth/size, non-finite numbers, non-string
  object keys, and unsupported objects fail closed rather than being copied.
  Sanitized structures never retain caller-owned aliases, and mapping keys that
  collide after redaction receive deterministic suffixes so no value is lost.
- Judge reasoning/errors and manual-span names, errors, and nested attributes are
  sanitized at persistence as well.
- Redaction uses linear candidate scanning for emails plus regex discovery and
  format-specific validation for other patterns. The email scanner anchors at
  each `@` and advances monotonically, avoiding pathological backtracking on
  malformed or very long input. A **Luhn checksum + valid card-length gate**
  keeps non-card digit runs (order IDs, tracking numbers) intact, while
  standard-library IPv6 validation keeps colon-delimited clock values such as
  `12:34:56` intact and separates a validated address from trailing text that
  is not part of that address.
- Two redaction modes are implemented:
  - `redact` — replace the match with a placeholder (e.g. `<REDACTED>`).
  - `hash` — HMAC-SHA-256 the matched value (requires a `redaction_secret`).
- **`encrypt` mode is planned / not yet implemented.** Selecting it is rejected at
  `init()` and `redact()` raises `NotImplementedError` (envelope encryption with a
  user-held KMS key is deferred to a later release).
- User IDs are never stored raw: they are HMAC/SHA-256 hashed (keyed with the
  redaction secret when configured).

Buffered persistence does not add a trace/span acknowledgement protocol. Every
ended manual span is persisted exactly once, independently of whether a provider
trace is sampled, succeeds, fails, is delayed, or is dropped during shutdown.
Because automatic capture never writes a provider trace ID into the span, it
needs no pending state, reconciliation callback, record mutation, or compensating
upsert. Reverse lookup uses the indexed `Trace.parent_span_id` field.

Retention pruning deletes expired standalone span rows and expired spans whose
referenced trace no longer exists, in addition to deleting spans attached to
expired traces. A retained Trace protects its `parent_span_id` span even when the
span began before the retention cutoff. SQL adapters perform the related
multi-table cleanup in one transaction and serialize concurrent trace writers
while deciding which shared parent spans remain protected.

## Consequences

- We own and maintain our `wrapt` instrumentors for each supported provider SDK,
  rather than inheriting OpenLLMetry's coverage. This is more maintenance than the
  originally-proposed "reuse OpenLLMetry" approach would have been, but keeps us
  free of an OTel runtime dependency and gives us full control of the schema.
- The captured data lives in our own vendor-neutral schema. Interop with the
  OpenTelemetry/OpenInference ecosystems is possible later via an exporter, but is
  not available today.
- Existing calls through supported provider SDK methods remain unchanged after
  adding the Verdict import and `init()` call.
- For frameworks we don't auto-instrument (custom retrieval, business logic), the
  user can add `@verdict.trace`.
- Redaction is best-effort pattern matching, not a full PII engine; teams with
  strict requirements should keep content capture off or review the regex set.
  Names, postal addresses, dates of birth, many international identifiers, and
  opaque tenant/session/cluster identifiers are not comprehensively detected.
  Application identifiers must be non-sensitive. `encrypt` mode is not
  available yet.

## References

- `wrapt` library: github.com/GrahamDumpleton/wrapt (BSD-2)
- OpenTelemetry GenAI semconv (relevant only to the *planned* future exporter):
  github.com/open-telemetry/semantic-conventions/tree/main/docs/gen-ai
- OpenInference (relevant only to the *planned* future exporter):
  github.com/Arize-ai/openinference (Apache 2.0)
