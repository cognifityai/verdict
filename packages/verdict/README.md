# Verdict Python SDK

PyPI distribution: `cognifity-verdict`. Python import: `verdict`.

Install and start the loopback-only product UI:

```bash
python -m pip install cognifity-verdict
verdict
```

The initial setup page can approve and rescan local Claude Code/Codex histories,
import supported telemetry files, show the SDK snippet, or open an existing
store. Local histories are persisted as typed `AgentRun`/`AgentTurn`/
`AgentEvent` evidence; they are not converted into fake provider LLM traces.
Those records use normalized source/run/turn/event storage. A model-call event
links to the genuine `Trace`, which remains the only owner of LLM content and
the complete provider-call record used for evaluation and drift. Events may
retain bounded scalar summaries but never copy prompt, response, or raw-message
content. Run timelines are read in bounded pages instead of from one growing
serialized row.
Bounded redacted content retention is on by default; metadata-only capture is
an explicit SDK/programmatic override, not a shortcut in local setup. After capture, the same page becomes
**Data sources**, reports the evidence sources observed in the current store, and requires
fresh in-process path approval for manual edits or rescans. Agent Run and LLM
Trace totals remain visibly separate. The server requires a successful preview
of the exact local or historical paths before it accepts the corresponding
write. An explicitly saved daily schedule intentionally retains source paths in
the local control store for `verdict-service`; no OS scheduler is installed.
Claude Code history fragments that share one provider response identity are
coalesced into one linked Trace; later response text completes that Trace while
genuine tool-only calls remain textless.
Source-identified Claude sidechains and Codex child histories are stored as
separate child runs. Agent Run views show bounded final-response previews and
source-reported per-turn tokens when available. Text is redacted before the
preview cutoff, and incomplete token components remain explicitly partial;
Claude totals appear only after terminal, complete response usage. Those source
activity totals are not a quality ranking: latency, price, judging, and model
comparisons still use genuine `Trace` records only.

For automation, the equivalent commands are:

```bash
verdict-import local --storage sqlite:///./verdict.db
verdict-dashboard --storage sqlite:///./verdict.db
verdict-monitor run --storage sqlite:///./verdict.db
verdict-service --storage sqlite:///./verdict.db --once
```

Instrumented applications can add the execution structure surrounding those
LLM calls without creating a second content record:

```python
import verdict

verdict.init(storage="sqlite:///./verdict.db", service_name="support-api")
with verdict.agent_run(name="support-agent", session_id=session_id) as run:
    with run.turn(user_input=user_message) as turn:
        with turn.tool("lookup_order", arguments={"order_id": order_id}) as tool:
            order = lookup_order(order_id)
            tool.set_output({"found": order is not None})
        answer = respond(user_message, order)
        turn.set_output(answer)
    run.record_business_outcome("resolved", True)
```

Supported provider calls inside the turn automatically create linked genuine
`Trace` records. The SDK also exposes typed helpers for instructions, context,
commands, tests, artifacts, retries, handoffs, feedback, and outcomes. Sync and
async context managers share the same API contract. With content capture on, a
turn whose caller does not provide an output records missing response evidence;
metadata-only capture records that content as not captured.

To keep application processes off the database, select the bounded local file
transport and later import its process-owned JSONL segments through canonical
storage:

```python
verdict.init(transport="file", spool_directory="./verdict-capture")
```

```bash
verdict-import agent-file ./verdict-capture --storage sqlite:///./verdict.db
```

The same file transport carries provider Traces, Agent evidence, manual spans,
and user signals. Use one spool directory per producer process. Manual import
reads complete records from active, sealed, or rejected segments. For central
PostgreSQL Agent ingestion, `verdict-collector` provides authenticated bounded
batches and durable idempotent acknowledgements, while `verdict-shipper`
uploads complete segment prefixes and deletes only sealed, fully accepted
segments. Collector-rejected segments remain locally recoverable. Standalone
Trace, Span, and UserSignal records still require direct or manual file import.
Use `verdict-shipper --spool-directory ./verdict-capture --status --json` to
inspect local backlog without the collector or its key. Completed appends bypass
Python userspace buffering but are not `fsync`-ed. If an Agent evidence stream
fails, later provider calls fall back to standalone Trace records. Quota or
write failures increment the process-local `capture.dropped_records` metric and
produce a bounded warning.

The Monitor UI previews an immutable count-based (older 80% / newer 20% by
default) or explicit-date policy before activation. Each metric has its own
eligible denominator, Fisher's exact p-value, Benjamini-Hochberg adjustment,
and effect-size gate. With provider/model or reviewed-cluster grouping, Verdict
computes separate group-by-metric comparisons and adjusts across the complete
tested family; it does not pool the selected groups. The Measurement selector
can add stored PASS/FAIL results from one complete evaluator identity without
running or paying for a judge. That evaluator fingerprint and its expected
dimensions become immutable policy
inputs. A reviewed-cluster policy also pins its registry version and completes
projection of eligible new traces through that fixed version before saving
monitor membership, without refitting it. Unfinished bounded projection is
reported as `projection_pending` and writes no monitor snapshot. UNCLEAR,
missing, and error states remain outside the PASS/FAIL denominator and are
shown as coverage. Unassigned and new groups do not wait on judgments they
cannot use in like-for-like tests. Ongoing cohorts are prospective and
non-overlapping; late arrivals are counted and included in the next open cohort
rather than silently discarded. An evaluator-backed cohort fixes membership
and waits for stored results needed by its tested metric cells before
comparing; changed or deleted pending evidence requires a new reviewed preview.
An activated policy starts with an empty prospective bucket, and repeated looks
use a summable quadratic alpha-spending rule.
`insufficient` and `reference_stale` are first-class results; unassigned or new
groups are reported rather than pooled into a comparison. `verdict-monitor` is
a one-shot idempotent runner. It and `verdict-service` use the same stored
evaluator, dimensions, grouping version, and trace selection as the dashboard.
`verdict-service` executes the dashboard's saved schedule once or continuously.
The approved baseline membership and normalized metric counts are immutable.
Grouped monitors are limited to 250 distinct groups. Older stored monitors
without frozen cohort facts, or without evaluator-finalization state when an
evaluator is selected, require a new reviewed preview before execution.

The dashboard reads key-free findings from immutable analysis snapshots rather
than recomputing them on every page load. It reports provider outcome,
evaluation status, finding severity, and drift comparison independently.
`not evaluated` and `judge error` are explicit Trace states. A prospective
monitor distinguishes traffic collection from a full cohort awaiting selected
evaluator results.
The dashboard has five top-level workspaces: Overview, Explore, Evaluate,
Monitor, and Settings. Monitor is the current drift workflow: it shows reviewed
historical comparisons, the active prospective policy, and optional facets.
Results created by older fixed-window pipeline releases remain available under
**Monitor → Legacy History** but are read-only and do not affect current status.

The Verdict Python SDK. Auto-instruments your LLM calls via `wrapt` and
captures them into a vendor-neutral `Trace` schema (attribute *names* follow
the OpenTelemetry GenAI semantic conventions, but no OTel spans are emitted).
The same package also imports existing OTLP and vendor telemetry into that
unchanged schema with the `verdict-import` command.
Traces are written to SQLite by default (or any `Storage` adapter). Content
capture (prompts/completions) is **on by default** and can be disabled with
`capture_content=False`; captured content is run through built-in
pattern redaction, including common provider/API credentials, recursively
across supported JSON-compatible message and tool
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
This is best-effort matching, not a compliance guarantee; explicitly disable
content capture when its documented coverage is insufficient.

Import existing telemetry without instrumenting the application:

```bash
pip install "cognifity-verdict[telemetry]==0.1.0a17"  # extra is for OTLP protobuf
verdict-import file traces.jsonl --format auto --storage sqlite:///./verdict.db
verdict-import receive-otlp --storage sqlite:///./verdict.db
```

Native readers cover Langfuse v2, LangSmith, Datadog LLM Observability,
Phoenix, Opik, MLflow files, and a text-only voice-conversation schema. The
importer stores every eligible LLM call; the existing evaluation pipeline later
samples stored traces for judging. It never stores a second raw vendor envelope
or imputes missing token, latency, cost, model, session, or content fields. See
the repository's `examples/telemetry/README.md` for exact source contracts and
privacy limits; ADR-006 records only the architectural boundary.

OTLP message objects may provide text in `content`, `text`, or typed text
`parts`. Verdict joins genuine text parts in order and ignores unsupported
tool-only parts rather than presenting them as an assistant response.

The Langfuse reader targets the supported v4 Observations API v2, not the
deprecated trace-list endpoint, so Verdict receives one record per actual
generation or embedding rather than a trace aggregate.

For a customer proof of concept, follow the versioned
[`0.1.0a17 POC release profile`](https://github.com/cognifityai/verdict/blob/v0.1.0a17/docs/POC_RELEASE_PROFILE.md).
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
The `0.1.0a17` POC profile uses `buffered_writes=False`. Buffered mode requires
an explicit `shutdown()` imported from `verdict.client` before process exit.
Fixed-window `DriftRun` snapshots created by older releases remain readable for
compatibility; the current pipeline does not create or replace them.
`prune_before()`
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
latency summaries only, never prompts, responses, or exception text. The
capture counts include `dropped_records`, which increases when a provider Trace
or Agent SDK record cannot be retained.

```python
import verdict
from anthropic import Anthropic

verdict.init(service_name="my-app", storage="sqlite:///./verdict.db")
client = Anthropic()
# Use Anthropic normally — supported SDK calls are captured.
```

Install and run the version-matched dashboard without a source checkout:

```bash
python -m pip install "cognifity-verdict[dashboard]==0.1.0a17"
verdict-dashboard --storage sqlite:///./verdict.db
```

Add the `postgres` extra for a PostgreSQL store. Verdict requires PostgreSQL
databases to use UTF-8 encoding. Legacy SQL_ASCII databases are not supported.
Dashboard analytics are read-only; the setup/import and Monitor controls are
explicit storage mutations. The app can also be mounted with
`verdict.dashboard.create_app()` behind an existing
FastAPI application's authentication. Trace Explorer pages through every
non-judge application trace in deterministic 30-row pages with complete store
totals. Provider/content-state filters apply to the current page. Judge
telemetry remains in aggregate cost and store totals but does not displace
application traces from this view. A `Historical
metadata-only trace` means content was not captured when that specific trace was
recorded; it does not report the application's current capture setting. Monitor
previews show eligible evidence for the selected count-based or explicit
event-time cohorts. Fixed-window results produced by older releases remain
available only as read-only legacy history.

Upgrade an existing synchronized `0.1.0a5` through `0.1.0a16` environment with
`python -m pip install --upgrade`
and the same provider, dashboard, semantic, and storage extras already in use.
The published wheels replace editable installs without a new clone and reuse the
selected SQLite file or PostgreSQL tables in place. See the repository
[upgrade instructions](https://github.com/cognifityai/verdict#upgrade-from-an-earlier-synchronized-alpha)
for the synchronized three-package command and verification steps.
All writers sharing a store must be stopped and upgraded together when the store
first moves to normalized agent evidence; migrated stores reject legacy bundle
writes.

An authenticated host may add infrastructure and job evidence under
**Settings → Integrations** by passing a same-origin API path:

```python
app.mount(
    "/admin/verdict",
    create_app(storage=storage_url, operations_url="/api/admin/operations"),
)
```

Verdict renders the normalized metrics/jobs response, while the host remains
responsible for cloud credentials, authorization, CSRF protection, collection,
and job execution. Without `operations_url`, no operations panel or extra
request is present.

The dashboard's **Monitor → Segments** workspace is a bounded view of the Task 5
tenant/version registry. It shows active and preview versions, stable display
names, frozen algorithm/selector/model configuration, representative redacted
prompts, bounded provider/model distributions, membership explanations,
terminal outlier/ineligible reasons, coverage, and validation readiness. Its
per-cluster independent-conversation counts accept only strict-UTF-8, nonempty,
NUL-free session IDs of at most 256 bytes. Their time-to-readiness value is a
diagnostic estimate at the documented default windows/floor, not activation
or drift decisions; fragmentation/dominant-cluster warnings likewise require
operator inspection. The full 250-cluster list remains visible while nested
evidence is limited to the 20 highest-volume clusters. Standalone use selects the reserved local scope and
uses its same-origin setup capability for mutations. A mounted host can
instead set `request.state.verdict_registry_tenant`; that authorization-owned
value wins over query input. Mounted mutation buttons use the same-origin
Operations adapter. Semantic and hybrid fallback retain their experimental
disclosure. When a mounted host supplies that authorized tenant, Overview,
Trace Explorer, cluster pass-rate charts, and drift rows project assignments
and stable labels from the same active registry. Standalone and legacy stores
without an authorized active registry continue to use `Trace.cluster_id`.

For published release `0.1.0a17`, the bounded POC entry points include Anthropic
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
