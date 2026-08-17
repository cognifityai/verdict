# Verdict Python SDK

PyPI distribution: `cognifity-verdict`. Python import: `verdict`.

The Verdict Python SDK. Auto-instruments your LLM calls via `wrapt` and
captures them into a vendor-neutral `Trace` schema (attribute *names* follow
the OpenTelemetry GenAI semantic conventions, but no OTel spans are emitted).
Traces are written to SQLite by default (or any `Storage` adapter). Content
capture (prompts/completions) is **off by default** — opt in with
`capture_content=True`; when enabled, captured content is run through built-in
pattern redaction recursively across supported JSON-compatible message and tool
structures before `Trace` assignment and again at storage. Card candidates use
Luhn validation; IPv6 candidates use standard-library address validation so
clock values such as `12:34:56` remain intact, and email candidates use a linear
`@`-anchored scanner so malformed or very long input cannot trigger regex
backtracking. Unsupported objects fail closed.
Traversal is bounded by node and character budgets, and cycles or repeated
container references fail closed at every occurrence so sanitized output never
retains caller-owned aliases. Redacted mapping-key collisions keep every value
under deterministic suffixed keys rather than overwriting one entry.
This is best-effort matching, not a compliance guarantee; keep content capture
off when its documented coverage is insufficient.

Supported streams finalize after full consumption, iteration error, explicit
`close()` / `aclose()`, context exit, or async cancellation. Garbage collection
of a never-iterated unclosed stream is not a persistence guarantee. A supported
instrumented provider call made inside a manual span records the innermost
span's ID in `Trace.parent_span_id`. This is the sole automatic direction because
multiple provider traces may share one manual span; automatic capture never
chooses one reverse `SpanRecord.trace_id`. Manual-only work can bind to an
existing stored trace with
`trace_context(trace_id)` or `set_context(trace_id=...)`. An unknown explicit
trace ID is recorded as an unlinked span with a link-status attribute rather
than as an orphan; spans with no provider call or explicit context remain
standalone.

`sample_rate` controls the fraction of supported calls retained, and
`buffered_writes=True` moves persistence to a background batched writer. Stored
manual spans do not wait for provider acknowledgement or receive repair writes;
each ended span is persisted once independently of provider success. `flush()` is
a FIFO point-in-time barrier and accepts an optional timeout. `close()` rejects
new reads/writes, drains every accepted FIFO write, stops and joins the worker,
then closes the inner adapter; post-close `flush()` is an idempotent no-op.
Completed drift analyses use atomic `DriftRun` snapshots, including explicit
zero-signal runs. Storage readers select a run marker and its exact signals from
one snapshot; deleting a matched attributed signal window removes the completed
run as a unit. `prune_before()`
removes expired standalone and orphan span rows while preserving an old span
referenced by a retained Trace. SQLite and PostgreSQL execute multi-table trace
deletion and pruning atomically and serialize concurrent trace writers while
they decide which shared parent spans must survive.
Stored costs are best-effort estimates from Verdict's dated static base-price table;
unknown models remain unpriced, and the values are not billing truth.

```python
import verdict
from anthropic import Anthropic

verdict.init(service_name="my-app", storage="sqlite:///./verdict.db")
client = Anthropic()
# Use Anthropic normally — supported SDK calls are captured.
```

See the
[repository README](https://github.com/cognifityai/verdict#readme) for the full
picture, the [architecture decisions](https://github.com/cognifityai/verdict/tree/main/docs/adrs),
and the [examples](https://github.com/cognifityai/verdict/tree/main/examples).

Apache 2.0.
