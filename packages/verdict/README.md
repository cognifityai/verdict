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
trailing text that is not part of the validated address remains outside it
while clock values such as `12:34:56` remain intact. Email candidates use a
linear `@`-anchored scanner so malformed or very long input cannot trigger
regex backtracking. Unsupported objects fail closed.
Traversal is bounded by node and character budgets, and cycles or repeated
container references fail closed at every occurrence so sanitized output never
retains caller-owned aliases. Redacted mapping-key collisions keep every value
under deterministic suffixed keys rather than overwriting one entry.
This is best-effort matching, not a compliance guarantee; keep content capture
off when its documented coverage is insufficient.

For a customer proof of concept, follow the versioned
[`0.1.0a9 POC release profile`](https://github.com/cognifityai/verdict/blob/v0.1.0a9/docs/POC_RELEASE_PROFILE.md).
It pins the package set, provider entry points, persistence mode, and privacy
boundary used for release verification.

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

Provider SDK unset sentinels and other non-primitive numeric metadata are
normalized to unavailable (`None`) before a `Trace` reaches storage. A
synchronous telemetry persistence failure never replaces the provider call's
result or exception; Verdict emits one warning per provider, storage type, and
exception type instead of flooding application logs.

`sample_rate` controls the fraction of supported calls retained, and
`buffered_writes=True` moves persistence to a background batched writer. Stored
manual spans do not wait for provider acknowledgement or receive repair writes;
each ended span is persisted once independently of provider success. `flush()` is
a FIFO point-in-time barrier and accepts an optional timeout. `close()` rejects
new reads/writes, drains every accepted FIFO write, stops and joins the worker,
then closes the inner adapter; post-close `flush()` is an idempotent no-op.
The `0.1.0a9` POC profile uses `buffered_writes=False`. Buffered mode requires
an explicit `shutdown()` imported from `verdict.client` before process exit.
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

Hosts that need agent-versus-evaluator cost provenance can bind a bounded,
task-local workload label:

```python
with verdict.workload_context("agent"):
    response = client.messages.create(...)
```

The packaged dashboard recognizes `agent` and `judge`; missing and custom labels
remain visible as unclassified rather than being guessed. The SDK also exposes
aggregate process-local capture/queue telemetry through
`VerdictClient.runtime_metrics.snapshot(client.storage)`. It contains counts and
latency summaries only, never prompts, responses, or exception text.

```python
import verdict
from anthropic import Anthropic

verdict.init(service_name="my-app", storage="sqlite:///./verdict.db")
client = Anthropic()
# Use Anthropic normally — supported SDK calls are captured.
```

Install and run the version-matched dashboard without a source checkout:

```bash
python -m pip install "cognifity-verdict[dashboard]==0.1.0a9"
verdict-dashboard --storage sqlite:///./verdict.db
```

Add the `postgres` extra for a PostgreSQL store. Verdict requires PostgreSQL
databases to use UTF-8 encoding. Legacy SQL_ASCII databases are not supported.
The dashboard is read-only and can also be mounted with
`verdict.dashboard.create_app()` behind an existing
FastAPI application's authentication. Trace Explorer shows the 30 newest traces
with complete store totals and provider/content-state filters. A `Historical
metadata-only trace` means content was not captured when that specific trace was
recorded; it does not report the application's current capture setting. Drift
and Judge empty states show global content-bearing trace availability over the
default 24-hour current and 7-day baseline windows. Meeting both displayed
totals does not establish statistical readiness: the pipeline still checks each
eligible cluster and rubric dimension for enough judged traces, and job flags
may use different windows or sample floors. The dashboard distinguishes a run
that has not completed from a completed run with zero signals.

Upgrade an existing synchronized `0.1.0a5` through `0.1.0a8` environment with
`python -m pip install --upgrade`
and the same provider, dashboard, semantic, and storage extras already in use.
The published wheels replace editable installs without a new clone and reuse the
selected SQLite file or PostgreSQL tables in place. See the repository
[upgrade instructions](https://github.com/cognifityai/verdict#upgrade-from-an-earlier-synchronized-alpha)
for the synchronized three-package command and verification steps.

An authenticated host may add the dashboard's Operations tab by passing a
same-origin API path:

```python
app.mount(
    "/admin/verdict",
    create_app(storage=storage_url, operations_url="/api/admin/operations"),
)
```

Verdict renders the normalized metrics/jobs response, while the host remains
responsible for cloud credentials, authorization, CSRF protection, collection,
and job execution. Without `operations_url`, no Operations tab or extra request
is present.

The dashboard's Registry tab is a bounded, read-only view of the Task 5
tenant/version registry. It shows active and preview versions, stable display
names, frozen algorithm/selector/model configuration, representative redacted
prompts, bounded provider/model distributions, membership explanations,
terminal outlier/ineligible reasons, coverage, and validation readiness. Its
per-cluster independent-conversation counts accept only strict-UTF-8, nonempty,
NUL-free session IDs of at most 256 bytes. Their time-to-readiness value is a
diagnostic estimate at the documented default windows/floor, not activation
or drift decisions; fragmentation/dominant-cluster warnings likewise require
operator inspection. The full 250-cluster list remains visible while nested
evidence is limited to the 20 highest-volume clusters. Standalone use selects the reserved local scope by
default and may select an explicit tenant with `?tenant=`. A mounted host can
instead set `request.state.verdict_registry_tenant`; that authorization-owned
value wins over query input. Mutation buttons appear only with the same-origin
Operations adapter. Semantic and hybrid fallback retain their experimental
disclosure.

For published release `0.1.0a9`, the bounded POC entry points include Anthropic
`messages.create(...)` (including `stream=True`), OpenAI
`chat.completions.create(...)` and its stream helper, and Google
`models.generate_content(...)` / `generate_content_stream(...)`, plus the
Anthropic `messages.stream(...)` helper's synchronous and asynchronous accessors
and OpenAI `responses.create(...)`, `responses.parse(...)`, and
`responses.stream(...)` for new or existing responses. OpenAI's
`responses.with_streaming_response` raw-response manager and the separate
experimental `client.beta.responses` multi-agent resource are not instrumented.

See the
[repository README](https://github.com/cognifityai/verdict#readme) for the full
picture, the [architecture decisions](https://github.com/cognifityai/verdict/tree/main/docs/adrs),
and the [examples](https://github.com/cognifityai/verdict/tree/main/examples).

Apache 2.0.
