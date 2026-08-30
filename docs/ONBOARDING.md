# Tester Onboarding

Goal: get from installation to "I can see Verdict working on my own data" in
about 10 minutes. Most of this runs **key-free**; only the judge layer needs a
provider key (bring your own — Verdict never ships one).

## 0. Prerequisites

- **Python 3.10+.** On macOS the system `/usr/bin/python3` is often 3.9 and
  will fail — use `brew install python@3.12`, `pyenv`, or `uv`.
- Optional: a provider key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or
  `GOOGLE_API_KEY`) if you want the judge / quality-drift layer. Everything
  else works without one.

## 1. Install (one chain covers all three packages)

The public-alpha distributions are `cognifity-verdict`,
`cognifity-verdict-eval`, and `cognifity-verdict-inspect`. Do not install the
unrelated `verdict` distribution from PyPI; it exposes the same import namespace
and cannot safely coexist in one environment.

```bash
uv venv --python 3.12 && source .venv/bin/activate     # or your own 3.10+ venv

# Include the provider extras you want to test live. Google capture needs `google`.
python -m pip install \
  "cognifity-verdict[anthropic,openai,google,dashboard]==0.1.0a13" \
  "cognifity-verdict-eval[semantic]==0.1.0a13" \
  "cognifity-verdict-inspect==0.1.0a13"
```

For a customer POC on the public alpha, use the pinned commands and provider
coverage matrix in [`POC_RELEASE_PROFILE.md`](POC_RELEASE_PROFILE.md).

You do **not** need a separate `pip install scipy scikit-learn` — `verdict_eval`
lists them as hard dependencies, so the line above brings them in.

Minimal alternative without the local semantic model:

```bash
python -m pip install "cognifity-verdict-eval==0.1.0a13"  # lexical hash fallback
```

Already on an earlier synchronized alpha? Use the upgrade command in the repository
[README](../README.md#upgrade-from-an-earlier-synchronized-alpha). It preserves the selected SQLite or
PostgreSQL store and does not require deleting or recloning an existing checkout.

## 2. Confirm it's healthy (30 seconds, no key)

```bash
python -c "import verdict, verdict_eval; print(verdict.__version__, verdict_eval.__version__)"
verdict-pipeline --help
verdict-dashboard --help
```

All three commands above come from the installed distributions. A Git checkout
is needed only for contributor tests and the research calibration scripts later
in this guide.

## 3. Easiest first win — `verdict-inspect` on a file you already have

This is the fastest way to see Verdict find something real, with **no
instrumentation and no code changes**. Point it at a ChatGPT or Claude.ai data
export (or any OpenAI-format message dump) and it runs locally:

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
python -m pip install "cognifity-verdict[telemetry]==0.1.0a13"

verdict-import file ./traces.ndjson --format auto \
  --storage sqlite:///./verdict.db --tenant-id my-team

verdict-pipeline --storage sqlite:///./verdict.db \
  --judge-provider anthropic --judge-model claude-haiku-4-5
verdict-dashboard --storage sqlite:///./verdict.db
```

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

## 5. Instrument your own app (the five-line pattern)

```python
import verdict
from anthropic import Anthropic     # or openai / google

verdict.init(
    service_name="my-app",
    storage="sqlite:///./verdict.db",
    buffered_writes=False,
    capture_content=False,
)
client = Anthropic()
# use the client normally — supported SDK calls are captured
```

Content capture is **off by default** (PII surface). With
`verdict.init(capture_content=True)`, supported JSON-compatible message fields
are recursively sanitized before `Trace` assignment and again at storage. This
includes nested OpenAI tool arguments and Anthropic-style tool inputs/results.
Unknown top-level provider fields are dropped; malformed, cyclic, non-JSON, and
excessively deep or large values fail closed. Repeated container references fail
closed at every occurrence so sanitized output cannot alias caller-owned data;
redacted mapping-key collisions preserve every value under deterministic
suffixed keys. The pattern detector uses Luhn validation
for card candidates and standard-library validation for IP candidates, so
trailing text that is not part of a validated IPv6 address is preserved while a
clock value such as `12:34:56` is not classified as IPv6. Email discovery uses
a linear `@`-anchored scanner to keep malformed and long inputs bounded. It
remains best effort, not a compliance control, and opaque metadata such as
tenant/session/cluster IDs must be non-sensitive. Keep content capture off for
customer POC data. The `0.1.0a13` POC profile also keeps
`buffered_writes=False`; buffered mode requires an explicit `shutdown()`
imported from `verdict.client` before process exit.

Use only the provider methods listed in the
[`POC release profile`](POC_RELEASE_PROFILE.md). Release `0.1.0a13` includes the
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
matrix in the POC release profile. Every requested provider must complete or the
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

## 7. See your results — run the pipeline, then open the dashboard

Capture starts when `verdict.init()` installs the supported provider wrappers.
By default, supported calls that pass the configured sampling policy are
persisted during capture; with `buffered_writes=True`, they are queued for a
background batched writer. **Judging and drift detection are not automatic** -
they are a batch step you run, then a dashboard you read. Two commands:

```bash
# 1. Cluster → judge a stratified sample → compute current-vs-baseline drift →
#    write drift signals back into the DB. Use --judge-provider fake to run the
#    whole pipeline key-free (the "judge" is a stub — no real quality signal).
verdict-pipeline \
    --storage sqlite:///./verdict.db \
    --judge-provider anthropic --judge-model claude-haiku-4-5

# 2. Launch the read-only dashboard and open http://127.0.0.1:8000/dashboard.
verdict-dashboard --storage sqlite:///./verdict.db
```

Add `--capture-judge-telemetry` only when you intentionally want judge model
cost/latency traces written to the same store. Those traces are tagged as the
`judge` workload and excluded from future drift inputs so the evaluator does not
become part of the workload it evaluates. The flag is off by default.

For PostgreSQL, install `cognifity-verdict[dashboard,postgres]==0.1.0a13` and pass
the same protected storage URL used by the SDK. The dashboard only reads
existing Verdict tables; it never creates or migrates them.
`python ui/server.py --db ...` remains a compatible source-checkout wrapper.

Applications that mount the dashboard can optionally pass a same-origin
`operations_url` to `verdict.dashboard.create_app()`. The host endpoint supplies
normalized infrastructure/application metrics and authorized job controls;
Verdict does not acquire cloud credentials or execute host jobs. Existing
standalone and mounted dashboards are unchanged when the option is omitted.

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
persists the health record, exits 2, and does not write production judgments or
drift signals.

The dashboard shows per-provider traffic (trace counts, error rate, latency,
tokens, **estimated cost**), intent clusters, pass-rate by dimension, and the
**drift signals** — each with its dimension, direction, effect size (Cliff's δ),
BH-adjusted p-value, sample sizes, a recommended action, and up to five current-
window evidence trace IDs. For regressions, those examples prioritize failed
traces; improvements prioritize passing traces; evaluability regressions
prioritize `UNCLEAR` traces. That's the payoff:
instead of "the model feels worse," you get "instruction_following on cluster 4
dropped, p-adj 0.003, δ −0.31," and can act — roll back a model version, fix a
prompt, or escalate.

The dashboard reads SQLite or PostgreSQL directly. If more than one evaluator
identity is in the database, select one before reading judgment or drift results. Identity
includes provider, model list, rubric name/version, behavior-relevant config,
expected dimensions, and a prompt/rubric fingerprint. Drift signals carry that
fingerprint too. Historical drift rows without it are excluded and labeled
unavailable rather than counted as zero drift. Completed analyses are persisted
as atomic run snapshots, including explicit zero-signal runs. The dashboard
shows only the latest completed snapshot for the selected evaluator; legacy
signals without a run identity are excluded rather than presented as current.
If retention removes the last defining judgment, a retained run remains
selectable by its fingerprint as a historical incomplete identity; unavailable
provider, model, and rubric details are not reconstructed.
Evaluator requests are sequenced and cancelled so an older response cannot
replace a newer selection. If a load fails, the dashboard explicitly names the
last confirmed evaluator that remains on screen, and trace/drift detail is
derived from IDs in that confirmed snapshot.

Trace Explorer pages newest-first through every stored non-judge application
trace in deterministic 30-row pages, breaking equal timestamps by trace ID.
Provider and `Content captured` or `Metadata only` filters apply to the current
page. Judge telemetry remains in aggregate cost and store totals but does not
displace application traces from this view.
Each row uses its recorded UTC time plus relative age. A metadata-only row is
described as a **historical metadata-only trace**: this means prompt and response
were not captured when that trace was recorded, not that capture is currently
disabled. Captured empty strings remain distinct from metadata-only history.
Provider comparison badges are derived only from persisted signals in the
selected completed drift run; provider identity alone never implies a regression.
When an authenticated FastAPI host injects
`request.state.verdict_registry_tenant`, Overview, Trace Explorer, cluster
pass-rate charts, and drift rows use that tenant's active-registry assignments
and stable labels. Browser query parameters cannot select that projection.
Standalone and legacy stores keep using each trace's stored `cluster_id`.

Before a drift or judge run exists, Overview, Drift, and Judge show the same live
global content-bearing trace counts instead of rendering an empty chart as zero
drift. The legacy `verdict-pipeline` defaults to a latest-24-hour current window
and a preceding 7-day baseline after a 24-hour lag, with a global minimum of 30
traces in each. `verdict-local` and `verdict-monitor` instead use equal older and
newer count cohorts over independent sessions and do not impose that n=30 gate.
Both paths still report insufficient per-cluster evidence rather than treating
it as zero drift.
`No drift analysis has completed yet`, `Completed with no signals`, and
`Completed with signals` are separate states. A mounted host that supplies the
same-origin Operations adapter also exposes the action from the empty state.

Set both `VERDICT_USER` and `VERDICT_PASS` before starting the server to require
HTTP Basic authentication for the dashboard shells at `/` and `/dashboard` plus
`/api/data`; `/api/health` remains public. Do not bind beyond localhost without that gate or a
trusted reverse proxy. Dashboard time series include only observed hourly bins
and half-hour latency bins. Presentation data is capped at the latest 100
observed chart points, 8 providers, 20 usable intent clusters, 12 dimensions,
20 evaluator identities, 20 models per displayed provider, 40 drift signals,
and one 30-row page of non-judge application traces. Trace Explorer can page
through the remaining application traces. The non-intent `unclustered` bucket
is excluded from the cluster chart and its cap counts, and capped drift signals
are ordered by absolute effect size. Full-store totals remain in the summary, while a visible
banner reports every shown-versus-available capped count. A bundle that still
exceeds the redaction safety budget returns an explicit service error instead
of an empty successful dashboard.

The overview pass-rate chart compares providers when multiple providers are
present. For a single-provider store with multiple judged intent clusters, it
compares those clusters instead, so an affected workload can be read against
the other captured workloads.

**Two things to know so you don't think it's broken:**

For a fast, judge-free POC over saved history, use count cohorts instead of the
legacy calendar-window pipeline:

```bash
verdict-monitor --storage sqlite:///verdict.db bootstrap --activate --json
verdict-dashboard --storage sqlite:///verdict.db
```

This reads all eligible historical event times, splits sessions into equal
older/newer groups, persists the initial result immediately, and derives the
future cohort size from that baseline (capped at 10). Schedule
`verdict-monitor ... run --json` for production monitoring. The notes below
about 24-hour/7-day windows and 30 judgments apply to the separate judge-based
`verdict-pipeline` path.

For histories already saved by Claude Code or Codex, the shortest local flow is:

```bash
python -m pip install "cognifity-verdict[local]==0.1.0a13"
verdict-local
```

The command reads the default `~/.claude/projects` and `~/.codex/sessions`
trees without modifying them, imports completed root turns, persists an initial
count-cohort comparison when history permits it, fits the pinned local MiniLM
model from one representative per older session, and serves
`http://127.0.0.1:8765`. Use `--claude-root`, `--codex-root`, `--source`, or
`--storage` to override discovery. Use `--no-serve --json` for automation.
The command performs a deterministic full rescan at startup; the dashboard does
not watch history files. Rerun it after new local turns, or schedule the
standalone capture and monitor commands below.

The first semantic run downloads the pinned MiniLM revision if it is not
already cached. No judge call is automatic. Add real response-quality evidence
only with an explicit budgeted invocation:

```bash
ANTHROPIC_API_KEY=... verdict-local \
  --judge-provider anthropic --judge-budget-usd 15
```

Capture is independently runnable when analysis and serving have separate
lifecycle owners. Use durable SQLite or PostgreSQL storage; local capture
rejects `memory://` because the monitor and server must reopen the same data:

```bash
verdict-agent-capture --storage sqlite:///./verdict.db
verdict-monitor --storage sqlite:///./verdict.db \
  --semantic-model-path /path/to/pinned-MiniLM-snapshot \
  bootstrap --activate --json
verdict-dashboard --storage sqlite:///./verdict.db
```

For PostgreSQL, install `cognifity-verdict[local,postgres]` and use the same
PostgreSQL URL in all three commands.

One imported Trace contains the user prompt and final visible response for one
completed root turn. The source session is pseudonymized and retained as the
independent statistical unit. Thinking, tool arguments/results, child sessions,
and arbitrary source metadata are omitted. The resulting database contains
content and must be protected even though storage applies Verdict's best-effort
redaction.

- **It's periodic, not real-time.** "Live results" means "run the pipeline, then
  refresh the dashboard." The dashboard is a read view over the DB — as fresh as
  your last pipeline run, not a streaming detector. Re-run `verdict-pipeline`
  on a schedule (cron / CI) to keep it current.
- **Judge-based drift needs volume *and* elapsed time.** The defaults want n ≥ 30
  judgments per (cluster, dimension) and a current-vs-baseline split (24h current
  vs a 7-day baseline with a 24h gap). Capturing a trickle for an afternoon gives
  you capture stats and maybe structural/semantic drift, but **no judge drift
  signals yet** — there simply isn't enough data for the statistics. That's
  expected, not a failure. `verdict-pipeline --help` lists flags
  (`--current-hours`, `--baseline-days`, `--min-sample-size`) if you want to
  shorten the windows for a quick demo on smaller data.
- **The effect-size gate is also a sensitivity floor.** The default Cliff's
  delta threshold is 0.147, which equals a 14.7 percentage-point pass-rate
  change on binary dimensions. Smaller changes do not alert regardless of
  sample size. Use `--effect-size-threshold` only after choosing the smallest
  operationally meaningful change for your workload.
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
  semantic fallback are experimental opt-in alpha features. Inspect reports the
  strategy status. The frozen semantic evaluation missed its 30% dominant-
  cluster limit by one example, so do not present it as generally validated or
  enable it silently for customers.
  The supported explicit workflow stamps calls with
  `verdict.intent_context("billing.v1")`, normalizes upgraded stores in bounded
  pages, then runs fit, assign, validate, and activate. Active mode follows the
  tenant pointer. Shadow analysis is disabled pending the tenant-isolation fix
  tracked in Verdict issue #24. Tenantless Memory/SQLite uses the reserved
  `__verdict_local__` registry scope. See `packages/verdict_eval/README.md` for
  exact commands, safe error codes, inspect/rename, and rollback.
  The dashboard Registry tab is the bounded product view over those same
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
  the 20 highest-volume clusters. Standalone dashboards are read-only by
  default; mounted deployments may expose mutations only through their
  authenticated, CSRF-protected Operations adapter. The host must inject the
  authorized registry tenant rather than trusting a browser query parameter;
  that same value projects the active assignments and stable labels throughout
  the rest of the dashboard.
- **Windows use capture time.** The pipeline places judgments into windows using
  the associated trace's `started_at`, not the time the judgment was created.
  Re-running the same hourly analysis bucket replaces that bucket's signals, so
  a signal is removed if it no longer clears the gates after new evidence arrives.
  The completed run marker and exact signal set are one atomic snapshot, and a
  clean run is stored explicitly with zero signals. Consumers select the latest
  completed snapshot instead of treating all historical signals as current.
  A rerun uses the latest attempt per trace for one complete evaluator identity:
  provider, model list, rubric name/version, behavior-relevant configuration,
  expected dimensions, and immutable prompt/rubric fingerprint. A latest judge
  error is coverage failure rather than a PASS/FAIL score and is eligible for a
  future retry. Other evaluator definitions remain stored but are excluded.
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
  provider entry points listed in the POC release profile.
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
  Buffered provider writes do not control span survival or trigger a later span
  repair: each ended span is persisted exactly once.

## What needs a key vs. not

| Capability | Provider key? |
|---|---|
| Capture (traces, tokens, latency, estimated cost, errors) | No |
| Structural checks (refusal / JSON / length / latency drift) | No |
| Lexical embedding drift (built-in hash fallback) | No |
| Semantic embedding drift (local MiniLM; extra install) | No |
| Intent clustering | No |
| `verdict-inspect` semantic + structural report | No |
| Judge PASS/FAIL quality drift | Yes (BYOK) |
| Cross-model Bradley–Terry comparison | Yes (BYOK) |

## Honest expectations for this alpha

- This is an early local-first toolkit, not a hosted monitoring service or a
  substitute for workload-specific calibration.
- Calibrate the judge on your own labeled traces before relying on quality
  alerts.
- Pairwise model rankings and PASS/FAIL drift scoring are different tasks; use
  the included alignment scripts to verify the mode you plan to rely on.
- First-class agent-run/tool-call evidence remains **v1 roadmap, not shipping**.
  Local Claude/Codex capture is a bounded prompt/final-response turn projection,
  not authoritative execution or task-success tracing.
- Judge calls run sequentially. Judge usage/budget controls, cache-token
  accounting, human-readable cluster naming, and automatic cluster fusion are
  not implemented; see `docs/v1-roadmap.md` for the scoped follow-ups.
- Reproduce the validation checks yourself with the scripts here; don't take
  calibration on faith.

Questions or a capture failure on your SDK version? Open an issue with the output
of `scripts/live_capture_check.py`.
