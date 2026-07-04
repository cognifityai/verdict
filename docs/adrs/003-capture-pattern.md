# ADR-003 — Capture Pattern via wrapt Monkey-Patching

**Status:** Accepted
**Date:** 2026-05-12

## Context

We need to capture LLM traffic from customer applications. Five capture patterns exist: wrap-the-client, monkey-patch (auto-instrumentation), gateway/proxy, native SDK callbacks, eBPF. Each has tradeoffs.

Users should be able to add observability without rewriting their application
around Verdict-specific clients.

## Decision

**Primary capture: monkey-patch via `wrapt`.** The customer runs `verdict.init()`
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
Manual spans are persisted to the `spans` table alongside captured traces.

**Tertiary (planned): OpenAI-compatible gateway proxy** for environments where
running code in-process is impossible.

**Not in v0: eBPF capture.** This is out of scope for the current SDK.

## Implementation

The SDK is a self-contained capture + storage layer with **no OpenTelemetry or
OpenLLMetry runtime dependency**. It:

1. Installs `wrapt` wrappers over supported provider SDK call sites at
   `verdict.init()` time.
2. Normalizes each intercepted call into a **vendor-neutral `Trace` record**
   (see `verdict/schema.py`) — provider, model, token counts, latency, cost,
   finish reason, redacted prompt/response, plus tenant/session/cluster tags.
3. Provides the manual `@verdict.trace` decorator / context manager, which emits
   `SpanRecord`s into the same store.
4. Provides the configuration surface (`verdict.init()`).
5. Runs PII redaction on captured content **before it is persisted** (see "PII
   handling").
6. Persists everything through the `Storage` port (SQLite in v0; Postgres
   available behind the same interface).

Streaming responses are handled by wrapping the returned iterator with a
pass-through that yields each chunk immediately, collects a copy for telemetry,
and finalizes the trace in a `finally` block.

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
- When enabled, content is redacted by `verdict/redaction.py` **before it is
  written to storage** (not in an OTel SpanProcessor — we don't use OTel).
- Redaction is **regex-based**, with a **Luhn checksum + valid card-length gate**
  for credit-card numbers so that non-card digit runs (order IDs, tracking
  numbers) survive intact.
- Two redaction modes are implemented:
  - `redact` — replace the match with a placeholder (e.g. `<REDACTED>`).
  - `hash` — HMAC-SHA-256 the matched value (requires a `redaction_secret`).
- **`encrypt` mode is Planned / not yet implemented.** Selecting it is rejected at
  `init()` and `redact()` raises `NotImplementedError` (envelope encryption with a
  customer-held KMS key is deferred to a later release).
- User IDs are never stored raw: they are HMAC/SHA-256 hashed (keyed with the
  redaction secret when configured).

## Consequences

- We own and maintain our `wrapt` instrumentors for each supported provider SDK,
  rather than inheriting OpenLLMetry's coverage. This is more maintenance than the
  originally-proposed "reuse OpenLLMetry" approach would have been, but keeps us
  free of an OTel runtime dependency and gives us full control of the schema.
- The captured data lives in our own vendor-neutral schema. Interop with the
  OpenTelemetry/OpenInference ecosystems is possible later via an exporter, but is
  not available today.
- The customer never edits their LLM client code: install + import + `init()`.
- For frameworks we don't auto-instrument (custom retrieval, business logic), the
  customer uses `@verdict.trace`.
- Redaction is best-effort pattern matching, not a full PII engine; teams with
  strict requirements should keep content capture off or review the regex set.
  `encrypt` mode is not available yet.

## References

- `wrapt` library: github.com/GrahamDumpleton/wrapt (BSD-2)
- OpenTelemetry GenAI semconv (relevant only to the *planned* future exporter):
  github.com/open-telemetry/semantic-conventions/tree/main/docs/gen-ai
- OpenInference (relevant only to the *planned* future exporter):
  github.com/Arize-ai/openinference (Apache 2.0)
