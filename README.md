# Verdict

> Open-source drift detection and quality monitoring for LLM-powered apps. Catches when the models inside your app silently change behavior, when costs move, and when response quality degrades on real production traffic.

A [Cognifity AI](https://cognifity.ai) project. Apache 2.0.

---

## What this is

Verdict instruments your LLM-powered app with one line (`wrapt` monkey-patching), so every model call is captured — model, tokens, latency, cost, finish reason, and (opt-in) redacted content. On top of that capture it adds a monitoring stack:

- **Structural checks** (no LLM needed): refusal-rate spikes, JSON-validity drops, response-length and latency drift, hedge/apology rate.
- **Semantic drift** (local embedding model, no API key): detects when your responses shift in topic/shape/length.
- **Judge-based quality drift** (needs a provider key — see BYOK below): an LLM "judge" scores each response PASS/FAIL on a rubric; Verdict clusters prompts by intent, samples, and runs non-parametric statistics (Fisher's exact, Cliff's δ, Benjamini–Hochberg) per cluster per dimension over rolling windows, emitting a drift signal only when a change is statistically real.
- **Cross-model comparison** (Bradley–Terry) and a **synthetic regression injector** for verifying the pipeline catches known corruptions.

Verdict is **not a better judge** than the model you point it at — it *uses* that model as a measuring instrument and adds the monitoring system around it (capture → cluster → sample → judge → aggregate over time → detect drift).

**Scope (v0, honest):** Verdict measures the **individual LLM-call layer**. Agent-level metrics (tool-call patterns, plan adherence, multi-step task success) are **v1 roadmap, not shipping today** — see [`docs/v1-roadmap.md`](docs/v1-roadmap.md). Do not read v0 as agent-task evaluation.

## Runs key-free; add a key for the judge (BYOK)

Verdict never ships with anyone's API key. It reads **your** provider key from the environment — bring your own key (BYOK). Critically, most of Verdict works with **no key at all**:

| Capability | Needs a provider API key? |
|---|---|
| Capture (traces, tokens, latency, cost, errors) | **No** |
| Structural checks (refusal/JSON/length/latency drift) | **No** |
| Semantic drift (built-in deterministic/hash embedder; optional MiniLM) | **No** |
| Intent clustering | **No** |
| Judge-based PASS/FAIL quality drift | **Yes** (your key) |
| Cross-model Bradley–Terry comparison | **Yes** (your key) |

So: clone it and you immediately get key-free observability + structural/semantic drift using the built-in lightweight embedder. For higher-quality local semantic embeddings with `sentence-transformers/all-MiniLM-L6-v2`, install the optional semantic extra shown below. Set a provider key to turn on judge-based quality scoring. If no key is set, the judge layer simply skips — nothing crashes.

```bash
export ANTHROPIC_API_KEY=...     # or OPENAI_API_KEY / GOOGLE_API_KEY
```

The judge is pluggable behind a provider interface, so you can point it at Anthropic, OpenAI, Google, or a local/self-hosted OpenAI-compatible model. The default judge model is configurable; a cheap model (e.g. Haiku) is the recommended default, with a stronger model (e.g. Sonnet) as an accuracy upgrade.

## Install

**Requires Python 3.10+.** On macOS the system `/usr/bin/python3` is often 3.9 and will fail to install — use a 3.10+ interpreter (`brew install python@3.12`, `pyenv`, or `uv venv --python 3.12`).

Install the public alpha from PyPI. Choose only the provider extras you use:

```bash
pip install "cognifity-verdict[anthropic,openai,google]"
pip install cognifity-verdict-eval cognifity-verdict-inspect
```

Extras for `cognifity-verdict`: `anthropic`, `openai`, `google`, `postgres`, or
`all`. Google capture specifically needs the `google` extra (`google-genai`).
The dashboard is intentionally a repo-local app; from a source checkout,
install its server dependencies with `pip install -r ui/requirements.txt`.

An unrelated project owns the `verdict` distribution on PyPI and exposes the
same top-level `verdict` import. Do not install that distribution in the same
environment as `cognifity-verdict`; overlapping Python package paths make the
combination unsafe. Cognifity's distribution name is different, while the SDK
API remains `import verdict`.

Optional higher-quality semantic embeddings:

```bash
pip install "cognifity-verdict-eval[semantic]"   # sentence-transformers / all-MiniLM-L6-v2 support
```

The full test suite also needs pytest:

```bash
pip install pytest pytest-asyncio
python -m pytest -q
```

Key-free smoke test (needs only numpy + wrapt):

```bash
python scripts/smoke_test.py
```

## Five-line install pattern

```python
import verdict
from anthropic import Anthropic

verdict.init(service_name="my-app", storage="sqlite:///./verdict.db")
client = Anthropic()
# Use Anthropic normally; every call is captured. Judge scoring runs if a key is set.
```

Content capture is **off by default** (a PII surface); enable it with `verdict.init(capture_content=True)`, and prompts/completions are redacted (regex + Luhn) before storage. For high-volume production, `verdict.init(buffered_writes=True)` moves writes to a background batched writer off the request hot path.

## Validation status

Reproduce the checks yourself with the scripts here. The defensible claim is
deliberately narrow:

> **Verdict supports calibrated PASS/FAIL drift monitoring on real LLM traces, with per-workload judge calibration.**

What this repo includes:

- A synthetic regression battery for checking that the capture -> judge -> score
  path catches known injected failures (`scripts/run_regression_injection.py`).
- A pairwise judge-alignment harness for comparing model-ranking judgments
  against human-labeled public data (`scripts/verify_judge_alignment.py`).
- A rubric-alignment harness for measuring PASS/FAIL judge consistency against
  your own labeled traces (`scripts/verify_rubric_alignment.py`).

The shippable number for any given team is **their** held-out, human-labeled task
data measured with the same rubric-alignment harness. Treat public benchmarks as
sanity checks, not proof that a judge is calibrated for your workload.

## Judge calibration workflow

```bash
python scripts/sample_to_label.py --source sqlite --db verdict.db --dedupe-by-prompt --out raw.jsonl
python scripts/verify_rubric_alignment.py --make-template raw.jsonl labels.jsonl
python scripts/label_ui.py --file labels.jsonl          # local labeling UI, autosaves
python scripts/verify_rubric_alignment.py --labeled labels.jsonl --provider anthropic --judge-model <model>
```

You hand-label a sample PASS/FAIL (blind, before the judge runs), then the harness reports per-dimension and pooled agreement with 95 % bootstrap CIs. A threshold is "cleared" only when the **CI lower bound** clears it, not the point estimate.

## Architecture

Hexagonal / ports-and-adapters, ≥2 adapters per port (one real + in-memory for tests). Storage: `SQLiteStorage`, `PostgresStorage`, `InMemoryStorage`, plus a `BufferedStorage` wrapper for async batched writes. Judge providers: Anthropic, OpenAI, Google, and a `FakeProvider` for tests. Capture uses a **vendor-neutral `Trace` schema** (it borrows OpenTelemetry GenAI *attribute names* but does **not** emit OTel/OpenInference spans — an exporter is a v1 roadmap item). See the ADRs in [`docs/adrs/`](docs/adrs/).

## Honest limits / not in v0

- **No OpenTelemetry/OpenInference span emission** yet (vendor-neutral schema today; exporter is planned).
- **Streaming:** Anthropic, OpenAI, and Google modern-SDK streaming are captured and confirmed against live SDKs via `scripts/live_capture_check.py`. Keep that script in the release checklist, because provider SDK stream chunk shapes can drift.
- **`encrypt` redaction mode** is not implemented (rejected at `init()`); redaction is regex + Luhn. Presidio is not used.
- **Agent-run / tool-call tracing** is v1, not built. Manual `@trace` spans *are* persisted, but multi-step agent-run stitching is not.
- Judge quality depends on the model, rubric, and workload; for math/code
  correctness, use stronger judges on samples or deterministic checks.
- This is a **public alpha** release — not a hosted monitoring service and not a substitute for workload-specific calibration.

## License

Apache 2.0 — see [LICENSE](LICENSE).

## Docs

- [`docs/STATS_PRIMER.md`](docs/STATS_PRIMER.md) — plain-language explanation of every statistical method Verdict uses (Fisher's exact, Cliff's δ, Wasserstein, PSI, Benjamini-Hochberg, Cohen's κ / Gwet's AC2, Bradley-Terry) and *why* each was chosen.
- [`docs/EXPLAINER.md`](docs/EXPLAINER.md) — how the pipeline works end to end.
- [`docs/adrs/`](docs/adrs/) — architecture decision records.
- [`docs/v1-roadmap.md`](docs/v1-roadmap.md) — known limits and follow-up work.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a pull request. Major
decisions are documented in [`docs/adrs/`](docs/adrs/), known limits are in
[`docs/v1-roadmap.md`](docs/v1-roadmap.md), and community expectations are in
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
