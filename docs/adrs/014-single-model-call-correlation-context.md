# ADR-014: Single model-call correlation context

**Status:** Proposed
**Date:** 2026-09-11
**Decider:** Cognifity AI

## Context

Verdict's stable V1 ReadPort exposes the exact Trace ID attached to a captured
model-call event. A private dependent package can therefore identify a stored
Verdict model call without reading Verdict tables. It still cannot prove that
the model call is the same request observed by an LLM gateway: Verdict creates
the Trace ID inside provider instrumentation, after application code has
already assembled the provider request.

The application needs the identifier before one provider call so a separately
owned gateway profile can put it in the gateway's supported request-ID header.
The same identifier must then become that call's Verdict Trace ID. Verdict must
not know about LiteLLM, HTTP headers, request logs, Prometheus, Operations, or
hardware topology. It must not change storage schemas or reinterpret the
existing `trace_context`, which links manual spans to an already captured
Trace.

## Contract and defect-class closure

| Finding or adjacent case | Governing contract | Last affected sink |
| --- | --- | --- |
| Application code cannot know a Verdict Trace ID before a provider call | `model_call_context()` generates and yields the ID before executing user code | Public context-manager value and stored model-call Trace ID |
| Verdict injects a LiteLLM or vendor header | Verdict only reserves the Trace identity; a separate private gateway profile owns header construction | Provider request captured by a real gateway |
| Two provider calls reuse one Trace ID | One context reservation is claimed atomically by at most one supported instrumented provider Trace | Storage uniqueness and ReadPort model-call collection |
| Copied async/thread contexts race to use the reservation | Copied contexts share one small claim object protected by a lock; exactly one caller can claim it | Concurrent provider traces and stored Trace IDs |
| Nested contexts corrupt or consume one another | Every context has an independent reservation and restores the prior context on every exit | Inner and outer stored provider traces |
| The context exits normally, raises, is cancelled, or is never consumed | The context-local reservation is always restored; an unused ID creates no Verdict record | Subsequent provider call and process context |
| A lazy stream is created inside the context but entered after it exits | Only a provider Trace actually constructed inside the context may claim the ID; object creation alone is not evidence of a request | Stream Trace and gateway request log |
| A provider call fails before reaching the gateway | Verdict may capture the failed call under the reserved Trace ID, while the gateway join remains absent and therefore unmapped | ReadPort result and private correlation result |
| Sampling or an unsampled Agent Run prevents persistence | The context reserves identity but does not promise persistence or override existing sampling decisions | Verdict store and ReadPort `None`/missing call |
| Instrumentation is disabled, unavailable, or not initialized | The ID can still reach a gateway through private code, but Verdict creates no matching record and correlation remains unavailable | Private correlation result |
| Caller performs an application-level retry in the same context | Only the first supported instrumented attempt claims the reservation; each separately correlated retry needs its own context | Stored attempts and gateway facts |
| OpenAI, Anthropic, or Google sync, async, streaming, error, or cancellation paths diverge | All provider Trace builders use one provider-neutral claim helper before the provider call | Real SDK return/error plus stored Trace |
| Existing `trace_context`, manual spans, routing context, or automatic parent spans change meaning | The new reservation uses a distinct context variable and does not read or write the manual-span link | Existing public API and span storage fixtures |
| Caller supplies a collision-prone, secret-bearing, or personally identifying ID | V1 accepts no caller-provided value and yields a generated 128-bit lowercase hexadecimal identifier | Public value, request header, logs, and storage |
| The identifier is logged or presented as non-sensitive | Verdict emits no new log; callers must protect it as operational Trace metadata | Application/gateway logs and dependent-package output |
| A dependent package assumes the ID proves gateway, deployment, or hardware identity | The ID proves only equality when an authorized correlation source returns exactly one matching fact; topology and sampled hardware remain separate evidence | Customer correlation assessment |
| A Verdict upgrade changes tables or capture internals | The public context API and V1 ReadPort remain the only dependent contracts; no private package imports capture/storage internals | Built public and private wheels |

## Decision

Verdict will add one provider-neutral public context manager:

```python
from collections.abc import Iterator
from contextlib import contextmanager

@contextmanager
def model_call_context() -> Iterator[str]: ...
```

Normal use is:

```python
with verdict.model_call_context() as correlation_id:
    headers = gateway_profile.headers(correlation_id)
    response = provider_client.chat.completions.create(
        model="customer-model",
        messages=messages,
        extra_headers=headers,
    )
```

`model_call_context()` takes no arguments. On entry it creates a UUID4-style
128-bit identifier encoded as exactly 32 lowercase ASCII hexadecimal
characters, binds one pending reservation to the current logical context, and
yields the identifier. The first supported Verdict provider instrumentor that
constructs a Trace while the reservation is active atomically claims it and
uses it as that Trace's `trace_id`. Later provider calls in the same context
use their normal independently generated Trace IDs. On every exit, the prior
reservation is restored.

An internal claim operation returns either the one reserved identifier or
`None`. The reservation object holds only the identifier, a claimed flag, and
a lock. A copied `contextvars` context intentionally shares that object so
concurrent children cannot duplicate the ID. The lock is held only while
checking and changing the in-memory flag; it spans no provider, storage, or
network work. The operation is not public and does not expose mutable claim
state.

Every supported provider Trace builder calls the same claim operation from the
existing routing-context application point. A successful claim replaces only
the freshly generated Trace ID. It does not change the provider request,
sampling, capture content, tenant/session/user/workload routing, parent spans,
Agent Run sampling, error handling, persistence, or redaction. Existing Trace
normalization and storage validation remain authoritative.

The public value is operational metadata, not a credential or proof of
authorization. Verdict does not log it. A caller may send it only to an
authorized service and should avoid ordinary application logs. A gateway
adapter must accept the identifier only through a fixed local API, not an
arbitrary browser parameter, and must use a gateway-supported exact request-ID
field. Gateway lookup, response minimization, tenant authorization, topology,
and hardware evidence are outside Verdict.

## Exact meaning of correlation

Matching the returned value to `ModelCallRead.trace_id` proves that Verdict
captured the instrumented provider call that claimed the reservation. Matching
the same value to exactly one fact from an authorized gateway source proves an
exact request-to-gateway join for that call. It does not by itself prove:

- that the gateway fact names the correct deployment unless the source owns
  that field;
- that a configured deployment-to-backend mapping was active at the time;
- that sampled CPU, memory, or GPU activity was caused exclusively by the
  request; or
- exact per-request hardware consumption.

Those conclusions require separate evidence and must remain unavailable or be
labelled configured, correlated, sampled, or estimated as appropriate.

## Bounds, compatibility, and failure behavior

The context owns one fixed-size identifier and one fixed-size claim object. It
does no I/O, enumeration, persistence, serialization, or logging. Entry and
claim are constant work. UUID generation failure propagates from context entry
before any reservation is installed. Restoration happens in `finally` for
ordinary exceptions and cancellation.

This is an additive public API. Existing `set_context(trace_id=...)` and
`trace_context(trace_id)` continue to govern manual-span links only and never
override provider Trace identity. Existing provider calls outside
`model_call_context()` are byte-for-byte and behaviorally unchanged. There is
no database migration, event/Trace schema change, ReadPort shape change,
network route, configuration field, runtime dependency, gateway dependency,
or header injection.

V1 does not expose caller-selected IDs, reservation inspection, multi-call
reservations, cross-process propagation, gateway profiles, or a promise that a
reserved call is persisted. Any of those requires a new contract rather than
silently extending this one.

## Options considered

### Reuse `trace_context`

Rejected. That API links manual spans to an existing Trace and accepts a
caller-provided ID. Making it also assign the next provider Trace would change
established semantics, permit accidental reuse, and mix two directions of
correlation.

### Let the private package read or patch Verdict internals

Rejected. Importing context variables, instrumentor builders, or storage
tables would recreate the upgrade coupling that the ReadPort removed.

### Have Verdict inject gateway headers

Rejected. Header names, provider SDK request shapes, gateway credentials, and
retry behavior belong to private gateway profiles. Putting them in Verdict
would make an open-source capture package depend on vendor policy.

### Infer joins from model name and timestamps

Rejected. Shared models, retries, queues, concurrency, and clock skew make the
result ambiguous. It cannot support an exact customer claim.

## Verification before merge

The implementation is not complete until all of the following pass on the
exact candidate:

1. Public API tests prove the identifier format, one claim, unused cleanup,
   exception/cancellation restoration, nesting, copied-context concurrency,
   `clear_context`, and separation from `trace_context`.
2. Every supported provider's sync, async, streaming, error, and cancellation
   construction/finalization paths preserve the reserved Trace ID when a Trace
   is actually built, and leave later calls independent.
3. Storage and V1 ReadPort tests prove the value reaches the exact model-call
   `trace_id` without adding any DTO field or schema migration.
4. A real installed OpenAI SDK calls the pinned real LiteLLM proxy with the
   yielded value in LiteLLM's supported `x-litellm-trace-id` header. A
   database-backed LiteLLM spend-log read returns that exact value as its
   `session_id`, while Verdict's real capture/read path returns it as the
   model-call Trace ID. The provider behind LiteLLM may be the repository's
   deterministic local provider so the test spends no external model budget.
5. Adversarial tests cover no instrumentation, unsampled success, provider
   failure before gateway evidence, two calls, concurrent copied contexts,
   lazy stream entry outside the context, and a persistence failure. They must
   make absence explicit rather than synthesize a join.
6. Mutations that remove atomic claiming, permit a second claim, skip provider
   Trace assignment, leak the reservation after exit, or couple the provider
   Trace to `trace_context` fail the relevant last-sink tests.
7. The full Verdict gate, cold built-wheel install, documentation search,
   independent architecture/security review, and hosted CI pass for the exact
   immutable candidate.

## Consequences

The public change is deliberately smaller than a gateway integration: it
provides a safe pre-call identity seam and nothing else. Private packages can
compose the V1 ReadPort with independently versioned gateway and metric
adapters without reading Verdict tables. The cost is one additional explicit
context around calls that require exact external correlation. Calls that do
not use it keep today's behavior, and missing evidence remains visible rather
than guessed.
