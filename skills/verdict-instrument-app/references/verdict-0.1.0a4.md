# Verdict `0.1.0a4` compatibility map

Use this reference only for public Verdict `main` at commit
`49eae0a67d471b087d7c146c5abbd215e723f3ad`. Re-inspect a newer release before
carrying these claims forward.

## Installation identity

- Python: 3.10 through 3.12.
- PyPI distributions: `cognifity-verdict`, `cognifity-verdict-eval`, and
  `cognifity-verdict-inspect`.
- Python imports: `verdict`, `verdict_eval`, and `verdict_inspect`.
- Do not install the unrelated PyPI distribution named `verdict`. Detect the
  namespace collision before testing.
- The drift pipeline, probe runner, and dashboard are repository-local files. A
  package-only install does not supply `scripts/run_drift_pipeline.py`,
  `scripts/run_probes.py`, or `ui/server.py`; use a pinned source checkout for them.
- A native agent-skill install does not supply those repository files either. Resolve
  the source checkout separately and run the skill's
  `scripts/verify_verdict_checkout.py` before using it.
- The checkout verifier accepts a shallow clone only when every runtime path matches
  its pinned immutable Git-object manifest. A present release tag must still resolve
  to the expected commit. It independently hashes checked-out runtime bytes, rejects
  hidden index flags, and performs no implicit fetch.

## Released auto-instrumentors

Verdict capture is Python-only in this release.

| Provider SDK | Supported calls | Important unsupported calls |
|---|---|---|
| Anthropic | sync/async `client.messages.create(...)`, including `stream=True` | `client.messages.stream(...)` |
| OpenAI | sync/async `client.chat.completions.create(...)`, including `stream=True`, and `client.chat.completions.stream(...)` | Responses API and other OpenAI endpoints |
| Google `google-genai` | sync/async `client.models.generate_content(...)`, `generate_content_stream(...)` | other models methods |
| Legacy Google Generative AI | `GenerativeModel.generate_content(...)` | other legacy methods |

Verify exact runtime behavior against the pinned source. Similar method names in
JavaScript or TypeScript are not supported by these Python instrumentors.

For OpenAI streaming, this release records accumulated content, usage when the caller
requests it, and finish reason, but leaves `response_model` at the requested model
instead of adopting a different model reported by stream chunks. Treat that field as
request-model evidence on this path.

## Initialization and capture

- `verdict.init(...)` installs process-wide instrumentation. Run it once per process
  before the first supported provider call, not once per request.
- Relevant configuration includes `service_name`, `environment`, `storage`,
  `capture_content`, `redaction_mode`, `redaction_secret`, `sample_rate`, `tenant_id`,
  `instrumentors`, and `buffered_writes`.
- `service_name` and `environment` are client configuration/log fields in this
  release, but they are not columns on the persisted `Trace`. Do not promise stored
  service/environment filtering. Use separate stores for environment/service
  isolation unless a separately designed schema or routing layer is approved.
- `capture_content` defaults to false. Quality clustering and judging require stored
  prompt and response content, so metadata-only capture cannot produce those drift
  results.
- Chat instrumentors flatten captured message content into one newline-joined
  `prompt_redacted` string. OpenAI message arrays can therefore include system text.
  Anthropic's separate `system=` argument is not captured by `0.1.0a4`, and Google's
  separate system-instruction configuration is not part of its captured `contents`.
  The released drift pipeline has no role-aware feature selector. Shared boilerplate
  can dominate one provider while a task-defining system field can be absent from
  another. Define the desired intent feature and validate cluster semantics before
  judging. Any separate projection or external assignment is a custom unverified
  adapter, not released pipeline behavior.
- Content redaction is best effort. It is not a compliance boundary and is known to
  have broad gaps such as names, postal addresses, dates of birth, many international
  identifiers, opaque secrets/tokens, and opaque metadata values.
- Encryption mode is not supported in this release.
- Default to synchronous writes for a local trial. Buffered writes require an
  explicit, exercised `shutdown()` imported from `verdict.client` and still need
  failure testing. `shutdown` is not exported as `verdict.shutdown` in this release.

## Storage

- Released stores: memory, SQLite, and Postgres; `BufferedStorage` can wrap a store.
- Prefer an absolute SQLite path and verify the resolved file on the customer's
  operating system. Relative paths depend on each process working directory and can
  silently split data. The four-slash example in the skill is POSIX-specific.
- Local or ephemeral filesystems can lose traces and are not a shared-store design.
- Use one tenant scope per analysis store. The drift runner rejects mixed tenants.
- Storage exposes `prune_before(cutoff_iso)` for retention, but Verdict does not
  schedule retention jobs.
- The bundled dashboard reads SQLite directly. It does not read Postgres.
- The drift runner prints the supplied `--storage` value. Passing a Postgres URL
  containing credentials can leak it to logs. Do not use a credential-bearing URL
  with that runner in this release; treat Postgres capture and downstream analysis as
  separate until a secret-safe connection method is verified.

## Batch evaluation and signals

- `scripts/run_drift_pipeline.py` compares a current window (default 24 hours) with a
  baseline (default 7 days) separated by a default 24-hour lag.
- The drift pipeline selects its store with `--storage`. The dashboard server selects
  its SQLite file with `--db`; these flags are not interchangeable.
- Key defaults include minimum sample 30, p-value threshold 0.01, Cliff's delta
  threshold 0.147, stratified target 40, sentence-transformer clustering, and cluster
  version `v2`. Defaults are starting points, not universal policy.
- Live judges support Anthropic, OpenAI, and Google. The fake judge is wiring-only
  evidence. Calls are sequential and the release does not enforce a spend budget.
  `run_drift_pipeline.py` does not accept `--yes-spend` or
  `--max-spend-usd`; enforce authorization, call ceilings, and cost controls
  outside the runner.
- Keep one evaluator identity and rubric per comparable analysis series.
- The pipeline atomically persists the latest `DriftRun`, including a zero-signal run.
- Repeated turns from the same conversation are correlated; do not present them as
  independent statistical evidence.
- `--trust-existing-clusters` accepts a stable external assignment only when every
  judgeable trace already has a cluster ID. It is not permission to skip trace parity,
  provenance, semantic validation, or clustering-version discipline.

## Dashboard and probes

- `ui/server.py` serves `/api/health`, `/api/data`, `/`, and `/dashboard`.
- `VERDICT_USER` and `VERDICT_PASS` gate dashboard/data access when configured.
- Both variables must be non-empty; setting only one leaves the gate disabled.
  `/` and `/api/health` remain public even when the gate is enabled.
- Bind to localhost by default. A remotely exposed dashboard needs authentication and
  a trusted TLS/reverse-proxy boundary.
- The dashboard displays persisted drift signals. It neither configures schedules nor
  sends outbound notifications.
- `scripts/run_probes.py` is a separate path that returns JSON and exit codes: `0`
  pass, `1` gate failure, and `2` execution error. Probe results do not automatically
  become dashboard signals.

## Release-risk posture

Treat this release as public-alpha/POC software. A successful synthetic trial proves
only the exercised path. It does not prove production durability, comprehensive
redaction, statistical validity for a different workload, or support for an
unexercised provider or deployment model.
