# Verdict Explainer

Verdict is a local-first observability toolkit for LLM applications and agent
systems. It helps teams capture model calls, evaluate response quality with a
rubric, and look for meaningful changes in behavior over time.

This document is intentionally public-facing. It explains what the project does,
what you can try today, and where to look for implementation details.

## Short Version

Verdict instruments your Python LLM app with a small SDK call. It captures
requests and responses from supported providers, stores traces locally by
default, runs rubric-based evaluation with a judge model you choose, and gives
you tools to inspect quality, cost, and drift across your own traffic.

For agents, Verdict works at the LLM-call layer: planning prompts, tool-selection
prompts, replanning prompts, and final-response prompts can all be captured when
they go through a supported provider SDK.

## What Verdict Does

- Captures Anthropic, OpenAI, and Google GenAI calls through lightweight Python
  instrumentation.
- Supports non-streaming and streaming responses for the supported SDK paths.
- Stores traces in SQLite by default, with Postgres support for deployments that
  need a server database.
- Redacts common sensitive patterns when content capture is enabled.
- Scores responses with a configurable judge model and rubric dimensions such as
  groundedness, relevance, completeness, safety, and instruction following.
- Groups similar prompts so quality changes can be inspected by workload or
  intent instead of only as one global average.
- Provides local inspection tools and a dashboard for exploring traces, judge
  results, and drift signals.
- Includes calibration scripts so users can compare judge decisions against their
  own human labels before relying on alerts.

## How It Works

1. **Capture**: call `verdict.init(...)` in your app. Verdict wraps supported
   provider SDK methods and records request metadata, response metadata, token
   usage, cost estimates, errors, and optional redacted content. `sample_rate`
   controls the retained fraction.
2. **Store**: traces are written through a storage interface. SQLite is the
   default local store; Postgres is available for shared environments. Optional
   buffered writes move persistence to a background batched writer.
3. **Group**: on a pipeline run, prompt embeddings are assigned against a
   persisted cluster registry so existing cluster IDs remain stable. Local
   MiniLM is the semantic default; the explicit hash fallback is lexical.
4. **Evaluate**: the separately invoked batch pipeline selects traces per
   cluster and time window, then scores them with a configured judge and rubric.
   Capture itself does not make judge calls.
5. **Detect**: Verdict compares current and baseline judgments using each
   trace's capture timestamp. It emits per-cluster, per-dimension signals only
   when both statistical and practical thresholds clear, and retains up to five
   current-window trace IDs as review evidence. Re-running the same hourly
   analysis bucket replaces stale results from that bucket.
6. **Inspect**: use the CLI, Python APIs, or dashboard to review traces, scores,
   clusters, and drift reports.

For a visual overview, see `docs/architecture-current.svg`.

## What You Can Try Today

- Run the quickstart in `README.md` to capture real provider traffic.
- Run `scripts/live_capture_check.py` to verify capture against your configured
  providers.
- Use `scripts/sample_to_label.py`, `scripts/label_ui.py`, and
  `scripts/verify_rubric_alignment.py` to measure judge agreement on your own
  labeled examples.
- Use `verdict-inspect` or the dashboard to inspect stored traces and reports.
- Run the test suite before changing instrumentation or evaluation behavior.

## What Verdict Is Good For

Verdict is useful when you need to answer questions like:

- Are model responses getting worse for a specific type of user request?
- Did a prompt, model, or provider change alter response quality?
- Which traces should a human reviewer look at first?
- Is a cheaper judge model consistent enough for this workload?
- Are errors, latency, token usage, or costs changing after a release?

## Current Scope

Verdict v0 focuses on LLM-call observability inside LLM apps and agents. It is
not a full agent runtime, task planner, tool executor, or hosted monitoring
service.

The current implementation is best suited for local evaluation, early pilots,
and teams that want transparent observability primitives they can run and inspect
themselves. Production deployments should validate provider coverage,
storage settings, redaction behavior, retention policy, and judge calibration for
their own traffic before depending on alerts.

The v0 drift runner supports one tenant scope per store and rejects mixed-tenant
analysis. Cost figures are best-effort estimates from a dated static table of
public base token prices, not provider billing data.

## Security And Privacy Notes

- Content capture is opt-in. You can capture metadata without storing prompts or
  responses.
- Redaction is best-effort pattern redaction, not a compliance guarantee.
- API keys are read from your environment and should not be committed to the
  repository.
- Structural checks and local embedding inference keep trace content local. A
  provider-backed judge sends the sampled prompt/response content to the
  configured provider under that provider's data-handling terms.
- Local SQLite files and generated reports may contain sensitive trace data.
  Keep them out of source control.
- Before using Verdict with production data, review `SECURITY.md`, your
  retention requirements, and your provider data-handling settings.

## Validation Position

Verdict ships with tests and reproducible scripts for capture, storage,
evaluation, judge alignment, rubric calibration, and drift behavior. These are
intended to make claims inspectable rather than magical.

The most important validation step is workload-specific calibration: label a
sample of your own traces, run the rubric alignment workflow, and use the
resulting agreement numbers to decide which dimensions are trustworthy for your
application.

## Repository Map

- `README.md`: quickstart, install options, and common workflows.
- `docs/ONBOARDING.md`: a more complete first-run walkthrough.
- `docs/architecture-current.svg`: current architecture diagram.
- `docs/adrs/`: architecture decision records for major design choices.
- `docs/v1-roadmap.md`: planned work and known product boundaries.
- `packages/verdict/`: SDK, instrumentation, storage, and core APIs.
- `packages/verdict_eval/`: evaluation, clustering, and drift logic.
- `packages/verdict_inspect/`: inspection CLI and report tools.
- `ui/`: local dashboard.
