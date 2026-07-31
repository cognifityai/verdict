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

The public-alpha PyPI distributions will be named `cognifity-verdict`,
`cognifity-verdict-eval`, and `cognifity-verdict-inspect`. Until they are
published, use the source-checkout installation below. Do not install the
unrelated `verdict` distribution from PyPI; it exposes the same import namespace
and cannot safely coexist in one environment.

```bash
git clone https://github.com/cognifityai/verdict.git
cd verdict
uv venv --python 3.12 && source .venv/bin/activate     # or your own 3.10+ venv

# Order matters only in that each depends on the previous; install all three.
# Include the provider extras you want to test live. Google capture needs `google`.
pip install -e "packages/verdict[anthropic,openai,google]"
pip install -e packages/verdict_eval           # pulls scipy + scikit-learn automatically
pip install -e packages/verdict_inspect         # adds the `verdict-inspect` CLI
pip install -r ui/requirements.txt              # dashboard server: FastAPI + Uvicorn
pip install pytest pytest-asyncio               # only needed if you want to run the test suite
```

You do **not** need a separate `pip install scipy scikit-learn` — `verdict_eval`
lists them as hard dependencies, so the line above brings them in.

Optional: for higher-quality local semantic embeddings with
`sentence-transformers/all-MiniLM-L6-v2`, install:

```bash
pip install -e "packages/verdict_eval[semantic]"
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

Key-free, you get semantic drift with the built-in lightweight embedder plus
structural metrics (length, hedge/refusal/apology rates over time). Install the
`semantic` extra above if you want the heavier MiniLM embedder. Set
`ANTHROPIC_API_KEY` to add the PASS/FAIL judge sample; omit it (or pass
`--no-judge`) to stay fully local. Meaningful drift stats need conversations of
~30+ substantive turns per window.

## 4. Instrument your own app (the five-line pattern)

```python
import verdict
from anthropic import Anthropic     # or openai / google

verdict.init(service_name="my-app", storage="sqlite:///./verdict.db")
client = Anthropic()
# use the client normally — every call is captured
```

Content capture is **off by default** (PII surface); enable with
`verdict.init(capture_content=True)` — prompts/completions are redacted (regex +
Luhn) before storage. For high volume, add `buffered_writes=True`.

### Verify capture on your own SDK versions (recommended before you trust it)

Capture is the load-bearing piece, and provider SDK internals drift. Prove it on
*your* machine with real (tiny, ~cents) calls:

```bash
export ANTHROPIC_API_KEY=...      # and/or OPENAI_API_KEY / GOOGLE_API_KEY
python scripts/live_capture_check.py
python scripts/live_capture_check.py --providers anthropic,openai --no-streaming
```

A pass confirms traces land with tokens, cost, finish reason, and errors
populated — non-streaming and streaming — on the SDK versions you actually have.

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

Capture is live and inline: once `verdict.init()` runs, every call streams into
`verdict.db` immediately. But **judging and drift detection are not automatic** —
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

The dashboard shows per-provider traffic (trace counts, error rate, latency,
tokens, **cost**), intent clusters, pass-rate by dimension, and the **drift
signals** — each with its dimension, direction, effect size (Cliff's δ),
BH-adjusted p-value, sample sizes, and a recommended action. That's the payoff:
instead of "the model feels worse," you get "instruction_following on cluster 4
dropped, p-adj 0.003, δ −0.31," and can act — roll back a model version, fix a
prompt, or escalate.

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

## What needs a key vs. not

| Capability | Provider key? |
|---|---|
| Capture (traces, tokens, latency, cost, errors) | No |
| Structural checks (refusal / JSON / length / latency drift) | No |
| Semantic drift (built-in deterministic/hash embedder; optional MiniLM) | No |
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
- Reproduce the validation checks yourself with the scripts here; don't take
  calibration on faith.

Questions or a capture failure on your SDK version? Open an issue with the output
of `scripts/live_capture_check.py`.
