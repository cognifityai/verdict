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
`Anthropic.messages.create`, `OpenAI.chat.completions.create`) by wrapping the
target functions with `wrapt`. This is a common pattern for Python
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
automatically stores the innermost `parent_span_id`. Only after that Trace is
accepted by storage does the active span chain retain the trace ID, preventing a
sampled or failed write from creating an orphan. Multiple provider calls keep
distinct Trace IDs and point to the same parent span; `SpanRecord.trace_id`
retains the first successfully persisted or explicitly bound trace. A nested
child inherits an outer provider link for correlation, but its first child-local
provider call replaces that inherited link; the outer span keeps its own first
link. For
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
finalization boundary.

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
- Redaction uses regex candidate discovery plus format-specific validation: a
  **Luhn checksum + valid card-length gate** keeps non-card digit runs (order
  IDs, tracking numbers) intact, while standard-library IPv6 validation keeps
  colon-delimited clock values such as `12:34:56` intact.
- Two redaction modes are implemented:
  - `redact` — replace the match with a placeholder (e.g. `<REDACTED>`).
  - `hash` — HMAC-SHA-256 the matched value (requires a `redaction_secret`).
- **`encrypt` mode is planned / not yet implemented.** Selecting it is rejected at
  `init()` and `redact()` raises `NotImplementedError` (envelope encryption with a
  user-held KMS key is deferred to a later release).
- User IDs are never stored raw: they are HMAC/SHA-256 hashed (keyed with the
  redaction secret when configured).

With buffered persistence, a provider/manual-span link changes from pending to
linked only after the inner trace write acknowledges success. Failure clears the
reservation and persists the manual span standalone with
`verdict.link_status=trace_write_failed`. Durable-link verification uses the
non-flushing `trace_exists()` storage operation, so closing manual spans does not
force unrelated queued trace writes onto the request path.

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
