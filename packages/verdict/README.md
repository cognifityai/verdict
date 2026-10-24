# Verdict Python SDK

PyPI distribution: `cognifity-verdict`. Python import: `verdict`.

Verdict connects application-agent runs, turns, tools, tests, and explicit
outcomes to supported provider model calls. Use the local dashboard to inspect
recorded failures and usage, evaluate bounded evidence, compare a reviewed
change on fixed cases, and monitor ongoing behavior. Prospective statistical
alerts currently apply to genuine model-call traces; scheduled analysis can
also persist deterministic findings from captured agent activity.

Install the current release and start its loopback-only UI:

```bash
python -m pip install "cognifity-verdict[dashboard]"
verdict
```

The published beta includes guided Home, worker status, and optional selected-evidence explanations. The dashboard opens a local setup page when it has no connected evidence store.

The initial setup page can connect an existing application store, approve and
rescan local Claude Code/Codex histories,
import supported telemetry files, show the SDK snippet, or open an existing
store. Local histories are persisted as typed `AgentRun`/`AgentTurn`/
`AgentEvent` evidence; they are not converted into fake provider LLM traces.
Those records use normalized source/run/turn/event storage. A model-call event
links to the genuine `Trace`, which remains the only owner of LLM content and
the complete provider-call record used for evaluation and drift. Events may
retain bounded scalar summaries but never copy prompt, response, or raw-message
content. Run timelines are read in bounded pages instead of from one growing
serialized row.
When a local history reaches 1,500 source events, the canonical run retains
its bounded prefix and a capture-limit marker. Investigation can also show a
separate, dated window of up to 100 recent turns and 50 redacted events;
the marker starts at the first Turn with an omitted event, so earlier fully
captured Turns remain eligible for judging. The recent window indexes up to
50 selected events. Bounded, fully parsed files within the local full-capture
budget also retain other non-prefix events and tail turns. Investigation's
**Browse recorded events** pages those events after checking the committed
revision, turn ownership, and exact event count; this proves recorded-event
coverage for that captured revision, not task success or source-absent actions.
**Analyze recorded run** applies deterministic run checks to those retained
typed events and turns after the same proof and links findings to an exact
recorded-event page. It shows one evidence-linked suggested next step when an
existing deterministic rule supports one; a suggestion is not a verified fix.
The on-demand Turn analysis API can inspect a retained tail Turn after this
proof, within its event limit. A saved Insights analysis also includes verified
retained turns and events for admitted sources within a 5,000 additional-row
scan budget; sources beyond that analysis budget stay labeled partial. The
ordinary run detail remains a bounded canonical-prefix view. Day
recorded-issue totals can use all retained typed events after
the same proof and bounded scan. Files above the full-capture admission budget
that remain within the parser limits retain selected recent facts only.
Explicit recorded failures can create durable issue notices
after monitoring is activated, and linked model calls retain their Trace for
supported call-level evaluation. The partial bounded view supplies no whole-run
issue or no-alert denominator and does not make an incomplete Turn eligible for
Turn judging. A window whose source revision no longer
matches the committed import is withheld until a successful refresh.
Investigation provides first-seen-revision exact links for retained recent
events, including after the window moves. The saved window keeps event references;
Investigation loads their retained details in one read snapshot. An older exact
link to an event still in the window opens it there. Rescans reverify retained facts and
linked Traces before accepting a new source revision. Insights labels observed
historical-prefix findings and excludes unverified or analysis-budget-limited
histories from whole-run outcome counts. Selecting an Insights issue beyond the
quick-view prefix opens the deterministic recorded-run report and its exact
retained event when available; this navigation makes no AI judge call. The
recorded report appears first, while bounded prefix findings stay available
under **Historical prefix details** with prefix-labeled counts. A report's
evidence action opens the selected recorded event in view.
Local imports above 1,000 turns retain a bounded historical prefix and an
atomic recent window. The V1 whole-run read rejects that partial prefix;
earlier fully captured turns remain selectable. A live file with an unfinished final JSONL line retains its
complete records, while source coverage and optional AI previews mark the
affected run/last turn partial. A later completed append clears that transient
coverage. Agent Turn judge previews and writes exclude the affected last turn;
any earlier score is hidden while its source is partial. Earlier complete turns
remain eligible. A fresh saved analysis incorporates the source revision.
Conflicting or truncated rescans leave the earlier canonical run
intact and report `local_source_conflict`.
Bounded redacted content capture is on by default; metadata-only capture is
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
activity totals are not a quality ranking: provider-call latency and statistical
model-call monitoring still use genuine `Trace` records only. Agent Turn usage
can receive a static API list-price estimate when one supported model and valid
counts are attributable; it is not a Claude/Codex subscription bill. A separate
source-declared paired-conversation comparison is described below.

The dashboard's Agent Profiler flow charts reported token use by UTC day,
source, model, session, and turn, then opens a combined session report and links its runs to deeper timelines.
Selected Turn analysis and evidence-linked suggested actions are key-free.
Optional selected-scope session/run/tool evaluation previews exact bounded redacted
evidence and requires approval before external judging. Reviewed cases can be
run against two explicit local application versions with fixed checks; an
opt-in service or manual run-cycle can analyze already captured activity and consume a separately approved, one-attempt external evaluation. The
[Agent Profiler guide](../../docs/AGENT_PROFILER.md) gives the runnable demo,
configuration, evidence boundaries, and limitations.

Codex Turn requests include text from completed `UserMessage` items and the
older `user_message` format. Rescanning can fill previously missing request
text; non-text-only messages remain missing evidence.

For automation, the equivalent commands are:

```bash
verdict-import local --storage sqlite:///./verdict.db
verdict-dashboard --storage sqlite:///./verdict.db
verdict-monitor run --storage sqlite:///./verdict.db
verdict-service --storage sqlite:///./verdict.db --once
```

For a configured single-tenant dashboard, give `verdict-service` the same
`--tenant-id`; otherwise both commands use `__verdict_local__`.

Instrumented applications can add the execution structure surrounding those
LLM calls without creating a second content record:

```python
import verdict

verdict.init(storage="sqlite:///./verdict.db", service_name="support-api")
with verdict.agent_run(name="support-agent", session_id=session_id) as run:
    with run.turn(user_input=user_message) as turn:
        with turn.tool(
            "lookup_order",
            arguments={"order_id": order_id},
            origin=verdict.ToolOrigin.APPLICATION,
        ) as tool:
            order = lookup_order(order_id)
            tool.set_output({"found": order is not None})
        answer = respond(user_message, order)
        turn.set_output(answer)
    run.record_business_outcome("resolved", True)
```

An explicit boolean `False` business outcome is retained as a failed outcome
event and surfaced as a deterministic Agent Insight, including when content
capture is disabled. Other values remain descriptive; Verdict does not infer
application-specific success semantics.

Supported provider calls inside the turn automatically create linked genuine
`Trace` records. The SDK also exposes typed helpers for instructions, context,
commands, tests, artifacts, retries, handoffs, feedback, and outcomes. Sync and
async context managers share the same API contract. With content capture on, a
turn whose caller does not provide an output records missing response evidence;
metadata-only capture records that content as not captured.
For a command or test that may be rerun, pass the same non-secret opaque
`check_id` to `turn.record_command(...)` or `turn.record_test(...)` on each
attempt. Verdict stores a SHA-256 digest of the exact ID bytes, even with
content capture disabled. When an operator attaches a later passing event to
a failure notice, Monitoring identifies a matching application-supplied check
ID. This identifies the reported check, not the same inputs, task, or a
recovered application. IDs are case-sensitive, limited to 256 UTF-8 bytes,
and should not contain secrets or personal data: an unsalted digest of a
guessable value is not confidential.
`ToolOrigin` records only an explicitly observed outer dispatch boundary:
`MCP`, `PROVIDER_HOSTED`, or `APPLICATION`. Omit it when unknown. Verdict never
infers origin from a tool name, and the label cannot prove which protocols a
wrapper uses downstream.
With content capture enabled, tool arguments and results are recursively
redacted and bounded before storage. Values that exceed an event's depth, node,
or byte limits may contain omission markers. If the final event cannot fit,
Verdict stores the call or result event with its typed metadata and an explicit
`omission_reason`; it omits the content field. A tool exception or cancellation
still records a terminal result status and does not suppress the application
exception. If the storage sink itself fails, capture remains fail open: the
process-local dropped-record counter and a bounded warning identify the
failure class, but the rejected event is not durable.
An explicit `call_id` must be valid UTF-8 text without NUL bytes; invalid
values are rejected before a tool call starts. Long IDs retain the published
behavior of a bounded, redacted stored value; IDs beyond the redaction input
budget can share a redaction marker. An empty ID generates an ID.

## Stable read port for optional packages

Trusted same-process extensions can read one authorized Agent Run without
depending on Verdict tables or dashboard queries:

```python
from verdict.read_port import StorageVerdictReadPort
from verdict.storage import SQLiteStorage

storage = SQLiteStorage("./verdict.db")
port = StorageVerdictReadPort(storage)

# The host must authorize tenant_id before making this call.
run = port.get_agent_run(tenant_id=authorized_tenant_id, run_id=selected_run_id)
```

The immutable V1 result contains run timing/status, at most 16 exact model-call
event/Trace references, and at most four deterministic findings with explicit
totals and truncation flags. It excludes prompts, responses, provider/model
display names, arbitrary event attributes, and analysis prose. Returned IDs
remain protected Verdict metadata.

This is a Python composition boundary, not an HTTP service or authorization
layer. `tenant_id` scopes the lookup but does not prove the caller may read that
tenant. Runs above 1,000 turns or 1,500 events fail closed after the exact store
lookup. The port cannot identify a LiteLLM deployment or GPU; a dependent
package needs a separate propagated identity source and must otherwise report
the correlation as unmapped. See
[`ADR-013`](../../docs/adrs/013-stable-dependent-package-read-port.md).

### Bounded fleet metadata for trusted integrations

PostgreSQL deployments can explicitly enable a separate metadata-only fleet
reader for a trusted same-process package. It reads at most 2,000 traces from
one authorized tenant and one half-open window of at most 15 minutes. The DTO
contains timing, status, model/routing names, token/latency/cost scalars, and an
exact Agent Run link when one exists. Prompt, response, raw message, error,
session, user, tags, and arbitrary agent attributes never cross this boundary.

The database owner must opt in. Precreate a dedicated LOGIN reader with no role
memberships or direct Verdict-relation privileges, grant it `USAGE` but not
`CREATE` on the configured Verdict schema, set it read-only, and then run:

```bash
VERDICT_DATABASE_URL="$VERDICT_OWNER_DATABASE_URL" \
  python -m verdict.fleet_read_port prepare \
  --reader-role verdict_operations_reader --tenant-id customer-a
```

The private integration uses its dedicated reader URL:

```python
from verdict.fleet_read_port import PostgresVerdictFleetReadPortV2

fleet = PostgresVerdictFleetReadPortV2(
    verdict_reader_url,
    tenant_id=authorized_tenant_id,
)
window = fleet.read_trace_window(
    tenant_id=authorized_tenant_id,
    window_start=window_start,
    window_end=window_end,
)
fleet.close()
```

V2 reports `error_recorded`, `in_progress`, or
`ended_no_error_recorded`; none asserts successful response or task outcome.
The published V1 DTO remains importable, but its live reader now returns
`unsupported_version` for a window containing ended calls with no recorded
error or error records without an end time. Select V2 for mixed real traffic.
Both versions use the same bounded views and role grants.
The reader projects bounded text before fetching rows; overlong source fields
fail closed instead of transferring unbounded content to the reader process.

Disable only that role/tenant mapping with the same command using `disable` in
place of `prepare`. The shared views and index remain inert for another mapped
reader or a later re-enable. SQLite and ordinary Verdict startup do not install
or scan this fleet boundary. See
[`ADR-015`](../../docs/adrs/015-bounded-fleet-read-and-operations-links.md).

To keep application processes off the database, select the bounded local file
transport and later import its process-owned JSONL segments through canonical
storage:

```python
verdict.init(transport="file", spool_directory="./verdict-capture")
```

```bash
verdict-import agent-file ./verdict-capture --storage sqlite:///./verdict.db
```

The same file transport carries provider Traces, Agent evidence, and manual
spans. Use one spool directory per producer process. Manual import
reads complete records from active, sealed, or rejected segments. For central
PostgreSQL capture, `verdict-collector` provides authenticated bounded
batches and durable idempotent acknowledgements, while `verdict-shipper`
uploads complete segment prefixes and deletes only sealed, fully accepted
segments. The collector in this source tree accepts Agent, standalone Trace,
and Span records; collector-rejected segments remain locally recoverable. A
previously completed rejection receipt is immutable, and the shipper skips
quarantined `.rejected` segments. Recover those records through reviewed manual
import or an explicit new-delivery procedure after upgrading the collector.
Use `verdict-shipper --spool-directory ./verdict-capture --status --json` to
inspect local backlog without the collector or its key. Completed appends bypass
Python userspace buffering but are not `fsync`-ed. If an Agent evidence stream
fails, later provider calls fall back to standalone Trace records. Quota or
write failures increment the process-local `capture.dropped_records` metric and
produce a bounded warning.

The dashboard's **Model drift setup** previews an immutable count-based (older 80% / newer 20% by
default) or explicit-date policy before activation. One genuine model-call
Trace is the only analysis unit supported by the persisted prospective alert
lifecycle. Each metric has its own
eligible denominator, Fisher's exact p-value, Benjamini-Hochberg adjustment,
and effect-size gate. With provider/model or reviewed-cluster grouping, Verdict
computes separate group-by-metric comparisons and adjusts across the complete
tested family; it does not pool the selected groups. The Measurement selector
defaults to deterministic trace checks and can explicitly add stored PASS/FAIL
results from one complete evaluator identity without
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
The continuous worker polls for plan revisions every 30 seconds, preserves a
worker-only next due time, and waits while the plan is paused. A transient
cycle failure records its outcome and cooldown while the worker keeps polling.
The one-shot worker rejects a saved plan changed or paused before its lock was
acquired, without starting capture or judge work. `--once` exits
nonzero when its recorded cycle fails. Pausing prevents later cycles but does
not cancel a cycle already selected by the worker.
The approved baseline membership and normalized metric counts are immutable.
Grouped monitors are limited to 250 distinct groups. Older stored monitors
without frozen cohort facts, without evaluator-finalization state when an
evaluator is selected, or naming a non-Trace analysis unit remain readable but
require a new reviewed preview before execution.

A separate **Logical session (descriptive)** selection groups native Agent
runs only by explicit `AgentRun.session_id`. It compares currently terminal
sessions retrospectively and reports deterministic rates, optional current
Agent Turn judgment PASS rates, effect sizes, and coverage. Because Verdict
does not have an authoritative session-finalization signal or an established
independence premise, this preview has no p-values or alert decision, is not
persisted, and cannot be activated. Missing identities are reported rather than
inferred from source sessions, Traces, tags, or process metadata.
The bounded preview fails closed above 1,000 runs or 10,000 Turns. Selecting a
Turn evaluator also caps redacted Turn text at 16 MiB and selected judgments at
10,000 / 16 MiB; counts-only tool evidence additionally caps scanned events at
100,000. The returned projection contains no Turn text or event bodies and
never silently samples an incomplete session. Evaluator discovery examines the
newest 1,000 stored Turn-result slots, exposes at most 100 identities, and marks
the response when older slots were not inspected.

The dashboard reads key-free findings from immutable analysis snapshots rather
than recomputing them on every page load. It reports recorded provider errors,
evaluation status, finding severity, and drift comparison independently.
`not evaluated` and `judge error` are explicit Trace states. A prospective
monitor distinguishes traffic collection from a full cohort awaiting selected
evaluator results.
The dashboard has six primary destinations: Home, Suggested
setup, Monitoring, Investigation, Groups, and Model drift setup. Groups includes
separate bounded, read-only previews of captured run execution statuses and
reported tool outcomes for terminal production runs. Missing run completion
and unclear tool coverage remain explicit; neither preview makes an alert claim.
Malformed or non-object result attributes remain unclear; an explicit failed
or timed-out status still appears as recorded failure.
Settings shows committed alert delivery, bounded worker retries, and an
explicit review before an old alert is sent to a changed destination. The
queue shows problem counts, puts problems first among loaded items, and pages
through older alerts (including older problems); the worker
attempts at most two sends per drain so a slow destination does not hold the
analysis lock for an entire backlog.
Select a UTC day in Home and use **Explain this day** for its
measured token contributors and a bounded recent-day comparison. An incomplete
final local-history line or an older local import without a verified source
checkpoint on the selected or a reference day withholds that comparison while
preserving recorded token sums. Red markers on the agent-turn chart show dates with deterministic recorded issue events; the selected date links example events to their exact run evidence. An explicitly timed-out tool, command, or test execution counts as a recorded issue. Event dates and turn-token dates use different recorded timestamps. An incomplete stored-event scan withholds its count; when local event history is incomplete or unverified, captured issue examples remain linked but their observed count is only a lower bound for the displayed day. An event-only cap does not itself block token comparisons when all Turns and token totals were captured; missing Turns can block days intersecting their verified parsed Turn interval, while an unverified source can affect the displayed window. **Ask AI why**
previews aggregate-only context and the destination before one approved call;
the answer does not establish waste or verified savings. Report and Settings
are available from the sidebar. Report summarizes application calls excluding
Verdict-owned judge and paired-replay work over the last 30 UTC calendar days
by default, with 7-day, 90-day, and all-time
choices. Its chart shows up to 31 active dates in the selected period, plus the
top 20 service/environment rows and top 20 models. Latency coverage uses every
known value in the period; p50/p95 use the newest 10,000 known latencies.
The report and Reliability analysis distinguish recorded provider errors,
calls that ended without a recorded error, and unfinished calls. An absent
error is not proof of a successful response. Saved analysis from an older
analyzer version needs a fresh local run before these counts are current.
Older beta report payloads remain readable, but their error count is shown as
unavailable because that format could miss an empty-string recorded error.
Configured `service_name` and `environment`
values are persisted on new instrumented traces; historical and default-client
traces without an explicit service identity appear as **Unattributed**. HTML,
CSV, and browser Print/Save PDF outputs use the selected period and contain
aggregates only. Monitor is the current drift workflow: it shows reviewed
historical comparisons, the active prospective policy, and optional facets.
Results created by older fixed-window pipeline releases have no tenant owner;
the dashboard excludes them from `/api/data` and the UI. They remain readable
through the Python storage API.

The Verdict Python SDK. Auto-instruments your LLM calls via `wrapt` and
captures them into a vendor-neutral `Trace` schema (attribute *names* follow
the OpenTelemetry GenAI semantic conventions, but no OTel spans are emitted).
The same package also imports existing OTLP and vendor telemetry into the same
vendor-neutral schema with the `verdict-import` command.
Traces are written to SQLite by default (or any `Storage` adapter). Content
capture (prompts/completions) is **on by default** and can be disabled with
`capture_content=False`; captured content is run through built-in
pattern and field-aware redaction, including common provider/API credentials,
GitHub tokens, unlabeled vendor tokens with a distinctive prefix (for example
Stripe, Hugging Face, GitLab, Slack, npm, and PyPI), and authorization headers, recursively across supported
JSON-compatible message and tool structures before content limits, `Trace`
assignment, and storage. Opaque values under supported credential field names
such as `password`, `api_key`, `token`, `secret_key`, `cookie`, and `passcode`,
including explicit plural containers such as `passwords` and `api_keys`, are
removed without relying on their text shape. Typed Agent instruction, context,
and outcome events also remove paired content when their semantic `name` is a
supported credential field. Complete `=` assignment values, quoted values,
single-token `:` assignment values, and embedded serialized forms are removed.
Multiword values after `:` must be quoted because unquoted multiword colon
clauses are not removed wholesale based only on their label; independently
recognized secrets are still redacted. Basic and Bearer credential padding is
included in the removed value. Existing Verdict
redaction/hash placeholders remain terminal across repeated storage passes.
Agent Run names and descriptive service, version, and environment fields cross
the same boundary while routing IDs remain unchanged.
Metadata-only capture retains
error categories but not provider or manual-span exception messages. Card candidates use
Luhn validation; IPv6 candidates use standard-library address validation so
trailing text that is not part of the validated address remains outside it
while clock values such as `12:34:56` remain intact. Email candidates use a
linear `@`-anchored scanner so malformed or very long input cannot trigger
regex backtracking. Unsupported objects fail closed.
Traversal is bounded by node, character, and recursive-key depth budgets, and
cycles or repeated
container references fail closed at every occurrence so sanitized output never
retains caller-owned aliases. Redacted mapping-key collisions keep every value
under deterministic suffixed keys rather than overwriting one entry.
This is best-effort matching, not a compliance guarantee; explicitly disable
content capture when its documented coverage is insufficient.

Import existing telemetry without instrumenting the application:

```bash
pip install "cognifity-verdict[telemetry]"  # extra is for OTLP protobuf
verdict-import file traces.jsonl --format auto --storage sqlite:///./verdict.db
verdict-import receive-otlp --storage sqlite:///./verdict.db
```

Native readers cover Langfuse v2, LangSmith, Datadog LLM Observability,
Phoenix, Opik, MLflow files, and a text-only voice-conversation schema. The
importer stores every eligible LLM call; the existing evaluation pipeline later
samples stored traces for judging. Voice import also stores one current,
bounded, redacted conversation snapshot per source identity. It never stores a second raw vendor envelope
or imputes missing token, latency, cost, model, session, or content fields. See
the repository's `examples/telemetry/README.md` for exact source contracts and
privacy limits; ADR-006 records only the architectural boundary.
The snapshot keeps an incomplete prefix if its stored content would exceed
512,000 bytes; reply Trace import continues under the published mapping.
`import_into_storage` requires a direct synchronous adapter and rejects
`BufferedStorage` before consuming input. Its stored count refers to completed
adapter writes.

The storage API provides `get_conversation`, tenant-scoped
`list_conversations(tenant_id, after=..., limit=...)` (up to 20 per page), and
`delete_conversation`. The latter does not delete separate Trace rows, and
`delete_trace` does not delete a conversation snapshot. Retention via
`prune_before` covers both and still returns a Trace count. Evaluator Lab can
grade eligible snapshots using a local JSON rubric and review current evidence
with full or partial coverage. Monitor provides a descriptive comparison of
current whole-conversation binary grades in two historical windows, with
optional grouping by explicit Voice source label. It reports eligible,
not-evaluable, PASS/FAIL, UNCLEAR, judge-error, and ungraded-eligible coverage
separately. The PASS rate uses only
PASS and FAIL grades; changing a transcript or label invalidates its old grade.
This comparison does not create a prospective alert or discover semantic
clusters. Investigation → Compare additionally accepts one source-declared pair-ID
label and variant label for two versions of the same evaluation input. It
compares only exact current whole-conversation binary or bounded numeric grades
from one evaluator inside one UTC end-time window; duplicates and missing
grades are excluded with counts. Numeric paired differences have a conservative
bounded-score 95% interval under independent, representative-case sampling. This is an unsaved
descriptive result, not a causal model winner, replay, or alert. The producer
must supply truthful pair IDs and variant metadata; a shared ID does not prove
identical interactive turns. At most 10,000 selected-variant rows enter a query.
Extremely wide valid numeric ranges retain inspectable pairs but cannot show
a finite aggregate or interval.
Conversation snapshots and grading require synchronized core and eval builds.

OTLP message objects may provide text in `content`, `text`, or typed text
`parts`. Verdict joins genuine text parts in order and ignores unsupported
tool-only parts rather than presenting them as an assistant response.

Evaluator Lab can separately judge native completed Agent Turns whose redacted
request and final output are present and untruncated. This does not relabel a
provider tool-call Trace as an answer or change Trace judgment coverage.
Turn previews are tenant-scoped 100-candidate keyset pages with explicit
egress approval. Local histories without a committed source checkpoint, or
with an incomplete tail on the selected last turn, do not enter those pages.
The same applies to turns at or after an event capture-limit marker.
A bounded Turn/evaluator result slot is current only for the
exact redacted evidence fingerprint; later Turn extensions require reevaluation.
Before each provider call, execution rechecks that Turn under a cross-process
judge guard. A concurrent request skips a busy Turn and reports `inProgress`;
it does not treat the Turn as completed. SQLite uses a separate lock file next
to the database (the database directory must be trusted and writable), so
capture can continue during judging; SQLite Turn judges serialize per database.
Postgres guards individual Turn/evaluator pairs. A judge failure remains
retryable. This prevents simultaneous local judge egress while the guard is
held, but cannot guarantee a single provider charge after process failure,
connection loss, or a provider-side retry.
Agent Runs detail displays the latest valid current result among eight newest
evaluator slots per Turn, or one selected exact evaluator. Older results may
exist outside this bounded view. An optional `toolEvidence: "counts_v1"`
mode adds only bounded recorded tool call/result/error counts and explicit
producer-recorded dispatch-origin counts to the judge prompt; missing and
unusable origins remain separate buckets. The event projection never includes
tool names, arguments, result bodies, or URLs. Turns with no recorded tool
events or more than 64 events are ineligible in this mode. Origin labels
describe only the observed outer dispatch and cannot establish hidden
downstream protocols. Counts do not establish complete tool coverage, citation
support, or factual accuracy, and do not enable rubric dimensions requiring
retrieved context. Citation verification remains separate future work. Agent Turn judgments may be included in the descriptive
logical-session Monitor preview, which makes no inferential or activation claim.
Only judge-visible counts bind score currentness: adding an unrelated
model event below the 64-event limit does not invalidate a score. Total event
count remains a separate eligibility bound. Preview and Agent Run detail read
bounded event metadata for their Turn page in one batched query.

The Langfuse reader targets the supported v4 Observations API v2, not the
deprecated trace-list endpoint, so Verdict receives one record per actual
generation or embedding rather than a trace aggregate.

For a pilot, follow the versioned
[release profile](../../docs/RELEASE_PROFILE.md).
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
standalone. Newly captured spans carry the configured tenant identity (or the
local tenant when none is configured). The dashboard lists a span under a call
only when both identities match. Existing spans without a tenant identity are
preserved in storage but are not exposed through selected-call span inspection
or optional AI explanation. Older span capture records remain importable with
ownerless status.

When trusted application code needs the ID before one model request, use the
separate one-call reservation:

```python
with verdict.model_call_context() as correlation_id:
    response = provider_client.chat.completions.create(
        model="customer-model",
        messages=messages,
        extra_headers=gateway_profile.headers(correlation_id),
    )
```

Verdict generates the 32-character ID and assigns it to the first supported
instrumented provider Trace built inside the context. A second call gets its
normal independent Trace ID, and an unused or exited context creates no
record. Verdict does not inject a gateway header or make the ID proof of a
deployment, backend, or hardware resource; that requires a separately
authorized exact-ID source. `trace_context()` retains its existing manual-span
link meaning and is not interchangeable with this API.

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
The current release profile uses `buffered_writes=False`. Buffered mode requires
an explicit `shutdown()` imported from `verdict.client` before process exit.
Fixed-window `DriftRun` snapshots created by older releases remain readable for
compatibility; the current pipeline does not create or replace them.
`prune_before()`
removes expired standalone and orphan span rows while preserving an old span
referenced by a retained Trace. SQLite and PostgreSQL execute multi-table trace
deletion and pruning atomically and serialize concurrent trace writers while
they decide which shared parent spans must survive.
Stored costs are best-effort estimates from Verdict's dated static base-price table;
the table includes GPT-4.1 base text models and their published dated snapshots.
Unverified fine-tuned/custom derivatives remain unpriced; the values are not
billing truth.

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
python -m pip install "cognifity-verdict[dashboard]"
verdict-dashboard --storage sqlite:///./verdict.db
```

Any reachable store with no Agent Runs or LLM Traces visible for the selected
tenant is shown as connected and waiting for SDK traffic or an approved import.
The dashboard reports only whether the configured backend is SQLite or
PostgreSQL; it does not return a database path or DSN to the browser.

If capture/import used an explicit tenant, pass the same `--tenant-id` to the
dashboard or set `VERDICT_TENANT_ID`. The default remains
`__verdict_local__`; selecting another tenant requires no data migration and
does not relabel existing rows. Dashboard-selectable tenant IDs contain at most
128 ASCII letters, digits, `.`, `_`, `:`, or `-` and begin with a letter or
digit. Existing telemetry imports retain the published 256-character routing
boundary; use at most 128 characters for a new standalone dashboard workspace.
Browser `tenant=` parameters do not select a workspace.

Add the `postgres` extra for a PostgreSQL store. Verdict requires PostgreSQL
databases to use UTF-8 encoding. Legacy SQL_ASCII databases are not supported.
Dashboard analytics are read-only; the setup/import and Monitor controls are
explicit storage mutations. The app can also be mounted with
`verdict.dashboard.create_app()` behind an existing
FastAPI application's authentication. Investigation → Model calls pages through every
non-judge application trace in deterministic 30-row pages with complete store
totals. An exact selected trace outside the current page opens as separate
detail without displacing a page row. Provider/content-state filters apply to
the current page. Judge
telemetry remains in aggregate cost and store totals but does not displace
application traces from this view. A `Historical
metadata-only trace` means content was not captured when that specific trace was
recorded; it does not report the application's current capture setting. Monitor
previews lead with the comparison result for the selected count-based or
explicit event-time cohorts. Alert cards chart reference and current rates,
show the statistical gates and coverage, and link bounded evidence traces when
the snapshot contains them. Cohort summaries retain the first bounded true and
false trace IDs for every metric cell as their single stored owner; alert cards
select the relevant side for investigation rather than claiming an automated root cause. Fixed-window
results produced by older releases remain readable through the Python storage
API and do not appear in the dashboard.

For a one-off export that is not in the Verdict store, open **Evaluate →
Inspect JSON**. Paste JSON or choose a local export file; both use the same
bounded request path. Verdict does not save the upload or the resulting
report. Analysis runs on the Verdict dashboard host; semantic analysis and the
external judge are separate opt-ins. The judge also requires an explicit
confirmation before any content is sent to its provider.

Upgrade an existing synchronized `0.1.0a5` through `0.1.0b1` environment with
`python -m pip install --upgrade`
and the same provider, dashboard, semantic, and storage extras already in use.
The published wheels replace editable installs without a new clone and reuse the
selected SQLite file or PostgreSQL tables in place. Use the synchronized
[beta release profile](../../docs/RELEASE_PROFILE.md) for the exact
three-package install command and verification steps.
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

A private package that owns a full-page Operations application can instead
mount its authenticated routes and assets at `/operations` in the same FastAPI
composition root and pass `operations_page_url="/operations"` to
`create_app()`. Verdict then adds one same-tab top-level **Operations** control
and the same path to `/api/config`; it does not discover, import, or start the
private package. Omitting the argument leaves the route, control, and config
field absent. The older `operations_url` Settings adapter above is unchanged.

The dashboard's **Groups** workspace is a bounded view of the Task 5
tenant/version registry. It shows active and preview versions, stable display
names, frozen algorithm/selector/model configuration, representative redacted
prompts, bounded provider/model distributions, membership explanations,
terminal outlier/ineligible reasons, coverage, and validation readiness. Its
per-cluster independent-conversation counts accept only strict-UTF-8, nonempty,
NUL-free session IDs of at most 256 bytes. Their time-to-readiness value is a
diagnostic estimate at the documented default windows/floor, not activation
or drift decisions; fragmentation/dominant-cluster warnings likewise require
operator inspection. The full 250-cluster list remains visible while nested
evidence is limited to the 20 highest-volume clusters. Standalone use selects
the reserved local scope by default, or the explicit `--tenant-id`, and uses
that same scope for dashboard totals, reports, traces, judgments, Agent Run and
registry reads, plus its in-process mutations. A mounted host can
instead set `request.state.verdict_registry_tenant`; that authorization-owned
value wins; browser query input is ignored. Mounted mutation buttons use the
same-origin Operations adapter. Semantic and hybrid fallback retain their experimental
disclosure. When a mounted host supplies that authorized tenant, Home,
Investigation → Model calls, and cluster pass-rate charts project assignments
and stable labels from the same active registry. Standalone and legacy stores
without an active registry for the selected tenant continue to use
`Trace.cluster_id`.

For the current published beta, the bounded pilot entry points include Anthropic
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
