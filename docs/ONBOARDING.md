# Tester Onboarding

Goal: get from installation to a first local report on your own approved agent or LLM application evidence. The current beta includes guided Home, worker status, and optional selected-evidence AI explanations. Usage, run reports, Turn analysis, and deterministic suggestions are key-free. An external judge requires your own provider key and explicit evidence approval.

## 0. Prerequisites

- **Python 3.10+.** On macOS the system `/usr/bin/python3` is often 3.9 and
  will fail — use `brew install python@3.12`, `pyenv`, or `uv`.
- Optional: a provider key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `GOOGLE_API_KEY`, or `TYPESAFE_API_KEY` for Jev) if you want the judge / quality-drift layer. Everything
  else works without one.

## 1. Install

The commands below install the current published packages for agent profiling, provider-call capture, and monitoring.

The synchronized beta distributions are `cognifity-verdict`,
`cognifity-verdict-eval`, and `cognifity-verdict-inspect`. Do not install the
unrelated `verdict` distribution from PyPI; it exposes the same import namespace
and cannot safely coexist in one environment.

```bash
uv venv --python 3.12 && source .venv/bin/activate     # or your own 3.10+ venv

# Include the provider extras you want to test live. Google capture needs `google`.
python -m pip install --pre \
  "cognifity-verdict[anthropic,openai,google,dashboard]" \
  "cognifity-verdict-eval[semantic]" \
  "cognifity-verdict-inspect"
verdict-dashboard --storage sqlite:///./verdict.db --host 127.0.0.1
```

For a pilot on the public beta, use the provider coverage matrix in [`RELEASE_PROFILE.md`](RELEASE_PROFILE.md).

You do **not** need a separate `pip install scipy scikit-learn` — `verdict_eval`
lists them as hard dependencies, so the line above brings them in.

Minimal alternative without the local semantic model:

```bash
python -m pip install --pre "cognifity-verdict-eval"  # lexical hash fallback
```

## 2. Confirm it's healthy (30 seconds, no key)

```bash
python -c "import verdict, verdict_eval; print(verdict.__version__, verdict_eval.__version__)"
verdict-pipeline --help
verdict-dashboard --help
verdict-monitor --help
```

All three commands above come from the installed distributions. A Git checkout
is needed only for contributor tests and the research calibration scripts later
in this guide.

## 3. Import local Claude Code or Codex history

```bash
verdict
```

An empty loopback-only UI opens at Setup. Select **Claude Code / Codex**, review
the detected directories, choose **Preview sources**, then explicitly approve
the capture. Bounded redacted content capture is on by default; disable it only
for an intentional metadata-only run. The server rejects capture when those
exact paths were not previewed in the current process. Source files are
read-only and repeated rescans are idempotent.
Codex Turn requests are taken from text in either legacy `user_message`
records or completed `UserMessage` items. Non-text-only messages remain
missing evidence; Verdict does not turn image or attachment metadata into
request text. Rescanning with content capture enabled can fill a request that
an earlier import marked missing.

Claude Code may record one provider response across multiple history rows.
Verdict coalesces those rows into one model-call Trace and fills later response
text without changing its evidence identity; a genuine tool-only call remains
visible as textless rather than being presented as a missing capture.
For the standard `~/.codex/sessions` directory, the preview also shows the
sibling `~/.codex/logs_2.sqlite` source when it exists. Approval reads that
database directly; no Codex export is required. Each supported completed-call
marker becomes a metadata-only OpenAI Trace with its model and observed
response time. Verdict never stores the diagnostic body or raw Codex source
identifiers.

Successful capture opens **Home**. Local execution evidence is under
**Investigation → Agent runs** and genuine model calls are under
**Investigation → LLM Calls**. Data-source actions remain under
**Settings → Data Sources**, where **Manage data sources** asks you to approve local
paths again. The header reports Agent Runs and LLM Calls separately.
If a manual or worker check skips a local history file or cannot import its
recent index or Codex diagnostics, the latest cycle reports
`capture_incomplete` even if analysis succeeded. Inspect that cycle's capture
summary and rescan after fixing the source; previously stored runs remain the
last committed snapshots, not proof that the latest file was imported.
One approved background capture attempts every discovered `.jsonl` history
file once and reports file progress. Discovery is limited to 100,000 files per root;
exceeding that limit fails the job before import. Each attempted file has its
own parse and projection bounds. An interrupted process can be restarted and
the same roots approved again; committed source revisions are checked on
replacement. A rescan reads the source files again to detect changes.
Compressed `.jsonl.zst` Codex histories are not currently included in preview
or capture, so the reported file total does not include them.
During setup, **Cancel capture** requests a stop after the current file. The
job then reports `cancelled`, shows committed progress, and refreshes Home;
already committed files remain available. Preview the roots and approve a
rescan to continue. A cancelled file attempt is tracked separately from a
source failure. Cancellation is available while files are importing, not
during the later analysis step.
A local history file above 128 MiB and at most 1 GiB is parsed only through
a bounded prefix. Verdict hashes the whole file for revision checks, stores
the observed prefix, and reports `bounded_source_prefix` as partial source
coverage. Files above 1 GiB are skipped as `history_file_too_large`, with
any earlier accepted evidence retained. Later activity
in that file is unobserved; the visible run status and token totals describe
only captured evidence. A file with no usable record in the prefix, or a
record exceeding the line bound, can still be skipped with an explicit reason.
Parsing rejects a file after 100,000 projected events, including multiple
events from one Claude history row, with `history_event_limit`. An earlier
accepted revision remains stored.
The setup capture job reports incomplete when files are skipped; its captured
evidence remains available and the dashboard refreshes it. Each
attempt keeps a bounded reason category such as `failed_malformed_jsonl` or
`failed_local_source_conflict` without storing a path or exception body.
Data Sources also shows counts and reason categories from the latest durable
per-file attempts after a dashboard restart, including interrupted and
cancelled attempts. The durable counts describe history-file attempts; a
cancel request after the final history file has committed does not leave a
cancelled file count, although that job may still report cancelled before a
restart. `filesDone` counts attempted files, including a claimed file stopped
before parsing. These counts cover previously
tracked files for this tenant, including files from an older source root;
preview the current roots and rescan to establish their current status.
If a local session reaches the 1,500-event bound, the capture result counts
it and warns that later canonical event detail is missing; its recorded turns
and source-reported token totals can still appear. In that run, select
**View recent sample** for up to 100 recent turns and 50 redacted events,
with the committed source revision, record count, capture time, and events
outside the captured prefix and sample. Complete selected turns receive
deterministic recorded-issue checks;
incomplete selections receive no whole-turn finding. Bounded, fully parsed
files within the local full-capture budget also retain other non-prefix events
and tail turns. In Investigation, **Browse recorded events** pages them only
after checking the committed revision, turn ownership, and exact event count.
It proves coverage of recorded events for that captured revision, not task
success or actions absent from the source. **Analyze recorded run** runs
deterministic checks on all retained typed turns and events after that proof;
its findings open the matching recorded-event page. When a deterministic
recommendation applies, the report shows one suggested action and its evidence
link; it does not claim a verified fix or saving. The ordinary run overview
still uses its bounded canonical prefix. Saved Insights analysis uses verified
retained turns and events within a 5,000 additional-row scan budget; sources
that fail that proof or exceed the analysis budget remain partial there.
Recurring explicit-issue notices can use retained typed facts after activation
even when the aggregate remains partial. Selecting an Insights issue outside a
local run's quick-view prefix automatically checks its recorded-run report and
offers the exact retained event link when that full projection is available;
it makes no AI judge call. The recorded-run report leads this view; the
historical prefix's smaller counts and findings remain under **Historical
prefix details**. The run picker identifies sessions by source and time;
counts appear in the selected run with their coverage stated. The run header
labels prefix-only tool counts. The report
can open a suggested action's supporting event directly in the recorded-event
page; ordinary browsing leaves events collapsed. On-demand analysis of a
retained tail Turn requires that complete-capture proof and still shows a
bounded event review.
Day token coverage uses the same proof before treating retained tail Turns as
complete. The day issue reader also scans retained typed events and can show
a complete recorded-issue total when this proof and its bounded scan succeed;
larger partial imports still show observed issues as a lower bound.
Files above
the budget retain selected non-prefix events as typed facts: an explicit
failure can create a durable issue notice after monitoring is activated, and a
linked model call can enter supported call-level evaluation. The current view
does not establish a full-run issue or no-alert denominator. Use **Open exact event link** on a
recent finding or event to retain its first-seen source revision and original
turn/event IDs in the local Investigation URL. The retained fact remains
addressable after the 50-event window moves; an old unqualified window link
may become stale. Insights shows partial histories
as observed prefixes and excludes them from whole-run outcome counts.
If the file was shortened or its recorded evidence conflicts on rescan,
Verdict leaves the canonical run and dated sample intact and reports
`local_source_conflict`. A failed recent-window write rolls back the capture.
A larger source rewrite may complete missing evidence only
when both histories are fully captured and the old facts remain compatible.
An unfinished final JSONL line marks its run and last parsed turn partial;
optional AI previews do not send those incomplete scopes. Appending the
remainder and rescanning clears that temporary coverage state. Beyond 1,000
turns, the first 1,000 remain as historical evidence while the V1 whole-run
read refuses to call that prefix complete. For a bounded, fully parsed file
admitted to full capture, Browse recorded events and Analyze recorded run can
read verified middle events and tail turns. A file above that admission budget
retains only selected recent non-prefix facts, so its omitted middle events
remain unavailable. The recent view loads selected event details from retained
facts. An old exact link still opens a selected current event.
Investigation labels the stored status as a recorded prefix outcome and its
known token sum as partial until source coverage is complete.
Open a recent turn to check its canonical evidence coverage. This warning
uses the newest local import summary
visible to the running dashboard, including an import whose later analysis
failed; a rescan that fails before producing a summary leaves the prior one.
It is not an all-time store audit. A dashboard restart clears the in-process
job status.

The Agent runs view shows source sessions, typed turns and observable events,
tool/command failures, completion evidence, possible repeated-tool loops, and
what cannot be evaluated. A failure count is shown only when the source reports
success or failure for that event type; otherwise it reads **Not reported by
source**. Current Codex tool output and Claude Code shell commands carry no exit
status, so their failures cannot be counted. Source-identified Claude sidechains and Codex child
histories remain separate linked runs rather than being folded into a parent.
A parent reference can remain unresolved if the source no longer contains that
parent history. Local sources that do not expose a genuine provider
call do not create fake `Trace` rows, so the LLM-call Monitor may truthfully be
empty while agent evidence is useful.

Open a Turn and select **Analyze turn** for an on-demand, local review of
recorded actions, matched tool results, and reported failure signals. The
linked events let you inspect the evidence behind the review. A partial
capture is labeled, including when an earlier capture-limit marker means a
later Turn has no stored events. No judge request is sent. The review shows observable
behavior; it cannot reconstruct private reasoning or prove task correctness.

Agent timelines are stored as normalized source/run/turn/event rows. Genuine
model calls link to existing `Trace` records rather than copying their content
into event storage. When upgrading an existing SQLite or PostgreSQL store, stop
older Verdict processes and take a backup before the first new process opens
the store; that first open transactionally migrates legacy serialized agent
bundles. Do not run old and new Verdict writers against the same store.
The migrated store rejects writes from an older process rather than silently
hiding them; upgrade every SDK, collector, service, and CLI writer that shares
the database before resuming capture.

Local-history token counts are usage evidence, not billing evidence. Codex
counts are derived from within-turn cumulative-counter deltas. Claude counts
sum each unique provider response once and retain cache-read and cache-creation
components. Its total appears only after the turn is terminal and every
response has complete input/output usage. Missing or malformed counters display
as unavailable, not zero.
Verdict does not put a cost on these traces. It shows a separate **estimated
list price** for agent turns in Home's Performance details and in the per-source
table: the turn's token components priced at the public per-token rates for
the one model the turn named, with cache reads and cache writes at the
provider's cache rates (Claude Code counts exclude cached tokens from input;
Codex counts include them; cache writes are assumed to be five-minute writes).
It is a list price, not a bill: desktop and
subscription activity is not charged this way. A turn is left unpriced when it
names no model or more than one, when its model has no static price, or when
its token basis is unknown; the figure states how many turns it covers.
The static table recognizes GPT-6 Astra, Sol, 6.1 Sol, and Luna at their
standard short-context text rates; it does not infer a price for other GPT-6
variants or service tiers.
**Home** shows a 30-day UTC Agent Turn usage chart when turns report token totals. If only recorded issue events are present, Home links the latest issue day directly to its evidence instead of drawing a zero-token chart; Performance details retains the issue-event timeline. If provider calls report token totals but Agent Turns do not, Home shows a separately labeled model-call token chart with call-count coverage; selecting its day opens that UTC date's calls. Call totals are not assigned to agent sessions. On the agent-turn chart, a red marker identifies a UTC date with recorded issue events, including retained selected recent local-history facts; selecting it shows a bounded issue count and linked example runs. Retained-fact examples open their first-seen exact event links. Issue dates use event time, while token dates use turn start time. A partial bounded issue scan withholds the count. If the stored-event scan is complete but local source history is incomplete or unverified, the selected day shows recorded issues as a lower bound and retains exact evidence links, without claiming a full-day total. For an older local import without a source checkpoint, that uncertainty applies to the displayed window until a compatible rescan verifies the source. A partial local-history import with omitted events makes issue counts lower bounds across the displayed window; an admitted complete projection can clear that uncertainty after its event proof and bounded scan. When all Turns and token totals were captured, an event-only cap does not block token comparisons; missing Turns can block days intersecting their verified parsed Turn interval, while an unverified source can affect the displayed window. Empty captured days with partial source coverage remain selectable to inspect that uncertainty. If Home has no token totals or issue examples to chart, use **Inspect incomplete day** to open the day detail. **Performance details** expands the agent-turn drilldown. A turn
is assigned to the UTC date when it started. Choose a day to see sessions active
on that date and their model breakdown; choose a session to see its largest
turns across the report window. The session's **Analyze session** action explains
the recorded token contributors and links each turn back to its Agent Run. It
uses deterministic arithmetic and makes no external evaluator call. Token and
price coverage count turns with reported totals and priceable turns, respectively,
over all captured turns;
unavailable counts remain unavailable. The view scans at most 20,000 turns and
100,000 model/context events, shows up to 100 sessions per day and 50 turns per
session, and marks a partial scan. Child runs sharing a logical session ID are
grouped for this view. The observed first-to-last Turn span includes pauses and
tool work, so it is not provider latency. Genuine model-call latency remains in
the LLM Trace performance view when the source supplies it.
The selected session also opens one combined evidence report and an optional
session-wide evaluation preview. That report is independent of the 30-day chart
window and includes up to 20 runs, 20 turns, and 80 events. Larger sessions are
marked partial and link to their individual runs; external evaluation is then
unavailable for the whole session.
From **Explain this day**, **Inspect session** moves focus to that session's
detail after it loads. **Investigation → Agent runs** loads a bounded run
list first and expands only the selected run, so opening the list does not
analyze every run on the page. From a selected run, **Browse runs**
opens that tenant's list filtered by the recorded agent name, or by source if
the name was not recorded. Matching names do not establish the same task or
recovery from an earlier failure.
Codex diagnostic traces include input/output tokens only when one valid usage
event from the same session matches the completion timestamp. Prompt,
response, latency, and cost remain unavailable, and the traces are not
evaluator inputs. Codex controls diagnostic and session retention
independently, so Agent Run, model-call, and token-covered totals can differ;
an unknown diagnostic schema is skipped and reported as unavailable.
If a history rescan is rejected, an existing diagnostic call keeps
its previously stored token usage. Newly observed calls can still use a
uniquely matched valid usage event from a parsed history file; calls without
one remain metadata-only. A diagnostic import does not make a rejected Agent
Run revision current.
The capture summary counts a retained prior match as a skipped diagnostic
update and labels diagnostic coverage partial. A cycle with missing or rejected
history reports incomplete rather than implying current token coverage. The
same partial label applies when the diagnostic read limit omits older calls.
When that matched event also contains a valid cached-input count, Report shows
cached and uncached input separately and reports the number of calls covered by
the breakdown. Missing or impossible cache counts remain unavailable.

Opted-in content remains bounded and recursively redacted. Each turn and event
is bounded independently. Turn request and final-response text is redacted
before its 64 KiB preview cutoff and reports when it was truncated. If one
event's content exceeds its evidence limit,
Verdict retains that event's metadata and records why its content was omitted;
it does not downgrade the entire run.

The non-interactive equivalent is:

```bash
verdict-import local --storage sqlite:///./verdict.db
verdict-dashboard --storage sqlite:///./verdict.db
```

`verdict-pipeline` prepares optional clusters and judgments. It does not publish
a separate drift result. In the dashboard, open **Model drift setup → Advanced comparison settings**,
preview all traffic or an optional facet, and activate the reviewed policy if
new traffic will continue arriving.

## 3a. Instrument the surrounding agent execution

Provider instrumentation captures the LLM calls. Add `agent_run` and `turn`
contexts where the application knows the execution boundary, then record only
the observable facts it can establish:

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

For a recorded multi-agent handoff, pass the actual parent run ID to the
child, including when the child executes in another process:

```python
with verdict.agent_run(name="coordinator", session_id=session_id) as parent:
    child_request = prepare_child_request()
    # Send parent.run_id with the child request across your application boundary.
    with verdict.agent_run(
        name="research-agent",
        session_id=session_id,
        parent_run_id=parent.run_id,
    ) as child:
        with child.turn(user_input=child_request) as turn:
            turn.set_output(run_research(child_request))
```

The relationship is present only when the application records that ID.
Verdict does not infer a handoff from matching times, prompts, or agent names.
Keep the same tenant on both sides; a missing or foreign parent remains
unlinked in Investigation.

Use a boolean `False` only when the application can authoritatively establish
that the business outcome failed. Verdict retains that fact in the event status
and surfaces a deterministic `business_outcome_failed` finding, even in
metadata-only capture. Non-boolean outcome values remain descriptive.

The same contexts work with `async with`. Tool exceptions and run/turn timeout,
cancellation, failure, or completion are recorded without replacing the
application exception. Supported provider calls that begin inside a turn link
their genuine `Trace` to that turn. The Trace remains the sole owner of prompt,
response, and raw-message content; the event contains bounded operational
fields only. Sampling retains or omits the complete run as a unit. With content
capture enabled, omitting `set_output()` records missing response evidence;
metadata-only capture records the response as not captured.

Pass `ToolOrigin.MCP`, `ToolOrigin.PROVIDER_HOSTED`, or
`ToolOrigin.APPLICATION` only when the producer directly observes that outer
dispatch boundary. Omit `origin` when it cannot. Verdict does not infer origin
from tool names. A wrapper classified as application may still use an
unobserved downstream protocol, so the label cannot prove MCP was absent.

For an application host that should not hold database credentials, configure a
separate local spool directory for each producer process:

```python
verdict.init(
    transport="file",
    spool_directory="/var/spool/verdict/worker-1",
    service_name="support-api",
)
```

Stop or quiesce that producer, then import the files idempotently:

```bash
verdict-import agent-file /var/spool/verdict/worker-1 \
  --storage postgresql://verdict@db/verdict
```

This import URI contains no password. Supply database credentials through
the PostgreSQL client environment or a protected service configuration; do
not put a credentialed URI in command arguments or checked-in scripts.

The spool carries provider Traces, Agent evidence, manual spans, and user
signals in versioned JSONL with fixed record, segment, and directory byte
limits. An incomplete final record from a process crash is reported and ignored;
a malformed complete record fails the import. Retain the files until the import
succeeds. Completed appends bypass
Python userspace buffering but are not `fsync`-ed against an operating-system or
host failure. If an Agent evidence stream fails, later provider calls fall back
to standalone Trace records rather than creating sequence gaps. Check the
process-local `capture.dropped_records` runtime metric for records rejected by a
full or failed spool; equivalent failures produce one bounded warning per
failure class.

For a central PostgreSQL deployment from this source tree,
install with its `postgres` extra and start the authenticated collector:

```bash
export VERDICT_STORAGE='postgresql://verdict@db/verdict'
export VERDICT_TENANT_ID='__verdict_local__'
export VERDICT_COLLECTOR_API_KEY='use-a-protected-random-secret'
verdict-collector --host 0.0.0.0 --port 8765
```

Terminate TLS before the collector. It accepts bounded `verdict-capture-v1`
NDJSON batches containing `agent`, standalone `trace`, and `span` records and
returns durable replay-safe acknowledgements. Older `0.1.0b2` collectors do
not accept standalone records. On each producer host, start the shipper
with the same protected secret:

```bash
verdict-shipper \
  --spool-directory /var/spool/verdict/worker-1 \
  --collector-url https://collector.example.com \
  --json
```

Run with `--once` for a scheduler-managed cycle; without it the shipper polls
continuously. It uploads complete records from active files, advances its local
checkpoint only after validating the collector acknowledgement, and deletes
only sealed segments whose records were all accepted. A rejected segment is
retained with a `.rejected` suffix for recovery or manual import. Restart an
older producer with the upgraded SDK before enabling shipping. Shipping itself
does not run analysis, evaluation, clustering, or monitoring. Inspect local
delivery health without a collector connection or secret:

```bash
verdict-shipper \
  --spool-directory /var/spool/verdict/worker-1 \
  --status --json
```

The stock single-tenant dashboard reads `__verdict_local__` by default. Pass
`--tenant-id ID` or set `VERDICT_TENANT_ID` to use the same explicit tenant as
capture/import. Explicit IDs use 1–128 safe ASCII characters. An authenticated
host may supply a different authorized dashboard read/analysis tenant as
documented below.

## 3b. Existing conversation export with Inspect

This is the fastest way to see Verdict find something real, with **no
instrumentation and no code changes**. In the dashboard, open **Evaluate →
Inspect JSON**, upload or paste the export, and choose the format. The result is
returned to the browser and can be downloaded as JSON. Analysis runs on the
Verdict dashboard host; neither the source nor the report is added to the
Verdict store.

The same analysis remains available from the `verdict-inspect` CLI:

```bash
verdict-inspect analyze ~/Downloads/conversations.json
verdict-inspect analyze --report ./drift_report.md ~/Downloads/chatlog.jsonl
verdict-inspect analyze --no-judge ~/Downloads/conversations.json   # skip the key-gated judge
```

Key-free, the recommended install gives you local MiniLM semantic drift plus
structural metrics (length, hedge/refusal/apology rates over time). The minimal
install falls back to a lexical hash embedding and labels that limitation in the
report; it should not be interpreted as semantic similarity. The first MiniLM
run may download model weights; embedding inference then runs locally. Set
`ANTHROPIC_API_KEY` to add the PASS/FAIL judge sample; omit it (or pass
`--no-judge`) to stay fully local. The inspector can form windows from eight
substantive turns each, but treat those small-window results as exploratory;
roughly 30 or more per window gives more useful evidence.

## 4. Import telemetry you already collect

This is the shortest path when a customer already has LLM observability. The
importer writes normalized `Trace` rows into the same Verdict database used by
the current pipeline and dashboard:

Install the synchronized release with the optional `telemetry` extra when the
source uses OTLP protobuf. JSON files and hosted API readers do not require that
extra:

```bash
python -m pip install --pre "cognifity-verdict[telemetry]"

verdict-import file ./traces.ndjson --format auto \
  --storage sqlite:///./verdict.db --tenant-id my-team

verdict-pipeline --storage sqlite:///./verdict.db \
  --tenant-id my-team --judge-provider anthropic \
  --judge-model claude-haiku-4-5
verdict-dashboard --storage sqlite:///./verdict.db --tenant-id my-team
```

Keep the tenant identical across import, pipeline, and dashboard commands. A
non-default `verdict-import local --tenant-id` run prints this requirement. The
dashboard default remains `__verdict_local__`, and selecting another tenant
does not migrate or relabel stored rows. Browser `tenant=` parameters cannot
change the configured workspace. A Trace ID identifies one tenant's record
within a store; reusing it for a different tenant is rejected.

Use an explicit format (`otlp`, `langfuse`, `langsmith`, `datadog`, `phoenix`,
`opik`, `mlflow`, or `voice`) when auto-detection is ambiguous. The hosted API
commands require `--from` and `--to` so every run is bounded:

| Command | Credentials | Contract |
|---|---|---|
| `langfuse` | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | `/api/public/v2/observations` |
| `langsmith --project NAME` | `LANGSMITH_API_KEY` | `/runs/query` |
| `datadog` | `DD_API_KEY`, `DD_APP_KEY` | LLM Observability span event export (preview) |
| `phoenix --base-url URL --project NAME` | optional `PHOENIX_API_KEY` | `/v1/projects/{project}/traces` |
| `opik --project NAME` | optional `OPIK_API_KEY`, `OPIK_WORKSPACE` | `/v1/private/spans/search` |

Langfuse's legacy `/api/public/traces` read is not used. Langfuse now marks it
deprecated and recommends the bounded v2 Observations API; observations also
preserve one Verdict row per generation/embedding instead of a trace-level
aggregate. Langfuse v4 is the supported API-reader contract. For older
self-hosted exports, use a supported file/OTLP path or validate that deployment
before relying on the importer.

The OTLP receiver accepts JSON, gzip JSON, and protobuf at `/v1/traces`. Its
built-in listener is deliberately loopback-only; put an authenticated TLS OTel
Collector or reverse proxy in front of it for remote producers.
The mapper recognizes current/legacy GenAI fields, Semantic Kernel content
events, OpenInference, Vercel AI SDK provider-call spans, and OpenLLMetry
aliases. It also recognizes Claude Code's enhanced-telemetry
`claude_code.llm_request` spans, including the currently non-standard flat token
fields. It skips Vercel wrapper/tool spans to avoid double-counting a call.
Claude Code response content is commonly absent, so those rows retain useful
model/token/latency metadata but are not judgeable unless the source supplies
prompt and response text.

Import has no sampling switch. Every eligible source LLM call is stored with a
deterministic ID scoped by adapter, tenant, source project/file, and source
trace plus record. Repeating or overlapping an import updates the same rows,
while reused child span IDs in different parent traces remain distinct. The existing
pipeline—not the importer—selects the traces to judge. Source `started_at`
continues to control drift-window membership even when the import happens later.

Missing optional fields remain unavailable. A stable source record ID and valid
start time are required; invalid records and non-LLM spans are counted with
skip reasons. Sparse rows can remain visible as metadata but cannot be judged or
clustered from absent prompt/response text. Verbose text is bounded per Trace,
unknown metadata is not copied, and storage reapplies best-effort redaction.
This is an explicit content-transfer operation, so protect the Verdict database
like the source telemetry store.

After import, open **Home** to review captured activity and its next action. For genuine model-call comparison, open **Model drift setup** and preview either count cohorts (older 80% versus
newer 20% by default) or explicit event-time ranges. Verdict freezes exact
membership before computing outcomes. Under **Measurement**, keep the
deterministic trace checks or select one existing complete evaluator. Selecting
an evaluator compares its already-stored per-dimension PASS/FAIL results; it
does not invoke the judge. UNCLEAR, missing, and error results are shown as
coverage and excluded from the PASS/FAIL denominator. The activated policy
freezes that evaluator fingerprint and its expected dimensions. Provider/model
and reviewed-cluster facets produce separate comparisons for every eligible
group and metric; Verdict corrects across that complete family instead of
pooling groups. Reviewed-cluster policies pin the registry version used by the
preview, and each run completes projection of eligible new traces through that
version before saving monitor membership, without fitting or changing clusters.
If bounded projection work remains, the run reports `projection_pending` and
saves no monitor snapshot. Approved baseline facts are immutable. An open
evaluator-backed cohort fixes membership, reports pending/error coverage, and
waits for stored results required by like-for-like metric cells before
comparing. Unassigned and new groups remain coverage signals. Changed or
removed pending evidence requires a new reviewed preview. If no group/metric
cell has enough eligible completed LLM calls, Verdict reports
`insufficient`; if unassigned or new groups exceed the configured
support threshold, it reports `reference_stale`.
Previewed comparisons remain exploratory until explicitly activated. A reference with fewer eligible calls than the policy minimum cannot be activated; a small historical current cohort can still be activated against an adequate reference, but no comparison decision is implied.
Activation does not promote the historical preview result: it freezes the
reference and opens an empty prospective current bucket. In-flight traces are
excluded from monitor evidence.

To inspect native Agent evidence without treating correlated calls as separate
samples, choose **Logical session (descriptive)**. Verdict groups runs only by
their explicit `AgentRun.session_id`, excludes currently nonterminal sessions,
and reports completion/final-output rates, effects, and coverage. You can also
select one existing Agent Turn evaluator to aggregate current PASS/FAIL results;
the preview never invokes a judge. Missing session identities remain missing.
Because a currently terminal session may receive a later run and independence
is not established, this mode is recomputed as of each preview, is not saved or
activatable, and returns no p-values or alert decision. Grouping facets are not
available in this mode.
One preview accepts at most 1,000 runs or 10,000 Turns. Selecting a Turn
evaluator also caps redacted Turn text at 16 MiB and selected judgments at
10,000 / 16 MiB; counts-only tool evidence additionally caps scanned events at
100,000. The returned projection omits Turn text and event bodies. Larger
inputs fail closed instead of silently sampling sessions. The selector examines
the newest 1,000 stored Turn-result slots, exposes at most 100 identities, and
warns when older slots were not inspected.

To run the active policy from cron, systemd, Kubernetes, or another scheduler:

```bash
verdict-monitor run --storage sqlite:///./verdict.db
```

Each invocation consumes at most one new prospective non-overlapping cohort.
Late-arriving units remain eligible for the next open cohort rather than being
dropped. Across repeated looks, Verdict spends the configured alpha with the
summable `6 / (pi² × look²)` schedule after applying Benjamini-Hochberg within
each look. The dashboard, `verdict-monitor`, manual scheduled action, and
`verdict-service` share the same trace, stored-judgment, evaluator-dimension,
and frozen-group input construction; none of these monitor paths calls a judge.
Cluster-backed runs complete assignment through the pinned registry before
freezing membership. Rerunning after `projection_pending` resumes from durable
assignments already written.
Repeating the command without new eligible traffic returns the same snapshot
identity instead of duplicating work. The reference does not silently move or
recluster; create and review a new candidate when the comparison contract must
change. A grouped monitor can contain at most 250 distinct groups. A stored
monitor without frozen cohort facts remains readable but must be replaced from
a new reviewed preview before it can execute. The same applies to an older
evaluator-backed monitor that cannot represent pending finalization.

Safety limits are 64 MiB per JSON file or hosted API response, 16 MiB per
NDJSON row, and 16 MiB per OTLP receiver request by default (including bounded
gzip decompression). One mapped Trace retains at most 1,000 messages and
100,000 UTF-8 characters in each input/output direction. These limits truncate
content inside an otherwise valid record; they do not randomly sample source
records. Use NDJSON to stream exports larger than 64 MiB.

Deterministic IDs include `--source-scope`. API commands default it to the base
URL plus project, while file commands default it to the absolute path. Supply a
stable, non-secret `--source-scope` when the same export may move between paths
or when multiple source projects could otherwise share a scope.

For voice logs, the generic schema emits one Trace for each completed assistant
text turn and uses the preceding transcript as its prompt. It ignores audio
bytes/URLs and interrupted turns. This is not a claim that a transcript turn is
always a provider LLM call; tokens, model, cost, and latency are retained only
when the exported turn contains them. Start with the contract fixtures and
generator in `examples/telemetry/`. One source conversation is limited to 1,000
turns and reports `conversation_turn_limit` when additional turns are omitted.
The same import also stores one current, redacted text-only conversation
snapshot. Malformed or truncated snapshots are labeled incomplete. Source end
time governs retention when present; otherwise the first import time does.
The snapshot is capped at 512,000 stored bytes; longer transcripts keep an
incomplete prefix and the reply Trace mapping continues. A snapshot or Trace
that the storage adapter rejects is reported as `conversation_rejected` or
`trace_rejected` and the import moves on to the next record; only a source or
storage failure stops the run.
Use the storage API's tenant-scoped `list_conversations` cursor to review these
records. In **Evaluate → Evaluator Lab**, select **Conversation or reply** to
upload a JSON rubric and preview a bounded page. The reviewer sees excluded
conversations and partial reply coverage. A judge call requires explicit
approval of the previewed provider, endpoint, and call count. Only clean,
closed snapshots with a completed assistant reply after a user message are
eligible. Correcting a transcript invalidates its previous grades. The example
rubrics are at `examples/telemetry/conversation-rubric.example.json` and
`examples/telemetry/response-rubric.example.json`; custom
weighted totals are outside this generic schema. To explore quality over time,
open **Model drift setup → Advanced comparison settings**, choose **Conversation grade (descriptive)**,
copy the evaluator fingerprint from the Lab preview, enter a binary dimension
from a whole-conversation rubric, and select two non-overlapping windows in
local time. The result displays their UTC boundaries.
The optional source label key partitions both windows and shows group mix and
within-group PASS rates. A Voice record can supply a top-level `labels` object
with up to eight nonsensitive keys (`[a-z][a-z_]{0,31}`) and redacted string values of
at most 128 UTF-8 bytes. Missing values have their own group; changing a label
invalidates the old grade. The comparison makes no judge calls, supports at
most 10,000 selected conversations, and shows not-evaluable, UNCLEAR, judge
errors, and ungraded-eligible records separately. PASS rates use only PASS/FAIL
grades; source-group shares use eligible conversations. If no grade from the
selected evaluator is present in either window, check the fingerprint and dates.
This is a
historical, current-state view: correcting source data can change a later
preview, and it does not create a prospective drift alert or semantic cluster.
Records without a usable source end time cannot enter a dated window.
For this workflow, `OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` select custom
judge endpoints. The consent screen identifies the configured destination
without returning its URL. Numeric rubric bounds and thresholds must be finite
numbers; invalid values receive a validation error before any judge call.
Imports normalize source end times to UTC before storage. Preview rejects a
returned row with a noncanonical end time. A direct database change can make a
row sort outside the selected window before preview sees it; reimport the
source record to repair its snapshot.
For two configurations tested on the same declared input, add an opaque
`pair_id` and a `variant` to each Voice record's top-level `labels` object.
Open **Investigation → Compare**, enter the UTC campaign dates, the whole-conversation
evaluator fingerprint and dimension, both label keys, and the two variant
values. The view requires exactly one current conversation per variant and
pair ID; it reports duplicate, unmatched, ineligible, ungraded, error, and
unusable pairs separately. Binary grades show paired PASS/FAIL directions;
numeric grades show paired score difference and a conservative bounded-score
95% interval under independent, representative-case sampling. Open a pair to
inspect both current transcripts and grades.
The source must guarantee pair IDs denote the same evaluation input and explain
whether the variant is the assigned or actually used configuration. Verdict
does not verify identical turns or claim a model winner. A corrected transcript
or label invalidates its grade; the unsaved comparison must then be rerun.
Both variants must end within the selected window, which uses source end time.
Imported end times are normalized to UTC. A returned row with a noncanonical
stored time is rejected, but a direct database change can make a row sort
outside the requested window before comparison sees it.
For extremely wide numeric rubric bounds, individual scores remain visible
when the aggregate cannot be represented as a finite number.
This workflow is included in the current synchronized core and eval builds.

## 5. Instrument your own app (the five-line pattern)

```python
import verdict
from anthropic import Anthropic     # or openai / google

verdict.init(
    service_name="my-app",
    storage="sqlite:///./verdict.db",
    buffered_writes=False,
    capture_content=True,
)
client = Anthropic()
# use the client normally — supported SDK calls are captured
```

Content capture is **on by default** (PII surface). Supported JSON-compatible message fields
are recursively sanitized before content limits, `Trace` assignment, and storage. This
includes nested OpenAI tool arguments and Anthropic-style tool inputs/results.
Supported credential field names such as `password`, `api_key`, `token`,
`secret_key`, `cookie`, `passcode`, `authorization`, and `client_secret` cause
the complete opaque value to be removed, including explicit plural containers
such as `passwords` and `api_keys`. Typed Agent instruction, context, and
outcome events remove paired content when their semantic `name` is one of these
credential fields. This includes embedded assignments in serialized text.
Complete `=` values, quoted values, and single-token `:` values are removed.
Quote a multiword value after `:`;
unquoted multiword colon clauses are not removed wholesale based only on their
label, while independently recognized secrets are still redacted. Basic and
Bearer padding is removed with the credential. Existing Verdict redaction and hash
placeholders remain terminal when capture and storage apply the boundary more
than once. Agent Run names and descriptive service, version, and environment
fields use the same boundary while routing IDs remain unchanged; flat GitHub,
Bearer, Basic-auth, and common provider credentials are also recognized, as are
unlabeled vendor tokens with a distinctive prefix such as Stripe secret keys,
Hugging Face, GitLab, Slack, npm, and PyPI tokens. Public identifiers such as
Stripe publishable keys are left readable.
Unknown top-level provider fields are dropped; malformed, cyclic, non-JSON, and
excessively deep or large values fail closed. Repeated container references fail
closed at every occurrence so sanitized output cannot alias caller-owned data;
redacted mapping-key collisions preserve every value under deterministic
suffixed keys. Recursive Unicode key decoding uses the same nesting budget and
fails closed beyond it. The pattern detector uses Luhn validation
for complete card candidates and grouped prefixes; an ambiguous numeric
identifier with a valid card-shaped prefix may also be removed. Standard-library
validation applies to IP candidates, so
trailing text that is not part of a validated IPv6 address is preserved while a
clock value such as `12:34:56` is not classified as IPv6. Email discovery uses
a linear `@`-anchored scanner to keep malformed and long inputs bounded. It
remains best effort, not a compliance control, and opaque metadata such as
tenant/session/cluster IDs must be non-sensitive. Set `capture_content=False`
when the approved customer boundary is metadata-only; error categories remain
available, but provider and manual-span exception messages are omitted. The current release profile also keeps
`buffered_writes=False`; buffered mode requires an explicit `shutdown()`
imported from `verdict.client` before process exit.

Use only the provider methods listed in the
[`release profile`](RELEASE_PROFILE.md). The current beta includes the
Anthropic `messages.stream(...)` helper plus OpenAI `responses.create(...)`,
`responses.parse(...)`, and `responses.stream(...)` for new or existing
responses, in addition to the earlier Chat/Google paths. OpenAI's
`responses.with_streaming_response` raw-response manager and the separate
experimental `client.beta.responses` multi-agent resource are not instrumented.

### Verify capture on your own SDK versions (recommended before you trust it)

Capture is the load-bearing piece, and provider SDK internals drift. Prove it on
*your* machine with real (tiny, ~cents) calls:

```bash
export ANTHROPIC_API_KEY=...      # and/or OPENAI_API_KEY / GOOGLE_API_KEY
python scripts/live_capture_check.py
python scripts/live_capture_check.py --providers anthropic,openai --no-streaming
```

A pass confirms the entry points exercised by the script land exactly one new
trace apiece with
tokens, estimated cost for recognized models, finish reason, and errors on the
SDK versions you actually have. It does not expand the supported-entry-point
matrix in the release profile. Every requested provider must complete or the
command exits nonzero; use `--providers` and `--no-streaming` only to narrow the
gate explicitly. The final summary names every provider and entry point that
actually passed, so saved output records the exact live surface exercised.

## 6. Judge calibration with `verdict_eval` (only if you want quality-drift)

`verdict_eval` is a library, not a separate app — you exercise it through the
research calibration scripts in a Verdict source checkout. These scripts are
not required for normal capture, pipeline, or dashboard operation. This
is the honest per-workload step: the shippable judge number is **your** held-out,
human-labeled data, not a public benchmark.

```bash
python scripts/sample_to_label.py --source sqlite --db verdict.db --dedupe-by-prompt --out raw.jsonl
python scripts/verify_rubric_alignment.py --make-template raw.jsonl labels.jsonl
python scripts/label_ui.py --file labels.jsonl                 # local labeling UI, autosaves
python scripts/verify_rubric_alignment.py --labeled labels.jsonl --provider anthropic --judge-model <model>
```

`sample_to_label.py` reapplies Verdict's best-effort redaction at the JSONL
output boundary, including for legacy SQLite rows written before the current
storage sanitizer. Continue to handle the exported file as sensitive.

You hand-label PASS/FAIL **blind** (before the judge runs), then the harness
reports per-dimension and pooled agreement with 95% bootstrap CIs. A threshold
counts as "cleared" only when the **CI lower bound** clears it — not the point
estimate. Needs a provider key (the judge makes calls).

## 7. See findings, evaluate deliberately, and configure monitoring

An older event with malformed stored attributes does not stop a scheduled notice scan. Verdict still reports a separately recorded failure or timeout status and continues to later events; a completed event with unknown attributes is not counted as a failure. Deterministic analysis of that malformed evidence can still fail, and readiness marks the cycle failed rather than implying the whole check succeeded.


After capture, start with the dashboard's **Home** or **Monitoring**. Home leads to reviewed setup when no recurring plan exists; historical finding counts remain inspectable in the issue panel. Once a plan is configured, a current analysis leads from Home to those linked finding cards even when no post-activation recurring notice exists; older findings do not become new alerts. Monitoring also links saved deterministic findings to affected runs before recurring checks are configured. It labels saved-analysis coverage and freshness separately from post-activation recurring notices, and offers a direct local refresh when the saved analysis is stale. The suggested plan runs deterministic analysis once before a recurring schedule is saved, and a failed selected check does not unlock saving. A new guided plan disables consumption of pending approved judge jobs by default; enabling that advanced option can send bounded evidence to the selected provider and incur its separately approved cost. Existing schedules without this option retain their earlier judge behavior until reviewed. Open recurring notices are paged separately from acknowledged and dismissed history. Explicit event failures link to exact run events; terminal `failed` or `timed_out` Agent Run statuses link to the run without claiming task failure or drift. An authorized operator can attach exact IDs of a later explicitly positive event to an event notice as recheck evidence; the UI links both events and keeps review state separate. For SDK command and test events, an optional matching application-supplied `check_id` is shown as check identity on a linked later pass; it does not prove the same inputs, task, or recovery. Without that ID, the relationship remains unknown. Earlier imported failures remain in saved analysis findings. A run investigation leads with a positively recorded tool failure or timeout before advice about call/result records that cannot be paired one-to-one; both link to the affected evidence. Neither advice establishes cause or later recovery. Other recorded failures without a specific recommendation offer an evidence-linked recheck. The inbox works without an LLM key or external notification destination. A saved schedule still needs a running `verdict-service` worker for future cycles. Readiness reports heartbeat, worker-only next eligibility time, and latest cycle outcome separately; a started cycle without a finish is unknown, while a manual success does not prove a recurring worker ran. Pause stops later worker cycles without stopping its heartbeat; a cycle already selected may finish. Resume requires a fresh local check, and a legacy plan without an explicit judge choice defaults to no paid judging when reviewed for resume. The one-shot worker rejects a saved revision that changed or paused before its cycle lock was acquired. A transient cycle or initial heartbeat error does not terminate the continuous worker; cycle failures record a cooldown, and presence is shown only after a successful heartbeat write.

New suggested plans leave local history directory fields blank. **Run once and review** analyzes already captured evidence unless you enter a Claude Code or Codex directory to import or rescan. Source labels on stored runs do not establish a directory on the dashboard machine. Existing saved paths are preserved; common directories appear only as input placeholders.

From a linked finding, **Compare runs with the same recorded source and agent** opens a bounded filtered list with its run count. Matching labels help find related evidence; they do not establish a shared cause or task. Existing source-only and agent-only run links remain available.

For a command or test notice with a recorded check ID, **Find later matching checks** suggests bounded later positive events to inspect. Select **Use these IDs** to open the attachment form with editable run and event IDs, then review the evidence before attaching; a candidate is not automatically linked or treated as recovery. The search examines at most 200 later event records, so its partial result may omit a pass. If you attach one or more later passing events to a notice, Monitoring's next step is to compare them with the failed event and verify which, if any, covers the same work. With no attached pass, follow the source-specific recheck shown there.
The **Open run evidence** and **Open recheck evidence** links retain the exact notice in the Investigation URL. That view reloads the current notice and its valid attached passes for the configured tenant, so the failed and later evidence can be compared after a page refresh; it never labels an attached pass as overall recovery.

Capture starts when `verdict.init()` installs the supported provider wrappers.
By default, bounded redacted content from calls that pass the sampling policy is
persisted during capture; `capture_content=False` is the explicit metadata-only
mode. With `buffered_writes=True`, records are queued for a background writer.
Start the dashboard against the same store. Home opens with captured activity, monitoring status, one suggested next step, recorded issues, and token usage where Agent Turn totals exist:

```bash
verdict --storage sqlite:///./verdict.db
```

If the selected SQLite or PostgreSQL store is reachable but has no Agent Runs or
LLM Traces visible for the selected tenant, the dashboard identifies it as
connected and waiting for live SDK traffic or an approved import. Browser
responses include only the backend class, never the configured path or DSN.

Reliability, Performance, Behavior, persisted evidence coverage, Agent Runs,
and the ordered event explorer are key-free. The first deterministic analysis
is stored as an immutable terminal snapshot; dashboard reads do not silently
recompute it. Repeating an unchanged check reuses that snapshot; a failed check
is shown as the latest attempt until a later successful check. A saved analysis
from before evidence revisions were recorded needs a fresh check before its
findings are treated as current. In **Model drift setup → Advanced comparison settings**, preview an older 80% versus
newer 20% count cohort or explicit event-time ranges. No grouping is the default;
provider/model facets and reviewed clusters are optional and are compared
within each selected group rather than pooled. A preview cannot
become an authoritative alert: activation starts a new empty prospective
bucket against the frozen reference.

Evaluator Lab shows NOT_EVALUABLE reasons before any model call, reads provider
keys only from environment variables, and requires an explicit egress approval.
For Jev, install `cognifity-verdict-eval[jev]`, set `TYPESAFE_API_KEY` in the
dashboard process, select **Jev**, enter a versioned model ID, and preview before
running. `jev-1.13.0` is prefilled. Moving aliases such as `jev-latest` are
rejected so calibration stays tied to one model version. Jev returns labels
without explanations; Verdict does not estimate Jev charges. The existing
Monitor statistics remain unchanged. Jev supports final Turn text but not the
recorded tool-count option.
If `TYPESAFE_BASE_URL` is set, the preview and approval show its effective URL;
Verdict pins that URL in the Jev evaluator identity and in the SDK call.
Dashboard label-set calibration sends query/response text and the rubric without
automatic redaction. Inspect the file before approving. Optional context is
ignored to match the dashboard's production judge, and labels remain local.
Labels for context-required dimensions cannot calibrate that context-free judge.
The stored label-set fingerprint covers the effective examples, excluding the
ignored context.
Select provider Trace (the backward-compatible default) for an individual
model exchange, or Agent Turn for a completed final output. The Turn path
requires present, untruncated redacted request/response text and does not
inherit provider Trace scores. Each preview scans at most 100 candidate
Turns, counts ineligible Turns in that window, and offers a cursor for older
Turns. Approve that exact page before external judge calls. A Turn/evaluator
has one bounded SQLite/Postgres score slot; a later evidence extension makes
its score stale. Agent Runs detail displays the latest valid current result
among the eight newest evaluator slots per Turn, or one exact evaluator when
selected; older results are not implied absent. This remains separate from
Trace coverage. No tool provenance or citation verification is supplied to
the Turn judge by default. To include bounded tool-use metadata, select
"Include bounded counts" before previewing Agent Turns. The preview and
egress consent then cover recorded call/result/error counts and explicit
producer-recorded dispatch-origin counts. Missing and unusable origin metadata
remain separate buckets; the event projection adds no tool names, IDs,
arguments, result text, or URLs. This mode
excludes Turns with no recorded tool events or more than 64 total events.
Origins describe only the observed outer dispatch and do not establish hidden
downstream services or protocols. Counts do not prove source support or factual
correctness and do not enable rubric dimensions that require retrieved context. A changed count makes the prior
Turn judgment stale. Trace remains the only activatable Monitor unit; the
descriptive logical-session preview can aggregate current Turn judgments
without producing an alert.
Concurrent Turn judge requests recheck currentness under a cross-process guard
before egress. A busy Turn is skipped and reported as `inProgress` ("Judge busy;
retry" in the dashboard), not counted as evaluated. SQLite uses a separate
lock file beside the evidence database and serializes Turn judging per
database without blocking evidence capture. The directory must be trusted
and writable. Postgres guards each Turn/evaluator separately. A crash,
connection loss, or provider-side retry can still result in an extra charge;
this is not an exactly-once billing guarantee.
The judge-visible call/result/error and origin counts, not the total event
count, bind score currentness. An unrelated non-tool event below the 64-event
eligibility cap does not schedule a new judge call. At more than 64 total events
the Turn is not eligible for counts-only judging. Preview and Agent Run detail
batch the bounded event-metadata reads for each Turn page.
When `OPENAI_BASE_URL` or `ANTHROPIC_BASE_URL` is set, that provider uses its
configured endpoint; the UI reports only that a custom endpoint is configured
and never returns the URL. Unknown local model names remain unpriced.
Without saved tab preferences, the default selection is every evidence-complete,
not-yet-evaluated Trace in the bounded 10,000-Trace scan; an optional numeric
cap remains available. In the same browser tab, the evaluation unit, tool-count
option, provider, model, scope, and call cap survive a refresh in session
storage. These preferences are not server-side; previews, cursors, egress
approvals, and provider credentials are not saved there. Preview shows the exact
planned calls and a rough static-price cost estimate before approval; it is
not a billing cap because actual tokenization and provider charges can exceed
it. Coverage
is specific to the exact evaluator configuration; other evaluator results are
kept separate. The approval screen names the exact dimensions that will be sent
to the judge and lists context-required dimensions that cannot run because a
stored `Trace` has no canonical retrieved-context field. If none remain, preview
fails before any provider call. Customer-labelled calibration examples may
include explicit context and can validate those dimensions separately. During a
run, the page shows activity, planned calls, and elapsed time while completed
results are stored. Treat
PASS/FAIL output as exploratory until it clears a customer-labelled calibration
set. Prompt and response presence does not establish that an imported span is
the final visible agent answer, so use a rubric appropriate for the source
operation. Verdict does not run a hidden fake judge.

The Monitor plan and Settings pages store versioned schedule, alert, settings, review, and
change-request decision documents. Recording approval on a generic decision
does not deploy an evaluator, taxonomy, cluster, or baseline; use the linked
typed workflow for that state transition. It stores environment-variable names
rather than secret values. Run one configured rescan/analysis/monitor cycle with:

```bash
verdict-service --storage sqlite:///./verdict.db --once
```

If the dashboard uses a non-default `--tenant-id`, pass the same routing ID to
`verdict-service` (for example, `--tenant-id customer-a`). The worker reads only
that tenant's saved schedule, evidence, monitoring policy, and alert settings;
the default remains `__verdict_local__`. A mismatched ID leaves the dashboard's
plan without a worker cycle. Use the same store URL in both commands.

Run that command from cron, systemd, Kubernetes, or another supervisor for
production scheduling. The continuous `verdict-service` loop is also available.
The continuous worker checks committed alert delivery on every poll, even when
an analysis cycle is not due. Monitoring flags failed, blocked, or exhausted
delivery obligations and links to **Alert destinations → Delivery status**, which shows
pending, retrying, blocked, and exhausted obligations. A changed destination
requires a reviewed rebind; an exhausted obligation can be queued for one
more attempt. The receiver must deduplicate repeated webhook POSTs by their
stable idempotency key.
Each worker drain attempts at most two sends, so a backlog progresses across
polls instead of holding the analysis lock for the entire queue. The delivery
list puts blocked, exhausted, and failed items first among loaded alerts;
**Show more alerts** pages through the rest and can reveal older problems.
A missing or malformed webhook URL waits for a valid
final HTTPS URL without aborting the worker poll.
The customer-managed topology, worker checks,
backup/restore drill, and whole-store retention boundary are in the
[single-tenant operations guide](CUSTOMER_MANAGED_OPERATIONS.md). It is not a
GA claim for the published beta.
Manual capture path approval exists only in the current dashboard process. An
explicitly saved daily schedule intentionally retains the approved source paths
as local configuration so this worker can rescan them.
Blank Claude Code and Codex source fields are treated as absent, so an
analysis-only schedule can be saved and run without local history paths.
Daily Operations shows Claude Code and Codex path controls only after local
agent history has been captured or a local schedule has been saved. A
telemetry-only store continues through Data sources or SDK ingestion instead.
Older saved `retentionDays` settings remain readable for compatibility, but
the current worker does not enforce them and the dashboard no longer offers
that control. The storage `prune_before` method covers Traces, conversations,
and eligible spans; it does not remove normalized Agent Runs, turns, events,
or their bundled copies. Manage retention for the complete customer-managed
store outside Verdict until a full deletion workflow is available.

Add `--capture-judge-telemetry` only when you intentionally want judge model
cost/latency traces written to the same store. Those traces are tagged as the
`judge` workload and excluded from future drift inputs so the evaluator does not
become part of the workload it evaluates. The flag is off by default.
This flag is unavailable for Jev because its Choice API is not captured by the
supported provider instrumentors.

For PostgreSQL, install `cognifity-verdict[dashboard,postgres]` and pass
the same protected storage URL used by the SDK. Evidence tables use Verdict's
normal additive schema initialization. The dashboard control plane lazily
creates its append-only configuration table on first use.
`python ui/server.py --db ...` remains a compatible source-checkout wrapper.

Applications that mount the dashboard can optionally pass a same-origin
`operations_url` to `verdict.dashboard.create_app()`. The host endpoint supplies
normalized infrastructure/application metrics and authorized job controls;
Verdict does not acquire cloud credentials or execute host jobs. Existing
standalone and mounted dashboards are unchanged when the option is omitted.

For a private full-page Operations application, the host composition root
mounts that package's authenticated routes and assets at `/operations`, then
passes `operations_page_url="/operations"` to `create_app()`. This adds only the
top-level link and `/api/config` field; Verdict does not load or discover the
private package. The legacy `operations_url` Settings integration remains a
separate, unchanged option.

For independent judge-health trending, add a fixed human-labeled JSONL anchor
set. Its first optional row is `{"set_name":"support-v1"}`; each remaining row
contains `sentinel_id`, `query`, `response`, optional `context`, and a `labels`
mapping whose values are `pass` or `fail`:

```bash
verdict-pipeline \
    --storage sqlite:///./verdict.db \
    --judge-provider anthropic --judge-model claude-haiku-4-5 \
    --judge-sentinel-file ./support-sentinels.jsonl \
    --judge-health-min-examples 30 --judge-health-threshold 0.80
```

The health status is `healthy` only when the independent-example floor is met
and the 95% Wilson-interval lower bound clears the threshold. One example is
correct only when every declared label matches. The interval and gate use those
exact-match examples; label-level agreement is reported separately as a
diagnostic and cannot outweigh many failed examples.
Sentinel evidence is stored separately from production judgments and cannot
rule out behavior changes outside the anchor set. Any sentinel execution error
prevents a `healthy` status: too few usable examples remain `insufficient_data`;
otherwise the status is `degraded`. When the sentinel option is
present, `degraded` or `insufficient_data` status is a hard gate: the command
persists the health record, exits 2, and does not write production judgments.
New CLI and dashboard calibration health records belong to the selected tenant.
The dashboard will not show
older, ownerless health records after an upgrade; rerun the sentinel check or
dashboard calibration for that tenant to restore its judge-health display.

The dashboard shows per-provider traffic, optional intent clusters, and pass
rates by rubric dimension. **Report** shows application-only request, tokens
processed, latency, cost, service, and model summaries; evaluator calls are
excluded, as are Verdict-owned paired-replay calls. Where exact cache evidence exists, Report separates cached and
uncached input and states the covered call count. The default period is the
last 30 UTC calendar days; 7-day, 90-day, and all-time
choices are available. The timeline contains up to 31 active dates in the
selected period; tables contain at most 20 service/environment rows and 20 model
rows. Latency coverage uses every known value in the period, while p50/p95 use
the newest 10,000 known latencies. New instrumented
traces use the `service_name` and `environment` passed to `verdict.init`;
historical and default-client traces without an explicit service identity
appear as **Unattributed**. HTML, CSV, and browser Print/Save PDF exports
use the selected period and contain aggregates only. Dates are displayed in a
readable format: period boundaries use UTC, while capture and generation times
use the browser's local time. **Model drift setup → Advanced comparison settings** previews a reviewed
reference/current comparison and the active prospective cohort; **Monitoring**
starts with worker status, recorded failures, next eligible check, and links to
usage, model-call comparison, and groups. Completed comparison results appear
before the comparison settings. Each alert
charts its reference/current rates and includes its effect, raw and adjusted
p-values, sample counts, optional facet, evidence coverage, and up to five
frozen trace examples from each cohort. Those examples identify records to
inspect; they do not establish why the change occurred. Clusters are not
required.

The dashboard reads SQLite or PostgreSQL directly. If more than one evaluator
identity is in the database, select one before reading judgment results or
creating an evaluator-backed Monitor comparison. Identity
includes provider, model list, rubric name/version, behavior-relevant config,
expected dimensions, and a prompt/rubric fingerprint. Fixed-window drift rows
created by older releases have no tenant owner, so `/api/data` and the UI
exclude them. They remain readable through the Python storage API and are
never treated as a current zero or alert. Old drift and Monitor Signals
bookmarks open Model drift setup, except drift-cluster bookmarks, which open
Groups.
Evaluator requests are sequenced and cancelled so an older response cannot
replace a newer selection. If a load fails, the dashboard explicitly names the
last confirmed evaluator that remains on screen, and trace detail is
derived from IDs in that confirmed snapshot.

In Reliability, tool-call records and reported tool-result records appear beside the tool-error count. The counts are separate observations, not a guarantee of matched calls and results; zero errors covers only results with a reported outcome. The source breakdown shows the same call/result record counts per source.

Reliability, Performance, and Behavior detail pages identify partial Agent Run and Trace scans independently when their bounded source scans omit records. Interpret the displayed counts within those stated coverage limits.

Refreshing saved analysis also updates its available-run and available-trace counts when newly captured records fall outside the bounded analyzed sample. Those coverage changes publish a new result instead of reusing an older completeness claim.

Investigation → Model calls pages newest-first through every stored non-judge application
trace in deterministic 30-row pages, breaking equal timestamps by trace ID.
Evaluator-aware filters include evaluated, not evaluated, judge error, pass,
fail, and unclear; the selected evaluator and exact Trace link survive refresh and
pagination without replacing or displacing a page row. If the selected trace is
outside the displayed page, its detail is loaded separately and the page count
still describes only the page rows. Provider and
`Content captured` or `Metadata only` filters apply to the current page. Judge
telemetry remains in aggregate cost and store totals but does not displace
application traces from this view. Selecting a row opens judge-free per-trace
facts for a recorded provider error or its absence, prompt/response coverage, response length, JSON,
refusal/apology/hedging signatures, operation, finish reason, tokens, latency,
and supplied cost. Reliability, Performance, and Behavior summarize the same
categories across the stored dataset; none of these literal checks establishes
correctness, sentiment, or response quality.
Each row uses its recorded UTC time plus relative age. A metadata-only row is
described as a **historical metadata-only trace**: this means prompt and response
were not captured when that trace was recorded, not that capture is currently
disabled. Captured empty strings remain distinct from metadata-only history.
Viewing an older SQLite trace store without monitor tables leaves that file
unchanged and reports monitoring as not configured; configuring monitoring
requires opening it through the normal writable setup path.
Provider comparison is descriptive and provider identity alone never implies a
regression.
When an authenticated FastAPI host injects
`request.state.verdict_registry_tenant`, dashboard data, Registry, Agent Run,
and deterministic-analysis requests use that tenant; the Monitor summary
embedded in `/api/data` uses the same scope. Browser query parameters cannot
select that projection. Request state does not make one mounted app a dynamic
multi-tenant control plane: setup, Evaluator Lab, Monitor lifecycle, and control
routes remain bound to the configured tenant. Mount one app instance per tenant
for those mutable workflows. A host-routed request for a different tenant cannot
obtain the setup capability, perform setup-authorized writes, or read the
process-bound Evaluator, Monitor, and control state on that mount.

The standalone dashboard projects the active registry for its configured
tenant (`--tenant-id` or `VERDICT_TENANT_ID`); without one, it keeps using each
trace's stored `cluster_id`.

Before a judge run exists, Evaluate reports evaluator coverage instead of an empty zero chart. Home presents captured run counts, a source-aware next step, recorded issues, and daily token usage when totals are available; with only recorded issues it links the latest issue day directly to its evidence. Its workspace context separately reports monitor-eligible model calls; Model drift setup shows eligible calls against all stored traces. Eligible calls are completed traces excluding Verdict judge and paired-replay traffic. When turn totals are missing but model calls have totals, Home uses a separately labeled model-call chart without assigning those tokens to sessions. Selecting an issue opens its run evidence; selecting a day opens contributing sessions and turns when that attribution exists. A saved recurring schedule is distinguished from a recently seen worker heartbeat, and an old historical monitor preview is never presented as an active no-alert result. Model drift setup shows prospective collection, pending evaluator results, insufficient evidence, alert, no alert, and stale reference as separate states. A grouped reference must have at least one metric within one group that meets the reviewed sample floor before activation. The comparison controls are under **Advanced comparison settings**. Groups begin with recorded source/provider/model evidence; semantic fitting remains an optional reviewed action when suitable model-call prompts exist.
Groups also shows the latest bounded sample of recorded Agent Run execution
statuses by exact source and agent identity. Runs without a recorded end or
terminal status are unfinalized. If the sample contains only unfinalized runs,
Groups shows one coverage explanation, a Browse recorded runs action, and one
exact example link; it
does not show an empty outcome comparison. Otherwise status examples have
exact run links. A truncation notice marks omitted older runs. This is separate
from bounded terminal-run tool
outcome coverage. A run with
recorded calls but missing, duplicate, or unmatched result IDs is unclear even
if one reported result succeeded. A reported failure remains visible. These
counts describe recorded execution evidence, not task success or a run-level
statistical monitor. Opening an unclear run with unmatched calls shows a
coverage finding and a link to its recorded call event; check the producer's
result capture before treating the run as fully observed.
Malformed or non-object historical tool-result attributes are also unclear
unless the result itself explicitly reports failure or timeout.

A selected session, run, turn, tool event, model call, or newly tenant-owned span can request an optional AI explanation after preview of the exact bounded redacted context, destination, and static list-price estimate. The model answer is an interpretation, not a quality grade or verified optimization. Existing ownerless span rows remain stored but are not shown in the selected-call span list or sent to AI. **Model drift setup → Advanced comparison settings** can also request an aggregate-only AI setup or cohort draft; applying it fills local controls without activating a policy. A selected call or turn can draft an editable rubric criterion, and a reviewed cluster can draft a label from bounded representatives. These optional provider paths require a key and explicit one-call approval. A rejected provider call is recorded as one attempt with safe error guidance; choose another provider or changed evidence for a new approved preview. Local deterministic analysis, usage, and monitoring setup do not require a key.

A mounted host that supplies the same-origin Operations adapter also exposes its action from the empty state.

Set both `VERDICT_USER` and `VERDICT_PASS` before starting the server to require
HTTP Basic authentication for the dashboard shells at `/` and `/dashboard` and
all dashboard API routes; `/api/health` remains public. A setup capability
alone does not bypass Basic Auth. Do not bind beyond localhost without that gate or a
trusted reverse proxy. Dashboard time series include only observed hourly bins
and half-hour latency bins. Presentation data is capped at the latest 100
observed chart points, 8 providers, 20 usable intent clusters, 12 dimensions,
20 evaluator identities, 20 models per displayed provider,
and one 30-row page of non-judge application traces. Investigation → Model calls can page
through the remaining application traces. The non-intent `unclustered` bucket
is excluded from the cluster chart and its cap counts. Full-store totals remain
in the summary, while a visible banner reports every shown-versus-available
capped count. A bundle that still
exceeds the redaction safety budget returns an explicit service error instead
of an empty successful dashboard.

**Two things to know so you don't think it's broken:**

- **Monitoring is periodic, not streaming.** New evidence is captured
  continuously, but a comparison advances when the monitor runs. Use
  `verdict-service` or schedule `verdict-monitor run` to keep it current.
- **Statistical comparisons need enough eligible evidence.** Minimum counts
  apply to every tested metric and optional facet. Preview smaller operationally
  meaningful thresholds only when they are defensible for the workload.
- **Intent clustering needs workload validation.** MiniLM with a `0.50`
  cosine-distance threshold is a starting point. Check the cluster-health
  status for fragmentation or underpowered clusters. If you change the
  threshold or embedding model, bump `--clustering-version`; Verdict rejects a
  reused registry whose recorded threshold or embedding dimension does not match.
  When upgrading an existing store, run once with `--recluster` so old trace IDs
  are rebuilt under the new registry. Use `--trust-existing-clusters` only when
  every judgeable trace was assigned by your own stable external clusterer.
  For the versioned registry, `verdict-cluster fit` requires a deliberate
  strategy: `explicit` is supported, while automatic `semantic` and `hybrid`
  semantic fallback are experimental opt-in features. Inspect reports the
  strategy status. The frozen semantic evaluation missed its 30% dominant-
  cluster limit by one example, so do not present it as generally validated or
  enable it silently for customers.
  The dashboard defaults the fit end to the latest eligible event and analyzes
  the preceding 90 days. Select **Analyze historical traces**, review examples
  and warnings, then select **Use these clusters**. The pinned MiniLM snapshot
  is reused from the local cache or downloaded on first use when the semantic
  extra is installed. Traces imported after the fit are assigned incrementally
  and do not change the reviewed fit membership during activation.
  The supported explicit CLI workflow stamps calls with
  `verdict.intent_context("billing.v1")`, normalizes upgraded stores in bounded
  pages, then runs fit, assign, validate, and activate. Active mode follows the
  tenant pointer. Shadow analysis is disabled pending the tenant-isolation fix
  tracked in Verdict issue #24. Tenantless Memory/SQLite uses the reserved
  `__verdict_local__` registry scope. See `packages/verdict_eval/README.md` for
  exact commands, safe error codes, inspect/rename, and rollback.
  The dashboard's **Groups** workspace is the bounded product view over those same
  tenant/version rows. It explains exact-key matches, semantic
  distance-versus-radius matches, outliers, ineligible traces, validation
  coverage, active/preview state, the frozen definition/model, representative
  redacted prompts, and provider/model mix. Per-cluster planning readiness uses
  distinct strict-UTF-8, nonempty, NUL-free session IDs of at most 256 bytes as
  of the version cutoff with the default 7-day baseline, 1-day gap, 1-day
  current window, and n=30 floor. The displayed
  remaining-traffic/time estimate is directional only—not an activation gate or
  a persisted drift result. Fragmentation and dominant semantic-cluster warnings
  require inspection/refit rather than automatic membership changes. The full
  250-cluster identity list remains visible while nested evidence is limited to
  the 20 highest-volume clusters. Standalone dashboards use the same-origin
  setup capability for typed registry actions; mounted deployments may instead
  expose mutations through their authenticated Operations adapter. The host must
  inject the authorized registry tenant rather than trusting a browser query
  parameter; that same value projects active assignments and stable labels
  throughout the tenant-scoped views.
- **Cohorts use event time.** The activatable Monitor supports one genuine
  model-call Trace per analysis unit. Monitor uses the trace's captured event
  time, not the time a judgment or import was written. A historical preview freezes its
  selected membership and normalized facts. Activation records an event-time
  boundary and opens an empty prospective bucket, so older imported events do
  not become new traffic. A selected evaluator uses the latest attempt per trace
  for one complete evaluator identity:
  provider, model list, rubric name/version, behavior-relevant configuration,
  expected dimensions, and immutable prompt/rubric fingerprint. A latest judge
  error is coverage failure rather than a PASS/FAIL score and is eligible for a
  future retry. Other evaluator definitions remain stored but are excluded.
  Fixed-window rows created by older releases remain readable through the
  Python storage API. They have no tenant owner and are excluded from
  `/api/data` and the UI; use the current Monitor workflow instead.
  Stored policies naming a non-Trace analysis unit also remain readable but
  require a new trace-based preview before execution.
- **Logical-session comparisons are descriptive.** The optional Agent view
  groups sessions whose observed runs and Turns are terminal by explicit
  `AgentRun.session_id` and uses the earliest
  observed run start for retrospective window membership. It is recomputed
  from current bounded evidence, does not persist a policy or snapshot, and
  never emits p-values, an alert decision, or an activation control.
- **Legacy mode is single-tenant per store.** Registry `active` mode requires
  `--tenant-id` and fetches only that authorized trace scope, so an
  unrelated tenant in shared PostgreSQL does not block the run. `off` retains
  the legacy mixed-tenant refusal instead of pooling scopes.
- **Dashboard cost is an estimate.** Verdict uses a dated static table of public
  base token prices. Unknown models are left unpriced, and caching, special
  tiers, provider tools, residency, and negotiated rates are not modeled.
- **Streaming has explicit persistence boundaries.** Full consumption,
  iteration error, `close()` / `aclose()`, context exit, and async cancellation
  finalize traces. A never-iterated unclosed stream that is only garbage-
  collected has no persistence guarantee. These guarantees apply only to the
  provider entry points listed in the release profile.
- **Manual/provider linkage is automatic on the supported capture path.** A
  supported instrumented provider call inside a manual span persists the
  innermost span ID on `Trace.parent_span_id`. That is the sole automatic
  direction because several provider traces can share one span; automatic
  capture does not choose one lossy reverse `SpanRecord.trace_id`. For
  manual-only work, use
  `with verdict.trace_context(existing_trace_id): ...` or
  `verdict.set_context(trace_id=existing_trace_id)`. Context is task-local and
  scoped contexts restore the prior value. A missing explicit trace ID produces
  an unlinked span with `verdict.link_status=trace_not_found`, not an orphan.
  For a separately authorized gateway correlation workflow,
  `verdict.model_call_context()` instead yields a generated ID before one call
  and assigns it to at most one supported provider Trace. Verdict does not
  inject gateway headers, and equality with the Trace ID alone does not prove a
  deployment or hardware resource.
  Buffered provider writes do not control span survival or trigger a later span
  repair: each ended span is persisted exactly once.

## What needs a key vs. not

| Capability | Provider key? |
|---|---|
| Capture (traces, tokens, latency, estimated cost, errors) | No |
| Structural cohort checks (provider error / blank captured response text / refusal) | No |
| Lexical embedding drift (built-in hash fallback) | No |
| Semantic embedding drift (local MiniLM; extra install) | No |
| Intent clustering | No |
| `verdict-inspect` semantic + structural report | No |
| Judge PASS/FAIL quality monitoring | Yes (BYOK) |
| Optional `verdict-inspect` judge sample | Yes (Anthropic BYOK) |

## Evidence and evaluation expectations

- This is a local-first beta toolkit, not a substitute for workload-specific
  calibration.
- Calibrate the judge on your own labeled traces before relying on quality
  alerts.
- Local Claude Code/Codex capture and explicit application SDK contexts produce
  typed, bounded source/run/turn/event rows. SDK provider calls can establish
  genuine LLM `Trace` links; other facts remain unavailable unless the source
  or instrumented application records them. Verdict does not independently
  prove task success, artifact state, deployments, or subagent correctness.
- Judge calls run sequentially. Previewed all-eligible or numeric call caps are
  supported; cache-aware provider-Trace pricing, human-readable cluster naming, and
  automatic cluster fusion are not implemented. See `docs/v1-roadmap.md` for
  the scoped follow-ups.
- Reproduce the validation checks yourself with the scripts here; don't take
  calibration on faith.

Questions or a capture failure on your SDK version? Open an issue with the output
of `scripts/live_capture_check.py`.
