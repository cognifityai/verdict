# Tester Onboarding

Goal: get from `git clone` to "I can see Verdict working on my own data" in
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
git clone https://github.com/cognifityai/verdict.git
cd verdict
uv venv --python 3.12 && source .venv/bin/activate     # or your own 3.10+ venv

# Include the provider extras you want to test live. Google capture needs `google`.
pip install "cognifity-verdict[anthropic,openai,google]"
pip install "cognifity-verdict-eval[semantic]" cognifity-verdict-inspect
pip install -r ui/requirements.txt              # repo-local dashboard server
pip install pytest pytest-asyncio httpx         # only needed for the source test suite
```

You do **not** need a separate `pip install scipy scikit-learn` — `verdict_eval`
lists them as hard dependencies, so the line above brings them in.

Minimal alternative without the local semantic model:

```bash
pip install cognifity-verdict-eval   # hash fallback only; lexical, not semantic
```

## 2. Confirm it's healthy (30 seconds, no key)

```bash
python scripts/smoke_test.py     # key-free; needs only numpy + wrapt
python -m pytest -q              # full suite; needs scipy + scikit-learn (installed above)
```

Expect the smoke test to pass and the suite to report all green.

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

## 4. Instrument your own app (the five-line pattern)

```python
import verdict
from anthropic import Anthropic     # or openai / google

verdict.init(service_name="my-app", storage="sqlite:///./verdict.db")
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
for card candidates and standard-library validation for IP candidates, so a
clock value such as `12:34:56` is not classified as IPv6. Email discovery uses a
linear `@`-anchored scanner to keep malformed and long inputs bounded. It remains
best effort, not a compliance control, and opaque metadata such as
tenant/session/cluster IDs must be non-sensitive. For high volume, add
`buffered_writes=True`.

### Verify capture on your own SDK versions (recommended before you trust it)

Capture is the load-bearing piece, and provider SDK internals drift. Prove it on
*your* machine with real (tiny, ~cents) calls:

```bash
export ANTHROPIC_API_KEY=...      # and/or OPENAI_API_KEY / GOOGLE_API_KEY
python scripts/live_capture_check.py
python scripts/live_capture_check.py --providers anthropic,openai --no-streaming
```

A pass confirms traces land with tokens, estimated cost for recognized models,
finish reason, and errors populated — non-streaming and streaming — on the SDK
versions you actually have.

## 5. Judge calibration with `verdict_eval` (only if you want quality-drift)

`verdict_eval` is a library, not a separate app — you exercise it through the
calibration scripts. This is the honest per-workload step: the shippable judge
number is **your** held-out, human-labeled data, not a public benchmark.

```bash
python scripts/sample_to_label.py --source sqlite --db verdict.db --dedupe-by-prompt --out raw.jsonl
python scripts/verify_rubric_alignment.py --make-template raw.jsonl labels.jsonl
python scripts/label_ui.py --file labels.jsonl                 # local labeling UI, autosaves
python scripts/verify_rubric_alignment.py --labeled labels.jsonl --provider anthropic --judge-model <model>
```

You hand-label PASS/FAIL **blind** (before the judge runs), then the harness
reports per-dimension and pooled agreement with 95% bootstrap CIs. A threshold
counts as "cleared" only when the **CI lower bound** clears it — not the point
estimate. Needs a provider key (the judge makes calls).

## 6. See your results — run the pipeline, then open the dashboard

Capture starts when `verdict.init()` installs the supported provider wrappers.
By default, supported calls that pass the configured sampling policy are
persisted during capture; with `buffered_writes=True`, they are queued for a
background batched writer. **Judging and drift detection are not automatic** -
they are a batch step you run, then a dashboard you read. Two commands:

```bash
# 1. Cluster → judge a stratified sample → compute current-vs-baseline drift →
#    write drift signals back into the DB. Use --judge-provider fake to run the
#    whole pipeline key-free (the "judge" is a stub — no real quality signal).
python scripts/run_drift_pipeline.py \
    --storage sqlite:///./verdict.db \
    --judge-provider anthropic --judge-model claude-haiku-4-5

# 2. Launch the local dashboard and open http://127.0.0.1:8000/dashboard
#    Pass --db explicitly so it reads YOUR capture DB. (Without it the server
#    auto-picks verdict_experiment.db or verdict.db from the repo root, so a
#    leftover calibration DB could show stale data instead of your traces.)
python ui/server.py --db ./verdict.db     # FastAPI + Uvicorn (from ui/requirements.txt)
```

For independent judge-health trending, add a fixed human-labeled JSONL anchor
set. Its first optional row is `{"set_name":"support-v1"}`; each remaining row
contains `sentinel_id`, `query`, `response`, optional `context`, and a `labels`
mapping whose values are `pass` or `fail`:

```bash
python scripts/run_drift_pipeline.py \
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

For scheduled synthetic probes, `scripts/run_probes.py` requires a weighted
probe pass rate of 100% by default. Each probe's weight enters the suite and
category gate once, and a probe passes only when every declared expectation
passes. Weighted expectation agreement, including its per-dimension breakdown,
is a separate diagnostic and never the quality-gate denominator. Persisted JSON
contains both summaries under metric-schema version 3. Use `--min-pass-rate`
only for an intentionally chosen workload gate. A below-threshold run exits 1;
malformed or contradictory results fail closed, while target, follow-up, or
judge execution errors remain visible in each declared dimension and exit 2.

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

The dashboard reads SQLite directly. If more than one evaluator identity is in
the database, select one before reading judgment or drift results. Identity
includes provider, model list, rubric name/version, behavior-relevant config,
expected dimensions, and a prompt/rubric fingerprint. Drift signals carry that
fingerprint too. Historical drift rows without it are excluded and labeled
unavailable rather than counted as zero drift. Completed analyses are persisted
as atomic run snapshots, including explicit zero-signal runs. The dashboard
shows only the latest completed snapshot for the selected evaluator; legacy
signals without a run identity are excluded rather than presented as current.
Evaluator requests are sequenced and cancelled so an older response cannot
replace a newer selection. If a load fails, the dashboard explicitly names the
last confirmed evaluator that remains on screen, and trace/drift detail is
derived from IDs in that confirmed snapshot.
The SDK's Postgres adapter does not make this dashboard a Postgres read UI.

Set both `VERDICT_USER` and `VERDICT_PASS` before starting the server to require
HTTP Basic authentication for `/dashboard` and `/api/data`; `/` and
`/api/health` remain public. Do not bind beyond localhost without that gate or a
trusted reverse proxy. Dashboard time series include only observed hourly bins
and half-hour latency bins. Presentation data is capped at the latest 100
observed chart points, 8 providers, 20 clusters, 12 dimensions, 20 evaluator
identities, 20 models per displayed provider, 40 drift signals, and 30 trace
samples. Full-store totals remain in the summary, while a visible banner
reports every shown-versus-available count. A bundle that still exceeds the
redaction safety budget returns an explicit service error instead of an empty
successful dashboard.

The overview pass-rate chart compares providers when multiple providers are
present. For a single-provider store with multiple judged intent clusters, it
compares those clusters instead, so an affected workload can be read against
the other captured workloads.

**Two things to know so you don't think it's broken:**

- **It's periodic, not real-time.** "Live results" means "run the pipeline, then
  refresh the dashboard." The dashboard is a read view over the DB — as fresh as
  your last pipeline run, not a streaming detector. Re-run `run_drift_pipeline.py`
  on a schedule (cron / CI) to keep it current.
- **Judge-based drift needs volume *and* elapsed time.** The defaults want n ≥ 30
  judgments per (cluster, dimension) and a current-vs-baseline split (24h current
  vs a 7-day baseline with a 24h gap). Capturing a trickle for an afternoon gives
  you capture stats and maybe structural/semantic drift, but **no judge drift
  signals yet** — there simply isn't enough data for the statistics. That's
  expected, not a failure. `run_drift_pipeline.py --help` lists flags
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
- **The v0 runner is single-tenant per store.** It rejects a database containing
  multiple tenant scopes instead of pooling them. Use separate stores until
  tenant-scoped cluster registries and drift signals ship.
- **Dashboard cost is an estimate.** Verdict uses a dated static table of public
  base token prices. Unknown models are left unpriced, and caching, special
  tiers, provider tools, residency, and negotiated rates are not modeled.
- **Streaming has explicit persistence boundaries.** Full consumption,
  iteration error, `close()` / `aclose()`, context exit, and async cancellation
  finalize traces. A never-iterated unclosed stream that is only garbage-
  collected has no persistence guarantee.
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
- Agent-run / tool-call tracing is **v1 roadmap, not shipping** — v0 measures the
  individual LLM-call layer only.
- Judge calls run sequentially. Judge usage/budget controls, cache-token
  accounting, human-readable cluster naming, and automatic cluster fusion are
  not implemented; see `docs/v1-roadmap.md` for the scoped follow-ups.
- Reproduce the validation checks yourself with the scripts here; don't take
  calibration on faith.

Questions or a capture failure on your SDK version? Open an issue with the output
of `scripts/live_capture_check.py`.
