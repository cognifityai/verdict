# Verdict

[![Tests](https://github.com/cognifityai/verdict/actions/workflows/test.yml/badge.svg)](https://github.com/cognifityai/verdict/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/cognifity-verdict.svg)](https://pypi.org/project/cognifity-verdict/)
[![Python](https://img.shields.io/pypi/pyversions/cognifity-verdict.svg)](https://pypi.org/project/cognifity-verdict/)
[![License](https://img.shields.io/github/license/cognifityai/verdict.svg)](LICENSE)

> See what your AI agents and LLM apps actually did, and get told when their behavior changes.

Verdict is open-source, local-first monitoring for Claude Code, Codex, and LLM
applications. It runs on your machine, redacts captured content by default, and
produces findings without an API key.

A [Cognifity AI](https://cognifity.ai) project. Apache 2.0.

![Verdict dashboard showing synthetic sample drift evidence](docs/assets/verdict-dashboard-evidence-view.png)

*Bundled dashboard shown with clearly labeled synthetic sample data.*

## Try it in two minutes

```bash
python -m pip install cognifity-verdict
verdict
```

`verdict` opens a local dashboard bound to loopback. Choose **Claude Code /
Codex**, preview the history folders (`~/.claude/projects` and
`~/.codex/sessions` by default), and approve the import. Large histories take a
few minutes. Verdict makes no network calls unless you configure one: an LLM
judge with your own key, an alert webhook, a remote collector, or a hosted
telemetry import.

To capture a live application instead:

```python
import verdict

verdict.init(storage="sqlite:///./verdict.db", service_name="my-app")
# Supported Anthropic, OpenAI, and Google SDK calls are now captured.
```

## What you get without an API key

- **Agent evidence** from Claude Code and Codex history: runs, turns, tool calls,
  tool errors, possible tool loops, missing final responses, and the token usage
  the source reports.
- **LLM call capture** for supported Anthropic, OpenAI, and Google SDK methods:
  tokens, latency, estimated cost, and errors.
- **Drift alerts that do not cry wolf.** Monitor compares a reference cohort with
  a current cohort using Fisher's exact test, Benjamini–Hochberg correction, and
  a minimum effect size. Provider errors, empty replies, and refusal-like
  language are compared without a key.
- **Privacy by default.** Prompts, responses, and tool payloads are recursively
  redacted before storage. Set `capture_content=False` for metadata only.

With your own provider key you can also score responses with an LLM judge and
monitor PASS/FAIL quality drift. Semantic intent clustering is available as an
experimental option.

## What it does not do

- It is not a better judge than the model you point it at. Validate a judge on
  your own labeled data before trusting quality alerts.
- Redaction is best-effort pattern matching, not a compliance control.
- It does not infer task success that the source history does not record.
- It imports OpenTelemetry/OpenInference data but does not emit spans yet.

More detail: [honest limits](#honest-limits--not-in-v0),
[onboarding](docs/ONBOARDING.md), and the [statistics primer](docs/STATS_PRIMER.md).

---

## How it works

Verdict imports local Claude Code/Codex histories, normalizes existing telemetry,
and instruments supported Anthropic, OpenAI, and Google SDK methods. It keeps a
typed distinction between an agent session/run/turn/event and a genuine provider
LLM `Trace`; one agent turn is never relabeled as a provider call. Bounded,
best-effort-redacted content retention is on by default; explicitly set
`capture_content=False` for metadata-only capture.

The first agent-run analysis pass is deterministic and key-free: it reports
evidence coverage, source-exposed completion state, source-reported per-turn
token activity when available, observed tool/command/test failures, retries,
and possible repeated-tool patterns. Provider-call token, latency, cost, judge,
and provider-call comparison views remain limited to genuine LLM `Trace`
records. Source-declared whole-conversation pairs have a separate exploratory
comparison described below.
Programmatic policies can additionally require event
types, prohibit named tools, or require JSON responses. Verdict does not infer
task success, file state, retries, or cost when the source evidence does not
establish them.
On top of that capture Verdict also retains its existing monitoring stack:

- **Structural checks** (no LLM needed): provider errors, empty responses, and refusal-like language can be compared across reviewed cohorts.
- **Embedding drift** (no API key): MiniLM detects semantic distribution shifts; the built-in hash fallback is lexical only and is labeled as such.
- **Judge-based quality monitoring** (needs a provider key — see BYOK below): an LLM judge scores each response PASS/FAIL on a rubric; Monitor compares reviewed count-based or explicit event-time cohorts with Fisher's exact test and Benjamini–Hochberg correction. Comparisons cover all traffic by default; provider/model or reviewed clusters are optional facets.

Verdict is **not a better judge** than the model you point it at. Missing
evidence produces an unavailable/not-evaluable result before any optional judge
call. Semantic clustering is optional discovery, not a prerequisite for useful
findings or a default claim about intent.

**Scope (honest):** typed local-agent evidence supports observable execution
claims such as tool/command status and loop detection. Semantic correctness,
groundedness, and task success still require the relevant captured context,
authoritative outcomes, or a separately validated evaluator.

## Local setup details

```bash
python -m pip install cognifity-verdict
verdict
```

The `verdict` command binds to loopback, opens the packaged setup UI, and lets
you approve local Claude Code/Codex directories, import supported telemetry, or
connect an existing store. Local history capture retains bounded redacted
content by default so the first analysis is useful. The local setup wizard does
not offer a metadata-only shortcut; SDK and programmatic capture can still set
`capture_content=False` when that privacy tradeoff is intentional. Capture and
historical import remain disabled until the exact paths have been previewed in
the current server process.

After local capture, Verdict opens **Overview**. **Explore → Agent Runs & Tools**
shows local execution evidence, while **Explore → LLM Calls** shows only genuine
or imported model calls. A successful local capture can therefore show Agent
Runs while the LLM Call count remains zero. Data-source actions remain under
**Settings → Data Sources**. A manual rescan requires fresh in-process path
approval. If the user explicitly saves a daily schedule, Verdict intentionally
retains those source paths in the local control store so `verdict-service` can
rescan them; that durable schedule is configuration, not captured evidence.
When the source records child execution identity, local capture retains it as a
separate child run instead of folding its turns into the parent.
For Codex histories, text in completed `UserMessage` items is captured as the
Turn request alongside the older `user_message` format. A rescan can fill
previously missing text; non-text-only requests remain unavailable.

The findings-first dashboard has six top-level workspaces: **Overview**,
**Explore**, **Evaluate**, **Monitor**, **Report**, and **Settings**. Overview
contains Summary, Reliability, Performance, and Behavior. Monitor contains current
cohort status, historical comparisons, optional segments, and schedules. Old
fixed-window drift rows remain readable through the Python storage API, but
do not appear in the dashboard. Cluster and
monitor activation are explicit transitions; a stored historical candidate is
shown separately from the active prospective monitor and survives page reload.
Report presents application-only request, tokens processed, latency, cost,
service, and model summaries; Verdict judge and paired-replay calls are excluded. When a source
supplies exact cache evidence, the report also separates cached from uncached
input tokens and states how many calls have that breakdown. It defaults to the
last 30 UTC calendar days, with 7-day, 90-day, and all-time choices. The chart shows up
to 31 active dates in that period; tables contain at most 20 service/environment
rows and 20 model rows. Latency coverage uses every known value in the period;
p50/p95 use the newest 10,000
known latencies. New instrumented traces retain the configured `service_name`
and `environment`; historical and default-client traces without an explicit
service identity appear as **Unattributed**. Aggregate-only HTML, CSV, and
browser Print/Save PDF exports use the selected period and never include prompts
or responses. Period boundaries use UTC dates; capture and generation times use
the browser's readable local format.
Evaluate includes a one-off Inspect view for uploaded or pasted ChatGPT,
Claude.ai, Cowork, and OpenAI JSON/JSONL. Analysis runs on the Verdict dashboard
host and does not add the source export or report to the Verdict store.
A Trace reports execution success/error
separately from evaluation states: `not evaluated`, `judge error`, `pass`,
`fail`, or `unclear`. No drift conclusion is shown until a comparison is
persisted. Activating a monitor candidate retires other previews for the same
scope so an abandoned comparison cannot hide the active result.

Historical imports performed by the local setup UI enter the same local
workspace used by analysis and Evaluator Lab. Judge-free trace analysis reports
provider success/failure, evidence coverage, operation and finish-reason
counts, tokens, latency, supplied cost, and structural response signatures.
Agent Run outcomes remain unavailable when the imported source contains LLM
traces but no run/turn/event hierarchy.
New historical comparisons default to these deterministic trace checks;
selecting a stored evaluator is an explicit choice.

The equivalent non-interactive local import is:

```bash
verdict-import local --storage sqlite:///./verdict.db
verdict-dashboard --storage sqlite:///./verdict.db
```

### Instrument a complete agent run

Provider auto-instrumentation captures genuine LLM calls. Wrap the surrounding
application work to add the run, turn, tool, command, test, retry, feedback, and
business-outcome evidence that only the application can establish:

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
        answer = respond(user_message, order)  # supported LLM calls auto-link
        turn.set_output(answer)
    run.record_business_outcome("resolved", True)
```

An explicit boolean `False` business outcome is retained as a failed outcome
event and appears as a deterministic `business_outcome_failed` Agent Insight.
This status remains available when content capture is disabled. Other outcome
values remain descriptive; Verdict does not infer application-specific success
semantics from names, scores, or model responses.

The run is sampled as one unit. A supported provider call inside the active
turn creates one genuine `Trace` for its prompt/response and one model-call
event containing only operational scalars and the Trace link; LLM content is
not copied into event storage. Sync and async context managers have the same
contract. If content capture is enabled but `set_output()` is not called, the
turn records missing response evidence; disabled content is recorded separately
as not captured. Identifiers such as `tenant_id` and `session_id` must be
non-sensitive.

`ToolOrigin` records the outer dispatch boundary the application directly
observes: `MCP`, `PROVIDER_HOSTED`, or `APPLICATION`. Omit `origin` when the
producer cannot establish it. Verdict never guesses origin from a tool name.
An application wrapper may still use an unobserved downstream protocol, so an
application origin does not prove that MCP was absent behind the wrapper.

For application hosts that should not connect to the Verdict database, write
bounded, redacted, process-owned JSONL segments locally:

```python
verdict.init(
    transport="file",
    spool_directory="/var/spool/verdict/worker-1",
    service_name="support-api",
)
```

Import a stopped producer's files through the canonical idempotent storage path:

```bash
verdict-import agent-file /var/spool/verdict/worker-1 \
  --storage postgresql://verdict@db/verdict
```

Use a separate spool directory per producer process and retain files until the
import completes. This local transport has hard segment, record, and directory
bounds and carries provider Traces, Agent evidence, and manual spans through
the same selected transport. Completed appends bypass Python
userspace buffering, but are not `fsync`-ed against an operating-system or host
failure. If an Agent evidence stream fails, it stays closed to prevent sequence
gaps while later provider calls fall back to standalone Trace records. Quota or
write failures increment the process-local `capture.dropped_records` metric and
emit one bounded warning per failure class. For a central PostgreSQL deployment,
`verdict-collector` accepts authenticated, bounded batches of full `agent`
records and returns durable idempotent acknowledgements. Agent records include
their genuinely linked model-call Traces. Standalone Trace and Span
records remain on the direct and manual file-import paths. Run the host shipper
beside each producer spool:

```bash
export VERDICT_COLLECTOR_API_KEY='use-the-collector-secret'
verdict-shipper \
  --spool-directory /var/spool/verdict/worker-1 \
  --collector-url https://collector.example.com \
  --json
```

The shipper sends complete records from active segments, checkpoints only
validated acknowledgements, and deletes a segment only after the producer seals
it and every record is accepted. A segment containing standalone or malformed
records is retained with a `.rejected` suffix for inspection or manual import.
`verdict-shipper --spool-directory /var/spool/verdict/worker-1 --status --json`
reports local backlog, pending bytes, segment state, last append, receipt, and
bounded error without needing the collector or its key.
Restart an older producer with the upgraded SDK before enabling shipping;
shipping itself does not run analysis, judges, clustering, or monitors. See
[`examples/agent_sdk.py`](examples/agent_sdk.py) for a runnable local example.

An activatable monitor proposal uses one genuine model-call Trace per analysis unit
and exact event-time membership. Trace is the only unit supported by the
persisted prospective alert lifecycle. The count-mode
default is an older 80% reference and newer 20% current cohort; explicit date
ranges are also supported. Membership and the normalized metric counts used by
the comparison are frozen together. In-flight traces are excluded. An ongoing
cohort that uses a selected evaluator keeps its membership fixed while it waits
for stored evaluator results needed by like-for-like metric cells; it cannot
report “no drift” while those results are pending. Unassigned and new groups
remain coverage signals. Changed or deleted pending evidence requires a new
reviewed preview instead of silently changing the cohort. Insufficient data is
reported as `insufficient`, never as “no drift.” No clustering or judge is
required. A comparison can optionally bind one complete
existing evaluator identity and add its stored per-dimension PASS rate to the
deterministic metrics. It does not make judge calls. FAIL is included in that
rate; UNCLEAR, missing judgments, and judge errors are excluded from the
PASS/FAIL denominator and reported as coverage. Provider/model and reviewed
cluster facets are compared within each group, with correction across the full
group-by-metric family. A reviewed-cluster policy pins the exact registry
version and finishes projecting eligible new traces into that version before it
freezes monitor membership, without fitting or changing clusters. If bounded
projection work cannot finish in one run, Verdict reports that projection is
still pending and writes no monitor snapshot. Unassigned or new groups remain
visible as reference-coverage risk.
Grouped monitors support at most 250 distinct groups and reject larger
comparisons before saving a snapshot.
Activating a reviewed preview freezes its reference but starts an empty
prospective current bucket; the preview itself can never become an
authoritative alert. Scheduled looks use a summable
quadratic alpha-spending rule in addition to within-look Benjamini-Hochberg
correction. After activating one reviewed policy, schedule
the idempotent one-shot runner with cron or your existing scheduler:

```bash
verdict-monitor run --storage sqlite:///./verdict.db
```

The dashboard, one-shot monitor command, manual scheduled cycle, and continuous
service all construct monitor inputs from the same frozen evaluator and grouping
identity. They use stored judgments only and never invoke a judge implicitly.
Stored monitors that predate frozen cohort facts remain readable but must be
re-created from a reviewed preview before they can run again. The same applies
to older evaluator-backed monitors that cannot represent pending finalization
and to stored policies naming a non-Trace analysis unit.

Monitor also offers a separate **Logical session (descriptive)** preview for
native Agent evidence. It groups runs only by an explicit `AgentRun.session_id`
and compares currently terminal sessions using either an older/newer count
split or explicit event-time windows. It reports deterministic
completion/final-output rates, optional current Agent Turn judgment PASS rates,
effect sizes, and missing/in-progress coverage. It does not infer that sessions
are independent or finalized, so it emits no p-values or alert decision, saves
no policy or snapshot, and cannot be activated. Missing session identities are
never reconstructed from source-session, Trace, tag, or process metadata.
The preview fails closed above 1,000 runs or 10,000 Turns. Selecting a Turn
evaluator also caps redacted Turn text at 16 MiB and selected judgments at
10,000 / 16 MiB; counts-only tool evidence additionally caps scanned events at
100,000. The returned projection contains no Turn text or event bodies and
never silently samples an incomplete session. The evaluator selector inspects
the newest 1,000 stored Turn-result slots, shows at most 100 identities, and
reports when older slots were not inspected.

## Runs key-free; add a key for the judge (BYOK)

Verdict never ships with anyone's API key. It reads **your** provider key from the environment — bring your own key (BYOK). Critically, most of Verdict works with **no key at all**:

| Capability | Needs a provider API key? |
|---|---|
| Capture (traces, tokens, latency, estimated cost, errors) | **No** |
| Local Claude/Codex run, turn, tool, command, and evidence findings | **No** |
| Import existing telemetry into Verdict | **No** (source APIs need their own credentials) |
| Structural checks (refusal/JSON/length/latency drift) | **No** |
| Lexical embedding drift (built-in hash fallback) | **No** |
| Semantic embedding drift (local MiniLM; extra install) | **No** |
| Intent clustering | **No** |
| Judge-based PASS/FAIL quality drift | **Yes** (your key) |
| Optional `verdict-inspect` judge sample | **Yes** (Anthropic BYOK) |

After installation, capture and structural checks can run without a provider key. The built-in hash embedder can report lexical embedding-distribution changes, but it is not a semantic model and may split paraphrases into separate intent clusters. Install the local `sentence-transformers/all-MiniLM-L6-v2` extra shown below for semantic intent clustering and semantic drift. Capture never invokes a judge automatically. A provider-backed judge run requires that provider's key; `verdict-inspect` skips its optional Anthropic judge when no Anthropic key is set.

```bash
export ANTHROPIC_API_KEY=...     # or OPENAI_API_KEY / GOOGLE_API_KEY / TYPESAFE_API_KEY
```

Evaluator Lab can use Jev after installing
`cognifity-verdict-eval[jev]` and setting `TYPESAFE_API_KEY` in the dashboard
process. Choose **Jev** in its provider menu, enter a versioned model ID
(`jev-1.13.0` is the prefilled default), preview the exact eligible calls,
and approve external egress before running. Jev returns PASS/FAIL/UNCLEAR labels
without explanatory reasoning. Its pricing is unavailable in Verdict, so the
preview does not show a cost estimate. Jev supports Trace and final Agent Turn
text judging and label-set calibration; recorded Turn tool counts are unavailable.
Monitor continues to use its existing cohort comparison and Fisher test.
The optional `TYPESAFE_BASE_URL` selects a Jev endpoint. Evaluator Lab shows the
effective URL before approval and keeps results from different URLs and model
versions in separate evaluator identities. Moving aliases such as `jev-latest`
are rejected because they do not identify one fixed calibration target.
Dashboard calibration sends the label set's query and
response text plus the rubric without applying Verdict redaction; inspect the
file before approving. Optional context is ignored because dashboard production
judging has no retrieved context, and human labels stay local.

The judge can use Anthropic, OpenAI, Google, Jev, or a local/self-hosted OpenAI-compatible model. Jev uses its structured Choice API; the other built-in judges use provider completions. Validate the chosen judge against held-out labels from the intended workload before relying on quality alerts.

## Install

**Requires Python 3.10+.** On macOS the system `/usr/bin/python3` is often 3.9 and will fail to install — use a 3.10+ interpreter (`brew install python@3.12`, `pyenv`, or `uv venv --python 3.12`).

Install the synchronized public beta from PyPI. Choose only the provider extras
you use:

```bash
python -m pip install \
  "cognifity-verdict[anthropic,openai,google,dashboard]==0.1.0b1" \
  "cognifity-verdict-eval[semantic]==0.1.0b1" \
  "cognifity-verdict-inspect==0.1.0b1"
```

For a bounded pilot on `0.1.0b1`, follow the
[release profile](docs/POC_RELEASE_PROFILE.md). It names the provider entry
points exercised for this release, keeps persistence synchronous, and separates
a workflow demonstration from a production-readiness claim.

To let a coding agent instrument an application, use the
[`verdict-instrument-app` agent skill](docs/AGENT_POC_SKILL.md). The guide
includes a cross-agent prompt, approval boundaries, staged acceptance criteria,
and the current automation limits.

Extras for `cognifity-verdict`: `anthropic`, `openai`, `google`, `postgres`,
`telemetry`, or `dashboard`. The `telemetry` extra adds OTLP protobuf decoding;
JSON/JSONL imports and hosted API readers use the Python standard library.
Google capture specifically needs the `google` extra
(`google-genai`). Install `dashboard` with `postgres` when the dashboard reads a
PostgreSQL store:

```bash
python -m pip install \
  "cognifity-verdict[dashboard,postgres]==0.1.0b1" \
  "cognifity-verdict-eval==0.1.0b1" \
  "cognifity-verdict-inspect==0.1.0b1"
```

The dashboard server is part of the core distribution because the core
`verdict` command launches it. The `dashboard` extra is retained as an empty
compatibility selector; `all` adds provider, PostgreSQL, telemetry, and eval
dependencies on top of that core runtime.

The release workflow also publishes the verified dashboard image as
`ghcr.io/cognifityai/verdict:0.1.0b1`. It contains Verdict, not PostgreSQL or
application data, and includes the supported judge provider SDKs. Set
`VERDICT_STORAGE` to the deployment's database and
configure dashboard access before binding it outside loopback.

### Upgrade from an earlier synchronized alpha

Upgrade the synchronized distributions in the application's existing virtual
environment. This also replaces editable installs from an existing Verdict clone;
do not delete or reclone it:

```bash
python -m pip install --upgrade \
  "cognifity-verdict[anthropic,openai,google,dashboard]==0.1.0b1" \
  "cognifity-verdict-eval[semantic]==0.1.0b1" \
  "cognifity-verdict-inspect==0.1.0b1"

python -m pip check
python -c "import verdict, verdict_eval, verdict_inspect; print(verdict.__version__, verdict_eval.__version__, verdict_inspect.__version__)"
```

Add the same provider, semantic, and PostgreSQL extras that deployment already
uses. The upgrade reuses existing SQLite files and PostgreSQL tables in place;
it does not delete or rewrite traces, judgments, calibration records, drift
runs, or dashboard history. It does not move SQLite data to PostgreSQL or upgrade
a PostgreSQL server. Preserve the existing backend unless a separate migration is
approved. Installed deployments use the `verdict-pipeline` and
`verdict-dashboard` commands rather than source-tree wrappers.
If an older alpha created a `user_signals` table, the upgrade leaves it in place
as unread legacy data; no destructive migration is run.
Back up the store and lockfile before any alpha upgrade, then run the pipeline
and dashboard smoke checks against a non-production copy.
When upgrading a shared store to normalized agent evidence, stop and upgrade
every Verdict writer before resuming capture. The migrated database rejects
legacy agent-bundle writes rather than accepting evidence that current readers
cannot see.

An unrelated project owns the `verdict` distribution on PyPI and exposes the
same top-level `verdict` import. Do not install that distribution in the same
environment as `cognifity-verdict`; overlapping Python package paths make the
combination unsafe. Cognifity's distribution name is different, while the SDK
API remains `import verdict`.

Minimal install without the local semantic model:

```bash
python -m pip install "cognifity-verdict-eval==0.1.0b1"  # lexical hash fallback
```

The full test suite also needs pytest and the dashboard's HTTP test dependency:

```bash
pip install pytest pytest-asyncio httpx
python -m pytest -q
```

Contributor smoke test from a source checkout (needs only numpy + wrapt):

```bash
python scripts/smoke_test.py
```

## Import telemetry you already have

`verdict-import` converts existing telemetry into Verdict's current `Trace`
rows and writes them through the same SQLite/PostgreSQL storage port used by SDK
capture. Voice file import also keeps one bounded, redacted current conversation
snapshot per source identity. It does not create a raw-envelope database or replace the clustering,
sampling, judge, drift, or dashboard paths.
If a Voice transcript exceeds the snapshot's 512,000-byte stored-content
limit, Verdict keeps a bounded prefix labeled `incomplete` with a
`truncated_transcript` issue; its existing reply Trace mapping is unaffected.
The importer requires direct synchronous storage; it rejects `BufferedStorage`
so its stored count cannot be an acknowledgement of a queued write.
Conversation snapshots and grading require synchronized `0.1.0b1` core and eval builds.

Install the `telemetry` extra when accepting OTLP protobuf; it is optional for
JSON files and API readers:

```bash
python -m pip install "cognifity-verdict[telemetry,postgres]==0.1.0b1"

# JSON, JSONL, or NDJSON; use --format auto or name the source explicitly.
verdict-import file ./langsmith-runs.jsonl --format langsmith \
  --storage sqlite:///./verdict.db --tenant-id support

# Existing hosted telemetry. Every API import requires a bounded source-time window.
export LANGFUSE_PUBLIC_KEY=...
export LANGFUSE_SECRET_KEY=...
verdict-import langfuse --from 2026-08-01T00:00:00Z --to 2026-08-02T00:00:00Z \
  --storage postgresql://user:pass@host/verdict --tenant-id support

# Loopback OTLP/HTTP JSON or protobuf receiver (POST /v1/traces).
verdict-import receive-otlp --storage sqlite:///./verdict.db
```

Supported readers are OTLP/HTTP and OTLP JSON (current/legacy `gen_ai.*`,
OpenInference, Vercel AI SDK call spans, and OpenLLMetry aliases), Langfuse
observations API v2, LangSmith run query/export, Datadog LLM Observability span
export, Phoenix trace export, Opik span search, MLflow 2.x/3.x trace files, and
a bounded text-only voice conversation format. See the exact commands,
environment variables, and sample files in
[`examples/telemetry/README.md`](examples/telemetry/README.md).

Langfuse's deprecated `/api/public/traces` list is intentionally not queried.
The supported Langfuse v4 reader uses the vendor-recommended bounded v2
Observations API so each generation/embedding retains its own content, tokens,
cost, and latency when available.

Import is intentionally unsampled: every eligible LLM call is normalized and
stored, while duplicates from retries resolve to the same tenant/source-scoped
ID (including the parent source trace ID when present). The existing pipeline
decides which stored traces to judge. Missing optional
tokens, cost, end time, model, session, or content remain `None`; Verdict does
not invent them. Records without a stable source ID or valid start time are
skipped with an explicit reason, and so is a record the storage adapter
rejects (`conversation_rejected` or `trace_rejected` in `skip_reasons`); the
import continues past it. A source or storage failure stops the run and
reports the counts reached so far. Imported prompt/response text is an explicit
content transfer: only allowlisted fields are copied and the storage boundary
applies Verdict's best-effort redaction, but operators must still treat the
Verdict database as sensitive.

Evaluator Lab also has a separate opt-in Agent Turn unit for completed,
untruncated Turns with a present redacted request and final response. It judges
the Turn's final output without making a provider Trace or a Trace Judgment.
Preview examines at most 100 tenant-visible candidate Turns (including
ineligible ones) per keyset page and requires explicit approval of the planned
calls and external egress. SQLite/Postgres keep one bounded result slot per
Turn and evaluator; a changed Turn makes the old score stale until reevaluated.
Agent Runs detail shows the latest valid current Turn result among the eight
newest evaluator slots per Turn, or one exact evaluator fingerprint when
selected. Older results are not implied absent by this bounded view. Trace
coverage and pipelines remain Trace-based. The descriptive logical-session
Monitor preview can aggregate current native Turn judgments, but Turn judging
does not provide tool/citation provenance by default. An explicit
`toolEvidence: "counts_v1"` Turn option
sends only bounded counts of recorded tool calls, results, reported errors,
unknown outcomes, and explicitly recorded dispatch origins with the redacted
Turn text. Missing or unusable origin metadata is counted separately. The tool-event projection
does not add names, IDs, arguments, result bodies, or URLs to judge input.
Turns with no recorded tool events or more than 64 total events are ineligible
in this mode. Origin counts establish only the producer-recorded outer dispatch
boundary; they cannot establish hidden downstream protocols. Tool counts are
not retrieved source context and cannot establish citation support, claim
accuracy, or complete tool coverage; context-required rubric dimensions
remain skipped. Preview, approval, durable result, and Turn detail bind the
same counts snapshot, while default Turn and provider Trace results retain
their existing identities. Turn judging does not provide citation provenance or
claim verification, and the logical-session preview makes no
independent-session statistical claim. A completed
Turn result contains one score per evaluable rubric dimension; malformed or
partial stored results are not counted as judged and require a new approved run.

Files are bounded to 64 MiB for JSON and 16 MiB per NDJSON row; hosted API
responses are bounded to 64 MiB; the OTLP listener defaults to a 16 MiB request
cap. Content is bounded to 1,000 messages and 100,000 UTF-8 characters per
input/output direction. For retry-stable IDs after moving a file, pass a stable,
non-secret `--source-scope`; the file default is its absolute path.

## Five-line install pattern

```python
import verdict
from anthropic import Anthropic

verdict.init(
    service_name="my-app",
    storage="sqlite:///./verdict.db",
    buffered_writes=False,
    capture_content=True,
)
client = Anthropic()
# Use Anthropic normally; supported calls are captured.
# Run verdict-pipeline separately for optional clustering and judging.
```

Open the dashboard for a local SQLite store:

```bash
verdict-dashboard --storage sqlite:///./verdict.db
```

If capture or import used an explicit tenant, use the same value for the
dashboard. The dashboard also reads `VERDICT_TENANT_ID` when the flag is
omitted. Dashboard-selectable tenant IDs use 1–128 ASCII letters, digits, `.`,
`_`, `:`, or `-`, beginning with a letter or digit. Existing telemetry import
APIs continue to accept the published 256-character routing boundary; use at
most 128 characters for a new standalone dashboard workspace:

```bash
verdict-import local --storage sqlite:///./verdict.db --tenant-id support
verdict-dashboard --storage sqlite:///./verdict.db --tenant-id support
```

The process-selected tenant is one standalone workspace boundary for dashboard
totals, reports, trace/judgment samples, Agent Run reads, setup/import,
Evaluator Lab, clusters, Monitor, and control actions. Browser `tenant=`
parameters are ignored. The reserved default remains `__verdict_local__` and
includes historical tenantless traces; changing the selection does not move or
rewrite existing rows. Legacy fixed-window drift rows have no tenant owner and
do not appear in `/api/data` or the UI; use the current tenant-scoped Monitor workflow.

An authenticated FastAPI host may set
`request.state.verdict_registry_tenant` to choose the authorized tenant for
dashboard data, Registry, Agent Run, and deterministic-analysis requests. That
state also scopes the Monitor summary embedded in `/api/data`. It is not a
dynamic multi-tenant control plane: setup, Evaluator Lab, Monitor lifecycle,
and control routes remain bound to the process-selected tenant. Mount one app
instance per tenant when those mutable workflows must be tenant-specific.
When a host supplies a different tenant for a request, setup capability and
its write actions, plus process-bound Evaluator, Monitor, and control reads,
are unavailable for that request.

The Overview and explorer APIs do not mutate trace/judgment history. Setup,
capture, import, and Monitor actions are explicit write operations; do not
expose the standalone server beyond loopback without an authenticated host.
Non-loopback binding is refused unless HTTP credentials and an explicit
`VERDICT_ALLOWED_HOSTS` allowlist are both configured.

Run the installed analysis pipeline without cloning this repository:

```bash
verdict-pipeline --storage sqlite:///./verdict.db \
    --judge-provider anthropic --judge-model claude-haiku-4-5
```

This command prepares clusters and judgments; it does not create a second set
of drift results. Use **Monitor → Compare History** to choose the reference and
current cohorts, inspect the alert-first drift analysis, then activate that
reviewed policy for new traffic. Completed comparisons chart reference against
current rates and report effect, raw and adjusted p-values, eligible counts,
coverage, and up to five frozen evidence traces from each cohort for every
alert. Evidence examples support investigation; Verdict does not claim they
establish a root cause.

The same command accepts a PostgreSQL URL when the `postgres` extra is
installed. Applications can instead mount `verdict.dashboard.create_app()`
inside an existing FastAPI service; the browser API resolves relative to the
mount path, so the packaged UI and server stay on the same version. When the
authenticated host supplies `request.state.verdict_registry_tenant`, dashboard
data, Registry, Agent Run, and deterministic-analysis requests use that tenant,
including assignments and stable labels from its active registry. Standalone
and legacy stores use the dashboard's configured tenant and project its active
registry when one exists; without an active registry, they continue to use the
trace's stored `cluster_id`.

Content capture is **on by default** and is a PII surface. Verdict recursively sanitizes supported
JSON-compatible message fields, including nested tool inputs/results and OpenAI
tool arguments, before content limits, `Trace` assignment, and storage. Opaque
values under supported credential fields such as `password`, `api_key`,
`token`, `secret_key`, `cookie`, `passcode`, `authorization`, and
`client_secret`, including explicit plural containers such as `passwords` and
`api_keys`, are removed using the field name. Typed Agent instruction,
context, and outcome events treat paired content as sensitive when their
semantic `name` is a supported credential field. `=` assignments, quoted
values, single-token `:` assignments, and embedded serialized forms consume
the complete sensitive value. Quote a multiword value after `:`; an unquoted
multiword colon clause is not removed wholesale based only on its label, while
independently recognized secrets are still redacted. Valid padding on Basic and
Bearer authorization values is removed with the credential, and
existing Verdict redaction/hash placeholders remain unchanged when storage
reapplies the boundary. Agent Run names and descriptive service, version, and
environment fields cross the same boundary; routing IDs remain unchanged. The detector is
best-effort pattern matching with common provider/API, GitHub, Bearer, and Basic-auth patterns,
unlabeled vendor tokens with a distinctive prefix (Stripe secret and restricted keys,
Hugging Face, GitLab, Slack, npm, PyPI, Groq, xAI, Replicate, Perplexity, LangSmith),
Luhn card checks, and standard-library IP
address validation, not a compliance control; names, addresses, many
international identifiers, and opaque application metadata are not guaranteed
to be found. Recursive Unicode key decoding uses the existing nesting budget
and fails closed beyond it. Set `capture_content=False` when that residual risk is
unacceptable; provider and manual-span failures then retain an error category
without exception message content. IPv6 validation preserves trailing text that is not part of the
validated address; clock values such as `12:34:56` are not treated as IPv6. Use
non-sensitive tenant/session/cluster IDs. `sample_rate`
controls what fraction of supported calls is retained. The `0.1.0b1` POC
profile keeps `buffered_writes=False`, so a normal process exit cannot strand
queued telemetry. `buffered_writes=True` moves writes to a background batched
writer but requires an explicit `shutdown()` imported from `verdict.client`
before process exit. The storage wrapper's `close()` drains every accepted
write before
stopping the worker; writes and reads after close raise, while a post-close
`flush()` is an idempotent no-op.

## Validation status

Reproduce the checks yourself with the scripts here. The defensible claim is
deliberately narrow:

> **Verdict captures supported real LLM calls and runs evaluator-isolated
> PASS/FAIL drift analysis; the included workflows let each team measure judge
> agreement on its own held-out labels.**

What this repo includes:

- A rubric-alignment harness for measuring PASS/FAIL judge consistency against
  your own labeled traces (`scripts/verify_rubric_alignment.py`).

The shippable number for any given team is **their** held-out, human-labeled task
data measured with the same rubric-alignment harness. Treat public benchmarks as
sanity checks, not proof that a judge is calibrated for your workload.

## Judge calibration workflow

The calibration research harness remains a source-checkout workflow; it is not
part of the normal installed runtime.

```bash
python scripts/sample_to_label.py --source sqlite --db verdict.db --dedupe-by-prompt --out raw.jsonl
python scripts/verify_rubric_alignment.py --make-template raw.jsonl labels.jsonl
python scripts/label_ui.py --file labels.jsonl          # local labeling UI, autosaves
python scripts/verify_rubric_alignment.py --labeled labels.jsonl --provider anthropic --judge-model <model>
```

`sample_to_label.py` reapplies Verdict's best-effort redaction at the JSONL
output boundary, including for legacy SQLite rows written before the current
storage sanitizer. Treat the resulting file as sensitive despite that pass.

You hand-label a sample PASS/FAIL (blind, before the judge runs), then the harness reports per-dimension and pooled agreement with 95 % bootstrap CIs. A threshold is "cleared" only when the **CI lower bound** clears it, not the point estimate.

## Architecture

Hexagonal / ports-and-adapters, ≥2 adapters per port (one real + in-memory for tests). Storage: `SQLiteStorage`, `PostgresStorage`, `InMemoryStorage`, plus a `BufferedStorage` wrapper for async batched writes. Judge choices: Anthropic, OpenAI, Google, Jev, optional LiteLLM, and a `FakeProvider` for tests. SDK capture and existing-telemetry import both produce the same **vendor-neutral `Trace` schema**. Verdict accepts OTLP/OpenInference inputs but does **not** emit OTel/OpenInference spans; an exporter remains a v1 roadmap item. See the ADRs in [`docs/adrs/`](docs/adrs/).

Optional same-process packages should depend on the versioned
`verdict.read_port` DTO/Protocol boundary, not Verdict tables, the broad storage
port, or dashboard SQL. V1 performs one tenant-scoped selected Agent Run lookup
and exposes bounded run, model-call event/Trace, and finding facts. The trusted
host remains responsible for tenant authorization. This boundary does not infer
gateway deployment or GPU identity; those require a separate exact correlation
source. See [`ADR-013`](docs/adrs/013-stable-dependent-package-read-port.md).

## Honest limits / not in v0

- **No OpenTelemetry/OpenInference span emission** yet. OTLP/OpenInference
  import is supported; exporting Verdict records is still planned.
- Hosted vendor APIs change independently. Langfuse v2, LangSmith, Phoenix, and
  Opik readers follow their documented current contracts; Datadog's LLM
  Observability export API is preview. Synthetic contract servers and fixtures
  do not substitute for a credentialed check against a customer's deployment.
- The generic voice reader maps completed assistant transcript turns and stores
  one current text-only conversation snapshot. It does not import raw audio or a
  provider's agent graph. Tokens, cost, model, and latency exist only
  when the source turn supplies them. Verify a voice vendor's export against the
  documented generic schema before relying on it. Conversation snapshots are
  available through the storage API and Evaluator Lab can grade clean, closed
  snapshots using an uploaded JSON rubric. The review screen shows coverage
  and the current transcript with its grades. Monitor can compare current
  whole-conversation binary grades across two historical windows, optionally
  by an explicit source label. This is descriptive: it creates no prospective
  alert, and source labels are not semantic clusters. **Explore → Compare**
  also supports source-declared matched whole-conversation cases when both
  variants carry an opaque pair ID and variant label. `delete_trace` deletes only a Trace;
  use `delete_conversation` for its separate snapshot. `prune_before` removes
  snapshots whose source end time, or first import time when absent, is before
  the cutoff while still returning only the number of deleted Traces. Supply
  top-level `ended_at` or `end_time` in Voice records for a source end time;
  Verdict stores its UTC form as `event_at`.

To grade imported conversations, open **Evaluate → Evaluator Lab**, select
**Conversation or reply**, upload a local JSON rubric (see
[`examples/telemetry/conversation-rubric.example.json`](examples/telemetry/conversation-rubric.example.json)
or [`examples/telemetry/response-rubric.example.json`](examples/telemetry/response-rubric.example.json)),
and preview one bounded page. The default Trace evaluator remains separate.
This workflow is included in the synchronized `0.1.0b1` core and eval builds.
Review the eligible and excluded counts before approving the selected judge
calls. The judge uses the configured provider key. When `OPENAI_BASE_URL` or
`ANTHROPIC_BASE_URL` selects a custom endpoint, the consent screen names that
destination before sending transcript content, without exposing its URL;
preview and file validation make no judge call. Uploaded rubric descriptions
and transcript text are best-effort redacted. A generic rubric defines only
binary or bounded numeric dimensions; Verdict does not execute a vendor's
custom total-score formula. Grades bind the exact current transcript, rubric,
provider, model, prompt version, and endpoint. A corrected transcript removes
its old grades. Each page scans at most 20 conversations and runs at most 20
judge calls; move through the ID-ordered pages explicitly. Full coverage means
every eligible reply was completed by that evaluator; UNCLEAR remains visible
but does not count as PASS or FAIL. Judge agreement with human labels must be
checked before treating scores as a quality measurement.

To explore conversation quality, open **Monitor → Compare History**, select
**Conversation grade (descriptive)**, and enter the evaluator fingerprint shown
in the Evaluator Lab preview plus the name of one binary dimension from its
whole-conversation rubric. Enter two non-overlapping date ranges in local time
(the result displays their UTC boundaries) and, optionally, a source label key
such as `group` or `persona`. Voice imports may
include a top-level `labels` object with up to eight short string values; missing
values appear as a separate group. The comparison reads stored current grades
without calling a judge. It shows captured, eligible, not-evaluable, PASS,
FAIL, UNCLEAR, judge-error, and ungraded-eligible counts; PASS rates use only
PASS and FAIL grades. A missing evaluator in both windows is called out rather
than silently treated as a coverage failure. Group shares are based on eligible
conversations, so inspect both mix changes and within-group
rates before interpreting an overall change. A corrected transcript or label
invalidates its old grade and can change a later preview. Choose narrower
windows if they contain more than 10,000 conversations. Conversations without
a usable source end time cannot enter a dated window. This exploratory result
is not saved or used for alerts.
Imports normalize source end times to UTC before storage. If a returned stored
row has a noncanonical end time, preview fails closed. Direct database changes
can make a row sort outside the requested window before preview sees it; repair
such snapshots by reimporting the source record.

To compare two model or prompt variants on declared matched cases, include
top-level Voice labels such as `{"pair_id":"opaque_case_1","variant":"model_a"}`
and `{"pair_id":"opaque_case_1","variant":"model_b"}` on separate imported
conversations. The producer must use the same pair ID only for the same
evaluation input, and identify whether the variant denotes assigned or
actually used configuration. In **Explore → Compare**, enter a single UTC
campaign window, the evaluator fingerprint from Evaluator Lab, one
whole-conversation rubric dimension, the pair/variant label keys, and two
variant values. Both conversations must end inside the window and have one
current grade from that same evaluator. Verdict excludes ambiguous duplicate
current conversation IDs, unmatched, ineligible, missing, error, and unusable
grades, then shows paired binary outcomes or numeric score differences and
up to 50 inspectable pairs. Numeric differences use the rubric's score range
for a conservative 95% interval, assuming independent, representative cases.
Technical-failure closures with completed replies remain grade eligible and are counted
separately. This view reads at most 10,000 selected-variant rows and does not
call a judge, save a result, generate an alert, verify identical interactive
turns, or establish a model winner. Source corrections or label edits remove
old grades and can change a later comparison; earlier reimports under the
same source ID are overwritten and cannot be detected as duplicates.
Imports normalize source end times to UTC before storage. A returned row with
a noncanonical stored time is rejected. Direct changes to the database can
make a row sort outside the requested window before comparison sees it.
If a valid numeric rubric's bounds exceed finite aggregate arithmetic, the
paired scores remain inspectable but the aggregate and interval are unavailable.
Invalid comparison requests return a generic error without exposing stored
error details; an overlarge selection asks you to choose narrower dates.
- Trace Explorer pages through every stored non-judge application trace in
  30-row pages. Search and provider/content-state filters apply to the current
  page; dashboard aggregates continue to use the complete store. Selecting a
  row keeps that bounded page visible and opens provider outcome, evidence
  coverage, response structure, tokens, latency, and supplied cost for the
  individual trace. These judge-free facts do not establish semantic quality.
- Agent Run exploration pages through every stored run in 30-row pages while
  retaining bounded event and turn detail. Finding links continue to show the
  exact affected-run set rather than applying list offsets to it.
- **Published capture coverage in `0.1.0b1`:** the bounded POC profile names
  Anthropic
  `messages.create(...)` (including `stream=True`), OpenAI
  `chat.completions.create(...)` and its stream helper, and Google
  `models.generate_content(...)` / `generate_content_stream(...)`, plus the
  Anthropic `messages.stream(...)` helper and synchronous and asynchronous OpenAI
  `responses.create(...)`, `responses.parse(...)`, and
  `responses.stream(...)` for new or existing responses as supported entry
  points. OpenAI's `responses.with_streaming_response` raw-response manager and
  the separate experimental `client.beta.responses` multi-agent resource remain
  outside this bounded support surface. See the
  [`POC release profile`](docs/POC_RELEASE_PROFILE.md) before instrumenting an
  existing application.
- Stream traces finalize deterministically on full iteration, an iteration
  error, explicit `close()` / `aclose()`, or context-manager exit. Async
  cancellation is recorded as an error. Dropping a never-iterated or unclosed
  stream and relying on garbage collection is not a supported persistence
  guarantee. On Anthropic helper and OpenAI Responses stream paths,
  `Trace.tags["verdict.stream_completion"]` distinguishes `complete`, `partial`,
  and `error` finalization.
- **`encrypt` redaction mode** is not implemented (rejected at `init()`);
  redaction uses a linear email scanner plus regex candidates, Luhn card checks,
  and standard-library IP validation. Presidio is not used.
- **Agent-run evidence is source-bounded.** Local Claude Code/Codex capture and
  explicit application SDK contexts persist source/run/turn/event bundles
  atomically. Those rows remain separate from provider `Trace` rows. When a
  source supplies exact correlation, model-call events link to the Trace that
  owns LLM request/response content; Verdict does not duplicate that content in
  the event. Run detail reads page the normalized event timeline instead of loading
  one growing serialized run. The SDK can record typed tool/result, command,
  test, artifact, retry, handoff, feedback, and outcome events supplied by the
  application. Local-history adapters remain limited to evidence present in
  their source formats. Verdict does not independently prove artifact state,
  deployment success, task outcomes that the application did not explicitly
  report, or subagent correctness. An explicit boolean-false business outcome
  is surfaced as a deterministic failure finding. Source-identified
  child histories remain distinct runs; a parent reference can remain unresolved
  when the source's parent history is no longer present. Each turn and event is
  bounded independently. Turn request/response text is redacted before its
  64 KiB preview cutoff and explicitly reports truncation. If one event's
  opted-in content exceeds its evidence limit,
  Verdict retains that event's metadata and records why its content was
  omitted; it does not downgrade the entire run. Codex turn usage is derived
  from within-turn cumulative-counter deltas; Claude usage is summed once per
  unique provider response, including cache-read and cache-creation counts. A
  Claude total appears only after the turn is terminal and every response has
  complete input/output usage.
  Malformed or unavailable counters remain unavailable rather than becoming
  zero. These local-history token counts remain observable, but
  Verdict does not convert them into API-list-price spend because desktop or
  subscription billing is not established by those files. For the standard
  `~/.codex/sessions` source, local capture also reads completed-call metadata
  directly from the sibling `~/.codex/logs_2.sqlite`; no export is required.
  Supported completion markers become metadata-only OpenAI traces containing
  model and observed response time. When one valid usage event from the same
  session matches the completion timestamp, the trace also includes its input
  and output token counts. A valid cached-input count is retained in trace
  metadata so reporting can show cached and uncached input without changing the
  trace schema. Prompt, response, latency, cost, diagnostic body,
  and raw source identifiers are never copied from diagnostics. These traces
  are not evaluator inputs and are not speculatively linked to Agent Runs.
  Codex diagnostic and session retention can differ, so their counts and token
  coverage can differ. An absent,
  malformed, symlinked, or changed diagnostic source fails closed without
  blocking the existing history import.
- A supported instrumented provider call made inside a manual span now stores
  that span's ID in `Trace.parent_span_id`. This is the sole automatic link
  direction: one manual span can contain many distinct provider calls, so no
  arbitrary provider trace is written back into `SpanRecord.trace_id`.
  Automatic correlation therefore needs no acknowledgement callback, pending
  state, or repair write, and each ended span is stored once independently of
  provider persistence. `SpanRecord.trace_id` is reserved for callers that bind
  manual-only work to an
  existing stored trace with `verdict.trace_context(trace_id)` or
  `verdict.set_context(trace_id=...)`. Missing explicit trace IDs degrade to an
  unlinked span with `verdict.link_status=trace_not_found`, rather than an orphan.
  `verdict.model_call_context()` is a separate one-call API: it yields a
  generated ID before application code runs and assigns that ID to at most one
  supported provider Trace. It injects no gateway header and does not by itself
  prove gateway, deployment, backend, or hardware identity.
  Deletion and retention preserve old spans referenced by retained traces while
  removing expired standalone and orphan spans. SQL cleanup is transactional and
  serializes concurrent trace writers while shared-span ownership is evaluated.
  This linkage is not first-class tool-sequence or task-success evaluation.
- Judge quality depends on the model, rubric, and workload; for math/code
  correctness, use stronger judges on samples or deterministic checks.
- Monitor's default minimum effect is a 10 percentage-point change in a binary
  rate. Choose it deliberately when previewing the policy; significance alone
  is not a useful operational threshold.
- Intent clustering is workload-dependent. MiniLM plus a `0.50` cosine-distance
  threshold is the shipped starting point, not a universal cutoff. Review the
  dashboard's cluster-health warning and bump `--clustering-version` whenever
  you deliberately change the threshold or embedding model. The runner rejects
  a registry whose recorded threshold or embedding dimension is incompatible.
  Existing traces whose IDs are absent from the selected registry also fail
  closed: use a one-time `--recluster` after a Verdict clustering migration, or
  `--trust-existing-clusters` only for stable clusters assigned outside Verdict.
- Versioned-registry `explicit` clustering is supported. Automatic `semantic`
  clustering and `hybrid` semantic fallback are experimental, opt-in alpha
  features: `verdict-cluster fit` requires an explicit strategy, and
  `verdict-cluster inspect` reports its experimental status. The frozen
  semantic evaluation failed one preregistered fragmentation gate (largest
  nonoutlier cluster `30.1047%` versus the `30%` maximum), although its other
  quality and stability gates passed. Do not claim general validated semantic
  quality or silently enable it in customer deployments.
  **Monitor → Segments** exposes first-fit controls even when the registry is
  empty. Its default semantic action remains labeled experimental. The
  dashboard anchors the 90-day fit range to the latest
  eligible event, uses a cached pinned MiniLM snapshot, or downloads that exact
  snapshot on first use when the semantic extra is installed. An explicit
  preview counts traces without `verdict.intent_key` as ineligible and shows
  the reason; it never invents a label.
  The dashboard's primary path is **Analyze historical traces**, review the
  exemplars and warnings, then **Use these clusters**. Validation and complete
  fit-window assignment run before activation. Later traces, including traces
  imported with older event times, are assigned incrementally without changing
  the reviewed fit membership. The supported exact-key CLI path uses
  `verdict.intent_context("billing.v1")` and is documented in
  `packages/verdict_eval/README.md`. Active analysis follows the tenant pointer;
  tenantless Memory/SQLite uses the reserved `__verdict_local__` scope. Shadow
  analysis is disabled pending the tenant-isolation correction tracked in issue
  #24.
  The packaged dashboard's **Monitor → Segments** workspace reads these immutable versions and
  shows stable labels, the frozen selector/algorithm/model definition,
  representative redacted prompts, provider/model mix, membership explanations,
  terminal reasons, coverage, and activation readiness. Per-cluster planning
  estimates count distinct strict-UTF-8, nonempty, NUL-free session IDs of at
  most 256 bytes in the default 7-day baseline / 1-day gap / 1-day current
  windows at n=30; they are diagnostic traffic estimates,
  not activation or drift results. Fragmentation and dominant semantic-cluster
  warnings prompt inspection/refit without changing immutable membership. All
  250 allowed clusters remain visible; nested evidence is limited to the 20
  highest-volume clusters so the final redaction sink stays bounded. Standalone
  mode uses its same-origin setup capability for mutations; an authenticated
  host can instead supply the Operations adapter and owns tenant authorization.
  The active registry also drives cluster labels and assignments in Overview,
  Trace Explorer, and pass-rate charts. Semantic/hybrid rows keep the experimental disclosure
  above.
- Monitor freezes a reviewed historical comparison, then records an immutable
  activation event time and starts an empty prospective bucket. Older events
  imported later are excluded from that bucket. Historical and prospective
  comparisons use the same result contract, all traffic is the default, and
  provider/model or reviewed clusters are optional facets. Existing
  fixed-window `DriftRun` and `DriftSignal` rows remain readable through the
  Python storage API. They are excluded from `/api/data` and the dashboard
  because they have no tenant owner. Old drift and Monitor Signals bookmarks
  open Monitor History, except drift-cluster bookmarks, which open Monitor
  Segments.
  Each Monitor cohort-summary metric freezes the first five true and first five
  false unit identities in cohort order. Those summaries are the single stored
  owner of the evidence IDs; the dashboard selects the relevant side for an
  alerted comparison and shows it as reference and current examples. Later
  judgments therefore cannot silently rewrite the links. Older snapshots
  without evidence identities remain readable and simply omit the trace links.
  Evaluator requests are sequenced and cancelled; a failed switch explicitly
  retains and names the last confirmed snapshot, and detail selections are
  re-derived from that snapshot rather than retaining stale objects.
  The runner reuses and aggregates at most one judgment per trace for
  one complete evaluator identity (provider, model list, rubric name/version,
  behavior-relevant config, expected dimensions, and prompt/rubric fingerprint);
  Monitor policies carry that selected evaluator fingerprint; incomplete
  historical identities and other evaluator definitions are excluded.
  Optional fixed human-labeled sentinel runs monitor that fingerprint separately
  from production drift. Pipeline and dashboard calibration runs stamp their
  health aggregate with the selected tenant; the dashboard shows it only for
  that tenant, even when another tenant
  uses the same evaluator fingerprint. Health aggregates written before tenant
  ownership was recorded are hidden in tenant dashboards and require a new
  sentinel run to appear there. When `--judge-sentinel-file` is supplied, the
  runner persists the aggregate and blocks production judging unless the status is
  `healthy`; `degraded` and `insufficient_data` both exit with status 2. The
  health gate treats one independently judged sentinel example as one trial: an
  example passes only when every declared label matches. Its Wilson confidence
  interval and minimum floor use those exact-match examples; label-level
  agreement remains a separate diagnostic. Any sentinel execution error
  prevents a `healthy` status: too few usable examples remain
  `insufficient_data`; otherwise the result is `degraded`.
- The `0.1.0b1` POC drift demonstration assumes independently sampled calls.
  Do not treat repeated turns from the same conversation as independent
  evidence or use that profile for a production decision. Use Monitor's
  descriptive logical-session preview to inspect session-level rates and
  coverage; it deliberately omits inferential significance and alert claims.
- The current local Monitor scope is tenant-bound and rejects mixed-tenant
  analysis.
- `cost_usd` is a best-effort estimate from a dated static base-price table, not
  a billing source of truth. The table includes GPT-4.1 base text models and
  their published dated snapshots; unknown and unverified fine-tuned/custom
  models remain unpriced. Caching, special tiers, tools, residency, and
  negotiated discounts are not modeled.
- Judge execution is sequential. Judge token/cost usage, evaluation-budget
  enforcement, cache-aware provider-Trace pricing, human-readable cluster naming, and
  automatic fragmented-cluster fusion are not implemented. Their scoped
  follow-ups are listed in [`docs/v1-roadmap.md`](docs/v1-roadmap.md).
- This is a **public beta** release — not a hosted monitoring service and not a substitute for workload-specific calibration.
- The bundled dashboard server is a read-only view of **SQLite or PostgreSQL**.
  It does not create or migrate schemas. Protect it with the host application's
  authentication when mounting it, or set `VERDICT_USER` and `VERDICT_PASS`
  when running the standalone server outside localhost.
- Dashboard responses keep full-store totals but bound presentation data to the
  latest 100 observed chart points, 8 providers, 20 usable intent clusters,
  12 dimensions, 20 models per displayed provider, 20 evaluator identities,
  and one 30-row page of non-judge application traces. Trace
  Explorer can page through the remaining application traces. The non-intent
  `unclustered` bucket
  is outside the cluster chart and its cap counts. A visible banner reports every capped
  count; a bundle that still exceeds the redaction safety budget returns an
  explicit service error instead of an empty successful dashboard.

## License

Apache 2.0 — see [LICENSE](LICENSE).

## Docs

- [`CHANGELOG.md`](CHANGELOG.md) — curated release changes and version history.
- [`docs/RELEASING.md`](docs/RELEASING.md) — synchronized publication,
  partial-release recovery, and the immutable rollback boundary.
- [`docs/POC_RELEASE_PROFILE.md`](docs/POC_RELEASE_PROFILE.md) — the exact
  provider, persistence, privacy, and evidence boundaries for customer POCs.
- [`docs/STATS_PRIMER.md`](docs/STATS_PRIMER.md) — plain-language explanation of the statistical methods Verdict uses for monitoring, semantic drift, and calibration.
- [`docs/EXPLAINER.md`](docs/EXPLAINER.md) — how the pipeline works end to end.
- [`docs/adrs/`](docs/adrs/) — architecture decision records.
- [`docs/v1-roadmap.md`](docs/v1-roadmap.md) — known limits and follow-up work.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a pull request. Major
decisions are documented in [`docs/adrs/`](docs/adrs/), known limits are in
[`docs/v1-roadmap.md`](docs/v1-roadmap.md), and community expectations are in
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
