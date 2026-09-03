# Verdict

[![Tests](https://github.com/cognifityai/verdict/actions/workflows/test.yml/badge.svg)](https://github.com/cognifityai/verdict/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/cognifity-verdict.svg)](https://pypi.org/project/cognifity-verdict/)
[![Python](https://img.shields.io/pypi/pyversions/cognifity-verdict.svg)](https://pypi.org/project/cognifity-verdict/)
[![License](https://img.shields.io/github/license/cognifityai/verdict.svg)](LICENSE)

> Open-source drift detection and quality monitoring for LLM-powered apps. Helps surface behavior, estimated-cost, and response-quality changes in captured production traffic.

A [Cognifity AI](https://cognifity.ai) project. Apache 2.0.

![Verdict dashboard showing synthetic sample drift evidence](docs/assets/verdict-dashboard-evidence-view.png)

*Bundled dashboard shown with clearly labeled synthetic sample data.*

---

## What this is

Verdict imports local Claude Code/Codex histories, normalizes existing telemetry,
and instruments supported Anthropic, OpenAI, and Google SDK methods. It keeps a
typed distinction between an agent session/run/turn/event and a genuine provider
LLM `Trace`; one agent turn is never relabeled as a provider call. Bounded,
best-effort-redacted content retention is on by default; explicitly set
`capture_content=False` for metadata-only capture.

The first agent-run analysis pass is deterministic and key-free: it reports
evidence coverage, source-exposed completion state, model/tool-call counts,
tokens and latency, observed tool/command failures, and possible repeated-tool
patterns. Programmatic policies can additionally require event types, prohibit
named tools, or require JSON responses. Verdict does not infer task success,
retries, file state, or cost when the source evidence does not establish them.
On top of that capture Verdict also retains its existing monitoring stack:

- **Structural checks** (no LLM needed): refusal-rate spikes, JSON-validity drops, response-length and latency drift, hedge/apology rate.
- **Embedding drift** (no API key): MiniLM detects semantic distribution shifts; the built-in hash fallback is lexical only and is labeled as such.
- **Judge-based quality drift** (needs a provider key — see BYOK below): an LLM "judge" scores each response PASS/FAIL on a rubric; Verdict clusters prompts by intent, samples, and runs non-parametric statistics (Fisher's exact, Cliff's δ, Benjamini–Hochberg) per cluster per dimension over rolling windows, emitting a drift signal only when both the configured significance and effect-size gates clear.
- **Cross-model comparison** (Bradley–Terry) and a **synthetic regression injector** for verifying the pipeline catches known corruptions.

Verdict is **not a better judge** than the model you point it at. Missing
evidence produces an unavailable/not-evaluable result before any optional judge
call. Semantic clustering is optional discovery, not a prerequisite for useful
findings or a default claim about intent.

**Scope (honest):** typed local-agent evidence supports observable execution
claims such as tool/command status and loop detection. Semantic correctness,
groundedness, and task success still require the relevant captured context,
authoritative outcomes, or a separately validated evaluator.

## Fastest local start

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

After local capture, Verdict opens **Agent runs** and turns **Setup** into
**Data sources**. The header reports Agent Runs and genuine/imported LLM Traces
separately; a successful local capture can therefore show agent evidence while
the LLM Trace count remains zero. A manual rescan requires fresh in-process path
approval. If the user explicitly saves a daily schedule, Verdict intentionally
retains those source paths in the local control store so `verdict-service` can
rescan them; that durable schedule is configuration, not captured evidence.

The findings-first dashboard exposes persisted dataset-wide evidence health,
Reliability, Performance, Behavior, ordered run/turn/event exploration,
evidence-aware judging, review queues, schedules, alerts, and a single Drift
workspace for cohort design, monitoring, completed signals, and optional
clusters. Cluster and monitor activation are explicit transitions; previews
never silently replace active state. A Trace reports execution success/error
separately from evaluation states: `not evaluated`, `judge error`, `pass`,
`fail`, or `unclear`. No drift conclusion is shown until a comparison is
persisted.

Historical imports performed by the local setup UI enter the same local
workspace used by analysis and Evaluator Lab. Judge-free trace analysis reports
provider success/failure, evidence coverage, operation and finish-reason
counts, tokens, latency, supplied cost, and structural response signatures.
Agent Run outcomes remain unavailable when the imported source contains LLM
traces but no run/turn/event hierarchy.

The equivalent non-interactive local import is:

```bash
verdict-import local --storage sqlite:///./verdict.db
verdict-dashboard --storage sqlite:///./verdict.db
```

An initial monitor proposal uses exact event-time membership. The count-mode
default is an older 80% reference and newer 20% current cohort; explicit date
ranges are also supported. Membership is frozen before metrics are compared,
and insufficient data is reported as `insufficient`, never as “no drift.” No
clustering or judge is required. A comparison can optionally bind one complete
existing evaluator identity and add its stored per-dimension PASS rate to the
deterministic metrics. It does not make judge calls. FAIL is included in that
rate; UNCLEAR, missing judgments, and judge errors are excluded from the
PASS/FAIL denominator and reported as coverage. Activating a reviewed preview freezes its
reference but starts an empty prospective current bucket; the preview itself
can never become an authoritative alert. Scheduled looks use a summable
quadratic alpha-spending rule in addition to within-look Benjamini-Hochberg
correction. After activating one reviewed policy, schedule
the idempotent one-shot runner with cron or your existing scheduler:

```bash
verdict-monitor run --storage sqlite:///./verdict.db
```

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
| Cross-model Bradley–Terry comparison | **Yes** (your key) |

After installation, capture and structural checks can run without a provider key. The built-in hash embedder can report lexical embedding-distribution changes, but it is not a semantic model and may split paraphrases into separate intent clusters. Install the local `sentence-transformers/all-MiniLM-L6-v2` extra shown below for semantic intent clustering and semantic drift. Capture never invokes a judge automatically. A provider-backed judge run requires that provider's key; `verdict-inspect` skips its optional Anthropic judge when no Anthropic key is set.

```bash
export ANTHROPIC_API_KEY=...     # or OPENAI_API_KEY / GOOGLE_API_KEY
```

The judge is pluggable behind a provider interface, so you can point it at Anthropic, OpenAI, Google, or a local/self-hosted OpenAI-compatible model. The default judge model is configurable; a cheap model (e.g. Haiku) is the recommended default, with a stronger model (e.g. Sonnet) as an accuracy upgrade.

## Install

**Requires Python 3.10+.** On macOS the system `/usr/bin/python3` is often 3.9 and will fail to install — use a 3.10+ interpreter (`brew install python@3.12`, `pyenv`, or `uv venv --python 3.12`).

Install the synchronized public alpha from PyPI. Choose only the provider extras
you use:

```bash
python -m pip install \
  "cognifity-verdict[anthropic,openai,google,dashboard]==0.1.0a15" \
  "cognifity-verdict-eval[semantic]==0.1.0a15" \
  "cognifity-verdict-inspect==0.1.0a15"
```

For a customer proof of concept on `0.1.0a15`, follow the bounded
[`POC release profile`](docs/POC_RELEASE_PROFILE.md). It names the provider
entry points exercised for this release, keeps persistence synchronous, and
separates a workflow demonstration from a production-readiness claim.

To let a customer coding agent discover and implement that POC, use the
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
  "cognifity-verdict[dashboard,postgres]==0.1.0a15" \
  "cognifity-verdict-eval==0.1.0a15" \
  "cognifity-verdict-inspect==0.1.0a15"
```

The `all` extra preserves its existing provider-and-storage dependency set; it
does not add the optional dashboard server.

### Upgrade from an earlier synchronized alpha

Upgrade the synchronized distributions in the application's existing virtual
environment. This also replaces editable installs from an existing Verdict clone;
do not delete or reclone it:

```bash
python -m pip install --upgrade \
  "cognifity-verdict[anthropic,openai,google,dashboard]==0.1.0a15" \
  "cognifity-verdict-eval[semantic]==0.1.0a15" \
  "cognifity-verdict-inspect==0.1.0a15"

python -m pip check
python -c "import verdict, verdict_eval, verdict_inspect; print(verdict.__version__, verdict_eval.__version__, verdict_inspect.__version__)"
```

Add the same provider, semantic, and PostgreSQL extras that deployment already
uses. The upgrade reuses existing SQLite files and PostgreSQL tables in place;
it does not delete or rewrite traces, judgments, calibration records, drift
runs, or dashboard history. It does not move SQLite data to PostgreSQL or upgrade
a PostgreSQL server. Preserve the existing backend unless a separate migration is
approved. The retained `scripts/run_drift_pipeline.py` and `ui/server.py` source
entry points continue as wrappers after the workspace packages are installed.
Back up the store and lockfile before any alpha upgrade, then run the pipeline
and dashboard smoke checks against a non-production copy.

An unrelated project owns the `verdict` distribution on PyPI and exposes the
same top-level `verdict` import. Do not install that distribution in the same
environment as `cognifity-verdict`; overlapping Python package paths make the
combination unsafe. Cognifity's distribution name is different, while the SDK
API remains `import verdict`.

Minimal install without the local semantic model:

```bash
python -m pip install "cognifity-verdict-eval==0.1.0a15"  # lexical hash fallback
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
capture. It does not create a raw-envelope database or replace the clustering,
sampling, judge, drift, or dashboard paths.

Install the `telemetry` extra when accepting OTLP protobuf; it is optional for
JSON files and API readers:

```bash
python -m pip install "cognifity-verdict[telemetry,postgres]==0.1.0a15"

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
skipped with an explicit reason. Imported prompt/response text is an explicit
content transfer: only allowlisted fields are copied and the storage boundary
applies Verdict's best-effort redaction, but operators must still treat the
Verdict database as sensitive.

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
# Run verdict-pipeline separately for sampling, judging, and drift.
```

Open the dashboard for a local SQLite store:

```bash
verdict-dashboard --storage sqlite:///./verdict.db
```

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

The same command accepts a PostgreSQL URL when the `postgres` extra is
installed. Applications can instead mount `verdict.dashboard.create_app()`
inside an existing FastAPI service; the browser API resolves relative to the
mount path, so the packaged UI and server stay on the same version. When the
authenticated host supplies `request.state.verdict_registry_tenant`, Overview,
Trace Explorer, cluster pass-rate charts, and drift rows use the assignments and
stable labels from that tenant's active registry. Standalone and legacy stores
continue to use the trace's stored `cluster_id`.

Content capture is **on by default** and is a PII surface. Verdict recursively sanitizes supported
JSON-compatible message fields, including nested tool inputs/results and OpenAI
tool arguments, before `Trace` assignment and again at storage. The detector is
best-effort pattern matching with common provider/API credential patterns,
Luhn card checks, and standard-library IP
address validation, not a compliance control; names, addresses, many
international identifiers, and opaque application metadata are not guaranteed
to be found. Set `capture_content=False` when that residual risk is
unacceptable. IPv6 validation preserves trailing text that is not part of the
validated address; clock values such as `12:34:56` are not treated as IPv6. Use
non-sensitive tenant/session/cluster IDs. `sample_rate`
controls what fraction of supported calls is retained. The `0.1.0a15` POC
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

- A synthetic regression battery for checking that the capture -> judge -> score
  path catches known injected failures (`scripts/run_regression_injection.py`).
- A multi-run validation that exercises stable cluster assignment, trace-time
  windows, the n>=30 cell floor, planted regressions, clean controls, and
  `UNCLEAR`-rate drift across 12 separate runs (`scripts/validate_multirun.py`).
- A pairwise judge-alignment harness for comparing model-ranking judgments
  against human-labeled public data (`scripts/verify_judge_alignment.py`). Pass
  `--json-output <path>` for its versioned machine-readable result. The
  four-judge `scripts/run_alignment_sweep.sh` writes one JSON and text report
  per judge. The wrapper never sources repository environment files; inject
  provider keys into the process from a managed secret store or OS credential
  manager. It builds `SUMMARY.md` from JSON rather than formatted prose and
  exits non-zero if any run fails, is incomplete, produces an invalid result,
  or does not clear the binarized-AC2 confidence-interval gate. Online runs
  require at least 50 requested pairs, pin the public MT-Bench dataset revision,
  and record scored/available coverage, invalid judge output, provider errors,
  incomplete ensemble components, the verdict, Gwet's AC2, Cohen's κ, and their
  95% confidence intervals. Invalid/error rows are excluded from diagnostic
  agreement metrics and force the evidence gate to fail; they are never counted
  as ties. Offline rows are labeled `SYNTHETIC WIRING ONLY` inside the headline
  table and always use a fixed 120-pair synthetic fixture; they are not
  judge-quality evidence.
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

Hexagonal / ports-and-adapters, ≥2 adapters per port (one real + in-memory for tests). Storage: `SQLiteStorage`, `PostgresStorage`, `InMemoryStorage`, plus a `BufferedStorage` wrapper for async batched writes. Judge providers: Anthropic, OpenAI, Google, optional LiteLLM, and a `FakeProvider` for tests. SDK capture and existing-telemetry import both produce the same **vendor-neutral `Trace` schema**. Verdict accepts OTLP/OpenInference inputs but does **not** emit OTel/OpenInference spans; an exporter remains a v1 roadmap item. See the ADRs in [`docs/adrs/`](docs/adrs/).

## Honest limits / not in v0

- **No OpenTelemetry/OpenInference span emission** yet. OTLP/OpenInference
  import is supported; exporting Verdict records is still planned.
- Hosted vendor APIs change independently. Langfuse v2, LangSmith, Phoenix, and
  Opik readers follow their documented current contracts; Datadog's LLM
  Observability export API is preview. Synthetic contract servers and fixtures
  do not substitute for a credentialed check against a customer's deployment.
- The generic voice reader maps completed assistant transcript turns, not raw
  audio or a provider's agent graph. Tokens, cost, model, and latency exist only
  when the source turn supplies them. Verify a voice vendor's export against the
  documented generic schema before relying on it.
- Trace Explorer pages through every stored non-judge application trace in
  30-row pages. Search and provider/content-state filters apply to the current
  page; dashboard aggregates continue to use the complete store. Selecting a
  row keeps that bounded page visible and opens provider outcome, evidence
  coverage, response structure, tokens, latency, and supplied cost for the
  individual trace. These judge-free facts do not establish semantic quality.
- **Published capture coverage in `0.1.0a15`:** the bounded POC profile names
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
- **Agent-run evidence is source-bounded.** Local Claude Code/Codex capture now
  persists session/run/turn/event projections atomically and separately from
  genuine provider `Trace` rows. It currently normalizes model, tool,
  tool-result, command, and context events exposed by the supported history
  formats; it does not yet claim authoritative artifact state, deployment
  success, or subagent correctness. If opted-in content would exceed the
  atomic evidence-row limit, Verdict keeps the metadata-only run rather than
  dropping the session. Local-history token counts remain observable, but
  Verdict does not convert them into API-list-price spend because desktop or
  subscription billing is not established by those files. Codex runs remain
  outside LLM Trace comparisons when the source does not expose genuine model
  request/response boundaries.
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
  Deletion and retention preserve old spans referenced by retained traces while
  removing expired standalone and orphan spans. SQL cleanup is transactional and
  serializes concurrent trace writers while shared-span ownership is evaluated.
  This linkage is not first-class tool-sequence or task-success evaluation.
- Judge quality depends on the model, rubric, and workload; for math/code
  correctness, use stronger judges on samples or deterministic checks.
- The default Cliff's delta gate is `0.147`. On binary PASS/FAIL dimensions that
  is a 14.7 percentage-point sensitivity floor regardless of sample size. Tune
  `--effect-size-threshold` deliberately for smaller changes.
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
  **Drift → Clusters** exposes first-fit controls even when the registry is
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
  The packaged dashboard's **Drift → Clusters** workspace reads these immutable versions and
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
  The active registry also
  drives cluster labels and assignments in Overview, Trace Explorer, pass-rate
  charts, and drift rows. Semantic/hybrid rows keep the experimental disclosure
  above.
- The batch drift runner uses each captured trace's `started_at` timestamp for
  its current and baseline windows. Every successful analysis atomically stores
  one `DriftRun` marker plus its exact signal set, including an explicit
  zero-signal run. Re-running the same hourly analysis identity replaces that
  snapshot. The dashboard reads only the latest completed snapshot for the
  selected evaluator, so an old signal cannot survive a newer clean run;
  historical signals without a run identity are unavailable, not current.
  Evaluator requests are sequenced and cancelled; a failed switch explicitly
  retains and names the last confirmed snapshot, and detail selections are
  re-derived from that snapshot rather than retaining stale objects.
  Deleting an attributed signal window removes each matched completed snapshot
  as a unit so its signal count cannot become inconsistent. The runner reuses
  and aggregates at most one judgment per trace for
  one complete evaluator identity (provider, model list, rubric name/version,
  behavior-relevant config, expected dimensions, and prompt/rubric fingerprint);
  incomplete historical identities and other evaluator definitions are excluded.
  Drift signals carry the same evaluator fingerprint; historical unattributed
  signals are shown as unavailable rather than mixed with a selected evaluator.
  Optional fixed human-labeled sentinel runs monitor that fingerprint separately
  from production drift. When `--judge-sentinel-file` is supplied, the runner
  persists the aggregate and blocks production judging/drift unless the status is
  `healthy`; `degraded` and `insufficient_data` both exit with status 2. The
  health gate treats one independently judged sentinel example as one trial: an
  example passes only when every declared label matches. Its Wilson confidence
  interval and minimum floor use those exact-match examples; label-level
  agreement remains a separate diagnostic. Any sentinel execution error
  prevents a `healthy` status: too few usable examples remain
  `insufficient_data`; otherwise the result is `degraded`.
  Signals retain up to five current-window trace IDs as review evidence.
- The `0.1.0a15` POC drift demonstration assumes independently sampled calls.
  Do not treat repeated turns from the same conversation as independent
  evidence or use that profile for a production decision.
- The v0 drift runner supports one tenant scope per store and rejects mixed-
  tenant analysis. Use separate stores until tenant-scoped cluster registries
  and signals are implemented.
- `cost_usd` is a best-effort estimate from a dated static base-price table, not
  a billing source of truth. Unknown models remain unpriced; caching, special
  tiers, tools, residency, and negotiated discounts are not modeled.
- Judge execution is sequential. Judge token/cost usage, evaluation-budget
  enforcement, cache-token accounting, human-readable cluster naming, and
  automatic fragmented-cluster fusion are not implemented. Their scoped
  follow-ups are listed in [`docs/v1-roadmap.md`](docs/v1-roadmap.md).
- This is a **public alpha** release — not a hosted monitoring service and not a substitute for workload-specific calibration.
- The bundled dashboard server is a read-only view of **SQLite or PostgreSQL**.
  It does not create or migrate schemas. Protect it with the host application's
  authentication when mounting it, or set `VERDICT_USER` and `VERDICT_PASS`
  when running the standalone server outside localhost.
- Dashboard responses keep full-store totals but bound presentation data to the
  latest 100 observed chart points, 8 providers, 20 usable intent clusters,
  12 dimensions, 20 models per displayed provider, 20 evaluator identities,
  40 drift signals, and one 30-row page of non-judge application traces. Trace
  Explorer can page through the remaining application traces. The non-intent
  `unclustered` bucket
  is outside the cluster chart and its cap counts. Drift-signal truncation keeps
  the largest absolute effect sizes. A visible banner reports every capped
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
- [`docs/STATS_PRIMER.md`](docs/STATS_PRIMER.md) — plain-language explanation of every statistical method Verdict uses (Fisher's exact, Cliff's δ, Wasserstein, PSI, Benjamini-Hochberg, Cohen's κ / Gwet's AC2, Bradley-Terry) and *why* each was chosen.
- [`docs/EXPLAINER.md`](docs/EXPLAINER.md) — how the pipeline works end to end.
- [`docs/adrs/`](docs/adrs/) — architecture decision records.
- [`docs/v1-roadmap.md`](docs/v1-roadmap.md) — known limits and follow-up work.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a pull request. Major
decisions are documented in [`docs/adrs/`](docs/adrs/), known limits are in
[`docs/v1-roadmap.md`](docs/v1-roadmap.md), and community expectations are in
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
